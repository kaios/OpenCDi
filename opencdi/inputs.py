"""Translate keyboard, mouse and any game controller into the CD-i pointer.

CD-i players use a pointing device (thumbpad, mouse, joystick) with two
buttons; the SLAVE chip reports relative motion.  Every input source here adds
motion to one shared pointer:

* game controllers: left/right stick, d-pad; A/Cross/Y = button 1,
  B/Circle/X = button 2, Start = both, shoulders = fast pointer
  (SDL's controller database maps Xbox, PlayStation, Switch and most other
  pads; anything else falls back to raw joystick axes/hats/buttons)
* keyboard: arrows or WASD to move, Z/Space/J = button 1, X/K = button 2,
  C/L = both, Shift = fast
* mouse: click into the window to capture it, F10 releases it
* touch screen (Android): see touch.py
"""

import math

import pygame

try:
    from pygame._sdl2 import controller as sdl_controller
except ImportError:  # pragma: no cover - very old pygame
    sdl_controller = None

DEADZONE = 0.18
STICK_SPEED = 9.0      # pointer pixels per frame at full deflection
DPAD_MIN, DPAD_MAX = 2.0, 10.0
FAST = 2.0


class Pad:
    def __init__(self, index):
        self.index = index
        self.ctrl = None
        self.joy = None
        if sdl_controller is not None:
            try:
                if sdl_controller.is_controller(index):
                    self.ctrl = sdl_controller.Controller(index)
            except Exception:  # noqa: BLE001
                self.ctrl = None
        if self.ctrl is None:
            self.joy = pygame.joystick.Joystick(index)
            self.joy.init()
        self.name = self.ctrl.name if self.ctrl else self.joy.get_name()

    def instance_id(self):
        if self.ctrl:
            return self.ctrl.as_joystick().get_instance_id()
        return self.joy.get_instance_id()

    def read(self):
        """Returns (sx, sy, dpad_x, dpad_y, b1, b2, fast, menu)."""
        if self.ctrl:
            c = self.ctrl
            ax = c.get_axis(pygame.CONTROLLER_AXIS_LEFTX) / 32768.0
            ay = c.get_axis(pygame.CONTROLLER_AXIS_LEFTY) / 32768.0
            rx = c.get_axis(pygame.CONTROLLER_AXIS_RIGHTX) / 32768.0
            ry = c.get_axis(pygame.CONTROLLER_AXIS_RIGHTY) / 32768.0
            if math.hypot(rx, ry) > math.hypot(ax, ay):
                ax, ay = rx, ry
            b = c.get_button
            dx = b(pygame.CONTROLLER_BUTTON_DPAD_RIGHT) - b(pygame.CONTROLLER_BUTTON_DPAD_LEFT)
            dy = b(pygame.CONTROLLER_BUTTON_DPAD_DOWN) - b(pygame.CONTROLLER_BUTTON_DPAD_UP)
            start = b(pygame.CONTROLLER_BUTTON_START)
            b1 = b(pygame.CONTROLLER_BUTTON_A) or b(pygame.CONTROLLER_BUTTON_Y) or start
            b2 = b(pygame.CONTROLLER_BUTTON_B) or b(pygame.CONTROLLER_BUTTON_X) or start
            trig = max(c.get_axis(pygame.CONTROLLER_AXIS_TRIGGERLEFT), c.get_axis(pygame.CONTROLLER_AXIS_TRIGGERRIGHT))
            fast = b(pygame.CONTROLLER_BUTTON_LEFTSHOULDER) or b(pygame.CONTROLLER_BUTTON_RIGHTSHOULDER) or trig > 16000
            menu = b(pygame.CONTROLLER_BUTTON_BACK) or b(pygame.CONTROLLER_BUTTON_GUIDE)
            return ax, ay, dx, dy, bool(b1), bool(b2), bool(fast), bool(menu)
        j = self.joy
        na = j.get_numaxes()
        ax = j.get_axis(0) if na > 0 else 0.0
        ay = j.get_axis(1) if na > 1 else 0.0
        if na > 3:
            rx, ry = j.get_axis(2), j.get_axis(3)
            if math.hypot(rx, ry) > math.hypot(ax, ay):
                ax, ay = rx, ry
        dx = dy = 0
        if j.get_numhats():
            hx, hy = j.get_hat(0)
            dx, dy = hx, -hy
        nb = j.get_numbuttons()

        def btn(i):
            return i < nb and j.get_button(i)
        b1 = btn(0) or btn(3) or btn(9)
        b2 = btn(1) or btn(2) or btn(9)
        fast = btn(4) or btn(5) or btn(6) or btn(7)
        return ax, ay, dx, dy, bool(b1), bool(b2), bool(fast), bool(btn(8))


