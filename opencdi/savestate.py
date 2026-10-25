"""Save states: the whole machine at a frame boundary, in a file.

A state holds the CPU registers, all RAM, the NVRAM, every device's registers
and the scheduler's pending events, so a game continues exactly where it was
saved.  States are taken between frames (Machine.run_frame() has returned),
when the video chip has no half-built frame.

File layout (version FORMAT):
    MAGIC
    u32 length, meta JSON      what the launcher lists: time, disc, BIOS, region
    u32 length, thumbnail      zlib of the picture at a quarter size (RGB bytes)
    rest                       zlib of: u32 length, state JSON, then the blobs

The state JSON is built from plain values only; bytes and numpy arrays go in
the blob area and are referenced by index.  Nothing in a state file is ever
executed or unpickled, so a state from somewhere else cannot run code.

States live in ~/.opencdi/states/<disc id>/slot<N>.state (slots 1-9).  The disc id comes
from the disc's track layout and first sectors, so a .chd and a .cue/.bin of
the same disc share their states.
"""

import datetime
import hashlib
import json
import os
import struct
import time
import zlib
from pathlib import Path

import numpy as np

from . import __version__, debuglog
from .sched import Event

dlog = debuglog.get('savestate')

MAGIC = b'OpenCDi state\r\n\x1a\n'
FORMAT = 1
SLOTS = 9
STATE_DIR = Path.home() / '.opencdi' / 'states'
THUMB_STEP = 4               # thumbnail: every 4th pixel of every 4th line

_U32 = struct.Struct('>I')


class StateError(Exception):
    """A state that cannot be saved or loaded; the message is for the user."""


# ----- disc identity ------------------------------------------------------------------
def disc_id(disc):
    """Short id for a disc's contents: equal for a .chd and a .cue/.bin of it."""
    if disc is None:
        return 'nodisc'
    # kept on the disc: the frontend asks from its main thread, and the disc's
    # files must only be read by the emulation thread once that runs
    cached = getattr(disc, 'state_id', None)
    if cached is not None:
        return cached
    h = hashlib.sha1()
    h.update(str(disc.total_sectors).encode())
    for t in disc.tracks:
        h.update(f'|{t.kind},{t.start},{t.length}'.encode())
    first = disc.tracks[0]
    for lba in range(first.start, first.start + min(32, first.length)):
        h.update(disc.read_raw(lba))
    disc.state_id = h.hexdigest()[:16]
    return disc.state_id


def disc_id_for_path(path):
    """disc_id() of the disc image at path (None: no disc).  Raises what Disc() raises."""
    if not path:
        return disc_id(None)
    from .disc import Disc
    d = Disc(path)
    try:
        return disc_id(d)
    finally:
        d.close()


def slot_path(did, slot):
    """The file of slot 1..SLOTS for the disc with id did."""
    return STATE_DIR / did / f'slot{slot}.state'


# ----- value encoding -----------------------------------------------------------------
# Objects that are stored by their attributes (their classes are imported
# lazily: the devices import nothing from here)
def _object_classes():
    from .scc68070 import DmaChannel
    from .slave import Channel
    return {'DmaChannel': DmaChannel, 'Channel': Channel}


class _Encoder:
    def __init__(self):
        self.blobs = []
        self.classes = {c: n for n, c in _object_classes().items()}

    def blob(self, data):
        self.blobs.append(bytes(data))
        return len(self.blobs) - 1

    def enc(self, v):
        if v is None or isinstance(v, (bool, str)):
            return v
        if isinstance(v, (int, np.integer)):
            return int(v)
        if isinstance(v, (float, np.floating)):
            return {'f': float(v).hex()}       # exact, and JSON has no inf/nan
        if isinstance(v, list):
            return [self.enc(x) for x in v]
        if isinstance(v, tuple):
            return {'t': [self.enc(x) for x in v]}
        if isinstance(v, dict):
            return {'d': [[self.enc(k), self.enc(x)] for k, x in v.items()]}
        if isinstance(v, bytearray):
            return {'ba': self.blob(v)}
        if isinstance(v, bytes):
            return {'b': self.blob(v)}
        if isinstance(v, np.ndarray):
            a = np.ascontiguousarray(v)
            return {'a': self.blob(a.tobytes()), 'dt': a.dtype.str, 'sh': list(a.shape)}
        if isinstance(v, Event):
            return {'ev': v.seq}
        if isinstance(v, datetime.timedelta):
            return {'td': [v.days, v.seconds, v.microseconds]}
        name = self.classes.get(type(v))
        if name is not None:
            return {'o': name, 'v': self.enc(vars(v))}
        raise StateError(f'cannot save a value of type {type(v).__name__}')


