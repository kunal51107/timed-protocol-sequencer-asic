"""Run with python -m pytest -q, or directly with Python's unittest runner."""

from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.asm import assemble, crc16
from tools.sim import Mode, Simulator


def reference_listings():
    """Read the independent source and golden-word columns from the frozen spec."""
    spec = (ROOT / "SPEC.md").read_text(encoding="utf-8-sig")
    section = spec.split("## 5. Reference programs", 1)[1].split("## 6.", 1)[0]
    blocks = re.findall(r"```text\s*\nPC[^\n]*\n(.*?)\n```", section, re.S)
    groups = [("tx",), ("rx",), ("spi0", "spi2"), ("spi1", "spi3"), ("fast", "std")]
    assert len(blocks) == len(groups)
    result = {}
    for block, names in zip(blocks, groups):
        for column, name in enumerate(names):
            lines = [".od 3" if name in ("fast", "std") else ".od 0"]
            words = []
            for row in block.splitlines():
                fields = row.split(None, len(names) + 1)
                assert int(fields[0], 16) == len(words)
                words.append(int(fields[1 + column], 16))
                variants = re.split(r"\s+/\s+", fields[-1])
                lines.append(variants[column] if len(variants) > 1 else variants[0])
            result[name] = ("\n".join(lines), words)
    return result


LISTINGS = reference_listings()
GOLD = {"tx": (24, 0x7E24), "rx": (28, 0x74FF), "spi0": (26, 0x008D),
        "spi1": (25, 0xE36B), "spi2": (26, 0x9868), "spi3": (25, 0xBB84),
        "fast": (75, 0x9BAF), "std": (75, 0x1139)}


def new_sim():
    sim = Simulator()
    for _ in range(3):
        sim.tick()
    assert sim.ready
    return sim


def running(source, tx=(), cfg=3):
    sim = new_sim()
    sim.load(assemble(source))
    sim.command(0x15, lo=cfg)
    for byte in tx:
        assert sim.command(0x20, lo=byte) >> 24 == 0
    sim.command(0x13)
    return sim


def octet(bits):
    return sum(bit << (7 - i) for i, bit in enumerate(bits))


def read_rx(sim, count):
    result = []
    for _ in range(count):
        reply = sim.command(0x21)
        assert reply >> 24 == 0
        result.append(reply & 255)
    return bytes(result)


class AssemblyTests(unittest.TestCase):
    def test_reference_images(self):
        self.assertEqual(LISTINGS.keys(), GOLD.keys())
        for name, (used, checksum) in GOLD.items():
            with self.subTest(image=name):
                source, golden_words = LISTINGS[name]
                image = assemble(source)
                example = ROOT / "examples" / (name + ".asm")
                self.assertEqual(assemble(example.read_text(encoding="utf-8")), image)
                self.assertEqual(image.used, used)
                self.assertEqual(list(image.words), golden_words + [0] * (128 - used))
                self.assertEqual(image.crc, checksum)
                sim = new_sim()
                sim.load(image)
                self.assertEqual(sim.mode, Mode.READY)
                self.assertEqual(sim.crc, checksum)
                self.assertEqual(sim.imem, list(image.words))

    def test_crc_and_syntax(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)
        self.assertEqual(assemble("").crc, 0xB0E5)
        image = assemble("j end\n.od 0xff\nouth P1\nend: LIT X,08\nINR I0\nHI O0")
        self.assertEqual(image.words[:5], (0x6002, 0x3014, 0x5208, 0x4086, 0x3082))
        for source in ("D 0", "D 4097", "br 11,0", "call 128", "LX -1", "get P8",
                       "Z O0", "get O0", "HI I0", "NOP 1", "LIT T,,2", "J absent",
                       "a: a: nop", ".od 256", ".od 1\n.od 2", "nop\n" * 129):
            with self.subTest(source=source), self.assertRaises(ValueError):
                assemble(source)


