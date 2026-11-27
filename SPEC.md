# Timed Protocol Sequencer — Architecture & ISA Specification

**Revision 1.0 · Specification for implementation review · 22 November 2026**

One loaded image owns one 24 MHz execution context. The RP2350 loads a volatile 128 × 16-bit flip-flop instruction store, exchanges bytes through two eight-entry FIFOs, and can stop or replace a running image. Protocol timing, pin assignments, framing, and retries are defined by the image. All numbers in the listings are sufficient to reconstruct the exact 256-byte payload.

## 1. Implementation contract

Target the main branch of `ttihp-verilog-template`, IHP 130 nm CMOS5L, 8×4 tiles, and a 41.667 ns nominal clock period (exactly 24,000,000 Hz in the timing tables). The top module and future source path are `tt_um_kunal_timed_protocol_sequencer` and `src/project.v`. The audit metadata uses “Kunal” as the author attribution. The 7–9K-cell estimate is a planning figure. Cell count divided by 32K is **not physical utilization**. Measure mapped cell area divided by placeable area, routing congestion, reset fanout, and post-route setup/hold, including PC → 128:1 store read → decode → branch/outputs. The specification makes no measured silicon or closure claim. Tiny Tapeout's approximately 10 ns pad/mux round trip is a board estimate. [Competition rules](https://tinytapeout.com/competitions/jane-street-tt-protocol-emulator/) · [Announcement and FAQ](https://blog.janestreet.com/protocol-emulator-asic-competition/) · [Template metadata](https://github.com/TinyTapeout/ttihp-verilog-template/blob/main/info.yaml)

### Pins and pin numbering

| TT pins | Function |
|---|---|
| `ui_in[3:0]`, `uo_out[3:0]` | Four independent dedicated protocol inputs I0–I3 and outputs O0–O3. Operand numbers 8–11 select I0–I3 on a read and O0–O3 on a write. A read never returns an O latch. |
| `uio[7:0]` | P0–P7, operand numbers 0–7. Read the synchronized physical pad, including when transmitting. Each output data/OE pair is registered. OE=1 drives. OE=0 releases. |
| `ui_in[4,5,6,7]` (ascending) | H_CS_N, H_SCK, H_MOSI, reserved input tied low and ignored. |
| `uo_out[4,5,6,7]` (ascending) | H_MISO, H_READY, H_BUSY, H_IRQ. MISO is a dedicated, always-driven output with idle level zero. Use a point-to-point host connection. |
| `clk, rst_n, ena` | Only `clk` clocks functional logic. Selection/reset sequencing below. SCK and external protocol clocks are sampled as data. |

All pins are digital. Open-drain examples require external 3.3 V pull-ups. SPI CS_N should have an external pull-up while the chip releases that pin. Use external electrical adaptation when the peer requires it.

### Programmer-visible state

| State | Width and meaning |
|---|---|
| PC, IMEM | 7-bit word address, 128 × 16-bit single-write, combinational-read store. Fetch uses the registered PC throughout the cycle. |
| T, R | 8-bit outgoing and incoming shift registers. X and Y are separate 8-bit counters. |
| B, Q | Last explicit input sample and running XOR/parity bit. |
| Return stack | Two 7-bit entries and a depth in 0–2. No interrupt or asynchronous firmware entry. |
| Time | 12-bit saturating age since last timing anchor, plus a 12-bit remaining-delay count. |
| Pin state | Eight data bits and eight enables for P, four output bits for O, eight-bit staging latches V and E, eight-bit committed open-drain mask OD. |
| FIFOs | Independent TX and RX rings. Each has 8 × 8 payload bits, 3-bit read/write pointers, and a 4-bit occupancy in 0–8. TX flows host → engine. RX flows engine → host. |

All updates use pre-edge operands unless explicitly specified. One enabled rising edge retires any ordinary instruction, including a taken branch, pin write, call, or return. There are no fetch bubbles, speculative pin writes, interrupt cycles, or host steals. PC advances modulo 128 on sequential retirement. `HALT` and faults retain the PC of the terminating word. Held instructions keep their PC and produce no repeated side effects.

## 2. Reset, loading states, and ownership

**Reset assertion is asynchronous.** Either `rst_n=0` or `ena=0` asserts the internal reset while powered. Clear both reset-release synchronizer flops immediately. The functional domain remains reset until both are high. After both external signals are high, edge E1 sets the first release flop, E2 sets the second, and E3 is the first functional edge. Meet the library's reset pulse width/recovery specifications. Hold CS_N high and SCK low during release. A powered-down carrier retains no image.

