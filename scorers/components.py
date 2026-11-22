"""Totals per contestant and the match verdict, using the suite's success rule.

Default rule (`primary: outcome`, scorers/outcome.py): per paired attempt, success first; if both succeed,
efficiency; if neither, whoever got furthest; no real difference is a tie. Most pair wins takes the match.
Other primaries (passed, efficiency, ...) compare totals in order, then the tie-break list.

Fairness: the verdict only counts cases that BOTH contestants got to finish.
An attempt is left out if either side was cut off by the $ cap, never ran
it, or hit a provider failure (outage, overload, network) that survived the
retries; the report lists those. Model mistakes (invalid actions, not
calling the tool, wrong answers) always count.
"""

HIGHER_IS_BETTER = {"passed": True, "task_correct": True, "workflow_correct": True, "cost_usd": False, "wall_s": False}
# "efficiency" (lower is better) is scored per paired attempt: see scorers/efficiency.py.
# Left out of the verdict: things that say nothing about the model's ability.
#   not_run / budget_stop: the attempt never happened (or was cut off by the $ cap)
#   provider_error: the provider failed (overloaded, network, server error) after retries
NOT_COMPARABLE = {"not_run", "budget_stop", "provider_error"}


def reliability(results: list[dict]) -> dict:
    """Clean every time (pass^k, as in tau-bench): the share of tests where EVERY counted repeat passed. A model
    that passes 80% of single tries can be clean on far fewer tests, and that is what a workflow owner needs."""
    by_node: dict[str, list[bool]] = {}
    for r in results:
        by_node.setdefault(r["node"], []).append(bool(r["passed"]))
    clean = sum(1 for runs in by_node.values() if runs and all(runs))
    k = min((len(v) for v in by_node.values()), default=0)
    return {"tests": len(by_node), "clean_every_time": clean, "repeats": k}


def totals(results: list[dict]) -> dict:
    return {
        "cases": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "first_pass": sum(1 for r in results if r.get("first_pass")),
        "task_correct": sum(1 for r in results if r["task_correct"]),
        "workflow_correct": sum(1 for r in results if r["workflow_correct"]),
        "workflow_graded": sum(1 for r in results if r["workflow_correct"] is not None),
        "calls": sum(r["calls"] for r in results),
        "cost_usd": sum(r["cost_usd"] for r in results),
        "wall_s": round(sum(r["wall_s"] for r in results), 1),
    }


