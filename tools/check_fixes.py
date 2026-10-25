#!/usr/bin/env python3
"""Regression checks for the fixes in 0.1.1 and later (see docs/debug/).

Usage: tools/check_fixes.py

Runs without a BIOS or disc image.  Prints PASS/FAIL per check and exits
non-zero if any check fails.
"""
import atexit, sys, os, logging, shutil, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
WORK = tempfile.mkdtemp(prefix='opencdi-check-')
atexit.register(shutil.rmtree, WORK, True)
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

from opencdi.m68k_native import create_cpu, load as load_native

def cpu_checks(make, core):
    """Sections 1-3 for one CPU core (both cores must behave the same)."""
    def check_c(name, cond, info=''):
        check(f'{name} [{core}]', cond, info)
    def cpu_with(prog, at=0x1000, sp=0x8000, sr=0x2700):
        bus = Bus(internal_space=False); ram = bus.map_ram('r', 0, 0x100000)
        cpu = make(bus); ns = cpu.ns
        for i, w in enumerate(prog):
            ram[0][at + 2*i] = w >> 8; ram[0][at + 2*i + 1] = w & 0xff
        ns['R'][15] = sp; ns['srh'] = sr & 0xff00; ns['ccr'] = sr & 0x1f; ns['PC'] = at
        return cpu, ns, ram

    def rl(ram, a): return int.from_bytes(ram[0][a:a+4], 'big')

    # 1. LINK A7,#-8 pushes the original A7 (68000 hardware, per SingleStepTests)
    cpu, ns, ram = cpu_with([0x4e57, 0xfff8, 0x4e71])
    cpu.run(1)
    check_c('LINK A7 stores original A7', rl(ram, 0x7ffc) == 0x8000 and ns['R'][15] == 0x7ff4,
          f'stored {rl(ram,0x7ffc):08x} A7={ns["R"][15]:08x}')
    cpu, ns, ram = cpu_with([0x4e56, 0xfff8, 0x4e71]); ns['R'][14] = 0x12345678
    cpu.run(1)
    check_c('LINK A6 unchanged', rl(ram, 0x7ffc) == 0x12345678 and ns['R'][14] == 0x7ffc and ns['R'][15] == 0x7ff4)

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
    check_c('SR write counts as the whole budget step', n2['PC'] == 0x1004 and n2['R'][0] == 0, f'pc={n2["PC"]:x}')
    # traced: moveq x3 and the move-to-sr that clears T (T set at its start) -> 4 traps
    check_c('trace exception per traced instruction', R[7] == 4 and R[0] == 1 and R[1] == 2 and R[2] == 3,
          f'd7={R[7]} d0={R[0]} d1={R[1]} d2={R[2]}')
    check_c('trace off again', not ns['srh'] & 0x8000)

    # 3. IndexError inside the CPU loop is logged with a traceback
    from opencdi.bus import Device
    class Bad(Device):
        def read16(self, a): return [][1]
    bus = Bus(internal_space=False); ram = bus.map_ram('r', 0, 0x10000); bus.map_device(0x100000, 0x10ffff, Bad())
    cpu = make(bus); ns = cpu.ns
    seen = []
    ns['log_fault'] = lambda pc, op, exc: seen.append((pc, op, type(exc).__name__))
    prog = [0x3039, 0x0010, 0x0000, 0x4e71]   # move.w $100000,d0
    for i, w in enumerate(prog): ram[0][0x1000+2*i] = w >> 8; ram[0][0x1001+2*i] = w & 0xff
    ram[0][8:12] = (0x3000).to_bytes(4, 'big'); ram[0][0x3000:0x3002] = b'\x60\xfe'
    ns['R'][15] = 0x8000; ns['PC'] = 0x1000; ns['srh'] = 0x2700
    cpu.run(100)
    check_c('IndexError reported to log_fault', seen and seen[0][2] == 'IndexError', str(seen[:1]))



cpu_checks(lambda bus: M68070(bus), 'python')
if load_native() is not None:
    cpu_checks(lambda bus: create_cpu(bus, native=True), 'native')
else:
    check('native CPU core builds and loads', False, 'see the debug log')

