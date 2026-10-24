"""pygame window, audio output and the emulation loop."""

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pygame

from . import debuglog
from .audio import OUT_RATE, LinearResampler
from .inputs import InputManager

dlog = debuglog.get('frontend')

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
    """Continuous audio stream with a jitter buffer.

    The emulator delivers audio in uneven bursts (0, 588 or 1176 samples per
    frame for CD-DA, about 2000 per XA sector) and frames do not arrive at
    exact times.  Samples are collected in a buffer and handed to the mixer in
    fixed blocks: one playing, one queued.  A feeder thread tops the mixer up
    every few milliseconds, so a slow frame does not starve it while audio is
    waiting in the buffer.  Playback starts (and restarts after an underrun)
    only once PREBUFFER samples are waiting, so a late frame is absorbed instead
    of heard.  A small rate correction (at most 0.5 %) keeps the buffer near its
    target when the host and emulated clocks drift; the emulator loop also waits
    for the sound card when the buffer runs over (sync to audio).
    """

    BLOCK = 2048                 # samples per mixer Sound (46 ms)
    PREBUFFER = 3 * BLOCK        # samples waiting before playback (re)starts
    TARGET = 3 * BLOCK           # fill level the rate correction aims for
    MAX = 12 * BLOCK             # beyond this (fast forward) the oldest audio is dropped
    DRC = 0.005                  # largest rate correction
    TAIL_FRAMES = 3              # frames without new audio before a short tail is flushed
    FEED_INTERVAL = 0.005        # seconds between feeder thread top-ups

    def __init__(self, channel=None, rate=OUT_RATE, feeder=True):
        """Opens the pygame mixer; tools/check_fixes.py passes its own channel."""
        self.ok = False
        self.buf = np.zeros((0, 2), dtype=np.int16)
        self.resampler = LinearResampler()
        self.primed = False
        self.quiet_frames = 0
        self.stats = {'underruns': 0, 'dropped': 0, 'blocks': 0}
        self.rate = rate
        self.lock = threading.Lock()
        self._feeder = None
        self._running = False
        self.make_sound = lambda blk: pygame.sndarray.make_sound(np.ascontiguousarray(blk))
        if channel is not None:
            self.channel = channel
            self.ok = True
        else:
            try:
                pygame.mixer.init(frequency=OUT_RATE, size=-16, channels=2, buffer=1024)
                freq, _size, channels = pygame.mixer.get_init()
                if channels != 2:
                    raise pygame.error(f'mixer opened with {channels} channels, need 2')
                self.rate = freq
                self.channel = pygame.mixer.Channel(0)
                self.ok = True
                dlog.info('audio: %d Hz, block %d samples, prebuffer %.0f ms', freq, self.BLOCK,
                          self.PREBUFFER * 1000 / freq)
            except pygame.error as e:
                print(f'audio disabled: {e}')
                dlog.warning('audio disabled: %s', e)
        if self.ok and feeder:
            self._running = True
            self._feeder = threading.Thread(target=self._feed_loop, name='audio-feeder', daemon=True)
            self._feeder.start()

    def _feed_loop(self):
        while self._running:
            time.sleep(self.FEED_INTERVAL)
            try:
                self.service()
            except pygame.error as e:      # mixer closed under us during shutdown
                dlog.debug('audio feeder stopped: %s', e)
                return

    def close(self):
        self._running = False
        if self._feeder is not None:
            self._feeder.join(timeout=1)
            self._feeder = None

    def fill(self):
        return len(self.buf)

    def push(self, samples):
        """Add one frame's audio (int16 stereo at 44.1 kHz, or None)."""
        if not self.ok:
            return
        with self.lock:
            self._push(samples)

    def _push(self, samples):
        if samples is None or len(samples) == 0:
            self.quiet_frames += 1
            return
        self.quiet_frames = 0
        # rate correction: above target play slightly faster, below slightly slower
        err = max(-1.0, min(1.0, (self.fill() - self.TARGET) / self.TARGET))
        step = OUT_RATE / self.rate * (1.0 + err * self.DRC)
        out = self.resampler.process(samples.astype(np.float32), step)
        self.buf = np.concatenate([self.buf, np.clip(out, -32768, 32767).astype(np.int16)])
        if len(self.buf) > self.MAX:
            drop = len(self.buf) - self.TARGET
            self.buf = self.buf[drop:]
            self.stats['dropped'] += drop
            dlog.debug('audio buffer over %d samples; dropped the oldest %d', self.MAX, drop)

    def _take_block(self):
        if len(self.buf) >= self.BLOCK:
            blk, self.buf = self.buf[:self.BLOCK], self.buf[self.BLOCK:]
            return blk
        if len(self.buf) and self.quiet_frames >= self.TAIL_FRAMES:
            # the sound stopped: play what is left instead of holding it back
            blk = np.zeros((self.BLOCK, 2), dtype=np.int16)
            blk[:len(self.buf)] = self.buf
            self.buf = self.buf[:0]
            return blk
        return None

    def _sound(self, blk):
        self.stats['blocks'] += 1
        return self.make_sound(blk)

    def service(self):
        """Keep one block playing and one queued (called by the feeder thread
        and once per frame)."""
        if not self.ok:
            return
        with self.lock:
            self._service()

    def _service(self):
        queued = self.channel.get_queue() is not None
        # between one sound ending and the queued one starting get_busy() is
        # briefly False; with a sound still queued the channel has not run dry
        playing = self.channel.get_busy() or queued
        if self.primed and not playing:
            # the mixer ran dry: that was an audible gap
            self.primed = False
            if len(self.buf) or self.quiet_frames < self.TAIL_FRAMES:
                self.stats['underruns'] += 1
                n = self.stats['underruns']
                if n <= 20 or n % 100 == 0:
                    dlog.info('audio underrun #%d (%d samples buffered); refilling %.0f ms before restarting',
                              n, len(self.buf), self.PREBUFFER * 1000 / self.rate)
        if not self.primed:
            if len(self.buf) < self.PREBUFFER and not (len(self.buf) and self.quiet_frames >= self.TAIL_FRAMES):
                return
            self.primed = True
        if not playing:
            blk = self._take_block()
            if blk is None:
                return
            self.channel.play(self._sound(blk))
        if not queued:
            blk = self._take_block()
            if blk is not None:
                self.channel.queue(self._sound(blk))

    def stop(self):
        with self.lock:
            self.buf = self.buf[:0]
            self.resampler.reset()
            self.primed = False
            self.quiet_frames = 0
            if self.ok:
                self.channel.stop()


