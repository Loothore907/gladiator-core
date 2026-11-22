"""Efficiency: who reaches the finish line with the fewest mistakes, the least money, tokens and time.

The core score of a Gladiator match (docs/PRODUCT.md, "What wins"). Both contestants play the same world on
each attempt, so attempts are compared in pairs. For each pair, every component is divided by the pair's mean
(1.00 = even, below 1 = better than the other side), weighted by the suite's profile and summed:

    score = w_mistakes * mistakes_ratio + w_cost * cost_ratio + w_tokens * tokens_ratio + w_time * time_ratio
            + DNF_PENALTY if the attempt ran out of its budget or time,
              or DNF_PENALTY * (share of items left undecided) otherwise

Lower is better; a pair where both sides did exactly the same scores 1.00 each. Mistakes are severity points
from the grader (a duplicate refund weighs more than an unread record), counted as 1 + points so a clean run
compares sensibly with a messy one. The winner has the lower mean score over the attempts both finished
comparably (provider outages and run-cap cut-offs stay out, as for every metric).

Why undecided items carry a penalty: each component is a ratio to the pair's mean, so it can never exceed 2.
Without it, a model that quits after one call (almost no cost, tokens or time) would beat a perfect run on every
profile, because its cheapness outweighs its bounded mistake ratio. The penalty is proportional, so skipping 2 of
18 items costs a little (on top of their mistake points) and skipping everything costs the full penalty.
"""

PROFILES = {
    # Mistakes dominate; cost, tokens and time break the rest.
    "balanced": {"mistakes": 0.50, "cost": 0.20, "tokens": 0.15, "time": 0.15},
    # Limited compute and a hard budget: money first (the sweet spot for small and mid-tier models).
    "budget": {"mistakes": 0.35, "cost": 0.45, "tokens": 0.10, "time": 0.10},
    # Speed first (interactive flows).
    "latency": {"mistakes": 0.35, "cost": 0.10, "tokens": 0.10, "time": 0.45},
}
DNF_PENALTY = 1.0
DNF = {"over_budget", "time_limit"}  # the model didn't reach the finish line (counts against it)
TIME_FLOOR_S = 1.0  # added to every time, so millisecond noise can't look like "twice as slow"
TIME_NOISE_S = 2.0  # times this close count as equal: network and machine jitter must never pick a winner
FAILED_UNGRADED = 10  # mistake points for a failed attempt whose grader gives no severity (e.g. v1 cases)


def mistake_points(result: dict) -> float:
    details = result.get("grade_details") or {}
    if details.get("error_severity") is not None:
        points = float(details["error_severity"])
        # Workflow mistakes without severity (blind retries, unread records, no finish) still count.
        points += 2 * (details.get("blind_retries_after_unknown_outcome") or 0)
        points += details.get("acted_without_reading") or 0
        points += 0 if details.get("explicit_finish", True) else 3
        return points
    return 0.0 if result.get("passed") else float(FAILED_UNGRADED)


def unfinished_share(result: dict) -> float:
    """How far short of the finish line: 1.0 if cut off by its own budget or time, else the share of items
    left undecided (0.0 = every item decided)."""
    if result.get("termination") in DNF:
        return 1.0
    details = result.get("grade_details") or {}
    missing = details.get("missing") or 0
    if not missing:
        return 0.0
    items = details.get("items") or 0
    return min(1.0, missing / items) if items else 1.0


def finished(result: dict) -> bool:
    return unfinished_share(result) == 0.0


def components(result: dict) -> dict:
    return {
        "mistakes": 1 + mistake_points(result),
        "cost": float(result.get("cost_usd") or 0),
        "tokens": float((result.get("input_tokens") or 0) + (result.get("output_tokens") or 0)),
        "time": float(result.get("wall_s") or 0) + TIME_FLOOR_S,
    }


def pair_scores(result_a: dict, result_b: dict, profile: str = "balanced") -> tuple[float, float]:
    """Efficiency scores (A, B) for one paired attempt: lower is better, 1.00 each when identical."""
    weights = PROFILES[profile]
    ca, cb = components(result_a), components(result_b)
    sa = sb = 0.0
    for name, w in weights.items():
        mean = (ca[name] + cb[name]) / 2
        ra, rb = (ca[name] / mean, cb[name] / mean) if mean > 0 else (1.0, 1.0)
        if name == "time" and abs(ca[name] - cb[name]) <= TIME_NOISE_S:
            ra = rb = 1.0
        sa += w * ra
        sb += w * rb
    sa += DNF_PENALTY * unfinished_share(result_a)
    sb += DNF_PENALTY * unfinished_share(result_b)
    return round(sa, 4), round(sb, 4)


def summary(pairs: list[tuple[dict, dict]], profile: str = "balanced") -> dict:
    """Mean efficiency per side, pair wins, and the raw components behind them."""
    scores = [pair_scores(a, b, profile) for a, b in pairs]
    n = len(scores)
    mean = lambda xs: round(sum(xs) / n, 4) if n else None  # noqa: E731
    raw = {
        side: {k: round(sum(components(p[i])[k] for p in pairs), 6) for k in PROFILES[profile]}
        for side, i in (("A", 0), ("B", 1))
    }
    return {
        "profile": profile,
        "weights": PROFILES[profile],
        "pairs": n,
        "A": mean([s[0] for s in scores]),
        "B": mean([s[1] for s in scores]),
        "a_wins": sum(1 for sa, sb in scores if sa < sb),
        "b_wins": sum(1 for sa, sb in scores if sb < sa),
        "a_unfinished": sum(1 for a, _ in pairs if not finished(a)),
        "b_unfinished": sum(1 for _, b in pairs if not finished(b)),
        "raw": raw,
        "per_pair": scores,
    }
