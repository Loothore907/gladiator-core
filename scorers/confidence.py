"""How sure are we? Plain statistics for small samples (M2b, docs/PRODUCT.md section 7).

Both contestants play the same world on each attempt, so attempts are PAIRS. Pairs where both passed or
both failed say nothing about which model is better; only the split pairs (one passed, the other failed)
do. The exact sign test on those split pairs asks: if the two models were equally good, how likely is a
split at least this lopsided? Small likelihood (under 5%) = a real difference at this sample size.

Pass rates also get a 95% range (Wilson score interval), which stays honest for small n and for 0% / 100%.
"""

import math

Z95 = 1.959964


def wilson(passes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """95% range for a pass rate of passes/n."""
    if n == 0:
        return (0.0, 1.0)
    p = passes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def sign_test(a_only: int, b_only: int) -> float:
    """Two-sided exact p-value for a_only vs b_only split pairs under 'equally good' (p = 0.5)."""
    n = a_only + b_only
    if n == 0:
        return 1.0
    k = min(a_only, b_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired(results_a: list[dict], results_b: list[dict], comparable) -> dict:
    """Split pairs and the sign test over attempts both sides finished (`comparable(a, b)` decides)."""
    pairs = [(a, b) for a, b in zip(results_a, results_b, strict=True) if comparable(a, b)]
    a_only = sum(1 for a, b in pairs if a["passed"] and not b["passed"])
    b_only = sum(1 for a, b in pairs if b["passed"] and not a["passed"])
    return {"pairs": len(pairs), "a_only": a_only, "b_only": b_only, "p_value": sign_test(a_only, b_only)}


def statement(names: tuple[str, str], stats: dict) -> str:
    """One plain-English sentence about whether the gap is real."""
    a, b = names
    n, ao, bo, p = stats["pairs"], stats["a_only"], stats["b_only"], stats["p_value"]
    if n == 0:
        return "No attempts both models finished, so nothing can be said about who is better."
    if stats.get("metric") == "outcome":
        ties = n - ao - bo
        tail = f" ({ties} tied)" if ties else ""
        if ao == bo:
            return f"Over {n} paired attempts (same world for both), each model won {ao}{tail}: level on pairs."
        leader = a if ao > bo else b
        verdict = (
            f"a real difference at this sample size (chance between equals: {_pct(p)})"
            if p < 0.05
            else f"could still be luck (chance between equals: {_pct(p)})"
        )
        split = f"{max(ao, bo)}-{min(ao, bo)}"
        return f"Over {n} paired attempts (same world for both), {leader} won {split}{tail}: {verdict}."
    if stats.get("metric") == "efficiency":
        leader = a if ao > bo else b
        if ao == bo:
            return (
                f"Over {n} paired attempts (same world for both), each model was more efficient {ao} times: no winner."
            )
        verdict = (
            f"a real difference at this sample size (chance between equals: {_pct(p)})"
            if p < 0.05
            else f"could still be luck (chance between equals: {_pct(p)})"
        )
        split = f"{max(ao, bo)}-{min(ao, bo)}"
        return f"Over {n} paired attempts (same world for both), {leader} was more efficient {split}: {verdict}."
    if ao + bo == 0:
        return f"Over {n} paired attempts (same world for both) the models never split: no evidence either is better."
    leader = a if ao > bo else b
    split = f"{max(ao, bo)}-{min(ao, bo)}"
    if p < 0.05:
        verdict = f"a real difference at this sample size (chance of a split this lopsided between equals: {_pct(p)})"
    else:
        need = _pairs_needed(max(ao, bo), min(ao, bo))
        verdict = f"could still be luck (chance between equals: {_pct(p)})" + (
            f"; about {need} more split attempt{'s' if need != 1 else ''} at this rate would settle it" if need else ""
        )
    opening = f"Over {n} paired attempts (same world for both), the models split {ao + bo} times"
    return f"{opening}, {split} for {leader}: {verdict}."


def _pct(p: float) -> str:
    return "under 1%" if p < 0.01 else f"{p:.0%}"


def _pairs_needed(lead: int, trail: int, limit: int = 200) -> int | None:
    """How many more split pairs, at the current ratio, until the sign test reaches p < 0.05."""
    if lead == trail:
        return None
    ratio = lead / (lead + trail)
    for extra in range(1, limit):
        n = lead + trail + extra
        lo = round(n * ratio)
        if sign_test(lo, n - lo) < 0.05:
            return extra
    return None
