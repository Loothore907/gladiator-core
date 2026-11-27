# Deployment

One host is active: the race runner behind the Gladiator Ledger live demo (`gladiator serve`, described below),
on Fly.io since 2026-11-24. It is deployed from the project's private operations repository, by commit id, with
its keys in the host's secret store and its spending ledger and race records on a volume. Nothing else of the
engine is hosted (Gladiator Ledger keeps its own small record service, described in that repository).
This file records the rules any hosting follows, so the v1 cloud problems don't repeat.

## What gets deployed

One artifact: the **Linux container image** built from `Dockerfile`. CI already builds it and runs a match
inside it on every push, so whatever ships has been tested in the exact form it runs in.

```bash
docker build -t gladiator .
```
```bash
docker run --rm --env-file .env -v "$PWD/runs:/app/runs" gladiator match --suite suites/six_case_mvp.yaml --a openai:gpt-6-luna --b anthropic:claude-haiku-5-5 --max-usd 1
```

This same image is the **enterprise / benchmark-environment** form. It needs:

- A Linux container runtime (Docker, Podman, Kubernetes, or a batch runner).
- Three things passed in: API keys as environment variables, a folder mounted at `/app/runs` for results,
  and the command line.
- No database, no daemon and no inbound network. It runs, writes `runs/<id>/`, and exits.
- Behind a corporate proxy with its own security certificate: build on the organisation's approved base
  image with `docker build --build-arg BASE=<their python 3.12 image> .`

## The race runner (for a live view)

`gladiator serve` is the one form of this image that takes inbound requests: it starts a single match over HTTP
and streams it as it happens (`engine/serve.py`). It still needs no database: its only state is a small spending
ledger file and the records of the races it ran.

```bash
uv run gladiator serve                                # this machine only, your own key from .env
```
- **On your own machine it needs one key and no options.** Any one provider's key in `.env` gives two models to
  race (the README lists which), under a cap of $0.25 a race and $5.00 in all. It listens on this machine only
  and asks for no sign-in. A page served from this machine (`http://localhost:8000` and the like) may call it
  without being named. A request that names any other page as its sender starts nothing: a browser sends a
  plain request from any page without asking first, and nothing but the address would stand between that page
  and the keys.
- A race is built from safe choices only: a task from the base suite's locked challenges, two models from the
  server's list, a difficulty level and a scoring profile. Free text never reaches the engine.
- Money is guarded three times: a hard cap per race, limits the server will not go past (a race starts only if
  its whole cap still fits), and one race at a time. The limits are a total (`--total-usd`), a budget per
  provider (`--provider-usd openrouter=30`, repeatable), or both. A model is offered only while its provider
  can cover one more race and this server holds that provider's key; `/options` shows the cap of one race, each
  budget and what is left of it, and the total and what is left of that, so a page can say what a race may
  cost before it is started.
- A stop cannot lose money from the count. Every reservation is written to the ledger file while it is open. One
  still open at start-up belongs to a race that was running when the process stopped (a deploy, a crash, the
  host stopping the machine): nobody knows what it cost, so its whole cap is counted, as for a match that dies
  in a living server, and the start-up log says so. The race itself is lost: do not deploy while `/health` says
  `busy`, and keep the machine awake while people are racing.
- The ledger file is written beside itself and moved into place, so a stop in the middle of a write leaves the
  last whole one. A ledger that cannot be read stops the start: it is never taken for an empty one.
- A model whose price is read from OpenRouter's public list is left out, and said so, when that list cannot be
  read at start-up; the runner asks again every two minutes and offers it once it has a price (`/options`,
  `models_unpriced`). A model that can never be priced still refuses the start.
- `GET /health` names the engine build that is answering (`engine_commit`), so a deploy can check it.
- **More than one budget per attempt (`--track SUITE=CAP_USD`, repeatable).** A workflow test gives each attempt
  a hard budget, and an attempt that runs out ends as `over_budget`, which counts against the model. A big model
  raced under the small-model budget is cut off, not beaten. So a runner can hold more than one set of
  conditions: its base suite, and each `--track` (a suite with the same tests in the same versions and a larger
  budget per attempt, and the cap of one race under it: `--track suites/mid_tier.yaml=1.00`).
  - `providers/placement.yaml` places each model under the suite with the smallest budget it can finish within,
    and says how: from its own recorded attempts, or estimated from another model's token use at its own
    prices. A model placed under a suite this runner does not have is raced under the base suite, as before
    placements existed, so a runner started without `--track` behaves as it always did.
  - A pair is raced under the larger budget of its two models, so a small model meets a mid-tier one on equal
    terms. A request may name the conditions (`"track"`), never smaller ones than the pair needs. A batch is
    played under the base suite.
  - The cap of a race is its conditions' own (each model has half), and must be at least twice the budget per
    attempt. The record carries the budget it was played under: results under different budgets are different
    conditions, and a results store must not pool them.
  - `/options` lists the conditions (`tracks`: budget per attempt and cap of one race, smallest first) and
    which each model is raced under (`placed`).
