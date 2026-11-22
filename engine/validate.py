"""Automatic validation for workflow@1 challenges (docs/PRODUCT.md section 6, M2C design section 7).

For a sample of seeds at every difficulty level:
  1. the ORACLE player (applies the declared policy through the declared tools, verifies after
     unknown outcomes) must PASS: the challenge is solvable as written;
  2. each BAD player must FAIL wherever its hazard is present: the grader actually catches mistakes;
  3. nothing private (ground truth, hazard plans) appears in what the model sees.

Players drive the module exactly as a model would: through act(tool, args) and the observations.
"""

import json

from modules.workflow import (
    Workflow,
    decide_outcome,
    evaluate,
    group_info,
    joins_of,
    resource_funcs,
    sort_key,
    via_value,
)

SEEDS = range(1, 21)


def _roles(spec):
    task, tools = spec["task"], spec["tools"]

    def find(**match):
        return next(t for t in tools if all(t.get(k) == v for k, v in match.items()))

    return {
        "list": find(op="list", table=task["decide_each"]),
        "get": {j["name"]: find(op="get", table=j["table"]) for j in joins_of(task)},
        "approve": find(op="create", table=task["effect_table"]),
        "deny": find(op="update", table=task["decide_each"]),
        "check": next((t for t in tools if t["op"] == "check"), None),
        "finish": find(op="finish"),
    }


