"""Runs a match: two contestants, same suite, same settings, at the same time.

Each contestant runs in its own thread and works through the suite nodes in
order. Every node gets a fresh module instance and a fresh model conversation.

The conversation for one node goes:
    model sees: system = node instructions; first message = {"type":"start","case": <public packet>}
    model calls submit_action(action_json) -> simulator step -> observation goes back as the tool result
    ... until the simulator closes the session, the model sends a malformed action
        (v1 rule: session ends), the model stops calling the tool, the action limit,
        the time limit, or the $ cap.
"""

import hashlib
import json
import os
import random
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import modules
import providers
from engine.budget import Budget, BudgetExhausted, call_cost, next_input_tokens, price_for
from engine.suite import node as get_node
from providers.base import FatalProviderError, ProviderError, Settings

# Run-record schema version (docs/RUN_RECORD.md). 2: record_version, engine_commit, per-attempt first_pass and
# model_errors. Records without record_version are version 1; engine.record.load() upgrades them when read.
RECORD_VERSION = 2


def _git_commit(root: Path | None = None) -> str | None:
    """The git commit of the engine's own checkout, so a record says exactly which code produced it.

    Asked of the directory the engine lives in, not the working directory or the CI job's commit: when another
    repository's workflow checks the engine out and runs it, the record must still name the engine's commit.
    None when the engine is not the top of a git checkout (a container image, an installed copy).
    """
    root = (root or Path(__file__).resolve().parent.parent).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            cwd=root,
        )
        top, _, commit = out.stdout.strip().partition("\n")
        return commit.strip() or None if Path(top.strip()).resolve() == root else None
    except (OSError, subprocess.SubprocessError):
        return None


def engine_commit() -> str | None:
    """The engine's commit: its own checkout first, else the CI commit (a build with no .git directory)."""
    return _git_commit() or os.environ.get("GITHUB_SHA") or None


NODE_TIME_LIMIT_S = 600  # default per-attempt wall limit (v1's); suites can set attempt_time_limit_s
_print_lock = threading.Lock()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def say(msg: str):
    with _print_lock:
        print(msg, flush=True)


def emitter(on_event, **common):
    """Wrap a caller's event callback so the match can report what happens as it happens (for a live view).

    Events carry only what both contestants' public records will hold: never the answer side. The callback is
    called from two threads (one per contestant) and must not be able to hurt the match: whatever it raises is
    swallowed, and with no callback every emit is a no-op."""
    if on_event is None:
        return lambda kind, **fields: None

    def emit(kind: str, **fields):
        try:
            on_event({"event": kind, "t": round(time.time(), 3), **common, **fields})
        except Exception:  # noqa: BLE001  a broken listener must never stop or change a match
            pass

    return emit


def node_settings(suite: dict, node: dict, overrides: dict) -> Settings:
    merged = {**suite["defaults"], **node["settings"], **overrides}
    known = {k: merged[k] for k in ("max_output_tokens", "effort", "timeout_s") if k in merged}
    return Settings(**known, extra=merged.get("provider_extra", {}) or {})


def instance_for(salt: int, node: dict, attempt: int, suite: dict, overrides: dict) -> dict:
    """The per-attempt instance: a seed (identical for both contestants, since both use the match's salt)
    and the difficulty level configured for this node. Only generator test types use it."""
    seed = int(hashlib.sha256(f"{salt}:{node['id']}:{attempt}".encode()).hexdigest()[:12], 16)
    merged = {**suite.get("defaults", {}), **node.get("settings", {}), **overrides}
    return {"seed": seed, "difficulty": merged.get("difficulty", "standard")}


def attempt_limits(suite: dict, node: dict, overrides: dict) -> dict:
    """Hard per-attempt budgets (`attempt_budget_usd`, `attempt_budget_tokens`): a test condition, not a safety
    net. An attempt that runs out ends as `over_budget` and counts against the model (unlike the run cap)."""
    merged = {**suite.get("defaults", {}), **node.get("settings", {}), **overrides}
    return {
        k: merged[k] for k in ("attempt_budget_usd", "attempt_budget_tokens", "attempt_time_limit_s") if merged.get(k)
    }