class ProtocolTests(unittest.TestCase):
    def test_uart_tx(self):
        data = (0xA5, 0x5A)
        sim = running(LISTINGS["tx"][0], data)
        sim.tick()
        self.assertEqual((sim.p_oe, sim.p_data), (1, 1))
        durations = [208, 208, 209] * 3
        for byte in data:
            for _ in range(220):
                if sim.p_data == 0:
                    break
                sim.tick()
            self.assertEqual(sim.p_data, 0)
            for level, duration in zip([0] + [(byte >> i) & 1 for i in range(8)], durations):
                for _ in range(duration):
                    self.assertEqual((sim.p_oe, sim.p_data), (1, level))
                    sim.tick()
            for _ in range(209):
                self.assertEqual(sim.p_data, 1)
                sim.tick()
        for _ in range(20):
            sim.tick()
        self.assertEqual((sim.busy, sim.pc, sim.p_data, sim.errors), (True, 2, 1, 0))

    def test_uart_rx(self):
        for scale in (0.98, 1.0, 1.02):
            with self.subTest(relative_bit_period=scale):
                sim = running(LISTINGS["rx"][0])
                def drive(level, duration):
                    for _ in range(duration):
                        sim.tick(uio_in=level)
                        self.assertEqual(sim.p_oe, 0)
                drive(1, 8)
                drive(0, 50)
                drive(1, 200)
                self.assertEqual((sim.pc, sim.rx.count), (1, 0))
                bits = []
                for byte, stop in ((0x93, 1), (0x4E, 1), (0xD5, 0)):
                    bits.extend([0] + [byte >> i & 1 for i in range(8)] + [stop])
                period = 24_000_000 / 115_200 * scale
                for i, bit in enumerate(bits):
                    drive(bit, round((i + 1) * period) - round(i * period))
                self.assertEqual((sim.mode, sim.errors, sim.detail), (Mode.FAULT, 128, 1))
                self.assertEqual(read_rx(sim, 2), bytes((0x93, 0x4E)))

    def test_spi_modes(self):
        tx = bytes.fromhex("96 3c a5 81 00 ff 7e 42")
        incoming = bytes.fromhex("69 18 5a 01 ff 00 3d c2")
        bits = [(b >> i) & 1 for b in incoming for i in range(7, -1, -1)]
        for mode in range(4):
            with self.subTest(mode=mode):
                sim = running(LISTINGS[f"spi{mode}"][0], tx)
                cpol, cpha = divmod(mode, 2)
                sample, transitions, selects = [], [], []
                miso = 0
                for _ in range(1700):
                    old = sim.pads()
                    sim.tick(uio_in=sim.pads(0xFB | miso << 2))
                    new = sim.pads()
                    self.assertFalse(sim.p_oe & 4)
                    if (new ^ old) & 8:
                        selects.append(new >> 3 & 1)
                        self.assertEqual(new & 1, cpol)
                        if new & 8 == 0 and not cpha:
                            miso = bits[0]
                    if new & 8 == 0 and (old ^ new) & 1:
                        transitions.append(sim.cycle)
                        leading = (new & 1) != cpol
                        if leading != bool(cpha):
                            sample.append(new >> 1 & 1)
                        elif len(sample) < 64:
                            miso = bits[len(sample)]
                    if not sim.busy:
                        break
                self.assertEqual((sim.mode, sim.errors, sim.tx.count), (Mode.READY, 0, 0))
                self.assertEqual(selects, [0, 1])
                self.assertEqual(len(transitions), 128)
                self.assertEqual([b - a for a, b in zip(transitions, transitions[1:])], [12] * 127)
                self.assertEqual(bytes(octet(sample[i:i+8]) for i in range(0, 64, 8)), tx)
                self.assertEqual(read_rx(sim, 8), incoming)

    def i2c_transfer(self, name, nack=None, stretch=False):
        period, low, delay = (60, 40, 14) if name == "fast" else (240, 132, 96)
        tx, incoming = (0xA4, 0x37, 0xA5), (0x96, 0x4B)
        sim = running(LISTINGS[name][0], tx)
        external = 3
        bus = 3
        segment = -1
        pulses = 0
        fall = rise = None
        starts, stops, groups, partial = [], [], [], []
        stretched = 0
        # All peer decisions use physical START/STOP and clock transitions.
        for _ in range(16000):
            sim.tick(uio_in=sim.pads(external))
            self.assertEqual(sim.p_data & 3, 0)
            # Release the peer clock after the programmed physical rise lag.
            lag = 0
            if stretch and fall is not None:
                lag = 17 if pulses % 9 == 8 else (pulses % 3)
            hold = rise is None and fall is not None and sim.cycle < fall + low + lag
            external = (external & 1) | (0 if hold else 2)
            new = sim.pads(external) & 3
            if bus & new & 2 and (bus ^ new) & 1:
                if new & 1:
                    stops.append(sim.cycle)
                else:
                    starts.append(sim.cycle)
                    segment += 1
                    pulses = 0
                partial = []
                fall = rise = None
            if segment >= 0 and (bus ^ new) & 2:
                if new & 2:
                    self.assertIsNotNone(fall)
                    rise = sim.cycle
                    self.assertEqual(rise - fall, low + lag)
                    if lag == 17:
                        stretched += 1
                    group, index = divmod(pulses, 9)
                    if nack is None and segment == 1 and group in (1, 2) and index < 8:
                        self.assertFalse(sim.p_oe & 1)
                    partial.append(new & 1)
                    pulses += 1
                    if len(partial) == 9:
                        groups.append((octet(partial[:8]), partial[8]))
                        partial = []
                else:
                    if rise is not None:
                        self.assertEqual(sim.cycle, max(fall + period, rise + 3 + delay + 3))
                    fall, rise = sim.cycle, None
                    group, index = divmod(pulses, 9)
                    sda = 1
                    if segment == 0 and group < 2 and index == 8:
                        sda = int(nack == group)
                    elif segment == 1 and group == 0 and index == 8:
                        sda = int(nack == 2)
                    elif segment == 1 and group in (1, 2) and index < 8:
                        sda = incoming[group - 1] >> (7 - index) & 1
                    external = (external & 2) | sda
            bus = sim.pads(external) & 3
            if not sim.busy:
                break
        self.assertEqual(len(starts), 2 if nack in (None, 2) else 1)
        self.assertEqual(len(stops), 1)
        self.assertEqual((bus, sim.p_oe), (3, 0))
        if nack is None:
            self.assertEqual(groups, [(v, 0) for v in tx] + [(incoming[0], 0), (incoming[1], 1)])
            self.assertEqual((sim.mode, sim.errors, sim.rx.count), (Mode.READY, 0, 2))
            self.assertEqual(read_rx(sim, 2), bytes(incoming))
            self.assertEqual(stretched, 5 if stretch else 0)
        else:
            self.assertEqual(groups, [(v, int(i == nack)) for i, v in enumerate(tx[:nack+1])])
            self.assertEqual((sim.mode, sim.detail, sim.errors), (Mode.FAULT, 2, 128))

    def test_i2c_transfers(self):
        for name in ("fast", "std"):
            for nack, stretch in ((None, False), (None, True), (0, False), (1, False), (2, False)):
                with self.subTest(image=name, nack=nack, stretch=stretch):
                    self.i2c_transfer(name, nack, stretch)


