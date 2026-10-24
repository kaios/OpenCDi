"""CD image access: .cue/.bin sheets and .iso files.

Every read returns a full 2352-byte raw sector.  Images that store less than
that (2048-byte "cooked" ISO, 2336-byte Mode 2 or 2324-byte form 2) get their
sync pattern, header and (for 2048) a synthesized Mode 2 Form 1 subheader.
"""

import os
import re
import shlex
from pathlib import Path

SYNC = b'\x00' + b'\xff' * 10 + b'\x00'
RAW = 2352


def bcd(v):
    return (v // 10) << 4 | v % 10


def msf(lba):
    """Absolute MSF (BCD) of a logical block address (lba 0 = 00:02:00)."""
    a = lba + 150
    return bcd(a // 4500), bcd((a // 75) % 60), bcd(a % 75)


class Track:
    def __init__(self, number, kind, path, offset, sector_size, start, length, pregap=0):
        self.number = number
        self.kind = kind            # 'mode1', 'mode2', 'audio'
        self.path = path
        self.offset = offset        # byte offset of the track's first sector in path
        self.sector_size = sector_size
        self.start = start          # LBA of index 01
        self.length = length        # sectors
        self.pregap = pregap
        self._fh = None

    def fh(self):
        if self._fh is None:
            self._fh = open(self.path, 'rb')
        return self._fh

    def __repr__(self):
        return (f'Track({self.number}, {self.kind}, start={self.start}, '
                f'len={self.length}, size={self.sector_size})')


class Disc:
    def __init__(self, path):
        self.path = Path(path)
        ext = self.path.suffix.lower()
        if ext == '.cue':
            self.tracks = self._parse_cue(self.path)
        elif ext in ('.iso', '.bin', '.img', '.cdi', '.raw'):
            self.tracks = [self._single_file_track(self.path)]
        else:
            raise ValueError(f'unsupported disc image type: {path}')
        if not self.tracks:
            raise ValueError(f'no tracks found in {path}')
        last = self.tracks[-1]
        self.total_sectors = last.start + last.length
        self.byteswapped = False

    # ----- loading -------------------------------------------------------------
    @staticmethod
    def _detect_size(path):
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(RAW * 17)
        if head[:12] == SYNC:
            return RAW
        if head[:12] == bytes([0xff, 0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x00, 0xff]):
            return RAW  # byte swapped raw image, handled at read time
        # CD-i disc label lives in sector 16; check where it appears
        for ss in (2048, 2336, 2352):
            for off in (0, 8, 24):
                p = 16 * ss + off
                if head[p + 1:p + 6] in (b'CD-I ', b'CD001'):
                    return ss
        for ss in (2352, 2336, 2048):
            if size % ss == 0:
                return ss
        return 2048

    def _single_file_track(self, path):
        ss = self._detect_size(path)
        length = os.path.getsize(path) // ss
        kind = 'mode2'
        if ss == RAW:
            with open(path, 'rb') as f:
                f.seek(16 * RAW)
                hdr = f.read(16)
            if hdr[:12] == SYNC and hdr[15] == 1:
                kind = 'mode1'
        return Track(1, kind, path, 0, ss, 0, length)

    def _parse_cue(self, cue):
        text = cue.read_text(errors='replace')
        entries = []
        cur_file = None
        cur = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()
            cmd = parts[0].upper()
            if cmd == 'FILE':
                name = parts[1]
                p = cue.parent / name
                if not p.exists():
                    # case-insensitive fallback
                    for cand in cue.parent.iterdir():
                        if cand.name.lower() == Path(name).name.lower():
                            p = cand
                            break
                cur_file = p
            elif cmd == 'TRACK':
                mode = parts[2].upper()
                if mode == 'AUDIO':
                    kind, ss = 'audio', RAW
                else:
                    m = re.match(r'MODE(\d)/(\d+)', mode)
                    if not m:
                        raise ValueError(f'unsupported track mode {mode}')
                    kind = 'mode' + m.group(1)
                    ss = int(m.group(2))
                    if mode.startswith('CDI'):
                        kind = 'mode2'
                cur = {'num': int(parts[1]), 'kind': kind, 'ss': ss, 'file': cur_file,
                       'index': {}, 'pregap': 0}
                entries.append(cur)
            elif cmd == 'INDEX' and cur is not None:
                mm, ss_, ff = (int(x) for x in parts[2].split(':'))
                cur['index'][int(parts[1])] = (mm * 60 + ss_) * 75 + ff
            elif cmd == 'PREGAP' and cur is not None:
                mm, ss_, ff = (int(x) for x in parts[1].split(':'))
                cur['pregap'] = (mm * 60 + ss_) * 75 + ff
        tracks = []
        disc_pos = 0        # disc LBA following the previous file
        file_base = 0
        accum = 0           # PREGAP sectors (not stored in the file) so far in this file
        for i, e in enumerate(entries):
            if i == 0 or e['file'] != entries[i - 1]['file']:
                file_base = disc_pos
                accum = 0
            accum += e['pregap']
            idx1 = e['index'].get(1, e['index'].get(0, 0))
            nxt = entries[i + 1] if i + 1 < len(entries) else None
            if nxt is not None and nxt['file'] == e['file']:
                length = nxt['index'].get(1, nxt['index'].get(0, 0)) - idx1
            else:
                length = os.path.getsize(e['file']) // e['ss'] - idx1
            start = file_base + accum + idx1
            tracks.append(Track(e['num'], e['kind'], e['file'], idx1 * e['ss'], e['ss'],
                                start, max(length, 0), e['pregap']))
            disc_pos = start + max(length, 0)
        return tracks

    # ----- reading -------------------------------------------------------------------
    def track_for(self, lba):
        for t in reversed(self.tracks):
            if lba >= t.start:
                return t
        return self.tracks[0]

    def is_audio(self, lba):
        return self.track_for(lba).kind == 'audio'

    def read_raw(self, lba):
        """Return 2352 bytes for the sector at lba (0 = 00:02:00)."""
        t = self.track_for(lba)
        rel = lba - t.start
        if rel < 0 or rel >= t.length:
            return self._blank(lba, t)
        f = t.fh()
        f.seek(t.offset + rel * t.sector_size)
        data = f.read(t.sector_size)
        if len(data) < t.sector_size:
            data = data.ljust(t.sector_size, b'\x00')
        ss = t.sector_size
        if ss == RAW:
            return data
        m, s, fr = msf(lba)
        if ss == 2336:      # mode 2, no sync/header
            return SYNC + bytes([m, s, fr, 2]) + data + b'\x00' * (RAW - 16 - 2336)
        if ss == 2324:      # form 2 payload with no subheader
            sub = bytes([0, 0, 0x20, 0]) * 2
            return SYNC + bytes([m, s, fr, 2]) + sub + data + b'\x00' * 4
        if ss == 2048:
            if t.kind == 'mode1':
                return SYNC + bytes([m, s, fr, 1]) + data + b'\x00' * 288
            # cooked CD-i image: synthesize a Form 1 data subheader
            submode = 0x08
            if rel == t.length - 1:
                submode |= 0x81  # EOR + EOF on the last sector
            sub = bytes([0, 0, submode, 0]) * 2
            return SYNC + bytes([m, s, fr, 2]) + sub + data + b'\x00' * 280
        return SYNC + bytes([m, s, fr, 2]) + data.ljust(RAW - 16, b'\x00')

    def _blank(self, lba, track):
        if track.kind == 'audio':
            return bytes(RAW)
        m, s, fr = msf(lba)
        return SYNC + bytes([m, s, fr, 2]) + bytes([0, 0, 0x20, 0]) * 2 + bytes(RAW - 24)

    def close(self):
        for t in self.tracks:
            if t._fh:
                t._fh.close()
                t._fh = None

    def describe(self):
        return ', '.join(repr(t) for t in self.tracks)
