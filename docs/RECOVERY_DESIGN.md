# Recovery family as workflow@1 generators (M3)

Status: **built 2026-11-21** as `gladiator/job-queue-recovery` 1.0.0 (validated at every level; suite
`suites/recovery.yaml`). The revocation became an account suspension (re-read through the account lookup),
and added jobs go to the front of the queue so only re-listing finds them. Follows the build order in
`docs/PRODUCT.md` (M3: harder generator-based versions of the coding and recovery families). The coding family
comes later: it needs a code sandbox.

## Why
The three v1 recovery cases (`batch-checkpoint-recovery`, `snapshot-scan-recovery`,
`ticket-revocation-recovery`) are fixed single cases that strong models pass every time. Meanwhile,
calibration keeps showing that **recovery is the one skill that separates models** at every tier: blind
retries after a timeout, duplicate effects. A generator-based recovery test with difficulty levels turns that
finding into a test, and it fits the efficiency story: recovering cheaply is the skill.

## One new challenge: `job-queue-recovery` 1.0.0 (workflow@1)
A queue of jobs to apply (`apply_job`, not idempotent) or skip, by a simple stated rule. The rule stays easy on
purpose; the difficulty is in the recovery situations:

1. **Resume after an interruption (from batch-checkpoint).** The world starts mid-run: some jobs were already
   applied before the agent arrived (receipts exist), and a checkpoint record says fewer were done than really
   were (a stale checkpoint). The agent must check receipts before applying, never redo a job, and finish the
   rest. *Engine work:* a `resume` hazard that pre-applies effects for some items and writes a stale checkpoint
   row. Graded: pre-applied items need no new effect, and a second application is a duplicate.
2. **The queue shifts mid-scan (from snapshot-scan).** While the agent pages through the queue, new jobs are
   inserted ahead of its position. This is announced ("queue refreshed"), so the pages it already read have
   shifted. A careful agent re-lists; a careless one skips jobs. *Engine work:* a `changes` variant that adds
   rows to the listed table.
3. **Revoked approval (from ticket-revocation).** An announced change flips a job's approval mid-task. This
   already exists (`hazards.changes`).
4. **Ambiguous timeouts and transient errors,** as in every workflow test (the existing separator).

Levels: easy, standard, hard, expert, marathon, with each hazard scaled by level. Validation: new bad
players for each situation (`trusts_checkpoint`: applies everything after the checkpoint; `no_rescan`: keeps
its first listing), plus the usual set.

## Cost and order
Engine work and validation are offline (no spend). Then small-model calibration in December. The three v1
recovery cases stay as warm-ups (their catalog notes already say so).
