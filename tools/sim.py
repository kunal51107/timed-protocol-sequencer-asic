"""24 MHz Timed Protocol Sequencer model. One tick() is one rising edge.

ui_in is the physical dedicated input byte. uio_in, when supplied, is the
actual pad byte captured at this edge. Otherwise enabled P drivers feed back
and released pins see pullups (default 0xff). set_reset() acts immediately.
command() is a fast path for an already decoded host frame, costing exactly
its acceptance edge. Drive CS/SCK/MOSI through tick() for wire timing.
"""

import argparse
from contextlib import contextmanager
from enum import IntEnum
from pathlib import Path

if __package__:
    from .asm import assemble, crc16
else:
    from asm import assemble, crc16


class Mode(IntEnum):
    EMPTY, LOAD, READY, RUN, FAULT = range(5)


class FIFO:
    def __init__(self):
        self.cells = [0] * 8
        self.clear()

    def clear(self):
        self.rd = self.wr = self.count = 0

    def put(self, byte):
        assert self.count < 8
        self.cells[self.wr] = byte
        self.wr = (self.wr + 1) & 7
        self.count += 1

    def pop(self):
        assert self.count > 0
        byte = self.cells[self.rd]
        self.rd = (self.rd + 1) & 7
        self.count -= 1
        return byte


# Allowed argument bits and valid initial modes, in rejection-priority order.
ANY = tuple(Mode)
ACTIVE = (Mode.READY, Mode.RUN)
STOPPABLE = (*ACTIVE, Mode.FAULT)
COMMANDS = {
    0x00: (0, ANY), 0x10: (0xFF0000, ANY),
    0x11: (0xFFFFFF, (Mode.LOAD,)), 0x12: (0xFFFF, (Mode.LOAD,)),
    0x13: (0, (Mode.READY,)), 0x14: (0, STOPPABLE),
    0x15: (3, (Mode.READY,)), 0x16: (0, ANY), 0x17: (0, ANY),
    0x18: (0, ANY), 0x19: (0, ANY), 0x20: (0xFF, ACTIVE),
    0x21: (0, STOPPABLE), 0x22: (0, (Mode.READY, Mode.FAULT)),
}