def run_node(
    provider,
    price,
    budget: Budget,
    suite: dict,
    node: dict,
    settings: Settings,
    instance: dict | None = None,
    limits: dict | None = None,
    emit=None,
) -> tuple[dict, dict]:
    emit = emit or (lambda kind, **fields: None)
    module = modules.load(node["module"], node["params"], instance)
    uses_instance = getattr(module, "uses_instances", False)
    packet = module.start()
    system = "\n".join(node["instructions"])
    started = time.monotonic()
    turns, termination, error, observation_text = [], None, None, None
    last_usage = None  # the provider's token count for the previous call (better than counting characters)
    limits = limits or {}
    spent_usd = spent_tokens = 0.0  # this attempt only (for the hard per-attempt budget)
    try:
        chat = provider.open_chat(system, canonical({"type": "start", "case": packet}), settings, module.tools())
    except ProviderError as e:
        chat, termination, error = None, "provider_error", str(e)
    for turn in range(module.max_actions if chat else 0):
        if time.monotonic() - started > limits.get("attempt_time_limit_s", NODE_TIME_LIMIT_S):
            termination = "time_limit"
            break
        new_chars = len(observation_text or "") if turn else 0
        est_input = next_input_tokens(chat.approx_input_chars(), last_usage, new_chars)
        try:
            held = budget.hold(budget.worst_case(price, 0, settings.max_output_tokens, input_tokens=est_input))
        except BudgetExhausted as e:
            termination, error = "budget_stop", str(e)
            break
        t0 = time.monotonic()
        try:
            reply = chat.ask() if turn == 0 else chat.answer_tool(observation_text)
        except FatalProviderError:
            budget.release(held)
            raise
        except ProviderError as e:
            budget.release(held)
            termination, error = "provider_error", str(e)
            break
        call_s = time.monotonic() - t0
        u = last_usage = reply.usage
        cost, list_cost = call_cost(price, u)
        budget.settle(held, cost)
        spent_usd += cost
        spent_tokens += u.input_tokens + u.output_tokens
        record = {
            "turn": turn,
            "tool_name": reply.tool_name,
            "tool_args": reply.tool_args,
            "action_json": reply.action_json,
            "model_text": reply.text,
            "stop_reason": reply.stop_reason,
            "extra_tool_calls": reply.extra_tool_calls,
            "usage": asdict(u),
            "cost_usd": cost,  # what the provider billed, when it says; otherwise the list-price estimate
            "list_cost_usd": list_cost,  # tokens times the listed price, kept so any gap can be seen
            "call_s": round(call_s, 3),
            "response_id": reply.response_id,
        }
        turns.append(record)

        def step(observed=None, record=record):
            failed = observed.get("error") if isinstance(observed, dict) else None
            emit(
                "step",
                turn=record["turn"],
                tool_name=record["tool_name"],
                tool_args=record["tool_args"],
                error=failed,
                call_s=record["call_s"],
                cost_usd=record["cost_usd"],
                tokens=record["usage"]["input_tokens"] + record["usage"]["output_tokens"],
                elapsed_s=round(time.monotonic() - started, 3),
            )

        if reply.tool_name is None:
            termination = "no_action"
            step()
            break
        try:
            observation = module.act(reply.tool_name, reply.tool_args, reply.action_json)
        except (ValueError, TypeError) as e:  # e.g. action over the simulator's size limit
            record["observation"] = {"error": f"rejected by simulator: {e}"}
            termination = "invalid_action"
            step(record["observation"])
            break
        record["observation"] = observation
        step(observation)
        observation_text = canonical(observation)
        if not module.last_action_valid:
            termination = "invalid_action"
            break
        if module.finished:
            termination = "session_ended"
            break
        if (limits.get("attempt_budget_usd") and spent_usd >= limits["attempt_budget_usd"]) or (
            limits.get("attempt_budget_tokens") and spent_tokens >= limits["attempt_budget_tokens"]
        ):
            termination = "over_budget"  # ran out of this attempt's hard budget before finishing
            break
    else:
        termination = termination or "action_limit"

    grade = module.grade()
    public = {
        "node": node["id"],
        "name": node["name"],
        "module": node["module"],
        "challenge": node["provenance"],
        "instance": instance if uses_instance else None,
        "module_fingerprint": module.fingerprint(),
        "passed": grade.passed,
        "task_correct": grade.task_correct,
        "workflow_correct": grade.workflow_correct,
        "termination": termination,
        "error": error,
        "calls": len(turns),
        "cost_usd": sum(t["cost_usd"] for t in turns),
        "input_tokens": sum(t["usage"]["input_tokens"] for t in turns),
        "output_tokens": sum(t["usage"]["output_tokens"] for t in turns),
        "wall_s": round(time.monotonic() - started, 3),
        "first_pass": first_pass(grade, turns),
        "model_errors": model_errors(turns),
        "grade_details": {k: v for k, v in grade.details.items() if _public_detail(v)},
        "public_packet": packet,
        "turns": turns,
    }
    operator = {"node": node["id"], "grade_details": grade.details, "native_record": module.operator_record()}
    return public, operator


