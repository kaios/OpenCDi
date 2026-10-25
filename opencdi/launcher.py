"""Launcher screen: choose the system ROM (BIOS) and disc image, then start.

Everything is drawn with pygame (no tkinter needed) and works with the mouse,
the keyboard or a game controller:
    up/down (arrows, d-pad, stick)  move      Enter / A / click   select
    Backspace / B                   back      Esc                 back/cancel
"""

import json
import os
import string
import time
from pathlib import Path

import pygame

from . import android_support, savestate
from .frontend import BASE_SIZE
from .machine import check_bios

BIOS_URL = 'https://archive.org/download/philips-cd-i-misc-bios'
CONFIG_PATH = Path.home() / '.opencdi' / 'config.json'
BIOS_EXT = {'.rom', '.bin'}
DISC_EXT = {'.cue', '.iso', '.bin', '.img', '.chd'}

BG = (24, 26, 38)
PANEL = (38, 42, 60)
HILITE = (70, 92, 160)
TEXT = (230, 232, 240)
DIM = (150, 155, 175)
GOOD = (120, 210, 130)
BAD = (235, 110, 100)
WARN = (235, 200, 100)


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def use_ntsc(cfg):
    """NTSC (60 Hz) unless PAL was chosen; 0.1.3 and earlier defaulted to PAL (50 Hz)."""
    return bool(cfg.get('ntsc', True))


def save_config(cfg):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError as e:
        print(f'could not save settings: {e}')


class NavInput:
    """Turns keyboard and controller input into discrete menu actions."""

    def __init__(self, inp):
        self.inp = inp
        self.held = {}

    def poll_pad(self):
        x, y, b1, b2, menu = self.inp.pad_state()
        actions = []
        now = time.monotonic()
        for name, active in (('up', y < 0), ('down', y > 0), ('left', x < 0), ('right', x > 0),
                             ('select', b1), ('back', b2 or menu)):
            t = self.held.get(name)
            if not active:
                self.held.pop(name, None)
                continue
            if t is None:
                # first poll after the screen opens: ignore buttons that are already down
                self.held[name] = now + (0.4 if name in ('up', 'down', 'left', 'right') else 1e9)
                if self._armed:
                    actions.append(name)
            elif now >= t:
                self.held[name] = now + 0.12
                actions.append(name)
        self._armed = True
        return actions

    _armed = False

    @staticmethod
    def key_action(ev):
        return {
            pygame.K_UP: 'up', pygame.K_DOWN: 'down', pygame.K_LEFT: 'left', pygame.K_RIGHT: 'right',
            pygame.K_RETURN: 'select', pygame.K_KP_ENTER: 'select', pygame.K_SPACE: 'select',
            pygame.K_ESCAPE: 'back', pygame.K_BACKSPACE: 'up_dir',
            pygame.K_PAGEUP: 'pgup', pygame.K_PAGEDOWN: 'pgdn', pygame.K_HOME: 'home', pygame.K_END: 'end',
            pygame.K_TAB: 'toggle', pygame.K_F11: 'fullscreen',
            getattr(pygame, 'K_AC_BACK', -1): 'back',      # Android back button
        }.get(ev.key)


