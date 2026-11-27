# SPDX-FileCopyrightText: © 2026 Kunal
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import sys
import cocotb
from cocotb.triggers import RisingEdge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.asm import assemble


from host import Board


@cocotb.test()
async def test_sequencer_pin_level(dut):
    board = Board(dut)
    await board.reset()

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
    await board.frame(0x10000000 | (image.od << 16))
    for pc, word in enumerate(image.words):
        await board.frame(0x11000000 | (pc << 16) | word)
    await board.frame(0x12000000 | (image.crc ^ 0xFFFF))
    bad_commit_reply = await board.frame(0x00000000)  # POLL reads COMMIT mailbox
    assert (bad_commit_reply >> 24) == 5, f"Expected CRC reject rc=5, got {bad_commit_reply:#010x}"
    assert ((int(dut.uo_out.value) >> 7) & 1) == 1, "Expected H_IRQ=1 on CRC error"
    await board.frame(0x16000000)  # CLEAR
    assert ((int(dut.uo_out.value) >> 7) & 1) == 0, "Expected H_IRQ=0 after CLEAR"

    # 4. Load valid 128-word image and COMMIT
    await board.frame(0x10000000 | (image.od << 16))
    for pc, word in enumerate(image.words):
        await board.frame(0x11000000 | (pc << 16) | word)
    await board.frame(0x12000000 | image.crc)
    commit_reply = await board.frame(0x00000000)  # POLL reads COMMIT mailbox
    assert (commit_reply >> 24) == 0, f"Expected valid COMMIT rc=0, got {commit_reply:#010x}"

    # 5. Push payload byte 0xA5, START execution, and verify pin & RX FIFO results
    await board.frame(0x200000A5)
    await board.frame(0x13000000)
    start_reply = await board.frame(0x00000000)
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

    await board.frame(0x21000000)  # RX_READ
    rx_reply = await board.frame(0x00000000)  # POLL reads RX_READ mailbox
    assert (rx_reply >> 24) == 0
    assert (rx_reply & 0xFF) == 0xA5, f"Expected RX byte 0xA5, got {rx_reply:#010x}"
    assert ((int(dut.uo_out.value) >> 7) & 1) == 0, "Expected H_IRQ=0 after draining RX FIFO"