"""MAME CHD (Compressed Hunks of Data) CD-ROM images, version 5.

A CD CHD stores the disc as 2448-byte frames (2352 bytes of sector data and
96 of subcode) grouped into hunks, usually 8 frames each, and every hunk is
compressed on its own.  This module reads the hunk map, the track list (CHT2
metadata) and the CD codecs chdman uses:

* cdlz: sector data LZMA, subcode Deflate (native decoder, or Python's lzma)
* cdzl: both Deflate
* cdfl: sector data FLAC (audio), subcode Deflate (native decoder, or flac.py)
* cdzs: both Zstandard (only if a zstd module is available: Python 3.14's
  compression.zstd, or the zstandard / pyzstd packages)

plus uncompressed hunks and hunks that repeat another hunk ("copy from self").
Sectors that chdman stored without their sync pattern and ECC get both
regenerated.  Images that need a parent CHD, and versions before 5, are not
supported (``chdman copy`` converts old ones).

The format follows MAME's chd.cpp / cdrom.cpp and libchdr (BSD-3-Clause).
"""

import binascii
import os
import re
import struct
import zlib
from collections import OrderedDict

import numpy as np

from . import debuglog, flac

try:
    import lzma
except ImportError:          # the Android Python has no lzma module: the native decoder does it
    lzma = None

dlog = debuglog.get('chd')

FRAME = 2448                 # CD frame in a CHD: sector + subcode
SECTOR = 2352
SUBCODE = 96
TRACK_PADDING = 4            # each track's frames start on a multiple of 4
SYNC = b'\x00' + b'\xff' * 10 + b'\x00'

# map entry compression types (chd.h)
TYPE_0, TYPE_1, TYPE_2, TYPE_3, NONE, SELF, PARENT = 0, 1, 2, 3, 4, 5, 6
RLE_SMALL, RLE_LARGE, SELF_0, SELF_1, PARENT_SELF, PARENT_0, PARENT_1 = 7, 8, 9, 10, 11, 12, 13

# track types of the CHT2 metadata: (kind for disc.Track, bytes stored per frame)
TRACK_TYPES = {
    'MODE1': ('mode1', 2048), 'MODE1/2048': ('mode1', 2048),
    'MODE1_RAW': ('mode1', 2352), 'MODE1/2352': ('mode1', 2352),
    'MODE2': ('mode2', 2336), 'MODE2/2336': ('mode2', 2336),
    'MODE2_FORM1': ('mode2', 2048), 'MODE2/2048': ('mode2', 2048),
    'MODE2_FORM2': ('mode2', 2324), 'MODE2/2324': ('mode2', 2324),
    'MODE2_FORM_MIX': ('mode2', 2336), 'MODE2/2336X': ('mode2', 2336),
    'MODE2_RAW': ('mode2', 2352), 'MODE2/2352': ('mode2', 2352), 'CDI/2352': ('mode2', 2352),
    'AUDIO': ('audio', 2352),
}


class ChdError(ValueError):
    pass


# ----- bit reader and Huffman decoder for the compressed hunk map (huffman.cpp) ------
class _BitIn:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.bits = len(data) * 8

    def read(self, n):
        if n == 0:
            return 0
        p = self.pos
        byte = p >> 3
        nb = ((p & 7) + n + 7) >> 3
        chunk = self.data[byte:byte + nb].ljust(nb, b'\0')     # past the end reads as 0
        v = int.from_bytes(chunk, 'big') >> (nb * 8 - (p & 7) - n)
        self.pos = p + n
        return v & ((1 << n) - 1)

    def peek(self, n):
        p = self.pos
        v = self.read(n)
        self.pos = p
        return v


