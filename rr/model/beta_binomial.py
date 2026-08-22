"""Empirical-Bayes Beta-Binomial over the observable feature cross, with shrinkage.

Why this and not a gradient-booster: every score has to be defensible out loud.
"this cell has 43 observations, 11 successes, posterior mean 0.26, 90% CI
[0.17, 0.37], shrunk toward its parent cell at 0.31" is a sentence a payments
engineer can argue with. A single number from a fitted tree is not.

Hierarchy, finest first. Each level's prior is its parent's posterior mean with
`shrinkage_k` pseudo-counts, so a cell with three observations barely moves off
its parent while a cell with three hundred is essentially its own empirical rate.
"""
from __future__ import annotations

import json
import pathlib
import random
from dataclasses import dataclass, asdict
from typing import Iterable, Optional

from rr.config import MODEL

# Strictly nested: each level's features are a subset of the level above, which is
# what makes "shrink toward the parent" well defined. Features are dropped in
# order of least decision relevance first, so the ones that carry the diagnosed
# headroom (issuer_rate, dom, hour) survive down to levels that actually have
# enough observations to resolve them.
LEVELS_V1: tuple[tuple[str, ...], ...] = (
    ("action", "cause", "method", "attempt_index", "time_bucket", "regime"),
    ("action", "cause", "method", "attempt_index", "regime"),
    ("action", "cause", "attempt_index", "regime"),
    ("action", "cause", "regime"),
    ("action", "regime"),
    ("action",),
    (),
)

# v2 is v1 REFINED, not v1 rearranged: the three new features are added as extra
# levels ABOVE v1's chain, and levels 3.. are v1 verbatim. So a v2 lookup that
# lacks evidence in the refined cells falls through to exactly the cell v1 would
# have used. Any difference between the two is therefore caused by the new
# features carrying signal, never by a reshuffled hierarchy.
LEVELS_V2: tuple[tuple[str, ...], ...] = (
    ("action", "cause", "method", "attempt_index", "time_bucket", "regime",
     "issuer_rate", "hour", "dom"),
    ("action", "cause", "method", "attempt_index", "time_bucket", "regime",
     "issuer_rate", "hour"),
    ("action", "cause", "method", "attempt_index", "time_bucket", "regime", "issuer_rate"),
) + LEVELS_V1

FEATURE_SETS = {"v1": LEVELS_V1, "v2": LEVELS_V2}
LEVELS = LEVELS_V1          # module-level default; instances carry their own


def time_bucket(elapsed_h: float, edges=MODEL.time_buckets_h) -> str:
    for i, e in enumerate(edges):
        if elapsed_h < e:
            return f"t{i}"
    return f"t{len(edges)}"


@dataclass(frozen=True)
class CellPosterior:
    mean: float
    lo: float
    hi: float
    n: int
    successes: int
    level: int          # 0 = finest; higher = more shrunk toward the global rate
    key: str

    def as_evidence(self) -> dict:
        """What gets written into the candidate_set so a score can be traced."""
        return {"p_mean": round(self.mean, 4), "p_ci90": [round(self.lo, 4), round(self.hi, 4)],
                "observations": self.n, "successes": self.successes,
                "cell_level": self.level, "cell": self.key}


def _key(level: tuple[str, ...], feat: dict) -> str:
    return "|".join(f"{k}={feat[k]}" for k in level) if level else "(global)"


class BetaBinomialModel:
    def __init__(self, cfg=MODEL, feature_version: str = "v1"):
        self.cfg = cfg
        self.feature_version = feature_version
        self.levels = FEATURE_SETS[feature_version]
        self.posteriors: dict[int, dict[str, CellPosterior]] = {}

    # ------------------------------------------------------------------ fit --
    def fit(self, observations: Iterable[dict], seed: int = 4242) -> "BetaBinomialModel":
        for i in range(len(self.levels) - 1):
            assert set(self.levels[i + 1]) <= set(self.levels[i]), (
                f"levels must be strictly nested for shrinkage to be defined: "
                f"level {i+1} is not a coarsening of level {i}")
        rows = list(observations)
        counts: dict[int, dict[str, list[int]]] = {i: {} for i in range(len(self.levels))}
        for r in rows:
            for i, level in enumerate(self.levels):
                k = _key(level, r)
                c = counts[i].setdefault(k, [0, 0])
                c[0] += int(bool(r["success"]))
                c[1] += 1

        rnd = random.Random(seed)
        # Coarsest level first, so each finer level can shrink toward its parent.
        for i in reversed(range(len(self.levels))):
            self.posteriors[i] = {}
            for k, (s, n) in counts[i].items():
                parent = self._parent_mean(i, k, counts)
                a = self.cfg.shrinkage_k * parent + s
                b = self.cfg.shrinkage_k * (1.0 - parent) + (n - s)
                draws = sorted(rnd.betavariate(max(a, 1e-6), max(b, 1e-6))
                               for _ in range(self.cfg.ci_samples))
                q = self.cfg.ci_alpha / 2
                self.posteriors[i][k] = CellPosterior(
                    mean=a / (a + b), lo=draws[int(q * len(draws))],
                    hi=draws[int((1 - q) * len(draws)) - 1],
                    n=n, successes=s, level=i, key=k)
        return self

    def _parent_mean(self, i: int, key: str, counts) -> float:
        if i == len(self.levels) - 1:
            return 0.5                      # Beta(1,1) at the root
        parent_level = self.levels[i + 1]
        parts = dict(p.split("=", 1) for p in key.split("|")) if key != "(global)" else {}
        pkey = "|".join(f"{k}={parts[k]}" for k in parent_level) if parent_level else "(global)"
        p = self.posteriors.get(i + 1, {}).get(pkey)
        return p.mean if p else 0.5

    # --------------------------------------------------------------- lookup --
    def lookup(self, **feat) -> CellPosterior:
        """Finest cell with enough evidence wins.

        A level-0 cell holding a single observation is shrunk almost entirely to
        its parent, so returning it is nearly harmless -- but not quite: it reports
        n=1 as the evidence behind a score, and it stops the walk before reaching a
        parent that actually has data. Require min_cell_observations before trusting
        a level, then fall through."""
        fallback = None
        for i, level in enumerate(self.levels):
            hit = self.posteriors.get(i, {}).get(_key(level, feat))
            if hit is None:
                continue
            if hit.n >= self.cfg.min_cell_observations:
                return hit
            fallback = fallback or hit
        return fallback or CellPosterior(0.5, 0.0, 1.0, 0, 0, len(self.levels), "(unfitted)")

    # ------------------------------------------------------------------- io --
    def save(self, path: pathlib.Path) -> None:
        path.write_text(json.dumps(
            {"feature_version": self.feature_version,
             "levels": [list(l) for l in self.levels], "config": asdict(self.cfg),
             "posteriors": {str(i): {k: asdict(v) for k, v in d.items()}
                            for i, d in self.posteriors.items()}}, indent=1))

    @classmethod
    def load(cls, path: pathlib.Path) -> "BetaBinomialModel":
        raw = json.loads(path.read_text())
        m = cls(feature_version=raw.get("feature_version", "v1"))
        m.posteriors = {int(i): {k: CellPosterior(**v) for k, v in d.items()}
                        for i, d in raw["posteriors"].items()}
        return m

    def summary(self) -> dict:
        return {f"level_{i}": len(d) for i, d in sorted(self.posteriors.items())}
