"""Multiple-testing and data-snooping control.

THE FAILURE THIS PREVENTS. Test twenty independent nulls at alpha=0.05
and you expect one "significant" result. Test the same idea across four
horizons, three conditionings and two instruments and you have run
twenty-four tests while feeling like you asked one question. The
finding that survives is then written up with an uncorrected p-value.

TWO SEPARATE COUNTS, AND THE SECOND IS THE HONEST ONE

  registered tests  the pre-registered hypotheses
  tests actually run  EVERY comparison executed, including exploratory
                      ones that were never written up

Corrections use the second. A ledger that only counted the tests you
chose to report would be the same self-deception in a new place, so
every call through this module increments the ledger whether or not the
result is ever shown to anyone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..provenance import utcnow


@dataclass
class SnoopingLedger:
    """Counts every test ever run in a study. Append-only."""
    entries: List[Dict[str, str]] = field(default_factory=list)

    def record(self, label: str, exploratory: bool = False) -> int:
        self.entries.append({
            "label": label,
            "exploratory": str(exploratory),
            "at": utcnow().isoformat(),
        })
        return len(self.entries)

    @property
    def total_tests(self) -> int:
        return len(self.entries)

    @property
    def exploratory_tests(self) -> int:
        return sum(1 for e in self.entries if e["exploratory"] == "True")

    def summary(self) -> dict:
        return {"total_tests_run": self.total_tests,
                "exploratory": self.exploratory_tests,
                "confirmatory": self.total_tests - self.exploratory_tests}


@dataclass(frozen=True)
class Corrected:
    label: str
    p_raw: float
    p_adjusted: float
    significant: bool
    rank: int
    method: str


def benjamini_hochberg(pvalues: Sequence[Tuple[str, float]],
                       fdr: float = 0.05) -> List[Corrected]:
    """Control the false discovery rate.

    Chosen over Bonferroni because at this stage the cost of missing a
    real effect (never investigating it) is higher than the cost of one
    extra candidate that later fails validation -- and the validation
    and holdout stages exist precisely to catch those. Bonferroni is
    available below for the final, confirmatory stage where the trade-off
    reverses.
    """
    if not pvalues:
        return []
    ordered = sorted(pvalues, key=lambda kv: kv[1])
    m = len(ordered)
    out: List[Corrected] = []
    # Step-up: find the largest rank meeting p <= (i/m)*fdr, then declare
    # everything at or below that rank significant.
    largest_significant_rank = 0
    for i, (_, p) in enumerate(ordered, start=1):
        if p <= (i / m) * fdr:
            largest_significant_rank = i
    # Monotone adjusted p-values.
    adjusted = [0.0] * m
    running = 1.0
    for i in range(m, 0, -1):
        p = ordered[i - 1][1]
        running = min(running, p * m / i)
        adjusted[i - 1] = running
    for i, (label, p) in enumerate(ordered, start=1):
        out.append(Corrected(label=label, p_raw=p, p_adjusted=adjusted[i - 1],
                             significant=i <= largest_significant_rank,
                             rank=i, method=f"benjamini-hochberg(fdr={fdr})"))
    return out


def bonferroni(pvalues: Sequence[Tuple[str, float]],
               alpha: float = 0.05) -> List[Corrected]:
    """Family-wise error control. Used for confirmatory claims."""
    m = len(pvalues)
    out = []
    for rank, (label, p) in enumerate(sorted(pvalues, key=lambda kv: kv[1]), start=1):
        adj = min(1.0, p * m)
        out.append(Corrected(label=label, p_raw=p, p_adjusted=adj,
                             significant=adj <= alpha, rank=rank,
                             method=f"bonferroni(alpha={alpha}, m={m})"))
    return out
