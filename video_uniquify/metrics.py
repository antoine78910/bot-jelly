"""Similarity metrics, ported from the prototype: pHash, full-frame SSIM, chromaprint.

Fail closed: chromaprint_similarity() returns None when the fingerprints are too
short or do not overlap enough, and the guard treats None like a failure.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

MIN_FP_ITEMS = 40  # about 5 s of audio (chromaprint emits ~8 items per second)
MIN_FP_OVERLAP = 20
MAX_FP_OFFSET = 50


def luma(x: np.ndarray) -> np.ndarray:
    x = x.astype(np.float64)
    return x[..., 0] * 0.299 + x[..., 1] * 0.587 + x[..., 2] * 0.114


def gauss_ssim(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float64)
    b = b.astype(np.float64)

    def blur(x: np.ndarray) -> np.ndarray:
        k = np.exp(-np.arange(-5, 6) ** 2 / (2 * 1.5 ** 2))
        k /= k.sum()
        x = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 0, x)
        return np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, x)

    mu1, mu2 = blur(a), blur(b)
    s1 = blur(a * a) - mu1 ** 2
    s2 = blur(b * b) - mu2 ** 2
    s12 = blur(a * b) - mu1 * mu2
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ssim = ((2 * mu1 * mu2 + c1) * (2 * s12 + c2)) / (
        (mu1 ** 2 + mu2 ** 2 + c1) * (s1 + s2 + c2)
    )
    return float(ssim[8:-8, 8:-8].mean())


def fullframe_ssim(a: np.ndarray, b: np.ndarray) -> float:
    ra = np.asarray(Image.fromarray(a).resize((270, 480)), np.float32)
    rb = np.asarray(Image.fromarray(b).resize((270, 480)), np.float32)
    return gauss_ssim(luma(ra), luma(rb))


def phash_bits(img: np.ndarray) -> np.ndarray:
    g = np.asarray(Image.fromarray(img).convert("L").resize((32, 32), Image.LANCZOS), np.float64)
    n = 32
    c = np.cos(np.pi * (2 * np.arange(n)[:, None] + 1) * np.arange(n)[None, :] / (2 * n))
    d = c.T @ g @ c
    d = d[:8, :8].ravel()
    return d > np.median(d[1:])


def bits_to_hex(bits: np.ndarray) -> str:
    return np.packbits(bits.astype(np.uint8)).tobytes().hex()


def hex_to_bits(value: str) -> np.ndarray:
    raw = np.frombuffer(bytes.fromhex(value), np.uint8)
    bits = np.unpackbits(raw).astype(bool)
    if bits.size != 64:
        raise ValueError("pHash must be 64 bits")
    return bits


def bits_distance(a: np.ndarray, b: np.ndarray) -> int:
    return int((a != b).sum())


def phash_dist(a: np.ndarray, b: np.ndarray) -> int:
    return bits_distance(phash_bits(a), phash_bits(b))


def chromaprint_similarity(a: np.ndarray, b: np.ndarray, max_off: int = MAX_FP_OFFSET) -> float | None:
    """Bit-match rate at the best offset, rescaled so chance ~ 0 and identity ~ 1.

    None when either print is shorter than MIN_FP_ITEMS or no offset overlaps by
    MIN_FP_OVERLAP items. The prototype returned 0.0 there, which reads as "very
    different" and would have let an unmeasurable render through.
    """
    a = np.asarray(a, dtype=np.uint32)
    b = np.asarray(b, dtype=np.uint32)
    if a.size < MIN_FP_ITEMS or b.size < MIN_FP_ITEMS:
        return None
    best: float | None = None
    for off in range(-max_off, max_off + 1):
        if off >= 0:
            aa, bb = a[off:], b
        else:
            aa, bb = a, b[-off:]
        n = min(len(aa), len(bb))
        if n < MIN_FP_OVERLAP:
            continue
        xor = aa[:n] ^ bb[:n]
        bits = np.unpackbits(xor.view(np.uint8))
        sim = 1.0 - bits.sum() / (n * 32.0)
        if best is None or sim > best:
            best = sim
    if best is None:
        return None
    # Random 32-bit hashes match ~50% of bits; map chance to 0, identity to 1.
    return float(max(0.0, min(1.0, (best - 0.5) * 2.0)))
