"""CDIC (IMS66490) CD interface controller of the Mono-I board.

It reads sectors from the disc at single speed (75 sectors/s) into its 16 KiB
buffer RAM, raises interrupts, and decodes CD-ROM XA ADPCM and CD-DA audio.
CPU view: RAM at 0x300000-0x303bff, registers at 0x303c00-0x303fff.

Ported from MAME's cdicdic.cpp (BSD-3-Clause, Ryan Holtz, Vincent Halver).
The buffer RAM is kept in big-endian (CPU) byte order.
"""

import numpy as np

from . import debuglog
from .bus import Device

dlog = debuglog.get('cdic')

DISC_NONE, DISC_MODE1, DISC_MODE2, DISC_CDDA, DISC_TOC = range(5)

SECTOR_HEADER = 12
SECTOR_MINUTES, SECTOR_SECONDS, SECTOR_FRACS, SECTOR_MODE = 12, 13, 14, 15
SECTOR_FILE1, SECTOR_CHAN1, SECTOR_SUBMODE1, SECTOR_CODING1 = 16, 17, 18, 19
SECTOR_FILE2, SECTOR_CHAN2, SECTOR_SUBMODE2, SECTOR_CODING2 = 20, 21, 22, 23
SECTOR_DATA = 24
SECTOR_SIZE = 2352
SECTOR_AUDIO_SIZE = 2304

SUBMODE_EOF, SUBMODE_RT, SUBMODE_FORM, SUBMODE_TRIG = 0x80, 0x40, 0x20, 0x10
SUBMODE_DATA, SUBMODE_AUDIO, SUBMODE_VIDEO, SUBMODE_EOR = 0x08, 0x04, 0x02, 0x01

CODING_EMPHASIS = 0x40
CODING_BPS_MASK, CODING_4BPS, CODING_8BPS, CODING_16BPS, CODING_BPS_MPEG = 0x30, 0x00, 0x10, 0x20, 0x30
CODING_RATE_MASK, CODING_37KHZ, CODING_18KHZ, CODING_RATE_RESV, CODING_44KHZ = 0x0c, 0x00, 0x04, 0x08, 0x0c
CODING_CHAN_MASK, CODING_MONO, CODING_STEREO = 0x03, 0x00, 0x01

NO_MAP_POSITION = 0xffff
XA_FILTER = [(0, 0), (0xF0, 0), (0x1CC, -0xD0), (0x188, -0xDC)]
XA_RATE = 37800          # 45.1584 MHz * 3/7 / 512
# attenuation after reset: left -> left and right -> right at full volume, no cross-feed
DEFAULT_ATTEN = (0xff, 0x00, 0xff, 0x00)


def _scramble_table():
    t = bytearray(SECTOR_SIZE)
    reg = 1
    for i in range(12, SECTOR_SIZE):
        b = 0
        for bit in range(8):
            b |= (reg & 1) << bit
            fb = (reg ^ (reg >> 1)) & 1
            reg = (reg >> 1) | (fb << 14)
        t[i] = b
    return bytes(t)


SCRAMBLE = np.frombuffer(_scramble_table(), dtype=np.uint8)


def _crc_table():
    tab = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else c << 1
        tab.append(c & 0xffff)
    return tab


CRC_TABLE = _crc_table()


