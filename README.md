# 8x4 host-loaded UART bring-up

This checkpoint keeps the full host SPI loader, 128 × 16 store and FIFO interface. Its temporary execution engine implements the Section 5 UART TX instruction subset. The complete frozen ISA is specified in SPEC.md.

Assemble the reference image with `python tools/asm.py examples/tx.asm -o uart.bin`. The mask is 00 and the commit CRC is 7E24. The pin test loads all 128 words, checks the pipelined response, enqueues A5 and 5A, and measures every bit on P0 through the stop interval. Use `make -C test` with the template test requirements and Icarus installed.

The early experiment mapped 176,738.78 square micrometres of standard cells, of which 111,547.50 were sequential cells. This is a pre-placement estimate. Reproduction used Yosys 0.71+134, the IHP CMOS5L 1.20 V typical library at IHP-Open-PDK revision e73adb4045731ddec21634146d536380057f6a26, and the following Yosys commands with the library at cells.lib.

```
read_verilog src/project.v src/sequencer_fifo.v src/sequencer_engine.v
synth -top tt_um_kunal_timed_protocol_sequencer -flatten -noabc
dfflibmap -liberty cells.lib
abc -liberty cells.lib
stat -liberty cells.lib
```

The 8x4 template baseline is commit 93919e278f5f75df80a59399fb68028e1ad75a70. The custom hardening target is 41.666 ns, corresponding to 24 MHz.
