"""SCC68070 on-chip peripherals: interrupt controller, timers, UART, I2C,
DMA and MMU registers.  Mapped at 0x80000000 (supervisor only).

Ported from MAME's scc68070.cpp (BSD-3-Clause, Ryan Holtz).  As in MAME the
MMU registers are stored but no translation is performed, and the DMA
controller only holds the registers the CDIC uses for its transfers.
"""

from .bus import Device

# UART status bits
USR_RB, USR_FE, USR_PE, USR_OE, USR_TXEMT, USR_TXRDY, USR_RXRDY = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x01
TSR_OV0 = 0x80
CSR_COC = 0x80
CCR_SO, CCR_INE, CCR_IPL = 0x80, 0x08, 0x07
ISR_PIN = 0x10


class DmaChannel:
    def __init__(self):
        self.reset()

    def reset(self):
        self.status = 0
        self.error = 0
        self.device_control = 0
        self.operation_control = 0
        self.sequence_control = 0
        self.channel_control = 0
        self.transfer_counter = 0
        self.memory_address_counter = 0
        self.device_address_counter = 0


class SCC68070(Device):
    def __init__(self, machine):
        self.m = machine
        self.cpu = None
        self.dma = [DmaChannel(), DmaChannel()]
        self.uart_out = []
        self.uart_rx = []
        self.timer_event = None
        self.timer_start = 0
        self.uart_event = None
        self.in_lines = {2: False, 4: False, 5: False, 7: False}
        self.int1 = False
        self.int2 = False
        self.iack_callbacks = {}
        self.reset()

    def reset(self):
        self.lir = 0
        self.picr1 = 0
        self.picr2 = 0
        self.timer_int = False
        self.i2c_int = False
        self.uart_rx_int = False
        self.uart_tx_int = False
        self.i2c = [0, 0, ISR_PIN, 0, 0]  # data, address, status, control, clock control
        self.umr = 0
        self.usr = USR_TXRDY | USR_TXEMT
        self.ucsr = 0
        self.ucr = 0
        self.uth = 0
        self.urh = 0
        self.tsr = 0
        self.tcr = 0
        self.reload = 0
        self.timer0 = 0
        self.timer1 = 0
        self.timer2 = 0
        for ch in self.dma:
            ch.reset()
        self.mmu_status = 0
        self.mmu_control = 0
        self.mmu_desc = [[0, 0, 0, 0] for _ in range(8)]
        self.set_timer()
        self.update_ipl()

    # ----- interrupts --------------------------------------------------------
    def update_ipl(self):
        il = self.in_lines
        ext = 7 if il[7] else 5 if il[5] else 4 if il[4] else 2 if il[2] else 0
        lir = self.lir
        levels = [
            ext,
            (lir >> 4) & 7 if lir & 0x80 else 0,
            lir & 7 if lir & 0x08 else 0,
            self.picr1 & 7 if self.timer_int else 0,
            (self.picr2 >> 4) & 7 if self.uart_rx_int else 0,
            self.picr2 & 7 if self.uart_tx_int else 0,
            (self.picr1 >> 4) & 7 if self.i2c_int else 0,
        ]
        for ch in self.dma:
            if ch.status & CSR_COC and ch.channel_control & CCR_INE:
                levels.append(ch.channel_control & CCR_IPL)
        if self.cpu is not None:
            self.cpu.set_ipl(max(levels))

    def set_in(self, line, state):
        if self.in_lines[line] != bool(state):
            self.in_lines[line] = bool(state)
            self.update_ipl()

    def int1_w(self, state):
        state = bool(state)
        if self.int1 != state:
            if state and not self.lir & 0x80:
                self.lir |= 0x80
                self.update_ipl()
            self.int1 = state

    def int2_w(self, state):
        state = bool(state)
        if self.int2 != state:
            if state and not self.lir & 0x08:
                self.lir |= 0x08
                self.update_ipl()
            self.int2 = state

    def iack(self, level):
        if level in self.in_lines and self.in_lines[level]:
            cb = self.iack_callbacks.get(level)
            return cb() if cb else 24 + level
        if self.lir & 0x80 and level == (self.lir >> 4) & 7:
            self.lir &= 0x7f
        elif self.lir & 0x08 and level == self.lir & 7:
            self.lir &= 0xf7
        elif self.timer_int and level == self.picr1 & 7:
            self.timer_int = False
        elif self.uart_rx_int and level == (self.picr2 >> 4) & 7:
            self.uart_rx_int = False
        elif self.uart_tx_int and level == self.picr2 & 7:
            self.uart_tx_int = False
        elif self.i2c_int and level == (self.picr1 >> 4) & 7:
            self.i2c_int = False
        self.update_ipl()
        return 0x38 + level

    # ----- timer 0 ---------------------------------------------------------------
    def set_timer(self):
        if self.timer_event is not None:
            self.timer_event.cancel()
        s = self.m.sched
        period = 96 * (0x10000 - self.timer0)
        self.timer_start = s.now
        self.timer_period = period
        self.timer_event = s.after(period, self.timer_fire)

    def timer_fire(self):
        self.timer0 = self.reload
        self.tsr |= TSR_OV0
        if not self.timer_int:
            self.timer_int = True
            self.update_ipl()
        self.timer_event = None
        self.set_timer()

    def timer0_value(self):
        elapsed = (self.m.sched.now - self.timer_start) // 96
        return (self.timer0 + elapsed) & 0xffff

    # ----- UART --------------------------------------------------------------------
    def uart_tx_byte(self, v):
        self.uart_out.append(v)
        if v == 10 or len(self.uart_out) > 200:
            text = bytes(self.uart_out).decode('latin-1').rstrip('\r\n')
            self.uart_out.clear()
            self.m.log_serial(text)

    def update_tx_int(self):
        tx_enabled = ((self.ucr >> 2) & 3) == 1
        ready = tx_enabled and bool(self.usr & USR_TXRDY)
        if ready != self.uart_tx_int:
            self.uart_tx_int = ready
            self.update_ipl()

    def uart_tx_done(self):
        self.uart_event = None
        self.uart_tx_byte(self.uth)
        self.usr |= USR_TXRDY | USR_TXEMT
        self.update_tx_int()

    def uart_feed(self, data):
        """Characters typed into the serial port (OS-9 console)."""
        self.uart_rx.extend(data)
        self._uart_rx_next()

    def _uart_rx_next(self):
        if self.uart_rx and (self.ucr & 3) == 1 and not self.usr & USR_RXRDY:
            self.urh = self.uart_rx.pop(0)
            self.usr |= USR_RXRDY
            self.uart_rx_int = True
            self.update_ipl()

    # ----- register access ------------------------------------------------------------
    def read8(self, a):
        a &= 0xffff
        if a == 0x1001:
            return self.lir & 0x77
        if a == 0x2001:
            return self.i2c[0]
        if a == 0x2003:
            return self.i2c[1]
        if a == 0x2005:
            return self.i2c[2]
        if a == 0x2007:
            return self.i2c[3]
        if a == 0x2009:
            return self.i2c[4]
        if a == 0x2011:
            return self.umr
        if a == 0x2013:
            return self.usr
        if a == 0x2015:
            return self.ucsr | 0x08
        if a == 0x2017:
            return self.ucr | 0x80
        if a == 0x2019:
            return self.uth
        if a == 0x201b:
            v = self.urh
            if self.uart_rx_int:
                self.uart_rx_int = False
                self.update_ipl()
            self.usr &= ~USR_RXRDY
            self._uart_rx_next()
            return v
        if a == 0x2045:
            return self.picr1 & 0x77
        if a == 0x2047:
            return self.picr2 & 0x77
        if 0x2020 <= a <= 0x2029 or 0x4000 <= a <= 0x406d or 0x8000 <= a <= 0x807f:
            v = self.read16(a & ~1)
            return v & 0xff if a & 1 else v >> 8
        return 0

    def read16(self, a):
        a &= 0xffff
        if 0x2020 <= a <= 0x2029:
            o = a - 0x2020
            if o == 0:
                return self.tsr << 8 | self.tcr
            if o == 2:
                return self.reload
            if o == 4:
                return self.timer0_value()
            if o == 6:
                return self.timer1
            if o == 8:
                return self.timer2
            return 0
        if 0x4000 <= a <= 0x406d:
            ch = self.dma[(a - 0x4000) >> 6]
            o = (a - 0x4000) & 0x3f
            if o == 0x00:
                return ch.status << 8 | ch.error
            if o == 0x04:
                return ch.device_control << 8 | ch.operation_control
            if o == 0x06:
                return ch.sequence_control << 8 | ch.channel_control
            if o == 0x0a:
                return ch.transfer_counter
            if o == 0x0c:
                return ch.memory_address_counter >> 16
            if o == 0x0e:
                return ch.memory_address_counter & 0xffff
            if o == 0x14:
                return ch.device_address_counter >> 16
            if o == 0x16:
                return ch.device_address_counter & 0xffff
            return 0
        if 0x8000 <= a <= 0x807f:
            o = a - 0x8000
            if o == 0:
                return self.mmu_status << 8 | self.mmu_control
            if o >= 0x40:
                d = self.mmu_desc[(o - 0x40) >> 3]
                return d[((o - 0x40) & 7) >> 1]
            return 0
        return self.read8(a) << 8 | self.read8(a + 1)

    def write8(self, a, v):
        a &= 0xffff
        if a == 0x1001:
            self._lir_w(v)
        elif a in (0x2001, 0x2003, 0x2005, 0x2007, 0x2009):
            self._i2c_w((a - 0x2001) >> 1, v)
        elif a == 0x2011:
            self.umr = v & 0xdf
        elif a == 0x2015:
            self.ucsr = v
        elif a == 0x2017:
            self._ucr_w(v)
        elif a == 0x2019:
            self.uth = v
            self.usr &= ~(USR_TXRDY | USR_TXEMT)
            self.update_tx_int()
            if ((self.ucr >> 2) & 3) == 1 and self.uart_event is None:
                # roughly one character time at 9600 baud
                self.uart_event = self.m.sched.after(15000, self.uart_tx_done)
        elif a == 0x2045:
            self._picr1_w(v)
        elif a == 0x2047:
            self.picr2 = v & 0x77
            if v & 0x88:
                if v & 0x80:
                    self.uart_rx_int = False
                if v & 0x08:
                    self.uart_tx_int = False
                self.update_ipl()
        elif 0x2020 <= a <= 0x2029 or 0x4000 <= a <= 0x406d or 0x8000 <= a <= 0x807f:
            self._write16m(a & ~1, v << 8 if not a & 1 else v, 0xff00 if not a & 1 else 0x00ff)

    def write16(self, a, v):
        a &= 0xffff
        if 0x2020 <= a <= 0x2029 or 0x4000 <= a <= 0x406d or 0x8000 <= a <= 0x807f:
            self._write16m(a, v, 0xffff)
        else:
            self.write8(a, v >> 8)
            self.write8(a + 1, v & 0xff)

    def _write16m(self, a, v, mask):
        def comb(old):
            return (old & ~mask) | (v & mask)
        if 0x2020 <= a <= 0x2029:
            o = a - 0x2020
            if o == 0:
                if mask & 0x00ff:
                    self.tcr = v & 0xff
                if mask & 0xff00:
                    self.tsr &= ~(v >> 8)
            elif o == 2:
                self.reload = comb(self.reload)
                self.set_timer()
            elif o == 4:
                self.timer0 = comb(self.timer0)
                self.set_timer()
            elif o == 6:
                self.timer1 = comb(self.timer1)
            elif o == 8:
                self.timer2 = comb(self.timer2)
            return
        if 0x4000 <= a <= 0x406d:
            ch = self.dma[(a - 0x4000) >> 6]
            o = (a - 0x4000) & 0x3f
            if o == 0x00:
                if mask & 0xff00:
                    ch.status &= ~((v >> 8) & 0xb0)
                    self.update_ipl()
            elif o == 0x04:
                if mask & 0x00ff:
                    ch.operation_control = v & 0xff
                if mask & 0xff00:
                    ch.device_control = v >> 8
            elif o == 0x06:
                if mask & 0x00ff:
                    ch.channel_control = v & 0x7f
                    if v & CCR_SO:
                        ch.status |= CSR_COC
                    self.update_ipl()
                if mask & 0xff00:
                    ch.sequence_control = v >> 8
            elif o == 0x0a:
                ch.transfer_counter = comb(ch.transfer_counter)
            elif o == 0x0c:
                ch.memory_address_counter = (ch.memory_address_counter & ~(mask << 16)) | ((v & mask) << 16)
            elif o == 0x0e:
                ch.memory_address_counter = (ch.memory_address_counter & ~mask) | (v & mask)
            elif o == 0x14:
                ch.device_address_counter = (ch.device_address_counter & ~(mask << 16)) | ((v & mask) << 16)
            elif o == 0x16:
                ch.device_address_counter = (ch.device_address_counter & ~mask) | (v & mask)
            return
        if 0x8000 <= a <= 0x807f:
            o = a - 0x8000
            if o == 0:
                if mask & 0x00ff:
                    self.mmu_control = v & 0xff
            elif o >= 0x40:
                d = self.mmu_desc[(o - 0x40) >> 3]
                i = ((o - 0x40) & 7) >> 1
                if i == 2:
                    if mask & 0x00ff:
                        d[2] = v & 0xff
                else:
                    d[i] = comb(d[i])

    def _lir_w(self, data):
        sel = data & 0x88
        if sel == 0x08 and self.lir & 0x08:
            self.lir &= 0xf7
            self.update_ipl()
        elif sel == 0x80:
            self.lir &= 0x7f
            self.update_ipl()
        elif sel == 0x88:
            self.lir &= 0x77
            self.update_ipl()
        self.lir = (self.lir & 0x88) | (data & 0x77)

    def _picr1_w(self, data):
        self.picr1 = data & 0x77
        sel = data & 0x88
        if sel == 0x08 and self.timer_int:
            self.timer_int = False
            self.update_ipl()
        elif sel == 0x80 and self.i2c_int:
            self.i2c_int = False
            self.update_ipl()
        elif sel == 0x88 and (self.timer_int or self.i2c_int):
            self.timer_int = self.i2c_int = False
            self.update_ipl()

    def _i2c_w(self, idx, v):
        # The Mono-I board has nothing on the I2C bus; complete transfers at
        # once with no acknowledge so software never waits forever.
        self.i2c[idx] = v
        if idx == 2:     # status: start/stop requests
            self.i2c[2] = (v & 0xe0) | ISR_PIN | 0x01
        elif idx == 0:   # data written: transfer "done", NAK
            self.i2c[2] = (self.i2c[2] & ~ISR_PIN) | 0x01
            if self.i2c[3] & 0x08:
                self.i2c_int = True
                self.update_ipl()

    def _ucr_w(self, data):
        self.ucr = data
        misc = (data & 0xf0) >> 4
        if misc == 2:
            self.usr &= ~USR_RXRDY
            self.ucr &= 0xfc
            self.urh = 0
            self.uart_rx_int = False
            self.update_ipl()
        elif misc == 3:
            self.usr |= USR_TXEMT | USR_TXRDY
            self.ucr &= 0xf3
            self.uth = 0
            self.uart_tx_int = False
            self.update_ipl()
        elif misc == 4:
            self.usr &= ~(USR_RB | USR_FE | USR_PE | USR_OE)
        self.update_tx_int()
        self._uart_rx_next()