class _Decoder:
    def __init__(self, blobs, events):
        self.blobs = blobs
        self.events = events        # seq -> the re-created Event
        self.classes = _object_classes()

    def blob(self, i):
        if not isinstance(i, int) or not 0 <= i < len(self.blobs):
            raise StateError('the state file is damaged (bad data reference)')
        return self.blobs[i]

    def dec(self, v):
        if v is None or isinstance(v, (bool, int, str)):
            return v
        if isinstance(v, list):
            return [self.dec(x) for x in v]
        if not isinstance(v, dict) or len(v) not in (1, 2, 3):
            raise StateError('the state file is damaged (bad value)')
        if 'f' in v:
            return float.fromhex(v['f'])
        if 't' in v:
            return tuple(self.dec(x) for x in v['t'])
        if 'd' in v:
            return {self.dec(k): self.dec(x) for k, x in v['d']}
        if 'ba' in v:
            return bytearray(self.blob(v['ba']))
        if 'b' in v:
            return bytes(self.blob(v['b']))
        if 'a' in v:
            dt = np.dtype(v['dt'])
            if dt.hasobject:
                raise StateError('the state file is damaged (bad array type)')
            return np.frombuffer(self.blob(v['a']), dtype=dt).reshape(v['sh']).copy()
        if 'ev' in v:
            return self.events.get(v['ev'])    # an event that has already run: None
        if 'td' in v:
            return datetime.timedelta(*v['td'])
        if 'o' in v:
            cls = self.classes.get(v['o'])
            if cls is None:
                raise StateError(f'the state file is damaged (unknown object {v["o"]!r})')
            obj = cls.__new__(cls)
            for k, x in self.dec(v['v']).items():
                setattr(obj, k, x)
            return obj
        raise StateError('the state file is damaged (bad value)')


# ----- machine <-> state ----------------------------------------------------------------
MACHINE_FIELDS = ('ntsc', 'frames', 'line', '_line_t', '_reset_pending', 'lcd_text', '_resample_rate')
CPU_FIELDS = ('ccr', 'srh', 'usp', 'ssp', 'PC', 'vbr', 'xc', 'ipl', 'nmi_edge', 'stopped', 'halted',
              'FRAME_FORMAT', 'TRACE')
RAM_REGIONS = ('planeA', 'planeB', 'ram5', 'dvcram')


def _devices(m):
    return {'scc': m.scc, 'cdic': m.cdic, 'slave': m.slave, 'video': m.video}


def _device_state(dev):
    skip = getattr(dev, 'STATE_SKIP', ())
    return {k: v for k, v in vars(dev).items() if k not in skip}


def capture(m):
    """The machine's state as (state dict, blobs).  Call between frames."""
    if m.line != 0:
        raise StateError('not at a frame boundary')
    owners = {id(dev): name for name, dev in _devices(m).items()}
    owners[id(m)] = 'machine'
    events = []
    for ev in sorted(e for e in m.sched.q if not e.cancelled):
        owner = owners.get(id(getattr(ev.func, '__self__', None)))
        if owner is None:
            raise StateError(f'cannot save the pending event {ev.func!r}')
        events.append([ev.time, ev.seq, owner, ev.func.__name__])
    e = _Encoder()
    ns = m.cpu.ns
    res = m._resampler
    st = {
        'sched': {'now': m.sched.now, 'seq': m.sched.seq, 'clock': m.sched.clock, 'events': events},
        'cpu': {'R': [int(r) for r in ns['R']], **{k: int(ns[k]) for k in CPU_FIELDS}},
        'machine': {k: e.enc(getattr(m, k)) for k in MACHINE_FIELDS},
        'resampler': {'pos': e.enc(res.pos), 'last': e.enc(res.last)},
        'ram': {name: e.blob(b''.join(m.bus.regions[name])) for name in RAM_REGIONS},
        'nvram': {'data': e.enc(m.nvram.data), 'offset': e.enc(m.nvram.offset)},
        'devices': {name: e.enc(_device_state(dev)) for name, dev in _devices(m).items()},
    }
    return st, e.blobs


