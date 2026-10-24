"""MCD212 video decoder and system controller (VDSC).

The display control programs (ICA once per frame, DCA once per line) run at
line time exactly like the chip does, and every visible line records the
decoder state it would use.  Pixels are produced once per frame from those
records with numpy, which batches runs of lines that share a state.

Ported from MAME's mcd212.cpp (BSD-3-Clause, Ryan Holtz, Vincent Halver).
"""

import os
import struct

import numpy as np

from . import debuglog
from .bus import Device

dlog = debuglog.get('mcd212')

CSR1R_PA = 0x20
CSR1R_DA = 0x80
CSR1W_ST_BIT = 1
CSR2R_IT2, CSR2R_IT1 = 0x02, 0x04
DCR_DCA_BIT, DCR_ICA_BIT, DCR_CM_BIT, DCR_SM_BIT, DCR_FD_BIT, DCR_CF_BIT, DCR_DE_BIT = 8, 9, 11, 12, 13, 14, 15
DDR_FT, DDR_FT_RLE, DDR_FT_MOSAIC = 0x0300, 0x0200, 0x0300
DDR_MT, DDR_MT_SHIFT = 0x0c00, 10
ICM_NM_BIT, ICM_CS_BIT, ICM_EV_BIT = 19, 22, 18
ICM_RGB555, ICM_CLUT8, ICM_CLUT7, ICM_CLUT77, ICM_DYUV, ICM_CLUT4 = 1, 1, 3, 4, 5, 11
TCR_DISABLE_MX = 0x800000
CURCNT_EN, CURCNT_CUW, CURCNT_BLKC = 0x800000, 0x008000, 0x400000

_UNPACK16 = struct.Struct('>16I').unpack
_UNPACK1 = struct.Struct('>I').unpack_from
_DE = 1 << DCR_DE_BIT
_ICA_DCA = 1 << DCR_ICA_BIT | 1 << DCR_DCA_BIT
COLOR_OFF = 0x101010   # s_4bpp_color[0]
DISPLAY_COLORS = [0x000000, 0x00007b, 0x007b00, 0x007b7b, 0x7b0000, 0x7b007b, 0x7b7b00, 0x7b7b7b,
                  0x000000, 0x0000f9, 0x00f900, 0x00f9f9, 0xf90000, 0xf900f9, 0xf9f900, 0xf9f9f9]

DYUV_DELTAS = np.array([0, 1, 4, 9, 16, 27, 44, 79, 128, 177, 212, 229, 240, 247, 252, 255], dtype=np.int32)
_w = np.arange(0x300)
LIMIT = np.where(_w < 0x100, 0, np.where(_w < 0x200, _w - 0x100, 0xff)).astype(np.int32)
LIMIT_FE = LIMIT & 0xfe
_sw = np.arange(256)
U_TO_B = (444 * (_sw - 128)) >> 8
U_TO_G = -86 * (_sw - 128)
V_TO_G = -179 * (_sw - 128)
V_TO_R = (351 * (_sw - 128)) >> 8
# output stage: studio levels (16..235) expanded to full range
EXPAND = LIMIT[(((np.arange(256) - 16) * 298 + 128) >> 8) + 0x100].astype(np.uint32)
_MIX_TABLES = {}


# lookup tables in the layout the native helpers (native/video.c) take
_DELTAS32 = np.ascontiguousarray(DYUV_DELTAS, dtype=np.int32)
_LIMIT_FE32 = np.ascontiguousarray(LIMIT_FE, dtype=np.int32)
_LIMIT32 = np.ascontiguousarray(LIMIT, dtype=np.int32)
_EXPAND32 = np.ascontiguousarray(EXPAND, dtype=np.uint32)
_U_TO_B32, _U_TO_G32, _V_TO_G32, _V_TO_R32 = (np.ascontiguousarray(t, dtype=np.int32)
                                               for t in (U_TO_B, U_TO_G, V_TO_G, V_TO_R))
_native = []


def native_lib():
    """The native library for render_dyuv()/mix(), or None for numpy."""
    if not _native:
        lib = None
        if os.environ.get('OPENCDI_CPU', 'native').lower() != 'python':
            from . import m68k_native
            lib = m68k_native.load()
        _native.append(lib)
    return _native[0]


def mix_table(wa, wb):
    """out[pa << 8 | pb] for one colour channel at fixed plane weights."""
    t = _MIX_TABLES.get((wa, wb))
    if t is None:
        p = np.arange(256)
        ca = ((p - 16) * wa) >> 6
        cb = ((p - 16) * wb) >> 6
        mixed = LIMIT[ca[:, None] + cb[None, :] + 0x110]
        t = EXPAND[mixed].astype(np.uint8).reshape(65536)
        _MIX_TABLES[(wa, wb)] = t
    return t


def byte_to_clut_lut(icm, path, clut_select, nibble=None, mask=0xf):
    """256-entry table mapping a pixel byte to a CLUT index."""
    b = np.arange(256)
    if nibble == 'hi':
        b = (b >> 4) & mask
    elif nibble == 'lo':
        b = b & mask
    if icm == 1:
        return b
    if icm == 3:
        return (0x80 if path else 0) | (b & 0x7f)
    if icm == 4:
        if path == 0:
            return (0x80 if clut_select else 0) | (b & 0x7f)
        return np.zeros(256, dtype=np.int64)
    if icm == 11:
        return (0x80 if path else 0) | b
    return np.zeros(256, dtype=np.int64)