# 4. machine-level pieces with a fake machine
class Sched:
    clock = 15_000_000; now = 0
    def after(self, c, f):
        class E:
            def cancel(s): pass
        return E()
    def after_seconds(self, s, f): return self.after(0, f)
    def at(self, t, f): return self.after(0, f)
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
    rp = wp = [None] * 65536            # no RAM pages: the word-by-word path
    def write_word(self, a, v): self.mem[a] = v
    def read_word(self, a): return self.mem.get(a, 0)
m.bus = FB(); c.ram[0:8] = bytes(range(1, 9)); ch.status = 0
c.dma_transfer(0)
check('DMA finished state', ch.transfer_counter == 0 and ch.status & 0x80 and ch.memory_address_counter == 0x1008
      and FB.mem[0x1000] == 0x0102 and FB.mem[0x1006] == 0x0708)
# 0.1.3: a transfer within one RAM page is one slice copy, with the same result
rb = Bus(internal_space=False); rpages = rb.map_ram('r', 0, 0x20000); rb.cpu_functions()
m.bus = rb
c.ram[0:0x4000] = bytes((i * 7) & 0xff for i in range(0x4000))
for start, dev, words in ((0x1000, 0x100, 1000), (0xfff0, 0x10, 16), (0x2000, 0x3ff0, 16)):
    for pg in rpages: pg[:] = bytes(len(pg))
    ch.memory_address_counter = start; ch.transfer_counter = words; ch.operation_control = 0x80
    c.dma_transfer(dev)
    want = bytes(c.ram[(dev + 2 * i + k) & 0x3fff] for i in range(words) for k in (0, 1))
    got_b = bytes(rpages[(start + j) >> 16][(start + j) & 0xffff] for j in range(2 * words))
    check(f'DMA to RAM {start:#x} ({words} words) matches the word loop', got_b == want)
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

# 5b. cue sheets with the CD-i track types CDI/2352 and CDI/2336 load as Mode 2
raw16 = bytes(sec)                                  # 20 raw sectors from above
mode2 = b''.join(raw16[i*2352 + 16:(i+1)*2352] for i in range(20))   # same sectors, 2336 bytes each
open(f'{d}/c52.bin', 'wb').write(raw16); open(f'{d}/c36.bin', 'wb').write(mode2)
for name, mode in (('c52', 'CDI/2352'), ('c36', 'cdi/2336')):
    open(f'{d}/{name}.cue', 'w').write(f'FILE "{name}.bin" BINARY\n  TRACK 01 {mode}\n    INDEX 01 00:00:00\n')
c52, c36 = Disc(f'{d}/c52.cue'), Disc(f'{d}/c36.cue')
check('CDI/2352 cue loads as mode2', [(t.kind, t.sector_size, t.length) for t in c52.tracks] == [('mode2', 2352, 20)])
check('CDI/2336 cue loads as mode2', [(t.kind, t.sector_size, t.length) for t in c36.tracks] == [('mode2', 2336, 20)])
# the 2336-byte reader builds its own (BCD) header, so compare from the mode byte on
check('CDI/2336 sectors read like the raw ones', all(c52.read_raw(l)[15:] == c36.read_raw(l)[15:] for l in range(20)))
c52.close(); c36.close()
# 5c. a renamed image whose cue sheet still names the old file (0.1.3)
os.makedirs(f'{d}/ren', exist_ok=True)
shutil.copy(f'{d}/c52.bin', f'{d}/ren/My_Game.bin')
open(f'{d}/ren/My_Game.cue', 'w').write('FILE "My Game (Europe).bin" BINARY\n  TRACK 01 CDI/2352\n    INDEX 01 00:00:00\n')
rn = Disc(f'{d}/ren/My_Game.cue')
check('renamed .bin found by the cue sheet name', rn.tracks[0].path.name == 'My_Game.bin' and rn.read_raw(3) == c52.read_raw(3))
rn.close()
open(f'{d}/ren/Other.cue', 'w').write('FILE "missing.bin" BINARY\n  TRACK 01 CDI/2352\n    INDEX 01 00:00:00\n')
try:
    Disc(f'{d}/ren/Other.cue')
    check('missing .bin gives a clear error', False)
except ValueError as e:
    check('missing .bin gives a clear error', '"missing.bin" (named in the cue sheet) not found' in str(e), str(e))
try:
    open(f'{d}/bad.cue', 'w').write('FILE "c52.bin" BINARY\n  TRACK 01 MODE3/2352\n    INDEX 01 00:00:00\n')
    Disc(f'{d}/bad.cue')
    check('unknown track mode still rejected', False)
