"""Native (C) build of the SCC68070 core, loaded with ctypes.

The C source is not written by hand: every handler template that m68k.py
generates as Python is translated to C here (with Python's ast module), and so
are the small runtime helpers (flags, shifts, BCD, exceptions).  Only the run
loop, the bus error frame and memory access are hand-written, in
native/core.c, and they mirror RUNTIME in m68k.py line by line.  Both cores
therefore implement the same instruction semantics; tools/run_sst.py and
tools/check_fixes.py run against either.

The library is compiled with the system C compiler the first time it is needed
(or by tools/build_deb.sh) and cached under a name that includes a hash of
its source, so a changed m68k.py always gets a matching build.  Values are
int64_t throughout, compiled with -fwrapv, which matches Python's integer
arithmetic for the ranges the handlers use (every result that is kept is
masked to 32 bits).

ctypes releases the GIL while the core runs, so the emulated CPU no longer
competes with the window thread for the interpreter.  Device registers are
reached through callbacks into Python.
"""

import ast
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zlib
from array import array
from pathlib import Path

from . import debuglog
from . import m68k
from .m68k import AddressError, BusError, CpuBreak

dlog = debuglog.get('cpu')

HERE = Path(__file__).with_name('native')
CORE_C = HERE / 'core.c'
VIDEO_C = HERE / 'video.c'      # picture helpers used by mcd212.py
FLAC_C = HERE / 'flac.c'        # FLAC decoder for CHD audio hunks (chd.py)
LZMA_C = HERE / 'lzma.c'        # LZMA decoder for CHD hunks (the Android Python has no lzma module)
NATIVE_C = (CORE_C, VIDEO_C, FLAC_C, LZMA_C)

# struct fields shared with C (all int64), in order
FIELDS = ('ccr', 'srh', 'usp', 'ssp', 'PC', 'vbr', 'xc', 'ipl', 'nmi_edge', 'stopped', 'halted',
          'FRAME_FORMAT', 'TRACE', 'fault_addr', 'fault_write', 'fault_vector', 'abort_kind')
GLOBALS = {'ccr', 'srh', 'usp', 'ssp', 'PC', 'vbr', 'xc', 'ipl', 'nmi_edge', 'stopped', 'halted',
           'FRAME_FORMAT', 'TRACE'}
# runtime helpers translated from m68k.RUNTIME; the rest of RUNTIME is in core.c
HELPERS = ('ix', 'irq_pending', 'set_sr', 'push16', 'push32', 'enter_exception', 'exception', 'priv',
           'rte', 'take_interrupt', '_nz', 'sbcd', 'abcd', 'nbcd',
           'sh_asl', 'sh_asr', 'sh_lsl', 'sh_lsr', 'sh_rol', 'sh_ror', 'sh_roxl', 'sh_roxr')
CALLS = {'rb': 'mem_rb', 'rw': 'mem_rw', 'rl': 'mem_rl', 'wb': 'mem_wb', 'ww': 'mem_ww', 'wl': 'mem_wl',
         'brk': 'cpu_brk', 'abs': 'llabs', 'reset_devices': 'cb_reset_devices', 'iack': 'cb_iack',
         'bool': '!!'}
# abort kinds reported in abort_kind when an exception leaves run() (as in Python)
ABORT_BREAK, ABORT_BUS, ABORT_INDEX, ABORT_PYTHON = 1, 2, 3, 4


class Unsupported(Exception):
    pass


