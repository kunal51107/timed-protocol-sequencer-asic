# Timed Protocol Sequencer

A single 24 MHz engine executes a volatile 128 × 16-bit image. The RP2350 uses a dedicated mode-0 SPI port to load the image and exchange bytes. Firmware controls all eight bidirectional pads, four dedicated outputs, and sampling of four dedicated inputs. The target is Tiny Tapeout IHP CMOS5L, 8×4 tiles.

## Write and run firmware

From the repository root, use Python 3.8+ to assemble a padded, high-byte-first image or inspect simulated pin activity.

```sh
python tools/asm.py examples/tx.asm -o uart.bin
python tools/sim.py examples/tx.asm --tx a55a --cycles 5000 --vcd uart.vcd
```

The assembler reports the OD mask and commit CRC. Each file below is an independent program. The example hex bytes in the last column are FIFO payloads.

| File in examples/ | Pins | Behavior | TX prefill |
|---|---|---|---|
| tx.asm | P0 TX | 115200 baud, 8N1, idle high when the queue empties | A5 5A |
| rx.asm | P0 RX | 115200 baud, false-start filtering, framing diagnostics | none |
| spi0.asm through spi3.asm | P0 SCK, P1 MOSI, P2 MISO, P3 CS_N | 1 MHz, 64 full-duplex bits per chip selection | eight bytes |
| fast.asm and std.asm | P0 SDA, P1 SCL | 400/100 kHz combined transaction, writes register index then reads two bytes | A4 37 A5 for address 0x52 |

The I²C examples require external pull-ups and a single controller. SPI bursts assume eight TX bytes and an empty RX FIFO. UART TX and RX use separate images. A blocked wait keeps BUSY high until its condition is met or the host stops the engine.

## Host connection

The host drives CS_N, SCK and MOSI on UI4–UI6 and reads MISO, READY, BUSY and IRQ on UO4–UO7. Tie UI7 low. Use a point-to-point, 3.3 V connection. With the design selected and the core clock running, configure the host SPI controller at 1 MHz or slower, polarity 0, phase 0, MSB first. Use manual chip select. Verify setup, hold and phase widths against Section 3 of SPEC.md for your host hardware.

Copy tools/rp2350_loader.py and uart.bin to a MicroPython host. With spi, cs_n and ready bound to the pins wired to the chip, run:

```python
from rp2350_loader import Sequencer
chip = Sequencer(spi, cs_n, ready)
chip.load("uart.bin", 0x00, 0x7E24)
chip.run(bytes([0xA5, 0x5A]))
```

For SPI or I²C, wait for BUSY to fall before draining the RX FIFO with read_rx(). Recheck diagnostics after an error. A host frame returns the previous command's response. The driver follows each command with POLL and checks that response. Reset or carrier deselection requires a new load. Physical host pin assignments depend on the board wiring.

## Verify

Install Icarus Verilog with vvp, GNU Make, Python 3.11+ and the packages in test/requirements.txt. From the root run:

```sh
python -m pip install -r test/requirements.txt
make -C test
```

The RTL target runs the assembler and behavioral regressions, cycle-by-cycle differential comparison, and independent cocotb peers attached to the top-level pins. It requires Icarus. For waveform inspection, open test/tb.fst. The gate-level target uses the netlist and cell models supplied by the Tiny Tapeout GL workflow and exercises the loader, reset, open-drain pins, FIFOs and UART. The project uses the same clock for every functional update. Reset assertion is asynchronous, with functional operation starting on the third rising edge after release.

The hardening period is 41.666 ns. Use the GDS workflow's post-route STA for the final commit to assess clock closure. Board propagation, peer timing, metastability, actual I²C rise times and fabricated-silicon behavior require separate electrical verification. The host example has been designed for MicroPython; a board demonstration is pending.

## Design and verification sources

- [Architecture, instruction encodings, loader and timing ledgers](SPEC.md)
- [Assembler](tools/asm.py) and [cycle model](tools/sim.py)
- [Instruction engine](src/sequencer_engine.v), [FIFO](src/sequencer_fifo.v) and [host/top](src/project.v)
- [Golden listings and model peers](tests/test_milestone1.py), [lockstep](tests/test_rtl_lockstep.py), [UART transmitter](test/uart_tx.py) and [external protocol peers](test/protocols.py)

The design, implementation and tests were developed with ChatGPT/Codex assistance. Cross-checks use independently specified Section 5 words and CRCs, pin-level peers and post-layout tooling. SWD is a future firmware experiment.