def wire_frame(sim, request, count=32):
    """Mode-0 host with 12 core cycles per half-clock. Sample physical MISO."""
    def hold(cs, clk, mosi, ticks):
        for _ in range(ticks):
            sim.tick(ui_in=(cs << 4 | clk << 5 | mosi << 6))
    hold(0, 0, 0, 8)
    received = 0
    for i in range(count):
        bit = request >> (count - 1 - i) & 1
        hold(0, 0, bit, 12)
        received = received << 1 | (sim.uo_out >> 4 & 1)
        hold(0, 1, bit, 12)
    hold(0, 0, 0, 8)
    hold(1, 0, 0, 8)
    return received


class HostTests(unittest.TestCase):
    def test_boot_and_execute_over_wire(self):
        sim = new_sim()
        image = assemble(".od 1\nPULL\nT2R\nPUSH\nHI O0\nHALT")
        wire_frame(sim, 0x10010000)
        for index, word in enumerate(image.words):
            reply = wire_frame(sim, 0x11000000 | index << 16 | word)
            self.assertEqual(reply, 0x00850000 | index)
        self.assertEqual(wire_frame(sim, 0x12000000 | image.crc), 0x00850080)
        self.assertEqual(wire_frame(sim, 0), 0x00450000)
        self.assertEqual(wire_frame(sim, 0x200000BC), 0x00450203)
        self.assertEqual(wire_frame(sim, 0x13000000), 0x00440000)
        self.assertEqual(wire_frame(sim, 0x21000000), 0x00540000)
        self.assertEqual(wire_frame(sim, 0), 0x004500BC)
        self.assertEqual((sim.mode, sim.o, sim.pc, sim.errors), (Mode.READY, 1, 4, 0))
        sim.set_reset(rst_n=False)
        self.assertEqual((sim.uo_out, sim.tx.cells, sim.rx.cells), (0, [0] * 8, [0] * 8))

    def test_wire_mailbox_and_reset(self):
        sim = Simulator()
        for expected in (False, False, True):
            sim.tick()
            self.assertEqual(sim.ready, expected)
        self.assertEqual(wire_frame(sim, 0x10030000), 0x00050000)
        self.assertEqual(wire_frame(sim, 0x1100CAFE), 0x00850000)
        self.assertEqual(sim.imem[0], 0xCAFE)
        self.assertEqual(wire_frame(sim, 0x18000000, 31), 0x00850001 >> 1)
        self.assertEqual(sim.mailbox, 0x01A50000)
        wire_frame(sim, 0x10000000, 33)
        self.assertTrue(sim.poison)
        self.assertEqual(sim.bits, 33)
        for _ in range(8):
            sim.tick(ui_in=0)
        self.assertTrue(sim.frame)
        sim.set_reset(ena=False)
        self.assertEqual((sim.release, sim.uo_out, sim.p_oe, sim.mailbox), (0, 0, 0, 0x00050000))
        self.assertFalse(sim.frame)
        self.assertEqual(sim.imem, [0] * 128)
        sim.tick()
        sim.set_reset()
        for expected in (False, False, True):
            sim.tick()
            self.assertEqual(sim.ready, expected)

    def test_loader_rejections_and_priorities(self):
        sim = new_sim()
        self.assertEqual(sim.command(0x12, 1) >> 24, 2)
        self.assertEqual(sim.command(0x12) >> 24, 3)
        for bad in ("short", "skipped", "duplicate", "extra", "crc"):
            with self.subTest(bad=bad):
                sim.command(0x10, 0xFF)
                self.assertFalse(sim.poison)
                count = {"short": 0, "skipped": 0, "duplicate": 1}.get(bad, 128)
                for i in range(count):
                    self.assertEqual(sim.command(0x11, i) & 255, i + 1)
                if bad in ("short", "crc"):
                    rc = sim.command(0x12) >> 24
                else:
                    rc = sim.command(0x11, int(bad == "skipped")) >> 24
                self.assertEqual(rc, 5 if bad == "crc" else 4)
                self.assertEqual(sim.accepted, count)
                self.assertEqual(sim.command(0x17) & 255, 2)
                sim.command(0x16)
                self.assertTrue(sim.poison)
                self.assertEqual(sim.command(0x17) & 65535, 0)
                self.assertEqual(sim.command(0x12, hi=sim.crc >> 8, lo=sim.crc & 255) >> 24, 4)
                self.assertEqual((sim.mode, sim.valid, sim.od), (Mode.LOAD, False, 0))

    def test_fifo_collisions(self):
        for n in (0, 1, 7, 8):
            with self.subTest(count=n):
                sim = running("PULL\nPULL", range(n))
                reply = sim.command(0x20, lo=0xCD)
                self.assertEqual(reply >> 24, 6 if n == 8 else 0)
                self.assertEqual(sim.tx.count, n + int(n < 8) - int(n > 0))
                self.assertEqual(sim.pc, int(n > 0))
                if n == 0:
                    sim.tick()
                    self.assertEqual(sim.t, 0xCD)
                elif n < 8:
                    self.assertEqual(sim.tx.cells[(sim.tx.wr - 1) & 7], 0xCD)
                sim = running("PUSH", cfg=1)
                for byte in range(n):
                    sim.rx.put(byte)
                sim.r = 0xCD
                reply = sim.command(0x21)
                self.assertEqual(reply >> 24, 0 if n else 7)
                self.assertEqual(reply & 255, 0)
                self.assertEqual(sim.rx.count, n - int(n > 0) + int(n < 8))
                self.assertEqual(sim.pc, 1)
                if n == 8:
                    self.assertEqual(sim.errors, 16)
                    self.assertEqual(sim.rx.cells, list(range(8)))
                else:
                    self.assertEqual(sim.rx.cells[(sim.rx.wr - 1) & 7], 0xCD)
        sim = running("PULL", cfg=0)
        sim.command(0x20, lo=99)
        self.assertEqual((sim.mode, sim.errors, sim.tx.count), (Mode.FAULT, 8, 1))
        sim = running("PUSH")
        for byte in range(8):
            sim.rx.put(byte)
        self.assertEqual(sim.command(0x21) & 255, 0)
        self.assertEqual((sim.mode, sim.errors, sim.rx.count), (Mode.FAULT, 16, 7))

    def test_status_data_and_lifecycle(self):
        sim = running(".od 1\nHI P1\nTRAP 61", [0xC3])
        self.assertEqual(sim.pc, 0)
        self.assertEqual(sim.command(0x19) & 65535, 128)
        self.assertEqual(sim.p_oe, 2)
        reply = sim.command(0x16)
        self.assertEqual((reply >> 16, sim.pc, sim.p_oe, sim.detail), (0x64, 1, 0, 61))
        self.assertEqual(sim.command(0x17) & 65535, 0x3D80)
        sim.command(0x16)
        self.assertEqual((sim.mode, sim.errors, sim.detail), (Mode.FAULT, 0, 0))
        sim.command(0x14)
        self.assertEqual((sim.tx.count, sim.pc, sim.od, sim.mode), (1, 0, 1, Mode.READY))
        sim.command(0x13)
        sim.command(0x14)
        self.assertEqual(sim.p_oe, 0)
        sim.command(0x22)
        self.assertEqual((sim.tx.rd, sim.tx.wr, sim.tx.count, sim.tx.cells[0]), (0, 0, 0, 195))
        sim.command(0x13)
        sim.tick()
        sim.command(0x10, 5)
        self.assertEqual((sim.mode, sim.errors, sim.valid, sim.p_oe, sim.pending_od),
                         (Mode.LOAD, 0, False, 0, 5))