class Simulator:
    def __init__(self):
        self.cycle = 0
        self._trace = None
        self.rst_n = self.ena = True
        self.ui_in = 0x10
        self.uio_in = 0
        self._reset()

    def _execution_reset(self):
        self.pc = self.t = self.r = self.x = self.y = self.b = self.q = 0
        self.age = self.delay = self.v = self.e = self.depth = 0
        self.stack = [0, 0]
        self.p_data = self.p_oe = self.o = 0

    def _reset(self):
        self.release = 0
        self._execution_reset()
        self.mode = Mode.EMPTY
        self.imem = [0] * 128
        self.tx, self.rx = FIFO(), FIFO()
        self.cfg = 3
        self.valid = self.poison = False
        self.errors = self.detail = self.od = self.pending_od = 0
        self.accepted = self.crc = 0
        self.f1 = self.f2 = self.h = 1 << 12
        self.ready = self.frame = False
        self.bits = self.request = self.response_shift = self.miso = 0
        self.mailbox = 0x00050000

    def set_reset(self, rst_n=True, ena=True):
        self.rst_n, self.ena = bool(rst_n), bool(ena)
        if not (self.rst_n and self.ena):
            self._reset()
            if self._trace:
                self._trace()

    @property
    def busy(self):
        return self.mode == Mode.RUN

    @property
    def irq(self):
        return bool(self.errors or self.rx.count)

    @property
    def uo_out(self):
        return self.o | self.miso << 4 | self.ready << 5 | self.busy << 6 | self.irq << 7

    @property
    def status(self):
        return ((self.mode == Mode.LOAD) << 7 | self.valid << 6 |
                bool(self.errors) << 5 | self.busy << 4 |
                (self.rx.count == 8) << 3 | (self.rx.count == 0) << 2 |
                (self.tx.count == 8) << 1 | (self.tx.count == 0))

    def pads(self, released=0xFF):
        return (self.p_data & self.p_oe) | (released & ~self.p_oe & 0xFF)

    def _wire(self):
        cs, sck, mosi = (self.f2 >> n & 1 for n in (12, 13, 14))
        changed = self.f2 ^ self.h
        if not self.ready:
            self.ready = bool(cs and not sck)
        elif changed & (1 << 12):
            if not cs:
                self.frame = True
                self.bits = self.request = 0
                self.response_shift = self.mailbox
                self.miso = self.response_shift >> 31
            elif self.frame:
                self.frame = False
                self.miso = 0
                return self.request, self.bits
        elif self.frame and changed & (1 << 13):
            if sck:
                self.request = (self.request << 1 | mosi) & 0xFFFFFFFF
                self.bits = min(33, self.bits + 1)
            else:
                self.response_shift = (self.response_shift << 1) & 0xFFFFFFFF
                self.miso = self.response_shift >> 31
        return None

    def _reject_code(self, op, arg, length):
        if length != 32:
            return 1
        if op not in COMMANDS:
            return 2
        allowed, modes = COMMANDS[op]
        if arg & ~allowed:
            return 2
        if self.mode not in modes:
            return 3
        if op == 0x11 and (self.poison or self.accepted >= 128 or arg >> 16 != self.accepted):
            return 4
        if op == 0x12:
            if self.poison or self.accepted != 128:
                return 4
            if arg != self.crc:
                return 5
        if op == 0x20 and self.tx.count == 8:
            return 6
        if op == 0x21 and self.rx.count == 0:
            return 7
        return 0

    def _host(self, word, length):
        op, arg = word >> 24, word & 0xFFFFFF
        rc = self._reject_code(op, arg, length)
        if rc:
            self.errors |= 1 if rc <= 3 else 2 if rc <= 5 else 4
            if self.mode == Mode.LOAD:
                self.poison = True
            return rc, 0, False
        data = 0
        if op == 0x00:
            data = self.mode << 8 | self.cfg
        elif op == 0x10:
            self._execution_reset()
            self.tx.clear()
            self.rx.clear()
            self.od = self.accepted = self.detail = self.errors = 0
            self.pending_od = arg >> 16
            self.crc = crc16([self.pending_od])
            self.valid = self.poison = False
            self.cfg, self.mode = 3, Mode.LOAD
        elif op == 0x11:
            self.imem[self.accepted] = arg & 0xFFFF
            self.crc = crc16((arg >> 8 & 255, arg & 255), self.crc)
            self.accepted += 1
            data = self.accepted
        elif op == 0x12:
            self.valid = True
            self.od, self.mode = self.pending_od, Mode.READY
        elif op in (0x13, 0x14):
            self._execution_reset()
            self.mode = Mode.RUN if op == 0x13 else Mode.READY
        elif op == 0x15:
            self.cfg = arg
        elif op == 0x16:
            self.detail = self.errors = 0
        elif op == 0x17:
            data = self.detail << 8 | self.errors
        elif op == 0x18:
            data = self.tx.count << 8 | self.rx.count
        elif op == 0x19:
            data = self.pc << 8 | self.accepted
        elif op == 0x20:
            self.tx.put(arg)
        elif op == 0x21:
            data = self.rx.pop()
        elif op == 0x22:
            self.tx.clear()
            self.rx.clear()
        return 0, data, op in (0x10, 0x13, 0x14)

    def _fault(self, bit):
        self.mode = Mode.FAULT
        self.errors |= 1 << bit
        self.p_data = self.p_oe = self.o = 0

    def _pin(self, pin, action):
        if action == 3:
            value = self.t & 1
            self.t >>= 1
            self.q ^= value
        elif action == 4:
            value = self.t >> 7
            self.t = self.t << 1 & 255
            self.q ^= value
        else:
            value = {0: 0, 1: 0, 2: 1, 5: self.q, 6: self.b}[action]
        if pin >= 8:
            mask = 1 << (pin - 8)
            self.o = (self.o & ~mask) | (mask if value else 0)
        else:
            mask = 1 << pin
            od = bool(self.od & mask)
            self.p_data = (self.p_data & ~mask) | (mask if value and not od else 0)
            enabled = action != 0 and not (od and value)
            self.p_oe = (self.p_oe & ~mask) | (mask if enabled else 0)

    def _execute(self, tx_count, rx_count):
        word = self.imem[self.pc]
        op, sub = word >> 12, word & 0xFFF
        reg, byte = sub >> 8, sub & 255
        dest = sub & 127
        next_pc = (self.pc + 1) & 127
        self.age = min(4095, self.age + 1)
        if op == 0:
            if sub == 0:
                self.mode = Mode.READY
                return
            if sub == 2:
                self.age = 0
            elif sub == 3:
                if not self.depth:
                    return self._fault(6)
                self.depth -= 1
                next_pc = self.stack[self.depth]
            elif sub == 4:
                if not tx_count:
                    if not self.cfg & 1:
                        self._fault(3)
                    return
                self.t = self.tx.pop()
            elif sub == 5:
                if rx_count == 8:
                    if self.cfg & 2:
                        return self._fault(4)
                    self.errors |= 1 << 4
                else:
                    self.rx.put(self.r)
            elif sub == 6:
                self.r = 0
            elif sub == 7:
                self.t = self.r
            elif sub == 8:
                self.r = self.t
            elif sub in (9, 10, 11):
                self.q = (0, 1, self.b)[sub - 9]
            elif sub > 11:
                return self._fault(5)
        elif op == 1:
            self.delay = self.delay - 1 if self.delay else sub
            if self.delay:
                return
        elif op in (2, 11):
            if self.age < sub:
                return
            if op == 11:
                self.age = 0
        elif op in (3, 4):
            pin, action = sub >> 4, sub & 15
            if pin >= 12 or action >= 7 or (op == 3 and pin >= 8 and action == 0):
                return self._fault(5)
            if op == 3:
                self._pin(pin, action)
            else:
                level, old = self.f2 >> pin & 1, self.h >> pin & 1
                if action < 4:
                    if not (level == 0, level == 1, level > old, level < old)[action]:
                        return
                else:
                    self.b = level
                    if action == 5:
                        self.r = (self.r << 1 | level) & 255
                    elif action == 6:
                        self.r = self.r >> 1 | level << 7
                    if action != 4:
                        self.q ^= level
        elif op == 5 and reg < 4:
            setattr(self, "trxy"[reg], byte)
        elif op == 6 and sub >> 7 <= 10:
            conditions = (True, self.b, not self.b, self.q, not self.q,
                          self.t & 1, self.t >> 7, tx_count == 0,
                          rx_count == 8, self.x == 0, self.y == 0)
            if conditions[sub >> 7]:
                next_pc = dest
        elif op == 7 and reg == 0:
            name = "y" if sub & 128 else "x"
            value = (getattr(self, name) - 1) & 255
            setattr(self, name, value)
            if value:
                next_pc = dest
        elif op == 8 and sub < 128:
            if self.depth == 2:
                return self._fault(6)
            self.stack[self.depth] = next_pc
            self.depth += 1
            next_pc = dest
        elif op == 9 and reg == 0:
            self.detail = byte
            return self._fault(7)
        elif op == 10 and reg < 2:
            setattr(self, "ve"[reg], byte)
        elif word == 0xA200:
            self.p_data = self.v & ~self.od
            self.p_oe = self.e & ~(self.od & self.v)
        else:
            return self._fault(5)
        self.pc = next_pc

    def tick(self, ui_in=0x10, uio_in=None, *, pullups=0xFF, decoded=None):
        """Advance one edge. Return the published response, or None.

        decoded=(request_word, rise_count) substitutes a completed frame.
        Use this only on an armed, idle host port. Pad arguments describe
        levels immediately before the edge, independent of its output changes.
        """
        if decoded is not None:
            idle = all(stage & 0x3000 == 0x1000 for stage in (self.f1, self.f2, self.h))
            if not (self.ready and idle) or self.frame or ui_in & 0x30 != 0x10:
                raise ValueError("decoded frame requires an armed idle host port")
        self.ui_in = ui_in & 255
        self.uio_in = self.pads(pullups) if uio_in is None else uio_in & 255
        self.cycle += 1
        reply = None
        if self.rst_n and self.ena:
            if self.release != 3:
                self.release = (self.release << 1 | 1) & 3
            else:
                event = self._wire() if decoded is None else decoded
                tx_count, rx_count = self.tx.count, self.rx.count
                suppress = False
                if event is not None:
                    rc, data, suppress = self._host(*event)
                if self.busy and not suppress:
                    self._execute(tx_count, rx_count)
                if event is not None:
                    reply = self.mailbox = rc << 24 | self.status << 16 | data
                pads = self.uio_in | (self.ui_in & 0x7F) << 8
                self.h, self.f2, self.f1 = self.f2, self.f1, pads
        if self._trace:
            self._trace()
        return reply

    def command(self, op, a=0, hi=0, lo=0, **pins):
        word = int.from_bytes(bytes((op, a, hi, lo)), "big")
        return self.tick(decoded=(word, 32), **pins)

    def load(self, image):
        """BEGIN, 128 WORDs and COMMIT through the decoded command interface."""
        if len(image.words) != 128:
            raise ValueError("expected a padded 128-word image")
        def accepted(*args):
            reply = self.command(*args)
            if reply >> 24:
                raise ValueError(f"loader rejected command {args[0]:02X}: {reply:08X}")
        accepted(0x10, image.od)
        for pc, word in enumerate(image.words):
            accepted(0x11, pc, word >> 8, word & 255)
        checksum = image.crc
        accepted(0x12, 0, checksum >> 8, checksum & 255)

    @contextmanager
    def vcd(self, path):
        """Stream registered post-edge values and sampled pad inputs at 24 MHz."""
        signals = {
            "rst_n": 1, "ena": 1, "release": 2, "ui_in": 8, "uio_in": 8,
            "uo_out": 8, "p_data": 8, "p_oe": 8, "mode": 3, "pc": 7,
            "f1": 15, "f2": 15, "h": 15, "t": 8, "r": 8, "x": 8, "y": 8,
            "b": 1, "q": 1, "age": 12, "delay": 12, "depth": 2, "v": 8,
            "e": 8, "od": 8, "tx.count": 4, "rx.count": 4, "errors": 8,
            "detail": 8, "ready": 1, "frame": 1, "bits": 6, "mailbox": 32,
        }
        if self._trace:
            raise ValueError("trace already open")
        with Path(path).open("w", encoding="ascii") as out:
            out.write("$timescale 1 fs $end\n$scope module sequencer $end\n")
            for i, (name, width) in enumerate(signals.items()):
                out.write(f"$var wire {width} s{i} {name.replace('.', '_')} $end\n")
            out.write("$upscope $end\n$enddefinitions $end\n")
            previous = {}

            def sample():
                changes = []
                for i, (name, width) in enumerate(signals.items()):
                    value = self
                    for part in name.split("."):
                        value = getattr(value, part)
                    if previous.get(name) != value:
                        changes.append(f"b{int(value):0{width}b} s{i}\n")
                        previous[name] = value
                if changes:
                    time = self.cycle * 10**15 // 24_000_000
                    out.write(f"#{time}\n")
                    out.writelines(changes)
            self._trace = sample
            try:
                sample()
                yield self
            finally:
                self._trace = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--vcd", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=10000, help="execution edge limit")
    parser.add_argument("--tx", type=bytes.fromhex, default=b"", help="preload up to 8 hex bytes")
    args = parser.parse_args()
    if len(args.tx) > 8 or args.cycles < 0:
        parser.error("expected up to 8 TX bytes and a nonnegative cycle limit")
    try:
        image = assemble(args.source.read_text(encoding="utf-8-sig"))
        sim = Simulator()
        with sim.vcd(args.vcd):
            for _ in range(3):
                sim.tick()
            sim.load(image)
            for byte in args.tx:
                sim.command(0x20, lo=byte)
            sim.command(0x13)
            for _ in range(args.cycles):
                if not sim.busy:
                    break
                sim.tick()
        print(f"{sim.mode.name} PC={sim.pc:02X} ERR={sim.errors:02X} RX={sim.rx.count}")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
