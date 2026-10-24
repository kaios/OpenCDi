"""Debug log written to ~/.opencdi/debug.log.

Warnings (emulator faults, unexpected guest behaviour) are always written.
Detailed per-event messages are only written when the emulator is started
with --debug.  Every code path changed in 0.1.1 reports here; see
docs/debug/0.1.1.md for what each message means.
"""

import logging
from pathlib import Path

DEFAULT_PATH = Path.home() / '.opencdi' / 'debug.log'

log = logging.getLogger('opencdi')
log.addHandler(logging.NullHandler())
log.propagate = False


def setup(debug=False, path=DEFAULT_PATH):
    """Attach the log file; returns its path (None if it cannot be written)."""
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode='w', encoding='utf-8')
    except OSError as e:
        print(f'could not open debug log {path}: {e}')
        log.addHandler(logging.NullHandler())
        return None
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(name)s: %(message)s'))
    log.addHandler(handler)
    return path


def get(name):
    return log.getChild(name)