- **A race its visitor pays for (`--own-keys`, off unless asked for).** A hosted runner can also take a race
  whose request carries the API key of each model's provider (`"keys": {"openrouter": "..."}`), so anyone with
  a key can race without a sign-in and without this server's money. What holds for such a race:
  - It is played with the keys in the request and with no other. A model whose provider has no key in the
    request is refused: it can never fall back on a key this machine holds. The runner needs no key of its own
    for the models it offers this way, and starts with none. Unless named (`--own-key-model`), they are every
    model placed under conditions this runner has (`providers/placement.yaml`): with the mid-tier track, 23
    models of four providers.
  - Each key is checked with its provider first, by a call that costs nothing (the provider's list of models;
    for OpenRouter its own question about a key). A key that is refused, or that may not use the model, stops
    the race there: nothing is run, spent or filed, and the visitor is told which of the two it was.
  - A key lives in memory for that one race. It is not in the spending ledger, the sealed record, its private
    half, the stream, the log or any listing. A provider that refuses a key often repeats part of it, starred
    out: what a provider says is cleaned of the key, whole or in part, before it is kept anywhere, and why a
    key failed its check is said from the kind of failure and its HTTP status alone. As a last stop, a record
    that still names a key is not filed. `tests/test_serve.py` (`VisitorKeyTests`) holds all of it.
  - None of this server's budgets is held or settled, and no sign-in is counted. The cap per race still stops
    the race, and the record says who paid (`own_keys: true`), so the operator's own spend can still be added
    up from the records.
  - One scenario at a time: batches are not offered. The one-race-at-a-time rule holds, and one address may
    start so many an hour (`--own-keys-per-hour`, 12), a refused key counted too, so the server is neither one
    visitor's alone nor a place to try keys out. Behind a host's proxy, name the header the proxy writes the
    visitor's address into (`--client-address-header`; on Fly.io `Fly-Client-IP`): a header is believed only
    if it is named.
  - Only tests that generate a fresh world for each attempt should be on offer to it: whether a visitor's key
    is enrolled in a provider's data-sharing programme cannot be known, and a fixed or holdout test must never
    run through such a key. The base suites the race runner uses hold generated tests only.
  - It needs HTTPS in front of it: the key travels in the request. `/options` says whether a runner takes such
    races (`own_keys`: the models, the cap of one race, the hourly count).
- With no invites (`GLADIATOR_RACE_CODES`, comma-separated) it refuses to listen beyond this machine. An invite
  is exchanged once for a session (`POST /signin`) that lasts 30 days; a browser then needs only the session.
  Sessions are signed with a key made from the invites, so changing the invites signs everyone out. The ledger
  file records a sign-in by an id, never the invite.
- An invite carries its own terms, after colons. `invite:40` gives it its own number of races (without one it
  gets `--runs-per-code`). `invite:usd=40` gives it a budget of its own in dollars instead: everything started
  with it, races, batches and readings alike, is set aside against that budget before it starts and counted at
  what it cost, and no number of races or readings applies. `from=2026-12-10` and `until=2026-12-17` are the
  first and last day it works on (whole days, UTC): outside them the invite and every session made from it are
  refused, and its holder is told which day it opens or ended. One invite for a group of people whose number
  is not known is then `invite:usd=40:from=2026-12-10:until=2026-12-17`. The provider budgets and the cap per
  race still apply to it. The start-up log lists each invite's terms beside five characters of a hash, never a
  character of the invite; `POST /signin` answers with `budget_usd`, `left_usd` and `until_unix` for such an
  invite, and `races_left` is then how many single races the budget still covers.
- `GET /races` lists the live races it has run (record id and seal, task, the two models, who won, who passed,
  what each cost), read back from the sealed records on disk, so a restart forgets none. Public facts only.
- `GET /records/<run_id>` hands out one filed race's sealed public record, exactly as filed.
  `GET /records/<run_id>/operator` hands out its private half, the answer key of its generated worlds, so
  anyone can re-grade a finished race (owner, 2026-11-27). Two rules, checked on every request: every test in
  the record generates a fresh world for each attempt (the answers are spent the moment the race ends; the
  answer key of a fixed test never leaves the server), and the file matches the fingerprint in the sealed
  record. A record is on file only once its race is over. The stream of a race follows the first rule too: its
  last message carries the answer key only for fresh worlds.
- A connection that sends or takes nothing for 30 seconds is closed, so silent callers cannot fill the places
  the host allows at one time. A race does not depend on its viewer's connection.
- A browser page on another address must be named with `--origin`. A hosted runner (one with invites) allows
  only the pages it was started with.

### The build page (a test for your own flow)

The runner also serves one page of its own, `GET /build`: the conversation of `gladiator chat` in a browser
(docs/PRODUCT.md, section 4a). It is the same engine code (`engine/session.py`), one small request at a time.