except ValueError as e:
    check('unknown track mode still rejected', 'MODE3/2352' in str(e), str(e))

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

# ----- 0.1.3: audio stream and frame cache ---------------------------------------
# 7. the streaming resampler gives the same output however the input is split
from opencdi.audio import LinearResampler
t = np.arange(37800)
x = np.stack([np.sin(t * 0.05), np.cos(t * 0.031)], 1).astype(np.float32) * 10000
rng = np.random.default_rng(7)
for step in (37800 / 44100, 18900 / 44100, 1.004):
    whole = LinearResampler().process(x, step)
    r, parts, i = LinearResampler(), [], 0
    while i < len(x):
        n = int(rng.integers(1, 2500)); parts.append(r.process(x[i:i + n], step)); i += n
    chunked = np.concatenate(parts)
    check(f'resampler seamless across blocks (step {step:.3f})',
          len(chunked) == len(whole) and np.array_equal(chunked, whole))

# 8. the audio stream survives slow frames: simulated sound card on an exact clock,
#    CD-DA delivered per frame (588/1176 samples), some frames 70 ms late
import pygame
from opencdi.frontend import Audio
class FakeChannel:
    """Plays blocks back to back on a simulated clock and records gaps."""
    def __init__(self, clock, rate):
        self.clock, self.rate = clock, rate
        self.cur_end = None; self.queued = None; self.gaps = 0; self.idle_since = None
    def _update(self):
        now = self.clock[0]
        while self.cur_end is not None and now >= self.cur_end:
            if self.queued is not None:
                self.cur_end += len(self.queued) / self.rate; self.queued = None
            else:
                self.idle_since = self.cur_end; self.cur_end = None
    def get_busy(self):
        self._update(); return self.cur_end is not None
    def get_queue(self):
        self._update(); return self.queued
    def play(self, snd):
        self._update()
        if self.idle_since is not None:
            self.gaps += 1
        self.cur_end = self.clock[0] + len(snd) / self.rate; self.queued = None; self.idle_since = None
    def queue(self, snd):
        self._update(); self.queued = snd
    def stop(self):
        self.cur_end = self.queued = None

def simulate(feeder):
    clock = [0.0]
    ch = FakeChannel(clock, 44100)
    au = Audio(channel=ch, feeder=False)
    au.make_sound = lambda blk: blk.copy()
    fps = 15_000_000 / (312 * 960)
    sectors = lambda k: int((k + 1) * 75 / fps) - int(k * 75 / fps)
    next_feed = 0.0
    for k in range(int(fps * 30)):                     # 30 s of CD audio
        due = k / fps + (0.070 if k % 47 == 0 and k else 0.0)   # every 47th frame 70 ms late
        while next_feed < due:                         # feeder thread keeps running meanwhile
            clock[0] = next_feed
            if feeder:
                au.service()
            next_feed += Audio.FEED_INTERVAL
        clock[0] = max(clock[0], due)
        au.push(np.full((588 * sectors(k), 2), 1000, dtype=np.int16))
        au.service()
    return au.stats['underruns'], ch.gaps
under, gaps = simulate(feeder=True)
check('audio: no gaps with late frames (feeder thread)', under == 0 and gaps == 0, f'underruns {under}, gaps {gaps}')
under, gaps = simulate(feeder=False)
check('audio: the same test without the feeder does find gaps (test is meaningful)', gaps > 0,
      f'underruns {under}, gaps {gaps}')

# 9. frame cache: unchanged input reuses the picture, any change in the bytes the
#    picture reads re-renders it, changes elsewhere in plane memory do not
class FM:
    frame = None
    class scc:
        @staticmethod
        def int1_w(s): pass
    def frame_ready(self, f): self.frame = f
pa = [bytearray(65536) for _ in range(8)]; pb = [bytearray(65536) for _ in range(8)]
fm = FM(); v = MCD212(fm, pa, pb)
v.icm = 1                                  # plane A CLUT8, plane B off
v.tcr = 0x08                               # plane A opaque (transparency control 0 = always transparent)
v.set_register(0, 0xdb, 63)                # full plane A weight, as the ROM sets it (reset value 0 is black)
for i in range(256):
    v.clut[i] = (i << 16) | ((255 - i) << 8) | (i * 7 & 0xff)
v.clut_ver += 1
for i in range(0x10000):
    pa[1][i] = i * 13 & 0xff               # picture data at 0x10000
