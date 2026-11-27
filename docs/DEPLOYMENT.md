# Deployment

One host is active: the race runner behind the Gladiator Ledger live demo (`gladiator serve`, described below),
on Fly.io since 2026-11-24. It is deployed from the project's private operations repository, by commit id, with
its keys in the host's secret store and its spending ledger and race records on a volume. Nothing else is hosted.
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
uv run python gladiator.py serve                      # this machine only, your own keys from .env
```
- A race is built from safe choices only: a task from the base suite's locked challenges, two models from the
  server's list, a difficulty level and a scoring profile. Free text never reaches the engine.
- Money is guarded three times: a hard cap per race, limits the server will not go past (a race starts only if
  its whole cap still fits), and one race at a time. The limits are a total (`--total-usd`), a budget per
  provider (`--provider-usd openrouter=30`, repeatable), or both. A model is offered only while its provider
  can cover one more race and this server holds that provider's key; `/options` shows each budget and what is
  left of it.
- With no invites (`GLADIATOR_RACE_CODES`, comma-separated; `invite:40` gives one its own number of races) it
  refuses to listen beyond this machine. An invite is exchanged once for a session (`POST /signin`) that lasts
  30 days; a browser then needs only the session. Sessions are signed with a key made from the invites, so
  changing the invites signs everyone out. The ledger file records a sign-in by an id, never the invite.
- `GET /races` lists the live races it has run (record id and seal, task, the two models, who won, who passed,
  what each cost), read back from the sealed records on disk, so a restart forgets none. Public facts only.
- `GET /records/<run_id>` hands out one filed race's sealed public record, exactly as filed.
  `GET /records/<run_id>/operator` hands out its private half, the answer key of its generated worlds, so
  anyone can re-grade a finished race (owner, 2026-11-27). Two rules, checked on every request: every test in
  the record generates a fresh world for each attempt (the answers are spent the moment the race ends; the
  answer key of a fixed test never leaves the server), and the file matches the fingerprint in the sealed
  record. A record is on file only once its race is over.
- A browser page on another address must be named with `--origin`.

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
6. **Rollback = run the previous image tag.** No migrations or state make this harder.
7. **Results leave the host as files** (`record.json`, `operator.json`, `report.md`). `gladiator verify`
   checks them anywhere.
8. **Live smoke runs against the deployed image** before it's used for anything that matters.

## When we pick a host: the switch-on checklist

- [ ] Choose the target (a GitHub Actions scheduled job, a container service, or the customer's cluster).
      For the race runner this is done: a container service (Fly.io).
- [x] Image published by CI: every push to `main` that passes its checks is pushed to
      `ghcr.io/loothore907/gladiator-core`, tagged with its full commit id and `main` (the `publish` job).
      Deploy by commit id, never by `main`.
- [ ] Put the keys in the host's secret store; set provider-side spend limits.
- [ ] Decide where `runs/` lives (bucket or volume) and who can read `operator.json` (it contains the
      private answer-side data). For the race runner this is decided: a volume, and the runner itself hands
      out the answer key of a finished race on generated worlds (`/records/<run_id>/operator`, above).
- [ ] Run the live smoke test against the deployed image.