class _Huffman:
    """MAME's huffman_decoder<numcodes, maxbits> with an RLE-coded tree."""

    def __init__(self, numcodes, maxbits):
        self.numcodes, self.maxbits = numcodes, maxbits

    def import_tree_rle(self, bits):
        numbits = 5 if self.maxbits >= 16 else 4 if self.maxbits >= 8 else 3
        lengths = []
        while len(lengths) < self.numcodes:
            nb = bits.read(numbits)
            if nb != 1:
                lengths.append(nb)
            else:
                nb = bits.read(numbits)
                if nb == 1:
                    lengths.append(nb)
                else:
                    lengths.extend([nb] * (bits.read(numbits) + 3))
        if len(lengths) != self.numcodes:
            raise ChdError('bad Huffman tree in the hunk map')
        # canonical codes, MAME style: assigned from the longest length down
        histo = [0] * 33
        for n in lengths:
            if n > self.maxbits:
                raise ChdError('bad Huffman code length')
            histo[n] += 1
        start = 0
        for length in range(32, 0, -1):
            nxt = (start + histo[length]) >> 1
            if length != 1 and nxt * 2 != start + histo[length]:
                raise ChdError('bad Huffman tree')
            histo[length] = start
            start = nxt
        self.lookup = [None] * (1 << self.maxbits)
        for code, n in enumerate(lengths):
            if n == 0:
                continue
            bits_ = histo[n]
            histo[n] += 1
            shift = self.maxbits - n
            for k in range(bits_ << shift, (bits_ + 1) << shift):
                self.lookup[k] = (code, n)

    def decode(self, bits):
        code, n = self.lookup[bits.peek(self.maxbits)]
        bits.pos += n
        return code


# ----- CD-ROM ECC (cdrom.cpp: ecc_generate) ---------------------------------------
def _ecc_tables():
    low = np.array([((i << 1) ^ (0x11d if i & 0x80 else 0)) & 0xff for i in range(256)], dtype=np.uint8)
    high = np.zeros(256, dtype=np.uint8)
    for i in range(256):
        high[low[i] ^ i] = i
    p = np.array([[b + 86 * c for c in range(24)] for b in range(86)], dtype=np.intp)
    q = np.array([[2 * ((43 * (b >> 1) + 44 * c) % 1118) + (b & 1) for c in range(43)] for b in range(52)],
                 dtype=np.intp)
    return low, high, p, q


_ECC_LOW, _ECC_HIGH, _ECC_P, _ECC_Q = _ecc_tables()


def _ecc_rows(src, rows):
    v1 = np.zeros(rows.shape[0], dtype=np.uint8)
    v2 = np.zeros(rows.shape[0], dtype=np.uint8)
    for c in range(rows.shape[1]):
        s = src[rows[:, c]]
        v1 ^= s
        v2 ^= s
        v1 = _ECC_LOW[v1]
    v1 = _ECC_HIGH[_ECC_LOW[v1] ^ v2]
    v2 ^= v1
    return v1, v2


def ecc_generate(sector):
    """Fill in the P and Q parity of a raw sector (bytearray, 2352 bytes)."""
    src = np.frombuffer(bytes(sector[12:12 + 2236]), dtype=np.uint8).copy()
    if sector[15] == 2:
        src[0:4] = 0                     # mode 2: the header does not count
    p1, p2 = _ecc_rows(src, _ECC_P)
    sector[2076:2076 + 86] = p1.tobytes()
    sector[2076 + 86:2076 + 172] = p2.tobytes()
    src[2064:2064 + 172] = np.frombuffer(bytes(sector[2076:2076 + 172]), dtype=np.uint8)
    q1, q2 = _ecc_rows(src, _ECC_Q)
    sector[2248:2248 + 52] = q1.tobytes()
    sector[2248 + 52:2248 + 104] = q2.tobytes()


# ----- codecs ---------------------------------------------------------------------
def _inflate(data, size):
    d = zlib.decompressobj(-15)
    out = d.decompress(data, size)
    return out


def _lzma_dict_size(reduce_size):
    # LzmaEncProps_Normalize at level 9 for a buffer of reduce_size bytes
    size = 1 << 26
    if size > reduce_size:
        for i in range(11, 31):
            if reduce_size <= 2 << i:
                size = 2 << i
                break
            if reduce_size <= 3 << i:
                size = 3 << i
                break
    return size


def _unlzma(data, size):
    lib = _native()
    if lib is not None:
        import ctypes
        out = ctypes.create_string_buffer(size)
        n = lib.lzma_decode_raw(data, len(data), out, size)
        if n != size:
            raise ChdError(f'bad LZMA data in hunk (native decoder: {n})')
        return out.raw
    if lzma is None:
        raise ChdError('this CHD uses LZMA (cdlz), and neither the native library nor Python\'s lzma '
                       'module is available')
    filt = {'id': lzma.FILTER_LZMA1, 'dict_size': max(4096, _lzma_dict_size(size)), 'lc': 3, 'lp': 0, 'pb': 2}
    d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[filt])
    return d.decompress(data, size)


