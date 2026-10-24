"""Philips SCC68070 CPU core (68000 instruction set, 68010-style exception frames).

The core is generated: for every one of the 65536 opcodes a small Python
function is produced from a source template.  Templates are deduplicated, so
only a few thousand are compiled; register numbers and constants are bound
through closures.  All handlers share one globals dictionary (``self.ns``)
that holds the register file and the bus functions, which keeps the hot paths
free of attribute lookups.

Handler contract: ``handler(pc) -> pc``.  ``pc`` points just past the opcode
word on entry; the handler fetches its extension words, executes and returns
the address of the next instruction.

Behaviour follows the 68000 programmer's reference and MAME's Musashi core
(configured as SCC68070).  Cycle counts come from data/cycles070.bin, which is
extracted from MAME's opcode table (see tools/extract_cycles.py).
"""

import zlib
from pathlib import Path

M32 = 0xffffffff

# ccr bits
XF, NF, ZF, VF, CF = 0x10, 0x08, 0x04, 0x02, 0x01

SIZE = {
    # name: (bits, mask, n-shift, byte count)
    'b': (8, 0xff, 4, 1),
    'w': (16, 0xffff, 12, 2),
    'l': (32, 0xffffffff, 28, 4),
}


class BusError(Exception):
    vector = 2

    def __init__(self, address, write=False):
        super().__init__(f'{self.__class__.__name__} at {address:08x} ({"write" if write else "read"})')
        self.address = address
        self.write = write


class AddressError(BusError):
    vector = 3


class CpuBreak(Exception):
    """Raised by a handler that needs the run loop to look at interrupts/STOP."""


def _cond_tables():
    tabs = []
    for cc in range(16):
        t = []
        for f in range(32):
            c, v, z, n = f & 1, (f >> 1) & 1, (f >> 2) & 1, (f >> 3) & 1
            t.append(bool((
                1, 0, not c and not z, c or z, not c, c, not z, z,
                not v, v, not n, n, n == v, n != v, (not z) and n == v, z or n != v)[cc]))
        tabs.append(tuple(t))
    return tabs


COND = _cond_tables()


# ---------------------------------------------------------------------------
# Source generation helpers
# ---------------------------------------------------------------------------

class Src:
    """Accumulates the body of one handler."""

    def __init__(self):
        self.lines = []
        self.ind = 1
        self.globs = set()

    def __call__(self, line):
        self.lines.append('    ' * self.ind + line)

    def g(self, *names):
        self.globs.update(names)

    def text(self):
        head = ['def op(pc):']
        if self.globs:
            head.append('    global ' + ', '.join(sorted(self.globs)))
        return '\n'.join(head + self.lines)


def fetch(s, var):
    s(f'_p = FP[pc >> 16]; _o = pc & 0xffff; {var} = _p[_o] << 8 | _p[_o + 1]; pc += 2')


def fetch_long(s, var):
    s(f'_p = FP[pc >> 16]; _o = pc & 0xffff; {var} = _p[_o] << 8 | _p[_o + 1]')
    s(f'_p = FP[(pc + 2) >> 16]; _o = (pc + 2) & 0xffff; {var} = {var} << 16 | _p[_o] << 8 | _p[_o + 1]; pc += 4')


def sx16(e):
    return f'(({e} ^ 0x8000) - 0x8000)'


def sx8(e):
    return f'((({e}) & 0xff ^ 0x80) - 0x80)'


# EA mode numbers: 0..6 plus 7..11 for mode 7 sub-registers
# 7 abs.w, 8 abs.l, 9 d16(pc), 10 d8(pc,xn), 11 #imm
def ea_kind(mode, reg):
    return mode if mode < 7 else 7 + reg if reg <= 4 else None