**Reset values are physical initialization requirements.** Clear all 2,048 IMEM cells to zero (HALT), all 128 FIFO payload bits to zero, both pointer pairs/counts, PC, T/R/X/Y/B/Q, stack entries/depth, age, delay, V/E, OD and pending OD, P data/OE, O, all errors and firmware detail, image-valid, poison, accepted-word count, and CRC working register to zero. Set CFG=03 hex and mode=EMPTY (0). Reset both stages and history of the CS_N sampler to one. Reset other input samplers/history to zero. Clear the SPI arm flag, frame-active flag, bit counter, receive/response shift registers, MISO latch, and response mailbox except that the mailbox reset word is 00050000 hex. Any auxiliary implementation register must have a specified deterministic reset value, zero unless necessary to encode these states. IMEM and FIFO cells receive reset even when invalid. No initialization file or FPGA power-up value supplies reset. Throughout reset `uio_out=00`, **`uio_oe=00`**, and all dedicated outputs, including READY, are zero.

On E3 or any later edge recognizing synchronized CS_N high and SCK low, set host-armed. H_READY=host-armed remains high until reset, including during RUN and LOAD. H_BUSY=(mode=RUN). H_IRQ=ERR or RX_COUNT≠0. Outputs reflect the new registered state after an edge. There is no execution until a successful RUN.

| Mode | Entry and transitions |
|---|---|
| 0 EMPTY | Reset destination. BEGIN → LOAD. |
| 1 LOAD | BEGIN restarts from word 0. Successful COMMIT → READY. Failed load/commit remains invalid in LOAD. |
| 2 READY | A committed image exists. RUN → RUN. Normal HALT returns here with the last pin levels retained. |
| 3 RUN | Executes starting at PC=0 on the first edge **after** RUN acceptance. A wait still asserts BUSY. STOP → READY. Fatal engine error → FAULT. |
| 4 FAULT | Retains committed image and offending PC. STOP → READY, or BEGIN → LOAD. Clearing sticky errors alone leaves the engine stopped in FAULT. |

BEGIN is accepted in any mode, atomically aborts execution, invalidates the image, releases all P pins, zeros O, resets the execution registers/staging/stack/timers, empties both FIFOs with pointers zero, clears errors/detail, restores CFG=03, clears active OD, records pending OD, initializes CRC and count, and clears poison. Existing IMEM words and FIFO payloads may persist across BEGIN; a valid new image requires all 128 writes. STOP is accepted in READY/RUN/FAULT, releases pins and zeros O, clears execution state as on BEGIN, and retains image, OD, CFG, FIFO contents, and diagnostics. RUN is accepted only in READY. It reinitializes execution state and pin outputs in the same way, preserves queued bytes, image, OD, CFG and diagnostics, and enters RUN. HALT retains outputs, FIFO state and execution registers. A fatal fault releases all P pins and zeros P data/O on that edge, holds PC, and suppresses the faulting instruction's other effects. An external reset supersedes everything.

## 3. Host wire protocol

Four-wire SPI target, mode 0, MSB first, fixed 32-bit frames. Idle CS_N=1, SCK=0. For the 24 MHz configuration use SCK ≤1 MHz, each high/low phase ≥10 core periods, CS setup before first rise ≥8 periods, CS hold after last falling edge ≥8 periods, and deselect interval ≥8 periods. MOSI must be stable ≥3 periods before and after each external rising edge. The host samples MISO on external rising edges. These conservative margins accommodate the synchronized decoder; no host clock crosses directly into the core. Wait for H_READY before the first frame.

CS_N, SCK and MOSI each have two input flops. Use the pre-edge second-stage level, and its previous value for edge recognition. Recognized CS assertion starts a frame, zeros its request accumulator/count, and presents response bit 31. Recognized SCK rising samples one MOSI bit, oldest first. Recognized falling launches the next MISO bit, zero after bit 0. Count rises to 32 and saturate excess at 33. Recognized CS deassertion ends the frame, forces MISO low, and accepts a command only for exactly 32 rising edges. CS transitions take priority over SCK transitions. Deassertion publishes a new 32-bit response for the **next** frame. A truncated/overlong frame publishes an error without performing its request. Host polling is always safe. Reset during a frame discards it and restores the reset mailbox. If the host loses track of a reply, it must query status/counts before repeating a destructive command.

Request bytes are `OP A H L` in transmission order. All constants in the next tables are hexadecimal unless labeled otherwise. Unused argument bytes/bits must be zero. Response bytes are `RC STATUS D1 D0`. STATUS describes **post-command and post-engine** state. Data items, except explicit post-write counts, sample the pre-edge state. For a rejection D1=D0=0. The reset response is RC=00, STATUS=05, D1=D0=00.

