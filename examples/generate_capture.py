"""Generate a synthetic capture containing 610 spatial samples."""

import argparse
from math import pi, sin
from pathlib import Path
from struct import pack


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with args.output.open("xb") as capture:
        for packet in range(1, 62):
            counts = [
                round(2000 + 70 * sin(2 * pi * ((packet - 1) * 10 + index) / 37))
                for index in range(10)
            ]
            header = bytes((0xFA, 0x1A, 0xA3, 0, 0, 0, (10 * packet) % 256))
            frame = header + pack(">10H", *counts)
            capture.write(frame + bytes((sum(frame) & 0xFF,)))


if __name__ == "__main__":
    main()
