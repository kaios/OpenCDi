#!/usr/bin/env python3
"""Regression checks for the fixes in 0.1.1 (see docs/debug/0.1.1.md).

Usage: tools/check_fixes.py

Runs without a BIOS or disc image.  Prints PASS/FAIL per check and exits
non-zero if any check fails.
"""
import sys, os, logging, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
WORK = tempfile.mkdtemp(prefix='opencdi-check-')
from opencdi import debuglog
logpath = os.path.join(WORK, 'check_debug.log')
debuglog.setup(debug=True, path=logpath)
from opencdi.bus import Bus
from opencdi.m68k import M68070
ok = True
def check(name, cond, info=''):
    global ok
    ok &= bool(cond)
    print(('PASS' if cond else 'FAIL'), name, info)

def cpu_with(prog, at=0x1000, sp=0x8000, sr=0x2700):
    bus = Bus(internal_space=False); ram = bus.map_ram('r', 0, 0x100000)
    cpu = M68070(bus); ns = cpu.ns
    for i, w in enumerate(prog):
        ram[0][at + 2*i] = w >> 8; ram[0][at + 2*i + 1] = w & 0xff
    ns['R'][15] = sp; ns['srh'] = sr & 0xff00; ns['ccr'] = sr & 0x1f; ns['PC'] = at
    return cpu, ns, ram

def rl(ram, a): return int.from_bytes(ram[0][a:a+4], 'big')

# 1. LINK A7,#-8 pushes the original A7 (68000 hardware, per SingleStepTests)
cpu, ns, ram = cpu_with([0x4e57, 0xfff8, 0x4e71])
cpu.run(1)
check('LINK A7 stores original A7', rl(ram, 0x7ffc) == 0x8000 and ns['R'][15] == 0x7ff4,
      f'stored {rl(ram,0x7ffc):08x} A7={ns["R"][15]:08x}')
cpu, ns, ram = cpu_with([0x4e56, 0xfff8, 0x4e71]); ns['R'][14] = 0x12345678
cpu.run(1)
check('LINK A6 unchanged', rl(ram, 0x7ffc) == 0x12345678 and ns['R'][14] == 0x7ffc and ns['R'][15] == 0x7ff4)

# 2. trace: move #$a700,sr sets T; each following instruction takes vector 9
#    handler at 0x2000: addq.l #1,d7 ; rte
prog = [0x46fc, 0xa700, 0x7001, 0x7202, 0x7403, 0x46fc, 0x2700, 0x4e71, 0x4e71, 0x60fe]
cpu, ns, ram = cpu_with(prog)
ram[0][0x24:0x28] = (0x2000).to_bytes(4, 'big')
for i, w in enumerate([0x5287, 0x4e73]):
    ram[0][0x2000 + 2*i] = w >> 8; ram[0][0x2001 + 2*i] = w & 0xff
cpu.run(2000)
R = ns['R']
# budget accounting: one instruction run with a 1-cycle budget, even when it writes SR
c2, n2, _ = cpu_with([0x46fc, 0x2000, 0x7005, 0x4e71])
c2.run(1)
check('SR write counts as the whole budget step', n2['PC'] == 0x1004 and n2['R'][0] == 0, f'pc={n2["PC"]:x}')
# traced: moveq x3 and the move-to-sr that clears T (T set at its start) -> 4 traps
check('trace exception per traced instruction', R[7] == 4 and R[0] == 1 and R[1] == 2 and R[2] == 3,
      f'd7={R[7]} d0={R[0]} d1={R[1]} d2={R[2]}')
check('trace off again', not ns['srh'] & 0x8000)

# 3. IndexError inside the CPU loop is logged with a traceback
from opencdi.bus import Device
class Bad(Device):
    def read16(self, a): return [][1]
bus = Bus(internal_space=False); ram = bus.map_ram('r', 0, 0x10000); bus.map_device(0x100000, 0x10ffff, Bad())
cpu = M68070(bus); ns = cpu.ns
seen = []
ns['log_fault'] = lambda pc, op, exc: seen.append((pc, op, type(exc).__name__))
prog = [0x3039, 0x0010, 0x0000, 0x4e71]   # move.w $100000,d0
for i, w in enumerate(prog): ram[0][0x1000+2*i] = w >> 8; ram[0][0x1001+2*i] = w & 0xff
ram[0][8:12] = (0x3000).to_bytes(4, 'big'); ram[0][0x3000:0x3002] = b'\x60\xfe'
ns['R'][15] = 0x8000; ns['PC'] = 0x1000; ns['srh'] = 0x2700
cpu.run(100)
check('IndexError reported to log_fault', seen and seen[0][2] == 'IndexError', str(seen[:1]))

# 4. machine-level pieces with a fake machine
class Sched:
    clock = 15_000_000; now = 0
    def after(self, c, f):
        class E:
            def cancel(s): pass
        return E()
    def after_seconds(self, s, f): return self.after(0, f)
class FakeM:
    sched = Sched(); disc = None
    def log_serial(self, t): pass
    def request_reset(self): pass
