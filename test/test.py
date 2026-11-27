"""Decode the complete UART frame at the physical P0 driver."""
from pathlib import Path
import sys
import cocotb
from host import Board

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.asm import assemble


@cocotb.test()
async def host_loaded_uart_tx(dut):
    board = Board(dut)
    await board.reset()
    image = assemble((ROOT / "examples/tx.asm").read_text())
    data = (0xA5, 0x5A)
    await board.load(image, data)
    assert board.oe == 1 and board.data == 1
    for byte in data:
        for _ in range(220):
            if board.data == 0:
                break
            await board.tick()
        else:
            raise AssertionError("UART start bit missing")
        levels = [0] + [byte >> i & 1 for i in range(8)]
        for bit, width in zip(levels, [208, 208, 209] * 3):
            for _ in range(width):
                assert board.oe == 1 and board.data == bit
                await board.tick()
        for _ in range(209):
            assert board.data == 1 and board.oe == 1
            await board.tick()
    for _ in range(24):
        await board.tick()
    assert board.out & 0xC0 == 0x40
    assert board.pads() & 1