class Translator:
    """Python handler/helper source (the subset m68k.py emits) -> C."""

    def __init__(self, helpers):
        self.helpers = helpers          # name -> 'pair' | 'i64' | 'void'
        self.args_struct = False

    def function(self, fn, name, params, args_struct=False):
        # Python scoping: parameters and assigned names are local unless
        # declared global (sh_asl's ccr parameter shadows the global ccr)
        self.params = set(params)
        self.args_struct = args_struct
        self.declared = {g for n in ast.walk(fn) if isinstance(n, ast.Global) for g in n.names}
        self.locals = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                if n.id not in self.declared and n.id not in self.params:
                    self.locals.add(n.id)
        body = []
        self.block(fn.body, body, 1)
        rtype = self.return_type(fn)
        decl = ''
        loc = sorted(self.locals - {'_p'})
        if loc:
            decl += '    i64 ' + ', '.join(f'{v} = 0' for v in loc) + ';\n'
        if '_p' in self.locals:
            decl += '    const uint8_t *_p = 0;\n'
        if args_struct:
            sig = f'static i64 {name}(i64 pc, const opargs *A)'
        else:
            sig = f'static {rtype} {name}(' + (', '.join(f'i64 {p}' for p in params) or 'void') + ')'
        tail = ''
        if rtype != 'void' and not (fn.body and isinstance(fn.body[-1], ast.Return)):
            # Python falls off the end only after brk()/raise, which never return
            tail = '    return ' + ('(pair){0, 0}' if rtype == 'pair' else '0') + ';\n'
        return sig + ' {\n' + decl + '\n'.join(body) + '\n' + tail + '}\n'

    def return_type(self, fn):
        if self.args_struct:
            return 'i64'
        for n in ast.walk(fn):
            if isinstance(n, ast.Return) and n.value is not None:
                v = n.value
                if isinstance(v, ast.Tuple):
                    return 'pair'
                if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and self.helpers.get(v.func.id) == 'pair':
                    return 'pair'               # nbcd returns sbcd(...)
                return 'i64'
        return 'void'

    # ----- statements --------------------------------------------------------
    def block(self, stmts, out, ind):
        for st in stmts:
            self.stmt(st, out, ind)

    def stmt(self, st, out, ind):
        pad = '    ' * ind
        if isinstance(st, ast.Global):
            return
        if isinstance(st, ast.Assign):
            if len(st.targets) != 1:
                raise Unsupported(ast.unparse(st))
            tgt = st.targets[0]
            if isinstance(tgt, ast.Tuple):
                if isinstance(st.value, ast.Call):
                    out.append(f'{pad}{{ pair _t = {self.expr(st.value)}; '
                               f'{self.lvalue(tgt.elts[0])} = _t.a; {self.lvalue(tgt.elts[1])} = _t.b; }}')
                elif isinstance(st.value, ast.Tuple):
                    vals = [self.expr(v) for v in st.value.elts]
                    tmp = ' '.join(f'i64 _t{i} = {v};' for i, v in enumerate(vals))
                    asg = ' '.join(f'{self.lvalue(t)} = _t{i};' for i, t in enumerate(tgt.elts))
                    out.append(f'{pad}{{ {tmp} {asg} }}')
                else:
                    raise Unsupported(ast.unparse(st))
                return
            out.append(f'{pad}{self.lvalue(tgt)} = {self.expr(st.value)};')
            return
        if isinstance(st, ast.AugAssign):
            fake = ast.BinOp(left=_load(st.target), op=st.op, right=st.value)
            out.append(f'{pad}{self.lvalue(st.target)} = {self.expr(fake)};')
            return
        if isinstance(st, ast.Expr):
            if isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                return                                   # docstring
            if not isinstance(st.value, ast.Call):
                raise Unsupported(ast.unparse(st))
            out.append(f'{pad}{self.expr(st.value)};')
            return
        if isinstance(st, ast.Return):
            if st.value is None:
                out.append(f'{pad}return;')
            elif isinstance(st.value, ast.Tuple):
                a, b = (self.expr(v) for v in st.value.elts)
                out.append(f'{pad}return (pair){{{a}, {b}}};')
            else:
                out.append(f'{pad}return {self.expr(st.value)};')
            return
        if isinstance(st, ast.If):
            out.append(f'{pad}if ({self.expr(st.test)}) {{')
            self.block(st.body, out, ind + 1)
            if st.orelse:
                out.append(f'{pad}}} else {{')
                self.block(st.orelse, out, ind + 1)
            out.append(f'{pad}}}')
            return
        if isinstance(st, ast.While) and not st.orelse:
            out.append(f'{pad}while ({self.expr(st.test)}) {{')
            self.block(st.body, out, ind + 1)
            out.append(f'{pad}}}')
            return
        raise Unsupported(ast.unparse(st))

    def lvalue(self, t):
        if isinstance(t, ast.Name):
            return self.name(t.id)
        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == 'R':
            return f'C->R[{self.expr(t.slice)}]'
        raise Unsupported(ast.unparse(t))

    # ----- expressions -------------------------------------------------------
    def name(self, n):
        if n in self.params or n in self.locals:
            return n
        if self.args_struct and n in ('X', 'Y', 'Z'):
            return f'A->{n.lower()}'
        if n in GLOBALS:
            return f'C->{n}'
        raise Unsupported(f'name {n}')

    def expr(self, e):
        if isinstance(e, ast.Constant):
            if isinstance(e.value, bool):
                return '1' if e.value else '0'
            if isinstance(e.value, int):
                return f'INT64_C({e.value})'
            raise Unsupported(repr(e.value))
        if isinstance(e, ast.Name):
            return self.name(e.id)
        if isinstance(e, ast.BinOp):
            l, r = self.expr(e.left), self.expr(e.right)
            op = type(e.op)
            if op is ast.LShift:
                if isinstance(e.right, ast.Constant) and e.right.value < 64:
                    return f'((i64)((uint64_t)({l}) << {e.right.value}))'
                return f'shl64({l}, {r})'
            if op is ast.RShift:
                if isinstance(e.right, ast.Constant) and e.right.value < 64:
                    return f'({l} >> {e.right.value})'
                return f'shr64({l}, {r})'
            if op is ast.FloorDiv:
                return f'floordiv64({l}, {r})'
            if op is ast.Mod:
                return f'floormod64({l}, {r})'
            sym = {ast.BitAnd: '&', ast.BitOr: '|', ast.BitXor: '^', ast.Add: '+', ast.Sub: '-',
                   ast.Mult: '*'}.get(op)
            if sym is None:
                raise Unsupported(ast.unparse(e))
            return f'({l} {sym} {r})'
        if isinstance(e, ast.UnaryOp):
            v = self.expr(e.operand)
            sym = {ast.USub: '-', ast.Invert: '~', ast.Not: '!'}[type(e.op)]
            return f'({sym}{v})'
        if isinstance(e, ast.BoolOp):
            sym = ' && ' if isinstance(e.op, ast.And) else ' || '
            return '(' + sym.join(f'({self.expr(v)} != 0)' for v in e.values) + ')'
        if isinstance(e, ast.IfExp):
            return f'(({self.expr(e.test)}) ? {self.expr(e.body)} : {self.expr(e.orelse)})'
        if isinstance(e, ast.Compare):
            parts = []
            left = e.left
            for op, right in zip(e.ops, e.comparators):
                sym = {ast.Lt: '<', ast.LtE: '<=', ast.Gt: '>', ast.GtE: '>=', ast.Eq: '==',
                       ast.NotEq: '!='}[type(op)]
                parts.append(f'({self.expr(left)} {sym} {self.expr(right)})')
                left = right
            return '(' + ' && '.join(parts) + ')'
        if isinstance(e, ast.Subscript) and isinstance(e.value, ast.Name):
            base, idx = e.value.id, self.expr(e.slice)
            if base == 'R':
                return f'C->R[{idx}]'
            if base == 'T' and self.args_struct:
                return f'((i64)A->t[{idx}])'
            if base == 'FP':
                return f'fetch_page({idx})'
            if base == '_p':
                return f'((i64)_p[{idx}])'
            raise Unsupported(ast.unparse(e))
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Name):
            fn = e.func.id
            args = ', '.join(self.expr(a) for a in e.args)
            if fn == 'bool':
                return f'({args} != 0)'
            if fn in CALLS:
                return f'{CALLS[fn]}({args})'
            if fn in self.helpers:
                return f'h_{fn}({args})'
            raise Unsupported(f'call {fn}')
        raise Unsupported(ast.unparse(e))


