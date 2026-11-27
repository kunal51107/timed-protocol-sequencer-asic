"""MicroPython host for the sequencer. Pass a configured mode-0 MSB-first SPI
object (at most 1 MHz) and initialized machine.Pin objects for CS_N and READY.
Use distinct pins for the protocol under test. The carrier supplies core clk.
"""
from time import sleep_us, ticks_diff, ticks_ms


class Sequencer:
    def __init__(self, spi, select_n, ready):
        self.spi = spi
        self.select_n = select_n
        self.ready = ready
        self.select_n(1)

    def frame(self, request):
        tx = request.to_bytes(4, "big")
        rx = bytearray(4)
        self.select_n(0)
        try:
            sleep_us(2)
            self.spi.write_readinto(tx, rx)
            sleep_us(2)
        finally:
            self.select_n(1)
            sleep_us(2)
        return int.from_bytes(rx, "big")

    def command(self, request):
        self.frame(request)
        reply = self.frame(0)
        if reply >> 24:
            raise ValueError("command %08x rejected: reply %08x" % (request, reply))
        return reply

    def load(self, path, mask, crc, timeout_ms=1000):
        with open(path, "rb") as source:
            data = source.read(257)
        if len(data) != 256 or not 0 <= mask <= 255 or not 0 <= crc <= 65535:
            raise ValueError("expected 256 payload bytes, an 8-bit mask and a 16-bit CRC")
        start = ticks_ms()
        while not self.ready():
            if ticks_diff(ticks_ms(), start) >= timeout_ms:
                raise OSError("host READY timed out")
            sleep_us(10)
        self.command(0x10000000 | (mask << 16))
        for address in range(128):
            word = data[2*address] << 8 | data[2*address+1]
            reply = self.command(0x11000000 | address << 16 | word)
            if reply & 255 != address + 1:
                raise OSError("unexpected loader count at word %d" % address)
        self.command(0x12000000 | crc)

    def run(self, tx=b""):
        if len(tx) > 8:
            raise ValueError("at most eight bytes per preloaded burst")
        for byte in tx:
            self.command(0x20000000 | byte)
        self.command(0x13000000)

    def stop(self):
        self.command(0x14000000)

    def read_rx(self):
        return self.command(0x21000000) & 255
