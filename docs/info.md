## How it works

A dedicated host SPI target loads a 128-word program into the volatile instruction store. The UART bring-up engine shifts queued bytes onto bidirectional pin P0. The program holds the pin high while waiting for the next byte.

## How to test

With the 24 MHz core running, load examples/tx.asm using open-drain mask 00 and commit CRC 7E24. Queue A5 and 5A hexadecimal and send RUN. Decode two consecutive 115200-baud, 8N1 frames on P0. STOP releases the pin.

## External hardware

Connect P0 to a 3.3 V UART receiver or logic analyzer with a shared ground.