# Errors the test planted (faults), not mistakes by the model: an attempt can still be first-pass after them.
FAULT_ERRORS = {"timeout", "temporarily_unavailable"}


def model_errors(turns: list[dict]) -> int:
    """Tool results that were errors caused by the model's own action (unknown record, missing fields, not enough
    stock...), as opposed to planted faults."""
    return sum(
        1
        for t in turns
        if isinstance(t.get("observation"), dict)
        and t["observation"].get("error")
        and t["observation"]["error"] not in FAULT_ERRORS
    )


def first_pass(grade, turns: list[dict]) -> bool:
    """Succeeded without a single mistaken step: passed, and no action of its own was rejected along the way.
    (Eventual success is `passed`.) Tests that grade a first report say so directly (`first_report_correct`)."""
    if "first_report_correct" in grade.details:
        return bool(grade.passed and grade.details["first_report_correct"])
    return bool(grade.passed) and model_errors(turns) == 0


def _public_detail(v) -> bool:
    """Grade details that go in the public record: scalars, and small flat maps of scalars (per-item mistakes,
    confusion counts; reports need them to flag mistakes both models share)."""
    if isinstance(v, (bool, int, float, str)) or v is None:
        return True
    return isinstance(v, dict) and len(v) <= 500 and all(isinstance(x, (bool, int, float, str)) for x in v.values())


def not_run(node: dict, reason: str, attempt: int = 1) -> dict:
    return {
        "node": node["id"],
        "attempt": attempt,
        "name": node["name"],
        "module": node["module"],
        "challenge": node["provenance"],
        "passed": False,
        "task_correct": None,
        "workflow_correct": None,
        "termination": "not_run",
        "error": reason,
        "first_pass": False,
        "model_errors": 0,
        "calls": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "wall_s": 0.0,
        "turns": [],
    }


def run_contestant(
    side: str,
    contestant: str,
    suite: dict,
    budget: Budget,
    overrides: dict,
    repeats: int = 1,
    salt: int = 0,
    on_event=None,
):
    """Play every node `repeats` times (a fresh session each time). Results are node-major:
    node1 attempt1..N, node2 attempt1..N, ... so both contestants' lists line up pair by pair."""
    provider = providers.create(contestant)
    price = price_for(*providers.parse(contestant))
    results, operator = [], []
    dead = None
    for node_id in suite["order"]:
        node = get_node(suite, node_id)
        for attempt in range(1, repeats + 1):
            if dead:
                results.append(not_run(node, dead, attempt))
                continue
            if budget.refused:
                results.append(not_run(node, "budget cap reached", attempt))
                continue
            emit = emitter(on_event, side=side, model=contestant, node=node["id"], attempt=attempt)
            emit("attempt_start", name=node["name"], challenge=node["provenance"])
            try:
                instance = instance_for(salt, node, attempt, suite, overrides)
                public, op = run_node(
                    provider,
                    price,
                    budget,
                    suite,
                    node,
                    node_settings(suite, node, overrides),
                    instance,
                    attempt_limits(suite, node, overrides),
                    emit,
                )
            except FatalProviderError as e:
                emit("attempt_end", passed=False, termination="not_run", error=str(e))
                dead = str(e)
                say(f"  [{side}] {contestant}: stopping - {dead}")
                results.append(not_run(node, dead, attempt))
                continue
            except Exception as e:  # a bug or unexpected SDK error: record it, keep the run going
                dead = f"unexpected error: {type(e).__name__}: {e}"
                emit("attempt_end", passed=False, termination="not_run", error=dead)
                say(f"  [{side}] {contestant}: {dead}\n{traceback.format_exc()}")
                results.append(not_run(node, dead, attempt))
                continue
            public["attempt"] = op["attempt"] = attempt
            emit(
                "attempt_end",
                passed=bool(public["passed"]),
                termination=public["termination"],
                calls=public["calls"],
                cost_usd=public["cost_usd"],
                wall_s=public["wall_s"],
                grade_details=public["grade_details"],  # the public half only, as the record will hold it
            )
            results.append(public)
            operator.append(op)
            verdict = "PASS" if public["passed"] else "fail"
            label = node["name"] + (f" #{attempt}" if repeats > 1 else "")
            say(
                f"  [{side}] {contestant:<32} {label:<44} {verdict:<4}  "
                f"{public['calls']:>2} calls  ${public['cost_usd']:.4f}  {public['termination']}"
            )
    return results, operator


