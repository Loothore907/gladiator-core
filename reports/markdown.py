"""Markdown match report."""

from scorers import components
from scorers.confidence import statement


def _rate(count: int, total: int, single: bool, pass_word: bool = False):
    """One attempt: PASS/fail or yes/no. Several: a fraction like 4/5. Nothing counted: n/a."""
    if total == 0:
        return "n/a"
    if single:
        if pass_word:
            return "**PASS**" if count else "fail"
        return "yes" if count else "no"
    return f"{count}/{total}"


# Counts in grade details that mean something went wrong (workflow@1), with plain-English labels.
ERROR_COUNTS = {
    "wrong": "wrong decisions",
    "missing": "items left undecided",
    "duplicate_effects": "duplicate effects (the same action done twice)",
    "wrong_amounts": "wrong amounts",
    "wrong_args": "right outcome with wrong details (team, merge target...)",
    "conflicting": "contradictory decisions",
    "injections_obeyed": "obeyed planted instructions",
    "acted_without_reading": "acted without reading the record",
    "blind_retries_after_unknown_outcome": "retried after an unknown outcome without checking",
}
# Labels that change for a count of 1 ("1 wrong decision", not "1 wrong decisions").
SINGULAR = {
    "wrong decisions": "wrong decision",
    "items left undecided": "item left undecided",
    "duplicate effects (the same action done twice)": "duplicate effect (the same action done twice)",
    "wrong amounts": "wrong amount",
    "contradictory decisions": "contradictory decision",
}
# Boolean grade fields that aren't failure reasons when False.
NOT_REASONS = {"passed", "evaluated", "task_evaluated", "first_report_correct"}


def why_failed(details: dict, termination: str | None = None) -> str:
    """Short plain-English reasons an attempt failed, from its grade details."""
    reasons = [
        f"{details[k]} {SINGULAR.get(label, label) if details[k] == 1 else label}"
        for k, label in ERROR_COUNTS.items()
        if (details.get(k) or 0) > 0
    ]
    if details.get("confusions"):  # several-outcome challenges: which outcome was chosen instead of which
        mix = ", ".join(
            f"{pair.split('->')[1]} instead of {pair.split('->')[0]} x{n}" for pair, n in details["confusions"].items()
        )
        reasons = [f"{r} ({mix})" if r.endswith(("wrong decisions", "wrong decision")) else r for r in reasons]
    if reasons:
        if details.get("explicit_finish") is False:
            reasons.append("did not call finish")
        return "; ".join(reasons)
    failed_checks = [k.replace("_", " ") for k, v in details.items() if v is False and k not in NOT_REASONS]
    ending = f"ended by {termination}" if termination and termination != "session_ended" else ""
    if failed_checks:
        return "failed checks: " + ", ".join(failed_checks) + (f" ({ending})" if ending else "")
    return ending or "final answer was wrong"


def _efficiency_rows(eff: dict | None) -> list[str]:
    """Efficiency score (the core score: lower is better, 1.00 = even) and the raw numbers behind it."""
    if not eff or eff.get("A") is None:
        return []
    ra, rb = eff["raw"]["A"], eff["raw"]["B"]
    pts = lambda r: r["mistakes"] - eff["pairs"]  # noqa: E731  (stored as 1 + points per attempt)
    rows = [
        f"| **Efficiency score** ({eff['profile']}; lower is better, 1.00 = even) "
        f"| **{eff['A']:.2f}** | **{eff['B']:.2f}** |",
        f"| More efficient in (paired attempts) | {eff['a_wins']}/{eff['pairs']} | {eff['b_wins']}/{eff['pairs']} |",
        f"| Mistake points (severity-weighted) | {pts(ra):g} | {pts(rb):g} |",
        f"| Tokens | {int(ra['tokens']):,} | {int(rb['tokens']):,} |",
    ]
    if eff.get("a_unfinished") or eff.get("b_unfinished"):
        rows.append(
            f"| Not finished (out of budget/time, or items left undecided) "
            f"| {eff['a_unfinished']} | {eff['b_unfinished']} |"
        )
    return rows


def _outcome_rows(out: dict | None) -> list[str]:
    """Pair wins under the victory rule: success first, then efficiency (both succeeded) or progress (neither)."""
    if not out or not out.get("pairs"):
        return []
    n, ba, bb = out["pairs"], out["by"]["A"], out["by"]["B"]
    how = lambda by: ", ".join(f"{by[k]} by {LABEL[k]}" for k in LABEL if by[k]) or "none"  # noqa: E731
    return [
        f"| **Paired attempts won** (ties: {out['ties']}) | **{out['a_wins']}/{n}** | **{out['b_wins']}/{n}** |",
        f"| How they were won | {how(ba)} | {how(bb)} |",
    ]


LABEL = {"success": "success", "progress": "getting further", "efficiency": "efficiency"}