class EngineTests(unittest.TestCase):
    def test_branch_conditions_and_pre_edge_counts(self):
        for cond in range(11):
            for flag in (0, 1):
                sim = running(f"BR {cond},17")
                sim.b = sim.q = flag
                sim.t = 0x81 if flag else 0
                sim.x = sim.y = flag
                if flag:
                    sim.tx.put(3)
                    for byte in range(8):
                        sim.rx.put(byte)
                # Change the relevant count on the branch's retirement edge.
                if cond == 7:
                    sim.command(0x20, lo=0xB5)
                elif cond == 8 and flag:
                    sim.command(0x21)
                else:
                    sim.tick()
                take = (True, flag, not flag, flag, not flag, flag, flag,
                        not flag, flag, not flag, not flag)[cond]
                self.assertEqual(sim.pc, 17 if take else 1)

    def test_shift_and_parity(self):
        source = ("LIT T,129\nOUTL O0\nOUTH O1\nLIT R,128\nINL I0\n"
                  "INR I0\nB2Q\nOUTQ P7\nCLRQ\nSETQ\nCLRR\nR2T\nT2R")
        sim = running(source)
        for _ in range(2):
            sim.tick(ui_in=0x11)
        self.assertEqual((sim.o, sim.t, sim.q), (1, 64, 1))
        sim.tick(ui_in=0x11)
        self.assertEqual((sim.o, sim.t, sim.q), (1, 128, 1))
        sim.tick(ui_in=0x11)
        sim.tick(ui_in=0x11)
        self.assertEqual((sim.r, sim.q, sim.b), (1, 0, 1))
        sim.tick(ui_in=0x11)
        self.assertEqual((sim.r, sim.q, sim.b), (128, 1, 1))
        for _ in range(3):
            sim.tick()
        self.assertEqual((sim.p_data, sim.p_oe, sim.q), (128, 128, 0))
        for _ in range(4):
            sim.tick()
        self.assertEqual((sim.q, sim.r, sim.t), (1, 0, 0))

    def test_all_word_encodings(self):
        legal = set(range(12))
        for start, length in ((0x1000, 8192), (0x5000, 1024), (0x6000, 11 * 128),
                              (0x7000, 256), (0x8000, 128), (0x9000, 256),
                              (0xA000, 513), (0xB000, 4096)):
            legal.update(range(start, start + length))
        for pin in range(12):
            for action in range(7):
                legal.add(0x4000 + pin * 16 + action)
                if pin < 8 or action:
                    legal.add(0x3000 + pin * 16 + action)
        sim = new_sim()
        for word in range(65536):
            sim._execution_reset()
            sim.mode, sim.errors, sim.imem[0] = Mode.RUN, 0, word
            sim.tick()
            if bool(sim.errors & 32) != (word not in legal):
                self.fail(f"legality mismatch at {word:04X}")
            if word not in legal:
                self.assertEqual((sim.pc, sim.mode, sim.errors), (0, Mode.FAULT, 32))

    def test_timers_wrap_and_stack(self):
        sim = running("MARK\nD 1\nD 4096\nAT 4095\nPER 3\nAT 2\nHALT")
        sim.tick()
        sim.tick()
        self.assertEqual((sim.pc, sim.age), (2, 1))
        for i in range(4095):
            sim.tick()
            self.assertEqual((sim.pc, sim.delay), (2, 4095 - i))
        sim.tick()
        self.assertEqual((sim.pc, sim.age, sim.delay), (3, 4095, 0))
        sim.tick()
        sim.tick()
        self.assertEqual((sim.pc, sim.age), (5, 0))
        sim.tick()
        self.assertEqual(sim.pc, 5)
        sim.tick()
        self.assertEqual(sim.pc, 6)
        sim = running("DX 0\nDY 1\nJ 127\n" + "HALT\n" * 124 + "NOP")
        for counter in ("x", "y"):
            for i in range(256):
                sim.tick()
                self.assertEqual(getattr(sim, counter), (255 - i) & 255)
        sim.tick()
        sim.tick()
        self.assertEqual(sim.pc, 0)
        sim = running("CALL 0")
        for _ in range(3):
            sim.tick()
        self.assertEqual((sim.depth, sim.stack, sim.errors), (2, [1, 1], 64))
        sim = running("RET")
        sim.tick()
        self.assertEqual((sim.pc, sim.errors), (0, 64))

    def test_drive_staging_sampling_and_vcd(self):
        source = ".od 5\nV 15\nE 7\nAPPLY\nHI O0\nGET I0\nOUTB P2\nZ P1\nHALT"
        sim = running(source)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pins.vcd"
            with sim.vcd(path):
                for _ in range(3):
                    sim.tick(ui_in=0x11)
                self.assertEqual((sim.p_data, sim.p_oe), (10, 2))
                for _ in range(5):
                    sim.tick(ui_in=0x11)
                self.assertEqual((sim.mode, sim.o, sim.b, sim.v, sim.e, sim.p_data, sim.p_oe),
                                 (Mode.READY, 1, 1, 15, 7, 8, 0))
            trace = path.read_text(encoding="ascii")
            self.assertIn("$enddefinitions $end", trace)
            self.assertIn("p_oe $end", trace)
            self.assertIn(f"#{sim.cycle * 10**15 // 24_000_000}\n", trace)
        sim = running("LOW 0\nRISE 0\nGET 0\nFALL 0")
        for _ in range(3):
            sim.tick(uio_in=0)
        for level, pc in ((1, 1), (1, 1), (1, 2), (0, 3), (0, 3), (0, 4)):
            sim.tick(uio_in=level)
            self.assertEqual(sim.pc, pc)
        self.assertEqual(sim.b, 1)


if __name__ == "__main__":
    unittest.main()