def _zstd():
    try:
        from compression import zstd          # Python 3.14+
        return lambda data, size: zstd.decompress(data)[:size]
    except ImportError:
        pass
    try:
        import zstandard
        return lambda data, size: zstandard.ZstdDecompressor().decompress(data, max_output_size=size)
    except ImportError:
        pass
    try:
        import pyzstd
        return lambda data, size: pyzstd.decompress(data)[:size]
    except ImportError:
        return None


def _native():
    """The native library (FLAC and LZMA decoders), or None."""
    if not _NATIVE:
        lib = None
        if os.environ.get('OPENCDI_CPU', 'native').lower() != 'python':
            from . import m68k_native
            lib = m68k_native.load()
        _NATIVE.append(lib)
    return _NATIVE[0]


_NATIVE = []


def _unflac(data, frames):
    """cdfl sector data: big-endian 16-bit stereo, as CHD stores CD audio.
    Returns (bytes, bytes of data used)."""
    nsamples = frames * SECTOR // 4
    lib = _native()
    if lib is not None:
        import ctypes
        out = ctypes.create_string_buffer(frames * SECTOR)
        used = lib.flac_decode_cd(data, len(data), out, nsamples)
        if used < 0:
            raise ChdError(f'bad FLAC data in hunk (native decoder error {used})')
        return out.raw, used
    try:
        (left, right), used = flac.decode_frames(data, nsamples)
    except flac.FlacError as e:
        raise ChdError(f'bad FLAC data in hunk: {e}') from None
    pcm = np.empty((nsamples, 2), dtype='>i2')
    pcm[:, 0] = left
    pcm[:, 1] = right
    return pcm.tobytes(), used


class _CdCodec:
    """chd_cd_compressor: base codec for the sectors, deflate for the subcode."""

    def __init__(self, name, hunkbytes):
        self.name = name
        self.frames = hunkbytes // FRAME
        self.hunkbytes = hunkbytes
        if name == 'cdlz':
            self.base = _unlzma
        elif name in ('cdzl', 'cdfl'):
            self.base = _inflate
        elif name == 'cdzs':
            self.base = _zstd()
            if self.base is None:
                raise ChdError('this CHD uses Zstandard (cdzs); install the "zstandard" Python package, '
                               'or recompress it with chdman (-c cdlz,cdzl,cdfl)')
        else:
            raise ChdError(f'unsupported CHD codec {name!r}')
        self.sub = self.base if name == 'cdzs' else _inflate

    def decompress(self, src):
        frames = self.frames
        if self.name == 'cdfl':
            sectors, used = _unflac(src, frames)
            subcode = _inflate(src[used:], frames * SUBCODE)
            return self._assemble(sectors, subcode, None)
        ecc_bytes = (frames + 7) // 8
        clen_bytes = 2 if self.hunkbytes < 65536 else 3
        head = ecc_bytes + clen_bytes
        clen = int.from_bytes(src[ecc_bytes:head], 'big')
        sectors = self.base(src[head:head + clen], frames * SECTOR)
        subcode = self.sub(src[head + clen:], frames * SUBCODE)
        return self._assemble(sectors, subcode, src[:ecc_bytes])

    def _assemble(self, sectors, subcode, ecc_flags):
        frames = self.frames
        if len(sectors) < frames * SECTOR:
            raise ChdError(f'{self.name}: sector data decompressed to {len(sectors)} bytes')
        subcode = subcode.ljust(frames * SUBCODE, b'\0')
        out = bytearray(self.hunkbytes)
        for f in range(frames):
            sec = bytearray(sectors[f * SECTOR:(f + 1) * SECTOR])
            if ecc_flags is not None and ecc_flags[f >> 3] & (1 << (f & 7)):
                # chdman dropped the sync pattern and ECC: put them back
                sec[0:12] = SYNC
                ecc_generate(sec)
            out[f * FRAME:f * FRAME + SECTOR] = sec
            out[f * FRAME + SECTOR:(f + 1) * FRAME] = subcode[f * SUBCODE:(f + 1) * SUBCODE]
        return bytes(out)


