"""The CD-i Mono-I board (Philips CD-i 220 F2 / Magnavox CD-i 200).

Memory map (from MAME's cdi.cpp):
    000000-07ffff  plane A RAM          200000-27ffff  plane B RAM
    300000-303fff  CDIC                 310000-317fff  SLAVE
    320000-323fff  MK48T08 NVRAM/clock  400000-47ffff  system ROM
    4fffe0-4fffff  MCD212 registers     500000-57ffff  RAM
    d00000-dfffff  RAM                  80000000-      SCC68070 peripherals
Unmapped addresses raise a bus error, which the ROM uses to size memory.
"""

import traceback
import zlib
from pathlib import Path

import numpy as np

from .bus import Bus, Device
from . import debuglog
from .cdic import CDIC
from .m68k import M68070
from .mcd212 import MCD212
from .nvram import MK48T08
from .scc68070 import SCC68070
from .sched import Scheduler
from .slave import Slave

dlog = debuglog.get('machine')

CPU_CLOCK_PAL = 15_000_000        # 30 MHz crystal / 2
CPU_CLOCK_NTSC = 15_104_900       # 30.2098 MHz / 2
LINE_CYCLES = 960                 # 64 us video line

KNOWN_ROMS = {
    0x40c4e6b9: 'Magnavox CD-i 200',
    0x279683ca: 'Philips CD-i 220 F2',
    0x584c0af8: 'Philips CD-i 220 (alt, does not boot in MAME)',
}

# ROMs for other CD-i boards, recognised so the user gets a clear message
OTHER_BOARDS = {
    0xe115f45b: 'Philips CD-i 490 (Mono-IV board)',
    0xe2f200f6: 'Philips CD-i 490 (Mono-IV board)',
}