def _load(t):
    if isinstance(t, ast.Name):
        return ast.Name(id=t.id, ctx=ast.Load())
    if isinstance(t, ast.Subscript):
        return ast.Subscript(value=t.value, slice=t.slice, ctx=ast.Load())
    raise Unsupported(ast.unparse(t))


def opcode_tables():
    """(templates in id order, per-opcode template id, args, cycles) from m68k.py."""
    cyc = zlib.decompress((Path(m68k.__file__).with_name('data') / 'cycles070.bin').read_bytes())
    ids = {}
    templates = []
    tid = [0] * 65536
    args = [(0, 0, 0, 0)] * 65536
    cycles = [0] * 65536
    illegal = 'def op(pc):\n    return exception(4, pc - 2)'
    for op in range(65536):
        res = m68k.gen_opcode(op) if cyc[op] else None
        if res is None:
            src, a, c = illegal, (), 20
        else:
            (src, a), c = res, cyc[op]
        if src not in ids:
            ids[src] = len(templates)
            templates.append(src)
        tid[op] = ids[src]
        x = list(a) + [0] * 4
        t = 0
        if len(a) == 4:
            t = m68k.COND.index(a[3])
        elif len(a) == 3 and isinstance(a[2], tuple):
            t = m68k.COND.index(a[2])
            x[2] = 0
        args[op] = (x[0], x[1], x[2] if not isinstance(x[2], tuple) else 0, t)
        cycles[op] = c
    return templates, tid, args, cycles


