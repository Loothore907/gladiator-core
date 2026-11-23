#!/usr/bin/env python3
"""Gladiator v2: head-to-head model evaluation.

  python gladiator.py match --suite suites/six_case_mvp.yaml \\
      --a openai:gpt-6-luna --b anthropic:claude-sonnet-5-5 --max-usd 1

  python gladiator.py verify runs/<run_id>     # check a run record's hashes
  python gladiator.py list                     # suites, modules, priced models

Keys are read from .env (see .env.example). No prompts, no approval steps:
the only guard is the hard --max-usd cap.
"""

import argparse
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from engine import record, runner
from engine import suite as suites
from reports import markdown
from scorers import components

# The environment variables that hold each provider's key (providers/*.py).
PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
}

# The models a live race may use unless told otherwise: small and cheap, each with a listed or published price.
DEFAULT_RACE_MODELS = [
    "anthropic:claude-haiku-5-5",
    "openai:gpt-6-luna",
    "google:gemini-3.1-flash-lite",
    "google:gemini-3.5-flash-lite",
    "openrouter:deepseek/deepseek-v4.1-flash-1017",
    "openrouter:minimax/minimax-m3.1-flash",
    "openrouter:qwen/qwen3.8-27b",
    "openrouter:z-ai/glm-5.3",
]


def event_writer(target: str | None):
    """Where a match's live events go: nowhere, standard output ("-") or a file, one JSON object per line."""
    if not target:
        return None, lambda: None
    out = sys.stdout if target == "-" else open(target, "w", encoding="utf-8")  # noqa: SIM115  closed by the caller
    lock = threading.Lock()

    def write(event: dict):
        with lock:  # two contestants report from two threads
            out.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False, default=str) + "\n")
            out.flush()

    return write, (lambda: None) if target == "-" else out.close


def cmd_match(args):
    s = suites.load(args.suite)
    overrides = {}
    if args.effort is not None:
        overrides["effort"] = None if args.effort == "none" else args.effort
    if args.max_output_tokens:
        overrides["max_output_tokens"] = args.max_output_tokens
    if args.max_usd <= 0:
        raise SystemExit("--max-usd must be greater than 0")

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    print(
        f"Gladiator {run_id}: {s['suite']} ({len(s['order'])} cases)  A={args.a}  B={args.b}  cap=${args.max_usd:.2f}"
    )
    try:
        sides = {
            side: {"effort": None if value == "none" else value}
            for side, value in (("A", args.a_effort), ("B", args.b_effort))
            if value is not None
        }
        listener, close = event_writer(args.events)
        try:
            body, operator = runner.run_match(
                s, args.a, args.b, args.max_usd, overrides, args.repeats, sides, on_event=listener
            )
        finally:
            close()
    except ValueError as e:  # bad contestant string or missing price: nothing was spent
        raise SystemExit(f"Not started: {e}") from None
    body["run_id"] = run_id
    v = components.verdict(body)
    body["verdict"] = v
    report = markdown.render(run_id, body, v)
    folder = ROOT / "runs" / run_id
    record.write(folder, body, operator, report)
    print()
    print(report)
    print(f"Saved: {folder}/report.md, record.json, operator.json")
    crashed = [
        r for c in body["contestants"].values() for r in c["results"] if (r.get("error") or "").startswith("unexpected")
    ]
    if crashed:
        print(f"ERROR: {len(crashed)} case(s) hit an unexpected error (a bug, not a model result). See report.")
        sys.exit(2)