def bcd(v):
    return (v // 10) << 4 | v % 10


def sector_count_for_coding(coding):
    base = 2
    bps = coding & CODING_BPS_MASK
    if bps == CODING_4BPS:
        base *= 2
    elif bps == CODING_BPS_MPEG:
        base = 0
    rate = coding & CODING_RATE_MASK
    if rate == CODING_18KHZ:
        base *= 2
    elif rate == CODING_RATE_RESV:
        base = 0
    ch = coding & CODING_CHAN_MASK
    if ch == CODING_MONO:
        base *= 2
    elif ch != CODING_STEREO:
        base = 0
    return base


class CDIC(Device):
    def __init__(self, machine):
        self.m = machine
        self.ram = bytearray(0x4000)
        self.sector_event = None
        self.audio_event = None
        self.muted = False
        self.reset()

    def reset(self):
        self.command = 0
        self.time = 0
        self.file = 0
        self.channel = 0xffffffff
        self.audio_channel = 0xffff
        self.audio_buffer = 0
        self.x_buffer = 0
        self.dma_control = 0
        self.z_buffer = 0
        self.interrupt_vector = 0x0f
        self.data_buffer = 0
        self.cd_byteswap = False
        self.disc_command = 0
        self.disc_mode = DISC_NONE
        self.disc_spinup_counter = 0
        self.curr_lba = 0
        self.audio_sector_counter = 0
        self.audio_format_sectors = 0
        self.decoding_audio_map = False
        self.decode_addr = NO_MAP_POSITION
        self.atten = list(DEFAULT_ATTEN)
        self.xa_last = [0, 0, 0, 0]
        self.deemph_x1 = [0.0, 0.0]
        self.deemph_y1 = [0.0, 0.0]
        self.intreq = False
        for ev in (self.sector_event, self.audio_event):
            if ev:
                ev.cancel()
        period = self.m.sched.clock / 75
        self.tick_period = period
        # sector and audio ticks keep a fixed 75 Hz timeline (rescheduling from
        # the time the tick actually ran would add the CPU's overrun every time)
        self._sector_t = self._audio_t = self.m.sched.now + period
        self.sector_event = self.m.sched.at(self._sector_t, self.sector_tick)
        self.audio_event = self.m.sched.at(self._audio_t, self.audio_tick)
        self.update_interrupt_state()

    def set_attenuation(self, gains):
        self.atten = list(gains)
        dlog.debug('attenuation LL=%02x LR=%02x RR=%02x RL=%02x', *self.atten)

    # ----- interrupts ---------------------------------------------------------
    def update_interrupt_state(self):
        active = bool((self.x_buffer | self.audio_buffer) & 0x8000)
        if active != self.intreq:
            self.intreq = active
        self.m.scc.set_in(4, active)

    def intack(self):
        return self.interrupt_vector & 0xff

    # ----- bus ----------------------------------------------------------------------
    def read16(self, a):
        off = a - 0x300000
        if off < 0x3c00:
            return self.ram[off & 0x3ffe] << 8 | self.ram[(off & 0x3ffe) + 1]
        return self.regs_r(off & 0x3ffe)

    def write16(self, a, v):
        off = a - 0x300000
        if off < 0x3c00:
            o = off & 0x3ffe
            self.ram[o] = v >> 8
            self.ram[o + 1] = v & 0xff
        else:
            self.regs_w(off & 0x3ffe, v, 0xffff)

    def read8(self, a):
        off = a - 0x300000
        if off < 0x3c00:
            return self.ram[off & 0x3fff]
        v = self.regs_r(off & 0x3ffe)
        return v & 0xff if a & 1 else v >> 8

    def write8(self, a, v):
        off = a - 0x300000
        if off < 0x3c00:
            self.ram[off & 0x3fff] = v
        else:
            if a & 1:
                self.regs_w(off & 0x3ffe, v, 0x00ff)
            else:
                self.regs_w(off & 0x3ffe, v << 8, 0xff00)

    def regs_r(self, off):
        if off == 0x3c00:
            return self.command
        if off == 0x3c02:
            return self.time >> 16
        if off == 0x3c04:
            return self.time & 0xffff
        if off == 0x3c06:
            return self.file
        if off == 0x3c08:
            return self.channel >> 16
        if off == 0x3c0a:
            return self.channel & 0xffff
        if off == 0x3c0c:
            return self.audio_channel
        if off == 0x3ff4:
            v = self.audio_buffer
            self.audio_buffer &= 0x7fff
            self.update_interrupt_state()
            return v
        if off == 0x3ff6:
            v = self.x_buffer
            self.x_buffer &= 0x7fff
            self.update_interrupt_state()
            return v
        if off == 0x3ffa:
            live = self.decoding_audio_map and self.decode_addr != NO_MAP_POSITION
            v = (self.z_buffer & ~0x0800) | (0x0800 if live else 0)
            self.z_buffer &= ~0x0001
            return v
        if off == 0x3ffe:
            return self.data_buffer
        return 0

    def regs_w(self, off, data, mask):
        def comb(old):
            return (old & ~mask) | (data & mask)

        if off == 0x3c00:
            self.command = comb(self.command)
        elif off == 0x3c02:
            self.time = (self.time & ~(mask << 16)) | ((data & mask) << 16)
        elif off == 0x3c04:
            self.time = (self.time & ~mask) | (data & mask)
        elif off == 0x3c06:
            self.file = comb(self.file)
        elif off == 0x3c08:
            self.channel = (self.channel & ~(mask << 16)) | ((data & mask) << 16)
        elif off == 0x3c0a:
            self.channel = (self.channel & ~mask) | (data & mask)
        elif off == 0x3c0c:
            self.audio_channel = comb(self.audio_channel)
        elif off == 0x3ff4:
            self.audio_buffer = comb(self.audio_buffer)
        elif off == 0x3ff6:
            self.x_buffer = comb(self.x_buffer)
        elif off == 0x3ff8:
            self.dma_transfer(data)
        elif off == 0x3ffa:
            self.z_buffer = comb(self.z_buffer)
            if not self.z_buffer & 0x2000:
                if self.z_buffer & 0x0800 and self.audio_sector_counter == 0:
                    self.z_buffer |= 0x0001
                if self.decoding_audio_map:
                    self.decoding_audio_map = False
                    self.audio_sector_counter = 0
                    self.audio_format_sectors = 0
            elif not self.decoding_audio_map or self.decode_addr == NO_MAP_POSITION:
                if self.decode_addr == NO_MAP_POSITION:
                    self.decode_addr = self.z_buffer & 0x3a00
                self.audio_format_sectors = 0
                self.audio_sector_counter = 1
                self.decoding_audio_map = True
                self.xa_last = [0, 0, 0, 0]
                self.deemph_x1 = [0.0, 0.0]
                self.deemph_y1 = [0.0, 0.0]
        elif off == 0x3ffc:
            self.interrupt_vector = comb(self.interrupt_vector)
        elif off == 0x3ffe:
            self.data_buffer = comb(self.data_buffer)
            if self.data_buffer & 0x8000:
                self.handle_command()
            if not self.data_buffer & 0x4000:
                self.disc_command = 0
                self.disc_mode = DISC_NONE
                self.disc_spinup_counter = 0
                self.curr_lba = 0
                self.x_buffer &= 0x7fff
                self.update_interrupt_state()

    def dma_transfer(self, data):
        ch = self.m.scc.dma[0]
        start = ch.memory_address_counter
        count = ch.transfer_counter
        dev = (data & 0x3fff) & ~1
        ram = self.ram
        bus = self.m.bus
        to_memory = ch.operation_control & 0x80
        a0 = start & 0xffffff
        o = a0 & 0xffff
        n = count * 2
        page = (bus.wp if to_memory else bus.rp)[a0 >> 16]
        if page is not None and not a0 & 1 and o + n <= 0x10000 and dev + n <= 0x4000:
            # within one RAM page and without wrapping the buffer: one slice
            # copy does what the word loop below does
            if to_memory:
                page[o:o + n] = ram[dev:dev + n]
            else:
                ram[dev:dev + n] = page[o:o + n]
            count_done = count
        else:
            count_done = 0
        for i in range(count_done, count):
            a = (start + i * 2) & 0xffffff
            d = (dev + i * 2) & 0x3ffe
            if to_memory:
                bus.write_word(a, ram[d] << 8 | ram[d + 1])
            else:
                v = bus.read_word(a)
                ram[d] = v >> 8
                ram[d + 1] = v & 0xff
        ch.memory_address_counter = (ch.memory_address_counter + count * 2) & 0xffffffff
        # the transfer is instantaneous: leave the channel registers as the
        # SCC68070 does after a finished transfer
        ch.device_address_counter = (ch.device_address_counter + count * 2) & 0xffffffff
        ch.transfer_counter = 0
        ch.status |= 0x80   # CSR COC: channel operation complete
        self.m.scc.update_ipl()
        dlog.debug('DMA %s %d words, memory %06x, buffer %04x', 'buffer->memory' if to_memory else 'memory->buffer',
                   count, start & 0xffffff, dev)

    # ----- commands ----------------------------------------------------------------
    def lba_from_time(self):
        def unbcd(v):
            return (v >> 4) * 10 + (v & 0xf)
        mins = unbcd((self.time >> 24) & 0xff)
        secs = unbcd((self.time >> 16) & 0xff)
        lba = (mins * 60 + secs) * 75
        frac = (self.time >> 8) & 0xff
        if not frac & 0x80:
            lba += unbcd(frac)
        if lba >= 150:
            lba -= 150
        return lba

    def init_disc_read(self, mode):
        self.disc_command = self.command
        self.disc_mode = mode
        self.curr_lba = self.lba_from_time()
        self.disc_spinup_counter = 6

    def cancel_disc_read(self):
        self.disc_command = 0
        self.disc_mode = DISC_NONE
        self.curr_lba = 0
        self.disc_spinup_counter = 0

    def handle_command(self):
        c = self.command
        if c in (0x23, 0x24):   # reset mode 1 / mode 2
            if self.disc_command == 0:
                self.init_disc_read(DISC_MODE1)
        elif c == 0x2b:         # stop CDDA
            self.cancel_disc_read()
        elif c == 0x27:         # fetch TOC
            self.init_disc_read(DISC_TOC)
        elif c == 0x28:         # play CDDA
            self.init_disc_read(DISC_CDDA)
        elif c in (0x29, 0x2c):  # read mode 1 / seek
            self.init_disc_read(DISC_MODE1)
        elif c == 0x2a:         # read mode 2
            self.init_disc_read(DISC_MODE2)
        self.data_buffer &= ~0x8000

    # ----- sector processing ---------------------------------------------------------------
    def sector_tick(self):
        self._sector_t += self.tick_period
        self.sector_event = self.m.sched.at(max(self._sector_t, self.m.sched.now + 1), self.sector_tick)
        if self.disc_command == 0:
            return
        if self.disc_spinup_counter:
            self.disc_spinup_counter -= 1
            return
        self.process_disc_sector()
        if self.disc_command == 0:
            self.cancel_disc_read()
            return
        self.curr_lba += 1

    def audio_tick(self):
        self._audio_t += self.tick_period
        self.audio_event = self.m.sched.at(max(self._audio_t, self.m.sched.now + 1), self.audio_tick)
        if self.audio_sector_counter > 0:
            self.audio_sector_counter -= 1
            if self.audio_sector_counter > 0:
                return
        if self.decoding_audio_map:
            self.process_audio_map()

    def process_audio_map(self):
        if self.decode_addr == NO_MAP_POSITION:
            self.audio_sector_counter = 0
            self.audio_format_sectors = 0
            self.decoding_audio_map = False
            return
        base = self.decode_addr & 0x3ffe
        self.decode_addr ^= 0x1a00
        was_decoding = self.audio_format_sectors != 0
        coding = self.ram[base + (SECTOR_CODING2 - SECTOR_HEADER)]
        if coding != 0xff:
            self.decoding_audio_map = True
            self.audio_format_sectors = sector_count_for_coding(coding)
            self.audio_sector_counter = self.audio_format_sectors
            start = base + SECTOR_DATA - SECTOR_HEADER
            data = bytes(self.ram[start:start + SECTOR_AUDIO_SIZE]).ljust(SECTOR_AUDIO_SIZE, b'\0')
            self.play_audio_sector(coding, data)
        else:
            self.decode_addr = NO_MAP_POSITION
            self.audio_sector_counter = 0
            self.z_buffer |= 0x0001
        if was_decoding:
            self.audio_buffer |= 0x8000
            self.update_interrupt_state()

    def _msf_bcd(self, lba):
        real = lba + 150
        return bcd(real // 4500), bcd((real // 75) % 60), bcd(real % 75)

    def is_valid_sector(self, buf):
        m, s, f = self._msf_bcd(self.curr_lba)
        if buf[SECTOR_MINUTES] != m or buf[SECTOR_SECONDS] != s or buf[SECTOR_FRACS] != f:
            return False
        if buf[SECTOR_MODE] not in (1, 2):
            return False
        return (buf[SECTOR_FILE1] == buf[SECTOR_FILE2] and buf[SECTOR_CHAN1] == buf[SECTOR_CHAN2]
                and buf[SECTOR_SUBMODE1] == buf[SECTOR_SUBMODE2] and buf[SECTOR_CODING1] == buf[SECTOR_CODING2])

    def is_mode2_sector_selected(self, buf):
        if (buf[SECTOR_FILE2] << 8) != self.file:
            return False
        sub = buf[SECTOR_SUBMODE2]
        if sub & SUBMODE_EOF:
            self.disc_command = 0
        if sub & (SUBMODE_EOF | SUBMODE_TRIG | SUBMODE_EOR):
            return True
        if not sub & (SUBMODE_DATA | SUBMODE_AUDIO | SUBMODE_VIDEO):
            return False
        return bool((self.channel >> buf[SECTOR_CHAN2]) & 1) if buf[SECTOR_CHAN2] < 32 else False

    def is_mode2_audio_selected(self, buf):
        sub = buf[SECTOR_SUBMODE2]
        if not sub & SUBMODE_FORM or not sub & SUBMODE_AUDIO:
            return False
        ch = buf[SECTOR_CHAN2]
        return ch < 16 and bool((self.audio_channel >> ch) & 1)

    def process_disc_sector(self):
        disc = self.m.disc
        m, s, f = self._msf_bcd(self.curr_lba)
        if disc is not None:
            raw = disc.read_raw(self.curr_lba)
        else:
            raw = bytes(SECTOR_SIZE)
        buf = bytearray(2560)
        buf[:len(raw)] = raw
        if self.disc_mode != DISC_CDDA and buf[:12] == bytes([0xff, 0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0, 0xff]):
            self.cd_byteswap = True
        if self.cd_byteswap:
            b = np.frombuffer(bytes(buf), dtype='<u2').byteswap()
            buf = bytearray(b.tobytes())
        if disc is not None and not disc.is_audio(self.curr_lba) and not self.is_valid_sector(buf):
            d = np.frombuffer(bytes(buf[:SECTOR_SIZE]), dtype=np.uint8) ^ SCRAMBLE
            cand = bytearray(d.tobytes()) + buf[SECTOR_SIZE:]
            if self.is_valid_sector(cand):
                buf = cand

        if buf[SECTOR_MODE] == 2 and self.disc_mode == DISC_MODE2:
            if not self.is_mode2_sector_selected(buf):
                return
            if self.is_mode2_audio_selected(buf):
                self.audio_sector_counter = sector_count_for_coding(buf[SECTOR_CODING2])
                self.decoding_audio_map = False
                self.decode_addr = NO_MAP_POSITION
                self.play_audio_sector(buf[SECTOR_CODING2], bytes(buf[SECTOR_DATA:SECTOR_DATA + SECTOR_AUDIO_SIZE]))
        elif self.disc_mode == DISC_CDDA:
            self.audio_sector_counter = 2
            self.decoding_audio_map = False
            self.decode_addr = NO_MAP_POSITION
            self.play_cdda_sector(bytes(raw))

        sub = [0] * 24
        if self.disc_mode == DISC_TOC:
            entries = self.build_toc()
            e = entries[self.curr_lba % len(entries)]
            # the TOC entries are also what lands in the sector buffer
            flat = bytes(x for ent in entries for x in ent)
            buf[:len(flat)] = flat
            sub[12] = e[0]
            sub[13] = 0
            sub[14] = e[1]
            sub[15] = 0xa0
            sub[16] = s
            sub[17] = f
            sub[18] = 0
            sub[19], sub[20], sub[21] = e[2], e[3], e[4]
        else:
            track = 1
            start = 0
            if disc is not None:
                for i, t in enumerate(disc.tracks):
                    if self.curr_lba >= t.start:
                        track = i + 1
                        start = t.start
            rel = max(self.curr_lba - start, 0)
            sub[12] = 0x01 if self.disc_mode == DISC_CDDA else 0x41
            sub[13] = bcd(track)
            sub[14] = 0x01
            sub[15] = bcd(rel // 4500)
            sub[16] = bcd((rel // 75) % 60)
            sub[17] = bcd(rel % 75)
            sub[18] = 0
            sub[19], sub[20], sub[21] = m, s, f
        crc = 0
        for i in range(10):
            crc = (((crc << 8) | sub[12 + i]) ^ CRC_TABLE[crc >> 8]) & 0xffff
        crc = (((crc << 8) | 0xff) ^ CRC_TABLE[crc >> 8]) & 0xffff
        crc = (((crc << 8) | 0xff) ^ CRC_TABLE[crc >> 8]) & 0xffff
        sub[22] = crc >> 8
        sub[23] = crc & 0xff
        self.process_sector_data(buf, sub)

    def build_toc(self):
        disc = self.m.disc
        tracks = disc.tracks if disc else []
        audio = [t for t in tracks if t.kind == 'audio']
        other = len(tracks) - len(audio)
        frames = (disc.total_sectors if disc else 0) + 150
        entries = []
        for i, t in enumerate(audio):
            a = t.start + 150
            ent = (0x01, bcd(i + 1), bcd(a // 4500), bcd((a // 75) % 60), bcd(a % 75))
            entries += [ent] * 3
        entries += [(0x41 if other else 0x01, 0xa0, 0x01, 0x10 if other else 0x00, 0x00)] * 3
        entries += [(0x01 if audio else 0x41, 0xa1, bcd(len(audio)) if audio else 0, 0, 0)] * 3
        entries += [(0x01 if audio else 0x41, 0xa2, bcd(frames // 4500), bcd((frames // 75) % 60),
                     bcd(frames % 75))] * 3
        return entries

    def process_sector_data(self, buf, sub):
        self.data_buffer ^= 0x0001
        self.data_buffer &= ~0x0004
        o = (self.data_buffer & 0x0005) * 0xa00
        ram = self.ram
        ram[o:o + 8] = buf[SECTOR_HEADER:SECTOR_FILE2]
        o += 8
        if self.command == 0x2a and self.is_mode2_audio_selected(buf):
            self.data_buffer |= 0x0004
            o += 0x2800
        n = SECTOR_SIZE - SECTOR_FILE2
        end = min(o + n, 0x4000)
        ram[o:end] = buf[SECTOR_FILE2:SECTOR_FILE2 + (end - o)]
        o = end
        for i in range(12, 24):
            if o + 1 < 0x4000:
                ram[o] = 0
                ram[o + 1] = sub[i]
            o += 2
        self.x_buffer |= 0x8000
        self.data_buffer |= 0x4000
        self.update_interrupt_state()
        if self.command in (0x23, 0x24):
            self.cancel_disc_read()

    # ----- audio --------------------------------------------------------------------------
    def play_cdda_sector(self, raw):
        s = np.frombuffer(raw[:SECTOR_SIZE], dtype='<i2')
        if self.cd_byteswap:
            s = s.byteswap()
        left = s[0::2].astype(np.float32)
        right = s[1::2].astype(np.float32)
        out_l, out_r = self.attenuate(left, right)
        self.m.audio_out(out_l, out_r, 44100)

    def attenuate(self, left, right):
        """Apply the SLAVE volume matrix (same for CD-DA and ADPCM)."""
        a = self.atten
        ll, lr, rr, rl = a[0] / 255.0, a[1] / 255.0, a[2] / 255.0, a[3] / 255.0
        out_l = np.clip(left * ll + right * rl, -32768, 32767)
        out_r = np.clip(left * lr + right * rr, -32768, 32767)
        return out_l, out_r

    def play_audio_sector(self, coding, data):
        if ((coding & CODING_CHAN_MASK) > CODING_STEREO or (coding & CODING_BPS_MASK) == CODING_BPS_MPEG
                or (coding & CODING_RATE_MASK) == CODING_RATE_RESV):
            return
        bps = coding & CODING_BPS_MASK
        if bps == CODING_16BPS or (coding & CODING_RATE_MASK) == CODING_44KHZ:
            return  # not handled by the CDIC (MAME treats these as fatal)
        stereo = bool(coding & CODING_STEREO)
        half_rate = (coding & CODING_RATE_MASK) == CODING_18KHZ
        eight = bps == CODING_8BPS
        groups = np.frombuffer(data[:18 * 128].ljust(18 * 128, b'\0'), dtype=np.uint8).reshape(18, 128)
        # sample bytes of every group as (group, sample 0..27, column 0..3)
        body = groups[:, 16:].reshape(18, 28, 4).astype(np.int32)
        if eight:
            params = groups[:, 4:8].astype(np.int32)                       # (18, 4)
            raw = body.transpose(0, 2, 1) << 8                              # (18, 4, 28)
        else:
            params = groups[:, [4, 5, 6, 7, 12, 13, 14, 15]].astype(np.int32)  # (18, 8)
            cols = body.transpose(0, 2, 1)[:, [0, 0, 1, 1, 2, 2, 3, 3]]       # (18, 8, 28)
            shifts = np.array([0, 4, 0, 4, 0, 4, 0, 4], dtype=np.int32)[None, :, None]
            raw = ((cols >> shifts) & 15) << 12
        ranges = np.minimum(params & 0xf, 12)
        units = (((raw ^ 0x8000) - 0x8000) >> ranges[:, :, None]).tolist()
        filters = ((params >> 4) & 3).tolist()
        chans = [[], []]
        last = self.xa_last
        for g in range(18):
            for i, samples in enumerate(units[g]):
                ch = (i & 1) if stereo else 0
                f0, f1 = XA_FILTER[filters[g][i]]
                s0, s1 = last[ch * 2], last[ch * 2 + 1]
                append = chans[ch].append
                for smp in samples:
                    v = smp + ((f0 * s0 + f1 * s1 + 128) >> 8)
                    if v > 32767:
                        v = 32767
                    elif v < -32768:
                        v = -32768
                    s1 = s0
                    s0 = v
                    append(v)
                last[ch * 2], last[ch * 2 + 1] = s0, s1
        left = np.array(chans[0], dtype=np.float32)
        right = np.array(chans[1], dtype=np.float32) if stereo else left
        if coding & CODING_EMPHASIS:
            g, z, p = (0.614375, -0.062739, 0.347080) if half_rate else (0.475001, 0.135021, 0.589135)
            outs = []
            for ci, arr in enumerate((left, right) if stereo else (left,)):
                x1, y1 = self.deemph_x1[ci], self.deemph_y1[ci]
                res = []
                append = res.append
                for x in arr.tolist():
                    y = g * (x - z * x1) + p * y1
                    x1, y1 = x, y
                    append(y)
                self.deemph_x1[ci], self.deemph_y1[ci] = x1, y1
                outs.append(np.clip(np.array(res, dtype=np.float32), -32768, 32767))
            left = outs[0]
            right = outs[1] if stereo else left
        out_l, out_r = self.attenuate(left, right)
        # 18.9 kHz goes to the machine's streaming resampler as it is
        self.m.audio_out(out_l, out_r, XA_RATE // 2 if half_rate else XA_RATE)