def frame():
    v.set_vsr(0, 0x10000)
    for line in range(v.total_height):
        v.scanline(line)
    return fm.frame.copy()
f1 = frame(); f2 = frame()
check('frame cache test picture is visible', len(np.unique(f1)) > 100, f'{len(np.unique(f1))} colours')
check('frame cache reuses an unchanged picture', v.render_stats[:2] == [1, 1] and np.array_equal(f1, f2),
      str(v.render_stats))
pa[0][0x1000] ^= 0xff                      # OS-9 variables in plane A, not displayed
f3 = frame()
check('frame cache ignores plane memory the picture does not read', v.render_stats[:2] == [1, 2]
      and np.array_equal(f1, f3), str(v.render_stats))
pa[1][100] ^= 0xff                         # first displayed line, pixel 100
f4 = frame()
fresh = MCD212(FM(), pa, pb); fresh.icm, fresh.tcr, fresh.clut, fresh.clut_ver = 1, 0x08, list(v.clut), 1
fresh.set_register(0, 0xdb, 63)
fresh.set_vsr(0, 0x10000)
for line in range(fresh.total_height):
    fresh.scanline(line)
check('frame cache re-renders when displayed bytes change', v.render_stats[:2] == [2, 2]
      and not np.array_equal(f1, f4) and np.array_equal(f4, fresh.m.frame), str(v.render_stats))
v.skip_render = True
before = fm.frame
frame()
check('frame skip keeps the previous picture', v.render_stats[2] == 1 and fm.frame is before)
v.skip_render = False

# 10. plane_line() caches the decoder state; register writes from the CPU and
#     from ICA/DCA programs must still reach the next line
v.set_vsr(0, 0x10000)
k1 = v.plane_line(0).kind
v.write16(0x18, 0x0200)                    # CPU: plane A DDR file type = run-length
v.set_vsr(0, 0x10000)
k2 = v.plane_line(0).kind
v.write16(0x18, 0)
v.set_register(0, 0xc0, 0)                 # ICA/DCA: image coding off
v.set_vsr(0, 0x10000)
k3 = v.plane_line(0).kind
v.set_register(0, 0xc0, 1)
check('plane decoder state follows register writes', (k1, k2, k3) == ('bmp', 'rle', 'off'), f'{k1} {k2} {k3}')

# 11. decoded DCA blocks are replayed only while their bytes are unchanged
from opencdi.mcd212 import DCR_DE_BIT, DCR_ICA_BIT, DCR_DCA_BIT
def dca_block(value):
    pa[0][0x2000:0x2040] = bytes(64)
    pa[0][0x2000:0x2004] = (0x80 << 24 | value).to_bytes(4, 'big')   # CLUT entry 0
    v.dca[0] = 0x2000
    v.process_dca(0)
    return v.clut[0]
v.dcr[0] = 1 << DCR_DE_BIT | 1 << DCR_ICA_BIT | 1 << DCR_DCA_BIT
c1 = dca_block(0x102030); c2 = dca_block(0x102030); c3 = dca_block(0x405060)
check('DCA block cache re-decodes changed bytes', (c1, c2, c3) == (0x102030, 0x102030, 0x405060)
      and v.dca[0] == 0x2040, f'{c1:x} {c2:x} {c3:x} dca={v.dca[0]:x}')

# 11b. lines start on a fixed timeline: a CPU that runs past each line end must
#      not make frames longer (Alice in Wonderland: up to 22 % longer frames)
from opencdi.machine import Machine, LINE_CYCLES
from opencdi.sched import Scheduler
class LineVideo:
    total_height = 262
    def __init__(self, m): self.m = m; self.skip_render = False
    def scanline(self, line):
        if line == self.total_height - 1:
            self.m.frames += 1
class LineM:
    _reset_pending = False; serial_input = None; frames = 0; frame = None; line = 0; _line_t = 0
    def __init__(self):
        self.sched = Scheduler(15_000_000); self.video = LineVideo(self)
    def run_until(self, t):                   # every slice overruns by 150 cycles, like a long movem
        while self.sched.now < t:
            self.sched.now += (t - self.sched.now) + 150
lm = LineM()
for _ in range(10):
    Machine.run_frame(lm)
per = lm.sched.now / 10
check('frames keep their length when the CPU overruns lines', abs(per - 262 * LINE_CYCLES) <= 150,
      f'{per:.0f} cycles per frame, nominal {262 * LINE_CYCLES}')
