"""pygame window, audio output and the emulation loop."""

import collections
import time
from pathlib import Path

import numpy as np
import pygame

from .inputs import InputManager

HELP = """OpenCDi controls
  Pointer      arrows / WASD, any controller stick or d-pad, mouse (click to capture)
  Button 1     Z / Space / J, controller A, left mouse button
  Button 2     X / K, controller B, right mouse button
  Both         C / L, controller Start, middle mouse button
  Fast pointer Shift, controller shoulder buttons
  Esc / F2 / controller Back: menu (BIOS, disc, quit)
  F1 help   F3 reset   F10 release mouse   F11 fullscreen   F12 screenshot
  P pause   Tab (hold) fast forward"""

BASE_SIZE = (768, 560)


def frame_to_surface(frame, surf=None):
    h, w = frame.shape
    if surf is None or surf.get_size() != (w, h):
        surf = pygame.Surface((w, h), depth=32, masks=(0xff0000, 0x00ff00, 0x0000ff, 0))
    pygame.surfarray.blit_array(surf, frame.T)
    return surf


def save_png(frame, path):
    pygame.image.save(frame_to_surface(frame), str(path))


class Audio:
    def __init__(self):
        self.ok = False
        self.pending = collections.deque()
        self.pending_len = 0
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024)
            self.channel = pygame.mixer.Channel(0)
            self.ok = True
        except pygame.error as e:
            print(f'audio disabled: {e}')

    def push(self, samples):
        if not self.ok or samples is None:
            return
        self.pending.append(samples)
        self.pending_len += len(samples)
        # bound latency to half a second
        while self.pending_len > 22050 and len(self.pending) > 1:
            self.pending_len -= len(self.pending.popleft())

    def service(self):
        if not self.ok or not self.pending:
            return
        if self.channel.get_queue() is not None:
            return
        data = np.concatenate(list(self.pending))
        self.pending.clear()
        self.pending_len = 0
        snd = pygame.sndarray.make_sound(np.ascontiguousarray(data))
        if self.channel.get_busy():
            self.channel.queue(snd)
        else:
            self.channel.play(snd)

    def stop(self):
        self.pending.clear()
        self.pending_len = 0
        if self.ok:
            self.channel.stop()


class App:
    """Window, audio and input shared by the launcher and the emulator screen."""

    def __init__(self, scale=1.0, fullscreen=False, audio=True):
        pygame.init()
        self.scale = scale
        self.fullscreen = fullscreen
        self.window = None
        self.open_window()
        self.audio = Audio() if audio else None
        self.input = InputManager()
        print(self.input.describe())

    def open_window(self):
        if self.fullscreen:
            self.window = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            size = (int(BASE_SIZE[0] * self.scale), int(BASE_SIZE[1] * self.scale))
            self.window = pygame.display.set_mode(size, pygame.RESIZABLE)
        pygame.display.set_caption('OpenCDi')

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.open_window()

    def quit(self):
        pygame.quit()


class Emulator:
    """Runs a Machine in the App window until the user opens the menu or quits."""

    def __init__(self, app, machine, title='OpenCDi'):
        self.app = app
        self.m = machine
        self.base_title = title
        self.surf = None
        self.paused = False
        self.shots = 0
        machine.audio_enabled = app.audio is not None and app.audio.ok

    def draw(self, frame):
        if frame is None:
            return
        window = self.app.window
        self.surf = frame_to_surface(frame, self.surf)
        ww, wh = window.get_size()
        fw, fh = self.surf.get_size()
        k = min(ww / fw, wh / fh)
        tw, th = int(fw * k), int(fh * k)
        scaled = self.surf if (tw, th) == (fw, fh) else pygame.transform.scale(self.surf, (tw, th))
        window.fill((0, 0, 0))
        window.blit(scaled, ((ww - tw) // 2, (wh - th) // 2))
        pygame.display.flip()

    def screenshot(self):
        if self.m.frame is None:
            return
        out = Path.cwd() / f'opencdi_{time.strftime("%Y%m%d_%H%M%S")}_{self.shots}.png'
        self.shots += 1
        save_png(self.m.frame, out)
        print(f'screenshot saved to {out}')

    def run(self):
        """Returns 'menu' or 'quit'."""
        app, m, inp = self.app, self.m, self.app.input
        fps = 59.94 if m.ntsc else 50.0
        period = 1.0 / fps
        next_t = time.perf_counter()
        stats_t = time.perf_counter()
        stats_frames = 0
        pygame.display.set_caption(self.base_title)
        result = None
        # do not react to a menu button that is still held from the launcher
        menu_held = True
        while result is None:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    result = 'quit'
                elif inp.handle_event(ev):
                    continue
                elif ev.type == pygame.KEYDOWN:
                    k = ev.key
                    if k in (pygame.K_ESCAPE, pygame.K_F2):
                        if inp.mouse_captured and k == pygame.K_ESCAPE:
                            inp.capture_mouse(False)
                        else:
                            result = 'menu'
                    elif k == pygame.K_F3:
                        m.request_reset()
                    elif k == pygame.K_F11:
                        app.toggle_fullscreen()
                    elif k == pygame.K_F12:
                        self.screenshot()
                    elif k == pygame.K_p:
                        self.paused = not self.paused
                    elif k == pygame.K_F1:
                        print(HELP)
            menu_btn = inp.pad_state()[4]
            if menu_btn and not menu_held:
                result = 'menu'
            menu_held = menu_btn
            if result is not None:
                break
            if self.paused:
                time.sleep(0.05)
                next_t = time.perf_counter()
                continue
            x, y, b = inp.update()
            m.set_pointer(x, y, b)
            frame = m.run_frame()
            if app.audio:
                app.audio.push(m.take_audio())
                app.audio.service()
            self.draw(frame)
            stats_frames += 1
            now = time.perf_counter()
            if now - stats_t >= 1.0:
                speed = stats_frames / (now - stats_t) / fps * 100
                pygame.display.set_caption(f'{self.base_title} - {stats_frames / (now - stats_t):.1f} fps '
                                           f'({speed:.0f}%)')
                stats_t = now
                stats_frames = 0
            fast_forward = pygame.key.get_pressed()[pygame.K_TAB]
            next_t += period
            if fast_forward:
                next_t = now
            elif next_t > now:
                time.sleep(next_t - now)
            elif now - next_t > 0.25:
                next_t = now  # running slow; do not try to catch up
        if inp.mouse_captured:
            inp.capture_mouse(False)
        if app.audio:
            app.audio.stop()
        m.save()
        return result