- **What anyone may do:** describe a flow, see the closest test, try values for its open choices and read the
  rules with those values. None of that costs money or needs a sign-in. The description is matched by its words.
- **What a sign-in adds:** creating the test on the page (`--open-create` opens that to everyone), and, when the
  runner is started with `--reader provider:model`, that model reads the description: it says which test is the
  same kind of job and which choices the words already settle. Each read is capped (`--read-usd`, 1 cent unless
  set), reserved against that provider's budget before it starts like a race, and limited to 30 per sign-in.
  A reader must have a price and this server must hold its provider's key, or it is not used.
- **Creating a test** runs the full automatic validation, a minute or two of this machine's processor. It
  shares the one-thing-at-a-time rule with races, and a sign-in may create 10. The result is handed over as a
  zip (sealed test, validation record, suite file) and kept for 20 minutes; nothing is written into the image's
  library, and no part of a visitor's description or test is logged.
- **Free text never reaches the engine as structure.** A visitor's words are matched or read; everything that
  shapes the test is one of the matched test's declared choices, checked by the engine against its allowed range.
- The page loads nothing from any other address and may only talk to the server it came from (a strict
  content-security policy). What the server answers is put on the page as text, never as markup.

### The assistant connection (the same test builder for assistants and programs)

`gladiator serve --assistants` also answers `/mcp` (MCP over HTTP, for an AI assistant) and `/api` (the same
calls as plain JSON, for a program): `engine/hosted.py`, over the calls of `engine/assistant.py`. How a person
connects: `docs/ASSISTANT.md`.

- **Keys.** Every request carries one, as `Authorization: Bearer <key>` or as the last part of the address
  (`/mcp/<key>`, for a client that can send no header). A key is an organisation's own (`GLADIATOR_API_KEYS`,
  comma-separated `name:key` pairs, kept in the host's secret store like every other key) or one of the
  runner's invites. `GET /api`, which only describes the calls, needs none.
- **A private space for each key.** Tests a key creates are kept under `<state>/spaces/<space>/`, laid out like
  a checkout. A space never joins the library. The space is worked out from the key on every request and is
  never taken from the request, so one key cannot see, name or download another's tests. A test leaves its
  space only through a signed link its owner asked for, which works for 20 minutes.
- **No model, no spend.** The caller's own model does the reading; this server calls none. Creating a test runs
  the full automatic validation, so it shares the one-thing-at-a-time rule with races and with the build page.
  A key may create 30 tests and a space holds 50.
- **Download links are made for the address given at start** (`--public-url https://race.example.org`). Without
  it they are made for the address the first request was sent to (its `Host`); a forwarding header, which is
  whatever its sender wrote, is never used.
- **MCP is the MCP library's own web app**, on an inner port of the machine that only the runner's process can
  reach. The runner checks the key, then passes the request on with the caller's space; the inner app refuses
  anyone else. No protocol is hand-made.
- **The log** prints the method, the path and the status of a request, and nothing else: never a body, a query
  or a key (a key carried in the address is printed as `/mcp/(key)`). A request is read in full before it is
  refused, so a refused body cannot be mistaken for the next request.

## Rules for any hosted environment

1. **One environment per purpose.** No parallel staging copies, preview environments or "campaign" layers
   unless there's a concrete need.
2. **Deploy only images CI built from `main` with all checks green.** Nothing is built by hand on a server.
3. **Each image is tagged with the git commit it came from**, so any result can be traced to exact code.
4. **Keys live only in the host's secret store**, never in the image, the repo or logs.
5. **Two spending caps:** the app's `--max-usd` per run, and a spending limit set in each provider's
   console (and the host's billing alert). Either one stops a runaway.
6. **Rollback = run the previous image tag**, with the start command it was deployed with (a newer command may
   name options an older image does not know, and that image would not start). No migrations or state make
   this harder.
7. **Results leave the host as files** (`record.json`, `operator.json`, `report.md`). `gladiator verify`
   checks them anywhere.
8. **Live smoke runs against the deployed image** before it's used for anything that matters.

## When we pick a host: the switch-on checklist

- [x] Choose the target (a GitHub Actions scheduled job, a container service, or the customer's cluster).
      For the race runner: a container service (Fly.io).
- [x] Image published by CI: every push to `main` that passes its checks is pushed to
      `ghcr.io/loothore907/gladiator-core`, tagged with its full commit id and `main` (the `publish` job).
      Deploy by commit id, never by `main`.
- [x] Put the keys in the host's secret store; set provider-side spend limits. For the race runner the keys
      are in Fly's secret store; the limits are set in each provider's console by the owner.
- [x] Decide where `runs/` lives (bucket or volume) and who can read `operator.json` (it contains the
      private answer-side data). For the race runner: a volume, and the runner itself hands out the answer key
      of a finished race on generated worlds (`/records/<run_id>/operator`, above).
- [ ] Run the live smoke test against the deployed image. Not automated yet: after each deploy a person signs
      in and runs one live race, and the deploy checks `/health`, `/options` and that a wrong invite is refused.