def play_multi(module: Workflow, style: str = "oracle"):
    """Several-outcome challenges. A careful player lists the whole queue first (groups such as duplicates need
    it), looks each record up once, applies the outcome rules in order, and verifies after unknown outcomes.
    Styles: oracle | default_all | blind_retry | obeys_injections | quits_early | no_reading | naive | unordered |
    stale (keeps using records read before an announced change).
    Coupled challenges (task.resources): items are processed in the stated order, checking current stock first
    (`unordered` skips the ordering)."""
    spec, task = module.spec, module.spec["task"]
    tools = {t["name"]: t for t in spec["tools"]}
    by_outcome = {o["name"]: o for o in task["outcomes"]}
    default = next(o["name"] for o in task["outcomes"] if o.get("default"))
    listing = next(t for t in spec["tools"] if t["op"] == "list" and t["table"] == task["decide_each"])
    check = next((t for t in spec["tools"] if t["op"] == "check"), None)
    finish = next(t for t in spec["tools"] if t["op"] == "finish")
    getters = {
        j["name"]: next(t for t in spec["tools"] if t["op"] == "get" and t["table"] == j["table"])
        for j in joins_of(task)
    }
    v = spec.get("validation") or {}
    if style == "naive":
        rules = v.get("naive_outcomes")
    elif style.startswith("naive:"):  # one of several named rule variants, each missing one rule or exception
        rules = v["naive_variants"][style.split(":", 1)[1]]
    else:
        rules = task["outcomes"]
    cache = {}

    def call(tool, args=None):
        if len(module.log) >= module.max_actions or module.finished:
            raise StopIteration
        res = module.act(tool["name"], args or {}, json.dumps(args or {}))
        if res.get("notices") and style != "stale":
            cache.clear()  # an announced change: records read earlier may be out of date
        return res

    def lookup(target):
        joined = {}
        for j in joins_of(task):
            value = via_value(j["via"], target, joined)
            if (j["table"], value) not in cache:
                record = call(getters[j["name"]], {getters[j["name"]]["key"]: value})
                if record.get("notices") and style != "stale":  # changed during this very call: read it again
                    record = call(getters[j["name"]], {getters[j["name"]]["key"]: value})
                cache[(j["table"], value)] = record
            joined[j["name"]] = cache[(j["table"], value)]
        return joined

    def perform(outcome, target, ctx):
        tool = tools[by_outcome[outcome]["tool"]]
        key = target[task["target_key"]]
        args = {tool["effect_key"]: key}
        for a, expr in (by_outcome[outcome].get("args") or {}).items():
            try:
                args[a] = evaluate(expr, ctx) if ctx is not None else "unknown"
            except Exception:
                args[a] = "unknown"
        for f in tool.get("fields", []):
            args.setdefault(f, "Per policy.")
        fresh = style != "stale"
        for _ in range(8):  # keep checking and retrying, as a careful agent would
            res = call(tool, args)
            if res.get("ok"):
                return None
            if res.get("error") == "temporarily_unavailable":
                if res.get("notices") and fresh:
                    return "redo"  # nothing happened yet and the facts just changed: decide again
                continue
            if res.get("outcome") == "unknown":
                if tool["op"] != "create":
                    return None  # repeating an update is harmless; nothing to verify
                if style != "blind_retry" and check:
                    found = call(check, {check["key"]: key})
                    if found.get("found"):
                        return None
                    if (found.get("notices") or res.get("notices")) and fresh:
                        return "redo"
                continue
            return None
        return None

    try:
        first = call(listing, {"page": 1})
        last_page = 1 if style == "quits_early" else first["pages"]
        targets = list(first["items"])
        for page in range(2, last_page + 1):
            targets += call(listing, {"page": page})["items"]
        res = task.get("resources")
        stock_seen = {}  # item -> {(location, item): qty} as last read (the stale player keeps using it)
        finder = next((t for t in spec["tools"] if t["op"] == "find" and res and t["table"] == res["table"]), None)
        book = {}  # a hidden counter (no tool shows it): the agent's own tally, starting at 0
        if res and style not in ("no_reading", "unordered"):
            ctxs = {t[task["target_key"]]: {task["target_name"]: t, **lookup(t)} for t in targets}
            targets.sort(key=lambda t: (sort_key(task["order_by"], ctxs[t[task["target_key"]]]), t[task["target_key"]]))
        for target in targets:
            key = target[task["target_key"]]
            if style == "no_reading":
                perform(default, target, None)
                continue
            for _ in range(3):  # decide again if an announced change arrives before the effect lands
                ctx = {task["target_name"]: target, **lookup(target)}
                if task.get("group"):
                    ctx[task["group"]["name"]] = group_info(task, targets, target)
                if res and finder is None:
                    ctx["_funcs"] = resource_funcs(book)
                elif res:  # look at the current stock of what this item needs (it reflects earlier shipments)
                    item = evaluate(res["item_of"], ctx)
                    if style == "stale" and item in stock_seen:  # trusts its own bookkeeping, never re-reads stock
                        ctx["_funcs"] = resource_funcs(stock_seen[item])
                    else:
                        found = call(finder, {finder["by"]: item})
                        if found.get("notices") and style != "stale":
                            found = call(finder, {finder["by"]: item})
                        stock_seen[item] = {tuple(r[k] for k in res["key"]): r[res["field"]] for r in found["items"]}
                        ctx["_funcs"] = resource_funcs(stock_seen[item])
                if style == "default_all":
                    outcome = default
                elif style == "obeys_injections" and key in module.injected_targets:
                    outcome = module.injection_pushes
                else:
                    outcome = decide_outcome(rules, ctx)
                done = perform(outcome, target, ctx)
                use = by_outcome[outcome].get("consumes")
                if res and use and done != "redo" and module.decided_at.get(key) is not None:
                    # keep its own stock bookkeeping in step with what it shipped
                    sent = {a: evaluate(e, ctx) for a, e in (by_outcome[outcome].get("args") or {}).items()}
                    ctx2 = {**ctx, "args": sent}
                    at = tuple(evaluate(e, ctx2) for e in use["at"])
                    seen = book if finder is None else stock_seen.get(evaluate(res["item_of"], ctx), {})
                    seen[at] = seen.get(at, 0) - evaluate(use["amount"], ctx2)
                if done != "redo":
                    break
        call(finish)
    except StopIteration:
        pass
    return module.grade()


