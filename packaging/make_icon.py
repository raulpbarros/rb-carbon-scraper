"""Generate `assets/app.ico` — the window's and the EXE's icon.

    python packaging/make_icon.py

Committed as a generator rather than only as a binary, so the mark can be
changed without a paint program and so the colours stay tied to the palette in
`gui/theme.py` rather than drifting from it.

The mark is a ledger: three rules of decreasing width on the palette's petrol.
That is what the window is — a list of registries, what is stored in each, and
how old it is — and it stays legible at 16 px, where an illustration would not.

Stdlib only (`zlib`, `struct`). The 256 px entry is a PNG because that is what
Windows expects at that size; every smaller entry is a plain 32-bit DIB, which
every Windows version reads without help.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "assets" / "app.ico"

SIZES = (16, 24, 32, 48, 64, 128, 256)

# Straight from gui/theme.py. Two colours: the ground and the rules on it.
PETROL = (0x0F, 0x52, 0x57)
PAPER = (0xF5, 0xF6, 0xF7)

#: (top, width) of each rule, as a fraction of the icon's side.
RULES = ((0.26, 0.56), (0.45, 0.42), (0.64, 0.28))
RULE_LEFT = 0.22
RULE_HEIGHT = 0.10


def pixels(size: int) -> list[list[tuple[int, int, int, int]]]:
    """The icon at one size, as rows of RGBA, top row first."""
    rows = [[(*PETROL, 255) for _ in range(size)] for _ in range(size)]
    left = max(1, round(RULE_LEFT * size))
    height = max(1, round(RULE_HEIGHT * size))
    for top_fraction, width_fraction in RULES:
        top = round(top_fraction * size)
        width = max(1, round(width_fraction * size))
        for y in range(top, min(size, top + height)):
            for x in range(left, min(size, left + width)):
                rows[y][x] = (*PAPER, 255)
    return rows


# --- the two container formats -------------------------------------------


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    size = len(rows)
    raw = b"".join(
        b"\x00" + bytes(value for pixel in row for value in pixel) for row in rows
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def dib(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    """A 32-bit BMP without its file header, bottom-up, plus an empty AND mask.

    The doubled height in the header is not a mistake: an icon DIB declares the
    colour rows and the transparency mask together, and a viewer that reads the
    stated height literally shows the top half stretched over the whole square.
    """
    size = len(rows)
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0
    )
    colour = b"".join(
        bytes(value for pixel in row for value in (pixel[2], pixel[1], pixel[0], pixel[3]))
        for row in reversed(rows)
    )
    mask_stride = ((size + 31) // 32) * 4
    return header + colour + b"\x00" * (mask_stride * size)


def ico(sizes: tuple[int, ...] = SIZES) -> bytes:
    images = [
        (size, png(pixels(size)) if size >= 256 else dib(pixels(size)))
        for size in sizes
    ]
    offset = 6 + 16 * len(images)
    directory = b""
    for size, blob in images:
        directory += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)
    return (
        struct.pack("<HHH", 0, 1, len(images))
        + directory
        + b"".join(blob for _, blob in images)
    )


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(ico())
    print(f"Wrote {TARGET} ({TARGET.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