def restore(m, st, blobs):
    """Put a captured state into machine m (same BIOS and disc)."""
    # start from a reset machine, so anything a state does not hold (from an
    # older version) has its reset value
    live_input = (m.slave.input_x, m.slave.input_y, m.slave.input_buttons)
    m.reset()
    s = m.sched
    s.clear()
    sch = st['sched']
    s.now, s.seq, s.clock = sch['now'], sch['seq'], sch['clock']
    devs = _devices(m)
    owners = dict(devs, machine=m)
    events = {}
    for t, seq, owner, name in sch['events']:
        obj = owners.get(owner)
        func = getattr(obj, name, None) if obj is not None else None
        if not callable(func):
            raise StateError(f'the state needs {owner}.{name}, which this version does not have')
        ev = Event(t, seq, func)
        events[seq] = ev
        s.q.append(ev)
    s.q.sort()                       # a sorted list is a valid heap
    d = _Decoder(blobs, events)
    for name, pages in m.bus.regions.items():
        if name not in RAM_REGIONS:
            continue
        data = d.blob(st['ram'][name])
        if len(data) != len(pages) * len(pages[0]):
            raise StateError(f'the state file is damaged ({name} has the wrong size)')
        n = len(pages[0])
        for i, p in enumerate(pages):
            p[:] = data[i * n:(i + 1) * n]      # in place: the CPU core points into these pages
    nv = m.nvram
    nv.data[:] = d.dec(st['nvram']['data'])
    nv.offset = d.dec(st['nvram']['offset'])
    for name, dev in devs.items():
        for k, v in d.dec(st['devices'][name]).items():
            old = getattr(dev, k, None)
            if isinstance(old, bytearray) and isinstance(v, bytearray) and len(old) == len(v):
                old[:] = v
            else:
                setattr(dev, k, v)
        after = getattr(dev, 'after_load', None)
        if after is not None:
            after()
    for k, v in st['machine'].items():
        if k in MACHINE_FIELDS:
            setattr(m, k, d.dec(v))
    m.slave.ntsc = m.ntsc
    res = m._resampler
    res.pos = d.dec(st['resampler']['pos'])
    res.last = d.dec(st['resampler']['last'])
    ns = m.cpu.ns
    R = ns['R']
    for i, v in enumerate(st['cpu']['R']):
        R[i] = v
    for k in CPU_FIELDS:
        ns[k] = st['cpu'][k]
    # the pointer: keep the live input, and take it as the last position the
    # SLAVE saw, so loading does not look like a jump of the pointer
    sl = m.slave
    sl.input_x, sl.input_y, sl.input_buttons = live_input
    if sl.last_x is not None:
        sl.last_x, sl.last_y = live_input[0] & 0xffff, live_input[1] & 0xffff
    m.audio_chunks.clear()
    m.frame = m.video.frame


# ----- files ----------------------------------------------------------------------------------
def _thumbnail(frame):
    if frame is None:
        return 0, 0, b''
    t = np.ascontiguousarray(frame[::THUMB_STEP, ::THUMB_STEP])
    h, w = t.shape
    rgb = np.empty((h, w, 3), dtype=np.uint8)
    rgb[..., 0] = t >> 16
    rgb[..., 1] = t >> 8
    rgb[..., 2] = t
    return w, h, zlib.compress(rgb.tobytes(), 6)


