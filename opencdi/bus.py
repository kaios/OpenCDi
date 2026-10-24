"""Address decoding for the SCC68070.

RAM and ROM live in 64 KiB pages (bytearrays) referenced from page tables so
the CPU can access them without any dispatch.  Everything else goes to device
handlers.  The 68070 drives a 24-bit external bus; addresses 0x80000000 to
0xbfffffff reach the on-chip peripherals instead.
"""

from .m68k import AddressError, BusError

PAGE = 0x10000
M32 = 0xffffffff


class Device:
    """Base class for memory-mapped devices.  Addresses are 24-bit (or the
    full 32-bit address for on-chip peripherals)."""

    def read8(self, a):
        v = self.read16(a & ~1)
        return v & 0xff if a & 1 else v >> 8

    def read16(self, a):
        raise BusError(a)

    def write8(self, a, v):
        raise BusError(a, True)

    def write16(self, a, v):
        raise BusError(a, True)


class Bus:
    def __init__(self, internal_space=True):
        self.internal_space = internal_space
        self.blank = bytearray(PAGE)
        self.rp = [None] * 65536
        self.wp = [None] * 65536
        self.fp = [self.blank] * 65536
        # per 24-bit page: list of (start, end, device)
        self.io = [None] * 256
        self.internal = None
        self.regions = {}

    # ----- configuration ------------------------------------------------
    def _mirror(self, page24, rpage, wpage):
        for hi in range(256):
            if 0x80 <= hi < 0xc0 and self.internal_space:
                continue
            pi = hi << 8 | page24
            self.rp[pi] = rpage
            self.wp[pi] = wpage
            self.fp[pi] = rpage if rpage is not None else self.blank

    def map_ram(self, name, base, size):
        pages = [bytearray(PAGE) for _ in range(size // PAGE)]
        for i, p in enumerate(pages):
            self._mirror((base >> 16) + i, p, p)
        self.regions[name] = pages
        return pages

    def map_rom(self, name, base, data):
        data = bytes(data)
        pages = []
        for i in range(0, len(data), PAGE):
            p = bytearray(data[i:i + PAGE].ljust(PAGE, b'\xff'))
            pages.append(p)
            self._mirror((base >> 16) + i // PAGE, p, None)
        self.regions[name] = pages
        return pages

    def map_device(self, start, end, dev):
        """Map dev at [start, end] (inclusive, 24-bit)."""
        for page24 in range(start >> 16, (end >> 16) + 1):
            if self.io[page24] is None:
                self.io[page24] = []
            self.io[page24].append((start, end, dev))
            self._mirror(page24, None, None)

    def set_internal(self, dev):
        self.internal = dev

    # ----- slow path -------------------------------------------------------
    def _dev(self, a, write=False):
        if 0x80000000 <= a < 0xc0000000 and self.internal_space:
            return self.internal, a
        a &= 0xffffff
        ents = self.io[a >> 16]
        if ents:
            for start, end, dev in ents:
                if start <= a <= end:
                    return dev, a
        raise BusError(a, write)

    def io_r8(self, a):
        dev, a = self._dev(a)
        return dev.read8(a)

    def io_r16(self, a):
        dev, a = self._dev(a)
        return dev.read16(a)

    def io_w8(self, a, v):
        if self.rp[a >> 16] is not None:  # ROM
            raise BusError(a & 0xffffff, True)
        dev, a = self._dev(a, True)
        dev.write8(a, v)

    def io_w16(self, a, v):
        if self.rp[a >> 16] is not None:
            raise BusError(a & 0xffffff, True)
        dev, a = self._dev(a, True)
        dev.write16(a, v)

    # ----- fast functions handed to the CPU --------------------------------
    def cpu_functions(self):
        RP, WP = self.rp, self.wp
        io_r8, io_r16, io_w8, io_w16 = self.io_r8, self.io_r16, self.io_w8, self.io_w16

        def rb(a, RP=RP):
            p = RP[a >> 16]
            if p is not None:
                return p[a & 0xffff]
            return io_r8(a)

        def rw(a, RP=RP):
            if a & 1:
                raise AddressError(a)
            p = RP[a >> 16]
            if p is not None:
                o = a & 0xffff
                return p[o] << 8 | p[o + 1]
            return io_r16(a)

        def rl(a, RP=RP):
            if a & 1:
                raise AddressError(a)
            p = RP[a >> 16]
            if p is not None:
                o = a & 0xffff
                if o < 0xfffd:
                    return p[o] << 24 | p[o + 1] << 16 | p[o + 2] << 8 | p[o + 3]
            return rw(a) << 16 | rw((a + 2) & 0xffffffff)

        def wb(a, v, WP=WP):
            p = WP[a >> 16]
            if p is not None:
                p[a & 0xffff] = v
            else:
                io_w8(a, v)

        def ww(a, v, WP=WP):
            if a & 1:
                raise AddressError(a, True)
            p = WP[a >> 16]
            if p is not None:
                o = a & 0xffff
                p[o] = v >> 8
                p[o + 1] = v & 0xff
            else:
                io_w16(a, v)

        def wl(a, v, WP=WP):
            if a & 1:
                raise AddressError(a, True)
            p = WP[a >> 16]
            if p is not None:
                o = a & 0xffff
                if o < 0xfffd:
                    p[o] = v >> 24
                    p[o + 1] = v >> 16 & 0xff
                    p[o + 2] = v >> 8 & 0xff
                    p[o + 3] = v & 0xff
                    return
            ww(a, v >> 16)
            ww((a + 2) & 0xffffffff, v & 0xffff)

        self.rb, self.rw, self.rl, self.wb, self.ww, self.wl = rb, rw, rl, wb, ww, wl
        return {'FP': self.fp, 'rb': rb, 'rw': rw, 'rl': rl, 'wb': wb, 'ww': ww, 'wl': wl}

    # ----- helpers for devices doing DMA ------------------------------------
    def read_word(self, a):
        return self.rw(a & 0xffffff)

    def write_word(self, a, v):
        self.ww(a & 0xffffff, v & 0xffff)