| OP | Arguments A H L | Valid mode and action | Success D1 D0 |
|---|---|---|---|
| 00 POLL | 00 00 00 | Any. Read state and policy. | mode, CFG |
| 10 BEGIN | od 00 00 | Any. Begin sequential load under mask od. | 00 00 |
| 11 WORD | index hi lo | LOAD, unpoisoned, count<128, index=count. Write IMEM[index]=hi:lo, update CRC with hi then lo, increment count once. | 00, new count |
| 12 COMMIT | 00 crcHi crcLo | LOAD, unpoisoned, count=128, CRC matches. Latch OD, set image-valid, enter READY. | 00 00 |
| 13 RUN | 00 00 00 | READY. Start at PC 00. | 00 00 |
| 14 STOP | 00 00 00 | READY/RUN/FAULT. Abort or release held pins. | 00 00 |
| 15 CONFIG | 00 00 cfg | READY. cfg[1:0] as below, upper bits zero. | 00 00 |
| 16 CLEAR | 00 00 00 | Any. Clear sticky errors and firmware detail. Load poison persists. | 00 00 |
| 17 DIAG | 00 00 00 | Any. Read diagnostic bytes. | firmware detail, error bitmap |
| 18 COUNTS | 00 00 00 | Any. Read occupancy. | TX count, RX count |
| 19 PROGRESS | 00 00 00 | Any. Inspect execution/load position. | PC (zero-extended), accepted word count (0–128 decimal) |
| 20 TX_WRITE | 00 00 byte | READY/RUN. Enqueue one byte if space exists. | 00 00 |
| 21 RX_READ | 00 00 00 | READY/RUN/FAULT. Pop one byte if available. | 00, oldest RX byte |
| 22 FLUSH | 00 00 00 | READY/FAULT. Zero both counts and all four pointers. Retain payload cells. | 00 00 |

**Image representation.** Transmit addresses 00–7F in order, one WORD per address, padding unused locations with 0000. CRC-16/CCITT-FALSE has polynomial 1021, initial FFFF, no reflection, xorout 0000. Fold the BEGIN mask as the first byte, then exactly 256 instruction bytes, high byte first. For each byte XOR it into the high CRC byte, then shift left eight times, XORing 1021 after each shift whose old MSB was one and truncating to 16 bits. Check vectors are 29B1 for ASCII “123456789” and B0E5 for mask 00 plus 256 zero bytes. COMMIT is the sole validity transition. No execution on COMMIT. The host reads the COMMIT reply with POLL before RUN. A failed frame/command processed while in LOAD sets sticky load poison. Only a new BEGIN or reset clears poison. POLL, DIAG, COUNTS, PROGRESS and CLEAR remain available while poisoned. Duplicated, skipped, extra and out-of-sequence words cannot produce a valid commit.

| STATUS bit | 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0 |
|---|---|---|---|---|---|---|---|---|
| Meaning | LOAD mode | image valid | ERR | BUSY | RX_FULL | RX_EMPTY | TX_FULL | TX_EMPTY |

RC priority on host rejection is 01 malformed length, 02 unknown opcode/reserved argument, 03 wrong mode, 04 bad load sequence/poison/count, 05 checksum mismatch, 06 TX full, 07 RX empty. RC=00 means accepted. The error bitmap is sticky: bit 0 host frame/opcode/argument/mode (RC 01–03), bit 1 loader (RC 04–05), bit 2 host FIFO misuse (RC 06–07), bit 3 engine TX underrun, bit 4 engine RX overrun, bit 5 illegal instruction, bit 6 return stack overflow/underflow, bit 7 firmware TRAP. ERR is the OR of this bitmap. Firmware detail is the most recent TRAP immediate, otherwise unchanged except on reset/BEGIN/CLEAR. Load poison also prevents COMMIT after sticky errors have been cleared.

**FIFO and collision semantics.** PULL consumes the oldest TX byte into T. If TX was empty and CFG[0]=1, hold PC and T and retry each edge; no error. With CFG[0]=0, fault immediately and set error bit 3. PUSH copies R into RX. If RX was full and CFG[1]=1, fault and set bit 4. With CFG[1]=0, discard the incoming byte, retain queued bytes, set bit 4 and retire. This policy never stalls on RX full. Host empty reads/full writes reject without changing FIFO contents. A host frame and an instruction may act on the same edge. Each uses pre-edge counts and data; accepted producer and consumer both advance their own pointers. At empty, a producer's byte becomes available next edge. At full, a simultaneous pop frees space next edge. No bypass or overwrite. Reset wins first, then an accepted BEGIN/STOP/RUN suppresses ordinary engine actions on its acceptance edge. All other host commands compose with the running engine. New errors beat simultaneous CLEAR. A fatal instruction still permits an independently accepted host FIFO operation. The eight-byte burst guarantee assumes exactly eight TX bytes preloaded, RX initially empty, and exclusive FIFO access by the demonstration until BUSY falls. Arbitrary-length continuous service has no timing guarantee.

## 4. ISA encoding and retirement

Bit 15 is the MSB. Hex constants in this section include all fixed bits. Each row describes a complete set of encodings. Fields marked zero must be zero, including for a branch whose condition is false. Any unlisted word faults in one edge with error bit 5. The illegal-instruction check precedes operand effects. Unless a row changes a register, that register is preserved (the running timer and input samplers advance independently). Bytes and shifts truncate to eight bits. All branch/call addresses are **absolute word addresses**.