# the CD drive's 75 Hz ticks ignore when the tick actually ran
class TickM:
    class scc:
        @staticmethod
        def set_in(n, state): pass
    def __init__(self): self.sched = Scheduler(15_000_000); self.disc = None
    def log_serial(self, t): pass
    def request_reset(self): pass
tm = TickM(); tc = CDIC(tm); ticks = [0]
orig_tick = tc.sector_tick
def counted():
    ticks[0] += 1
    orig_tick()
tc.sector_tick = counted
tc.reset()
while tm.sched.now < 15_000_000 * 10:
    nxt = tm.sched.next_time()
    tm.sched.now = nxt + 400                  # every event runs 400 cycles late
    tm.sched.run_due()
check('CD sector ticks stay at 75 per second', ticks[0] in (749, 750, 751), f'{ticks[0]} ticks in 10 s')

# ----- 0.1.3: 60 Hz and the emulation thread -------------------------------------
# 12. the launcher defaults to NTSC (60 Hz); a stored choice is kept
from opencdi.launcher import use_ntsc
check('video defaults to NTSC (60 Hz)', use_ntsc({}) and use_ntsc({'ntsc': True}) and not use_ntsc({'ntsc': False}))

# 13. the emulation thread: runs frames and shows them, quits cleanly, and an
#     error in it reaches the main thread instead of being lost
import threading
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
pygame.display.init()
from opencdi.frontend import Emulator
class FakeInput:
    mouse_captured = False
    touch = None
    def update(self): return 0, 0, 0
    def pad_state(self): return 0, 0, False, False, False
    def handle_event(self, ev): return False
class FakeApp:
    audio = None
    input = FakeInput()
    fullscreen = False
    def __init__(self): self.window = pygame.display.set_mode((16, 8))
    def set_content_size(self, size): pass
    def note_resize(self, size): pass
class FakeMachine:
    frame = None
    disc = None
    def __init__(self, fail_at=None, quit_at=None):
        self.n, self.fail_at, self.quit_at, self.saved = 0, fail_at, quit_at, False
        self.video = type('V', (), {'render_stats': [0, 0, 0]})()
    def frame_rate(self): return 60.0
    def set_pointer(self, x, y, b): pass
    def take_audio(self): return None
    def save(self): self.saved = True
    def run_frame(self, render=True):
        self.n += 1
        if self.n == self.fail_at:
            raise RuntimeError('boom')
        if self.n == self.quit_at:
            pygame.event.post(pygame.event.Event(pygame.QUIT))
        self.frame = np.full((8, 16), self.n, dtype=np.uint32)
        return self.frame
drawn = []
em = Emulator(FakeApp(), FakeMachine(quit_at=30))
em.draw = lambda f: drawn.append(int(f[0, 0]))
switch = sys.getswitchinterval()
res = em.run()
check('emulation thread runs frames and quits', res == 'quit' and em.m.saved and em.frames >= 30
      and len(drawn) > 10 and drawn == sorted(drawn), f'{res} frames={em.frames} drawn={len(drawn)}')
em = Emulator(FakeApp(), FakeMachine(fail_at=5))
try:
    em.run()
    check('emulation thread error reaches the main thread', False)
except RuntimeError as e:
    check('emulation thread error reaches the main thread', str(e) == 'boom')
check('emulation thread stopped, switch interval restored',
      not any(t.name == 'emulation' for t in threading.enumerate()) and sys.getswitchinterval() == switch)

# ----- 0.1.4: Android touch controls ------------------------------------------------
# 14. drag moves the pointer, a tap clicks button 1, the buttons hold, MENU is
#     taken once; on a squarer screen the buttons go over the picture
import time as _time
from opencdi.touch import TouchControls, CLICK_FRAMES, TAP_TIME
tc = TouchControls()
LW, LH = 1067, 480                             # a 20:9 phone at 480 logical lines
tc.layout((LW, LH), pygame.Rect(149, 0, 768, 480))
def fe(kind, x, y, fid, dx=0, dy=0):
    return pygame.event.Event(kind, x=x / LW, y=y / LH, dx=dx / LW, dy=dy / LH, finger_id=fid, touch_id=0, pressure=1)
tc.handle_event(fe(pygame.FINGERDOWN, 400, 200, 1))
for i in range(10):
    tc.handle_event(fe(pygame.FINGERMOTION, 400, 200, 1, dx=10, dy=-4))