DATA = {0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
ALL = DATA | {1}
MEMORY = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
CONTROL = {2, 5, 6, 7, 8, 9, 10}
ALTERABLE = {0, 1, 2, 3, 4, 5, 6, 7, 8}
DATA_ALT = {0, 2, 3, 4, 5, 6, 7, 8}
MEM_ALT = {2, 3, 4, 5, 6, 7, 8}
CTRL_ALT = {2, 5, 6, 7, 8}


class EA:
    """One effective address operand of a handler being generated.

    ``slot`` names the closure parameter that holds the register index into R
    (R[0..7] = D0-D7, R[8..15] = A0-A7).
    """

    def __init__(self, kind, reg, size, slot, tag):
        self.kind, self.reg, self.size, self.slot, self.tag = kind, reg, size, slot, tag
        self.addr = 'ea_' + tag
        self.computed = False

    @property
    def is_reg(self):
        return self.kind in (0, 1)

    def regidx(self):
        return self.reg if self.kind == 0 else self.reg + 8

    def calc(self, s):
        """Emit address computation (memory operands only)."""
        k, r, a = self.kind, self.slot, self.addr
        inc = SIZE[self.size][3]
        if inc == 1 and self.reg == 7 and k in (3, 4):
            inc = 2
        if k == 2:
            s(f'{a} = R[{r}]')
        elif k == 3:
            s(f'{a} = R[{r}]; R[{r}] = ({a} + {inc}) & 0xffffffff')
        elif k == 4:
            s(f'{a} = (R[{r}] - {inc}) & 0xffffffff; R[{r}] = {a}')
        elif k == 5:
            fetch(s, '_e')
            s(f'{a} = (R[{r}] + {sx16("_e")}) & 0xffffffff')
        elif k == 6:
            fetch(s, '_e')
            s(f'{a} = ix(R[{r}], _e)')
        elif k == 7:
            fetch(s, '_e')
            s(f'{a} = {sx16("_e")} & 0xffffffff')
        elif k == 8:
            fetch_long(s, a)
        elif k == 9:
            s('_b = pc')
            fetch(s, '_e')
            s(f'{a} = (_b + {sx16("_e")}) & 0xffffffff')
        elif k == 10:
            s('_b = pc')
            fetch(s, '_e')
            s(f'{a} = ix(_b, _e)')
        else:
            raise ValueError('no address for this mode')
        self.computed = True

    def read(self, s, var):
        k, size = self.kind, self.size
        bits, mask, _, _ = SIZE[size]
        if k in (0, 1):
            if size == 'l':
                s(f'{var} = R[{self.slot}]')
            else:
                s(f'{var} = R[{self.slot}] & {mask:#x}')
        elif k == 11:
            if size == 'b':
                fetch(s, var)
                s(f'{var} &= 0xff')
            elif size == 'w':
                fetch(s, var)
            else:
                fetch_long(s, var)
        else:
            if not self.computed:
                self.calc(s)
            s(f'{var} = r{size}({self.addr})')

    def write(self, s, var):
        k, size = self.kind, self.size
        if k == 0:
            if size == 'b':
                s(f'R[{self.slot}] = R[{self.slot}] & 0xffffff00 | {var}')
            elif size == 'w':
                s(f'R[{self.slot}] = R[{self.slot}] & 0xffff0000 | {var}')
            else:
                s(f'R[{self.slot}] = {var}')
        elif k == 1:
            # address registers are always written in full (callers sign-extend)
            s(f'R[{self.slot}] = {var}')
        else:
            if not self.computed:
                self.calc(s)
            s(f'w{size}({self.addr}, {var})')


def nz(size, r):
    """ccr update for N, Z with V=C=0, X kept."""
    nsh = SIZE[size][2]
    return f'ccr = ccr & 16 | {r} >> {nsh} & 8 | (0 if {r} else 4)'


def add_flags(size, s, d, t, r):
    bits = SIZE[size][0]
    return (f'ccr = ({t} >> {bits}) * 17 | {r} >> {SIZE[size][2]} & 8 | (0 if {r} else 4)'
            f' | (({s} ^ {r}) & ({d} ^ {r})) >> {bits - 2} & 2')


def sub_flags(size, s, d, t, r, x=True):
    bits = SIZE[size][0]
    c = f'({t} >> {bits} & 1) * 17' if x else f'ccr & 16 | {t} >> {bits} & 1'
    return (f'ccr = {c} | {r} >> {SIZE[size][2]} & 8 | (0 if {r} else 4)'
            f' | (({s} ^ {d}) & ({r} ^ {d})) >> {bits - 2} & 2')


def need_s(s):
    s('if not srh & 0x2000: return priv(pc)')


# ---------------------------------------------------------------------------
# Opcode decoder -> (template source, closure args)
# ---------------------------------------------------------------------------

SZ2 = {0: 'b', 1: 'w', 2: 'l'}


def ea_operand(mode, reg, size, slot, tag, allowed):
    k = ea_kind(mode, reg)
    if k is None or k not in allowed:
        return None
    if size == 'b' and k == 1:
        return None
    return EA(k, reg, size, slot, tag)


def gen_opcode(op):
    """Return (source, args) for opcode or None when it is illegal."""
    line = op >> 12
    fn = GEN_LINES[line]
    return fn(op)


def _args(*ea_or_vals):
    out = []
    for v in ea_or_vals:
        out.append(v.regidx() if isinstance(v, EA) else v)
    return tuple(out)


# ----- line 0: immediate ops, bit ops, movep --------------------------------

def gen_line0(op):
    mode, reg = (op >> 3) & 7, op & 7
    if op & 0x0100:
        dn = (op >> 9) & 7
        if mode == 1:
            return gen_movep(op)
        return gen_bitop(op, dynamic=True, dn=dn)
    sub = (op >> 9) & 7
    if sub == 4:
        return gen_bitop(op, dynamic=False, dn=None)
    if sub == 7:
        return None
    sz = (op >> 6) & 3
    name = ('ori', 'andi', 'subi', 'addi', None, 'eori', 'cmpi', None)[sub]
    if name is None:
        return None
    # to CCR / SR
    if op & 0x3f == 0x3c and name in ('ori', 'andi', 'eori'):
        o = {'ori': '|', 'andi': '&', 'eori': '^'}[name]
        s = Src()
        if sz == 0:
            s.g('ccr')
            fetch(s, 'v')
            s(f'ccr = (ccr {o} v) & 0x1f')
            s('return pc')
            return s.text(), ()
        if sz == 1:
            need_s(s)
            fetch(s, 'v')
            s(f'if set_sr((srh | ccr) {o} v): brk(pc)')
            s('return pc')
            return s.text(), ()
        return None
    if sz == 3:
        return None
    size = SZ2[sz]
    allowed = DATA_ALT if name != 'cmpi' else DATA_ALT
    dst = ea_operand(mode, reg, size, 'X', 'd', allowed)
    if dst is None:
        return None
    s = Src()
    s.g('ccr')
    imm = EA(11, 4, size, None, 'i')
    imm.read(s, 'v')
    dst.read(s, 'd')
    bits, mask = SIZE[size][0], SIZE[size][1]
    if name in ('ori', 'andi', 'eori'):
        o = {'ori': '|', 'andi': '&', 'eori': '^'}[name]
        s(f'r = d {o} v')
        s(nz(size, 'r'))
        dst.write(s, 'r')
    elif name == 'addi':
        s('t = d + v'); s(f'r = t & {mask:#x}')
        s(add_flags(size, 'v', 'd', 't', 'r'))
        dst.write(s, 'r')
    elif name == 'subi':
        s('t = d - v'); s(f'r = t & {mask:#x}')
        s(sub_flags(size, 'v', 'd', 't', 'r'))
        dst.write(s, 'r')
    else:  # cmpi
        s('t = d - v'); s(f'r = t & {mask:#x}')
        s(sub_flags(size, 'v', 'd', 't', 'r', x=False))
    s('return pc')
    return s.text(), _args(dst)


def gen_bitop(op, dynamic, dn):
    mode, reg = (op >> 3) & 7, op & 7
    kind = (op >> 6) & 3  # 0 btst 1 bchg 2 bclr 3 bset
    k = ea_kind(mode, reg)
    if k is None:
        return None
    if kind == 0:
        allowed = DATA if dynamic else DATA - {11}
    else:
        allowed = DATA_ALT
    if k not in allowed:
        return None
    size = 'l' if k == 0 else 'b'
    dst = EA(k, reg, size, 'X', 'd')
    s = Src()
    s.g('ccr')
    if dynamic:
        s('b = R[Y]')
    else:
        fetch(s, 'b')
    s(f'b = 1 << (b & {31 if k == 0 else 7})')
    if k == 11:  # btst #n,#imm (dynamic only)
        fetch(s, 'd')
        s('d &= 0xff')
    else:
        dst.read(s, 'd')
    s('ccr = ccr & 0x1b | (0 if d & b else 4)')
    if kind == 1:
        s('d ^= b')
    elif kind == 2:
        s('d &= ~b')
    elif kind == 3:
        s('d |= b')
    if kind:
        dst.write(s, 'd')
    s('return pc')
    args = (dst.regidx() if k in (0, 1) else reg + 8, dn if dynamic else 0)
    return s.text(), args


def gen_movep(op):
    dn = (op >> 9) & 7
    opm = (op >> 6) & 3
    s = Src()
    fetch(s, '_e')
    s(f'a = (R[X] + {sx16("_e")}) & 0xffffffff')
    if opm == 0:    # word mem -> reg
        s('R[Y] = R[Y] & 0xffff0000 | rb(a) << 8 | rb((a + 2) & 0xffffffff)')
    elif opm == 1:  # long mem -> reg
        s('R[Y] = rb(a) << 24 | rb((a + 2) & 0xffffffff) << 16 | rb((a + 4) & 0xffffffff) << 8 | rb((a + 6) & 0xffffffff)')
    elif opm == 2:  # word reg -> mem
        s('v = R[Y]; wb(a, v >> 8 & 0xff); wb((a + 2) & 0xffffffff, v & 0xff)')
    else:
        s('v = R[Y]; wb(a, v >> 24); wb((a + 2) & 0xffffffff, v >> 16 & 0xff); '
          'wb((a + 4) & 0xffffffff, v >> 8 & 0xff); wb((a + 6) & 0xffffffff, v & 0xff)')
    s('return pc')
    return s.text(), (8 + (op & 7), dn)


# ----- lines 1-3: move ------------------------------------------------------

def gen_move(op):
    size = {1: 'b', 3: 'w', 2: 'l'}[op >> 12]
    smode, sreg = (op >> 3) & 7, op & 7
    dmode, dreg = (op >> 6) & 7, (op >> 9) & 7
    src = ea_operand(smode, sreg, size, 'X', 's', ALL)
    if src is None:
        return None
    dk = ea_kind(dmode, dreg)
    if dk is None:
        return None
    s = Src()
    if dk == 1:  # movea
        if size == 'b':
            return None
        src.read(s, 'v')
        if size == 'w':
            s(f'R[Y] = {sx16("v")} & 0xffffffff')
        else:
            s('R[Y] = v')
        s('return pc')
        return s.text(), _args(src, dreg + 8)
    if dk not in DATA_ALT:
        return None
    dst = EA(dk, dreg, size, 'Y', 'd')
    s.g('ccr')
    src.read(s, 'v')
    s(nz(size, 'v'))
    dst.write(s, 'v')
    s('return pc')
    return s.text(), _args(src, dst)


# ----- line 4: miscellaneous ------------------------------------------------

def gen_line4(op):
    mode, reg = (op >> 3) & 7, op & 7
    k = ea_kind(mode, reg)
    if op & 0x0100:
        an = (op >> 9) & 7
        if (op >> 6) & 3 == 3:  # lea
            if k not in CONTROL:
                return None
            ea = EA(k, reg, 'l', 'X', 'e')
            s = Src()
            ea.calc(s)
            s(f'R[Y] = {ea.addr}')
            s('return pc')
            return s.text(), (reg + 8, an + 8)
        if (op >> 6) & 3 == 2:  # chk.w
            if k not in DATA:
                return None
            ea = EA(k, reg, 'w', 'X', 's')
            s = Src()
            s.g('ccr', 'xc')
            ea.read(s, 'v')
            s('d = R[Y] & 0xffff')
            s('d = (d ^ 0x8000) - 0x8000; v = (v ^ 0x8000) - 0x8000')
            s('ccr = ccr & 0x10 | (0 if d else 4)')
            s('if d < 0:')
            s('    ccr |= 8; xc += 20; return exception(6, pc)')
            s('if d > v:')
            s('    xc += 20; return exception(6, pc)')
            s('return pc')
            return s.text(), (ea.regidx() if ea.is_reg else reg + 8, an)
        return None
    sub = (op >> 8) & 0xf
    sz = (op >> 6) & 3
    if sub in (0, 2, 4, 6) and sz != 3:
        size = SZ2[sz]
        ea = ea_operand(mode, reg, size, 'X', 'd', DATA_ALT)
        if ea is None:
            return None
        bits, mask = SIZE[size][0], SIZE[size][1]
        s = Src()
        s.g('ccr')
        if sub == 2:  # clr
            ea.write(s, '0') if ea.is_reg else (ea.calc(s), s(f'w{size}({ea.addr}, 0)'))
            s('ccr = ccr & 16 | 4')
        else:
            ea.read(s, 'd')
            if sub == 0:  # negx
                s('t = -d - (ccr >> 4 & 1)')
                s(f'r = t & {mask:#x}')
                s(f'ccr = (t >> {bits} & 1) * 17 | r >> {SIZE[size][2]} & 8 | (0 if r else ccr & 4)'
                  f' | (d & r) >> {bits - 2} & 2')
            elif sub == 4:  # neg
                s('t = -d')
                s(f'r = t & {mask:#x}')
                s(f'ccr = (t >> {bits} & 1) * 17 | r >> {SIZE[size][2]} & 8 | (0 if r else 4)'
                  f' | (d & r) >> {bits - 2} & 2')
            else:  # not
                s(f'r = d ^ {mask:#x}')
                s(nz(size, 'r'))
            ea.write(s, 'r')
        s('return pc')
        return s.text(), _args(ea)
    if sub == 0 and sz == 3:  # move from sr (not privileged on the 68070)
        ea = ea_operand(mode, reg, 'w', 'X', 'd', DATA_ALT)
        if ea is None:
            return None
        s = Src()
        if not ea.is_reg:
            ea.calc(s)
        s('v = srh | ccr')
        ea.write(s, 'v')
        s('return pc')
        return s.text(), _args(ea)
    if sub == 4 and sz == 3:  # move to ccr
        ea = ea_operand(mode, reg, 'w', 'X', 's', DATA)
        if ea is None:
            return None
        s = Src()
        s.g('ccr')
        ea.read(s, 'v')
        s('ccr = v & 0x1f')
        s('return pc')
        return s.text(), _args(ea)
    if sub == 6 and sz == 3:  # move to sr
        ea = ea_operand(mode, reg, 'w', 'X', 's', DATA)
        if ea is None:
            return None
        s = Src()
        need_s(s)
        ea.read(s, 'v')
        s('if set_sr(v): brk(pc)')
        s('return pc')
        return s.text(), _args(ea)
    if sub == 8:
        if sz == 0:  # nbcd
            ea = ea_operand(mode, reg, 'b', 'X', 'd', DATA_ALT)
            if ea is None:
                return None
            s = Src()
            s.g('ccr')
            ea.read(s, 'd')
            s('r, ccr = nbcd(d, ccr)')
            ea.write(s, 'r')
            s('return pc')
            return s.text(), _args(ea)
        if sz == 1:
            if mode == 0:  # swap
                s = Src()
                s.g('ccr')
                s('v = R[X]; r = (v >> 16 | v << 16) & 0xffffffff; R[X] = r')
                s(nz('l', 'r'))
                s('return pc')
                return s.text(), (reg,)
            if k not in CONTROL:
                return None
            ea = EA(k, reg, 'l', 'X', 'e')  # pea
            s = Src()
            ea.calc(s)
            s(f'sp = (R[15] - 4) & 0xffffffff; R[15] = sp; wl(sp, {ea.addr})')
            s('return pc')
            return s.text(), (reg + 8,)
        # sz 2,3: ext or movem reg->mem
        if mode == 0:
            s = Src()
            s.g('ccr')
            if sz == 2:
                s('v = R[X] & 0xff; v = (v ^ 0x80) - 0x80 & 0xffff')
                s('R[X] = R[X] & 0xffff0000 | v')
                s(nz('w', 'v'))
            else:
                s('v = R[X] & 0xffff; v = (v ^ 0x8000) - 0x8000 & 0xffffffff')
                s('R[X] = v')
                s(nz('l', 'v'))
            s('return pc')
            return s.text(), (reg,)
        return gen_movem(op, to_mem=True)
    if sub == 0xa:
        if sz == 3:
            if op == 0x4afc:  # illegal
                s = Src()
                s('return exception(4, pc - 2)')
                return s.text(), ()
            ea = ea_operand(mode, reg, 'b', 'X', 'd', DATA_ALT)  # tas
            if ea is None:
                return None
            s = Src()
            s.g('ccr')
            ea.read(s, 'd')
            s(nz('b', 'd'))
            s('d |= 0x80')
            ea.write(s, 'd')
            s('return pc')
            return s.text(), _args(ea)
        size = SZ2[sz]
        ea = ea_operand(mode, reg, size, 'X', 'd', DATA_ALT)  # tst
        if ea is None:
            return None
        s = Src()
        s.g('ccr')
        ea.read(s, 'v')
        s(nz(size, 'v'))
        s('return pc')
        return s.text(), _args(ea)
    if sub == 0xc and sz in (2, 3):
        return gen_movem(op, to_mem=False)
    if sub == 0xe:
        if sz == 1:
            return gen_4e(op)
        if sz in (2, 3):
            if k not in CONTROL:
                return None
            ea = EA(k, reg, 'l', 'X', 'e')
            s = Src()
            ea.calc(s)
            if sz == 2:  # jsr
                s('sp = (R[15] - 4) & 0xffffffff; R[15] = sp; wl(sp, pc)')
            s(f'return {ea.addr}')
            return s.text(), (reg + 8,)
    return None


def gen_movem(op, to_mem):
    mode, reg = (op >> 3) & 7, op & 7
    k = ea_kind(mode, reg)
    size = 'l' if op & 0x40 else 'w'
    n = SIZE[size][3]
    cyc = 11 if size == 'l' else 7
    if to_mem:
        if k not in CTRL_ALT and k != 4:
            return None
    else:
        if k not in CONTROL and k != 3:
            return None
    s = Src()
    s.g('xc')
    fetch(s, 'm')
    if to_mem and k == 4:
        s('a = R[X]')
        s('i = 15')
        s('n = 0')
        s('while m:')
        s('    if m & 1:')
        s(f'        a = (a - {n}) & 0xffffffff; w{size}(a, R[i]' + (' & 0xffff)' if size == 'w' else ')') + '; n += 1')
        s('    m >>= 1; i -= 1')
        s('R[X] = a')
    else:
        if k == 3:
            s('a = R[X]')
        else:
            ea = EA(k, reg, size, 'X', 'e')
            ea.calc(s)
            s(f'a = {ea.addr}')
        s('i = 0')
        s('n = 0')
        s('while m:')
        s('    if m & 1:')
        if to_mem:
            s(f'        w{size}(a, R[i]' + (' & 0xffff)' if size == 'w' else ')'))
        elif size == 'w':
            s('        v = rw(a); R[i] = (v ^ 0x8000) - 0x8000 & 0xffffffff')
        else:
            s('        R[i] = rl(a)')
        s(f'        a = (a + {n}) & 0xffffffff; n += 1')
        s('    m >>= 1; i += 1')
        if k == 3:
            s('R[X] = a')
    s(f'xc += n * {cyc}')
    s('return pc')
    return s.text(), (reg + 8,)


def gen_4e(op):
    low = op & 0x3f
    s = Src()
    if 0x00 <= low < 0x10:  # trap #n
        s(f'return exception({32 + (op & 15)}, pc)')
        return s.text(), ()
    if 0x10 <= low < 0x18:  # link
        fetch(s, '_e')
        s('sp = (R[15] - 4) & 0xffffffff; wl(sp, R[X]); R[X] = sp')
        s(f'R[15] = (sp + {sx16("_e")}) & 0xffffffff')
        s('return pc')
        return s.text(), ((op & 7) + 8,)
    if 0x18 <= low < 0x20:  # unlk
        s('sp = R[X]; v = rl(sp); R[15] = (sp + 4) & 0xffffffff; R[X] = v')
        s('return pc')
        return s.text(), ((op & 7) + 8,)
    if 0x20 <= low < 0x28:  # move An,usp
        s.g('usp')
        need_s(s)
        s('usp = R[X]')
        s('return pc')
        return s.text(), ((op & 7) + 8,)
    if 0x28 <= low < 0x30:  # move usp,An
        need_s(s)
        s('R[X] = usp')
        s('return pc')
        return s.text(), ((op & 7) + 8,)
    if op == 0x4e70:  # reset
        need_s(s)
        s('reset_devices()')
        s('return pc')
        return s.text(), ()
    if op == 0x4e71:  # nop
        s('return pc')
        return s.text(), ()
    if op == 0x4e72:  # stop
        s.g('stopped')
        need_s(s)
        fetch(s, 'v')
        s('set_sr(v)')
        s('stopped = True')
        s('brk(pc)')
        return s.text(), ()
    if op == 0x4e73:  # rte
        need_s(s)
        s('return rte(pc)')
        return s.text(), ()
    if op == 0x4e75:  # rts
        s('sp = R[15]; R[15] = (sp + 4) & 0xffffffff')
        s('return rl(sp)')
        return s.text(), ()
    if op == 0x4e76:  # trapv
        s('if ccr & 2: return exception(7, pc)')
        s('return pc')
        return s.text(), ()
    if op == 0x4e77:  # rtr
        s.g('ccr')
        s('sp = R[15]; ccr = rw(sp) & 0x1f; v = rl((sp + 2) & 0xffffffff); R[15] = (sp + 6) & 0xffffffff')
        s('return v')
        return s.text(), ()
    return None


# ----- line 5: addq/subq/scc/dbcc --------------------------------------------

def gen_line5(op):
    mode, reg = (op >> 3) & 7, op & 7
    sz = (op >> 6) & 3
    k = ea_kind(mode, reg)
    if sz == 3:
        cc = (op >> 8) & 0xf
        if mode == 1:  # dbcc
            s = Src()
            s('if T[ccr]: return pc + 2')
            s('n = (R[X] - 1) & 0xffff; R[X] = R[X] & 0xffff0000 | n')
            s('if n == 0xffff: return pc + 2')
            fetch(s, '_e')
            s(f'return (pc - 2 + {sx16("_e")}) & 0xffffffff')
            return s.text(), (reg, 0, COND[cc])
        ea = ea_operand(mode, reg, 'b', 'X', 'd', DATA_ALT)  # scc
        if ea is None:
            return None
        s = Src()
        if not ea.is_reg:
            ea.calc(s)
        s('v = 0xff if T[ccr] else 0')
        ea.write(s, 'v')
        s('return pc')
        return s.text(), (ea.regidx() if ea.is_reg else reg + 8, 0, COND[cc])
    size = SZ2[sz]
    q = (op >> 9) & 7 or 8
    sub = op & 0x100
    if k == 1:
        if size == 'b':
            return None
        s = Src()  # address register: whole register, no flags
        s(f'R[X] = (R[X] {"-" if sub else "+"} {q}) & 0xffffffff')
        s('return pc')
        return s.text(), (reg + 8,)
    ea = ea_operand(mode, reg, size, 'X', 'd', DATA_ALT)
    if ea is None:
        return None
    mask = SIZE[size][1]
    s = Src()
    s.g('ccr')
    ea.read(s, 'd')
    if sub:
        s(f't = d - {q}; r = t & {mask:#x}')
        s(sub_flags(size, str(q), 'd', 't', 'r'))
    else:
        s(f't = d + {q}; r = t & {mask:#x}')
        s(add_flags(size, str(q), 'd', 't', 'r'))
    ea.write(s, 'r')
    s('return pc')
    return s.text(), _args(ea)


# ----- line 6: branches ------------------------------------------------------

def gen_line6(op):
    cc = (op >> 8) & 0xf
    disp = op & 0xff
    s = Src()
    if disp == 0:
        fetch(s, '_e')
        target = f'(pc - 2 + {sx16("_e")}) & 0xffffffff'
    else:
        target = '(pc + Z) & 0xffffffff'
    d8 = (disp ^ 0x80) - 0x80
    if cc == 1:  # bsr
        s('sp = (R[15] - 4) & 0xffffffff; R[15] = sp; wl(sp, pc)')
        s(f'return {target}')
        return s.text(), (0, 0, d8)
    if cc == 0:
        s(f'return {target}')
        return s.text(), (0, 0, d8)
    if disp == 0:
        s.lines.clear()
        s('if not T[ccr]: return pc + 2')
        fetch(s, '_e')
        s(f'return {target}')
        return s.text(), (0, 0, COND[cc])
    s('if T[ccr]: return (pc + Z) & 0xffffffff')
    s('return pc')
    return s.text(), (0, 0, d8, COND[cc])


# ----- line 7: moveq ------------------------------------------------------------

def gen_line7(op):
    if op & 0x100:
        return None
    s = Src()
    s.g('ccr')
    s('R[X] = Z')
    s('ccr = ccr & 16 | Y')
    s('return pc')
    v = ((op & 0xff) ^ 0x80) - 0x80
    f = (8 if v < 0 else 0) | (4 if v == 0 else 0)
    return s.text(), ((op >> 9) & 7, f, v & M32)


# ----- lines 8, 9, b, c, d: arithmetic/logic --------------------------------------

def gen_alu(op):
    line = op >> 12
    dn = (op >> 9) & 7
    opm = (op >> 6) & 7
    mode, reg = (op >> 3) & 7, op & 7
    k = ea_kind(mode, reg)
    if k is None:
        return None
    # special encodings first
    if opm in (3, 7):
        if line == 8:
            return gen_div(op, signed=opm == 7)
        if line == 0xc:
            return gen_mul(op, signed=opm == 7)
        return gen_addr_arith(op)  # suba/cmpa/adda
    if opm >= 4 and mode in (0, 1):
        if line == 8 and opm == 4:
            return gen_bcd(op, 'sbcd')
        if line == 0xc and opm == 4:
            return gen_bcd(op, 'abcd')
        if line == 0xc and opm in (5, 6):
            return gen_exg(op)
        if line in (9, 0xd):
            return gen_addx(op, sub=line == 9)
        if line == 0xb and mode == 1:
            return gen_cmpm(op)
        if line in (8, 0xc):
            return None
    size = SZ2[opm & 3]
    name = {8: 'or', 9: 'sub', 0xb: 'cmp' if opm < 4 else 'eor', 0xc: 'and', 0xd: 'add'}[line]
    mask = SIZE[size][1]
    s = Src()
    s.g('ccr')
    if opm < 4 or name == 'eor' and False:
        # <ea> op Dn -> Dn
        allowed = ALL if name in ('add', 'sub', 'cmp') else DATA
        src = ea_operand(mode, reg, size, 'X', 's', allowed)
        if src is None:
            return None
        src.read(s, 's')
        s(f'd = R[Y] & {mask:#x}' if size != 'l' else 'd = R[Y]')
        dst = EA(0, dn, size, 'Y', 'd')
        args = (src.regidx() if src.is_reg else reg + 8, dn)
    else:
        # Dn op <ea> -> <ea>
        allowed = DATA_ALT if name == 'eor' else MEM_ALT
        dst = ea_operand(mode, reg, size, 'X', 'd', allowed)
        if dst is None:
            return None
        s(f's = R[Y] & {mask:#x}' if size != 'l' else 's = R[Y]')
        dst.read(s, 'd')
        args = (dst.regidx() if dst.is_reg else reg + 8, dn)
    if name in ('or', 'and', 'eor'):
        o = {'or': '|', 'and': '&', 'eor': '^'}[name]
        s(f'r = d {o} s')
        s(nz(size, 'r'))
        dst.write(s, 'r')
    elif name == 'add':
        s(f't = d + s; r = t & {mask:#x}')
        s(add_flags(size, 's', 'd', 't', 'r'))
        dst.write(s, 'r')
    elif name == 'sub':
        s(f't = d - s; r = t & {mask:#x}')
        s(sub_flags(size, 's', 'd', 't', 'r'))
        dst.write(s, 'r')
    else:  # cmp
        s(f't = d - s; r = t & {mask:#x}')
        s(sub_flags(size, 's', 'd', 't', 'r', x=False))
    s('return pc')
    return s.text(), args


def gen_addr_arith(op):
    line = op >> 12
    an = (op >> 9) & 7
    size = 'l' if op & 0x100 else 'w'
    mode, reg = (op >> 3) & 7, op & 7
    src = ea_operand(mode, reg, size, 'X', 's', ALL)
    if src is None:
        return None
    s = Src()
    src.read(s, 's')
    if size == 'w':
        s('s = (s ^ 0x8000) - 0x8000 & 0xffffffff')
    if line == 0xd:
        s('R[Y] = (R[Y] + s) & 0xffffffff')
    elif line == 9:
        s('R[Y] = (R[Y] - s) & 0xffffffff')
    else:  # cmpa: always long compare
        s.g('ccr')
        s('d = R[Y]; t = d - s; r = t & 0xffffffff')
        s(sub_flags('l', 's', 'd', 't', 'r', x=False))
    s('return pc')
    return s.text(), (src.regidx() if src.is_reg else reg + 8, an + 8)


def gen_addx(op, sub):
    size_code = (op >> 6) & 3
    size = SZ2[size_code]
    bits, mask = SIZE[size][0], SIZE[size][1]
    rx, ry = (op >> 9) & 7, op & 7
    mem = op & 8
    s = Src()
    s.g('ccr')
    if mem:
        inc = SIZE[size][3]
        incy = 2 if (inc == 1 and ry == 7) else inc
        incx = 2 if (inc == 1 and rx == 7) else inc
        s(f'a = (R[X] - {incy}) & 0xffffffff; R[X] = a; s = r{size}(a)')
        s(f'b = (R[Y] - {incx}) & 0xffffffff; R[Y] = b; d = r{size}(b)')
    else:
        s(f's = R[X] & {mask:#x}' if size != 'l' else 's = R[X]')
        s(f'd = R[Y] & {mask:#x}' if size != 'l' else 'd = R[Y]')
    if sub:
        s('t = d - s - (ccr >> 4 & 1)')
        s(f'r = t & {mask:#x}')
        s(f'ccr = (t >> {bits} & 1) * 17 | r >> {SIZE[size][2]} & 8 | (0 if r else ccr & 4)'
          f' | ((s ^ d) & (r ^ d)) >> {bits - 2} & 2')
    else:
        s('t = d + s + (ccr >> 4 & 1)')
        s(f'r = t & {mask:#x}')
        s(f'ccr = (t >> {bits}) * 17 | r >> {SIZE[size][2]} & 8 | (0 if r else ccr & 4)'
          f' | ((s ^ r) & (d ^ r)) >> {bits - 2} & 2')
    if mem:
        s(f'w{size}(b, r)')
    else:
        EA(0, rx, size, 'Y', 'd').write(s, 'r')
    s('return pc')
    if mem:
        return s.text(), (ry + 8, rx + 8)
    return s.text(), (ry, rx)


def gen_cmpm(op):
    size = SZ2[(op >> 6) & 3]
    bits, mask = SIZE[size][0], SIZE[size][1]
    ax, ay = (op >> 9) & 7, op & 7
    inc = SIZE[size][3]
    incy = 2 if (inc == 1 and ay == 7) else inc
    incx = 2 if (inc == 1 and ax == 7) else inc
    s = Src()
    s.g('ccr')
    s(f'a = R[X]; R[X] = (a + {incy}) & 0xffffffff; s = r{size}(a)')
    s(f'b = R[Y]; R[Y] = (b + {incx}) & 0xffffffff; d = r{size}(b)')
    s(f't = d - s; r = t & {mask:#x}')
    s(sub_flags(size, 's', 'd', 't', 'r', x=False))
    s('return pc')
    return s.text(), (ay + 8, ax + 8)


def gen_bcd(op, name):
    rx, ry = (op >> 9) & 7, op & 7
    mem = op & 8
    s = Src()
    s.g('ccr')
    if mem:
        incy = 2 if ry == 7 else 1
        incx = 2 if rx == 7 else 1
        s(f'a = (R[X] - {incy}) & 0xffffffff; R[X] = a; s = rb(a)')
        s(f'b = (R[Y] - {incx}) & 0xffffffff; R[Y] = b; d = rb(b)')
    else:
        s('s = R[X] & 0xff; d = R[Y] & 0xff')
    s(f'r, ccr = {name}(s, d, ccr)')
    if mem:
        s('wb(b, r)')
    else:
        s('R[Y] = R[Y] & 0xffffff00 | r')
    s('return pc')
    if mem:
        return s.text(), (ry + 8, rx + 8)
    return s.text(), (ry, rx)


def gen_exg(op):
    rx, ry = (op >> 9) & 7, op & 7
    opm = (op >> 3) & 0x1f
    if opm == 0x08:
        a, b = rx, ry
    elif opm == 0x09:
        a, b = rx + 8, ry + 8
    elif opm == 0x11:
        a, b = rx, ry + 8
    else:
        return None
    s = Src()
    s('R[X], R[Y] = R[Y], R[X]')
    s('return pc')
    return s.text(), (a, b)


def gen_mul(op, signed):
    mode, reg = (op >> 3) & 7, op & 7
    src = ea_operand(mode, reg, 'w', 'X', 's', DATA)
    if src is None:
        return None
    s = Src()
    s.g('ccr')
    src.read(s, 's')
    s('d = R[Y] & 0xffff')
    if signed:
        s('r = ((s ^ 0x8000) - 0x8000) * ((d ^ 0x8000) - 0x8000) & 0xffffffff')
    else:
        s('r = s * d')
    s('R[Y] = r')
    s(nz('l', 'r'))
    s('return pc')
    return s.text(), (src.regidx() if src.is_reg else reg + 8, (op >> 9) & 7)


def gen_div(op, signed):
    mode, reg = (op >> 3) & 7, op & 7
    src = ea_operand(mode, reg, 'w', 'X', 's', DATA)
    if src is None:
        return None
    s = Src()
    s.g('ccr', 'xc')
    src.read(s, 's')
    s('if s == 0:')
    s('    ccr &= 0x1e; xc += 30; return exception(5, pc)')
    s('d = R[Y]')
    if signed:
        s('d = (d ^ 0x80000000) - 0x80000000; s = (s ^ 0x8000) - 0x8000')
        s('q = abs(d) // abs(s)')
        s('if (d < 0) != (s < 0): q = -q')
        s('m = d - q * s')
        s('if -0x8000 <= q <= 0x7fff:')
        s('    q &= 0xffff')
        s('    R[Y] = (m & 0xffff) << 16 | q')
        s('    ccr = ccr & 16 | q >> 12 & 8 | (0 if q else 4)')
        s('else:')
        s('    ccr = ccr & 0x10 | 0x0a')
    else:
        s('q = d // s')
        s('if q <= 0xffff:')
        s('    R[Y] = (d - q * s) << 16 | q')
        s('    ccr = ccr & 16 | q >> 12 & 8 | (0 if q else 4)')
        s('else:')
        s('    ccr = ccr & 0x10 | 0x0a')
    s(f'xc += {80 if signed else 64}')
    s('return pc')
    return s.text(), (src.regidx() if src.is_reg else reg + 8, (op >> 9) & 7)


# ----- line e: shifts and rotates ------------------------------------------------

SHIFT_FUNCS = ('as', 'ls', 'rox', 'ro')


def gen_linee(op):
    sz = (op >> 6) & 3
    left = bool(op & 0x100)
    if sz == 3:  # memory shift by one
        typ = (op >> 9) & 7
        if typ > 3:
            return None
        mode, reg = (op >> 3) & 7, op & 7
        ea = ea_operand(mode, reg, 'w', 'X', 'd', MEM_ALT)
        if ea is None:
            return None
        s = Src()
        s.g('ccr')
        ea.read(s, 'd')
        s(f'r, ccr = sh_{SHIFT_FUNCS[typ]}{"l" if left else "r"}(d, 1, 16, ccr)')
        ea.write(s, 'r')
        s('return pc')
        return s.text(), _args(ea)
    size = SZ2[sz]
    bits = SIZE[size][0]
    typ = (op >> 3) & 3
    dreg = op & 7
    cnt = (op >> 9) & 7
    s = Src()
    s.g('ccr', 'xc')
    if op & 0x20:
        s('n = R[Y] & 63')
        args = (dreg, cnt)
    else:
        s('n = Z')
        args = (dreg, 0, cnt or 8)
    dst = EA(0, dreg, size, 'X', 'd')
    dst.read(s, 'd')
    s(f'r, ccr = sh_{SHIFT_FUNCS[typ]}{"l" if left else "r"}(d, n, {bits}, ccr)')
    dst.write(s, 'r')
    s('xc += n * 3')
    s('return pc')
    return s.text(), args


def gen_linea(op):
    s = Src()
    s('return exception(10, pc - 2)')
    return s.text(), ()


def gen_linef(op):
    s = Src()
    s('return exception(11, pc - 2)')
    return s.text(), ()


GEN_LINES = [gen_line0, gen_move, gen_move, gen_move, gen_line4, gen_line5, gen_line6, gen_line7,
             gen_alu, gen_alu, gen_linea, gen_alu, gen_alu, gen_alu, gen_linee, gen_linef]


# ---------------------------------------------------------------------------
# Runtime helpers that live in the shared namespace
# ---------------------------------------------------------------------------

RUNTIME = r'''
def ix(base, e):
    x = R[e >> 12]
    if not e & 0x800:
        x = ((x & 0xffff) ^ 0x8000) - 0x8000
    return (base + x + ((e & 0xff) ^ 0x80) - 0x80) & 0xffffffff


def set_sr(v):
    """Write SR; returns True when an interrupt became serviceable."""
    global srh, ccr, usp, ssp
    v &= 0xa71f
    if (v ^ srh) & 0x2000:
        if v & 0x2000:
            usp = R[15]; R[15] = ssp
        else:
            ssp = R[15]; R[15] = usp
    srh = v & 0xff00
    ccr = v & 0x1f
    return irq_pending()


def irq_pending():
    if ipl == 7:
        return nmi_edge
    return ipl > (srh >> 8 & 7)


def brk(pc):
    global PC
    PC = pc
    raise CpuBreak()


def push16(v):
    sp = (R[15] - 2) & 0xffffffff
    R[15] = sp
    ww(sp, v)


def push32(v):
    sp = (R[15] - 4) & 0xffffffff
    R[15] = sp
    wl(sp, v)


def enter_exception():
    sr = srh | ccr
    set_sr((sr | 0x2000) & 0x7fff)
    return sr


def exception(vec, pc):
    global xc
    sr = enter_exception()
    if FRAME_FORMAT:
        push16(vec << 2)
    push32(pc)
    push16(sr)
    xc += 50
    return rl((vbr + (vec << 2)) & 0xffffffff)


def priv(pc):
    return exception(8, pc - 2)


def rte(pc):
    global ccr
    sp = R[15]
    if not FRAME_FORMAT:
        sr = rw(sp)
        npc = rl((sp + 2) & 0xffffffff)
        R[15] = (sp + 6) & 0xffffffff
        if set_sr(sr):
            brk(npc)
        return npc
    fmt = rw((sp + 6) & 0xffffffff) >> 12
    if fmt == 0:
        sr = rw(sp)
        npc = rl((sp + 2) & 0xffffffff)
        R[15] = (sp + 8) & 0xffffffff
        if set_sr(sr):
            brk(npc)
        return npc
    # MAME's SCC68070 core only accepts short frames; anything else is a format error
    return exception(14, pc - 2)


def take_interrupt(pc):
    """Service the pending interrupt at level ipl; returns new pc."""
    global stopped, xc, nmi_edge, srh
    level = ipl
    vec = iack(level)
    stopped = False
    if level == 7:
        nmi_edge = False
    sr = enter_exception()
    srh = (srh & ~0x0700) | (level << 8)
    npc = rl((vbr + (vec << 2)) & 0xffffffff)
    if npc == 0:
        npc = rl((vbr + (15 << 2)) & 0xffffffff)
    if FRAME_FORMAT:
        push16(vec << 2)
    push32(pc)
    push16(sr)
    xc += 65
    return npc


def bus_error(e, ppc, ir):
    global halted, xc
    sr = enter_exception()
    try:
        # SCC68070 long format (type 1111) frame, 17 words, as MAME stacks it
        R[15] = (R[15] - 2) & 0xffffffff      # internal information
        push16(0)                            # instruction input buffer
        push16(ir)                           # instruction register
        push32(0)                            # data input buffer
        push32(e.address)                    # fault address
        push32(0)                            # data output buffer
        R[15] = (R[15] - 4) & 0xffffffff      # internal information
        push16(0)                            # movem mask
        push16(0)                            # special status word
        push16(0xf000 | (2 << 2))
        push32(ppc)
        push16(sr)
    except BusError:
        halted = True
        return ppc
    xc += 80
    return rl((vbr + (e.vector << 2)) & 0xffffffff)


def sbcd(s, d, ccr):
    # BCD flag behaviour as measured on hardware (flamewing's analysis)
    dd = (d - s - (ccr >> 4 & 1)) & 0xff
    bc = ((~d & s) | (dd & ~d) | (dd & s)) & 0x88
    corf = bc - (bc >> 2)
    rr = (dd - corf) & 0xff
    c = (bc | (~dd & rr)) >> 7 & 1
    v = (dd & ~rr) >> 7 & 1
    return rr, c * 17 | (rr >> 4 & 8) | ((ccr & 4) if rr == 0 else 0) | v << 1


def abcd(s, d, ccr):
    ss = s + d + (ccr >> 4 & 1)
    dc = (((ss + 0x66) ^ ss) & 0x110) >> 1
    ss &= 0xff
    bc = ((s & d) | (~ss & s) | (~ss & d)) & 0x88
    corf = (bc | dc) - ((bc | dc) >> 2)
    rr = (ss + corf) & 0xff
    c = (bc | (ss & ~rr)) >> 7 & 1
    v = (~ss & rr) >> 7 & 1
    return rr, c * 17 | (rr >> 4 & 8) | ((ccr & 4) if rr == 0 else 0) | v << 1


def nbcd(d, ccr):
    return sbcd(d, 0, ccr)


def _nz(r, bits):
    return (r >> (bits - 4) & 8) | (0 if r else 4)


def sh_asl(d, n, bits, ccr):
    mask = (1 << bits) - 1
    if n == 0:
        return d, (ccr & 16) | _nz(d, bits)
    if n < bits:
        r = (d << n) & mask
        c = (d >> (bits - n)) & 1
        top = d >> (bits - 1 - n)          # the bits that pass through the sign position
        allm = (1 << (n + 1)) - 1
        v = 0 if (top == 0 or top == allm) else 2
        return r, c * 17 | _nz(r, bits) | v
    c = (d & 1) if n == bits else 0
    return 0, c * 17 | 4 | (2 if d else 0)


def sh_asr(d, n, bits, ccr):
    if n == 0:
        return d, (ccr & 16) | _nz(d, bits)
    mask = (1 << bits) - 1
    sd = d - (1 << bits) if d >> (bits - 1) else d
    if n < bits:
        r = (sd >> n) & mask
        c = (sd >> (n - 1)) & 1
        return r, c * 17 | _nz(r, bits)
    if sd < 0:
        return mask, 0x11 | _nz(mask, bits)
    return 0, 4


def sh_lsl(d, n, bits, ccr):
    if n == 0:
        return d, (ccr & 16) | _nz(d, bits)
    mask = (1 << bits) - 1
    if n <= bits:
        r = (d << n) & mask
        c = (d >> (bits - n)) & 1
        return r, c * 17 | _nz(r, bits)
    return 0, 4


def sh_lsr(d, n, bits, ccr):
    if n == 0:
        return d, (ccr & 16) | _nz(d, bits)
    if n <= bits:
        r = d >> n
        c = (d >> (n - 1)) & 1
        return r, c * 17 | _nz(r, bits)
    return 0, 4


def sh_rol(d, n, bits, ccr):
    x = ccr & 16
    if n == 0:
        return d, x | _nz(d, bits)
    mask = (1 << bits) - 1
    m = n % bits
    r = ((d << m) | (d >> (bits - m))) & mask if m else d
    return r, x | (r & 1) | _nz(r, bits)


def sh_ror(d, n, bits, ccr):
    x = ccr & 16
    if n == 0:
        return d, x | _nz(d, bits)
    mask = (1 << bits) - 1
    m = n % bits
    r = ((d >> m) | (d << (bits - m))) & mask if m else d
    return r, x | (r >> (bits - 1) & 1) | _nz(r, bits)


def sh_roxl(d, n, bits, ccr):
    x = ccr >> 4 & 1
    n %= bits + 1
    if n == 0:
        return d, (ccr & 16) | x | _nz(d, bits)
    mask = (1 << bits) - 1
    v = (x << bits) | d                       # bits+1 wide rotate
    v = ((v << n) | (v >> (bits + 1 - n))) & ((1 << (bits + 1)) - 1)
    r = v & mask
    c = v >> bits
    return r, c * 17 | _nz(r, bits)


def sh_roxr(d, n, bits, ccr):
    x = ccr >> 4 & 1
    n %= bits + 1
    if n == 0:
        return d, (ccr & 16) | x | _nz(d, bits)
    mask = (1 << bits) - 1
    v = (d << 1) | x                          # x below bit 0
    w = bits + 1
    v = ((v >> n) | (v << (w - n))) & ((1 << w) - 1)
    r = v >> 1
    c = v & 1
    return r, c * 17 | _nz(r, bits)


def run(budget):
    """Execute about ``budget`` cycles; returns the number of cycles used."""
    global PC, xc, stopped
    pc = PC
    left = budget
    if halted:
        return budget
    if irq_pending():
        pc = take_interrupt(pc)
    elif stopped:
        return budget
    op = 0
    ppc = pc
    while left > 0:
        try:
            while left > 0:
                ppc = pc
                if pc & 1:
                    raise AddressError(pc)
                p = FP[pc >> 16]
                o = pc & 0xffff
                op = p[o] << 8 | p[o + 1]
                pc = OPS[op](pc + 2)
                left -= CYC[op]
        except CpuBreak:
            pc = PC
            if irq_pending():
                pc = take_interrupt(pc)
            elif stopped:
                break
        except BusError as e:
            pc = bus_error(e, ppc, op)
            if halted:
                break
        except IndexError:
            # misaligned word access running off the end of a page
            log_fault(ppc, op)
            pc = bus_error(BusError(pc, False), ppc, op)
            if halted:
                break
    PC = pc
    used = budget - left + xc
    xc = 0
    if stopped or halted:
        return max(used, budget)
    return used
'''


class M68070:
    def __init__(self, bus):
        self.bus = bus
        ns = {
            'R': [0] * 16, 'ccr': 0, 'srh': 0x2700, 'usp': 0, 'ssp': 0,
            'PC': 0, 'vbr': 0, 'xc': 0, 'ipl': 0, 'nmi_edge': False,
            'stopped': False, 'halted': False, 'FRAME_FORMAT': True,
            'CpuBreak': CpuBreak, 'BusError': BusError, 'AddressError': AddressError,
            'iack': lambda level: 24 + level,
            'reset_devices': lambda: None,
            'log_fault': lambda pc, op: None,
        }
        ns.update(bus.cpu_functions())
        exec(compile(RUNTIME, '<m68k-runtime>', 'exec'), ns)
        self.ns = ns
        self._build_ops()

    def _build_ops(self):
        ns = self.ns
        data = Path(__file__).with_name('data') / 'cycles070.bin'
        cyc = zlib.decompress(data.read_bytes())
        factories = {}
        ops = [None] * 65536
        cyctab = [0] * 65536
        illegal_src = 'def op(pc):\n    return exception(4, pc - 2)'
        illegal = None
        for op in range(65536):
            res = gen_opcode(op) if cyc[op] else None
            if res is None:
                if illegal is None:
                    loc = {}
                    exec(compile(illegal_src, '<illegal>', 'exec'), ns, loc)
                    illegal = loc['op']
                ops[op] = illegal
                cyctab[op] = 20
                continue
            src, args = res
            fac = factories.get(src)
            if fac is None:
                params = ('X', 'Y', 'Z', 'T')
                body = src.replace('\n', '\n    ')
                fsrc = (f'def make(X=0, Y=0, Z=0, T=None):\n    {body}\n    return op')
                loc = {}
                exec(compile(fsrc, f'<m68k {op:04x}>', 'exec'), ns, loc)
                fac = factories[src] = loc['make']
            if len(args) == 4:
                fn = fac(args[0], args[1], args[2], args[3])
            elif len(args) == 3 and isinstance(args[2], tuple):
                fn = fac(args[0], args[1], 0, args[2])
            else:
                fn = fac(*args)
            ops[op] = fn
            cyctab[op] = cyc[op]
        ns['OPS'] = ops
        ns['CYC'] = cyctab
        self.templates = len(factories)

    # ----- convenient accessors -------------------------------------------
    def reset(self):
        ns = self.ns
        ns['srh'] = 0x2700
        ns['ccr'] = 0
        ns['vbr'] = 0
        ns['stopped'] = False
        ns['halted'] = False
        ns['xc'] = 0
        rl = ns['rl']
        ns['R'][15] = rl(0)
        ns['ssp'] = ns['R'][15]
        ns['PC'] = rl(4)

    def run(self, cycles):
        return self.ns['run'](cycles)

    @property
    def pc(self):
        return self.ns['PC']

    @pc.setter
    def pc(self, v):
        self.ns['PC'] = v & M32

    @property
    def sr(self):
        return self.ns['srh'] | self.ns['ccr']

    def set_ipl(self, level):
        ns = self.ns
        if level == 7 and ns['ipl'] != 7:
            ns['nmi_edge'] = True
        ns['ipl'] = level

    @property
    def supervisor(self):
        return bool(self.ns['srh'] & 0x2000)

    def state(self):
        ns = self.ns
        R = ns['R']
        d = ' '.join(f'D{i}={R[i]:08x}' for i in range(8))
        a = ' '.join(f'A{i}={R[8 + i]:08x}' for i in range(8))
        return f'PC={ns["PC"]:08x} SR={self.sr:04x}\n{d}\n{a}'