| [15:12] | [11:0] layout, MSB first | Mnemonic and exact effect | Edges occupied |
|---|---|---|---|
| 0 | Exact low 12-bit subcode, table below | System/register operation | 1, except empty PULL |
| 1 | `d[11:0]` | D k, where k=d+1 (1–4096). Occupy k consecutive edges; advance PC on the last. | d+1 |
| 2 | `n[11:0]` | AT n. Wait for edge at or after anchor+n. Preserve anchor. | First eligible edge, minimum 1 |
| 3 | `0000 p[3:0] a[3:0]` | PIN. Registered pin action, table below. | 1 |
| 4 | `0000 p[3:0] s[3:0]` | SENSE. Wait or sample, table below. | 1 for sample, level/edge waits variable |
| 5 | `r[3:0] imm[7:0]` | LIT r,imm. r=0,1,2,3 writes T,R,X,Y respectively. Other r illegal. | 1 |
| 6 | `c[4:0] addr[6:0]` | BR c,addr. If condition true, PC=addr; otherwise fall through. | 1 either way |
| 7 | `0000 y addr[6:0]` | DX/DY addr. Select X for y=0, Y for y=1. Decrement modulo 256, branch if **new** value ≠0. Initial zero gives 256 iterations. | 1 either way |
| 8 | `00000 addr[6:0]` | CALL addr. Push PC+1 modulo 128 at stack[depth], increment depth, jump. At depth=2 fault, error bit 6. | 1 |
| 9 | `0000 imm[7:0]` | TRAP imm. Set error bit 7 and detail=imm, enter FAULT. | 1 |
| A | `sub[3:0] imm[7:0]` | A0vv writes V=vv; A1ee writes E=ee; A200 applies V,E to all P pins on one edge. All other encodings illegal. | 1 |
| B | `n[11:0]` | PER n. Same eligibility as AT, and the actual retirement edge becomes the next anchor. No catch-up after a late event. | First eligible edge, minimum 1 |
| C–F | any | Illegal instruction. | 1 |

| System word | Operation | System word | Operation |
|---|---|---|---|
| 0000 HALT | Enter READY and retain PC and pins. | 0001 NOP | Fall through. |
| 0002 MARK | Set the anchor to this edge. | 0003 RET | If depth>0, PC=stack[depth−1] and decrement depth, retaining popped cell. Otherwise fault, bit 6. |
| 0004 PULL | TX → T with the FIFO rules above. | 0005 PUSH | R → RX with the FIFO rules above. |
| 0006 CLRR | R=0. | 0007 R2T | T=R. |
| 0008 T2R | R=T. | 0009 CLRQ | Q=0. |
| 000A SETQ | Q=1. | 000B B2Q | Q=B. |

**Time arithmetic.** RUN acceptance provides an initial anchor. If its edge or a MARK/PER retirement edge is m, edge m+1 has elapsed=1 for comparison. Equivalently, at each execution edge compute elapsed=min(4095,pre_age+1); AT/PER retire when elapsed≥n. Write age=elapsed except on MARK/PER retirement, which write zero. Saturation prevents wraparound. The next instruction first attempts the edge after retirement. D consumes its issue edge; for d>0 it leaves d more edges to consume with PC held. An implementation may use remaining=0 as “new instruction”; initialize remaining=d on the issue edge and decrement on subsequent held edges, advancing PC when the old remaining equals one. Waits and FIFO stalls also age the timer. Timing state holds outside RUN except on specified lifecycle transitions.

**PIN actions.** p=0..7 addresses P; p=8..11 addresses O on output. p=12..15 illegal. Logical drive(v) on a P pin atomically writes (data,OE)=(v,1) in push-pull mode. When OD[p]=1 it writes (0,1−v). Explicit release writes (0,0) in either mode. For O pins drive(v) writes the corresponding output bit; explicit release is illegal. Changes to one pin preserve every other pin. A200 writes eight P data bits as V AND NOT OD and eight P enables as E AND NOT (OD AND V). V/E are independent staging registers, untouched by individual PIN actions. A disabled push-pull bit may retain its staged data; an OD bit's physical data latch is always low. A200 leaves the O bank unchanged. “Atomic” means common registered commit edge; external net skew and rise times remain physical quantities.

| a | Mnemonic | Action on selected pin and registers |
|---|---|---|
| 0 | Z | Release P. |
| 1 / 2 | LO / HI | Drive logical zero / one. |
| 3 | OUTL | Drive old T[0], then T=T>>1 (zero fill), Q=old Q XOR transmitted bit. |
| 4 | OUTH | Drive old T[7], then T=T<<1 (zero fill), Q=old Q XOR transmitted bit. |
| 5 / 6 | OUTQ / OUTB | Drive old Q / B without shifting or updating parity. |

