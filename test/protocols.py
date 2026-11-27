"""Protocol peers driven solely by Tiny Tapeout pins, usable on the gate netlist."""
from pathlib import Path
import sys
import cocotb
from host import Board

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.asm import assemble


async def start(dut, name, tx=()):
    board = Board(dut)
    await board.reset()
    await board.load(assemble((ROOT / f"examples/{name}.asm").read_text()), tx)
    return board


def octet(bits):
    assert len(bits) == 8
    return sum(bit << (7 - index) for index, bit in enumerate(bits))


@cocotb.test()
async def uart_receiver(dut):
    board = await start(dut, "rx")

    async def drive(bit, clocks):
        for _ in range(clocks):
            await board.tick(bit)
            assert board.oe == 0

    await drive(1, 12)
    await drive(0, 48)
    await drive(1, 240)
    bits = []
    for byte, stop in ((0x93, 1), (0x4e, 1), (0xd5, 0)):
        bits.extend([0] + [byte >> i & 1 for i in range(8)] + [stop])
    for i, bit in enumerate(bits):
        await drive(bit, round((i+1)*625/3) - round(i*625/3))
    assert board.out & 0xc0 == 0x80
    await board.frame(0x17000000)
    assert (await board.frame(0)) & 0xffff == 0x0180
    assert await board.receive(2) == bytes([0x93, 0x4e])


@cocotb.test()
@cocotb.parametrize(mode=range(4))
async def spi_controller(dut, mode):
    tx = bytes.fromhex("96 3c a5 81 00 ff 7e 42")
    rx = bytes.fromhex("69 18 5a 01 ff 00 3d c2")
    bits = [b >> i & 1 for b in rx for i in reversed(range(8))]
    board = await start(dut, f"spi{mode}", tx)
    polarity, phase = divmod(mode, 2)
    sampled, edges, selects = [], [], []
    miso = 0
    for _ in range(1700):
        old = board.pads()
        await board.tick(0xfb | miso << 2)
        new = board.pads()
        assert board.oe & 4 == 0
        if (new ^ old) & 8:
            selects.append((new >> 3) & 1)
            assert new & 1 == polarity
            if new & 8 == 0 and phase == 0:
                miso = bits[0]
        if new & 8 == 0 and (old ^ new) & 1:
            edges.append(board.cycle)
            leading = new & 1 != polarity
            if leading != bool(phase):
                sampled.append(new >> 1 & 1)
            elif len(sampled) < 64:
                miso = bits[len(sampled)]
        if board.out & 64 == 0:
            break
    assert selects == [0, 1] and len(edges) == 128
    assert all(b-a == 12 for a,b in zip(edges, edges[1:]))
    assert bytes(octet(sampled[i:i+8]) for i in range(0,64,8)) == tx
    assert await board.receive(8) == rx


@cocotb.test()
@cocotb.parametrize(name=("fast","std"), scenario=("prompt","stretch","nack"))
async def i2c_controller(dut, name, scenario):
    period, low, high_delay = (60,40,14) if name == "fast" else (240,132,96)
    tx, rx = bytes.fromhex("a4 37 a5"), bytes.fromhex("96 4b")
    board = await start(dut, name, tx)
    external = bus = 3
    segment, pulses, stops, stretches = -1, 0, 0, 0
    falling = rising = None
    partial, groups = [], []
    for _ in range(16000):
        await board.tick(external)
        assert board.data & 3 == 0
        lag = 17 if scenario == "stretch" and pulses % 9 == 8 else 0
        hold = falling is not None and rising is None and board.cycle < falling+low+lag
        external = external & 1 | (0 if hold else 2)
        new = board.pads(external) & 3
        if bus & new & 2 and (bus ^ new) & 1:
            if new & 1:
                stops += 1
            else:
                segment += 1
                pulses = 0
            partial = []
            falling = rising = None
        if segment >= 0 and (bus ^ new) & 2:
            group, index = divmod(pulses, 9)
            if new & 2:
                rising = board.cycle
                assert rising - falling == low + lag
                stretches += int(lag > 0)
                partial.append(new & 1)
                pulses += 1
                if len(partial) == 9:
                    groups.append((octet(partial[:8]), partial[8]))
                    partial = []
            else:
                if rising is not None:
                    assert board.cycle == max(falling+period, rising+high_delay+6)
                falling, rising = board.cycle, None
                sda = 1
                if index == 8 and ((segment == 0 and group < 2) or
                                   (segment == 1 and group == 0)):
                    sda = int(scenario == "nack" and segment == 0 and group == 1)
                elif segment == 1 and group in (1,2) and index < 8:
                    sda = rx[group-1] >> (7-index) & 1
                external = external & 2 | sda
        bus = board.pads(external) & 3
        if board.out & 64 == 0:
            break
    assert stops == 1 and bus == 3 and board.oe == 0
    if scenario == "nack":
        assert groups == [(tx[0],0),(tx[1],1)]
        await board.frame(0x17000000)
        assert (await board.frame(0)) & 0xffff == 0x0280
    else:
        assert segment == 1 and groups == [(b,0) for b in tx] + [(rx[0],0),(rx[1],1)]
        assert stretches == (5 if scenario == "stretch" else 0)
        assert await board.receive(2) == rx
