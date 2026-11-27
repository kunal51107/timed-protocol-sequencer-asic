# Simulation

Run `make -C test` from the repository root using the versions in requirements.txt and Icarus Verilog with vvp. The RTL target first runs the Python suite and differential simulation, then compiles the Tiny Tapeout top and runs cocotb against the pads. A failing command or cocotb result fails CI.

The UART transmitter test decodes both SPI-loaded bytes, including bit widths, at P0. The external peers in protocols.py check UART RX, all four controller SPI modes and both I2C speeds. The I2C target varies clock release and exercises NACK handling. The peer decisions use the physical clock, data and output-enable signals. They have no access to the engine PC.

The lockstep bench in ../tests compares the Python and RTL state at each rising edge. It also observes asynchronous reset before the next rising edge. Coverage includes malformed and poisoned loads, FIFO collisions, deselection while P0 is driven, reset during a partial WORD and loading a second image. A skip from invoking pytest alone indicates a missing simulator; the full RTL make target requires both iverilog and vvp.

For gate-level simulation the Tiny Tapeout action supplies gate_level_netlist.v and the matching PDK models. The GL target uses two pin-only suites: the loader/FIFO/reset test and the UART TX timing test. Do not enable the lockstep bench on a synthesized netlist; it observes named architectural registers in the RTL.

## Early measurements

The UART bring-up commit mapped to 176738.78 square micrometres of standard cells and ran a host-loaded two-byte UART transmission before the remaining ISA was enabled. On the same synthesis recipe, the full engine mapped to 198046.37 square micrometres. Both estimates use Yosys 0.71+134, dfflibmap and ABC against the typical IHP CMOS5L 1.20 V library at IHP-Open-PDK commit e73adb4045731ddec21634146d536380057f6a26. Physical utilization, slack and violations must be taken from the final GDS job.

The earlier Milestone 2 run on main used a resolved 20 ns period. Its slow-corner setup slack was -6.170 ns. This branch uses 41.666 ns to verify the selected 24 MHz operating point.
