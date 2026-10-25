"""High level emulation of the Mono-I SLAVE microcontroller (68HC05).

It owns the pointing device (CD-i thumbpad/mouse), the front panel display,
audio attenuation and some system status queries.  Mapped at 0x310000.

Ported from MAME's cdislavehle.cpp (BSD-3-Clause, Ryan Holtz).
"""

from . import debuglog
from .bus import Device

dlog = debuglog.get('slave')

ATTENUATION = [
    0xff, 0xe4, 0xcb, 0xb5, 0xa2, 0x90, 0x80, 0x72,
    0x66, 0x5b, 0x51, 0x48, 0x40, 0x39, 0x33, 0x2e,
    0x29, 0x24, 0x20, 0x1d, 0x1a, 0x17, 0x14, 0x12,
    0x10, 0x0e, 0x0d, 0x0b, 0x0a, 0x09, 0x08, 0x07,
    0x06, 0x06, 0x05, 0x05, 0x04, 0x04, 0x03, 0x03,
    0x03, 0x02, 0x02, 0x02, 0x02, 0x01,
]


class Channel:
    def __init__(self):
        self.out_buf = [0, 0, 0, 0]
        self.out_index = 0
        self.out_count = 0
        self.out_cmd = 0


class Slave(Device):
    # not in save states (savestate.py); the pointer input is the live one
    STATE_SKIP = frozenset({'m', 'input_x', 'input_y', 'input_buttons'})

    def __init__(self, machine):
        self.m = machine
        self.int_event = None
        self.poll_event = None
        self.lcd = [0] * 16
        self.ntsc = False
        self.testplug = False
        # pointer input, written by the frontend
        self.input_x = 0
        self.input_y = 0
        self.input_buttons = 0
        self.reset()

    def reset(self):
        self.ch = [Channel() for _ in range(4)]
        self.in_buf = [0] * 17
        self.in_index = 0
        self.in_count = 0
        self.debug_mode = 0
        self.xbus_interrupt_enable = 0
        self.last_x = None
        self.last_y = None
        self.last_btn = None
        self.dev_x = 0
        self.dev_y = 0
        self.irq(False)
        if self.int_event:
            self.int_event.cancel()
            self.int_event = None
        if self.poll_event:
            self.poll_event.cancel()
        self.poll_event = self.m.sched.after_seconds(1 / 60, self.poll_inputs)

    def irq(self, state):
        self.m.scc.set_in(2, state)

    def trigger_readback_int(self):
        self.int_event = None
        self.irq(True)

    def prepare_readback(self, delay, channel, count, d0, d1, d2, d3, cmd):
        c = self.ch[channel]
        c.out_index = 0
        c.out_count = count
        c.out_buf = [d0, d1, d2, d3]
        c.out_cmd = cmd
        if self.int_event:
            self.int_event.cancel()
            self.int_event = None
        if delay is not None:
            self.int_event = self.m.sched.after_seconds(delay, self.trigger_readback_int)

    def poll_inputs(self):
        self.poll_event = self.m.sched.after_seconds(1 / 60, self.poll_inputs)
        x, y, btn = self.input_x & 0xffff, self.input_y & 0xffff, self.input_buttons
        if x == self.last_x and y == self.last_y and btn == self.last_btn:
            return
        bits = 0x01
        if btn & 1:
            bits |= 0x02
        if btn & 2:
            bits |= 0x04
        if btn & 4:
            bits |= 0x06
        dx = dy = 0
        if self.last_x is not None:
            dx = (x - self.last_x + 0x8000) % 0x10000 - 0x8000
            dy = (y - self.last_y + 0x8000) % 0x10000 - 0x8000
        self.last_x, self.last_y, self.last_btn = x, y, btn
        self.dev_x = min(max(self.dev_x + dx, 0), 767)
        self.dev_y = min(max(self.dev_y + dy, 0), 559)
        b3 = ((self.dev_x & 0x380) >> 7) | (bits << 3)
        b2 = self.dev_x & 0x7f
        b1 = (self.dev_y & 0x380) >> 7
        b0 = self.dev_y & 0x7f
        self.prepare_readback(0, 0, 4, b3, b2, b1, b0, 0xf7)

    def disc_type(self):
        disc = self.m.disc
        if disc is None:
            return 0x00
        return 0x02 if any(t.kind != 'audio' for t in disc.tracks) else 0x01

    # ----- bus ---------------------------------------------------------------
    def read16(self, a):
        return self.read8(a | 1)

    def read8(self, a):
        if not a & 1:
            return 0
        c = self.ch[(a >> 1) & 3]
        if c.out_count:
            ret = c.out_buf[c.out_index]
            if c.out_index == 0 and c.out_cmd in (0xb0, 0xb1, 0xf0, 0xf3, 0xf4, 0xf7):
                self.irq(False)
            c.out_index += 1
            c.out_count -= 1
            if not c.out_count:
                c.out_index = 0
                c.out_cmd = 0
                c.out_buf = [0, 0, 0, 0]
            return ret
        return 0xff

    def write16(self, a, v):
        self.write8(a | 1, v & 0xff)

    def write8(self, a, v):
        if not a & 1:
            return
        self.slave_w((a >> 1) & 3, v & 0xff)

    def _clear_in(self):
        self.in_buf = [0] * 17
        self.in_index = 0
        self.in_count = 0

    def slave_w(self, offset, data):
        if offset == 1 and self.in_index == 0:
            self._clear_in()
            return
        if self.in_index >= len(self.in_buf):
            # a command sequence the HLE does not understand ran past the
            # 17-byte buffer; drop it instead of raising IndexError
            dlog.warning('input buffer overflow (offset %d, data %02x, buffer %s); command dropped',
                         offset, data, ' '.join(f'{b:02x}' for b in self.in_buf))
            self._clear_in()
            return
        self.in_buf[self.in_index] = data
        self.in_index += 1
        if offset == 0:
            if self.in_buf[0] >= 0xc0:
                if self.in_index == 1:
                    self.in_count = 3
                elif self.in_index == self.in_count:
                    ib = self.in_buf
                    self.dev_x = ((ib[1] & 0x70) << 3) | (ib[2] & 0x7f)
                    self.dev_y = ((ib[1] & 0x0f) << 6) | (ib[0] & 0x3f)
                    self._clear_in()
            elif self.in_index == 1:
                self.in_index = 0
        elif offset == 1:
            if self.in_index > 1 and self.in_index == self.in_count:
                if self.in_buf[0] == 0xf0:
                    self.set_lcd(self.in_buf[1:17])
                self._clear_in()
        elif offset == 2:
            if self.in_index > 1:
                if self.in_index == self.in_count:
                    cmd = self.in_buf[0]
                    if 0xc0 <= cmd <= 0xcf:
                        gains = [ATTENUATION[b] if b < len(ATTENUATION) else 0 for b in self.in_buf[1:5]]
                        self.m.cdic.set_attenuation(gains)
                        self.in_index = 0
                        self.in_count = 0
                    elif cmd == 0xf0:
                        self.in_buf[1:17] = [0] * 16
                        self.in_count = 17
                    else:
                        self._clear_in()
            else:
                if data == 0x82:    # mute
                    self.m.cdic.muted = True
                    self.in_index = self.in_count = 0
                elif data == 0x83:  # unmute
                    self.m.cdic.muted = False
                    self.in_index = self.in_count = 0
                elif data == 0x8a:  # reset system
                    self.in_index = self.in_count = 0
                    self.m.request_reset()
                elif 0xc0 <= data <= 0xcf:
                    self.in_count = 5
                elif data == 0xf0:
                    self.in_count = 17
                else:
                    self._clear_in()
        elif offset == 3:
            if self.in_index > 1:
                if self.in_index == self.in_count:
                    if self.in_buf[0] == 0xb0:
                        self.prepare_readback(0.25, 3, 4, 0xb0, 0x00, self.disc_type(), 0x15, 0xb0)
                    self._clear_in()
            else:
                if data in (0x80, 0x81, 0xb0, 0xb1):
                    self.in_count = 4
                elif data == 0xf0:   # slave revision
                    self.prepare_readback(1e-4, 2, 2, 0xf0, 0x32, 0x31, 0, 0xf0)
                    self.in_index = 0
                elif data == 0xf3:   # pointer type
                    self.in_index = 0
                    self.prepare_readback(1e-4, 2, 2, 0xf3, 1, 0, 0, 0xf3)
                elif data == 0xf4:   # test plug
                    self.in_index = 0
                    self.prepare_readback(1e-4, 2, 2, 0xf4, 1 if self.testplug else 0, 0, 0, 0xf4)
                elif data == 0xf6:   # NTSC/PAL
                    self.prepare_readback(None, 2, 2, 0xf6, 1 if self.ntsc else 2, 0, 0, 0xf6)
                    self.in_index = 0
                elif data == 0xf7:
                    self.debug_mode = 1
                    self.in_index = 0
                elif data == 0xfa:
                    self.xbus_interrupt_enable = 1
                    self.in_index = 0
                elif data == 0xfe:
                    self.debug_mode = 0
                    self.in_index = 0
                else:
                    self._clear_in()

    def set_lcd(self, data):
        self.lcd = list(data)
        self.m.lcd_changed(self.lcd)
