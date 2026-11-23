"""Assembler for the Timed Protocol Sequencer. Run with --help for CLI usage.

Source uses decimal operands by default, or explicit 0x/0b/0o prefixes.
Labels are case-insensitive. Commas or whitespace separate operands, and
';' or '#' starts a comment. .od sets the image mask (default zero).
V byte, E byte, and APPLY encode A0vv, A1ee, and A200 respectively.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import struct


SYSTEM = "HALT NOP MARK RET PULL PUSH CLRR R2T T2R CLRQ SETQ B2Q".split()
DRIVES = "Z LO HI OUTL OUTH OUTQ OUTB".split()
SAMPLES = "LOW HIGH RISE FALL GET INL INR".split()
REGISTERS = "TRXY"
IDENT = r"[A-Za-z_][A-Za-z_0-9]*"


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE, optionally continuing from a prior result."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


@dataclass(frozen=True)
class Image:
    od: int
    words: tuple
    used: int

    @property
    def payload(self):
        return struct.pack(">128H", *self.words)

    @property
    def crc(self):
        return crc16(bytes([self.od]) + self.payload)


def number(token, low, high):
    try:
        value = int(token, 10) if re.fullmatch(r"[+-]?\d+", token) else int(token, 0)
    except ValueError:
        raise ValueError(f"expected integer, got {token!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{token} outside {low}..{high}")
    return value


def encode(op, args, labels):
    def arity(n):
        if len(args) != n:
            raise ValueError(f"{op} expects {n} operand(s), got {len(args)}")

    def address(token):
        if re.fullmatch(IDENT, token):
            if token not in labels:
                raise ValueError(f"undefined label {token!r}")
            return labels[token]
        return number(token, 0, 127)

    if op in SYSTEM or op in ("APPLY", "A200"):
        arity(0)
        return SYSTEM.index(op) if op in SYSTEM else 0xA200
    if op in ("D", "AT", "PER"):
        arity(1)
        if op == "D":
            return 0x1000 + number(args[0], 1, 4096) - 1
        return (0x2000 if op == "AT" else 0xB000) | number(args[0], 0, 4095)
    if op in DRIVES + SAMPLES + ["PIN", "SENSE"]:
        arity(2 if op in ("PIN", "SENSE") else 1)
        drive = op in DRIVES or op == "PIN"
        actions = DRIVES if drive else SAMPLES
        token = args[0]
        bank = re.fullmatch(r"([PIO])(\d+)", token)
        if bank:
            kind, index = bank.groups()
            if kind == "P":
                pin = number(index, 0, 7)
            elif kind == ("O" if drive else "I"):
                pin = 8 + number(index, 0, 3)
            else:
                raise ValueError(f"wrong pin bank for {op}")
        else:
            pin = number(token, 0, 11)
        action = actions.index(op) if len(args) == 1 else number(args[1], 0, 6)
        if drive and action == 0 and pin >= 8:
            raise ValueError("only P pins can be released")
        return (0x3000 if drive else 0x4000) | pin << 4 | action
    if op in ("LIT", "LX", "LY"):
        arity(2 if op == "LIT" else 1)
        reg = args[0] if op == "LIT" else op[1]
        index = REGISTERS.index(reg) if reg in list(REGISTERS) else number(reg, 0, 3)
        return 0x5000 | index << 8 | number(args[-1], 0, 255)
    if op in ("BR", "J", "JB", "JNB"):
        arity(2 if op == "BR" else 1)
        cond = number(args[0], 0, 10) if op == "BR" else ("J", "JB", "JNB").index(op)
        return 0x6000 | cond << 7 | address(args[-1])
    if op in ("DX", "DY", "CALL"):
        arity(1)
        return {"DX": 0x7000, "DY": 0x7080, "CALL": 0x8000}[op] | address(args[0])
    if op in ("TRAP", "V", "E"):
        arity(1)
        return {"TRAP": 0x9000, "V": 0xA000, "E": 0xA100}[op] | number(args[0], 0, 255)
    raise ValueError(f"unknown mnemonic {op!r}")


def assemble(source):
    labels, instructions = {}, []
    od = None
    for line_no, raw in enumerate(source.splitlines(), 1):
        line = re.split(r"[;#]", raw, maxsplit=1)[0].strip().upper()
        try:
            while match := re.match(rf"({IDENT})\s*:\s*", line):
                name = match[1]
                if name in labels or len(instructions) == 128:
                    raise ValueError(f"duplicate or out-of-range label {name!r}")
                labels[name] = len(instructions)
                line = line[match.end():]
            if not line:
                continue
            if re.search(r"^\S+\s*,|,\s*(?:,|$)", line):
                raise ValueError("missing operand")
            op, *args = re.split(r"[\s,]+", line)
            if op == ".OD":
                if len(args) != 1 or od is not None:
                    raise ValueError("expected one .od directive with one mask")
                od = number(args[0], 0, 255)
            else:
                if len(instructions) >= 128:
                    raise ValueError("image exceeds 128 words")
                instructions.append((line_no, op, args))
        except ValueError as exc:
            raise ValueError(f"line {line_no}: {exc}") from None
    words = []
    for line_no, op, args in instructions:
        try:
            words.append(encode(op, args, labels))
        except ValueError as exc:
            raise ValueError(f"line {line_no}: {exc}") from None
    return Image(od or 0, tuple(words + [0] * (128 - len(words))), len(words))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="write 256 big-endian payload bytes")
    args = parser.parse_args()
    try:
        image = assemble(args.source.read_text(encoding="utf-8-sig"))
        if args.output:
            args.output.write_bytes(image.payload)
        else:
            for pc, word in enumerate(image.words):
                print(f"{pc:02X} {word:04X}")
        print(f"used={image.used} OD={image.od:02X} CRC={image.crc:04X}")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