**Input sampling.** p=0..7 reads P; p=8..11 reads I; larger p is illegal. For each input the rising core edge captures the pad in F1, F2 captures old F1, and H captures old F2. Instructions and the host decoder evaluate **old F2 and H**. If F1 first captures a new level at e, F2 changes just after e+1, and the first instruction that can consume it retires at e+2. A shift at e+2 sees the external sample taken at e. An asynchronous transition safely away from the aperture is recognized approximately 2–3 core periods later, plus pad delay. Metastability can add uncertainty; this is a digital timing contract, not a physical worst-case metastability bound. Budget at least three core periods of stable high and low for asynchronous event sources in the rated envelope. A live edge wait must already be active on the recognition edge. There is no pending event latch. An earlier level change is still visible to a level wait. Reset history is zero on protocol inputs; firmware requiring a genuine edge first establishes the opposite idle level.

| s | Mnemonic | Retirement condition or effect |
|---|---|---|
| 0 / 1 | LOW / HIGH | Retire when F2=0 / 1. Already true costs one edge. |
| 2 / 3 | RISE / FALL | Retire when (H,F2)=(0,1) / (1,0). |
| 4 | GET | B=F2. |
| 5 | INL | B=F2, R=((R<<1) OR F2), Q=old Q XOR F2. First of eight samples ends in R[7]. |
| 6 | INR | B=F2, R=((R>>1) OR (F2<<7)), Q=old Q XOR F2. First of eight samples ends in R[0]. |

A wait changes no B, Q, or shift state. A false wait retries every edge indefinitely until satisfied, STOP, BEGIN or reset. Engine stalls retain protocol outputs. A synchronous peer's setup and hold still have to be met at the physical F1 sampling edge.

| Branch c (decimal) | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Predicate | always | B=1 | B=0 | Q=1 | Q=0 | T[0]=1 | T[7]=1 | TX_EMPTY | RX_FULL | X=0 | Y=0 |

Listing aliases J, JB, JNB select c=0,1,2. LX/LY are LIT to X/Y. The instruction encoder concatenates fields exactly as above. For example, OUTH P1=3014, INR P0=4006, DY 17 hex=7097, and D 208 decimal=10CF. The firmware can build parity with Q, examine ACK bits using B or R2T, release a wire using Z, and branch for retry. SWD firmware is deferred until after ISA freeze as an independent test of these primitives.


## 5. Reference programs and hand timing

These are word-addressed assembly listings, including initialization, loop control, exceptional exits and subroutines. PC and machine words are hexadecimal. **All numeric mnemonic operands are decimal**; branch labels resolve to the shown PC. Pin operands 0–7 refer to P0–P7. Each column is a separate image. Pad each to address 7F inclusive with 0000. The commit CRC includes OD. These proofs count architecture edges. Pad-level rates require the stated electrical conditions and later implementation measurements.

| Image | Used / 128 words | Free | OD | Commit CRC |
|---|---:|---:|---|---|
| UART TX | 24 | 104 | 00 | 7E24 |
| UART RX | 28 | 100 | 00 | 74FF |
| SPI mode 0 | 26 | 102 | 00 | 008D |
| SPI mode 1 | 25 | 103 | 00 | E36B |
| SPI mode 2 | 26 | 102 | 00 | 9868 |
| SPI mode 3 | 25 | 103 | 00 | BB84 |
| I²C Fast-mode | 75 | 53 | 03 | 9BAF |
| I²C Standard-mode | 75 | 53 | 03 | 1139 |

### UART TX · 115,200 baud, 8N1, P0

Set CFG[0]=1. P0 idles high while waiting for the next TX byte. The first HI and delay establish idle before transmission. A pin action occupies one edge. The next transition follows after D k and the next pin action, an interval of k+1 ticks. From start to stop the nine spacings are **208, 208, 209** repeated three times. Nine bits occupy exactly 1,875 ticks at 24 MHz, or 115,200 bits/s. The cumulative boundary error is less than one tick. OUTL sends LSB first. The stop bit lasts at least 209 ticks; branching, fetching the next byte, or host starvation extends only idle high.

```text
PC   word  instruction
00   3002    HI 0
01   10CF    D 208
02   0004    loop: PULL
03   3001    LO 0
04   10CE    D 207
05   3003    OUTL 0
06   10CE    D 207
07   3003    OUTL 0
08   10CF    D 208
09   3003    OUTL 0
0A   10CE    D 207
0B   3003    OUTL 0
0C   10CE    D 207
0D   3003    OUTL 0
0E   10CF    D 208
0F   3003    OUTL 0
10   10CE    D 207
11   3003    OUTL 0
12   10CE    D 207
13   3003    OUTL 0
14   10CF    D 208
15   3002    HI 0
16   10CF    D 208
17   6002    J loop
```

### UART RX · 115,200 baud, 8N1, P0

Leave P0 released. The peer must present idle high before the first falling edge. Let e be retirement of FALL. MARK retires at e+1. AT k followed by GET/INR therefore samples the synchronized stream at e+k+2. The receiver validates low at e+104, samples data at e+313, 521, 729, 938, 1146, 1354, 1563 and 1771, then checks stop high at e+1979. These are within half a tick of the ideal centers. Since FALL and every data sample traverse the same two flops, the fixed two stages cancel in their relative timing. Allow for input phase uncertainty, transmitter baud error and edge slew. As a test envelope, ±2% relative baud error drifts about 40 ticks by the stop center, inside the nominal 104-tick half-bit margin. The program discards a false start, faults with detail 01 on framing error, and otherwise pushes one byte. It rearms during the stop bit, in time for back-to-back frames. Eight INR samples overwrite all eight R bits.