def play(module: Workflow, style: str = "oracle"):
    """Play one attempt. style: oracle | approve_all | blind_retry | obeys_injections | quits_early | no_reading |
    naive (applies the challenge's `validation.naive_policy`: the rule without its exceptions)."""
    if module.multi:
        return play_multi(module, "default_all" if style == "approve_all" else style)
    spec, task = module.spec, module.spec["task"]
    r = _roles(spec)
    joins = joins_of(task)
    policy = (spec.get("validation") or {}).get("naive_policy") if style == "naive" else task["policy"]
    cache = {}  # (table, key) -> record: each record is looked up once, like a careful agent would

    def lookup(target, only_first=False):
        joined = {}
        for j in joins[:1] if only_first else joins:
            value = via_value(j["via"], target, joined)
            if (j["table"], value) not in cache:
                tool = r["get"][j["name"]]
                record = call(tool, {tool["key"]: value})
                if record.get("notices") and style != "stale":  # changed during this very call: read it again
                    record = call(tool, {tool["key"]: value})
                cache[(j["table"], value)] = record
            joined[j["name"]] = cache[(j["table"], value)]
        return joined

    def call(tool, args=None):
        if len(module.log) >= module.max_actions or module.finished:
            raise StopIteration
        res = module.act(tool["name"], args or {}, json.dumps(args or {}))
        if res.get("notices") and style != "stale":
            cache.clear()  # an announced change: records read earlier may be out of date
        return res

    resume = spec["hazards"].get("resume")

    def approve(target, joined):
        args = {r["approve"]["effect_key"]: target[task["target_key"]]}
        if resume and style != "trusts_checkpoint" and r["check"]:
            # After an interrupted run, the receipts (not the queue or the checkpoint) say what was already done.
            seen = call(r["check"], {r["check"]["key"]: args[r["approve"]["effect_key"]]})
            if seen.get("found"):
                return None
            if seen.get("notices") and style != "stale":
                return "redo"  # the facts changed while it checked: decide again
        if task.get("amount_field"):
            ctx = {task["target_name"]: target, **joined}
            try:
                args[task["amount_field"]] = module.expected_amount(ctx)
            except Exception:  # a bad player that skipped a lookup still sends something
                args[task["amount_field"]] = joined[joins[0]["name"]].get(task.get("amount_source", "amount"), 0)
        fresh = style != "stale"
        for _ in range(8):  # keep checking and retrying, as a careful agent would
            res = call(r["approve"], args)
            if res.get("ok"):
                return None
            if res.get("error") == "temporarily_unavailable":
                if res.get("notices") and fresh:
                    return "redo"  # nothing happened yet and the facts just changed: decide again
                continue
            if res.get("outcome") == "unknown":
                if style != "blind_retry" and r["check"]:
                    found = call(r["check"], {r["check"]["key"]: args[r["approve"]["effect_key"]]})
                    if found.get("found"):
                        return None
                    if (found.get("notices") or res.get("notices")) and fresh:
                        return "redo"
                continue
        return None

    def deny(target):
        args = {r["deny"]["effect_key"]: target[task["target_key"]]}
        for f in r["deny"].get("fields", []):
            args.setdefault(f, "Does not meet the refund policy.")
        for _ in range(8):  # keep retrying temporary errors, as a careful agent would
            res = call(r["deny"], args)
            if res.get("ok") or res.get("outcome") == "unknown":
                return None
            if res.get("notices") and style != "stale":
                return "redo"
        return None

    done = set()  # items handled in this attempt (or skipped as already handled)
    marked = r["deny"].get("set") or {}
    skip_upto = ""
    if resume and style == "trusts_checkpoint":  # starts after the checkpoint and believes it
        reader = next(t for t in spec["tools"] if t.get("table") == resume.get("checkpoint_table", "run_checkpoint"))
        skip_upto = call(reader, {"page": 1})["items"][0]["last_done"]

    def process(target):
        key = target[task["target_key"]]
        if key in done:  # seen again because the queue shifted
            return
        done.add(key)
        if marked and all(target.get(f) == v for f, v in marked.items()):
            return  # already marked by the earlier run
        if skip_upto and key <= skip_upto:
            return
        if style == "approve_all":
            approve(target, lookup(target, only_first=True))
            return
        if style == "no_reading":
            deny(target)
            return
        for _ in range(3):  # decide again if an announced change arrives before the effect lands
            joined = lookup(target)
            ok = evaluate(policy, {task["target_name"]: target, **joined})
            if style == "obeys_injections" and key in module.injected_targets:
                ok = module.injection_pushes == "approve"  # does what the planted text says
            if (approve(target, joined) if ok else deny(target)) != "redo":
                break

    try:
        first = call(r["list"], {"page": 1})
        pages = first["pages"]
        last_page = 1 if style == "quits_early" else pages
        for page in range(1, last_page + 1):
            for target in first["items"] if page == 1 else call(r["list"], {"page": page})["items"]:
                process(target)
        # Items can be added while it works (announced): a careful agent lists the queue again at the end.
        while module.inserts and style not in ("no_rescan", "quits_early", "stale"):
            epoch, fresh, page, pages = module.epoch, [], 1, 1
            while page <= pages:
                listing = call(r["list"], {"page": page})
                pages = listing["pages"]
                fresh += [t for t in listing["items"] if t[task["target_key"]] not in done]
                page += 1
            if not fresh and module.epoch == epoch:  # nothing new, and nothing announced while re-listing
                break
            for target in fresh:
                process(target)
        call(r["finish"])
    except StopIteration:
        pass
    return module.grade()


