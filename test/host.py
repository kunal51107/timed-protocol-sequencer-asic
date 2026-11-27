"""Mode-0 host and external pad resolver for RTL and gate-level cocotb."""
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge
import cocotb


class Board:
    def __init__(self, dut):
        self.dut = dut
        self.cycle = 0

    async def reset(self):
        dut = self.dut
        dut.ena.value = 1
        dut.rst_n.value = 0
        dut.ui_in.value = 16
        dut.uio_in.value = 255
        cocotb.start_soon(Clock(dut.clk, 41666, unit="ps").start())
        await ClockCycles(dut.clk, 4)
        await FallingEdge(dut.clk)
        assert self.oe == 0 and self.out == 0
        dut.rst_n.value = 1
        for expected in (0, 0, 32):
            await self.tick()
            assert self.out == expected

    @property
    def out(self):
        return int(self.dut.uo_out.value)

    @property
    def data(self):
        return int(self.dut.uio_out.value)

    @property
    def oe(self):
        return int(self.dut.uio_oe.value)

    def pads(self, released=255):
        return (self.data & self.oe) | (released & ~self.oe & 255)

    async def tick(self, released=255):
        self.dut.uio_in.value = self.pads(released)
        await FallingEdge(self.dut.clk)
        self.cycle += 1

    async def frame(self, word):
        dut = self.dut
        async def phase(ui, clocks):
            dut.ui_in.value = ui
            for _ in range(clocks):
                await FallingEdge(dut.clk)
            self.cycle += clocks

        await phase(0, 8)
        reply = 0
        for index in range(31, -1, -1):
            mosi = (word >> index & 1) << 6
            await phase(mosi, 12)
            reply = reply << 1 | (self.out >> 4 & 1)
            await phase(mosi | 32, 12)
        await phase(0, 8)
        await phase(16, 8)
        return reply

    async def load(self, image, tx=()):
        reply = await self.frame(0x10000000 | image.od << 16)
        assert reply >> 24 == 0
        for address, word in enumerate(image.words):
            reply = await self.frame(0x11000000 | address << 16 | word)
            assert reply >> 24 == 0 and reply & 255 == address
        reply = await self.frame(0x12000000 | image.crc)
        assert reply >> 24 == 0 and reply & 255 == 128
        reply = await self.frame(0)
        assert reply == 0x00450000
        for byte in tx:
            assert (await self.frame(0x20000000 | byte)) >> 24 == 0
        assert (await self.frame(0x13000000)) >> 24 == 0
        assert self.out & 0x40

    async def receive(self, count):
        await self.frame(0x21000000)
        result = []
        for i in range(count):
            reply = await self.frame(0x21000000 if i + 1 < count else 0)
            assert reply >> 24 == 0
            result.append(reply & 255)
        return bytes(result)