def generate_c(templates):
    """The C source of the handler templates and the translated helpers."""
    rt = ast.parse(m68k.RUNTIME)
    funcs = {f.name: f for f in rt.body if isinstance(f, ast.FunctionDef)}
    kinds = {}
    tr = Translator(kinds)
    for name in HELPERS:
        kinds[name] = tr.return_type(funcs[name])
    protos, bodies = [], []
    for name in HELPERS:
        f = funcs[name]
        code = tr.function(f, f'h_{name}', [a.arg for a in f.args.args])
        protos.append(code.split(' {')[0] + ';')
        bodies.append(code)
    out = ['/* generated by opencdi/m68k_native.py from opencdi/m68k.py: do not edit */'] + protos + bodies
    for i, src in enumerate(templates):
        fn = ast.parse(src).body[0]
        out.append(tr.function(fn, f't{i}', ['pc'], args_struct=True))
    out.append(f'static const handler_fn TEMPLATES[{len(templates)}] = {{' +
               ', '.join(f't{i}' for i in range(len(templates))) + '};')
    out.append(f'#define N_TEMPLATES {len(templates)}')
    return '\n'.join(out) + '\n'


class _State(ctypes.Structure):
    _fields_ = [('R', ctypes.c_int64 * 16)] + [(f, ctypes.c_int64) for f in FIELDS]


IOR = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int64, ctypes.c_int, ctypes.POINTER(ctypes.c_int64))
IOW = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int64, ctypes.c_int, ctypes.c_int64)
IACK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int64, ctypes.POINTER(ctypes.c_int64))
VOIDCB = ctypes.CFUNCTYPE(ctypes.c_int)
PCOP = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int64, ctypes.c_int64)

_lib = None
_tables = None
_load_error = None


def cache_dirs():
    yield HERE
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    yield Path(base) / 'opencdi'


def sources():
    """Everything a build needs: (library name, {file: text}, tables).
    tables = (template count, template id per opcode, flat args, cycles)."""
    templates, tid, args, cycles = opcode_tables()
    ops_c = generate_c(templates)
    files = {f.name: f.read_text() for f in NATIVE_C}
    digest = hashlib.sha256((''.join(files.values()) + ops_c).encode()).hexdigest()[:16]
    files['ops.inc'] = ops_c
    return f'_m68k_{digest}.so', files, (len(templates), tid, [v for a in args for v in a], cycles)


def compile_command(out, src_dir, cc=None):
    cc = cc or os.environ.get('CC', 'cc')
    return [cc, '-O2', '-fwrapv', '-fno-strict-aliasing', '-shared', '-fPIC', '-o', str(out)] + \
        [str(Path(src_dir) / f.name) for f in NATIVE_C]


def inputs_digest():
    """Hash of every file the native build is made from (see prebuilt.json)."""
    h = hashlib.sha256()
    for f in (Path(m68k.__file__), Path(__file__)) + NATIVE_C + (
              Path(m68k.__file__).with_name('data') / 'cycles070.bin',):
        h.update(f.read_bytes())
    return h.hexdigest()


def write_prebuilt(dest_dir, so_name, tables):
    """Record a finished build in dest_dir/prebuilt.json, with the opcode
    tables, so later starts skip generating 65,536 handlers (seconds on a phone)."""
    n, tid, args, cycles = tables
    blob = (array('H', tid).tobytes() + array('q', args).tobytes() + array('i', cycles).tobytes())
    stem = so_name[:-3]
    (Path(dest_dir) / f'{stem}.tables').write_bytes(zlib.compress(blob, 9))
    (Path(dest_dir) / 'prebuilt.json').write_text(json.dumps(
        {'so': so_name, 'tables': f'{stem}.tables', 'templates': n, 'inputs': inputs_digest()}))