def check_bios(path):
    """Inspect a system ROM file.

    Returns (usable, description).  ``usable`` is False when the file cannot
    run on the emulated Mono-I board; unknown 512 KiB ROMs are allowed but
    described as untested.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        return False, f'cannot read file: {e.strerror or e}'
    if len(data) != 0x80000:
        return False, f'not a CD-i system ROM (expected 512 KiB, file is {len(data):,} bytes)'
    crc = zlib.crc32(data)
    if crc in KNOWN_ROMS:
        return True, KNOWN_ROMS[crc]
    if crc in OTHER_BOARDS:
        return False, f'{OTHER_BOARDS[crc]}: different hardware, not supported'
    if data[4:7] != b'\x00\x40\x00':
        return False, 'not a CD-i Mono-I system ROM (reset vector does not point into ROM)'
    return True, f'unknown ROM (crc {crc:08x}), untested'


class Quiet(Device):
    """Address range that ignores writes and reads as zero."""

    def read16(self, a):
        return 0

    def read8(self, a):
        return 0

    def write16(self, a, v):
        pass

    def write8(self, a, v):
        pass


class Machine:
    def __init__(self, rom_path, disc=None, nvram_path=None, ntsc=False, log=print):
        self.log = log
        rom = Path(rom_path).read_bytes()
        if len(rom) != 0x80000:
            raise ValueError(f'{rom_path}: expected a 512 KiB CD-i system ROM, got {len(rom)} bytes')
        crc = zlib.crc32(rom)
        self.rom_name = KNOWN_ROMS.get(crc, f'unknown ROM (crc {crc:08x})')
        self.rom = rom
        self.disc = disc
        self.ntsc = ntsc
        self.sched = Scheduler(CPU_CLOCK_NTSC if ntsc else CPU_CLOCK_PAL)
        self.serial_lines = []
        self.lcd_text = ''
        self.audio_chunks = []
        self.audio_enabled = True
        self.frame = None
        self.frames = 0
        self._reset_pending = False
        self.serial_input = None   # queue of bytes for the UART, set by the frontend

        bus = self.bus = Bus()
        self.plane_a = bus.map_ram('planeA', 0x000000, 0x80000)
        self.plane_b = bus.map_ram('planeB', 0x200000, 0x80000)
        bus.map_rom('rom', 0x400000, rom)
        bus.map_ram('ram5', 0x500000, 0x80000)
        bus.map_ram('dvcram', 0xd00000, 0x100000)

        self.scc = SCC68070(self)
        self.cdic = CDIC(self)
        self.slave = Slave(self)
        self.slave.ntsc = ntsc
        self.nvram = MK48T08(nvram_path)
        self.video = MCD212(self, self.plane_a, self.plane_b)

        bus.set_internal(self.scc)
        bus.map_device(0x300000, 0x303fff, self.cdic)
        bus.map_device(0x310000, 0x317fff, self.slave)
        bus.map_device(0x318000, 0x31ffff, Quiet())
        bus.map_device(0x320000, 0x323fff, self.nvram)
        bus.map_device(0x4fffe0, 0x4fffff, self.video)

        self.cpu = M68070(bus)
        self.scc.cpu = self.cpu
        self.scc.iack_callbacks = {4: self.cdic.intack}
        ns = self.cpu.ns
        ns['iack'] = self.scc.iack
        ns['reset_devices'] = self.reset_peripherals
        ns['log_fault'] = self._log_fault
        ns['log_trace'] = self._log_trace
        self._faults = 0
        self.reset()

    # ----- reset ---------------------------------------------------------------
    def reset_peripherals(self):
        self.scc.reset()
        self.cdic.reset()
        self.slave.reset()
        self.video.reset()

    def _log_fault(self, pc, op, exc):
        # an IndexError inside the CPU loop is turned into a guest bus error;
        # record where it came from so emulator bugs are not hidden
        self._faults += 1
        if self._faults <= 50:
            tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            dlog.warning('IndexError at pc=%08x op=%04x turned into a bus error\n%s', pc, op, tb)
        elif self._faults == 51:
            dlog.warning('more IndexError faults; no longer logging them')

    def _log_trace(self, pc, op):
        dlog.debug('trace exception after pc=%08x op=%04x', pc, op)

    def reset(self):
        self.sched.clear()
        self.sched.now = 0
        for p in self.plane_a:
            p[:] = bytes(len(p))
        self.plane_a[0][0:8] = self.rom[0:8]
        self.scc.cpu = None
        self.reset_peripherals()
        # full system reset only: the RESET instruction does not reset the mute state
        self.cdic.muted = False
        self.scc.cpu = self.cpu
        self.scc.update_ipl()
        self.cpu.reset()
        self.line = 0

    def request_reset(self):
        self._reset_pending = True

    # ----- callbacks from devices -------------------------------------------------
    def log_serial(self, text):
        self.serial_lines.append(text)
        self.log(f'[serial] {text}')

    def lcd_changed(self, data):
        self.lcd_text = bytes(c if 32 <= c < 127 else 32 for c in data).decode('ascii').strip()

    def frame_ready(self, frame):
        self.frame = frame
        self.frames += 1

    def audio_out(self, left, right, rate):
        if not self.audio_enabled or self.cdic.muted:
            return
        if rate != 44100:
            n = int(round(len(left) * 44100 / rate))
            xs = np.linspace(0, len(left) - 1, n)
            left = np.interp(xs, np.arange(len(left)), left)
            right = np.interp(xs, np.arange(len(right)), right)
        st = np.empty((len(left), 2), dtype=np.int16)
        st[:, 0] = np.clip(left, -32768, 32767)
        st[:, 1] = np.clip(right, -32768, 32767)
        self.audio_chunks.append(st)
        if len(self.audio_chunks) > 64:
            del self.audio_chunks[:-64]

    def take_audio(self):
        if not self.audio_chunks:
            return None
        out = np.concatenate(self.audio_chunks)
        self.audio_chunks.clear()
        return out

    # ----- execution -----------------------------------------------------------------------
    def run_until(self, t):
        s = self.sched
        cpu_run = self.cpu.ns['run']
        while s.now < t:
            nxt = s.next_time()
            end = t if nxt is None or nxt > t else nxt
            n = end - s.now
            if n > 0:
                s.now += cpu_run(n)
            s.run_due()

    def run_frame(self):
        """Emulate one video frame (312 lines PAL / 262 NTSC)."""
        if self._reset_pending:
            self._reset_pending = False
            self.reset()
        q = self.serial_input
        if q is not None and not q.empty():
            data = q.get_nowait()
            dlog.debug('serial input %r', data)
            self.scc.uart_feed(data)
        video = self.video
        start_frames = self.frames
        guard = 0
        while self.frames == start_frames and guard < 400:
            video.scanline(self.line)
            self.line += 1
            if self.line >= video.total_height:
                self.line = 0
            self.run_until(self.sched.now + LINE_CYCLES)
            guard += 1
        return self.frame

    def set_pointer(self, x, y, buttons):
        sl = self.slave
        sl.input_x = x
        sl.input_y = y
        sl.input_buttons = buttons

    def save(self):
        self.nvram.save()

    def close(self):
        """Save NVRAM and release the disc image files."""
        self.save()
        if self.disc is not None:
            self.disc.close()
            dlog.debug('closed disc image %s', self.disc.path)