_time.sleep(TAP_TIME + 0.05)
tc.handle_event(fe(pygame.FINGERUP, 500, 160, 1))
d = tc.take()
check('touch: drag moves the pointer, no click', d == (100, -40, 0), str(d))
tc.handle_event(fe(pygame.FINGERDOWN, 300, 300, 2)); tc.handle_event(fe(pygame.FINGERUP, 300, 300, 2))
taps = [tc.take()[2] for _ in range(CLICK_FRAMES + 2)]
check('touch: a tap is a short button 1 click', taps == [1] * CLICK_FRAMES + [0, 0], str(taps))
bt = {b.name: b for b in tc.buttons}
check('touch: buttons beside the picture on a phone', all(b.center[0] - b.radius >= 917 for k, b in bt.items() if k != 'menu')
      and bt['menu'].center[0] + bt['menu'].radius <= 149, str({k: (b.center, b.radius) for k, b in bt.items()}))
tc.handle_event(fe(pygame.FINGERDOWN, *bt['b2'].center, 3)); s2 = tc.take()[2]
tc.handle_event(fe(pygame.FINGERDOWN, *bt['b1'].center, 4)); s12 = tc.take()[2]
tc.handle_event(fe(pygame.FINGERUP, *bt['b1'].center, 4)); tc.handle_event(fe(pygame.FINGERUP, *bt['b2'].center, 3))
check('touch: hold 2, then 1 and 2, then nothing', (s2, s12, tc.take()[2]) == (2, 3, 0), f'{s2} {s12}')
tc.handle_event(fe(pygame.FINGERDOWN, *bt['menu'].center, 5))
check('touch: MENU is taken once', (tc.take_menu(), tc.take_menu()) == (True, False))
tc2 = TouchControls(); tc2.layout((768, 480), pygame.Rect(0, 0, 768, 480))
check('touch: buttons over the picture on a 4:3 screen',
      all(0 <= b.center[0] < 768 and 0 <= b.center[1] < 480 for b in tc2.buttons))