class App:
    """Window, audio and input shared by the launcher and the emulator screen."""

    def __init__(self, scale=1.0, fullscreen=False, audio=True):
        pygame.init()
        self.scale = scale
        self.fullscreen = fullscreen
        self.window = None
        self.content_size = BASE_SIZE   # what is shown: the launcher canvas or the CD-i picture
        self.user_sized = False         # the user resized the window: keep their size
        self.open_window()
        self.audio = Audio() if audio else None
        self.input = InputManager()
        print(self.input.describe())

    def window_size(self):
        return (int(self.content_size[0] * self.scale), int(self.content_size[1] * self.scale))

    def open_window(self):
        if self.fullscreen:
            self.window = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.window = pygame.display.set_mode(self.window_size(), pygame.RESIZABLE)
        pygame.display.set_caption('OpenCDi')
        try:
            pygame.display.set_icon(pygame.image.load(str(Path(__file__).with_name('data') / 'opencdi.png')))
        except (pygame.error, FileNotFoundError):
            pass

    def set_content_size(self, size):
        """Size the window to what it shows (times --scale), so that no
        scaling or black border has to be drawn and sent to the display;
        over SSH X forwarding every window pixel costs bandwidth."""
        size = tuple(size)
        if size == self.content_size:
            return
        self.content_size = size
        if not self.fullscreen and not self.user_sized:
            dlog.debug('window %dx%d for a %dx%d picture', *self.window_size(), *size)
            self.window = pygame.display.set_mode(self.window_size(), pygame.RESIZABLE)

    def note_resize(self, size):
        """A VIDEORESIZE event: a size we did not ask for came from the user."""
        if not self.fullscreen and tuple(size) != self.window_size():
            self.user_sized = True

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.open_window()

    def quit(self):
        if self.audio:
            self.audio.close()
        pygame.quit()