def _prebuilt():
    """(path, tables) of a recorded build that matches the current sources, or None."""
    try:
        current = inputs_digest()
    except OSError:
        # sources not readable (an app packaged without .py files): only the
        # Android build, whose library always comes with the app, is trusted then
        current = None
    for d in cache_dirs():
        try:
            info = json.loads((d / 'prebuilt.json').read_text())
            if not (d / info['so']).exists():
                continue
            if info['inputs'] != current and not (current is None and os.environ.get('OPENCDI_ANDROID') == '1'):
                continue
            blob = zlib.decompress((d / info['tables']).read_bytes())
            tid = array('H'); tid.frombytes(blob[:2 * 65536])
            args = array('q'); args.frombytes(blob[2 * 65536:2 * 65536 + 32 * 65536])
            cycles = array('i'); cycles.frombytes(blob[34 * 65536:])
            if len(cycles) != 65536:
                continue
            return d / info['so'], (info['templates'], tid, args, cycles)
        except (OSError, ValueError, KeyError, zlib.error):
            continue
    return None


def build(dest_dir=None, quiet=False):
    """Generate and compile the library; returns (path, tables)."""
    if dest_dir is None:
        hit = _prebuilt()
        if hit is not None:
            return hit
    name, files, tables = sources()
    dirs = [Path(dest_dir)] if dest_dir else list(cache_dirs())
    for d in dirs:
        if (d / name).exists():
            return d / name, tables
    last = None
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=d) as tmp:
                for fn, text in files.items():
                    (Path(tmp) / fn).write_text(text)
                out = Path(tmp) / name
                cmd = compile_command(out, tmp)
                if not quiet:
                    print(f'building the native CPU core ({tables[0]} templates), this takes a minute...',
                          flush=True)
                dlog.info('compiling native CPU core: %s', ' '.join(cmd))
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                os.replace(out, d / name)
            for old in list(d.glob('_m68k_*.so')) + list(d.glob('_m68k_*.tables')):
                if old.stem != name[:-3]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            try:
                write_prebuilt(d, name, tables)
            except OSError:
                pass
            return d / name, tables
        except (OSError, subprocess.CalledProcessError) as e:
            last = e
            detail = getattr(e, 'stderr', '') or ''
            dlog.warning('could not build the native CPU core in %s: %s %s', d, e, detail[-2000:])
    raise RuntimeError(f'native CPU core could not be built: {last}')


def load():
    """The loaded library, or None (then the Python core is used).
    OPENCDI_CPU=python also turns off the native picture helpers."""
    global _lib, _tables, _load_error
    if _lib is not None or _load_error is not None:
        return _lib
    try:
        path, tables = build()
        lib = ctypes.CDLL(str(path))
        lib.cpu_run.restype = ctypes.c_int64
        lib.cpu_run.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        lib.cpu_setup.restype = ctypes.c_void_p
        lib.cpu_free.argtypes = [ctypes.c_void_p]
        lib.cpu_n_templates.restype = ctypes.c_int
        vp, ci, i64 = ctypes.c_void_p, ctypes.c_int, ctypes.c_int64
        lib.vid_dyuv.restype = None
        lib.vid_dyuv.argtypes = [vp, vp, ci, ci, i64, vp, vp, vp, vp, vp, vp, ctypes.c_uint32, vp]
        lib.flac_decode_cd.restype = ctypes.c_long
        lib.flac_decode_cd.argtypes = [ctypes.c_char_p, ctypes.c_long, vp, ci]
        lib.lzma_decode_raw.restype = ctypes.c_long
        lib.lzma_decode_raw.argtypes = [ctypes.c_char_p, ctypes.c_long, vp, ctypes.c_long]
        lib.vid_mix.restype = None
        lib.vid_mix.argtypes = [vp, vp, vp, vp, ci, ci, vp, vp, ci, ci, ctypes.c_uint32, vp, vp, ci,
                                ctypes.c_uint32, vp]
        n_templates, tid, args, cycles = tables
        if lib.cpu_n_templates() != n_templates:
            raise RuntimeError('native core does not match m68k.py')
        n = 65536
        t_arr = (ctypes.c_uint16 * n)(*tid)
        flat = (ctypes.c_int64 * (4 * n))(*args)
        c_arr = (ctypes.c_int32 * n)(*cycles)
        cond = (ctypes.c_uint8 * 512)(*[int(b) for t in m68k.COND for b in t])
        if lib.cpu_init_tables(t_arr, flat, c_arr, cond) != 0:
            raise RuntimeError('native core rejected the opcode tables')
        _lib, _tables = lib, tables
        dlog.info('native CPU core loaded from %s', path)
    except Exception as e:  # noqa: BLE001 - any failure falls back to the Python core
        _load_error = e
        dlog.warning('native CPU core not available, using the Python core: %s', e)
        print(f'native CPU core not available ({e}); using the slower Python core')
    return _lib