# ----- 0.1.5: CHD disc images ----------------------------------------------------------
# 15. FLAC (CHD audio hunks): a small encoder here makes fixed-predictor,
#     rice-coded frames, independent and mid/side; both decoders must return
#     the samples exactly
sys.path.insert(0, str(Path(__file__).resolve().parent))
from opencdi import flac as flacmod, chd as chdmod
class BitW:
    def __init__(self): self.v, self.n = 0, 0
    def w(self, x, k): self.v = (self.v << k) | (x & ((1 << k) - 1)); self.n += k
    def align(self): self.w(0, (-self.n) % 8)
    def bytes(self): self.align(); return self.v.to_bytes(self.n // 8, 'big')
def flac_frame(bw, left, right, mid_side):
    n = len(left)
    bw.w(0x7ffc, 15); bw.w(0, 1); bw.w(7, 4); bw.w(9, 4); bw.w(10 if mid_side else 1, 4); bw.w(4, 3); bw.w(0, 1)
    bw.w(0, 8); bw.w(n - 1, 16); bw.w(0, 8)
    if mid_side:
        chans = [[(l + r) >> 1 for l, r in zip(left, right)], [l - r for l, r in zip(left, right)]]
        bps = [16, 17]
    else:
        chans, bps = [left, right], [16, 16]
    for x, b in zip(chans, bps):
        bw.w(0, 1); bw.w(8 + 2, 6); bw.w(0, 1)                 # fixed predictor, order 2
        bw.w(x[0], b); bw.w(x[1], b)
        res = [x[i] - 2 * x[i - 1] + x[i - 2] for i in range(2, n)]
        u = [(r << 1) ^ (r >> 63) for r in res]
        k = max(0, (sum(u) // max(len(u), 1)).bit_length() - 1)
        bw.w(0, 2); bw.w(0, 4); bw.w(k, 4)                     # rice, one partition
        for v in u:
            q = v >> k
            bw.w(0, q); bw.w(1, 1); bw.w(v & ((1 << k) - 1), k)
    bw.align(); bw.w(0, 16)
rng = np.random.default_rng(3)
t = np.arange(1176 * 4)
L = (np.sin(t * 0.02) * 9000 + rng.normal(0, 200, len(t))).astype(int).tolist()
R = (np.cos(t * 0.017) * 7000 + rng.normal(0, 300, len(t))).astype(int).tolist()
bw = BitW()
for i in range(4):
    flac_frame(bw, L[i * 1176:(i + 1) * 1176], R[i * 1176:(i + 1) * 1176], mid_side=bool(i & 1))
stream = bw.bytes() + b'TAIL'
(pl, pr), used = flacmod.decode_frames(stream, len(L))
check('FLAC (Python): fixed predictor, rice, mid/side', pl == L and pr == R and used == len(stream) - 4, f'used {used}')
want = np.empty((len(L), 2), dtype='>i2'); want[:, 0] = L; want[:, 1] = R
if chdmod._native() is not None:
    import ctypes
    out = ctypes.create_string_buffer(len(L) * 4)
    used_n = chdmod._native().flac_decode_cd(stream, len(stream), out, len(L))
    check('FLAC (native) gives the same', out.raw == want.tobytes() and used_n == used, f'used {used_n}')

# 16. CHD: written by tools/chd_writer.py (no chdman needed) with compressed,
#     uncompressed and "copy from self" hunks, ECC stripped from Mode 1 and
#     Mode 2 form 1 sectors, three tracks (padding between them) and audio
#     stored big-endian; every sector must read back as in the .bin
from chd_writer import write_cd_chd
from opencdi.disc import msf as _msf
def raw_sector(lba, mode, form2=False):
    s = bytearray(2352); s[0:12] = chdmod.SYNC; s[12:15] = bytes(_msf(lba)); s[15] = mode
    if mode == 1:
        s[16:2068] = rng.integers(0, 256, 2052, dtype=np.uint8).tobytes()
    else:
        s[16:24] = bytes([1, 0, 0x20 if form2 else 0x08, 0]) * 2
        s[24:2352 if form2 else 2076] = rng.integers(0, 256, 2328 if form2 else 2052, dtype=np.uint8).tobytes()
    if not form2:
        chdmod.ecc_generate(s)
    return bytes(s)
m1 = [raw_sector(i, 1) for i in range(37)]
au = [rng.integers(0, 256, 2352, dtype=np.uint8).tobytes() for _ in range(16)] * 2 + [bytes(2352)] * 5
m2 = [raw_sector(37 + len(au) + i, 2, form2=i % 3 == 2) for i in range(29)]
want_s = m1 + au + m2
regen = [0]
_orig_ecc = chdmod.ecc_generate
def _count_ecc(sec):
    regen[0] += 1
    _orig_ecc(sec)
codecs = ['cdzl'] + (['cdlz'] if chdmod.lzma is not None or chdmod._native() is not None else [])
if chdmod._zstd() is not None:
    codecs.append('cdzs')
for codec in codecs:
    p = os.path.join(WORK, f'test_{codec}.chd')
    write_cd_chd(p, [('MODE1_RAW', m1), ('AUDIO', au), ('MODE2_RAW', m2)], codec, store={2})
    chdmod.ecc_generate = _count_ecc
    regen[0] = 0
    dd = Disc(p)
    got = [dd.read_raw(i) for i in range(len(want_s))]
    chdmod.ecc_generate = _orig_ecc
    kinds = sorted({e[0] for e in dd.chd.map})
    check(f'CHD {codec}: every sector as in the .bin', got == want_s
          and [(t.kind, t.start, t.length) for t in dd.tracks] == [('mode1', 0, 37), ('audio', 37, 37), ('mode2', 74, 29)]
          and kinds == [0, 4, 5] and regen[0] == 37 + 20 - 8,      # hunk 2 (8 Mode 1 sectors) is stored as is
          f'hunk types {kinds}, ECC rebuilt {regen[0]}')
    dd.close()
if 'cdzs' not in codecs:
    print('SKIP CHD cdzs (no zstd module on this Python)')
for name, content, expect in (('v4.chd', b'MComprHD' + (124).to_bytes(4, 'big') + (4).to_bytes(4, 'big') + bytes(112),
                               'version 4 is not supported'),
                              ('junk.chd', b'hello', 'not a CHD file')):
    p = os.path.join(WORK, name); open(p, 'wb').write(content)
    try:
        Disc(p)
        check(f'CHD error message ({name})', False)
    except ValueError as e:
        check(f'CHD error message ({name})', expect in str(e), str(e))

# ----- 0.1.6: save states ----------------------------------------------------------
# 17. a loaded state continues exactly as the machine did after saving it: CPU,
#     RAM, timers, pending events.  The test ROM keeps writing the SCC timer's
#     count into RAM, so any timing difference shows up in memory.
from opencdi import savestate
from opencdi.machine import Machine as _Machine
rom = bytearray(b'\xff' * 0x80000)
rom[0:8] = (0x0007fff0).to_bytes(4, 'big') + (0x00400100).to_bytes(4, 'big')
prog = bytes.fromhex('41f900500000' '323980002024' '30c1' '5280' 'b1fc00560000' '66ee' '60e6')
rom[0x100:0x100 + len(prog)] = prog
rom_path = os.path.join(WORK, 'test.rom'); open(rom_path, 'wb').write(rom)
def st_machine():
    return _Machine(rom_path, nvram_path=os.path.join(WORK, 'st.nvram'), ntsc=True, log=lambda t: None)
def st_run(mm, n):
    for i in range(n):
        mm.set_pointer(i * 5, i * 3, i & 1)
        mm.run_frame()
    return (mm.sched.now, mm.cpu.pc, bytes(b''.join(mm.bus.regions['ram5'])), mm.frame.tobytes())
ma = st_machine(); st_run(ma, 40)
spath = os.path.join(WORK, 'slot1.state')
savestate.save(ma, spath)
ptr = (ma.slave.input_x, ma.slave.input_y, ma.slave.input_buttons)
want = st_run(ma, 25)
mb = st_machine(); st_run(mb, 3)
mb.set_pointer(*ptr)
savestate.load(mb, spath)
got = st_run(mb, 25)
check('save state: a new machine continues exactly as the saved one', got == want and got[0] > 0,
      f'cycles {got[0]} vs {want[0]}')
ma.set_pointer(*ptr)
savestate.load(ma, spath)
check('save state: loading into the running machine goes back exactly', st_run(ma, 25) == want)
meta, thumb = savestate.read_meta(spath)
check('save state: meta and picture can be read', meta['disc_id'] == 'nodisc' and thumb is not None
      and len(thumb[2]) == thumb[0] * thumb[1] * 3, str(meta.get('thumb')))
# 18. refused states leave the running game alone
def refused(path, expect):
    mc = st_machine(); st_run(mc, 10)
    snap = savestate.capture(mc)
    try:
        savestate.load(mc, path)
        return False, 'loaded'
    except savestate.StateError as e:
        same = savestate.capture(mc)
        return expect in str(e) and same[0] == snap[0] and same[1] == snap[1], str(e)
import json as _json, zlib as _zlib, struct as _struct
data = open(spath, 'rb').read()
_, _, o = savestate._read_head(data, spath)
body = _zlib.decompress(data[o:])
n = _struct.unpack_from('>I', body)[0]
stj = _json.loads(body[4:4 + n]); del stj['devices']['cdic']
head = _json.dumps(stj).encode()
broken = os.path.join(WORK, 'broken.state')
open(broken, 'wb').write(data[:o] + _zlib.compress(_struct.pack('>I', len(head)) + head + body[4 + n:]))
meta_o = dict(meta, rom_crc=1)
meta_b = _json.dumps(meta_o).encode()
ml = _struct.unpack_from('>I', data, len(savestate.MAGIC))[0]
other_bios = (savestate.MAGIC + _struct.pack('>I', len(meta_b)) + meta_b +
              data[len(savestate.MAGIC) + 4 + ml:])
for name, content, expect in (('junk.state', b'hello', 'not an OpenCDi state file'),
                              ('cut.state', data[:len(data) - 50], 'damaged'),
                              ('broken.state', None, 'damaged'),
                              ('otherbios.state', other_bios, 'another BIOS')):
    path = broken if content is None else os.path.join(WORK, name)
    if content is not None:
        open(path, 'wb').write(content)
    good, info = refused(path, expect)
    check(f'save state: {name} refused, machine unchanged', good, info)
check('save state: the hotkeys are in the help', 'F5 save state' in __import__('opencdi.frontend').frontend.HELP)

logging.shutdown()
text = open(logpath).read()
for key in ('timer0 reload', 'input buffer overflow', 'DMA buffer->memory', 'byte swapped raw image'):
    check(f'debug log has "{key}"', key in text)
print('ALL PASS' if ok else 'SOME FAILED')
sys.exit(0 if ok else 1)
