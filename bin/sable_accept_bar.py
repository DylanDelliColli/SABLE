#!/usr/bin/env python3
"""sable_accept_bar — S4 sample-size derivation for the Accept protocol
(SABLE-5lli.6 / SABLE-4nmi).

A statistical acceptance bar is only meaningful when it is DERIVED from the
defect's base rate, not picked as a round number. Given a per-trial defect
probability p, this module answers the two questions an agent needs before
setting or trusting a bar:

  false_green_probability(n, p) -> P(all n trials pass | the bug is fully
      present) = (1-p)**n. This is the number a round-number bar hides.
  bar_for_confidence(p, confidence=0.95) -> the smallest n such that
      false_green_probability(n, p) <= 1 - confidence, i.e. the sample size
      that observes the defect at the stated confidence.

Verified against SABLE-qby7's own numbers (the case that founded this rule):
n=100 at p=1/300 -> ~0.716 false-green probability; the 95%-confidence bar at
p=1/300 -> ~897.
"""
from __future__ import annotations

import math


def false_green_probability(n: int, p: float) -> float:
    """P(all n independent trials pass) when the defect is present at rate p."""
    if not 0 <= p <= 1:
        raise ValueError(f"p must be in [0, 1], got {p}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    return (1 - p) ** n


def bar_for_confidence(p: float, confidence: float = 0.95) -> int:
    """Smallest n whose false-green probability is <= 1 - confidence."""
    if not 0 < p < 1:
        raise ValueError(f"p must be in (0, 1), got {p}")
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    residual = 1 - confidence
    return round(math.log(residual) / math.log(1 - p))