def cmd_serve(args):
    import providers
    from engine import serve

    # GLADIATOR_RACE_CODES: invites, comma-separated; "invite:40" gives that invite its own number of races.
    codes, limits = set(), {}
    for entry in os.environ.get("GLADIATOR_RACE_CODES", "").split(","):
        code, _, limit = entry.strip().partition(":")
        if code:
            codes.add(code)
            if limit:
                limits[serve.who_of(code)] = int(limit)
    codes = codes or None
    budgets = {}
    for entry in args.provider_usd or []:
        name, _, amount = entry.partition("=")
        if name not in providers.PROVIDERS or not amount:
            sys.exit(f"--provider-usd takes provider=amount, e.g. openrouter=30 (got {entry!r})")
        budgets[name] = float(amount)
    # with budgets per provider and no total asked for, the budgets are the limits
    total = args.total_usd if args.total_usd is not None else (None if budgets else 5.0)
    wanted = args.model or DEFAULT_RACE_MODELS
    # A model is offered only if this server holds its provider's key and, with budgets, the provider has one.
    models, left_out = [], []
    for model in wanted:
        name = providers.parse(model)[0]
        has_key = name == "scripted" or any(os.environ.get(k) for k in PROVIDER_KEYS.get(name, ()))
        has_budget = not budgets or total is not None or name in budgets
        (models if has_key and has_budget else left_out).append(model)
    if len(models) < 2:
        sys.exit("Fewer than two models have both a key and a budget on this machine: nothing to race.")
    state = Path(args.state)
    ledger = serve.Ledger(state / "race_ledger.json", total, args.runs_per_code, budgets, limits)
    races = serve.Races(
        args.suite, models, ledger, args.max_usd, codes, set(args.origin or []), runs_dir=state / "runs"
    )
    server = serve.serve(races, args.host, args.port)
    who = f"{len(codes)} invite(s)" if codes else "no invites: this machine only"
    limit = f"${total:.2f} in total" if total is not None else "the provider budgets"
    per = "".join(
        f"\n  budget {b['provider']}: ${b['left_usd']:.2f} left of ${b['limit_usd']:.2f}" for b in races.budgets()
    )
    out = f"\n  not offered (no key or no budget here): {', '.join(left_out)}" if left_out else ""
    print(
        f"Gladiator race runner on http://{args.host}:{server.server_address[1]}  ({who})\n"
        f"  tasks: {', '.join(races.tasks)}\n  models: {', '.join(models)}\n"
        f"  cap ${args.max_usd:.2f} per race, ${ledger.left():.2f} left of {limit}{per}{out}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


def cmd_verify(args):
    problems = record.verify(Path(args.run_dir))
    if problems:
        print("NOT OK:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("OK: record.json and operator.json match their hashes.")


def cmd_models(args):
    """Ask each provider which models this account can use, and check the priced ones exist."""
    import providers
    from engine.budget import load_prices

    prices = load_prices()
    wanted = [args.provider] if args.provider else [p for p in providers.PROVIDERS if p != "scripted"]
    missing_any = False
    for name in wanted:
        try:
            available = set(providers.create(f"{name}:-").list_models())
        except Exception as e:
            print(f"{name}: could not list models ({type(e).__name__}: {e})")
            missing_any = True
            continue
        priced = sorted((prices.get(name) or {}).keys())
        print(f"{name}: {len(available)} models available to this key")
        for model in priced:
            ok = model in available
            missing_any |= not ok
            print(f"  {'OK     ' if ok else 'MISSING'} {name}:{model}" + ("" if ok else "  (priced, but not served)"))
        similar = sorted(m for m in available if any(m.startswith(p.split("-")[0]) for p in priced) and m not in priced)
        if args.all or not priced:
            similar = sorted(available)
        if similar:
            print("  other models: " + ", ".join(similar[:60]) + (" ..." if len(similar) > 60 else ""))
    if missing_any and args.strict:
        sys.exit(1)


def cmd_challenges(_args):
    """List every challenge package and whether it is intact."""
    from engine import packages

    for pkg in packages.library().values():
        m = pkg.manifest
        state = "OK      " if pkg.intact else "MODIFIED"
        print(f"  {state} {pkg.ref:<46} {pkg.status['status']:<10} {m['instances']:<9} {', '.join(m['tags'])}")
        for problem in pkg.problems:
            print(f"           ! {problem}")


def cmd_fingerprint(args):
    """For challenge authors: compute (and optionally write) a package's content_sha256."""
    import yaml

    from engine import packages

    path = Path(args.manifest)
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    fp = packages.fingerprint(manifest)
    print(fp)
    if args.write:
        text = path.read_text(encoding="utf-8")
        old = manifest.get("content_sha256")
        if old and old in text:
            path.write_text(text.replace(old, fp), encoding="utf-8")
        else:
            path.write_text(text.rstrip("\n") + f"\ncontent_sha256: {fp}\n", encoding="utf-8")
        print(f"written to {path}")


def cmd_fork(args):
    """For challenge authors: copy a challenge to a new id (a fork) or a new version, ready to edit."""
    from engine import packages

    if not (args.to or args.version):
        raise SystemExit("say what to create: --to namespace/name (a fork) and/or --version X.Y.Z")
    pkg = packages.resolve(args.challenge)
    path = packages.fork(pkg, args.to, args.version)
    rel = path.relative_to(ROOT)
    print(f"created {rel} from {pkg.ref} (status: unreviewed)")
    print("next: edit its files, then")
    print(f"  python gladiator.py fingerprint --write {rel.as_posix()}")
    new = f"{args.to or pkg.manifest['id']}@{args.version or pkg.manifest['version']}"
    print(f"  python gladiator.py validate {new}")


def cmd_validate(args):
    """Automatic validation of a challenge (oracle must pass, bad players must fail, no leaks)."""
    from engine import packages

    pkg = packages.resolve(args.challenge)
    if pkg.manifest["test_type"] != "workflow@1":
        raise SystemExit(f"{pkg.ref}: automatic validation exists for workflow@1 challenges only (so far)")
    import yaml

    from engine.validate import validate

    spec = yaml.safe_load((packages.ROOT / pkg.manifest["params"]["spec"]).read_text(encoding="utf-8"))
    if args.write:
        write_validation_record(pkg, spec)
        return
    problems = validate(spec)
    if problems:
        print(f"NOT VALID: {pkg.ref}")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print(f"VALID: {pkg.ref} (oracle passes and every bad player fails, at every difficulty, 20 seeds each)")


def write_validation_record(pkg, spec):
    """Save validations/<namespace>/<name>@<version>.json: the sealed evidence that this exact package version
    passed (or failed) automatic validation. Free to regenerate (no model calls)."""
    import datetime

    from engine.runner import engine_commit
    from engine.validate import validation_record

    m = pkg.manifest
    provenance = {"id": m["id"], "version": m["version"], "content_sha256": m.get("content_sha256")}
    when = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = validation_record(spec, provenance, engine_commit(), when)
    path = ROOT / "validations" / f"{m['id']}@{m['version']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sha256": record.sha256(body), "body": body}, indent=1) + "\n", encoding="utf-8")
    print(f"{'VALID' if body['valid'] else 'NOT VALID'}: {pkg.ref} -> {path.relative_to(ROOT)}")
    if not body["valid"]:
        sys.exit(1)