class InputManager:
    def __init__(self, touch=False):
        if touch:
            from .touch import TouchControls
            self.touch = TouchControls()
        else:
            self.touch = None
        self.x = 0
        self.y = 0
        self.buttons = 0
        self.pads = {}
        self.mouse_captured = False
        self.mouse_dx = 0
        self.mouse_dy = 0
        self.mouse_buttons = 0
        self.dpad_hold = 0
        self.frac_x = 0.0
        self.frac_y = 0.0
        pygame.joystick.init()
        if sdl_controller is not None:
            try:
                sdl_controller.init()
            except Exception:  # noqa: BLE001
                pass
        self.rescan()

    def rescan(self):
        self.pads = {}
        for i in range(pygame.joystick.get_count()):
            try:
                pad = Pad(i)
                self.pads[pad.instance_id()] = pad
            except pygame.error:
                continue

    def describe(self):
        if not self.pads:
            return 'no game controllers found (keyboard and mouse active)'
        return 'controllers: ' + ', '.join(p.name for p in self.pads.values())

    def handle_event(self, ev):
        """Returns True when the event was consumed."""
        t = ev.type
        if t in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
            self.rescan()
            return True
        if self.touch is not None:
            if self.touch.handle_event(ev):
                return True
            if t in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION) and getattr(ev, 'touch', False):
                return True   # SDL's mouse copy of a touch: the touch controls have it
        if t == pygame.MOUSEBUTTONDOWN:
            if ev.button > 3:
                return True   # wheel
            if not self.mouse_captured:
                self.capture_mouse(True)
                return True
            if ev.button == 1:
                self.mouse_buttons |= 1
            elif ev.button == 3:
                self.mouse_buttons |= 2
            elif ev.button == 2:
                self.mouse_buttons |= 3
            return True
        if t == pygame.MOUSEBUTTONUP:
            if ev.button == 1:
                self.mouse_buttons &= ~1
            elif ev.button == 3:
                self.mouse_buttons &= ~2
            elif ev.button == 2:
                self.mouse_buttons &= ~3
            return True
        if t == pygame.MOUSEMOTION and self.mouse_captured:
            self.mouse_dx += ev.rel[0]
            self.mouse_dy += ev.rel[1]
            return True
        if t == pygame.KEYDOWN and ev.key == pygame.K_F10:
            self.capture_mouse(False)
            return True
        return False

    def capture_mouse(self, on):
        self.mouse_captured = on
        pygame.event.set_grab(on)
        pygame.mouse.set_visible(not on)
        if on:
            pygame.mouse.get_rel()

    def pad_state(self):
        """Combined controller state for menu navigation: (x, y, b1, b2, menu) with x, y in -1..1."""
        x = y = 0
        b1 = b2 = menu = False
        for pad in list(self.pads.values()):
            try:
                ax, ay, dx, dy, pb1, pb2, _fast, pmenu = pad.read()
            except pygame.error:
                continue
            x = x or dx or (round(ax) if abs(ax) > 0.6 else 0)
            y = y or dy or (round(ay) if abs(ay) > 0.6 else 0)
            b1 |= pb1
            b2 |= pb2
            menu |= pmenu
        return x, y, b1, b2, menu

    def update(self):
        """Poll everything once per frame; returns (x, y, buttons)."""
        keys = pygame.key.get_pressed()
        kx = (keys[pygame.K_RIGHT] or keys[pygame.K_d]) - (keys[pygame.K_LEFT] or keys[pygame.K_a])
        ky = (keys[pygame.K_DOWN] or keys[pygame.K_s]) - (keys[pygame.K_UP] or keys[pygame.K_w])
        kb1 = keys[pygame.K_z] or keys[pygame.K_SPACE] or keys[pygame.K_j] or keys[pygame.K_c] or keys[pygame.K_l]
        kb2 = keys[pygame.K_x] or keys[pygame.K_k] or keys[pygame.K_c] or keys[pygame.K_l]
        kfast = keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]

        sx = sy = 0.0
        dx, dy = kx, ky
        b1, b2, fast = bool(kb1), bool(kb2), bool(kfast)
        for pad in list(self.pads.values()):
            try:
                ax, ay, pdx, pdy, pb1, pb2, pfast, _menu = pad.read()
            except pygame.error:
                continue
            if abs(ax) > abs(sx):
                sx = ax
            if abs(ay) > abs(sy):
                sy = ay
            dx = dx or pdx
            dy = dy or pdy
            b1 |= pb1
            b2 |= pb2
            fast |= pfast

        mx = my = 0.0
        mag = math.hypot(sx, sy)
        if mag > DEADZONE:
            scale = (mag - DEADZONE) / (1 - DEADZONE)
            scale = scale * scale * STICK_SPEED / mag
            mx += sx * scale
            my += sy * scale
        if dx or dy:
            self.dpad_hold += 1
            speed = min(DPAD_MAX, DPAD_MIN + self.dpad_hold * 0.4)
            mx += dx * speed
            my += dy * speed
        else:
            self.dpad_hold = 0
        if fast:
            mx *= FAST
            my *= FAST
        self.frac_x += mx
        self.frac_y += my
        ix, iy = int(self.frac_x), int(self.frac_y)
        self.frac_x -= ix
        self.frac_y -= iy
        tx = ty = tb = 0
        if self.touch is not None:
            tx, ty, tb = self.touch.take()
        self.x += ix + self.mouse_dx + tx
        self.y += iy + self.mouse_dy + ty
        self.mouse_dx = self.mouse_dy = 0
        buttons = (1 if b1 else 0) | (2 if b2 else 0) | self.mouse_buttons | tb
        if buttons & 3 == 3:
            buttons |= 4
        self.buttons = buttons
        return self.x & 0xffff, self.y & 0xffff, buttons
