"""FLAC frame decoder, for the audio hunks of CHD files ("cdfl").

CHD stores CD audio as bare FLAC frames (no "fLaC" header or STREAMINFO):
16-bit stereo, 44.1 kHz.  This module decodes such frames in pure Python.  It
is the reference and the fallback; opencdi/native/flac.c does the same much
faster and is used when the native library is loaded.
"""


class FlacError(ValueError):
    pass


class _Bits:
    __slots__ = ('data', 'pos', 'end')

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos * 8          # bit position
        self.end = len(data) * 8

    def read(self, n):
        if n == 0:
            return 0
        if self.pos + n > self.end:
            raise FlacError('FLAC data ends early')
        p = self.pos
        byte = p >> 3
        nb = ((p & 7) + n + 7) >> 3
        v = int.from_bytes(self.data[byte:byte + nb], 'big')
        v >>= nb * 8 - (p & 7) - n
        self.pos = p + n
        return v & ((1 << n) - 1)

    def signed(self, n):
        v = self.read(n)
        return v - (1 << n) if n and v >> (n - 1) else v

    def unary(self):
        """Number of 0 bits before the next 1 bit."""
        n = 0
        data, p = self.data, self.pos
        while True:
            byte = p >> 3
            if byte >= len(data):
                raise FlacError('FLAC data ends early')
            b = (data[byte] << (p & 7)) & 0xff
            if b:
                z = 8 - b.bit_length()
                self.pos = p + z + 1
                return n + z
            n += 8 - (p & 7)
            p = (byte + 1) * 8

    def align(self):
        self.pos = (self.pos + 7) & ~7


BLOCK_SIZES = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608, 8: 256, 9: 512, 10: 1024, 11: 2048,
               12: 4096, 13: 8192, 14: 16384, 15: 32768}
SAMPLE_BITS = {1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}


def _utf8(b):
    v = b.read(8)
    if v < 0x80:
        return v
    n = 0
    mask = 0x40
    while v & mask:
        n += 1
        mask >>= 1
    v &= mask - 1
    for _ in range(n):
        v = (v << 6) | (b.read(8) & 0x3f)
    return v


def _residual(b, blocksize, order, out):
    method = b.read(2)
    if method > 1:
        raise FlacError('reserved residual coding')
    pbits, escape = (4, 15) if method == 0 else (5, 31)
    porder = b.read(4)
    parts = 1 << porder
    n = blocksize >> porder
    for p in range(parts):
        count = n - order if p == 0 else n
        k = b.read(pbits)
        if k == escape:
            bits = b.read(5)
            for _ in range(count):
                out.append(b.signed(bits))
        else:
            for _ in range(count):
                v = (b.unary() << k) | b.read(k)
                out.append((v >> 1) ^ -(v & 1))


FIXED = ((), (1,), (2, -1), (3, -3, 1), (4, -6, 4, -1))


def _subframe(b, blocksize, bps):
    if b.read(1):
        raise FlacError('bad subframe padding')
    kind = b.read(6)
    wasted = 0
    if b.read(1):
        wasted = b.unary() + 1
        bps -= wasted
    if kind == 0:
        s = [b.signed(bps)] * blocksize
    elif kind == 1:
        s = [b.signed(bps) for _ in range(blocksize)]
    elif 8 <= kind <= 12 or kind >= 32:
        if kind >= 32:
            order = kind - 31
            s = [b.signed(bps) for _ in range(order)]
            prec = b.read(4) + 1
            if prec == 16:
                raise FlacError('bad LPC precision')
            shift = b.signed(5)
            coefs = [b.signed(prec) for _ in range(order)]
        else:
            order = kind - 8
            s = [b.signed(bps) for _ in range(order)]
            coefs = list(FIXED[order])
            shift = 0
        res = []
        _residual(b, blocksize, order, res)
        for i, r in enumerate(res):
            j = len(s)
            acc = 0
            for c, coef in enumerate(coefs):
                acc += coef * s[j - 1 - c]
            s.append(r + (acc >> shift if shift >= 0 else acc << -shift))
    else:
        raise FlacError(f'reserved subframe type {kind}')
    if wasted:
        s = [v << wasted for v in s]
    return s


def decode_frames(data, nsamples, channels=2, bps_default=16):
    """Decode FLAC frames from data until nsamples per channel are out.
    Returns (samples, bytes used): samples is a list per channel."""
    b = _Bits(data)
    chans = [[] for _ in range(channels)]
    while len(chans[0]) < nsamples:
        b.align()
        if b.read(15) != 0x7ffc:
            raise FlacError('FLAC frame sync not found')
        b.read(1)                       # blocking strategy
        bs_code = b.read(4)
        sr_code = b.read(4)
        ch_code = b.read(4)
        ss_code = b.read(3)
        b.read(1)
        _utf8(b)
        if bs_code == 6:
            blocksize = b.read(8) + 1
        elif bs_code == 7:
            blocksize = b.read(16) + 1
        elif bs_code in BLOCK_SIZES:
            blocksize = BLOCK_SIZES[bs_code]
        else:
            raise FlacError('reserved block size')
        if sr_code == 12:
            b.read(8)
        elif sr_code in (13, 14):
            b.read(16)
        b.read(8)                       # CRC-8 of the header
        bps = SAMPLE_BITS.get(ss_code, bps_default) if ss_code else bps_default
        if ch_code < 8:
            if ch_code + 1 != channels:
                raise FlacError('unexpected channel count')
            subs = [_subframe(b, blocksize, bps) for _ in range(channels)]
        else:
            if channels != 2 or ch_code > 10:
                raise FlacError('bad channel assignment')
            a = _subframe(b, blocksize, bps + (1 if ch_code == 9 else 0))
            c = _subframe(b, blocksize, bps + (0 if ch_code == 9 else 1))
            if ch_code == 8:              # left / side
                subs = [a, [x - y for x, y in zip(a, c)]]
            elif ch_code == 9:            # side / right
                subs = [[x + y for x, y in zip(a, c)], c]
            else:                         # mid / side
                left, right = [], []
                for m, sd in zip(a, c):
                    m = (m << 1) | (sd & 1)
                    left.append((m + sd) >> 1)
                    right.append((m - sd) >> 1)
                subs = [left, right]
        b.align()
        b.read(16)                      # CRC-16 of the frame
        for ch, s in zip(chans, subs):
            ch.extend(s)
    return [c[:nsamples] for c in chans], b.pos >> 3