EXPOSE_EVENTS = {getattr(pygame, n) for n in ('VIDEOEXPOSE', 'WINDOWEXPOSED', 'WINDOWSHOWN', 'WINDOWRESTORED')
                 if hasattr(pygame, n)}


class Emulator:
    """Runs a Machine in the App window until the user opens the menu or quits.

    The machine runs on its own thread, paced at the emulated frame rate
    (60.05 Hz NTSC, 50.08 Hz PAL) and synced to the sound card.  The main
    thread handles events and puts the newest picture in the window.  Sending
    a picture to the display can take most of a frame (about 16 ms for
    768x480 over SSH X forwarding); pygame releases the GIL while it waits, so
    the emulation keeps running instead of waiting for the display (0.1.3 did
    both in turn, which gave about 30 fps).
    """

    MAX_SKIP = 3   # most frames in a row whose picture may be skipped
    SWITCH_INTERVAL = 0.001   # seconds; see run()
    # audio clock sync (see _emulate_loop)
    SYNC_SMOOTH = 1 / 120     # weight of each frame in the average buffer level (about 2 s)
    SYNC_GAIN = 0.5           # frame length change per relative error of the average level
    SYNC_RANGE = 0.10         # at most 10 % longer or shorter frames
    SYNC_QUIET = 20           # frames after the last audio during which the sync still applies

    def __init__(self, app, machine, title='OpenCDi'):
        self.app = app
        self.m = machine
        self.base_title = title
        self.surf = None
        self.paused = False
        self.shots = 0
        machine.audio_enabled = app.audio is not None and app.audio.ok
        # shared with the emulation thread
        self.input_lock = threading.Lock()   # InputManager state (events vs. per-frame update)
        self._cond = threading.Condition()   # guards _frame/_seq/_done
        self._frame = None
        self._seq = 0
        self._done = False
        self._stop = False
        self._error = None
        self.frames = 0          # emulated frames this session
        self.skipped = 0         # of those, pictures not drawn (host too slow)

    def draw(self, frame):
        if frame is None:
            return
        h, w = frame.shape
        self.app.set_content_size((w, h))
        window = self.app.window
        ww, wh = window.get_size()
        if (ww, wh) == (w, h) and window.get_bitsize() == 32 and window.get_masks()[:3] == (0xff0000, 0xff00, 0xff):
            pygame.surfarray.blit_array(window, frame.T)     # straight into the window, no copies
        else:
            self.surf = frame_to_surface(frame, self.surf)
            k = min(ww / w, wh / h)
            tw, th = int(w * k), int(h * k)
            scaled = self.surf if (tw, th) == (w, h) else pygame.transform.scale(self.surf, (tw, th))
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

    # ----- emulation thread --------------------------------------------------------
    def _emulate(self):
        try:
            self._emulate_loop()
        except BaseException as e:  # noqa: BLE001 - handed to the main thread
            self._error = e
        finally:
            with self._cond:
                self._done = True
                self._cond.notify_all()

    def _emulate_loop(self):
        app, m, inp = self.app, self.m, self.app.input
        # pace at the emulated frame rate (PAL 50.08 Hz, NTSC 60.05 Hz)
        fps = m.frame_rate()
        period = 1.0 / fps
        next_t = time.perf_counter()
        skip_next = False
        skipped_in_row = 0
        avg_fill = app.audio.TARGET if app.audio else 0
        while not self._stop:
            if self.paused:
                if app.audio and app.audio.primed:
                    app.audio.stop()
                time.sleep(0.05)
                next_t = time.perf_counter()
                continue
            # input is read once per emulated frame, so pointer speed does not
            # depend on how many pictures reach the display
            with self.input_lock:
                x, y, b = inp.update()
                fast_forward = pygame.key.get_pressed()[pygame.K_TAB]
            m.set_pointer(x, y, b)
            render = not skip_next
            frame = m.run_frame(render=render)
            if app.audio:
                app.audio.push(m.take_audio())
                app.audio.service()
            self.frames += 1
            if render:
                with self._cond:
                    self._frame = frame
                    self._seq += 1
                    self._cond.notify_all()
            else:
                self.skipped += 1
            now = time.perf_counter()
            fps = m.frame_rate()      # follows the ROM switching PAL/NTSC timing
            period = 1.0 / fps
            next_t += period
            au = app.audio
            if au and au.primed and not fast_forward and au.quiet_frames < self.SYNC_QUIET:
                # sync to the sound card's clock, both ways.  The buffer level
                # is averaged over about two seconds, because XA audio comes in
                # bursts of up to 9,400 samples; a frame is made up to
                # SYNC_RANGE longer when the card plays slower than the
                # emulator produces, and shorter when it plays faster
                fill = au.fill()
                avg_fill += (fill - avg_fill) * self.SYNC_SMOOTH
                err = (avg_fill - au.TARGET) / au.TARGET
                next_t += period * max(-self.SYNC_RANGE, min(self.SYNC_RANGE, err * self.SYNC_GAIN))
                if fill > au.MAX - 2 * au.BLOCK:
                    next_t += period            # about to overflow: wait a whole frame
            elif au:
                avg_fill = au.TARGET
            if fast_forward:
                next_t = now
            elif next_t > now:
                time.sleep(next_t - now)
            elif now - next_t > period * 2:
                # running slow: do not catch up with a burst of frames, which
                # would push a burst of audio into the buffer
                next_t = now
            # a whole frame behind schedule: skip drawing the next picture
            # (never the emulation) so the sound keeps up; at most MAX_SKIP in
            # a row.  Shorter delays (waiting for the GIL while a picture is
            # sent) are made up without skipping.
            late = now - next_t
            skip_next = (not fast_forward and late > period and skipped_in_row < self.MAX_SKIP)
            skipped_in_row = skipped_in_row + 1 if skip_next else 0

    # ----- main thread: events and display -----------------------------------------
    def run(self):
        """Returns 'menu' or 'quit'."""
        app, m, inp = self.app, self.m, self.app.input
        audio_start = dict(app.audio.stats) if app.audio else None
        start_frames, start_skipped = self.frames, self.skipped
        pygame.display.set_caption(self.base_title)
        self._stop = False
        self._done = False
        self._error = None
        # the main thread waits for the GIL each time a picture has been sent;
        # with Python's default 5 ms switch interval that wait alone took a
        # third of a 60 Hz frame
        switch = sys.getswitchinterval()
        sys.setswitchinterval(self.SWITCH_INTERVAL)
        worker = threading.Thread(target=self._emulate, name='emulation', daemon=True)
        worker.start()
        result = None
        # do not react to a menu button that is still held from the launcher
        menu_held = True
        shown_seq = self._seq
        last_drawn = None
        redraw = True
        sent = unchanged = missed = 0       # this session: pictures sent, identical ones not sent, never seen
        send_time = 0.0
        stats_t = time.perf_counter()
        stats_frames, stats_skipped, stats_sent, stats_missed = self.frames, self.skipped, 0, 0
        try:
            while result is None:
                for ev in pygame.event.get():
                    if ev.type == pygame.QUIT:
                        result = 'quit'
                        continue
                    if ev.type == pygame.VIDEORESIZE:
                        app.note_resize(ev.size)
                        redraw = True
                    elif ev.type in EXPOSE_EVENTS:
                        redraw = True
                    with self.input_lock:
                        handled = inp.handle_event(ev)
                    if handled:
                        continue
                    if ev.type == pygame.KEYDOWN:
                        k = ev.key
                        if k in (pygame.K_ESCAPE, pygame.K_F2):
                            if inp.mouse_captured and k == pygame.K_ESCAPE:
                                with self.input_lock:
                                    inp.capture_mouse(False)
                            else:
                                result = 'menu'
                        elif k == pygame.K_F3:
                            m.request_reset()
                        elif k == pygame.K_F11:
                            app.toggle_fullscreen()
                            redraw = True
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
                with self._cond:
                    if self._seq == shown_seq and not redraw and not self._done:
                        self._cond.wait(timeout=0.05)     # also keeps the window responsive while paused
                    frame, seq, done = self._frame, self._seq, self._done
                if done:
                    break
                if frame is not None and (seq != shown_seq or redraw):
                    if seq - shown_seq > 1:
                        missed += seq - shown_seq - 1
                        stats_missed += seq - shown_seq - 1
                    shown_seq = seq
                    if redraw or last_drawn is None or not np.array_equal(frame, last_drawn):
                        t = time.perf_counter()
                        self.draw(frame)
                        send_time += time.perf_counter() - t
                        sent += 1
                        stats_sent += 1
                    else:
                        unchanged += 1     # same picture as on screen: nothing to send
                    last_drawn = frame
                    redraw = False
                now = time.perf_counter()
                if now - stats_t >= 1.0:
                    dt = now - stats_t
                    rate = (self.frames - stats_frames) / dt
                    fps = m.frame_rate()
                    speed = rate / fps * 100
                    not_shown = (self.skipped - stats_skipped) + stats_missed
                    skip = f', {not_shown} not shown' if not_shown else ''
                    pygame.display.set_caption(f'{self.base_title} - {rate:.1f} fps ({speed:.0f}%{skip})')
                    if not_shown or speed < 98:
                        dlog.debug('%.1f fps (%.0f%%), %d pictures not shown (%d skipped, %d missed by the display), '
                                   '%d sent, audio buffer %d samples', rate, speed, not_shown,
                                   self.skipped - stats_skipped, stats_missed, stats_sent,
                                   app.audio.fill() if app.audio else 0)
                    stats_t = now
                    stats_frames, stats_skipped, stats_sent, stats_missed = self.frames, self.skipped, 0, 0
        finally:
            self._stop = True
            worker.join()
            sys.setswitchinterval(switch)
        if inp.mouse_captured:
            inp.capture_mouse(False)
        if self._error is not None:
            raise self._error
        total_frames = self.frames - start_frames
        total_skipped = self.skipped - start_skipped
        if app.audio:
            st = app.audio.stats
            dlog.info('session: %d frames, %d pictures skipped; audio %d blocks, %d underruns, %d samples dropped',
                      total_frames, total_skipped, st['blocks'] - audio_start['blocks'],
                      st['underruns'] - audio_start['underruns'], st['dropped'] - audio_start['dropped'])
            app.audio.stop()
        else:
            dlog.info('session: %d frames, %d pictures skipped (no audio)', total_frames, total_skipped)
        ww, wh = app.window.get_size()
        dlog.info('display: %d pictures sent to the %dx%d window (%.1f ms each), %d unchanged not sent, '
                  '%d missed (display too slow)', sent, ww, wh, send_time * 1000 / max(sent, 1), unchanged, missed)
        vs = m.video.render_stats
        dlog.info('video: %d pictures rendered, %d reused unchanged, %d skipped', vs[0], vs[1], vs[2])
        m.save()
        return result