def private_leaks(module: Workflow) -> list[str]:
    """Anything from the operator side that shows up in the model's first view."""
    public = json.dumps(module.start())
    leaks = []
    for word in ("expected", "distractor", "injected", "fault_plan", "ground_truth"):
        if word in public:
            leaks.append(word)
    return leaks


def _policy_terms(text: str) -> set:
    """The numbers (not list markers like "(2)") and double-quoted values a policy text states."""
    import re

    plain = re.sub(r"\(\d+\)", " ", text)
    return set(re.findall(r"\d+(?:\.\d+)?", plain)) | {f'"{q}"' for q in re.findall(r'"([^"]+)"', text)}


def validate(spec: dict, only_levels: list[str] | None = None, report: dict | None = None) -> list[str]:
    """Return a list of problems (empty = valid). `only_levels` limits the seed runs (e.g. to keep CI fast);
    the per-level settings check always covers every level. Pass a dict as `report` to also get the evidence per
    level: how many seeds the oracle passed and, for each bad player, whether it had to fail and how often it
    passed anyway (see validation_record)."""
    problems = []
    levels = spec.get("difficulty_levels", ["easy", "standard", "hard"])
    naive = (spec.get("validation") or {}).get("naive_policy") or (spec.get("validation") or {}).get("naive_outcomes")
    hz = spec["hazards"]

    def present(value, level):
        return (value.get(level, value.get("standard", 0)) if isinstance(value, dict) else value) or 0

    # Every per-level setting must name every level: a missing one silently falls back to `standard`.
    def walk(node, path):
        if isinstance(node, dict):
            keys = set(node)
            if keys & set(levels) and keys <= set(levels) and keys != set(levels):
                problems.append(f"{path}: no value for level(s) {sorted(set(levels) - keys)}")
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(spec, "spec")
    # Equivalent rewordings must keep every threshold and every quoted value of the original policy.
    for i, w in enumerate(spec.get("wordings") or [], start=1):
        if not (w.get("brief") and w.get("policy_text")):
            problems.append(f"wordings[{i}] needs both brief and policy_text")
            continue
        if _policy_terms(w["policy_text"]) != _policy_terms(spec["task"]["policy_text"]):
            a, b = _policy_terms(spec["task"]["policy_text"]), _policy_terms(w["policy_text"])
            problems.append(f"wordings[{i}] changes the policy's numbers or quoted values: {sorted(a ^ b)}")
    res = spec["task"].get("resources")
    if res and not any(t["op"] == "find" and t["table"] == res["table"] for t in spec["tools"]):
        start = spec["world"][res["table"]]["fields"].get(res["field"], {})
        if start != {"const": 0}:
            problems.append(f"hidden counter {res['table']}.{res['field']} must start at {{const: 0}}")
        if not all(res["field"] in (t.get("hide") or []) for t in spec["tools"] if t.get("table") == res["table"]):
            problems.append(f"hidden counter {res['table']}.{res['field']} is shown by a tool (add it to `hide`)")
    for level in [lv for lv in levels if not only_levels or lv in only_levels]:
        has_faults = any(
            present(f.get("times", 0), level) for f in hz.get("faults", []) if f["kind"] != "transient_error"
        )
        has_inj = present(hz.get("injections", 0), level)
        listing = next(t for t in spec["tools"] if t["op"] == "list" and t["table"] == spec["task"]["decide_each"])
        size = int(listing.get("page_size", 5))
        targets = present(spec["world"][spec["task"]["decide_each"]]["count"], level)
        expectations = {
            "approve_all": True,
            "no_reading": True,
            "quits_early": targets > size,
            "blind_retry": bool(has_faults),
            "obeys_injections": bool(has_inj),
            # The rule without its exceptions must fail wherever near-miss records are planted.
            "naive": bool(naive) and bool(present(hz.get("distractors", 0), level)),
            # Coupled challenges: ignoring the stated processing order must fail once stock runs short.
            "unordered": bool(spec["task"].get("resources")) and level in ("hard", "expert", "marathon"),
            # Announced mid-task changes: ignoring the notice must fail wherever a change is planned.
            "stale": any(present(c.get("at_action", 0), level) for c in hz.get("changes", [])),
            # Interrupted runs: trusting a stale checkpoint redoes work; never re-listing misses added items.
            "trusts_checkpoint": bool(present((hz.get("resume") or {}).get("handled", 0), level)),
            "no_rescan": any(
                c.get("insert") and present(c.get("at_action", 0), level) and present(c["insert"], level)
                for c in hz.get("changes", [])
            ),
        }
        for name in (spec.get("validation") or {}).get("naive_variants") or {}:
            expectations[f"naive:{name}"] = bool(present(hz.get("distractors", 0), level))
        oracle_fails, bad_passes = 0, {k: 0 for k in expectations}
        for seed in SEEDS:
            g = play(Workflow(spec, seed=seed, difficulty=level), "oracle")
            if not g.passed:
                oracle_fails += 1
                if oracle_fails == 1:
                    problems.append(f"{level}: oracle failed on seed {seed}: {g.details}")
            m = Workflow(spec, seed=seed, difficulty=level)
            if private_leaks(m):
                problems.append(f"{level}: private data visible to the model: {private_leaks(m)}")
            for style, must_fail in expectations.items():
                if must_fail and play(Workflow(spec, seed=seed, difficulty=level), style).passed:
                    bad_passes[style] += 1
        players = {}
        for style, n in bad_passes.items():
            # A bad player may occasionally get lucky (e.g. a fault never fires); allow 10% of seeds. The stale
            # player gets 15%: whether ignoring a notice matters depends on what that agent happened to read
            # before the change, so in a few worlds no change touches anything still ahead on its path.
            allowed = len(SEEDS) * (0.15 if style == "stale" else 0.1)
            if n > allowed:
                problems.append(f"{level}: bad player '{style}' passed {n}/{len(SEEDS)} seeds (grader too lenient)")
            players[style] = {"must_fail": expectations[style], "passed_seeds": n, "ok": n <= allowed}
        if report is not None:
            report[level] = {
                "seeds": len(SEEDS),
                "oracle_passed": len(SEEDS) - oracle_fails,
                "players": players,
            }
    return problems


# Validation-record schema version (docs/RUN_RECORD.md, "Validation records").
VALIDATION_RECORD_VERSION = 1


def validation_record(spec: dict, provenance: dict, engine_commit: str | None, when: str) -> dict:
    """A sealable record of one challenge version's automatic validation: the evidence behind "community" trust.
    `provenance` is the package's id, version and content_sha256. Deterministic apart from `when`/`engine_commit`:
    the same package and engine give the same results."""
    levels: dict = {}
    problems = validate(spec, report=levels)
    return {
        "kind": "validation",
        "record_version": VALIDATION_RECORD_VERSION,
        "challenge": provenance,
        "engine_commit": engine_commit,
        "validated_at": when,
        "valid": not problems,
        "problems": problems,
        "levels": levels,
    }
