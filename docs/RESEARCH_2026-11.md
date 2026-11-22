# Research pass: what fails frontier agents, and are we missing anything? (2026-11-21)

A quick check of 2026 benchmark literature against Gladiator's design, as the owner asked: "are we missing
anything glaring?" Seven web searches; mostly preprints and secondary write-ups, so treat the numbers as
indicative, not verified. Sources at the end.

## Short answer
No glaring hole in the core design. The field's 2026 findings line up with what our own runs showed: top models
are now very good at short, fully specified work, and what still fails them is **reliability** (doing it right
every time), **checking** (verifying instead of assuming), **knowing when not to act**, and **long horizons**.
Cost per task is becoming a standard metric too, which supports the efficiency-first direction.

There are **two real gaps** worth fixing (wording sensitivity, reliability reporting) and **one feature** the
literature points to as the most promising frontier test (abstain/escalate).

## What the literature says still fails frontier agents
1. **Consistency across repeats (pass^k).** τ-bench's pass^k (success on *all* k tries) drops well below
   pass^1, and 2026 extensions report a "capability-consistency gap" (e.g. 81.6% at k=1 vs 56.1% at k=4 for
   one model). Our runs show the same thing: perfect on one attempt, a slip on the next.
2. **Checking, not knowing.** An error analysis of Terminal-Bench attributes a large share of failures to
   skipped or wrong verification. Agents also claim success they haven't verified. That is our blind-retry
   finding exactly, and the recovery test we just built targets it.
3. **Knowing when not to act.** A cluster of 2026 benchmarks (HiL-Bench, LHAW, Ambig-DS, AgentAbstain,
   AM-Bench) finds agents keep acting under ambiguity or missing information instead of asking or stopping.
   The better benchmarks score **both** missed escalation **and** over-asking.
4. **Irreversible actions.** GateArena shows a prompt instruction to confirm before an irreversible step is not
   reliable: models skipped the confirmation tool in a large share of episodes.
5. **Long horizons.** Short tasks are largely solved; long, multi-constraint workflows still fail (METR's
   time-horizon line keeps rising, but "Agents' Last Exam"-style hard tiers stay very low).

## How Gladiator compares
| Finding | Gladiator today | Verdict |
|---|---|---|
| Reliability across repeats | Repeats + sign test per pair; no all-k clean rate reported | **Gap (small):** report clean-run rate (pass^k) and cost per clean run |
| Checking / recovery | Ambiguous timeouts in every workflow test; `job-queue-recovery` | **Covered**, and it is our main separator |
| Wording sensitivity | Data is generated fresh each attempt, but the brief and policy wording are fixed per version | **Gap:** a noise audit found prompt-wording changes move scores 11-58x more than reruns. Equivalent rewordings should be part of the generator |
| Abstain / escalate | Not tested | **Missing feature:** the most promising frontier axis; add an "escalate" outcome for items the rules don't cover, scoring both missed escalations and needless ones |
| Irreversible actions | Non-idempotent effects (refunds, applies) are tested for duplicates | Partly covered; a "confirm before an irreversible step" variant could join the escalate work |
| Long horizons | Marathon level (90-200 items, ~17 min at max effort) | Covered for workflow length; very long (hours) tasks are out of scope for now |
| Cost per task | Core of the efficiency score; cost per pass in reports | **Aligned**, and ahead of most benchmarks. Long-Horizon-Terminal-Bench 2.0 also finds the top model costs by far the most, matching our astra finding |
| Statistics | Sign test, Wilson ranges, ties declared | Aligned. Literature agrees single runs can't rank models |
| Harness effects | One fixed harness for both sides (fair) | Fine for head-to-head; state in reports that results are for Gladiator's harness (the run record already pins the engine commit) |

## Watch-outs if we add clarification or escalation
- Clarification can raise prompt-injection risk (ASPI): an agent that asks questions can be steered by
  planted text. Our planted-instruction hazards should apply to the escalate outcome too.

## Next steps (approved 2026-11-21; all three built the same day)
1. **Reliability reporting:** clean-run rate (all repeats passed) and cost per clean run in every report.
   Offline, small.
2. **Wording variants:** each challenge version ships 2-3 equivalent rewordings of its brief and policy, picked
   per attempt like the data (same for both contestants). Offline, medium; needs new challenge versions.
3. **Escalate outcome (frontier candidate):** planted items the policy doesn't cover, where the right action is
   escalate-to-human; grade missed and needless escalations. Offline build, then a probe (December budget).

## Sources
- [Frontier models are failing one in three production attempts (VentureBeat, Stanford AI Index)](https://venturebeat.com/security/frontier-models-are-failing-one-in-three-production-attempts-and-getting-harder-to-audit)
- [Beyond the Leaderboard: tool-use, planning and reasoning failures (arXiv 2607.05775)](https://arxiv.org/pdf/2607.05775)
- [The Hard Horizon: why frontier agents stall on real workflows](https://evoailabs.medium.com/the-hard-horizon-why-frontier-ai-agents-are-stalling-on-real-world-workflows-d29984014e6d)
- [What AI agents get wrong on Linux: they fail at checking, not knowing](https://yeet.cx/topical-takes/what-ai-agents-get-wrong-linux)
- [WorkBench Revisited (arXiv 2606.13715)](https://arxiv.org/pdf/2606.13715)
- [Model or Harness? Localizing agent failures (arXiv 2607.28802)](https://arxiv.org/pdf/2607.28802)
- [Tau-Bench explained: tau2, tau3 and pass^k](https://www.cekura.ai/blogs/tau-bench)
- [τ-bench (arXiv 2406.12045)](https://arxiv.org/pdf/2406.12045)
- [LOGIGEN: capability-consistency gap (arXiv 2603.00540)](https://arxiv.org/pdf/2603.00540)
- [Beyond pass@1: reliability science for long-horizon agents (arXiv 2603.29231)](https://arxiv.org/pdf/2603.29231)
- [METR task-completion time horizons](https://metr.org/time-horizons/)
- [Long-Horizon-Terminal-Bench 2.0 (arXiv 2610.14146)](https://arxiv.org/pdf/2610.14146)
- [OSWorld 2.0 (arXiv 2606.29537)](https://arxiv.org/pdf/2606.29537)
- [What effort level should I choose? Economics of reasoning effort (arXiv 2610.34491)](https://arxiv.org/pdf/2610.34491)
- [Agentic Abstention (arXiv 2606.28733)](https://arxiv.org/html/2606.28733)
- [AM-Bench: agents under task underspecification (arXiv 2610.18090)](https://arxiv.org/abs/2610.18090)
- [Ask Early, Ask Late, Ask Right: clarification timing (arXiv 2605.07937)](https://arxiv.org/pdf/2605.07937)
- [Ambig-DS (arXiv 2605.09698)](https://arxiv.org/pdf/2605.09698)
- [ASPI: clarification amplifies prompt injection (Scale Labs)](https://labs.scale.com/papers/aspi)
- [Noise Floor Audit for Agent Benchmarks (arXiv 2608.22331)](https://arxiv.org/pdf/2608.22331)
- [Audit the noise floor before trusting a benchmark gap](https://agentpatterns.ai/verification/benchmark-noise-floor-audit/)
- [GateArena: environment-enforced confirmation for irreversible actions (NeurIPS 2026)](https://neurips.cc/virtual/2026/loc/sydney/168290)
- [False confidence and irreversible actions](https://prefactor.tech/blog/false-confidence-irreversible-actions-silent-decision-verification)
