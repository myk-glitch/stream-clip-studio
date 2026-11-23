"""Machine-readable clock strip for timing verification.

A strip of 60 black/white cells across the top 6% of the frame encodes a 48-bit
millisecond value plus a CRC-8. It survives scaling and normal video compression
because each cell is large. The same layout is drawn by clock_overlay.html (for
OBS) and by footage.py (for generated local test footage), so clips from either
source can be decoded frame by frame.

Layout: [1,0] guard | 48 data bits (MSB first) | 8 CRC bits | [0,1] guard
"""

from __future__ import annotations

import numpy as np

DATA_BITS = 48
CRC_BITS = 8
CELLS = 2 + DATA_BITS + CRC_BITS + 2
STRIP_HEIGHT_FRAC = 0.06
_MASK = (1 << DATA_BITS) - 1


def crc8(data: bytes, poly: int = 0x07) -> int:
    """CRC-8/SMBUS (poly 0x07, init 0). Mirrored in clock_overlay.html."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode_bits(ms: int) -> list[int]:
    ms &= _MASK
    crc = crc8(ms.to_bytes(6, "big"))
    data = [(ms >> (DATA_BITS - 1 - i)) & 1 for i in range(DATA_BITS)]
    check = [(crc >> (CRC_BITS - 1 - i)) & 1 for i in range(CRC_BITS)]
    return [1, 0] + data + check + [0, 1]


def strip_height(frame_height: int) -> int:
    return max(8, int(round(frame_height * STRIP_HEIGHT_FRAC)))


def _cell_edges(width: int) -> np.ndarray:
    return np.linspace(0, width, CELLS + 1).round().astype(int)


def draw_strip(frame: np.ndarray, ms: int) -> None:
    """Draw the strip in place on an RGB uint8 frame (H, W, 3)."""
    h, w = frame.shape[:2]
    sh = strip_height(h)
    edges = _cell_edges(w)
    for i, bit in enumerate(encode_bits(ms)):
        frame[:sh, edges[i] : edges[i + 1]] = 255 if bit else 0


def decode_strip(frame: np.ndarray) -> int | None:
    """Return the encoded millisecond value, or None if the strip is unreadable."""
    gray = frame.mean(axis=2) if frame.ndim == 3 else frame.astype(float)
    h, w = gray.shape
    sh = strip_height(h)
    y0, y1 = int(sh * 0.25), max(int(sh * 0.75), int(sh * 0.25) + 1)
    edges = _cell_edges(w)
    bits = []
    for i in range(CELLS):
        a, b = edges[i], edges[i + 1]
        pad = max(1, (b - a) // 4)
        value = gray[y0:y1, a + pad : max(b - pad, a + pad + 1)].mean()
        bits.append(1 if value > 127 else 0)
    if bits[:2] != [1, 0] or bits[-2:] != [0, 1]:
        return None
    data = bits[2 : 2 + DATA_BITS]
    check = bits[2 + DATA_BITS : 2 + DATA_BITS + CRC_BITS]
    ms = int("".join(map(str, data)), 2)
    crc = int("".join(map(str, check)), 2)
    if crc8(ms.to_bytes(6, "big")) != crc:
        return None
    return ms
