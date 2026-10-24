#!/usr/bin/env python3
"""Check the CPU core against SingleStepTests/m68000 (https://github.com/SingleStepTests/m68000).

Usage: run_sst.py DIR_WITH_JSON_BIN [NAME ...]

Runs the native (C) core when it can be built; OPENCDI_CPU=python tests the
Python core instead.  The first line of output says which one ran.

The core is switched to plain 68000 exception frames for the run, and trace
exceptions are turned off because the tests record the state right after the
instruction (before a trace exception would be taken).  Tests that raise
address errors are skipped because the core does not model them.
"""
import sys
from pathlib import Path
from struct import unpack_from

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from opencdi.bus import Bus  # noqa: E402
from opencdi.m68k_native import create_cpu  # noqa: E402

REGS = ['d0', 'd1', 'd2', 'd3', 'd4', 'd5', 'd6', 'd7',
        'a0', 'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'usp', 'ssp', 'sr', 'pc']


def read_state(c, p):
    p += 8
    st = {}
    for r in REGS:
        st[r] = unpack_from('<I', c, p)[0]
        p += 4
    st['prefetch'] = unpack_from('<II', c, p)
    p += 8
    n = unpack_from('<I', c, p)[0]
    p += 4
    ram = []
    for _ in range(n):
        addr, data = unpack_from('<IH', c, p)
        p += 6
        ram.append((addr, data >> 8))
        ram.append((addr | 1, data & 0xff))
    st['ram'] = ram
    return p, st


def read_tests(path):
    c = Path(path).read_bytes()
    magic, n = unpack_from('<II', c, 0)
    p = 8
    for _ in range(n):
        p += 8
        p += 8
        ln = unpack_from('<I', c, p)[0]
        p += 4
        name = c[p:p + ln].decode()
        p += ln
        p, ini = read_state(c, p)
        p, fin = read_state(c, p)
        p += 8
        ncyc, ntr = unpack_from('<II', c, p)
        p += 8
        addr_err = False
        for _ in range(ntr):
            tw = c[p]
            p += 5
            if tw:
                if tw in (4, 5):
                    addr_err = True
                p += 20
        yield name, ini, fin, addr_err


verbose = False


def main():
    d = Path(sys.argv[1])
    names = [n for n in sys.argv[2:] if n != '-v']
    global verbose
    verbose = '-v' in sys.argv
    files = sorted(d.glob('*.json.bin'))
    if names:
        files = [f for f in files if f.name.split('.json')[0] in names]
    bus = Bus(internal_space=False)
    ram = bus.map_ram('ram', 0, 0x1000000)
    cpu = create_cpu(bus)
    print('core:', 'native' if getattr(cpu, 'native', False) else 'python', flush=True)
    ns = cpu.ns
    ns['FRAME_FORMAT'] = False
    ns['TRACE'] = False
    ns['reset_devices'] = lambda: None
    total_fail = 0
    for f in files:
        ok = fail = skip = 0
        shown = 0
        for name, ini, fin, aerr in read_tests(f):
            if aerr:
                skip += 1
                continue
            touched = set()
            for a, v in ini['ram']:
                ram[a >> 16][a & 0xffff] = v
                touched.add(a)
            pc = ini['pc'] - 4
            for i, w in enumerate(ini['prefetch']):
                a = pc + 2 * i
                ram[a >> 16][a & 0xffff] = w >> 8
                ram[a >> 16][(a + 1) & 0xffff] = w & 0xff
            R = ns['R']
            for i in range(8):
                R[i] = ini[f'd{i}']
            for i in range(7):
                R[8 + i] = ini[f'a{i}']
            sr = ini['sr']
            ns['srh'] = sr & 0xff00
            ns['ccr'] = sr & 0x1f
            if sr & 0x2000:
                R[15] = ini['ssp']
                ns['usp'] = ini['usp']
            else:
                R[15] = ini['usp']
                ns['ssp'] = ini['ssp']
            ns['PC'] = pc
            ns['stopped'] = False
            ns['halted'] = False
            ns['ipl'] = 0
            try:
                cpu.run(1)
            except Exception as e:  # noqa: BLE001
                err = f'exception {e!r}'
            else:
                err = None
            got = {f'd{i}': R[i] for i in range(8)}
            got.update({f'a{i}': R[8 + i] for i in range(7)})
            s = ns['srh'] | ns['ccr']
            got['sr'] = s
            if s & 0x2000:
                got['ssp'], got['usp'] = R[15], ns['usp']
            else:
                got['usp'], got['ssp'] = R[15], ns['ssp']
            # the reported pc includes the two prefetched words; a CPU stopped
            # by STOP has not prefetched past the next instruction
            got['pc'] = ((ns['PC'] or 0) + (0 if ns['stopped'] else 4)) & 0xffffffff
            if ns['PC'] is None:
                diffs_extra = ['handler returned None']
            else:
                diffs_extra = []
            diffs = diffs_extra + ([] if err is None else [err])
            for r in REGS:
                if got[r] != fin[r]:
                    diffs.append(f'{r}: got {got[r]:08x} want {fin[r]:08x}')
            for a, v in fin['ram']:
                if ram[a >> 16][a & 0xffff] != v:
                    diffs.append(f'mem {a:06x}: got {ram[a >> 16][a & 0xffff]:02x} want {v:02x}')
            if diffs:
                fail += 1
                if shown < 3:
                    shown += 1
                    print(f'  FAIL {name}: ' + '; '.join(diffs[:6]))
                    if verbose:
                        print('     init', {k: hex(v) for k, v in ini.items() if k not in ('ram', 'prefetch')}, [hex(x) for x in ini['prefetch']])
            else:
                ok += 1
            # clean memory touched by this test
            for a, _ in fin['ram']:
                ram[a >> 16][a & 0xffff] = 0
            for a in touched:
                ram[a >> 16][a & 0xffff] = 0
        total_fail += fail
        print(f'{f.name}: {ok} ok, {fail} failed, {skip} skipped (address error)')
    print('total failures:', total_fail)


if __name__ == '__main__':
    main()