def save(m, path):
    """Write machine m's state to path (atomically).  Returns the meta dict."""
    st, blobs = capture(m)
    meta = {
        'format': FORMAT,
        'version': __version__,
        'time': time.time(),
        'disc': m.disc.path.name if m.disc is not None else None,
        'disc_id': disc_id(m.disc),
        'rom_crc': zlib.crc32(m.rom),
        'rom_name': m.rom_name,
        'ntsc': m.ntsc,
    }
    tw, th, thumb = _thumbnail(m.frame)
    meta['thumb'] = [tw, th]
    head = json.dumps(st, separators=(',', ':')).encode()
    body = bytearray(_U32.pack(len(head)) + head)
    for b in blobs:
        body += _U32.pack(len(b)) + b
    meta_b = json.dumps(meta).encode()
    out = (MAGIC + _U32.pack(len(meta_b)) + meta_b + _U32.pack(len(thumb)) + thumb +
           zlib.compress(bytes(body), 6))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(out)
    os.replace(tmp, path)
    dlog.info('state saved to %s (%d KiB)', path, len(out) // 1024)
    return meta


def _read_head(data, path):
    if not data.startswith(MAGIC):
        raise StateError(f'{Path(path).name} is not an OpenCDi state file')
    o = len(MAGIC)
    try:
        (n,) = _U32.unpack_from(data, o)
        meta = json.loads(data[o + 4:o + 4 + n])
        o += 4 + n
        (n,) = _U32.unpack_from(data, o)
        thumb = data[o + 4:o + 4 + n]
        o += 4 + n
    except (struct.error, ValueError) as e:
        raise StateError(f'{Path(path).name} is damaged ({e})') from None
    if not isinstance(meta, dict):
        raise StateError(f'{Path(path).name} is damaged')
    return meta, thumb, o


def read_meta(path):
    """(meta, thumbnail as (w, h, RGB bytes) or None) of a state file, or None if it cannot be read."""
    try:
        with open(path, 'rb') as f:
            data = f.read(1 << 20)
        meta, thumb, _ = _read_head(data, path)
        w, h = meta.get('thumb', (0, 0))
        img = None
        if w and h:
            rgb = zlib.decompress(thumb)
            if len(rgb) == w * h * 3:
                img = (w, h, rgb)
        return meta, img
    except (OSError, StateError, zlib.error, ValueError, TypeError):
        return None


def load(m, path):
    """Load the state in path into machine m.  Raises StateError with a message for the user."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise StateError(f'cannot read {path.name}: {e.strerror or e}') from None
    meta, _thumb, o = _read_head(data, path)
    if meta.get('format') != FORMAT:
        raise StateError(f'{path.name} was saved by OpenCDi {meta.get("version", "?")} in a format '
                         f'this version cannot read')
    if meta.get('rom_crc') != zlib.crc32(m.rom):
        raise StateError(f'this state was saved with another BIOS ({meta.get("rom_name", "?")})')
    if meta.get('disc_id') != disc_id(m.disc):
        raise StateError(f'this state was saved with another disc ({meta.get("disc") or "no disc"})')
    try:
        body = zlib.decompress(data[o:])
        (n,) = _U32.unpack_from(body, 0)
        st = json.loads(body[4:4 + n])
        blobs = []
        p = 4 + n
        while p < len(body):
            (k,) = _U32.unpack_from(body, p)
            if p + 4 + k > len(body):
                raise ValueError('truncated')
            blobs.append(body[p + 4:p + 4 + k])
            p += 4 + k
    except (zlib.error, struct.error, ValueError) as e:
        raise StateError(f'{path.name} is damaged ({e})') from None
    backup = capture(m)
    try:
        restore(m, st, blobs)
    except (StateError, KeyError, TypeError, ValueError, IndexError, AttributeError) as e:
        # the machine is half restored: put back what it was running
        restore(m, *backup)
        if isinstance(e, StateError):
            raise
        raise StateError(f'{path.name} is damaged ({type(e).__name__}: {e})') from None
    dlog.info('state loaded from %s (saved by %s, %s)', path, meta.get('version'),
              time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(meta.get('time', 0))))
    return meta


def list_slots(did):
    """[(slot, path, meta or None, thumbnail or None)] for slots 1..SLOTS of disc id did."""
    out = []
    for slot in range(1, SLOTS + 1):
        p = slot_path(did, slot)
        info = read_meta(p) if p.exists() else None
        out.append((slot, p, info[0] if info else None, info[1] if info else None))
    return out


def describe(meta):
    """'2026-10-25 14:03' for the launcher."""
    if not meta:
        return 'empty'
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.get('time', 0)))