def run_match(
    suite: dict,
    contestant_a: str,
    contestant_b: str,
    max_usd: float,
    overrides: dict | None = None,
    repeats: int | None = None,
    side_overrides: dict | None = None,
    on_event=None,
):
    """on_event: optional callback given one dict per event while the match runs (match_start, attempt_start,
    step, attempt_end, match_end), for a live view. It sees only public facts and cannot affect the match.

    side_overrides: optional per-contestant settings, e.g. {"A": {"effort": "high"}, "B": {"effort": "medium"}}.
    Default is identical settings for both (a fair head-to-head). Unequal settings are allowed for calibration
    or "each at its recommended setting" comparisons, and are always flagged in the record and report."""
    overrides = overrides or {}
    side_overrides = {"A": {}, "B": {}, **(side_overrides or {})}
    per_side = {s: {**overrides, **side_overrides[s]} for s in ("A", "B")}
    repeats = int(repeats or suite["defaults"].get("repeats", 1))
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    # Fail fast, before any spending, if a price is missing or a key is not set.
    for c in (contestant_a, contestant_b):
        price_for(*providers.parse(c))
        try:
            providers.create(c)
        except Exception as e:
            raise SystemExit(
                f"Cannot set up {c}: {e}\nCheck the API key for that provider in your .env file (see .env.example)."
            ) from None

    budgets = {"A": Budget(max_usd / 2), "B": Budget(max_usd / 2)}
    salt = random.SystemRandom().randrange(2**32)  # one per match: both contestants get identical instances
    started = time.time()
    announce = emitter(on_event)
    announce(
        "match_start",
        suite=suite["suite"],
        models={"A": contestant_a, "B": contestant_b},
        nodes=list(suite["order"]),
        repeats=repeats,
        cap_usd=max_usd,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(run_contestant, "A", contestant_a, suite, budgets["A"], per_side["A"], repeats, salt, on_event)
        fb = pool.submit(run_contestant, "B", contestant_b, suite, budgets["B"], per_side["B"], repeats, salt, on_event)
        results_a, operator_a = fa.result()
        results_b, operator_b = fb.result()
    announce("match_end", spent_usd=budgets["A"].spent + budgets["B"].spent, wall_s=round(time.time() - started, 1))

    first = get_node(suite, suite["order"][0])
    sample = {s: node_settings(suite, first, per_side[s]) for s in ("A", "B")}
    return {
        "record_version": RECORD_VERSION,
        "engine_commit": engine_commit(),
        "suite": {
            "name": suite["suite"],
            "version": suite.get("version"),
            "path": suite["path"],
            "sha256": suite["sha256"],
            "order": suite["order"],
            "success": suite["success"],
            "challenges": {n["id"]: n["provenance"] for n in suite["nodes"]},
        },
        "settings": {
            "defaults": suite["defaults"],
            "overrides": overrides,
            "per_contestant": side_overrides,
            "equal": side_overrides["A"] == side_overrides["B"],
            "repeats": repeats,
            "instance_salt": salt,
            "per_node": {n["id"]: n["settings"] for n in suite["nodes"] if n["settings"]},
        },
        "contestants": {
            "A": {
                "id": contestant_a,
                "price_per_mtok": price_for(*providers.parse(contestant_a)),
                "api_params": providers.create(contestant_a).sent_settings(sample["A"]),
                "results": results_a,
            },
            "B": {
                "id": contestant_b,
                "price_per_mtok": price_for(*providers.parse(contestant_b)),
                "api_params": providers.create(contestant_b).sent_settings(sample["B"]),
                "results": results_b,
            },
        },
        "budget": {
            "cap_usd": max_usd,
            "per_contestant_cap_usd": max_usd / 2,
            "spent_usd": budgets["A"].spent + budgets["B"].spent,
            "spent_by": {k: b.spent for k, b in budgets.items()},
            "cap_reached": budgets["A"].refused or budgets["B"].refused,
        },
        "started_unix": started,
        "wall_s": round(time.time() - started, 1),
    }, {"A": operator_a, "B": operator_b}
