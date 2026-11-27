## How it works

The RP2350 loads one program at a time into a volatile 128-word instruction store. Each 16-bit word controls timing, branching, shifting, or one registered pin action. At the 24 MHz design target a tick lasts about 41.67 ns. Input instructions use the second synchronizer stage and its previous value. Open-drain outputs can drive low or release the pin.

The host SPI target occupies the upper dedicated pins. Connect active-low select, clock and MOSI to dedicated inputs 4, 5 and 6. Ground reserved input 7. Read MISO, READY, BUSY and IRQ from outputs 4 through 7. All eight bidirectional pins are available to firmware.

## How to test

After reset, wait for READY with host CS high and SCK low. Assemble an example with the repository's Python assembler. Send BEGIN with the open-drain mask, then all 128 WORD commands in order. Send COMMIT with the CRC of the mask and the 256 payload bytes. Each frame returns the preceding command's response. Use POLL to read the COMMIT reply before sending RUN.

For UART TX load examples/tx.asm, enqueue A5 hexadecimal and run. Observe an idle-high, 115200-baud 8N1 frame on P0, least-significant bit first. STOP releases the pins. Reselecting the chip requires reloading the program.

## External hardware

Use 3.3 V logic. For I2C connect external pull-ups to P0 (SDA) and P1 (SCL). For SPI connect P0 to clock, P1 to MOSI, P2 to MISO and P3 to active-low chip select, with an external chip-select pull-up. The host port is a separate point-to-point connection.