class ChdTrack:
    def __init__(self, number, ttype, kind, datasize, frames, pregap, pregap_stored, postgap, chd_frame):
        self.number, self.ttype, self.kind, self.datasize = number, ttype, kind, datasize
        self.frames, self.pregap, self.pregap_stored, self.postgap = frames, pregap, pregap_stored, postgap
        self.chd_frame = chd_frame       # first frame of this track's data in the CHD


class ChdFile:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        try:
            self._read_header()
            self._read_map()
            self.tracks = self._read_tracks()
        except BaseException:
            self.f.close()
            raise
        self._cache = OrderedDict()
        dlog.info('%s: CHD v5, %d hunks of %d bytes, codecs %s, %d tracks', path, self.hunkcount,
                  self.hunkbytes, ','.join(c or '-' for c in self.codec_names), len(self.tracks))

    def close(self):
        self.f.close()

    def _read(self, offset, n):
        self.f.seek(offset)
        data = self.f.read(n)
        if len(data) != n:
            raise ChdError(f'CHD file is truncated (offset {offset})')
        return data

    def _read_header(self):
        self.f.seek(0)
        head = self.f.read(16)
        if len(head) < 16 or head[:8] != b'MComprHD':
            raise ChdError('not a CHD file')
        length, version = struct.unpack('>II', head[8:16])
        if version != 5:
            raise ChdError(f'CHD version {version} is not supported; convert it with '
                           f'"chdman copy -i old.chd -o new.chd"')
        h = self._read(0, length)
        self.codec_tags = struct.unpack('>4I', h[16:32])
        self.logicalbytes, self.mapoffset, self.metaoffset = struct.unpack('>QQQ', h[32:56])
        self.hunkbytes, self.unitbytes = struct.unpack('>II', h[56:64])
        parent = h[104:124]
        if any(parent):
            raise ChdError('this CHD needs a parent CHD, which is not supported')
        self.hunkcount = (self.logicalbytes + self.hunkbytes - 1) // self.hunkbytes
        self.codec_names = [tag.to_bytes(4, 'big').decode('latin-1') if tag else None for tag in self.codec_tags]
        if self.hunkbytes % FRAME:
            raise ChdError(f'hunk size {self.hunkbytes} is not a whole number of CD frames')
        self.codecs = [_CdCodec(n, self.hunkbytes) if n else None for n in self.codec_names]

    def _read_map(self):
        n = self.hunkcount
        if not any(self.codec_tags):
            # uncompressed: one 32-bit hunk number per hunk
            raw = self._read(self.mapoffset, 4 * n)
            offs = struct.unpack(f'>{n}I', raw)
            self.map = [(NONE, self.hunkbytes, o * self.hunkbytes, None) if o else (None, 0, 0, None)
                        for o in offs]
            return
        mh = self._read(self.mapoffset, 16)
        mapbytes = int.from_bytes(mh[0:4], 'big')
        first = int.from_bytes(mh[4:10], 'big')
        mapcrc = int.from_bytes(mh[10:12], 'big')
        lengthbits, selfbits, parentbits = mh[12], mh[13], mh[14]
        bits = _BitIn(self._read(self.mapoffset + 16, mapbytes))
        huff = _Huffman(16, 8)
        huff.import_tree_rle(bits)
        types = []
        rep = 0
        last = 0
        for _ in range(n):
            if rep:
                types.append(last)
                rep -= 1
                continue
            v = huff.decode(bits)
            if v == RLE_SMALL:
                types.append(last)
                rep = 2 + huff.decode(bits)
            elif v == RLE_LARGE:
                types.append(last)
                rep = 2 + 16 + (huff.decode(bits) << 4)
                rep += huff.decode(bits)
            else:
                types.append(v)
                last = v
        entries = []
        raw = bytearray()
        cur = first
        last_self = last_parent = 0
        for hunk, t in enumerate(types):
            offset, length, crc = cur, 0, 0
            if t <= TYPE_3:
                length = bits.read(lengthbits)
                cur += length
                crc = bits.read(16)
            elif t == NONE:
                length = self.hunkbytes
                cur += length
                crc = bits.read(16)
            elif t == SELF:
                last_self = offset = bits.read(selfbits)
            elif t == PARENT:
                offset = bits.read(parentbits)
                last_parent = offset
            elif t in (SELF_0, SELF_1):
                if t == SELF_1:
                    last_self += 1
                t, offset = SELF, last_self
            elif t == PARENT_SELF:
                t = PARENT
                last_parent = offset = (hunk * self.hunkbytes) // self.unitbytes
            elif t in (PARENT_0, PARENT_1):
                if t == PARENT_1:
                    last_parent += self.hunkbytes // self.unitbytes
                t, offset = PARENT, last_parent
            else:
                raise ChdError(f'bad hunk map entry type {t}')
            entries.append((t, length, offset, crc))
            raw += bytes([t]) + length.to_bytes(3, 'big') + offset.to_bytes(6, 'big') + crc.to_bytes(2, 'big')
        if binascii.crc_hqx(bytes(raw), 0xffff) != mapcrc:
            raise ChdError('hunk map checksum mismatch (damaged CHD?)')
        self.map = entries

    def _metadata(self):
        off = self.metaoffset
        seen = 0
        while off and seen < 10000:
            h = self._read(off, 16)
            tag = h[0:4].decode('latin-1')
            length = int.from_bytes(h[5:8], 'big')
            nxt = int.from_bytes(h[8:16], 'big')
            yield tag, self._read(off + 16, length)
            off = nxt
            seen += 1

    def _read_tracks(self):
        tracks = []
        chd_frame = 0
        for tag, data in self._metadata():
            if tag not in ('CHT2', 'CHTR'):
                if tag in ('CHGD', 'CHCD'):
                    raise ChdError(f'CHD metadata {tag} (GD-ROM or very old CD) is not supported')
                continue
            text = data.rstrip(b'\0').decode('ascii', 'replace')
            fields = dict(re.findall(r'(\w+):(\S+)', text))
            try:
                number = int(fields['TRACK'])
                ttype = fields['TYPE']
                frames = int(fields['FRAMES'])
            except (KeyError, ValueError):
                raise ChdError(f'bad CHD track metadata: {text!r}') from None
            if ttype not in TRACK_TYPES:
                raise ChdError(f'unsupported CHD track type {ttype}')
            kind, datasize = TRACK_TYPES[ttype]
            pregap = int(fields.get('PREGAP', 0))
            pgtype = fields.get('PGTYPE', 'MODE1')
            postgap = int(fields.get('POSTGAP', 0))
            # a pregap whose type starts with V has its data in the CHD
            stored = pgtype.startswith('V') and pregap > 0
            tracks.append(ChdTrack(number, ttype, kind, datasize, frames, pregap, stored, postgap, chd_frame))
            chd_frame += (frames + TRACK_PADDING - 1) // TRACK_PADDING * TRACK_PADDING
        if not tracks:
            raise ChdError('CHD has no CD track metadata (not a CD image?)')
        tracks.sort(key=lambda t: t.number)
        return tracks

    # ----- hunks -----------------------------------------------------------------
    def hunk(self, n, depth=0):
        h = self._cache.get(n)
        if h is not None:
            self._cache.move_to_end(n)
            return h
        if not 0 <= n < self.hunkcount:
            raise ChdError(f'hunk {n} out of range')
        t, length, offset, crc = self.map[n]
        if t is None:
            data = bytes(self.hunkbytes)
        elif t <= TYPE_3:
            codec = self.codecs[t]
            if codec is None:
                raise ChdError(f'hunk {n} uses codec slot {t}, which is empty')
            data = codec.decompress(self._read(offset, length))
        elif t == NONE:
            data = self._read(offset, self.hunkbytes)
        elif t == SELF:
            if depth > 8 or offset == n:
                raise ChdError(f'hunk {n}: bad self reference')
            data = self.hunk(offset, depth + 1)
        else:
            raise ChdError('this CHD refers to a parent CHD, which is not supported')
        if crc is not None and t != SELF and binascii.crc_hqx(data, 0xffff) != crc:
            raise ChdError(f'hunk {n}: checksum mismatch (damaged CHD?)')
        self._cache[n] = data
        if len(self._cache) > 64:
            self._cache.popitem(last=False)
        return data

    def frame(self, n):
        """2448 bytes of CHD frame n (sector data first, then subcode)."""
        per = self.hunkbytes // FRAME
        h = self.hunk(n // per)
        o = (n % per) * FRAME
        return h[o:o + FRAME]