def verdict(record: dict) -> dict:
    ra = record["contestants"]["A"]["results"]
    rb = record["contestants"]["B"]["results"]
    both = [
        (a, b)
        for a, b in zip(ra, rb, strict=True)
        if a["termination"] not in NOT_COMPARABLE and b["termination"] not in NOT_COMPARABLE
    ]
    excluded = [
        a["name"]
        + (f" #{a.get('attempt', 1)}" if len(ra) != len({r["node"] for r in ra}) else "")
        + f" ({a['termination'] if a['termination'] in NOT_COMPARABLE else b['termination']})"
        for a, b in zip(ra, rb, strict=True)
        if a["termination"] in NOT_COMPARABLE or b["termination"] in NOT_COMPARABLE
    ]
    a = totals([x for x, _ in both])
    b = totals([y for _, y in both])
    from scorers.confidence import paired

    def comparable(x, y):
        return x["termination"] not in NOT_COMPARABLE and y["termination"] not in NOT_COMPARABLE

    confidence = paired(ra, rb, comparable)
    from scorers.efficiency import summary as efficiency_summary

    rule = record["suite"]["success"]
    efficiency = efficiency_summary(both, rule.get("profile", "balanced"))
    if rule["primary"] == "outcome":  # the sign test then runs on pair wins under the victory rule
        from scorers.confidence import sign_test
        from scorers.outcome import summary as outcome_summary

        outcome = outcome_summary(both, rule.get("profile", "balanced"))
        confidence = {
            "pairs": outcome["pairs"],
            "a_only": outcome["a_wins"],
            "b_only": outcome["b_wins"],
            "p_value": sign_test(outcome["a_wins"], outcome["b_wins"]),
            "metric": "outcome",
        }
    elif rule["primary"] == "efficiency":  # the sign test then runs on efficiency wins per pair
        from scorers.confidence import sign_test

        confidence = {
            "pairs": efficiency["pairs"],
            "a_only": efficiency["a_wins"],
            "b_only": efficiency["b_wins"],
            "p_value": sign_test(efficiency["a_wins"], efficiency["b_wins"]),
            "metric": "efficiency",
        }
    notes = []
    if excluded:
        notes.append(
            f"{len(excluded)} attempt(s) left out of the verdict because one side hit the $ cap, never ran, "
            f"or had a provider failure (not the model's fault): {', '.join(excluded)}."
        )
    result = {
        "totals": {"A": a, "B": b},
        "reliability": {"A": reliability([x for x, _ in both]), "B": reliability([y for _, y in both])},
        "all_cases": {"A": totals(ra), "B": totals(rb)},
        "excluded": excluded,
        "notes": notes,
        "confidence": confidence,
        "efficiency": efficiency,
    }
    if not both:
        return {**result, "winner": "no contest", "decided_by": None}
    if rule["primary"] == "outcome":
        result["outcome"] = outcome
        if outcome["winner"] in ("A", "B") and abs(outcome["a_wins"] - outcome["b_wins"]) <= 1 and outcome["pairs"] > 1:
            notes.append(
                "Close result: the pair wins split almost evenly. Run more repeats before reading much into it."
            )
        if outcome["winner"] == "draw":
            notes.append("No real difference: equal pair wins and efficiency within 1%. A true tie for this task.")
        return {**result, "winner": outcome["winner"], "decided_by": outcome["decided_by"]}
    # Efficiency scores within 1% of each other are a tie (timing noise must not pick a winner).
    if (
        rule["primary"] == "efficiency"
        and efficiency["A"] is not None
        and abs(efficiency["A"] - efficiency["B"]) > 0.01
    ):
        if abs(efficiency["a_wins"] - efficiency["b_wins"]) <= 1 and efficiency["pairs"] > 1:
            notes.append(
                "Close result: the efficiency wins split almost evenly. Run more repeats before reading much into it."
            )
        return {**result, "winner": "A" if efficiency["A"] < efficiency["B"] else "B", "decided_by": "efficiency"}
    for metric in [m for m in [rule["primary"], *rule["tiebreak"]] if m != "efficiency"]:
        if a[metric] == b[metric]:
            continue
        better_a = (a[metric] > b[metric]) == HIGHER_IS_BETTER[metric]
        if metric == rule["primary"] and abs(a[metric] - b[metric]) <= 1:
            notes.append("Close result: decided by a single attempt. Run more repeats before reading much into it.")
        elif metric != rule["primary"]:
            notes.append(f"Equal on `{rule['primary']}`; the winner was picked by the tie-breaker `{metric}`.")
        return {**result, "winner": "A" if better_a else "B", "decided_by": metric}
    return {**result, "winner": "draw", "decided_by": None}


def by_node(results: list[dict]) -> list[dict]:
    """Group attempts per node, in suite order, for the report."""
    groups: dict[str, list[dict]] = {}
    for r in results:
        groups.setdefault(r["node"], []).append(r)
    out = []
    for attempts in groups.values():
        ran = [a for a in attempts if a["termination"] not in NOT_COMPARABLE]
        endings: dict[str, int] = {}
        for a in attempts:
            endings[a["termination"]] = endings.get(a["termination"], 0) + 1
        graded_wf = [a for a in ran if a["workflow_correct"] is not None]
        out.append(
            {
                "node": attempts[0]["node"],
                "name": attempts[0]["name"],
                "attempts": len(attempts),
                "counted": len(ran),
                "passed": sum(1 for a in ran if a["passed"]),
                "task_correct": sum(1 for a in ran if a["task_correct"]),
                "workflow_correct": sum(1 for a in graded_wf if a["workflow_correct"]),
                "workflow_graded": len(graded_wf),
                "calls": sum(a["calls"] for a in attempts),
                "cost_usd": sum(a["cost_usd"] for a in attempts),
                "wall_s": round(sum(a["wall_s"] for a in attempts), 1),
                "endings": endings,
            }
        )
    return out