class PlaneLine:
    # vend/vend2: where the line's pixel data ends (the next vsr), so the frame
    # cache knows exactly which plane bytes the picture was made from
    __slots__ = ('kind', 'vsr', 'vsr2', 'key', 'runs', 'vend', 'vend2')

    def __init__(self, kind, key, vsr=0, vsr2=0, runs=None):
        self.kind, self.key, self.vsr, self.vsr2, self.runs = kind, key, vsr, vsr2, runs
        self.vend = self.vend2 = None


class MCD212(Device):
    def __init__(self, machine, plane_a_pages, plane_b_pages):
        self.m = machine
        self.pages = (plane_a_pages, plane_b_pages)
        self.frame = None
        self.frame_count = 0
        self.prev_field = None
        self._cursor_logged = None
        self.skip_render = False   # set by the machine for frame skipping
        self.render_stats = [0, 0, 0]   # rendered, reused from cache, skipped
        self.reset()

    def reset(self):
        self.csrr = [0, 0]
        self.csrw = [0, 0]
        self.dcr = [0, 0]
        self.vsr = [0, 0]
        self.ddr = [0, 0]
        self.dcp = [0, 0]
        self.dca = [0, 0]
        self.clut = [0] * 256
        self.clut_ver = 0
        self._clut_np = None
        self._clut_np_ver = -1
        self.icm = 0
        self.tcr = 0
        self.plane_order = 0
        self.clut_bank = [0, 0]
        self.trans_color = [0, 0]
        self.mask_color = [0, 0]
        self.dyuv_start = [0, 0]
        self.cursor_pos = 0
        self.cursor_ctrl = 0
        self.cursor_pattern = [0] * 16
        self.matte_control = [0] * 8
        self.backdrop = 0
        self.mosaic_hold = [0, 0]
        self.weight = [np.zeros(768, dtype=np.int32), np.zeros(768, dtype=np.int32)]
        self.matte_flag = [np.zeros(768, dtype=bool), np.zeros(768, dtype=bool)]
        self.matte_ver = 0
        self.ica_height = 32
        self.total_height = 312
        self.blink_time = 0
        self.blink_active = False
        self.int_line = False
        self.lines = []
        self.snap = None
        self.rle_cache = [{}, {}]
        # last rendered picture (before the cursor) and what it was made from;
        # clut_ver/matte_ver restart at 0 here, so the cache must not survive
        self._cache_sig = None
        self._cache_used = None     # (address ranges, bytes) from used_plane_bytes()
        self._cache_out = None
        # plane_line()/process_dca() caches: decoder state per plane, valid while
        # state_ver is unchanged, and decoded DCA blocks by address
        self.state_ver = 0
        self._pl_state = [None, None]
        self._dca_cache = [{}, {}]
        self.update_frame_geometry()

    # ----- helpers -----------------------------------------------------------
    def update_frame_geometry(self):
        ntsc = bool(self.dcr[0] >> DCR_FD_BIT & 1)
        self.total_height = 262 if ntsc else 312
        self.ica_height = 22 if ntsc else 32

    def screen_width(self):
        if not (self.dcr[0] >> DCR_CF_BIT & 1) or (self.csrw[0] >> CSR1W_ST_BIT & 1):
            return 720
        return 768

    def border_width(self):
        return 24 if self.screen_width() == 720 else 0

    def ica_enabled(self, ch):
        return bool(self.dcr[0] >> DCR_DE_BIT & 1) and bool(self.dcr[ch] >> DCR_ICA_BIT & 1)

    def dca_enabled(self, ch):
        return self.ica_enabled(ch) and bool(self.dcr[ch] >> DCR_DCA_BIT & 1)

    def get_vsr(self, p):
        return (self.dcr[p] & 0x3f) << 16 | self.vsr[p]

    def set_vsr(self, p, v):
        self.vsr[p] = v & 0xffff
        self.dcr[p] = (self.dcr[p] & 0xffc0) | ((v >> 16) & 0x3f)

    def get_dcp(self, p):
        return (self.ddr[p] & 0x3f) << 16 | self.dcp[p]

    def set_dcp(self, p, v):
        self.dcp[p] = v & 0xffff
        self.ddr[p] = (self.ddr[p] & 0xffc0) | ((v >> 16) & 0x3f)

    def set_display_parameters(self, p, v):
        self.state_ver += 1
        self.ddr[p] = (self.ddr[p] & 0xf0ff) | ((v & 0x0f) << 8)
        self.dcr[p] = (self.dcr[p] & 0xf7ff) | ((v & 0x10) << 7)

    def plane_word(self, p, waddr):
        a = (waddr * 2) & 0x7fffe
        pg = self.pages[p][a >> 16]
        o = a & 0xffff
        return pg[o] << 8 | pg[o + 1]

    def get_icm(self, p):
        return (self.icm >> (8 if p else 0)) & 0x0f

    def raise_interrupt(self, p):
        self.csrr[1] |= 1 << (2 - p)
        if self.csrr[1] & (CSR2R_IT1 | CSR2R_IT2):
            self.set_int(True)

    def set_int(self, state):
        self.int_line = state
        self.m.scc.int1_w(state)

    # ----- registers written by ICA/DCA -----------------------------------------------
    def set_register(self, p, reg, value):
        self.state_ver += 1
        if 0x80 <= reg < 0xc0:
            i = (self.clut_bank[p] * 0x40 + (reg - 0x80)) & 0xff
            value &= 0x00fcfcfc
            if self.clut[i] != value:
                self.clut[i] = value
                self.clut_ver += 1
            return
        if reg == 0xc0:
            if p == 0:
                old = self.icm
                self.icm = value
                if (old ^ value) >> ICM_NM_BIT & 1:
                    self.update_matte_arrays()
        elif reg == 0xc1:
            if p == 0:
                self.tcr = value
        elif reg == 0xc2:
            if p == 0:
                self.plane_order = value & 7
        elif reg == 0xc3:
            self.clut_bank[p] = (2 | (value & 1)) if p else (value & 3)
        elif reg == 0xc4:
            if p == 0:
                self.trans_color[0] = value & 0x00fcfcfc
        elif reg == 0xc6:
            if p == 1:
                self.trans_color[1] = value & 0x00fcfcfc
        elif reg == 0xc7:
            if p == 0:
                self.mask_color[0] = value & 0x00fcfcfc
        elif reg == 0xc9:
            if p == 1:
                self.mask_color[1] = value & 0x00fcfcfc
        elif reg == 0xca:
            if p == 0:
                self.dyuv_start[0] = value
        elif reg == 0xcb:
            if p == 1:
                self.dyuv_start[1] = value
        elif reg == 0xcd:
            if p == 0:
                self.cursor_pos = value
        elif reg == 0xce:
            if p == 0:
                self.cursor_ctrl = value
        elif reg == 0xcf:
            if p == 0:
                self.cursor_pattern[(value >> 16) & 0xf] = value & 0xffff
        elif 0xd0 <= reg <= 0xd7:
            if self.matte_control[reg & 7] != value:
                self.matte_control[reg & 7] = value
                self.update_matte_arrays()
        elif reg == 0xd8:
            if p == 0:
                self.backdrop = value
        elif reg == 0xd9:
            if p == 0:
                self.mosaic_hold[0] = value
        elif reg == 0xda:
            if p == 1:
                self.mosaic_hold[1] = value
        elif reg == 0xdb:
            if p == 0 and self.weight[0][0] != value & 0x3f:
                self.weight[0] = self.weight[0].copy()
                self.weight[0][0] = value & 0x3f
                self.update_matte_arrays()
        elif reg == 0xdc:
            if p == 1 and self.weight[1][0] != value & 0x3f:
                self.weight[1] = self.weight[1].copy()
                self.weight[1][0] = value & 0x3f
                self.update_matte_arrays()

    def update_matte_arrays(self):
        width = self.screen_width()
        num = 2 if self.icm >> ICM_NM_BIT & 1 else 1
        mf = [False, False]
        wf = [int(self.weight[0][0]), int(self.weight[1][0])]
        events = []
        for unit in range(num):
            i = 0 if unit == 0 else 4
            max_id = (4 if num == 2 else 8) + (4 if unit else 0)
            last_x = -1
            while i < max_id:
                ctrl = self.matte_control[i]
                x = ctrl & 0x3ff
                if x <= last_x or x >= width:
                    break
                events.append((x, unit, i))
                last_x = x
                if (ctrl >> 20) & 0xf == 0:
                    break
                i += 1
        events.sort()
        new_w = [np.empty(768, dtype=np.int32), np.empty(768, dtype=np.int32)]
        new_f = [np.empty(768, dtype=bool), np.empty(768, dtype=bool)]
        cur = 0
        for x, unit, i in events:
            if x > cur:
                for k in (0, 1):
                    new_w[k][cur:x] = wf[k]
                    new_f[k][cur:x] = mf[k]
                cur = x
            ctrl = self.matte_control[i]
            op = (ctrl >> 20) & 0xf
            flag = unit if num == 2 else (ctrl >> 16) & 1
            w = (ctrl >> 10) & 0x3f
            if op in (4, 6):
                wf[(op >> 1) & 1] = w
            elif op in (8, 9):
                mf[flag] = bool(op & 1)
            elif op >= 12:
                wf[(op >> 1) & 1] = w
                mf[flag] = bool(op & 1)
        for k in (0, 1):
            new_w[k][cur:] = wf[k]
            new_f[k][cur:] = mf[k]
        if all(np.array_equal(new_w[k], self.weight[k]) and np.array_equal(new_f[k], self.matte_flag[k])
               for k in (0, 1)):
            return
        self.weight = new_w
        self.matte_flag = new_f
        self.matte_ver += 1
        self.state_ver += 1

    # ----- ICA / DCA ---------------------------------------------------------------------
    def process_ica(self):
        max_n = self.ica_height * 120
        addr = [0x200 if not self.csrr[0] & CSR1R_PA else 0x202] * 2
        active = [self.ica_enabled(0), self.ica_enabled(1)]
        i = 0
        while i < max_n and (active[0] or active[1]):
            if active[1]:
                active[1] = self.ica_command(1, addr)
            if active[0]:
                active[0] = self.ica_command(0, addr)
            i += 1

    def ica_command(self, p, addr):
        a = addr[p]
        b = (a * 2) & 0x7fffe
        if b <= 0x7fffc and self.snap is not None:
            # frame_start() has just taken the snapshot, so it matches plane memory
            cmd = _UNPACK1(self.snap[p], b)[0]
        else:
            cmd = self.plane_word(p, a) << 16 | self.plane_word(p, a + 1)
        addr[p] = a + 2
        op = cmd >> 24
        hi = op >> 4
        if hi == 0:
            return False
        if hi == 1:
            return True
        if hi == 2:
            self.set_dcp(p, cmd & 0x003ffffc)
            return True
        if hi == 3:
            self.set_dcp(p, cmd & 0x003ffffc)
            return False
        if hi == 4:
            addr[p] = (cmd & 0x0007ffff) // 2
            return True
        if hi == 5:
            self.set_vsr(p, cmd & 0x003fffff)
            return False
        if hi == 6:
            self.raise_interrupt(p)
            return True
        if hi == 7 and op & 8:
            self.set_display_parameters(p, cmd & 0x1f)
            return True
        self.set_register(p, op, cmd & 0x00ffffff)
        return True

    def process_dca(self, p):
        addr = self.dca[p] & 0x0007fffc
        pages = self.pages[p]
        o = addr & 0xffff
        if o <= 0x10000 - 64:
            blk = pages[addr >> 16][o:o + 64]
        else:
            blk = bytes(pages[addr >> 16][o:]) + bytes(pages[((addr >> 16) + 1) & 7][:64 - (0x10000 - o)])
        # most lines run the same block as in the previous frame: decode a
        # block once and replay its actions while its bytes are unchanged
        cache = self._dca_cache[p]
        hit = cache.get(addr)
        if hit is None or hit[0] != blk:
            if len(cache) > 4096:
                cache.clear()
            hit = cache[addr] = (bytes(blk),) + self._decode_dca(blk, addr)
        for code, a, b in hit[1]:
            if code == 0:
                self.set_vsr(p, a)
            elif code == 3:
                self.set_register(p, a, b)
            elif code == 1:
                self.raise_interrupt(p)
            elif code == 2:
                self.set_display_parameters(p, a)
            else:
                self.set_dcp(p, a)
        self.dca[p] = hit[2]

    @staticmethod
    def _decode_dca(blk, addr):
        """One 64-byte DCA block as (actions, next DCA address); actions are
        (0 vsr | 1 interrupt | 2 display parameters | 3 register | 4 dcp, a, b)."""
        acts = []
        for cmd in _UNPACK16(blk):
            hi = cmd >> 28
            if hi == 1 or hi == 2:
                continue
            if hi == 0:
                break
            if hi == 3:
                acts.append((4, cmd & 0x003ffffc, 0))
                return tuple(acts), cmd & 0x0007fffc
            if hi == 4:
                acts.append((0, cmd & 0x003fffff, 0))
                continue
            if hi == 5:
                acts.append((0, cmd & 0x003fffff, 0))
                break
            if hi == 6:
                acts.append((1, 0, 0))
                continue
            op = cmd >> 24
            if op >= 0x78 and hi == 7:
                acts.append((2, cmd & 0x1f, 0))
                continue
            acts.append((3, op, cmd & 0x00ffffff))
        # the DCA always consumes a whole 64-byte block per line
        return tuple(acts), addr + 64

    # ----- line timing (called by the machine) ------------------------------------------------
    def frame_start(self):
        """ICA tick at the top of the frame."""
        self.update_frame_geometry()
        self.csrr[0] &= ~CSR1R_DA
        self.frame_parity = 1 if self.csrr[0] & CSR1R_PA else 0
        a = b''.join(self.pages[0])
        b = b''.join(self.pages[1])
        self.snap = (a, b)
        self.lines = []
        self.process_ica()
        if self.dca_enabled(0):
            self.dca[0] = self.get_dcp(0)
        if self.dca_enabled(1):
            self.dca[1] = self.get_dcp(1)
        self.blink_time += 5 + (self.dcr[0] >> DCR_FD_BIT & 1)
        con = (self.cursor_ctrl >> 19) & 7
        cof = (self.cursor_ctrl >> 16) & 7
        if not self.blink_active and self.blink_time >= con * 60:
            self.blink_active = True
            self.blink_time = 0
        if self.blink_active and self.blink_time >= cof * 60:
            self.blink_active = False
            self.blink_time = 0

    def scanline(self, line):
        """Called once per line (0..total-1), at the start of the line."""
        if line == 0:
            self.frame_start()
        if line < self.ica_height:
            return
        self.csrr[0] |= CSR1R_DA
        vis = line - self.ica_height
        draw = True
        if not (self.dcr[0] >> DCR_FD_BIT & 1) and (self.csrw[0] >> CSR1W_ST_BIT & 1):
            if vis < 20 or line >= self.total_height - 20:
                draw = False
        if draw:
            pa = self.plane_line(0)
            pb = self.plane_line(1)
            self.lines.append((pa, pb, self.mix_key()))
        else:
            self.lines.append(None)
        if line == self.total_height - 1:
            self.csrr[0] ^= CSR1R_PA
        dcr = self.dcr
        if dcr[0] & _DE:      # dca_enabled(), inlined: this runs twice per line
            if dcr[1] & _ICA_DCA == _ICA_DCA:
                self.process_dca(1)
            if dcr[0] & _ICA_DCA == _ICA_DCA:
                self.process_dca(0)
        if line == self.total_height - 1:
            self.render_frame()

    def clut_array(self):
        if self._clut_np_ver != self.clut_ver:
            self._clut_np = np.array(self.clut, dtype=np.uint32)
            self._clut_np_ver = self.clut_ver
        return self._clut_np

    def _plane_state(self, p):
        """Decoder state for plane p that only changes with a register write."""
        icm = self.get_icm(p)
        tp_ctrl = (self.tcr >> (8 if p else 0)) & 0x0f
        width = self.screen_width()
        ft = self.ddr[p] & DDR_FT
        clut_select = bool(self.icm >> ICM_CS_BIT & 1)
        mosaic = ft == DDR_FT_MOSAIC
        factor = 1 << (((self.ddr[p] & DDR_MT) >> DDR_MT_SHIFT) + 1)
        key = (icm, p, ft, factor if mosaic else 0, self.clut_ver, clut_select, tp_ctrl,
               self.trans_color[p], self.mask_color[p], self.matte_ver, width)
        if icm == ICM_DYUV:
            key = key + (self.dyuv_start[p],)
        st = (self.state_ver, icm, ('off', tp_ctrl, width), width, ft, mosaic, factor, key,
              (self.clut_array(), self.matte_flag, self.weight))
        self._pl_state[p] = st
        return st

    def plane_line(self, p):
        st = self._pl_state[p]
        if st is None or st[0] != self.state_ver:
            st = self._plane_state(p)
        _ver, icm, off_key, width, ft, mosaic, factor, key, state = st
        vsr = (self.dcr[p] & 0x3f) << 16 | self.vsr[p]
        if not icm or not vsr:
            return PlaneLine('off', off_key)
        if icm == ICM_DYUV:
            rec = PlaneLine('dyuv', key, vsr)
            rec.runs = (None,) + state
            rec.vend = vsr + 2 * (width // 4)
            self.set_vsr(p, rec.vend)
            return rec
        rgb = icm == ICM_RGB555 and p == 1
        if ft == DDR_FT_RLE and not rgb:
            data = self.snap[p]
            hit = self.rle_cache[p].get((vsr, width))
            if hit is not None:
                raw, runs, v = hit
                if data[vsr & 0x7ffff:(vsr & 0x7ffff) + len(raw)] == raw:
                    rec = PlaneLine('rle', key, vsr, runs=(runs,) + state)
                    rec.vend = v
                    self.set_vsr(p, v)
                    return rec
            x = 0
            codes = []
            lens = []
            v = vsr
            while x < width:
                byte = data[v & 0x7ffff]
                v += 1
                if byte & 0x80:
                    length = data[v & 0x7ffff]
                    v += 1
                    lm = length * 2 if length else width
                else:
                    lm = 2
                end = x + lm
                if end > width:
                    end = width
                codes.append(byte)
                lens.append(end - x)
                x = end
            runs = (np.array(codes, dtype=np.intp), np.array(lens, dtype=np.intp))
            start = vsr & 0x7ffff
            if start + (v - vsr) <= 0x80000:
                cache = self.rle_cache[p]
                if len(cache) > 4096:
                    cache.clear()
                cache[(vsr, width)] = (data[start:start + (v - vsr)], runs, v)
            rec = PlaneLine('rle', key, vsr, runs=(runs,) + state)
            rec.vend = v
            self.set_vsr(p, v)
            return rec
        lm = factor * 2 if mosaic else 2
        nb = -(-width // lm)
        if rgb:
            vsr2 = self.get_vsr(0)
            rec = PlaneLine('rgb', key, vsr, vsr2, runs=(None,) + state)
            rec.vend, rec.vend2 = vsr + nb, vsr2 + nb
            self.set_vsr(p, vsr + nb)
            self.set_vsr(0, vsr2 + nb)
            return rec
        rec = PlaneLine('bmp', key, vsr, runs=(None,) + state)
        rec.vend = vsr + nb
        self.set_vsr(p, vsr + nb)
        return rec

    def mix_key(self):
        return (self.get_icm(0), self.get_icm(1), bool(self.tcr & TCR_DISABLE_MX), self.plane_order & 1,
                self.matte_ver, self.mosaic_hold[0], self.mosaic_hold[1], self.backdrop & 0xf,
                bool(self.icm >> ICM_EV_BIT & 1), self.screen_width())

    # ----- bus ------------------------------------------------------------------------------
    def read16(self, a):
        o = a & 0x1e
        if o == 0x00:
            return self.csr2_read()
        if o == 0x02:
            return self.dcr[1]
        if o == 0x04:
            return self.vsr[1]
        if o == 0x08:
            return self.ddr[1]
        if o == 0x0a:
            return self.dca[1] & 0xffff
        if o == 0x10:
            return self.csrr[0]
        if o == 0x12:
            return self.dcr[0]
        if o == 0x14:
            return self.vsr[0]
        if o == 0x18:
            return self.ddr[0]
        if o == 0x1a:
            return self.dca[0] & 0xffff
        return 0

    def read8(self, a):
        o = a & 0x1f
        if o == 0x01:
            return self.csr2_read()
        if o == 0x11:
            return self.csrr[0]
        if o in (0x00, 0x10):
            return 0
        v = self.read16(a & ~1)
        return v & 0xff if a & 1 else v >> 8

    def csr2_read(self):
        d = self.csrr[1]
        self.csrr[1] &= ~(CSR2R_IT1 | CSR2R_IT2)
        if d & (CSR2R_IT1 | CSR2R_IT2):
            self.set_int(False)
        return d

    def write16(self, a, v):
        self._write(a & 0x1e, v, 0xffff)

    def write8(self, a, v):
        if a & 1:
            self._write(a & 0x1e, v, 0x00ff)
        else:
            self._write(a & 0x1e, v << 8, 0xff00)

    def _write(self, o, v, mask):
        self.state_ver += 1
        def comb(old):
            return (old & ~mask) | (v & mask)
        if o == 0x00:
            self.csrw[1] = comb(self.csrw[1])
        elif o == 0x02:
            mask &= 0xff00
            self.dcr[1] = comb(self.dcr[1])
        elif o == 0x04:
            self.vsr[1] = comb(self.vsr[1])
        elif o == 0x08:
            self.ddr[1] = comb(self.ddr[1])
        elif o == 0x0a:
            self.dca[1] = comb(self.dca[1])
        elif o == 0x10:
            self.csrw[0] = comb(self.csrw[0])
        elif o == 0x12:
            mask &= 0xff00
            self.dcr[0] = comb(self.dcr[0])
        elif o == 0x14:
            self.vsr[0] = comb(self.vsr[0])
        elif o == 0x18:
            self.ddr[0] = comb(self.ddr[0])
        elif o == 0x1a:
            self.dca[0] = comb(self.dca[0])

    # ----- rendering -------------------------------------------------------------------------------
    def frame_signature(self):
        """Everything the picture depends on besides plane memory and the cursor.

        Line keys hold the decoder state, including clut_ver/matte_ver, so equal
        signatures plus equal plane memory give an identical picture."""
        return [None if rec is None else
                (rec[0].kind, rec[0].key, rec[0].vsr, rec[0].vsr2,
                 rec[1].kind, rec[1].key, rec[1].vsr, rec[1].vsr2, rec[2])
                for rec in self.lines]

    def used_plane_bytes(self):
        """The plane memory bytes this frame's picture reads, as one bytes object
        (plus the address ranges, so equal results mean equal input)."""
        # consecutive lines usually continue where the previous one ended:
        # join those straight away, the merge below handles the rest
        runs = ([], [])

        def add(p, start, end):
            r = runs[p]
            if end <= start:
                return
            if r and r[-1][1] == start:
                r[-1][1] = end
            else:
                r.append([start, end])

        for rec in self.lines:
            if rec is None:
                continue
            for p in (0, 1):
                line = rec[p]
                kind = line.kind
                if kind == 'off':
                    continue
                add(p, line.vsr, line.vend)
                if kind == 'rgb':
                    add(0, line.vsr2, line.vend2)   # RGB555 takes half of each pixel from plane A
        pieces = []
        for p, start, end in [(p, a, b) for p in (0, 1) for a, b in runs[p]]:
            s0 = start & 0x7ffff
            n = max(end - start, 0)
            while n > 0:                                       # the decoders wrap at 512 KiB
                k = min(n, 0x80000 - s0)
                pieces.append((p, s0, s0 + k))
                s0, n = 0, n - k
        pieces.sort()
        merged = []
        for p, a, b in pieces:
            if merged and merged[-1][0] == p and a <= merged[-1][2]:
                if b > merged[-1][2]:
                    merged[-1][2] = b
            else:
                merged.append([p, a, b])
        snap = self.snap
        return tuple(map(tuple, merged)), b''.join(snap[p][a:b] for p, a, b in merged)

    def render_frame(self):
        if self.skip_render:
            # frame skipping: the frame was emulated in full, only its picture
            # is not drawn; the previous picture stays on screen
            self.render_stats[2] += 1
            self.frame_count += 1
            self.m.frame_ready(self.frame)
            return
        lines = self.lines
        n = len(lines)
        sig = self.frame_signature()
        used = self.used_plane_bytes()
        if self._cache_out is not None and sig == self._cache_sig and used == self._cache_used:
            # nothing the picture depends on changed: reuse it
            self.render_stats[1] += 1
            out = self._cache_out.copy()
            if n:
                self.draw_cursor(out)
            self._finish_frame(out)
            return
        self.render_stats[0] += 1
        out = np.zeros((n, 768), dtype=np.uint32)
        if n:
            planes = [np.frombuffer(self.snap[0], dtype=np.uint8), np.frombuffer(self.snap[1], dtype=np.uint8)]
            i = 0
            while i < n:
                rec = lines[i]
                if rec is None:
                    i += 1
                    continue
                j = i + 1
                ka, kb, km = rec[0].key, rec[1].key, rec[2]
                while j < n and lines[j] is not None and lines[j][0].key == ka and lines[j][1].key == kb \
                        and lines[j][2] == km and lines[j][0].kind == rec[0].kind and lines[j][1].kind == rec[1].kind:
                    j += 1
                group = lines[i:j]
                ca, ta = self.render_plane(planes, [g[0] for g in group], 0)
                cb, tb = self.render_plane(planes, [g[1] for g in group], 1)
                out[i:j] = self.mix(ca, ta, cb, tb, rec)
                i = j
        self._cache_sig = sig
        self._cache_used = used
        self._cache_out = out.copy()
        if n:
            self.draw_cursor(out)
        self._finish_frame(out)

    def _finish_frame(self, out):
        n = out.shape[0]
        self.frame_count += 1
        interlace = bool(self.dcr[0] >> DCR_SM_BIT & 1)
        full = np.empty((n * 2, 768), dtype=np.uint32)
        parity = 1 - self.frame_parity
        if interlace and self.prev_field is not None and self.prev_field.shape == out.shape:
            full[parity::2] = out
            full[1 - parity::2] = self.prev_field
        else:
            full[0::2] = out
            full[1::2] = out
        self.prev_field = out
        self.frame = full
        self.m.frame_ready(full)

    def render_plane(self, planes, recs, p):
        first = recs[0]
        n = len(recs)
        width = first.key[-1] if first.kind == 'off' else first.key[10]
        if first.kind == 'off':
            tp_ctrl = first.key[1]
            return (np.full((n, width), COLOR_OFF, dtype=np.uint32),
                    np.full((n, width), tp_ctrl == 0, dtype=bool))
        (icm, _p, ft, mosaic_factor, _cv, clut_select, tp_ctrl, trans_color, mask_color,
         _mv, width) = first.key[:11]
        runs, clut, matte_flags, _weights = first.runs
        tp_type = tp_ctrl & 7
        parity = not (tp_ctrl & 8)
        tp_always = tp_type == 0 and parity
        use_rgb_tp = tp_type == 2
        use_matte = 3 <= tp_type <= 6
        mflags = matte_flags[(~tp_type) & 1][:width]
        is_dyuv_rgb = icm == ICM_DYUV or (icm == ICM_RGB555 and p == 1)
        use_key = not is_dyuv_rgb and tp_type in (1, 5, 6)
        mask_bits = (~mask_color) & 0x00fcfcfc
        tp_match = trans_color & mask_bits

        if first.kind == 'dyuv':
            colors = self.render_dyuv(planes[p], np.array([r.vsr for r in recs]), width, first.key[11])
            if use_matte and not tp_always:
                tr = np.broadcast_to(mflags == parity, (n, width)).copy()
            else:
                tr = np.full((n, width), tp_always, dtype=bool)
            return colors, tr

        rgb_bits = None
        if first.kind == 'rgb':
            lm = mosaic_factor * 2 if mosaic_factor else 2
            nb = -(-width // lm)
            idx = (np.array([r.vsr for r in recs])[:, None] + np.arange(nb)[None, :]) & 0x7ffff
            idx2 = (np.array([r.vsr2 for r in recs])[:, None] + np.arange(nb)[None, :]) & 0x7ffff
            b0 = planes[1][idx].astype(np.uint32)
            b1 = planes[0][idx2].astype(np.uint32)
            blue = (b0 & 0x1f) << 3
            green = ((b0 & 0xe0) >> 2) + ((b1 & 3) << 6)
            red = (b1 & 0x7c) << 1
            c0 = (red << 16) | (green << 8) | blue
            c1 = c0
            if use_rgb_tp:
                rgb_bits = np.repeat(((b1 >> 7) & 1).astype(bool) == parity, lm, axis=1)[:, :width]
            colors = np.empty((n, nb, lm), dtype=np.uint32)
            colors[:, :, 0::2] = c0[:, :, None]
            colors[:, :, 1::2] = c1[:, :, None]
            colors = colors.reshape(n, nb * lm)[:, :width]
        elif first.kind == 'bmp':
            lm = mosaic_factor * 2 if mosaic_factor else 2
            nb = -(-width // lm)
            idx = (np.array([r.vsr for r in recs])[:, None] + np.arange(nb)[None, :]) & 0x7ffff
            data = planes[p][idx]
            if icm == ICM_CLUT4:
                c0 = clut[byte_to_clut_lut(icm, p, clut_select, 'hi')[data]]
                c1 = clut[byte_to_clut_lut(icm, p, clut_select, 'lo')[data]]
            else:
                c0 = clut[byte_to_clut_lut(icm, p, clut_select)[data]]
                c1 = c0
            colors = np.empty((n, nb, lm), dtype=np.uint32)
            colors[:, :, 0::2] = c0[:, :, None]
            colors[:, :, 1::2] = c1[:, :, None]
            colors = colors.reshape(n, nb * lm)[:, :width]
        else:  # rle: every line's runs add up to exactly width pixels
            codes = np.concatenate([r.runs[0][0] for r in recs])
            lens = np.concatenate([r.runs[0][1] for r in recs])
            if icm == ICM_CLUT4:
                c0 = clut[byte_to_clut_lut(icm, p, clut_select, 'hi', 0x7)[codes]]
                c1 = clut[byte_to_clut_lut(icm, p, clut_select, 'lo', 0x7)[codes]]
                p0 = np.repeat(c0, lens)
                p1 = np.repeat(c1, lens)
                p0[1::2] = p1[1::2]
            else:
                p0 = np.repeat(clut[byte_to_clut_lut(icm, p, clut_select)[codes]], lens)
            colors = p0.reshape(n, width)

        fixed = not use_key and not use_matte and not (use_rgb_tp and first.kind == 'rgb')
        if fixed:
            tr = np.full(colors.shape, tp_always, dtype=bool)
        else:
            tr = np.zeros(colors.shape, dtype=bool)
            if rgb_bits is not None:
                tr |= rgb_bits
            if use_key:
                tr |= ((colors & mask_bits) == tp_match) == parity
            if use_matte:
                tr |= (mflags == parity)[None, :]
        return colors, tr

    def render_dyuv(self, data, vsrs, width, start):
        lib = native_lib()
        if lib is not None:
            vsrs = np.ascontiguousarray(vsrs, dtype=np.int64)
            n = len(vsrs)
            out = np.empty((n, width), dtype=np.uint32)
            lib.vid_dyuv(data.ctypes.data, vsrs.ctypes.data, n, width, start, _DELTAS32.ctypes.data,
                         _LIMIT_FE32.ctypes.data, _U_TO_B32.ctypes.data, _U_TO_G32.ctypes.data,
                         _V_TO_G32.ctypes.data, _V_TO_R32.ctypes.data, COLOR_OFF, out.ctypes.data)
            return out
        pairs = width // 4
        n = len(vsrs)
        idx = (vsrs[:, None] + np.arange(pairs * 2)[None, :]) & 0x7ffff
        raw = data[idx].astype(np.int32)
        b0 = raw[:, 0::2]
        b1 = raw[:, 1::2]
        y0 = (start >> 16) & 0xff
        u0 = (start >> 8) & 0xff
        v0 = start & 0xff
        dy = np.empty((n, pairs * 2), dtype=np.int32)
        dy[:, 0::2] = DYUV_DELTAS[b0 & 15]
        dy[:, 1::2] = DYUV_DELTAS[b1 & 15]
        ys = (y0 + np.cumsum(dy, axis=1)) & 0xff
        us = (u0 + np.cumsum(DYUV_DELTAS[b0 >> 4], axis=1)) & 0xff
        vs = (v0 + np.cumsum(DYUV_DELTAS[b1 >> 4], axis=1)) & 0xff
        un = np.concatenate([us[:, 1:], us[:, -1:]], axis=1)
        vn = np.concatenate([vs[:, 1:], vs[:, -1:]], axis=1)
        uh = ((us + un) >> 1) & 0xff
        vh = ((vs + vn) >> 1) & 0xff
        c0 = self.yuv_to_rgb(ys[:, 0::2], us, vs)
        c1 = self.yuv_to_rgb(ys[:, 1::2], uh, vh)
        out = np.empty((n, pairs, 4), dtype=np.uint32)
        out[:, :, 0] = c0
        out[:, :, 1] = c0
        out[:, :, 2] = c1
        out[:, :, 3] = c1
        out = out.reshape(n, pairs * 4)
        if out.shape[1] < width:
            out = np.concatenate([out, np.full((n, width - out.shape[1]), COLOR_OFF, dtype=np.uint32)], axis=1)
        return out

    @staticmethod
    def yuv_to_rgb(y, u, v):
        y0 = y + 0x100
        r = LIMIT_FE[np.clip(y0 + V_TO_R[v], 0, 0x2ff)]
        g = LIMIT_FE[np.clip(y0 + ((U_TO_G[u] + V_TO_G[v]) >> 8), 0, 0x2ff)]
        b = LIMIT_FE[np.clip(y0 + U_TO_B[u], 0, 0x2ff)]
        return ((r << 16) | (g << 8) | b).astype(np.uint32)

    def mix(self, ca, ta, cb, tb, rec):
        icm_a, icm_b, disable_mx, order_ba, _mv, mh_a, mh_b, backdrop, ext_video, width = rec[2]
        weights = rec[0].runs[3] if rec[0].kind != 'off' else (rec[1].runs[3] if rec[1].kind != 'off' else self.weight)
        n = ca.shape[0]
        border = 24 if width == 720 else 0
        mca = (mh_a & 0xff) << 1
        mcb = (mh_b & 0xff) << 1
        if icm_a == ICM_CLUT4:
            mca >>= 1
        if icm_b == ICM_CLUT4:
            mcb >>= 1
        if mh_a & 0x800000 and mca:
            xs = np.arange(width)
            ca = ca[:, xs - xs % mca]
        if mh_b & 0x800000 and mcb:
            xs = np.arange(width)
            cb = cb[:, xs - xs % mcb]
        lib = native_lib()
        if lib is not None:
            ca = np.ascontiguousarray(ca, dtype=np.uint32)
            cb = np.ascontiguousarray(cb, dtype=np.uint32)
            ta8 = np.ascontiguousarray(ta, dtype=bool).view(np.uint8)
            tb8 = np.ascontiguousarray(tb, dtype=bool).view(np.uint8)
            wa = np.ascontiguousarray(weights[0][:width], dtype=np.int32)
            wb = np.ascontiguousarray(weights[1][:width], dtype=np.int32)
            out = np.empty((n, 768), dtype=np.uint32)
            lib.vid_mix(ca.ctypes.data, ta8.ctypes.data, cb.ctypes.data, tb8.ctypes.data, n, width,
                        wa.ctypes.data, wb.ctypes.data, 1 if disable_mx else 0, 1 if order_ba else 0,
                        DISPLAY_COLORS[0] if ext_video else DISPLAY_COLORS[backdrop],
                        _LIMIT32.ctypes.data, _EXPAND32.ctypes.data, border, COLOR_OFF, out.ctypes.data)
            return out
        a = np.where(ta, COLOR_OFF, ca)
        b = np.where(tb, COLOR_OFF, cb)
        if disable_mx:
            if not order_ba:
                b[~ta] = COLOR_OFF
            else:
                a[~tb] = COLOR_OFF
        wa = weights[0][:width]
        wb = weights[1][:width]
        out = np.empty((n, 768), dtype=np.uint32)
        if border:
            out[:, :border] = 0
            out[:, border + width:] = 0
        res = out[:, border:border + width]
        wa0, wb0 = int(wa[0]), int(wb[0])
        if (wa == wa0).all() and (wb == wb0).all():
            # constant weights: one 64K-entry table per weight pair mixes all three channels
            table = mix_table(wa0, wb0)
            av = np.ascontiguousarray(a, dtype=np.uint32).view(np.uint8).reshape(n, width, 4)
            bv = np.ascontiguousarray(b, dtype=np.uint32).view(np.uint8).reshape(n, width, 4)
            idx = av.astype(np.uint16) << 8
            idx |= bv
            mixed = table[idx]
            mixed[..., 3] = 0
            res[...] = mixed.view(np.uint32).reshape(n, width)
        else:
            res[...] = 0
            wa = wa.astype(np.int32)[None, :]
            wb = wb.astype(np.int32)[None, :]
            for shift in (16, 8, 0):
                pa = ((a >> shift) & 0xff).astype(np.int32)
                pb = ((b >> shift) & 0xff).astype(np.int32)
                mixed = LIMIT[((pa - 16) * wa >> 6) + ((pb - 16) * wb >> 6) + 0x110]
                res |= EXPAND[mixed].astype(np.uint32) << shift
        both = ta & tb
        if both.any():
            res[both] = DISPLAY_COLORS[0] if ext_video else DISPLAY_COLORS[backdrop]
        return out

    def draw_cursor(self, out):
        cc = self.cursor_ctrl
        if not cc & CURCNT_EN:
            return
        color_index = cc & 0xf
        if self.blink_active:
            if not cc & CURCNT_BLKC:
                return
            color_index ^= 0x7
        cx = self.cursor_pos & 0x3ff
        cy = (self.cursor_pos >> 12) & 0x3ff
        res = 1 if cc & CURCNT_CUW else 2
        color = DISPLAY_COLORS[color_index]
        width = self.screen_width()
        # the picture sits after the border in 720-pixel mode; the cursor
        # position is relative to the picture, so it moves with it
        border = self.border_width()
        if border and self._cursor_logged != (cx, cy):
            self._cursor_logged = (cx, cy)
            dlog.debug('cursor at %d,%d drawn at x=%d (720-pixel mode, border %d)', cx, cy, cx + border, border)
        for y in range(16):
            row = cy + y
            if not 0 <= row < out.shape[0]:
                continue
            pat = self.cursor_pattern[y]
            if not pat:
                continue
            for x in range(16):
                if pat >> (15 - x) & 1:
                    for j in range(res):
                        idx = cx + x * res + j
                        if idx < width:
                            out[row, border + idx] = color