```text
PC   word  instruction
00   4001    idle: HIGH 0
01   4003    FALL 0
02   0002    MARK
03   2066    AT 102
04   4004    GET 0
05   6080    JB idle
06   2137    AT 311
07   4006    INR 0
08   2207    AT 519
09   4006    INR 0
0A   22D7    AT 727
0B   4006    INR 0
0C   23A8    AT 936
0D   4006    INR 0
0E   2478    AT 1144
0F   4006    INR 0
10   2548    AT 1352
11   4006    INR 0
12   2619    AT 1561
13   4006    INR 0
14   26E9    AT 1769
15   4006    INR 0
16   27B9    AT 1977
17   4004    GET 0
18   611B    JNB frame
19   0005    PUSH
1A   6000    J idle
1B   9001    frame: TRAP 1
```

### SPI controller · 1 MHz, eight gap-free full-duplex bytes

P0=SCK, P1=MOSI, P2=MISO (released throughout), P3=CS_N. MSB first. Before RUN, set CFG=03, flush if needed, and queue eight TX bytes; drain the eight RX bytes after completion. For a later burst, reload only the FIFOs and RUN the same image. Chip select remains low across all 64 bits. HALT retains CS high and the correct idle clock. The host SPI port is separate and always uses mode 0.

| Mode | CPOL, CPHA | Leading edge | Trailing edge | Data sampling |
|---|---|---|---|---|
| 0 | 0,0 | rising | falling | leading |
| 1 | 0,1 | rising | falling | trailing |
| 2 | 1,0 | falling | rising | leading |
| 3 | 1,1 | falling | rising | trailing |