class _NS:
    """cpu.ns compatible view of the native state (see m68k.M68070.ns)."""

    CALLBACKS = ('iack', 'reset_devices', 'log_fault', 'log_trace')

    def __init__(self, cpu):
        self._cpu = cpu

    def __getitem__(self, k):
        cpu = self._cpu
        if k == 'R':
            return cpu.st.R
        if k in FIELDS:
            return getattr(cpu.st, k)
        if k in self.CALLBACKS:
            return cpu.cb[k]
        if k == 'run':
            return cpu.run
        if k in ('rb', 'rw', 'rl', 'wb', 'ww', 'wl'):
            return getattr(cpu.bus, k)
        raise KeyError(k)

    def __setitem__(self, k, v):
        cpu = self._cpu
        if k in FIELDS:
            setattr(cpu.st, k, int(v))
        elif k in self.CALLBACKS:
            cpu.cb[k] = v
        else:
            raise KeyError(k)

    def get(self, k, default=None):
        try:
            return self[k]
        except KeyError:
            return default


class NativeM68070:
    """Same interface as m68k.M68070, running the C core."""

    native = True

    def __init__(self, bus, lib):
        self.bus = bus
        self.lib = lib
        bus.cpu_functions()          # bus.rl etc., used by reset()
        self.st = _State()
        self.st.srh = 0x2700
        self.st.FRAME_FORMAT = 1
        self.st.TRACE = 1
        self.cb = {'iack': lambda level: 24 + level, 'reset_devices': lambda: None,
                   'log_fault': lambda pc, op, exc: None, 'log_trace': lambda pc, op: None}
        self._pending = None
        self.ns = _NS(self)
        self.templates = _tables[0]
        # page tables: pointers into the bus's bytearrays (kept alive by the bus)
        self._keep = []
        ptr = {}

        def addr(page):
            if page is None:
                return None
            k = id(page)
            if k not in ptr:
                buf = (ctypes.c_char * len(page)).from_buffer(page)
                self._keep.append(buf)
                ptr[k] = ctypes.addressof(buf)
            return ptr[k]
        mk = ctypes.c_void_p * 65536
        self._rp = mk(*[addr(p) for p in bus.rp])
        self._wp = mk(*[addr(p) for p in bus.wp])
        self._fp = mk(*[addr(p) for p in bus.fp])
        self._cbs = (IOR(self._io_read), IOW(self._io_write), IACK(self._iack), VOIDCB(self._reset_devices),
                     PCOP(self._log_fault), PCOP(self._log_trace))
        self._h = lib.cpu_setup(ctypes.byref(self.st), self._rp, self._wp, self._fp, *self._cbs)
        if not self._h:
            raise RuntimeError('native core setup failed')
        self._run = lib.cpu_run

    def __del__(self):
        h, self._h = getattr(self, '_h', None), None
        if h:
            self.lib.cpu_free(h)

    # ----- callbacks from C: every Python exception becomes a return code ----
    def _fail(self, e):
        if isinstance(e, BusError):
            self.st.fault_addr = e.address & 0xffffffffffff
            self.st.fault_write = 1 if e.write else 0
            self.st.fault_vector = e.vector
            return 1
        self._pending = e
        return 2 if isinstance(e, IndexError) else 3

    def _io_read(self, a, size, out):
        try:
            out[0] = self.bus.io_r8(a) if size == 1 else self.bus.io_r16(a)
            return 0
        except BaseException as e:  # noqa: BLE001
            return self._fail(e)

    def _io_write(self, a, size, v):
        try:
            if size == 1:
                self.bus.io_w8(a, v)
            else:
                self.bus.io_w16(a, v)
            return 0
        except BaseException as e:  # noqa: BLE001
            return self._fail(e)

    def _iack(self, level, out):
        try:
            out[0] = self.cb['iack'](level)
            return 0
        except BaseException as e:  # noqa: BLE001
            return self._fail(e)

    def _reset_devices(self):
        try:
            self.cb['reset_devices']()
            return 0
        except BaseException as e:  # noqa: BLE001
            return self._fail(e)

    def _log_fault(self, pc, op):
        # the IndexError came from a device callback (kept in _pending) or from
        # an address past the 32-bit page table inside the core
        exc, self._pending = self._pending, None
        if exc is None:
            exc = IndexError('page index out of range (native core)')
        try:
            self.cb['log_fault'](pc, op, exc)
            return 0
        except BaseException as e:  # noqa: BLE001
            return self._fail(e)

    def _log_trace(self, pc, op):
        try:
            self.cb['log_trace'](pc, op)
            return 0
        except BaseException as e:  # noqa: BLE001
            return self._fail(e)

    # ----- running ------------------------------------------------------------
    def run(self, cycles):
        used = self._run(self._h, cycles)
        if used < 0:
            self._raise_abort()
        return used

    def _raise_abort(self):
        kind = self.st.abort_kind
        pending, self._pending = self._pending, None
        if kind == ABORT_PYTHON and pending is not None:
            raise pending
        if kind == ABORT_INDEX:
            raise pending if isinstance(pending, IndexError) else IndexError('page index out of range')
        if kind == ABORT_BUS:
            cls = AddressError if self.st.fault_vector == 3 else BusError
            raise cls(self.st.fault_addr, bool(self.st.fault_write))
        raise CpuBreak()

    # ----- the accessors of m68k.M68070 ----------------------------------------
    def reset(self):
        st = self.st
        st.srh = 0x2700
        st.ccr = 0
        st.vbr = 0
        st.stopped = 0
        st.halted = 0
        st.xc = 0
        st.R[15] = self.bus.rl(0)
        st.ssp = st.R[15]
        st.PC = self.bus.rl(4)

    @property
    def pc(self):
        return self.st.PC

    @pc.setter
    def pc(self, v):
        self.st.PC = v & m68k.M32

    @property
    def sr(self):
        return self.st.srh | self.st.ccr

    def set_ipl(self, level):
        if level == 7 and self.st.ipl != 7:
            self.st.nmi_edge = 1
        self.st.ipl = level

    @property
    def supervisor(self):
        return bool(self.st.srh & 0x2000)

    def state(self):
        R = self.st.R
        d = ' '.join(f'D{i}={R[i]:08x}' for i in range(8))
        a = ' '.join(f'A{i}={R[8 + i]:08x}' for i in range(8))
        return f'PC={self.st.PC:08x} SR={self.sr:04x}\n{d}\n{a}'


