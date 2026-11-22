"""Who won each paired attempt, by the owner's victory rule (docs/PRODUCT.md, "What wins"; 2026-11-21).

Both contestants play the same world on each attempt, so attempts are judged in pairs, in this order:

1. **Success.** One succeeded (passed) and the other didn't: a clear win.
2. **Both succeeded:** the more efficient one wins (fewest mistakes, least money, tokens and time;
   `scorers/efficiency.py`).
3. **Neither succeeded:** the one that got furthest wins (the larger share of items decided correctly).
   Equal progress goes to efficiency.
4. **No real difference** (efficiency within TIE_MARGIN): a tie, stated as such. No winner is made up.

The match winner is whoever won more pairs. Equal pair wins go to the mean efficiency score, and if that is
also within the margin, the match is a draw.
"""

from scorers.efficiency import pair_scores

TIE_MARGIN = 0.01  # efficiency scores this close are "no real difference" (timing noise must not pick a winner)


def success(result: dict) -> bool:
    return bool(result.get("passed"))


def progress(result: dict) -> float:
    """How far the attempt got: the share of items decided correctly (1.0 = all), or for cases without
    items, whether the end result was right."""
    details = result.get("grade_details") or {}
    items = details.get("items")
    if items:
        return (details.get("correct") or 0) / items
    return 1.0 if result.get("task_correct") else 0.0


def pair_outcome(a: dict, b: dict, profile: str = "balanced") -> dict:
    """{'winner': 'A' | 'B' | 'tie', 'by': 'success' | 'progress' | 'efficiency' | None, 'efficiency': (A, B)}."""
    eff = pair_scores(a, b, profile)
    sa, sb = success(a), success(b)
    if sa != sb:
        return {"winner": "A" if sa else "B", "by": "success", "efficiency": eff}
    if not sa:
        pa, pb = progress(a), progress(b)
        if abs(pa - pb) > 1e-9:
            return {"winner": "A" if pa > pb else "B", "by": "progress", "efficiency": eff}
    if abs(eff[0] - eff[1]) > TIE_MARGIN:
        return {"winner": "A" if eff[0] < eff[1] else "B", "by": "efficiency", "efficiency": eff}
    return {"winner": "tie", "by": None, "efficiency": eff}


def summary(pairs: list[tuple[dict, dict]], profile: str = "balanced") -> dict:
    """Pair wins per side and how they were decided; the match winner by the rule above."""
    outcomes = [pair_outcome(a, b, profile) for a, b in pairs]
    by = {
        side: {k: sum(1 for o in outcomes if o["winner"] == side and o["by"] == k) for k in BY} for side in ("A", "B")
    }
    a_wins = sum(by["A"].values())
    b_wins = sum(by["B"].values())
    n = len(outcomes)
    mean_a = sum(o["efficiency"][0] for o in outcomes) / n if n else None
    mean_b = sum(o["efficiency"][1] for o in outcomes) / n if n else None
    if not n:
        winner, decided = "no contest", None
    elif a_wins != b_wins:
        winner, decided = ("A" if a_wins > b_wins else "B"), "pair wins"
    elif abs(mean_a - mean_b) > TIE_MARGIN:
        winner, decided = ("A" if mean_a < mean_b else "B"), "efficiency (pair wins were level)"
    else:
        winner, decided = "draw", None
    return {
        "pairs": n,
        "a_wins": a_wins,
        "b_wins": b_wins,
        "ties": n - a_wins - b_wins,
        "by": by,
        "winner": winner,
        "decided_by": decided,
        "per_pair": [o["winner"] + (f" ({o['by']})" if o["by"] else "") for o in outcomes],
    }


BY = ("success", "progress", "efficiency")
