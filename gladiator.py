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
        args.suite,
        models,
        ledger,
        args.max_usd,
        codes,
        set(args.origin or []),
        runs_dir=state / "runs",
        batch_sizes=tuple(int(n) for n in (args.batch_sizes or "").split(",") if n.strip()),
        batch_usd_per_run=args.batch_usd_per_run,
        batch_parallel=args.batch_parallel,
        batch_pass_rate=args.batch_pass_rate,
    )
    reader = args.reader
    if reader:  # the model that reads a signed-in visitor's description on the build page
        name = providers.parse(reader)[0]
        if not (name == "scripted" or any(os.environ.get(k) for k in PROVIDER_KEYS.get(name, ()))):
            print(
                f"  no key for {reader} on this machine: descriptions will be matched by their words only", flush=True
            )
            reader = None
    forks = serve.Forks(races, reader, args.read_usd, open_create=args.open_create)
    server = serve.serve(races, args.host, args.port, forks)
    who = f"{len(codes)} invite(s)" if codes else "no invites: this machine only"
    limit = f"${total:.2f} in total" if total is not None else "the provider budgets"
    per = "".join(
        f"\n  budget {b['provider']}: ${b['left_usd']:.2f} left of ${b['limit_usd']:.2f}" for b in races.budgets()
    )
    out = f"\n  not offered (no key or no budget here): {', '.join(left_out)}" if left_out else ""
    print(
        f"Gladiator race runner on http://{args.host}:{server.server_address[1]}  ({who})\n"
        f"  tasks: {', '.join(races.tasks)}\n  models: {', '.join(models)}\n"
        f"  cap ${args.max_usd:.2f} per race, ${ledger.left():.2f} left of {limit}{per}{out}\n"
        f"  build page: /build  (descriptions read by {reader or 'nobody: matched by their words'}; "
        f"creating open to {'everyone' if args.open_create else 'signed-in visitors'})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


def cmd_mcp(args):
    """Serve the test builder to an AI assistant over MCP (stdio). Standard output belongs to the protocol from
    here on, so nothing else may print to it."""
    from engine import assistant

    assistant.serve()


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


def cmd_blueprints(args):
    """Describe a flow in your own words and see the closest tests in the library, or list them all."""
    from engine import blueprint, packages

    words = " ".join(args.description)
    if words:
        found = blueprint.find(words)
    else:
        known = blueprint.library()
        found = [
            {
                "ref": pkg.ref,
                "name": pkg.manifest["name"],
                "description": " ".join(str(pkg.manifest.get("description", "")).split()),
                "status": pkg.status.get("status"),
                "choices": len(known[pkg.ref]["variables"]) if pkg.ref in known else 0,
            }
            for pkg in packages.catalog().values()
            if pkg.status.get("status") != "retired"
        ]
    if args.json:
        print(json.dumps({"description": words, "matches": found}, indent=1))
        return
    if words and not found:
        print(f'Nothing in the library matches "{words}". Run `gladiator blueprints` to see what exists.')
        return
    print(f'Closest tests to "{words}" (best first):' if words else "Tests in the library:")
    for r in found:
        open_ = f"{r['choices']} open choices" if r["choices"] else "no open choices declared yet"
        on = f"   matched on: {', '.join(r['matched'])}" if r.get("matched") else ""
        print(f"\n  {r['ref']}  ({r['name']}; {open_}){on}")
        print(f"    {r['description'][:200]}")
    print("\nNext: `gladiator choices <namespace/name@version>` shows what a test lets you change.")


def cmd_choices(args):
    """What a test lets you change when you fork it: each open choice, its default and its limits."""
    from engine import blueprint, packages

    pkg = packages.resolve(args.challenge)
    bp = blueprint.load(pkg.ref)
    if args.json:
        print(
            json.dumps({"ref": pkg.ref, "name": pkg.manifest["name"], "choices": blueprint.choices(bp) if bp else []})
        )
        return
    if not bp:
        print(f"{pkg.ref} declares no open choices yet. `gladiator fork` can still copy it for editing by hand.")
        return
    print(f"{pkg.ref}  ({pkg.manifest['name']}): what you can change\n")
    for c in blueprint.choices(bp):
        limits = f", {c['min']} to {c['max']}" if "min" in c else ""
        print(f"  {c['name']}\n    {c['ask']}\n    now: {c['default']} {c.get('unit', '')}".rstrip() + limits)
    for rule in bp.get("require") or []:
        print(f"  Always: {rule['say']}")
    print("\nEverything else (the records, tools, decisions and obstacles) stays as tested.")
    print(f"Next: gladiator fork {pkg.ref} --to yourname/your-test --set <choice>=<value> ...")


def fork_by_choices(args, pkg):
    """`gladiator fork --set`: a new sealed, validated test that differs from its source only in chosen values."""
    from engine import blueprint

    bp = blueprint.load(pkg.ref)
    if not bp:
        raise SystemExit(f"{pkg.ref} declares no open choices, so --set has nothing to set (see `gladiator choices`).")
    if not args.to:
        raise SystemExit("a fork by choices needs its own id: --to yourname/your-test")
    given = {}
    for item in args.set:
        name, eq, value = item.partition("=")
        if not eq:
            raise SystemExit(f"--set takes choice=value, got '{item}'")
        given[name.strip()] = value.strip()
    print(f"Checking {args.to} against the automatic checks (free, about a minute)...", flush=True)
    result = blueprint.fork(pkg, bp, args.to, args.version, given, name=args.name)
    if not report_fork(result, bp):
        sys.exit(1)


def report_fork(result: dict, bp: dict) -> bool:
    """Say what a fork by choices did, in plain words. True when the test was created."""
    from engine.validate import plain_problems, plain_summary

    if not result["ok"]:
        where = (
            "Those values are not allowed"
            if result["stage"] == "choices"
            else "With those values the test does not pass its automatic checks"
        )
        print(f"NOT CREATED. {where}:")
        for problem in plain_problems(result.get("levels") or {}) or result["problems"]:
            print(f"  - {problem}")
        print("Nothing was written.")
        return False
    units = {name: v.get("unit", "") for name, v in bp["variables"].items()}
    print(f"\nCREATED {result['ref']}, forked from {result['forked_from']}\n\nYour choices")
    moved = {c["name"] for c in result["changed"]}
    for name, value in result["values"].items():
        was = f"  (was {bp['variables'][name]['default']})" if name in moved else "  (unchanged)"
        print(f"  {name}: {value} {units[name]}".rstrip() + was)
    print("\nChecked")
    for line in plain_summary(result["levels"]):
        print(f"  {line}")
    w = result["written"]
    print(
        f"\nWritten\n  test      {w['test']}/  (sealed, fingerprint {result['fingerprint'][:12]}, status: unreviewed)"
    )
    print(f"  evidence  {w['evidence']}\n  suite     {w['suite']}")
    print("\nNext: race two models on it")
    print(f"  python gladiator.py match --suite {w['suite']} --a <provider:model> --b <provider:model> --max-usd 1")
    return True


def cmd_chat(args):
    """Talk your way to a test for your own flow: describe it, settle the closest test's open choices, and get a
    sealed, validated test. A model may read your description (--model); everything else is the engine."""
    import re
    import textwrap

    from engine import session

    def say(text: str, indent: str = "  "):
        """Print a paragraph wrapped for a terminal. Numbered clauses, "(1) ...", each start a new line."""
        for part in re.split(r"\s+(?=\(\d+\)\s)", " ".join(str(text).split())):
            print(textwrap.fill(part, width=100, initial_indent=indent, subsequent_indent=indent))

    def first_sentence(text: str) -> str:
        return text.split(". ", 1)[0].rstrip(".") + "."

    def ask(prompt: str) -> str:
        try:
            return input(prompt).strip()
        except EOFError:
            print("\nStopped. Nothing was written.")
            raise SystemExit(0) from None

    def yes(prompt: str) -> bool:
        return ask(f"{prompt} [Y/n] ").lower() not in ("n", "no")

    reader = None
    if args.model:
        load_dotenv(ROOT / ".env")
        reader = session.ModelReader(args.model, args.max_usd)
    talk = session.ForkSession(reader=reader)

    print("Describe the flow you want to test, in your own words.")
    found = talk.describe(ask("> "))
    if found["note"]:
        print(found["note"])
    if found["read_by"]:
        print(
            f"(Read by {found['read_by']} for ${found['read_cost_usd']:.4f}. It only reads; the engine does the rest.)"
        )
    forkable = found["forkable"]
    if not forkable:
        print("\nNo test in the library that you can adapt matches that description.")
        print("Run `gladiator blueprints` to see every test. A flow with a different shape needs a new blueprint.")
        return
    ref = found["proposed"]
    if ref is None:
        print(f"\nNone of the tests is the same kind of job. {found['why']}")
    while True:
        if ref is None:
            print("\nThe closest tests you can adapt:")
            for i, m in enumerate(forkable, start=1):
                print(f"  {i}. {m['name']} ({m['ref']})")
                say(first_sentence(m["description"]), "     ")
            picked = ask("Type a number to use one, or press Enter to stop: ")
            if not picked.isdigit() or not 1 <= int(picked) <= len(forkable):
                print("Stopped. Nothing was written.")
                return
            ref = forkable[int(picked) - 1]["ref"]
        chosen = talk.choose(ref)
        print(f"\nClosest test: {chosen['name']} ({chosen['ref']})")
        say(first_sentence(chosen["description"]))
        if found["proposed"] == ref and found["why"]:
            say(f"Why this one: {found['why']}")
        if yes("Is this the kind of job you mean?"):
            break
        ref = None

    def put(question: dict, number: str):
        unit = f" {question['unit']}" if question.get("unit") else ""
        limits = f"; allowed {question['min']} to {question['max']}" if "min" in question else ""
        came = "from your description" if question["source"] == "your description" else "the test's own value"
        if question["source"] == "you":
            came = "your answer"
        own = "" if question["value"] == question["default"] else f"; the test's own value is {question['default']}"
        print(f"\n{number}  {question['ask']}")
        print(f"   {question['value']}{unit}  ({came}{own}{limits})")
        while True:
            given = talk.answer(question["name"], ask("   > "))
            if given["ok"]:
                return
            print(f"   {given['problem'].split(': ', 1)[-1]} Try again, or press Enter to keep {question['value']}.")

    for line in chosen["unusable"]:
        print(f"(Could not use a value read from your description: {line})")
    if chosen["not_covered"]:
        print("\nThis test cannot change these parts of what you described. They stay as the test has them:")
        for item in chosen["not_covered"]:
            print(f"  - {item}")
    questions = chosen["questions"]
    print(f"\nThis test lets you change {len(questions)} things. Press Enter to keep the value shown.")
    for i, question in enumerate(questions, start=1):
        put(question, f"{i} of {len(questions)}")
    while True:
        look = talk.review()
        if not look["broken"]:
            break
        for rule in look["broken"]:
            print(f"\n{rule['say']}")
            for question in talk.questions(rule["choices"]):
                put(question, "Again:")

    print("\nYour test's rules, as a model will be told them:")
    say(look["rules"])
    if look["changed"]:
        print(
            "\nChanged from the test you started from: "
            + ", ".join(f"{c['name']} {c['from']} -> {c['to']}" for c in look["changed"])
        )
    else:
        print("\nNothing is changed from the test you started from.")

    while True:
        name = ask("\nWhat should your test be called? (for example: returns desk) ")
        owner = args.owner or ask("And your name or team, for its id (for example: acme): ")
        new_id = f"{session.slug(owner)}/{session.slug(name)}"
        if new_id.count("/") != 1 or not all(new_id.split("/")):
            print("Both need at least one letter or digit.")
        elif (ROOT / "challenges" / new_id).exists():
            print(f"{new_id} already exists. Pick another name.")
        else:
            break
    if not yes(f"Create {new_id}? It is checked first (free, about a minute)."):
        print("Stopped. Nothing was written.")
        return
    print("Checking...", flush=True)
    if not report_fork(talk.create(new_id, name=name), talk.bp):
        sys.exit(1)


def cmd_fork(args):
    """For challenge authors: copy a challenge to a new id (a fork) or a new version, ready to edit. With --set:
    fork by choices instead (no editing; the values are checked, and the result is validated and sealed)."""
    from engine import packages

    if not (args.to or args.version):
        raise SystemExit("say what to create: --to namespace/name (a fork) and/or --version X.Y.Z")
    pkg = packages.resolve(args.challenge)
    if args.set:
        return fork_by_choices(args, pkg)
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
        "--batch-sizes",
        default="",
        help="batch sizes on offer, e.g. 10,25: one test played that many times in one go (default: no batches)",
    )
    sv.add_argument(
        "--batch-usd-per-run",
        type=float,
        default=0.06,
        help="a batch's hard cap is this much times its runs (default 0.06, half per model)",
    )
    sv.add_argument("--batch-parallel", type=int, default=5, help="attempts each side of a batch plays at once")
    sv.add_argument(
        "--batch-pass-rate",
        type=float,
        default=0.8,
        help="a batch stops early once a model can no longer pass this share of its scenarios (default 0.8)",
    )
    sv.add_argument(
        "--origin", action="append", help="a web origin allowed to call this server from a browser (repeatable)"
    )
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--state", default="race_state", help="folder for the spending ledger and the races' records")
    sv.add_argument(
        "--reader",
        help="a model that reads a signed-in visitor's description on the build page, e.g. openai:gpt-6-luna "
        "(default: none, descriptions are matched by their words)",
    )
    sv.add_argument("--read-usd", type=float, default=0.01, help="hard cap for one reading of a description in USD")
    sv.add_argument(
        "--open-create",
        action="store_true",
        help="let anyone create a test on the build page (default: signed-in visitors only)",
    )
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
    fk.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="CHOICE=VALUE",
        help="fork by choices: set one of the source's open choices (see `choices`); repeat for more. "
        "The fork is validated and sealed, never edited by hand (default version 1.0.0)",
    )
    fk.add_argument("--name", help="with --set: what to call the new test (default: the source's name)")
    fk.set_defaults(func=cmd_fork)

    bl = sub.add_parser("blueprints", help="describe your flow: find the closest tests in the library")
    bl.add_argument("description", nargs="*", help="your flow in your own words (omit to list every test)")
    bl.add_argument("--json", action="store_true", help="print the matches as JSON")
    bl.set_defaults(func=cmd_blueprints)

    ct = sub.add_parser("chat", help="talk your way to a test for your own flow (describe, choose, create)")
    ct.add_argument("--model", help="a model to read your description, e.g. openai:gpt-6-luna (optional)")
    ct.add_argument("--max-usd", type=float, default=0.05, help="most the reading may cost in USD (default 0.05)")
    ct.add_argument("--owner", help="your name or team, for the new test's id (asked if not given)")
    ct.set_defaults(func=cmd_chat)

    sub.add_parser(
        "mcp", help="let an AI assistant build a test for your own flow (an MCP server on stdio; needs no key)"
    ).set_defaults(func=cmd_mcp)

    ch = sub.add_parser("choices", help="what a test lets you change when you fork it")
    ch.add_argument("challenge", help="namespace/name@version")
    ch.add_argument("--json", action="store_true", help="print the choices as JSON")
    ch.set_defaults(func=cmd_choices)

    md = sub.add_parser("models", help="ask providers which models this key can use (free)")
    md.add_argument("--provider", choices=["anthropic", "openai", "google", "openrouter"])
    md.add_argument("--all", action="store_true", help="print every available model, not just similar ones")
    md.add_argument("--strict", action="store_true", help="exit 1 if a priced model is not served")
    md.set_defaults(func=cmd_models)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
