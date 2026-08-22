"""Common random numbers.

Every stochastic draw in this project is a pure function of a key tuple, never of
a stateful generator. Two policy arms that reach the same (intent, purpose, slot)
consume the SAME uniform, so a difference between arms is caused by the policy and
not by RNG divergence. This is the variance reduction that lets a 6k-row cohort
resolve a real effect instead of drowning it in noise.
"""
from __future__ import annotations

import hashlib
import math
import struct
from typing import Sequence


def u01(*keys: object) -> float:
    """Deterministic uniform in [0, 1) keyed on arbitrary values."""
    payload = "|".join(str(k) for k in keys).encode()
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return struct.unpack("<Q", digest)[0] / 2.0**64


def bernoulli(p: float, *keys: object) -> bool:
    return u01(*keys) < p


def randint(lo: int, hi: int, *keys: object) -> int:
    """Uniform integer in [lo, hi]."""
    return lo + int(u01(*keys) * (hi - lo + 1))


def choice(options: Sequence, weights: Sequence[float], *keys: object):
    total = float(sum(weights))
    target = u01(*keys) * total
    acc = 0.0
    for opt, w in zip(options, weights):
        acc += w
        if target < acc:
            return opt
    return options[-1]


def beta_like(mean: float, concentration: float, *keys: object) -> float:
    """A Beta-shaped draw without scipy: mean of two order statistics, then a
    logistic squash toward `mean`. Good enough for latent heterogeneity; we only
    need a smooth unimodal spread on (0, 1), not exact Beta moments."""
    u = u01(*keys, "a")
    v = u01(*keys, "b")
    z = (u + v) / 2.0 - 0.5  # symmetric, triangular-ish, in [-0.5, 0.5]
    spread = 1.0 / math.sqrt(max(concentration, 1e-6))
    x = mean + z * spread
    return min(max(x, 0.01), 0.99)


def exponential(scale: float, *keys: object) -> float:
    u = max(u01(*keys), 1e-12)
    return -scale * math.log(u)


def clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x