def cmd_list(_args):
    import modules
    from engine.budget import load_prices

    print("Suites:")
    for p in sorted((ROOT / "suites").glob("*.y*ml")):
        s = suites.load(str(p))
        print(f"  {p.relative_to(ROOT)}  ({len(s['nodes'])} cases)")
    print("Test types:", ", ".join(sorted(modules.TEST_TYPES)))
    print("Priced models (providers/prices.yaml):")
    for provider, models in load_prices().items():
        for model in models or {}:
            print(f"  {provider}:{model}")


def main(argv=None):
    # Windows consoles/pipes may default to a legacy encoding; reports and model text are UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description="Gladiator v2: head-to-head model evaluation")
    sub = p.add_subparsers(dest="command", required=True)

    # Reasoning effort levels a provider may accept. Which ones a model takes is model-dependent (OpenAI lists
    # none, minimal, low, medium, high, xhigh, max); an unsupported level fails the call with the provider's error.
    efforts = ["minimal", "low", "medium", "high", "xhigh", "max", "none"]
    m = sub.add_parser("match", help="run two models through a suite")
    m.add_argument("--suite", required=True, help="suite file, e.g. suites/six_case_mvp.yaml")
    m.add_argument("--a", required=True, help="contestant A, e.g. openai:gpt-6-luna")
    m.add_argument("--b", required=True, help="contestant B, e.g. anthropic:claude-sonnet-5-5")
    m.add_argument("--max-usd", type=float, required=True, help="hard spending cap for the whole run")
    m.add_argument(
        "--effort",
        choices=efforts,
        help="override the suite's reasoning effort for both models ('none' = provider default)",
    )
    m.add_argument("--max-output-tokens", type=int, help="override the suite's output cap for both models")
    m.add_argument("--a-effort", choices=efforts, help="effort for A only (flagged)")
    m.add_argument("--b-effort", choices=efforts, help="effort for B only (flagged)")
    m.add_argument("--repeats", type=int, help="play each case this many times (fresh session each time); default 1")
    m.add_argument(
        "--events",
        metavar="FILE",
        help="write what happens as it happens, one JSON object per line, to FILE (or - for standard output): "
        "for a live view of the match. Public facts only.",
    )
    m.set_defaults(func=cmd_match)

    sv = sub.add_parser("serve", help="race runner: start one match over HTTP and stream it as it happens")
    sv.add_argument(
        "--suite",
        default="suites/small_tier.yaml",
        help="base suite: its challenges and settings are what a race can use",
    )
    sv.add_argument(
        "--model", action="append", help="a model a race may use (repeatable); default: the small, cheap set"
    )
    sv.add_argument("--max-usd", type=float, default=0.25, help="hard cap for one race in USD (half per model)")
    sv.add_argument(
        "--total-usd",
        type=float,
        help="stop starting races once this much has been spent in total (default 5, or none with --provider-usd)",
    )
    sv.add_argument(
        "--provider-usd",
        action="append",
        metavar="PROVIDER=USD",
        help="a budget for one provider's models, e.g. openrouter=30 (repeatable); a model is offered while its "
        "provider can cover one more race",
    )
    sv.add_argument("--runs-per-code", type=int, default=10, help="live races one invite may start")
    sv.add_argument(
        "--origin", action="append", help="a web origin allowed to call this server from a browser (repeatable)"
    )
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--state", default="race_state", help="folder for the spending ledger and the races' records")
    sv.set_defaults(func=cmd_serve)

    v = sub.add_parser("verify", help="check a run folder's hashes")
    v.add_argument("run_dir")
    v.set_defaults(func=cmd_verify)

    sub.add_parser("list", help="list suites, test types and priced models").set_defaults(func=cmd_list)
    sub.add_parser("challenges", help="list challenge packages and check they are intact").set_defaults(
        func=cmd_challenges
    )
    va = sub.add_parser("validate", help="automatically validate a challenge (oracle passes, bad players fail)")
    va.add_argument("challenge", help="namespace/name@version")
    va.add_argument(
        "--write", action="store_true", help="save the sealed evidence to validations/<namespace>/<name>@<version>.json"
    )
    va.set_defaults(func=cmd_validate)
    fp = sub.add_parser("fingerprint", help="compute a challenge package's content_sha256 (for authors)")
    fp.add_argument("manifest", help="path to challenge.yaml")
    fp.add_argument("--write", action="store_true", help="write the fingerprint into the manifest")
    fp.set_defaults(func=cmd_fingerprint)

    fk = sub.add_parser("fork", help="copy a challenge to a new id (fork) or new version, ready to edit")
    fk.add_argument("challenge", help="source, e.g. gladiator/refund-queue@2.1.0")
    fk.add_argument("--to", help="new id for a fork, e.g. myteam/refund-queue (forked_from is set)")
    fk.add_argument("--version", help="version of the copy, e.g. 2.2.0 (default: the source's version)")
    fk.set_defaults(func=cmd_fork)

    md = sub.add_parser("models", help="ask providers which models this key can use (free)")
    md.add_argument("--provider", choices=["anthropic", "openai", "google", "openrouter"])
    md.add_argument("--all", action="store_true", help="print every available model, not just similar ones")
    md.add_argument("--strict", action="store_true", help="exit 1 if a priced model is not served")
    md.set_defaults(func=cmd_models)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