from opencdi.scc68070 import SCC68070
m = FakeM(); scc = SCC68070(m); m.scc = scc
scc.write16(0x80002024, 0xff00)           # timer0 = ff00
m.sched.now = 96 * 0x10                    # 16 ticks later
before = scc.timer0_value()
scc.write16(0x80002022, 0x1234)            # reload write must not restart the count
check('timer reload keeps count', scc.timer0_value() == before == 0xff10 and scc.reload == 0x1234,
      f'{before:04x} -> {scc.timer0_value():04x}')
m.sched.now = 96 * 0x20
scc.write8(0x80002025, 0x00)               # low byte write merges with live count ff20
check('timer byte write merges live count', scc.timer0 == 0xff00 and scc.timer0_value() == 0xff00,
      f'{scc.timer0:04x}')

from opencdi.slave import Slave
from opencdi.cdic import CDIC
m.cdic = CDIC(m); sl = Slave(m)
sl.write8(0x310001 | (3 << 1), 0x80)       # port 3 command 0x80 expects 4 bytes on port 3 ...
for _ in range(40):
    sl.write8(0x310001, 0x55)               # ... but data arrives on port 0: used to overflow
check('slave overflow no longer raises', sl.in_index < 17)

c = m.cdic
check('default attenuation is straight stereo', c.atten == [0xff, 0, 0xff, 0])
ch = scc.dma[0]; ch.memory_address_counter = 0x1000; ch.transfer_counter = 4; ch.operation_control = 0x80
class FB:
    mem = {}
    def write_word(self, a, v): self.mem[a] = v
    def read_word(self, a): return self.mem.get(a, 0)
m.bus = FB(); c.ram[0:8] = bytes(range(1, 9)); ch.status = 0
c.dma_transfer(0)
check('DMA finished state', ch.transfer_counter == 0 and ch.status & 0x80 and ch.memory_address_counter == 0x1008
      and FB.mem[0x1000] == 0x0102 and FB.mem[0x1006] == 0x0708)
import numpy as np
got = []
m.audio_out = lambda l, r, rate: got.append((l, r))
c.atten = [0x80, 0, 0x80, 0]
c.play_cdda_sector(np.full(1176, 1000, dtype='<i2').tobytes())
check('CD-DA uses attenuation', abs(float(got[0][0][0]) - 1000 * 0x80 / 255) < 1e-3, str(got[0][0][0]))

# 5. byte swapped raw image reads like the normal one, audio tracks too
from opencdi.disc import Disc, SYNC
d = tempfile.mkdtemp(dir=WORK)
sec = bytearray(2352 * 20)
for i in range(20):
    s = bytearray(SYNC + bytes([0, 2, i, 2])) + bytes((i * 7 + k) & 0xff for k in range(2352 - 16))
    sec[i*2352:(i+1)*2352] = s
sw = bytearray(sec); sw[0::2], sw[1::2] = sec[1::2], sec[0::2]
audio = bytes((k * 3) & 0xff for k in range(2352 * 4)); asw = bytearray(audio); asw[0::2], asw[1::2] = audio[1::2], audio[0::2]
open(f'{d}/n.bin', 'wb').write(bytes(sec) + audio); open(f'{d}/s.bin', 'wb').write(bytes(sw) + bytes(asw))
for name in 'ns':
    open(f'{d}/{name}.cue', 'w').write(f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
                                       f'  TRACK 02 AUDIO\n    INDEX 01 00:00:20\n')
n, s = Disc(f'{d}/n.cue'), Disc(f'{d}/s.cue')
same = all(n.read_raw(l) == s.read_raw(l) for l in range(24))
check('byte swapped image reads the same (data + audio)', s.byteswapped and not n.byteswapped and same)
n.close(); s.close()

# 6. cursor follows the picture into the border in 720-pixel mode
from opencdi.mcd212 import MCD212, CURCNT_EN, DCR_CF_BIT
class VM:
    class scc:
        @staticmethod
        def int1_w(s): pass
pages = [bytearray(65536) for _ in range(8)]
v = MCD212(VM(), pages, pages)
v.cursor_ctrl = CURCNT_EN | 0xf
v.cursor_pos = (10 << 12) | 100
v.cursor_pattern[0] = 0x8000
for cf, expect in ((1, 100), (0, 124)):
    v.dcr[0] = cf << DCR_CF_BIT
    out = np.zeros((280, 768), dtype=np.uint32)
    v.draw_cursor(out)
    xs = np.nonzero(out[10])[0].tolist()
    check(f'cursor x in {v.screen_width()}-pixel mode', xs and xs[0] == expect, f'x={xs}')

logging.shutdown()
text = open(logpath).read()
for key in ('timer0 reload', 'input buffer overflow', 'DMA buffer->memory', 'byte swapped raw image'):
    check(f'debug log has "{key}"', key in text)
print('ALL PASS' if ok else 'SOME FAILED')
sys.exit(0 if ok else 1)