def _reliability_rows(rel: dict | None, ta: dict, tb: dict) -> list[str]:
    """Reliability: tests passed on every repeat (pass^k) and the cost of each clean result. Shown with repeats."""
    if not rel or rel["A"]["repeats"] < 2:
        return []
    ra, rb = rel["A"], rel["B"]

    def per_clean(t, r):
        return f"{t['cost_usd'] / r['clean_every_time']:.6f}" if r["clean_every_time"] else "none clean"

    return [
        f"| Clean every time (all {ra['repeats']} repeats passed) | {ra['clean_every_time']}/{ra['tests']} tests "
        f"| {rb['clean_every_time']}/{rb['tests']} tests |",
        f"| Cost per clean test (USD) | {per_clean(ta, ra)} | {per_clean(tb, rb)} |",
    ]


def _rate_range(t: dict) -> str:
    """Pass rate with its 95% range, e.g. '50% (15-85%)': small samples get wide ranges."""
    from scorers.confidence import wilson

    if not t["cases"]:
        return "n/a"
    lo, hi = wilson(t["passed"], t["cases"])
    return f"{t['passed'] / t['cases']:.0%} ({lo:.0%}-{hi:.0%})"


def _per_pass(t: dict) -> str:
    """What one successful attempt cost on average: total cost / passes (the price of reliability)."""
    return f"{t['cost_usd'] / t['passed']:.6f}" if t["passed"] else "no passes"


def shared_mistakes(results_a: list[dict], results_b: list[dict]) -> list[tuple]:
    """Items both contestants got wrong the same way on the same attempt (same world): (case, attempt, item, pair)."""
    out = []
    for ra, rb in zip(results_a, results_b, strict=False):
        ma = (ra.get("grade_details") or {}).get("mistakes") or {}
        mb = (rb.get("grade_details") or {}).get("mistakes") or {}
        for item in sorted(set(ma) & set(mb)):
            if ma[item] == mb[item]:
                out.append((ra["name"], ra.get("attempt", 1), item, ma[item]))
    return out


def _endings(endings: dict) -> str:
    return ", ".join(k if v == 1 else f"{k} ×{v}" for k, v in endings.items())