class Screen:
    """Draws onto a fixed 768x560 canvas that is scaled to the window."""

    def __init__(self, app):
        self.app = app
        app.set_content_size(BASE_SIZE)
        self.canvas = pygame.Surface(BASE_SIZE)
        self.font = pygame.font.Font(None, 26)
        self.small = pygame.font.Font(None, 21)
        self.big = pygame.font.Font(None, 48)
        self.offset = (0, 0)
        self.k = 1.0

    def present(self):
        window = self.app.window
        ww, wh = window.get_size()
        k = min(ww / BASE_SIZE[0], wh / BASE_SIZE[1])
        tw, th = int(BASE_SIZE[0] * k), int(BASE_SIZE[1] * k)
        self.k = k
        self.offset = ((ww - tw) // 2, (wh - th) // 2)
        window.fill((0, 0, 0))
        window.blit(pygame.transform.smoothscale(self.canvas, (tw, th)), self.offset)
        pygame.display.flip()

    def to_canvas(self, pos):
        return ((pos[0] - self.offset[0]) / self.k, (pos[1] - self.offset[1]) / self.k)

    def text(self, s, pos, color=TEXT, font=None, maxw=None):
        font = font or self.font
        if maxw is not None:
            s = fit(font, s, maxw)
        img = font.render(s, True, color)
        self.canvas.blit(img, pos)
        return img.get_width()


def fit(font, s, maxw):
    """Shorten s from the left (keeping the file name) until it fits maxw pixels."""
    if font.size(s)[0] <= maxw:
        return s
    while s and font.size('...' + s)[0] > maxw:
        s = s[1:]
    return '...' + s


class FileBrowser:
    def __init__(self, screen, nav, title, start_dir, extensions, describe=None):
        self.screen = screen
        self.nav = nav
        self.title = title
        self.ext = extensions
        self.describe = describe
        self.show_all = False
        default = android_support.browse_start() or Path.home()
        self.cwd = Path(start_dir) if start_dir and Path(start_dir).is_dir() else default
        self.sel = 0
        self.top = 0
        self.rows = 15
        self.last_click = (0, -1)
        self.drag = 0.0             # touch scrolling, in canvas pixels not yet scrolled
        self.info_cache = {}
        self.load()

    def load(self, select_name=None):
        entries = []
        if self.cwd is None:  # drive list on Windows
            for letter in string.ascii_uppercase:
                d = Path(f'{letter}:\\')
                if d.exists():
                    entries.append((str(d), d, True))
        else:
            entries.append(('..', None, True))
            try:
                items = sorted(self.cwd.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                items = []
            for p in items:
                if p.name.startswith('.'):
                    continue
                try:
                    is_dir = p.is_dir()
                except OSError:
                    continue
                if is_dir or self.show_all or p.suffix.lower() in self.ext:
                    entries.append((p.name + ('/' if is_dir else ''), p, is_dir))
        self.entries = entries
        self.sel = 0
        if select_name:
            for i, (name, p, _d) in enumerate(entries):
                if p is not None and p.name == select_name:
                    self.sel = i
        self.top = max(0, self.sel - self.rows // 2)

    def go_up(self):
        if self.cwd is None:
            return
        parent = self.cwd.parent
        if parent == self.cwd:
            if os.name == 'nt':
                self.cwd = None
                self.load()
            return
        old = self.cwd.name
        self.cwd = parent
        self.load(select_name=old)

    def activate(self):
        if not self.entries:
            return None
        name, p, is_dir = self.entries[self.sel]
        if p is None:
            self.go_up()
            return None
        if is_dir:
            self.cwd = p
            self.load()
            return None
        return p

    def move(self, d):
        if not self.entries:
            return
        self.sel = max(0, min(len(self.entries) - 1, self.sel + d))
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + self.rows:
            self.top = self.sel - self.rows + 1

    def run(self):
        """Returns the chosen Path or None when cancelled."""
        clock = pygame.time.Clock()
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    pygame.event.post(pygame.event.Event(pygame.QUIT))
                    return None
                if ev.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                    self.screen.app.input.rescan()
                elif ev.type == pygame.KEYDOWN:
                    a = NavInput.key_action(ev)
                    if a == 'back':
                        return None
                    if a == 'fullscreen':
                        self.screen.app.toggle_fullscreen()
                    elif a == 'up_dir' or a == 'left':
                        self.go_up()
                    elif a in ('select', 'right'):
                        r = self.activate()
                        if r is not None:
                            return r
                    elif a in ('up', 'down'):
                        self.move(-1 if a == 'up' else 1)
                    elif a in ('pgup', 'pgdn'):
                        self.move(-self.rows if a == 'pgup' else self.rows)
                    elif a == 'home':
                        self.move(-10 ** 6)
                    elif a == 'end':
                        self.move(10 ** 6)
                    elif a == 'toggle':
                        self.show_all = not self.show_all
                        self.load()
                elif ev.type == pygame.MOUSEWHEEL:
                    self.top = max(0, min(max(0, len(self.entries) - self.rows), self.top - ev.y * 3))
                elif ev.type == pygame.MOUSEMOTION and getattr(ev, 'touch', False) and ev.buttons[0]:
                    # touch: drag the list to scroll it
                    self.drag += ev.rel[1] / max(self.screen.k, 0.01)
                    rows = int(self.drag / 26)
                    self.drag -= rows * 26
                    self.top = max(0, min(max(0, len(self.entries) - self.rows), self.top - rows))
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    x, y = self.screen.to_canvas(ev.pos)
                    if 20 <= x <= 748 and 82 <= y < 82 + self.rows * 26:
                        i = self.top + int((y - 82) // 26)
                        if i < len(self.entries):
                            now = time.monotonic()
                            double = self.last_click[1] == i and now - self.last_click[0] < 0.4
                            if getattr(ev, 'touch', False) and self.sel == i:
                                double = True      # touch: tapping the selected entry opens it
                            self.sel = i
                            self.last_click = (now, i)
                            if double:
                                r = self.activate()
                                if r is not None:
                                    return r
                    elif 600 <= x <= 748 and 500 <= y <= 536:
                        return None
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3:
                    self.go_up()
            for a in self.nav.poll_pad():
                if a in ('up', 'down'):
                    self.move(-1 if a == 'up' else 1)
                elif a in ('left',):
                    self.go_up()
                elif a in ('select', 'right'):
                    r = self.activate()
                    if r is not None:
                        return r
                elif a == 'back':
                    if self.cwd is not None and self.cwd.parent != self.cwd:
                        self.go_up()
                    else:
                        return None
            self.draw()
            clock.tick(30)

    def draw(self):
        sc = self.screen
        c = sc.canvas
        c.fill(BG)
        sc.text(self.title, (20, 14), TEXT, sc.big)
        sc.text(str(self.cwd) if self.cwd else 'Drives', (20, 56), DIM, sc.small, maxw=728)
        pygame.draw.rect(c, PANEL, (16, 78, 736, self.rows * 26 + 8), border_radius=6)
        for row in range(self.rows):
            i = self.top + row
            if i >= len(self.entries):
                break
            name, p, is_dir = self.entries[i]
            y = 82 + row * 26
            if i == self.sel:
                pygame.draw.rect(c, HILITE, (20, y, 728, 26), border_radius=4)
            sc.text(name, (30, y + 4), TEXT if is_dir or p is None else (200, 220, 255), maxw=560)
            if p is not None and not is_dir:
                try:
                    size = p.stat().st_size
                    sc.text(human(size), (620, y + 5), DIM, sc.small)
                except OSError:
                    pass
        # info about the highlighted file
        info = ''
        color = DIM
        if self.describe and self.entries:
            name, p, is_dir = self.entries[self.sel]
            if p is not None and not is_dir:
                if p not in self.info_cache:
                    self.info_cache[p] = self.describe(p)
                ok, info = self.info_cache[p]
                color = GOOD if ok else BAD
        sc.text(info, (20, 480), color, sc.small, maxw=728)
        filt = 'all files' if self.show_all else ' '.join(sorted(self.ext))
        sc.text(f'Showing: {filt}   (Tab: toggle)', (20, 506), DIM, sc.small)
        sc.text('Enter/A/double-click: open   Backspace/B/right-click: up   Esc: cancel', (20, 530), DIM, sc.small)
        pygame.draw.rect(c, PANEL, (600, 500, 148, 30), border_radius=6)
        sc.text('Cancel', (645, 506), TEXT, sc.small)
        sc.present()


class SlotScreen:
    """Pick a save state slot (1-9), with the picture of the highlighted one."""

    ROW_Y, ROW_H = 82, 40

    def __init__(self, screen, nav, title, slots, saving):
        self.screen = screen
        self.nav = nav
        self.title = title
        self.slots = slots            # from savestate.list_slots()
        self.saving = saving
        self.sel = 0
        self.note = ''
        self.thumbs = {}
        # start on the newest state when loading, on the first empty slot when saving
        used = [i for i, sl in enumerate(slots) if sl[2]]
        if saving:
            free = [i for i, sl in enumerate(slots) if not sl[2]]
            self.sel = free[0] if free else 0
        elif used:
            self.sel = max(used, key=lambda i: slots[i][2].get('time', 0))

    def row_rect(self, i):
        return pygame.Rect(20, self.ROW_Y + i * self.ROW_H, 330, self.ROW_H - 6)

    def activate(self):
        slot, _path, meta, _thumb = self.slots[self.sel]
        if not self.saving and not meta:
            self.note = f'Slot {slot} is empty.'
            return None
        return slot

    def run(self):
        """Returns the chosen slot number or None."""
        clock = pygame.time.Clock()
        n = len(self.slots)
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    pygame.event.post(pygame.event.Event(pygame.QUIT))
                    return None
                if ev.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                    self.screen.app.input.rescan()
                elif ev.type == pygame.KEYDOWN:
                    a = NavInput.key_action(ev)
                    if a in ('back', 'up_dir'):
                        return None
                    if a == 'fullscreen':
                        self.screen.app.toggle_fullscreen()
                    elif a in ('up', 'down'):
                        self.sel = (self.sel + (-1 if a == 'up' else 1)) % n
                        self.note = ''
                    elif a in ('select', 'right'):
                        r = self.activate()
                        if r is not None:
                            return r
                    elif pygame.K_1 <= ev.key <= pygame.K_9 and ev.key - pygame.K_1 < n:
                        self.sel = ev.key - pygame.K_1
                        self.note = ''
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    pos = self.screen.to_canvas(ev.pos)
                    for i in range(n):
                        if self.row_rect(i).collidepoint(pos):
                            # a click selects, a click on the selected slot chooses it
                            if i == self.sel:
                                r = self.activate()
                                if r is not None:
                                    return r
                            else:
                                self.sel = i
                                self.note = ''
                    if pygame.Rect(600, 500, 148, 30).collidepoint(pos):
                        return None
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3:
                    return None
            for a in self.nav.poll_pad():
                if a in ('up', 'down'):
                    self.sel = (self.sel + (-1 if a == 'up' else 1)) % n
                    self.note = ''
                elif a in ('select', 'right'):
                    r = self.activate()
                    if r is not None:
                        return r
                elif a in ('back', 'left'):
                    return None
            self.draw()
            clock.tick(30)

    def thumb(self, i):
        if i not in self.thumbs:
            img = None
            t = self.slots[i][3]
            if t is not None:
                w, h, rgb = t
                img = pygame.transform.smoothscale(pygame.image.frombuffer(rgb, (w, h), 'RGB'), (384, 240))
            self.thumbs[i] = img
        return self.thumbs[i]

    def draw(self):
        sc = self.screen
        c = sc.canvas
        c.fill(BG)
        sc.text(self.title, (20, 14), TEXT, sc.big)
        for i, (slot, _path, meta, _t) in enumerate(self.slots):
            r = self.row_rect(i)
            pygame.draw.rect(c, HILITE if i == self.sel else PANEL, r, border_radius=6)
            sc.text(f'{slot}', (r.x + 12, r.y + 9), TEXT)
            sc.text(savestate.describe(meta), (r.x + 44, r.y + 9), TEXT if meta else DIM, maxw=r.w - 56)
        box = pygame.Rect(364, self.ROW_Y, 384, 240)
        pygame.draw.rect(c, PANEL, box)
        img = self.thumb(self.sel)
        slot, _path, meta, _t = self.slots[self.sel]
        if img is not None:
            c.blit(img, box)
        else:
            sc.text('empty' if not meta else 'no picture', (box.x + 150, box.y + 110), DIM)
        y = box.bottom + 14
        if meta:
            ntsc = 'NTSC' if meta.get('ntsc') else 'PAL'
            sc.text(meta.get('disc') or 'no disc', (364, y), TEXT, sc.small, maxw=384)
            sc.text(f'{meta.get("rom_name", "")}, {ntsc}', (364, y + 22), DIM, sc.small, maxw=384)
            sc.text(f'saved by OpenCDi {meta.get("version", "?")}', (364, y + 44), DIM, sc.small, maxw=384)
            if self.saving:
                sc.text('Saving here replaces this state.', (364, y + 70), WARN, sc.small, maxw=384)
        if self.note:
            sc.text(self.note, (20, 470), BAD, sc.small, maxw=560)
        action = 'save' if self.saving else 'load'
        sc.text(f'Enter/A or click the highlighted slot: {action}   1-9: slot   Esc/B: cancel',
                (20, 530), DIM, sc.small)
        pygame.draw.rect(c, PANEL, (600, 500, 148, 30), border_radius=6)
        sc.text('Cancel', (645, 506), TEXT, sc.small)
        sc.present()


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return str(n)


class Launcher:
    """Main menu.  run() returns one of:
        ('start', bios_path, disc_path_or_None, ntsc)
        ('resume',)
        ('save', slot)                    save the running game's state
        ('load', slot, bios_path, disc_path_or_None, ntsc, into_running)
        ('quit',)
    """

    def __init__(self, app, cfg, can_resume=False, message='', state_id=None):
        self.app = app
        self.cfg = cfg
        self.can_resume = can_resume
        self.state_id = state_id      # savestate.disc_id() of the running game's disc
        self.message = message
        self.screen = Screen(app)
        self.nav = NavInput(app.input)
        self.sel = 0
        self.bios_info = None
        self.initial = self.session_key()
        self.check_bios()

    def session_key(self):
        return self.cfg.get('bios'), self.cfg.get('disc'), use_ntsc(self.cfg)

    def check_bios(self):
        b = self.cfg.get('bios')
        if b:
            self.bios_info = check_bios(b) if Path(b).exists() else (False, 'file not found')
        else:
            self.bios_info = None

    def items(self):
        items = []
        if self.resumable():
            items.append(('resume', 'Resume'))
        items.append(('start', 'Restart' if self.resumable() else 'Start'))
        if self.resumable():
            items.append(('save', 'Save state'))
        if self.bios_ok():
            items.append(('load', 'Load state'))
        bios = self.cfg.get('bios')
        items.append(('bios', 'BIOS:  ' + (Path(bios).name if bios else '(none selected)')))
        disc = self.cfg.get('disc')
        items.append(('disc', 'Disc:  ' + (Path(disc).name if disc else '(no disc - player menu)')))
        if disc:
            items.append(('eject', 'Eject disc'))
        items.append(('region', 'Video:  ' + ('NTSC (60 Hz)' if use_ntsc(self.cfg) else 'PAL (50 Hz)')))
        items.append(('quit', 'Quit'))
        return items

    def resumable(self):
        # once BIOS, disc or region change, the running session no longer matches
        return self.can_resume and self.session_key() == self.initial

    def bios_ok(self):
        return bool(self.bios_info and self.bios_info[0])

    def choose(self, key):
        cfg = self.cfg
        if key == 'resume':
            return ('resume',)
        if key in ('save', 'load'):
            return self.choose_slot(key)
        if key == 'start':
            if not self.bios_ok():
                self.message = 'Select a working CD-i BIOS first.'
                return None
            save_config(cfg)
            return ('start', cfg['bios'], cfg.get('disc'), use_ntsc(cfg))
        if key == 'bios':
            start = cfg.get('bios_dir') or (str(Path(cfg['bios']).parent) if cfg.get('bios') else None)
            fb = FileBrowser(self.screen, self.nav, 'Select CD-i BIOS (system ROM)', start, BIOS_EXT,
                             describe=check_bios)
            p = fb.run()
            if p is not None:
                cfg['bios'] = str(p)
                cfg['bios_dir'] = str(p.parent)
                self.check_bios()
                self.message = ''
                save_config(cfg)
        elif key == 'disc':
            start = cfg.get('disc_dir') or (str(Path(cfg['disc']).parent) if cfg.get('disc') else None)
            fb = FileBrowser(self.screen, self.nav, 'Select disc image', start, DISC_EXT,
                             describe=describe_disc)
            p = fb.run()
            if p is not None:
                ok, info = describe_disc(p)
                if ok:
                    cfg['disc'] = str(p)
                    cfg['disc_dir'] = str(p.parent)
                    self.message = ''
                    save_config(cfg)
                else:
                    self.message = f'Cannot use {p.name}: {info}'
        elif key == 'eject':
            cfg.pop('disc', None)
            save_config(cfg)
        elif key == 'region':
            cfg['ntsc'] = not use_ntsc(cfg)
            save_config(cfg)
        elif key == 'quit':
            return ('quit',)
        return None

    def choose_slot(self, key):
        cfg = self.cfg
        running = self.resumable()
        did = self.state_id if running else None
        if did is None:
            try:
                did = savestate.disc_id_for_path(cfg.get('disc'))
            except Exception as e:  # noqa: BLE001 - the disc cannot be opened
                self.message = f'Cannot open the disc: {e}'
                return None
        saving = key == 'save'
        slot = SlotScreen(self.screen, self.nav, 'Save state' if saving else 'Load state',
                          savestate.list_slots(did), saving).run()
        if slot is None:
            return None
        if saving:
            return ('save', slot)
        save_config(cfg)
        return ('load', slot, cfg['bios'], cfg.get('disc'), use_ntsc(cfg), running)

    def item_rects(self, items):
        # nine items (with save states) need a tighter menu
        top, step = (150, 44) if len(items) <= 7 else (118, 38)
        return [pygame.Rect(184, top + i * step, 400, step - 8) for i in range(len(items))]

    def run(self):
        clock = pygame.time.Clock()
        if not self.bios_ok():
            self.sel = [k for k, _ in self.items()].index('bios')
        pygame.display.set_caption('OpenCDi')
        while True:
            items = self.items()
            self.sel = max(0, min(self.sel, len(items) - 1))
            rects = self.item_rects(items)
            action = None
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return ('quit',)
                if ev.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                    self.app.input.rescan()
                elif ev.type == pygame.KEYDOWN:
                    a = NavInput.key_action(ev)
                    if a == 'fullscreen':
                        self.app.toggle_fullscreen()
                    elif a == 'back' and self.resumable():
                        return ('resume',)
                    elif a in ('up', 'down'):
                        self.sel = (self.sel + (-1 if a == 'up' else 1)) % len(items)
                    elif a in ('select', 'right'):
                        action = items[self.sel][0]
                elif ev.type == pygame.MOUSEMOTION:
                    pos = self.screen.to_canvas(ev.pos)
                    for i, r in enumerate(rects):
                        if r.collidepoint(pos):
                            self.sel = i
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    pos = self.screen.to_canvas(ev.pos)
                    for i, r in enumerate(rects):
                        if r.collidepoint(pos):
                            self.sel = i
                            action = items[i][0]
            for a in self.nav.poll_pad():
                if a in ('up', 'down'):
                    self.sel = (self.sel + (-1 if a == 'up' else 1)) % len(items)
                elif a == 'select':
                    action = items[self.sel][0]
                elif a == 'back' and self.resumable():
                    return ('resume',)
            if action:
                res = self.choose(action)
                if res is not None:
                    return res
                self.nav = NavInput(self.app.input)  # swallow buttons held while leaving a sub screen
            self.draw(items, rects)
            clock.tick(30)

    def draw(self, items, rects):
        sc = self.screen
        c = sc.canvas
        c.fill(BG)
        w = sc.big.size('OpenCDi')[0]
        sc.text('OpenCDi', ((BASE_SIZE[0] - w) // 2, 40), TEXT, sc.big)
        sub = 'Philips CD-i emulator'
        sc.text(sub, ((BASE_SIZE[0] - sc.small.size(sub)[0]) // 2, 86), DIM, sc.small)
        for i, ((key, label), r) in enumerate(zip(items, rects)):
            pygame.draw.rect(c, HILITE if i == self.sel else PANEL, r, border_radius=6)
            enabled = key != 'start' or self.bios_ok()
            sc.text(label, (r.x + 14, r.y + 9), TEXT if enabled else DIM, maxw=r.w - 28)
        y = rects[-1].bottom + 18
        if self.bios_info is None:
            sc.text('A CD-i system ROM (BIOS) is required and is not included.', (40, y), WARN, sc.small)
            sc.text(f'Download it from {BIOS_URL}', (40, y + 22), WARN, sc.small)
            sc.text('then choose "BIOS" above and pick cdi220b.rom or cdi200.rom.', (40, y + 44), WARN, sc.small)
        else:
            ok, info = self.bios_info
            sc.text(('BIOS: ' if ok else 'BIOS problem: ') + info, (40, y), GOOD if ok else BAD, sc.small, maxw=690)
            if not ok:
                sc.text(f'Mono-I ROMs (cdi220b.rom, cdi200.rom) are at {BIOS_URL}', (40, y + 22), DIM, sc.small,
                        maxw=690)
        if self.message:
            sc.text(self.message, (40, 500), BAD, sc.small, maxw=690)
        sc.text('Arrows/d-pad: move   Enter/A/click: select   F11: fullscreen', (40, 530), DIM, sc.small)
        sc.present()


def describe_disc(p):
    from .disc import Disc
    try:
        d = Disc(p)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    kinds = {}
    for t in d.tracks:
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
    d.close()
    desc = ', '.join(f'{n} {k} track{"s" if n > 1 else ""}' for k, n in kinds.items())
    mins = (d.total_sectors // 75) // 60
    secs = (d.total_sectors // 75) % 60
    return True, f'{desc}, {mins}:{secs:02d}'
