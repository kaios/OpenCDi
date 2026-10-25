"""Touch screen controls for the CD-i pointer (Android).

* drag anywhere outside the buttons: moves the pointer, like a laptop touchpad
* a short tap there: a click of button 1
* on-screen buttons 1 and 2 (hold them as long as you like; press both for
  "both buttons"), and MENU (the launcher, like Esc)

The buttons sit in the black margins beside the picture when the screen is
wide enough (phones), otherwise they are drawn see-through over the picture.
Coordinates are in the window's logical pixels (pygame.SCALED).
"""

import time

import pygame

SPEED = 1.0              # pointer units per logical pixel dragged
TAP_TIME = 0.25          # seconds: shorter touches that hardly moved are clicks
TAP_MOVE = 12            # logical pixels
CLICK_FRAMES = 6         # how long a tap holds button 1 (emulated frames)
MARGIN_MIN = 110         # side margin needed to put the buttons beside the picture


class Button:
    def __init__(self, name, label, center, radius):
        self.name, self.label, self.center, self.radius = name, label, center, radius

    def hit(self, pos):
        dx, dy = pos[0] - self.center[0], pos[1] - self.center[1]
        return dx * dx + dy * dy <= (self.radius * 1.15) ** 2


class TouchControls:
    def __init__(self):
        self.size = None
        self.buttons = []
        self.fingers = {}        # finger id -> ('pad', start_time, start_pos, moved) or ('button', name)
        self.dx = 0.0
        self.dy = 0.0
        self.click = 0           # frames left of a tap's button 1 press
        self.menu = False        # MENU pressed (taken by the emulator screen)
        self.changed = True      # the overlay must be drawn again
        self._font = None
        self._cache = {}

    # ----- layout -------------------------------------------------------------
    def layout(self, size, picture):
        """size: (w, h) of the window; picture: Rect of the CD-i picture in it."""
        key = (tuple(size), tuple(picture))
        if key == self.size:
            return
        self.size = key
        w, h = size
        right = w - picture.right
        left = picture.left
        if right >= MARGIN_MIN:
            r = int(min(right * 0.40, h * 0.14))
            cx = w - right // 2
            self.buttons = [Button('b1', '1', (cx, int(h * 0.74)), r),
                            Button('b2', '2', (cx, int(h * 0.40)), r)]
        else:
            r = int(h * 0.09)
            self.buttons = [Button('b1', '1', (w - int(r * 1.4), h - int(r * 1.4)), r),
                            Button('b2', '2', (w - int(r * 1.4), h - int(r * 3.9)), r)]
        mr = int(h * 0.055)
        if left >= MARGIN_MIN * 0.7:
            menu_c = (left // 2, int(h * 0.10))
        else:
            menu_c = (int(mr * 1.6), int(mr * 1.6))
        self.buttons.append(Button('menu', 'MENU', menu_c, mr))
        self._cache.clear()
        self.changed = True

    # ----- events (main thread) -----------------------------------------------
    def handle_event(self, ev):
        """FINGER* events; returns True when used."""
        if ev.type not in (pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION) or self.size is None:
            return False
        w, h = self.size[0]
        pos = (ev.x * w, ev.y * h)
        fid = ev.finger_id
        if ev.type == pygame.FINGERDOWN:
            for b in self.buttons:
                if b.hit(pos):
                    self.fingers[fid] = ('button', b.name)
                    if b.name == 'menu':
                        self.menu = True
                    self.changed = True
                    return True
            self.fingers[fid] = ('pad', time.monotonic(), pos, 0.0)
            return True
        f = self.fingers.get(fid)
        if f is None:
            return True
        if ev.type == pygame.FINGERMOTION:
            if f[0] == 'pad':
                mx, my = ev.dx * w, ev.dy * h
                self.dx += mx * SPEED
                self.dy += my * SPEED
                self.fingers[fid] = ('pad', f[1], f[2], f[3] + abs(mx) + abs(my))
            return True
        # FINGERUP
        del self.fingers[fid]
        if f[0] == 'pad':
            if time.monotonic() - f[1] < TAP_TIME and f[3] < TAP_MOVE:
                self.click = CLICK_FRAMES
        else:
            self.changed = True
        return True

    def pressed(self, name):
        return any(f[0] == 'button' and f[1] == name for f in self.fingers.values())

    # ----- per emulated frame (emulation thread, under the input lock) ----------
    def take(self):
        """(dx, dy, buttons) since the last call."""
        dx, dy = int(self.dx), int(self.dy)
        self.dx -= dx
        self.dy -= dy
        b = 0
        if self.pressed('b1') or self.click:
            b |= 1
        if self.pressed('b2'):
            b |= 2
        if self.click:
            self.click -= 1
        return dx, dy, b

    def take_menu(self):
        m, self.menu = self.menu, False
        return m

    # ----- drawing (main thread) -------------------------------------------------
    def draw(self, surface):
        if self._font is None:
            self._font = pygame.font.Font(None, 40)
        for b in self.buttons:
            down = self.pressed(b.name)
            key = (b.name, down)
            img = self._cache.get(key)
            if img is None:
                r = b.radius
                img = pygame.Surface((2 * r + 4, 2 * r + 4), pygame.SRCALPHA)
                fill = (230, 230, 240, 150) if down else (200, 200, 215, 70)
                pygame.draw.circle(img, fill, (r + 2, r + 2), r)
                pygame.draw.circle(img, (240, 240, 250, 190), (r + 2, r + 2), r, 3)
                font = self._font if len(b.label) == 1 else pygame.font.Font(None, max(14, r * 2 // 3))
                t = font.render(b.label, True, (255, 255, 255))
                img.blit(t, t.get_rect(center=(r + 2, r + 2)))
                self._cache[key] = img
            surface.blit(img, img.get_rect(center=b.center))
        self.changed = False
