"""Pin-driven differential regression. Requires Icarus Verilog and vvp on PATH."""
import itertools
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_milestone1 import LISTINGS, ROOT, assemble, Simulator


# Each value is compared immediately after the same rising edge on both models.
FIELDS = (
    (8, "uo_out", "uo_out"), (8, "p_data", "uio_out"), (8, "p_oe", "uio_oe"),
    (3, "mode", "dut.mode"), (7, "pc", "dut.pc"),
    (12, "age", "dut.engine.age"), (12, "delay", "dut.engine.delay_left"),
    (8, "t", "dut.engine.t"), (8, "r", "dut.r"), (8, "x", "dut.engine.x"),
    (8, "y", "dut.engine.y"), (1, "b", "dut.engine.b"), (1, "q", "dut.engine.q"),
    (8, "v", "dut.engine.v"), (8, "e", "dut.engine.e"), (2, "depth", "dut.engine.depth"),
    (4, "tx.count", "dut.tx_count"), (4, "rx.count", "dut.rx_count"),
    (8, "errors", "dut.errors"), (8, "detail", "dut.detail"), (8, "accepted", "dut.accepted"),
    (16, "crc", "dut.crc"), (1, "poison", "dut.poison"), (2, "cfg", "dut.cfg"),
    (8, "od", "dut.od"), (8, "pending_od", "dut.pending_od"),
    (32, "mailbox", "dut.mailbox"), (32, "request", "dut.shift"),
    (6, "bits", "dut.bit_count"), (1, "frame", "dut.frame"),
    (15, "f1", "dut.f1"), (15, "f2", "dut.f2"), (15, "h", "dut.h"),
)


def make_bench():
    width = sum(w for w, _, _ in FIELDS)
    signals = ", ".join(signal for _, _, signal in FIELDS)
    return f"""\
`timescale 1ns/1ps
module bench;
    reg clk, rst_n, ena;
    reg [7:0] ui_in, uio_in;
    wire [7:0] uo_out, uio_out, uio_oe;
    tt_um_timed_protocol_sequencer dut(ui_in, uo_out, uio_in, uio_out, uio_oe, ena, clk, rst_n);
    integer file, records, rc, i;
    reg [{width-1}:0] expected;
    wire [{width-1}:0] actual = {{{signals}}};
    reg [17:0] stimulus;
    initial begin
        clk = 0;
        ena = 1;
        rst_n = 1;
        records = 0;
        file = $fopen("vectors.txt", "r");
        if (!file) begin $display("OPEN FAILED"); $finish; end
        while (!$feof(file)) begin
            rc = $fscanf(file, "%h %h", stimulus, expected);
            if (rc == 2) begin
                #2 {{rst_n,ena,ui_in,uio_in}} = stimulus;
                #2;
                if (!rst_n || !ena) begin
                    if (actual !== expected) begin
                        $display("ASYNC RESET FAILED at %d", records); $finish;
                    end
                    for (i=0; i<128; i=i+1)
                        if (dut.imem[i] !== 0) begin $display("IMEM reset"); $finish; end
                    for (i=0; i<8; i=i+1)
                        if (dut.tx_fifo.cells[i] !== 0 || dut.rx_fifo.cells[i] !== 0)
                            begin $display("FIFO reset"); $finish; end
                end
                clk = 1;
                #1;
                if (actual !== expected) begin
                    $display("MISMATCH edge %d actual %h expected %h", records, actual, expected);
                    $finish;
                end
                records = records + 1;
                #1 clk = 0;
            end
        end
        $display("PASS %0d", records);
        $finish;
    end
endmodule
"""


