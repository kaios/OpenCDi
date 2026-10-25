"""Minimal CHD v5 CD writer, for tests only (tools/check_fixes.py).

Writes what opencdi/chd.py reads, without needing MAME's chdman: codec cdzl,
cdlz or cdzs (zstd, if a zstd module is installed), uncompressed hunks,
"copy from self" hunks for repeated hunks, and sync/ECC stripping for sectors
whose ECC checks out (as chdman does).  The hunk map uses a fixed Huffman code
(16 codes of 4 bits), which is a valid MAME map encoding.

write_cd_chd(path, tracks, codec='cdzl', hunk_frames=8, store=())
    store: hunk numbers to keep uncompressed even if they compress
    tracks: list of (type, sectors), type a CHD track type ('MODE1_RAW',
    'MODE2_RAW', 'AUDIO', ...) and sectors a list of raw sector bytes as in a
    .bin file (audio little-endian; it is stored big-endian, like chdman).
"""
import binascii
import struct
import zlib

FRAME, SECTOR = 2448, 2352
SYNC = b'\x00' + b'\xff' * 10 + b'\x00'


class _BitOut:
    def __init__(self):
        self.v = 0
        self.n = 0

    def write(self, value, bits):
        self.v = (self.v << bits) | (value & ((1 << bits) - 1))
        self.n += bits

    def bytes(self):
        pad = (-self.n) % 8
        return (self.v << pad).to_bytes((self.n + pad) // 8, 'big')


def _deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def _lzma(data):
    import lzma
    from opencdi.chd import _lzma_dict_size
    filt = {'id': lzma.FILTER_LZMA1, 'dict_size': max(4096, _lzma_dict_size(len(data))),
            'lc': 3, 'lp': 0, 'pb': 2}
    return lzma.compress(data, format=lzma.FORMAT_RAW, filters=[filt])


def _zstd(data):
    try:
        from compression import zstd
        return zstd.compress(data)
    except ImportError:
        import zstandard
        return zstandard.ZstdCompressor().compress(data)


def write_cd_chd(path, tracks, codec='cdzl', hunk_frames=8, store=()):
    from opencdi.chd import ecc_generate
    frames = []
    meta = []
    for num, (ttype, sectors) in enumerate(tracks, 1):
        for s in sectors:
            s = bytes(s)
            if ttype == 'AUDIO':
                b = bytearray(s)
                b[0::2], b[1::2] = s[1::2], s[0::2]
                s = bytes(b)
            frames.append(s.ljust(SECTOR, b'\0') + bytes(FRAME - SECTOR))
        pad = (-len(sectors)) % 4
        frames.extend([bytes(FRAME)] * pad)
        meta.append(f'TRACK:{num} TYPE:{ttype} SUBTYPE:NONE FRAMES:{len(sectors)} PREGAP:0 '
                    f'PGTYPE:MODE1 PGSUB:NONE POSTGAP:0'.encode() + b'\0')
    hunkbytes = hunk_frames * FRAME
    while len(frames) % hunk_frames:
        frames.append(bytes(FRAME))
    hunks = [b''.join(frames[i:i + hunk_frames]) for i in range(0, len(frames), hunk_frames)]
    base = {'cdzl': _deflate, 'cdlz': _lzma, 'cdzs': _zstd}[codec]
    sub = _zstd if codec == 'cdzs' else _deflate

    def compress(h):
        flags = bytearray((hunk_frames + 7) // 8)
        secs = bytearray()
        subs = bytearray()
        for f in range(hunk_frames):
            s = bytearray(h[f * FRAME:f * FRAME + SECTOR])
            if s[0:12] == SYNC and s[15] in (1, 2):
                check = bytearray(s)
                ecc_generate(check)
                if check == s:                    # valid ECC: store without sync and ECC
                    flags[f >> 3] |= 1 << (f & 7)
                    s[0:12] = bytes(12)
                    s[2076:2352] = bytes(276)
            secs += s
            subs += h[f * FRAME + SECTOR:(f + 1) * FRAME]
        cs = base(bytes(secs))
        return bytes(flags) + len(cs).to_bytes(2, 'big') + cs + sub(bytes(subs))

    header_len = 124
    metaoff = header_len
    blob = bytearray()
    for i, m in enumerate(meta):
        nxt = metaoff + len(blob) + 16 + len(m) if i + 1 < len(meta) else 0
        blob += b'CHT2' + bytes([1]) + len(m).to_bytes(3, 'big') + nxt.to_bytes(8, 'big') + m
    first = metaoff + len(blob)
    data = bytearray()
    entries = []
    seen = {}
    for n, h in enumerate(hunks):
        crc = binascii.crc_hqx(h, 0xffff)
        if h in seen:
            entries.append((5, 0, seen[h], 0))                    # copy from self
            continue
        seen[h] = n
        c = compress(h)
        if len(c) < hunkbytes and n not in store:
            entries.append((0, len(c), first + len(data), crc))
            data += c
        else:
            entries.append((4, hunkbytes, first + len(data), crc))  # uncompressed
            data += h
    bits = _BitOut()
    for _ in range(16):
        bits.write(4, 4)                          # every code 4 bits long
    for t, _, _, _ in entries:
        bits.write(t, 4)                          # canonical code of symbol t is t itself
    raw = bytearray()
    for t, length, offset, crc in entries:
        if t == 0:
            bits.write(length, 24)
            bits.write(crc, 16)
        elif t == 4:
            bits.write(crc, 16)
        else:
            bits.write(offset, 24)
        raw += bytes([t]) + length.to_bytes(3, 'big') + offset.to_bytes(6, 'big') + crc.to_bytes(2, 'big')
    mapdata = bits.bytes()
    mapoff = first + len(data)
    maphead = (len(mapdata).to_bytes(4, 'big') + first.to_bytes(6, 'big') +
               binascii.crc_hqx(bytes(raw), 0xffff).to_bytes(2, 'big') + bytes([24, 24, 0, 0]))
    tag = int.from_bytes(codec.encode(), 'big')
    header = (b'MComprHD' + struct.pack('>II', header_len, 5) + struct.pack('>4I', tag, 0, 0, 0) +
              struct.pack('>QQQII', len(hunks) * hunkbytes, mapoff, metaoff, hunkbytes, FRAME) + bytes(60))
    with open(path, 'wb') as f:
        f.write(header + blob + data + maphead + mapdata)