def create_cpu(bus, native=None):
    """The native core when it can be loaded, else the Python one.
    OPENCDI_CPU=python (or native=False) forces the Python core."""
    if native is None:
        native = os.environ.get('OPENCDI_CPU', 'native').lower() != 'python'
    if native:
        lib = load()
        if lib is not None:
            return NativeM68070(bus, lib)
    return m68k.M68070(bus)


if __name__ == '__main__':
    # python -m opencdi.m68k_native [DIR]        build for this machine (tools/build_deb.sh)
    # python -m opencdi.m68k_native --sources DIR   write the C sources, print the library name
    # python -m opencdi.m68k_native --record DIR    record DIR/<library> in DIR/prebuilt.json
    #                                               (tools/build_apk.sh, after cross-compiling)
    if len(sys.argv) == 3 and sys.argv[1] == '--sources':
        so_name, files, _ = sources()
        for fn, text in files.items():
            (Path(sys.argv[2]) / fn).write_text(text)
        print(so_name)
    elif len(sys.argv) == 3 and sys.argv[1] == '--record':
        so_name, _, tables = sources()
        if not (Path(sys.argv[2]) / so_name).exists():
            sys.exit(f'{sys.argv[2]}/{so_name} not found')
        write_prebuilt(sys.argv[2], so_name, tables)
        print(so_name)
    else:
        p, _ = build(sys.argv[1] if len(sys.argv) > 1 else None)
        print(p)
