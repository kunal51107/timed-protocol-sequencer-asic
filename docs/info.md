## How it works

This baseline places the Tiny Tapeout example adder in the 8x4 footprint reserved for the timed protocol sequencer. The two input bytes are added combinationally. The sum wraps modulo 256 and appears on the dedicated outputs. All eight bidirectional drivers remain released.

## How to test

Select the design and drive 20 on the dedicated inputs and 30 on the bidirectional inputs. Read 50 on the dedicated outputs. For a wraparound check, drive 255 and 1 and read zero.

## External hardware

The example can be exercised with the demo board GPIOs.