def make_vectors(path):
    from operator import attrgetter
    getters = [(width, attrgetter(name)) for width, name, _ in FIELDS]
    sim = Simulator()
    with path.open("w", encoding="ascii") as out:
        def edge(ui=16, pads=None):
            pads = sim.pads() if pads is None else pads
            sim.tick(ui_in=ui, uio_in=pads)
            value = 0
            for width, get in getters:
                value = (value << width) | int(get(sim))
            stim = sim.rst_n << 17 | sim.ena << 16 | ui << 8 | pads
            out.write(f"{stim:x} {value:x}\n")

        def frame(word, bits=32):
            for _ in range(8):
                edge(0)
            for i in reversed(range(bits)):
                mosi = ((word >> i) & 1) << 6
                for level in (0, 32):
                    for _ in range(12):
                        edge(mosi | level)
            for _ in range(8):
                edge(0)
            for _ in range(8):
                edge()

        sim.set_reset(False)
        edge()
        sim.set_reset()
        for _ in range(4):
            edge()
        # Check each host rejection class, including persistent load poison.
        for request, length in ((0, 31), (0xFF000000, 32), (0x13000000, 32),
                                (0x10000000, 32), (0x11010000, 32),
                                (0x16000000, 32), (0x12000000, 32), (0, 33)):
            frame(request, length)

        special = ".od 5\nV 15\nE 7\nAPPLY\nCALL sub\nPULL\nHI O0\nTRAP 105\nsub: D 4096\nRET"
        images = itertools.chain([special], (listing[0] for listing in LISTINGS.values()))
        for index, source in enumerate(images):
            image = assemble(source)
            frame(0x10000000 | image.od << 16)
            for addr, word in enumerate(image.words):
                frame(0x11000000 | addr << 16 | word)
            if index == 0:
                frame(0x12000000 | (image.crc ^ 1))
                assert sim.poison
                frame(0x10000000 | image.od << 16)
                for addr, word in enumerate(image.words):
                    frame(0x11000000 | addr << 16 | word)
            frame(0x12000000 | image.crc)
            frame(0x21000000)
            for byte in range(9):
                frame(0x20000000 | ((0x96 + 31 * byte) & 255))
            frame(0x13000000)
            for tick in range(6500):
                # Released SCL rises, SDA is acknowledged low on selected
                # intervals. RX and MISO see a reproducible changing input.
                peer = 0xFA | ((tick // 208) & 1) | (((tick // 17) & 1) << 2)
                edge(pads=sim.pads(peer))
            for op in (0x17, 0x18, 0x19, 0x21, 0x14, 0x22, 0x16, 0):
                frame(op << 24)

        # Arrange an engine PUSH on the CS deassertion acceptance edge of
        # RX_READ. I0 rises one core edge ahead of CS, retiring RISE first.
        image = assemble("LIT R,165\nLX 8\nfill: PUSH\nDX fill\nRISE I0\nPUSH")
        frame(0x10000000)
        for address, word in enumerate(image.words):
            frame(0x11000000 | address << 16 | word)
        frame(0x12000000 | image.crc)
        frame(0x13000000)
        for _ in range(32):
            edge()
        assert sim.rx.count == 8
        for _ in range(8):
            edge(0)
        for i in reversed(range(32)):
            bit = (0x21000000 >> i & 1) << 6
            for clk in (0, 32):
                for _ in range(12):
                    edge(bit | clk)
        for _ in range(7):
            edge(0)
        edge(1)
        for _ in range(8):
            edge(17)
        assert sim.errors == 16 and sim.rx.count == 7 and sim.mailbox & 255 == 165
        frame(0x10030000)
        frame(0x11003001)
        assert sim.accepted == 1
        for ui in (0, 32, 0, 96):
            for _ in range(12):
                edge(ui)
        assert sim.frame and sim.bits == 2
        sim.set_reset(False)
        edge()
        sim.set_reset()
        for _ in range(4):
            edge()
        assert sim.accepted == 0 and not sim.valid and not any(sim.imem)

        image = assemble("LO P0\nPULL\nHIGH I0")
        frame(0x10000000)
        for address, word in enumerate(image.words):
            frame(0x11000000 | address << 16 | word)
        frame(0x12000000 | image.crc)
        frame(0x13000000)
        # TX_WRITE meets a blocked empty PULL, then T loads on the next edge.
        frame(0x200000c3)
        assert sim.p_oe == 1 and sim.t == 0xc3 and sim.tx.count == 0
        sim.set_reset(ena=False)
        edge()
    return sim.cycle


@unittest.skipUnless(shutil.which("iverilog") and shutil.which("vvp"), "needs iverilog and vvp")
class RTLLockstep(unittest.TestCase):
    def test_wire_and_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "bench.v").write_text(make_bench(), encoding="ascii")
            sources = [ROOT / "src" / name for name in
                       ("project.v", "sequencer_engine.v", "sequencer_fifo.v")]
            subprocess.run(["iverilog", "-g2001", "-Wall", "-s", "bench", "-o", "test.vvp",
                            *sources, "bench.v"], cwd=folder, check=True)
            count = make_vectors(folder / "vectors.txt")
            result = subprocess.run(["vvp", "test.vvp"], cwd=folder, check=True,
                                    text=True, capture_output=True)
            self.assertEqual(result.stdout.splitlines()[0], f"PASS {count}", result.stdout)