**CPHA=0.** Let m be retirement of PER at `bit`. At m+1 set idle polarity (the prior bit's trailing edge, except on the first bit). At +2 launch MOSI, at +13 issue the leading sample edge, and at +15 retire INL. F1 captured MISO on +13. The next bit's +1 is the trailing edge, exactly 12 ticks later. On a byte boundary DX, PUSH, DY, PULL, LX, J finish by +21, leaving the next PER eligible at +24. On the final bit a separate PER/idle pair supplies the 64th trailing edge. MOSI has 11 core ticks of setup before the sampling edge.

**Columns A and B are modes 0 and 2.**

```text
PC   word A  word B  instruction (A / B when different)
00   3032    3032    HI 3
01   3001    3002    LO 0  /  HI 0
02   3011    3011    LO 1
03   5308    5308    LY 8
04   0004    0004    PULL
05   5208    5208    LX 8
06   3031    3031    LO 3
07   0002    0002    MARK
08   B018    B018    bit: PER 24
09   3001    3002    LO 0  /  HI 0
0A   3014    3014    OUTH 1
0B   200C    200C    AT 12
0C   3002    3001    HI 0  /  LO 0
0D   1000    1000    D 1
0E   4025    4025    INL 2
0F   7008    7008    DX bit
10   0005    0005    PUSH
11   7097    7097    DY more
12   B018    B018    PER 24
13   3001    3002    LO 0  /  HI 0
14   100B    100B    D 12
15   3032    3032    HI 3
16   0000    0000    HALT
17   0004    0004    more: PULL
18   5208    5208    LX 8
19   6008    6008    J bit
```

**CPHA=1.** Relative to PER at m, issue the leading launch edge at +3, change MOSI at +4, issue the trailing sample edge at +15, and retire INL at +17. The longest boundary path is DX +18, PUSH +19, DY +20, PULL +21, LX +22, J +23, PER +24. The next leading edge is +27. MOSI again has 11 ticks of setup. In all modes the 128 SCK transitions are 12 ticks apart, including the seven interior byte boundaries. Each complete SCK period is 24 ticks = 1 µs. Changing MOSI one tick after the shift edge is intentional. It leaves hold margin at the preceding sample and setup margin at the next one. [SPI mode definitions](https://onlinedocs.microchip.com/oxy/GUID-DDF2C9BC-07FB-4ABF-938A-774B157B4519-en-US-11/GUID-0E901CC4-8D8D-458D-8FF5-8898F0C41259.html)

**Columns A and B are modes 1 and 3.**

```text
PC   word A  word B  instruction (A / B when different)
00   3032    3032    HI 3
01   3001    3002    LO 0  /  HI 0
02   3011    3011    LO 1
03   5308    5308    LY 8
04   0004    0004    PULL
05   5208    5208    LX 8
06   3031    3031    LO 3
07   0002    0002    MARK
08   B018    B018    bit: PER 24
09   2002    2002    AT 2
0A   3002    3001    HI 0  /  LO 0
0B   3014    3014    OUTH 1
0C   200E    200E    AT 14
0D   3001    3002    LO 0  /  HI 0
0E   1000    1000    D 1
0F   4025    4025    INL 2
10   7008    7008    DX bit
11   0005    0005    PUSH
12   7096    7096    DY more
13   100B    100B    D 12
14   3032    3032    HI 3
15   0000    0000    HALT
16   0004    0004    more: PULL
17   5208    5208    LX 8
18   6008    6008    J bit
```

The MISO capture occurs on the internal edge launching the external sample clock. For each data bit the peer must have returned stable MISO to F1 by that edge and satisfy F1 hold. Allow about 500 ns minus outbound/inbound pad and board delays, peer clock-to-out, setup and skew from the preceding launch edge. For the first CPHA=0 bit the peer must preload MISO after CS assertion. RX assembly and FIFO housekeeping consume the slots shown above. They never lengthen SCK under the eight-byte precondition.


### I²C controller · 400 kHz and 100 kHz, stretch-aware combined transfer

P0=SDA and P1=SCL. BEGIN with OD=03. Preload three bytes in TX: the 7-bit address shifted left with write bit 0, one register/data byte, and the address shifted left with read bit 1. Provide two free RX slots and use CFG=03. The image sends START, address-write, data, repeated START, address-read, receives two bytes, ACKs the first, NACKs the second, and generates STOP. A target NACK on **any** transmitted byte branches to STOP then TRAP 02. An intentional controller NACK on the last read is normal. R is pushed before the ninth sample can shift it. The host can terminate an indefinitely stretched transfer with STOP or reset; host STOP releases both pins immediately and does not synthesize an I²C STOP waveform. This image assumes a single controller, an initially free bus, and a clean Pmod. A stuck line can hold a level wait indefinitely.

**Columns A and B are Fast-mode and Standard-mode.** The two images share addresses. Each calls at most two levels deep (main → byte routine → pulse).

```text
PC   word A  word B  instruction (A / B when different)
00   3000    3000    Z 0
01   3010    3010    Z 1
02   4011    4011    HIGH 1
03   4001    4001    HIGH 0
04   1027    107B    D 40  /  D 124
05   3001    3001    LO 0
06   1019    106F    D 26  /  D 112
07   0002    0002    MARK
08   3011    3011    LO 1
09   801C    801C    CALL write
0A   609A    609A    JB bad
0B   801C    801C    CALL write
0C   609A    609A    JB bad
0D   802D    802D    CALL restart
0E   801C    801C    CALL write
0F   609A    609A    JB bad
10   8026    8026    CALL read
11   2009    2009    AT 9
12   3001    3001    LO 0
13   8043    8043    CALL pulse
14   8026    8026    CALL read
15   2009    2009    AT 9
16   3000    3000    Z 0
17   8043    8043    CALL pulse
18   8039    8039    CALL stop
19   0000    0000    HALT
1A   8039    8039    bad: CALL stop
1B   9002    9002    TRAP 2
1C   0004    0004    write: PULL
1D   5208    5208    LX 8
1E   2009    2009    wb: AT 9
1F   3004    3004    OUTH 0
20   8043    8043    CALL pulse
21   701E    701E    DX wb
22   2009    2009    AT 9
23   3000    3000    Z 0
24   8043    8043    CALL pulse
25   0003    0003    RET
26   5208    5208    read: LX 8
27   2009    2009    AT 9
28   3000    3000    Z 0
29   8043    8043    rb: CALL pulse
2A   7029    7029    DX rb
2B   0005    0005    PUSH
2C   0003    0003    RET
2D   2009    2009    restart: AT 9
2E   3000    3000    Z 0
2F   4001    4001    HIGH 0
30   2028    2084    AT 40  /  AT 132
31   3010    3010    Z 1
32   4011    4011    HIGH 1
33   1013    1077    D 20  /  D 120
34   3001    3001    LO 0
35   1019    106F    D 26  /  D 112
36   0002    0002    MARK
37   3011    3011    LO 1
38   0003    0003    RET
39   2009    2009    stop: AT 9
3A   3001    3001    LO 0
3B   2028    2084    AT 40  /  AT 132
3C   3010    3010    Z 1
3D   4011    4011    HIGH 1
3E   1013    1077    D 20  /  D 120
3F   3000    3000    Z 0
40   4001    4001    HIGH 0
41   1027    107B    D 40  /  D 124
42   0003    0003    RET
43   2028    2084    pulse: AT 40  /  pulse: AT 132
44   3010    3010    Z 1
45   4011    4011    HIGH 1
46   100D    105F    D 14  /  D 96
47   4005    4005    INL 0
48   B03C    B0F0    PER 60  /  PER 240
49   3011    3011    LO 1
4A   0003    0003    RET
```

#### Bit-cell ledger

Let f be the edge that drives SCL low. MARK or PER retired at f−1. Denote period by P, the low-phase setting by L and the explicit post-high delay by H. Fast uses (P,L,H)=(60,40,14); Standard uses (240,132,96). AT 9 places a changed SDA bit at f+9 under the queue preconditions. A repeated read bit simply leaves SDA released. Code around ACK, byte boundaries, and read/STOP setup reaches the scheduled SDA slot in time. On the first bit after repeated START a RET, CALL, PULL and LX still leave time to reach AT 9. There are 45 data/ACK clock pulses in a successful combined transaction (five groups of nine). The repeated-START interval and STOP/bus-free interval have separate timings.

| Pulse event | General core edge | Fast, prompt rise | Standard, prompt rise |
|---|---|---:|---:|
| Release SCL after AT L | f+L | f+40 | f+132 |
| HIGH SCL retires on observed high | w, at least release+3 for a self-generated rise first captured one edge later | f+43 | f+135 |
| INL reads SDA after D H | w+H+1 | f+58 | f+232 |
| PER retires | max(f−1+P, w+H+2) | f+59 | f+239 |
| Next SCL falling command | max(f+P, w+H+3) | **f+60** | **f+240** |

The cell period is therefore exactly 2.500 µs or 10.000 µs on the prompt-rise path. Waiting for SCL high occurs on **every** data and ACK clock and on repeated START and STOP. If a peer holds SCL low, w moves later and the max expression preserves the observed-high interval. On a Fast-mode bit, for example, w=f+54 gives the next fall at f+71. There is no “catch-up” pulse after a stretch and no claim of sustained 400 kHz while stretched.

**Electrical envelope for the proof.** A tick is 41.667 ns. Measure at the bus thresholds on the selected pull-up/load combination. Assume a driven SCL or SDA reaches the low threshold within 8 ticks of its command; SDA driven or released for a Fast-mode data change reaches its required valid threshold within 8 ticks. Require SCL to have reached its high timing threshold by at most one tick **after** HIGH retires. A prompt-rise 400 kHz waveform additionally requires the released clock to be sampled high by F1 on the next edge (or enough residual PER slack). A slower valid rise is handled by waiting; it can reduce the achieved bus rate. Board rise/fall maxima and input filtering are separate electrical qualification items. The two-flop sampler has no dedicated 50 ns spike filter.

- Fast low time is at least (40−8) ticks = **1.333 µs**, above 1.3 µs. Measured high time is at least (14+3−1) ticks = **0.667 µs**, above 0.6 µs. A data transition at f+9 occurs after SCL is physically low, is valid by f+17, and has at least 23 ticks setup before release. Even measured from f, 17 ticks = 0.708 µs fits the 0.9 µs data-valid limit.
- Standard low time is at least (132−8) ticks = **5.167 µs**, above 4.7 µs. High time is at least (96+3−1) ticks = **4.083 µs**, above 4.0 µs. Qualify the Standard-mode Pmod's slower permitted data rise against its 250 ns setup and 3.45 µs data-valid limits.
- Repeated START releases SDA while SCL is low and checks SDA high before releasing SCL. After observed SCL high it waits 20 / 120 ticks (Fast / Standard) before driving SDA low. START hold uses D 26 / 112 followed by MARK and SCL low. Even allowing eight ticks for SDA to fall, these holds exceed 0.6 / 4.0 µs. STOP prepares SDA low, releases SCL, waits for observed high, waits 20 / 120 ticks, and releases SDA. It then observes SDA high and waits 40 / 124 ticks before returning. Qualify tBUF from the actual STOP threshold to the next START (minima 1.3 / 4.7 µs); each RUN also waits for an idle bus and repeats this guard. [NXP UM10204 Rev. 7, §§3.1.9, 6.1 and Table 11](https://www.nxp.com/docs/en/user-guide/UM10204.pdf)

## 6. Acceptance evidence for implementation

1. Exhaustively classify all 65,536 instruction words for legality, encoding, and side effects. Compare assembler output and CRC with every column above. Verify delay values 1 and 4096, expired AT, late PER, PC wrap, both counter wraps, and return stack faults.
2. At RTL and post-layout GLS, interrupt reset during a WORD frame, a partial image, a blocked wait, and an active pin drive. Check all IMEM/FIFO cells after reset, OE while reset is active, the delayed RUN entry, and a clean second image after deselection/reselection. Inject bad CRC, duplicate/skipped words, malformed frames, and FIFO operations at counts 0, 1, 7 and 8 with simultaneous producers/consumers.
3. Use an independent pin-level peer to verify all 64 SPI TX and RX bits and every half-cycle, UART false start/bad stop/back-to-back bytes, and I²C ACK/NACK, repeated START, variable rise delays and stretching on the ninth clock. Measure pad-threshold timing separately from core-cycle listings. Close 24 MHz at the applicable corners, check I/O constraints, route and precheck before labeling that frequency verified.
4. Freeze this ISA and its opcode map before writing the SWD transfer/WAIT-retry image. Exercise turnaround, parity and retries on the same RTL. The evidence here is hand assembly and cycle accounting; RTL, place-and-route, GLS and silicon measurements are subsequent deliverables.

