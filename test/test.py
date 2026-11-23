# SPDX-FileCopyrightText: © 2026 Kunal
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.asm import assemble


async def spi_frame(dut, word, pullups=0xFF):
    """Send one 32-bit Mode-0 SPI frame on ui_in[6:4] and return the 32-bit MISO mailbox."""
    def drive(cs, sck, mosi):
        dut.ui_in.value = (cs << 4) | (sck << 5) | (mosi << 6)
        out = int(dut.uo_out.value) if str(dut.uo_out.value).isdigit() else 0
        p_data = int(dut.uio_out.value) if str(dut.uio_out.value).isdigit() else 0
        p_oe = int(dut.uio_oe.value) if str(dut.uio_oe.value).isdigit() else 0
        dut.uio_in.value = (p_data & p_oe) | (pullups & ~p_oe & 0xFF)
        return out

    drive(0, 0, 0)
    await ClockCycles(dut.clk, 6)

    reply = 0
    for i in range(31, -1, -1):
        bit = (word >> i) & 1
        drive(0, 0, bit)
        await ClockCycles(dut.clk, 6)
        out = drive(0, 1, bit)
        reply = (reply << 1) | ((out >> 4) & 1)
        await ClockCycles(dut.clk, 6)

    drive(0, 0, 0)
    await ClockCycles(dut.clk, 6)
    drive(1, 0, 0)
    await ClockCycles(dut.clk, 6)
    return reply


@cocotb.test()
async def test_sequencer_pin_level(dut):
    clock = Clock(dut.clk, 42, unit="ns")  # ~23.8 MHz
    cocotb.start_soon(clock.start())

    # 1. Asynchronous reset & E1/E2/E3 release check
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0x10
    dut.uio_in.value = 0xFF
    await ClockCycles(dut.clk, 4)
    assert int(dut.uo_out.value) == 0
    assert int(dut.uio_oe.value) == 0

    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 4)
    uo = int(dut.uo_out.value)
    assert (uo >> 5) & 1 == 1, f"Expected H_READY=1 after reset release, got uo_out={uo:#04x}"
    assert (uo >> 6) & 3 == 0, f"Expected H_BUSY=0, H_IRQ=0 after reset, got uo_out={uo:#04x}"

    # 2. Assemble test image with open-drain P0 (.od 1)
    program = """
    .od 1
    PULL
    T2R
    PUSH
    HI O0
    LO P0
    HI P0
    HALT
    """
    image = assemble(program)

    # 3. Verify bad CRC rejection sets H_IRQ and CLEAR recovers cleanly
    await spi_frame(dut, 0x10000000 | (image.od << 16))
    for pc, word in enumerate(image.words):
        await spi_frame(dut, 0x11000000 | (pc << 16) | word)
    await spi_frame(dut, 0x12000000 | (image.crc ^ 0xFFFF))
    bad_commit_reply = await spi_frame(dut, 0x00000000)  # POLL reads COMMIT mailbox
    assert (bad_commit_reply >> 24) == 5, f"Expected CRC reject rc=5, got {bad_commit_reply:#010x}"
    assert ((int(dut.uo_out.value) >> 7) & 1) == 1, "Expected H_IRQ=1 on CRC error"
    await spi_frame(dut, 0x16000000)  # CLEAR
    assert ((int(dut.uo_out.value) >> 7) & 1) == 0, "Expected H_IRQ=0 after CLEAR"

    # 4. Load valid 128-word image and COMMIT
    await spi_frame(dut, 0x10000000 | (image.od << 16))
    for pc, word in enumerate(image.words):
        await spi_frame(dut, 0x11000000 | (pc << 16) | word)
    await spi_frame(dut, 0x12000000 | image.crc)
    commit_reply = await spi_frame(dut, 0x00000000)  # POLL reads COMMIT mailbox
    assert (commit_reply >> 24) == 0, f"Expected valid COMMIT rc=0, got {commit_reply:#010x}"

    # 5. Push payload byte 0xA5, START execution, and verify pin & RX FIFO results
    await spi_frame(dut, 0x200000A5)
    await spi_frame(dut, 0x13000000)
    start_reply = await spi_frame(dut, 0x00000000)
    assert (start_reply >> 24) == 0, f"Expected valid START rc=0, got {start_reply:#010x}"

    for _ in range(30):
        await RisingEdge(dut.clk)
        p_data = int(dut.uio_out.value)
        p_oe = int(dut.uio_oe.value)
        dut.uio_in.value = (p_data & p_oe) | (0xFF & ~p_oe)
        if ((int(dut.uo_out.value) >> 6) & 1) == 0:  # H_BUSY dropped
            break

    uo_final = int(dut.uo_out.value)
    assert (uo_final & 0x01) == 1, f"Expected O0=1, got uo_out={uo_final:#04x}"
    assert ((uo_final >> 6) & 1) == 0, "Expected H_BUSY=0 after HALT"
    assert ((uo_final >> 7) & 1) == 1, "Expected H_IRQ=1 with RX data pending"
    assert (int(dut.uio_oe.value) & 0x01) == 0, "Expected open-drain P0 released after HI P0"

    await spi_frame(dut, 0x21000000)  # RX_READ
    rx_reply = await spi_frame(dut, 0x00000000)  # POLL reads RX_READ mailbox
    assert (rx_reply >> 24) == 0
    assert (rx_reply & 0xFF) == 0xA5, f"Expected RX byte 0xA5, got {rx_reply:#010x}"
    assert ((int(dut.uo_out.value) >> 7) & 1) == 0, "Expected H_IRQ=0 after draining RX FIFO"