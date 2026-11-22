# Deployment (not active yet)

No hosting is set up. This file records the rules for when it is, so the v1 cloud problems don't repeat.

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
- [ ] Add an image-publish step to CI (push to GitHub's container registry, tagged with the commit and
      `latest` on `main`).
- [ ] Put the keys in the host's secret store; set provider-side spend limits.
- [ ] Decide where `runs/` lives (bucket or volume) and who can read `operator.json` (it contains the
      private answer-side data).
- [ ] Run the live smoke test against the deployed image.