def render(run_id: str, record: dict, verdict: dict) -> str:
    A, B = record["contestants"]["A"], record["contestants"]["B"]
    ta, tb = verdict["totals"]["A"], verdict["totals"]["B"]
    repeats = record["settings"].get("repeats", 1)
    single = repeats == 1
    unit = "Cases" if single else "Attempts"
    if verdict["winner"] in ("draw", "no contest"):
        headline = f"**Result: {verdict['winner']}**"
    else:
        w = record["contestants"][verdict["winner"]]["id"]
        headline = f"**Winner: {w}** (decided by `{verdict['decided_by']}`)"

    lines = [
        f"# Gladiator match: {record['suite']['name']}",
        "",
        f"Run `{run_id}` · A = `{A['id']}` · B = `{B['id']}`"
        + ("" if single else f" · each case played {repeats} times, fresh session each time"),
        "",
        headline,
        "",
        *(
            [f"**How sure:** {statement((A['id'], B['id']), verdict['confidence'])}", ""]
            if verdict.get("confidence")
            else []
        ),
        *(
            []
            if record["settings"].get("equal", True)
            else [
                "> **Settings differ between contestants** (A: "
                + f"`{record['settings']['per_contestant']['A']}`, B: `{record['settings']['per_contestant']['B']}`). "
                + "This is a calibration / recommended-settings comparison, not an equal-settings head-to-head.",
                "",
            ]
        ),
        "| | A: " + A["id"] + " | B: " + B["id"] + " |",
        "|---|---:|---:|",
        *_outcome_rows(verdict.get("outcome")),
        *_efficiency_rows(verdict.get("efficiency")),
        f"| {unit} passed | {ta['passed']}/{ta['cases']} | {tb['passed']}/{tb['cases']} |",
        f"| Passed first time (no rejected step) | {ta.get('first_pass', 0)}/{ta['cases']} "
        f"| {tb.get('first_pass', 0)}/{tb['cases']} |",
        f"| Task correct | {ta['task_correct']}/{ta['cases']} | {tb['task_correct']}/{tb['cases']} |",
        f"| Workflow correct (where graded) | {ta['workflow_correct']}/{ta['workflow_graded']} | "
        f"{tb['workflow_correct']}/{tb['workflow_graded']} |",
        f"| Model calls | {ta['calls']} | {tb['calls']} |",
        f"| Pass rate (95% range) | {_rate_range(ta)} | {_rate_range(tb)} |",
        f"| Cost (USD) | {ta['cost_usd']:.6f} | {tb['cost_usd']:.6f} |",
        f"| Cost per pass (USD) | {_per_pass(ta)} | {_per_pass(tb)} |",
        *_reliability_rows(verdict.get("reliability"), ta, tb),
        f"| Time in cases (s) | {ta['wall_s']} | {tb['wall_s']} |",
        "",
        "## Per case",
        "",
        "| Case | Model | Passed | Task | Workflow | Calls | Cost (USD) | Time (s) | Ended by |",
        "|---|---|---|---|---|---:|---:|---:|---|",
    ]
    for ga, gb in zip(components.by_node(A["results"]), components.by_node(B["results"]), strict=True):
        for side, g in (("A", ga), ("B", gb)):
            model = record["contestants"][side]["id"]
            n = g["counted"]
            wf = _rate(g["workflow_correct"], g["workflow_graded"], single)
            lines.append(
                f"| {g['name']} | {model} | {_rate(g['passed'], n, single, pass_word=True)} | "
                f"{_rate(g['task_correct'], n, single)} | {wf} | {g['calls']} | "
                f"{g['cost_usd']:.6f} | {g['wall_s']} | {_endings(g['endings'])} |"
            )
    b = record["budget"]
    lines += [
        "",
        f"Total spend ${b['spent_usd']:.6f} of ${b['cap_usd']:.2f} cap "
        f"(A ${b['spent_by']['A']:.6f}, B ${b['spent_by']['B']:.6f}; each side may spend up to "
        f"${b['per_contestant_cap_usd']:.2f})"
        + (". Cap reached: some calls were not made." if b["cap_reached"] else "")
        + f" · wall {record['wall_s']}s",
        "",
        "**How to read this.** Each attempt is played on the same world by both models and judged as a pair: "
        "a model that succeeds beats one that doesn't; if both succeed, the more efficient wins; if neither does, "
        "the one that got further wins (ties go to efficiency); efficiency within 1% is a tie. Most pairs won "
        "takes the match. With repeats, *clean every time* counts the tests a model passed on every repeat "
        "(reliability, like tau-bench's pass^k). The *efficiency score* measures who reaches the finish line with "
        "the fewest mistakes, the least money, tokens and time. Per paired attempt each of those is divided by the "
        "pair's mean (1.00 = even), weighted by the suite's profile and summed; running out of the attempt "
        "budget or time adds 1.00, and leaving items undecided adds that share of 1.00. Lower is better. "
        "*Passed* is each case's original v1 verdict. "
        "*Task* = the end result was right. "
        "*Workflow* = the model followed a valid process (e.g. re-scanned after an interruption); n/a means "
        "the case only grades the final answer. Costs come from the providers' reported token usage times "
        "`providers/prices.yaml`. Time includes network overhead. The summary table counts only attempts "
        "both models finished; attempts cut off by the $ cap or by a provider failure are left out "
        "(listed in the notes)."
        + ("" if single else " Per-case fractions are passes out of the attempts that counted."),
    ]
    lines += ["", "## What was run", ""]
    for node_id, prov in (record["suite"].get("challenges") or {}).items():
        if prov.get("inline"):
            lines.append(
                f"- `{node_id}`: inline test (not a package), status **unreviewed**, test type `{prov['test_type']}`"
            )
        else:
            state = "MODIFIED" if prov.get("modified") else "unmodified"
            lines.append(
                f"- `{prov['id']}@{prov['version']}`: status **{prov['status']}**, {state}, "
                f"{prov['instances']} instances, "
                f"tags {', '.join(prov['tags'])}, fingerprint `{prov['content_sha256'][:12]}`"
            )
    for note in verdict["notes"]:
        lines += ["", f"Note: {note}"]
    failed = [
        (rec["id"], r)
        for rec in (A, B)
        for r in rec["results"]
        if not r["passed"] and r["termination"] != "not_run" and r.get("grade_details")
    ]
    if failed:
        lines += ["", "## Why attempts failed", ""]
        lines += [
            f"- {cid} / {r['name']}"
            + ("" if single else f" #{r.get('attempt', 1)}")
            + f": {why_failed(r['grade_details'], r['termination'])}"
            for cid, r in failed
        ]
    shared = shared_mistakes(A["results"], B["results"])
    if shared:
        lines += [
            "",
            "## Shared mistakes (check the rules)",
            "",
            "Both models made the same wrong call on the same item in the same world. That usually means the rule "
            "text is ambiguous or the answer key is off, not that both models are weak. Review these before "
            "trusting the scores:",
            "",
        ]
        lines += [
            f"- {name}" + ("" if single else f" #{att}") + f": {item} ({pair.replace('->', ' expected, chose ')})"
            for name, att, item, pair in shared
        ]
    errors = [(rec["id"], r) for rec in (A, B) for r in rec["results"] if r.get("error")]
    if errors:
        lines += ["", "## Problems", ""]
        lines += [
            f"- {cid} / {r['name']}"
            + ("" if single else f" #{r.get('attempt', 1)}")
            + f": {r['termination']}: {r['error']}"
            for cid, r in errors
        ]
    return "\n".join(lines) + "\n"
