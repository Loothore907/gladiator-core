# M2c design addendum: multi-outcome and coupled decisions (`workflow@1`)

Status: **approved 2026-11-21** (all three decisions in section 6; multi-outcome first). **Built:**
multi-outcome (section 2, plus groups and minute-level times) with `incident-triage` 1.0.0, and coupled
resources (section 3) with `inventory-transfer` 1.0.0. Announced mid-task changes (section 4) are built too, in refund-queue 2.1.0, access-review 1.1.0,
incident-triage 1.1.0 and inventory-transfer 1.1.0, each with a `marathon` level.
Follows `docs/M2C_WORKFLOW_DESIGN.md` and `docs/PRODUCT.md`.

## 1. Why

The remaining two M2c challenges don't fit today's `workflow@1`. Today every item gets one of **two**
outcomes (approve or deny), each item decided **on its own**:
- `incident-triage` needs several outcomes per alert: route to an owner, merge into another alert,
  escalate, or close.
- `inventory-transfer` needs decisions that **depend on each other**: stock is shared, so filling one
  order can make another impossible.

Calibration on 2026-11-21 also showed what *doesn't* make a test hard for strong models. On refund-queue
2.0.0 and access-review 1.0.0 at expert, gpt-6.2-sol (max effort) and gpt-6.2-astra (low and medium) made
**every single decision correctly** (40/40 and 44/44 every attempt), exceptions and near misses
included. The only failures were recovery mistakes (retrying after a timeout without checking).
Independent per-item rules, even with exceptions, are within easy reach of these models. What's left
to test is:
- **Coupled decisions:** a choice for one item changes what's right for others (shared stock, capacity,
  ordering, deadlines).
- **State that changes mid-task:** a record the model already read changes, and it's told so (e.g. an
  "updates since" feed), not silently.
- **Recovery** under more varied faults (the one hazard that already separates models).

## 2. Multi-outcome items (`decide_each` with N outcomes)

Generalise the policy from one yes/no expression to an **ordered list of outcome rules**. The first rule
that matches decides; a default catches the rest.

```yaml
task:
  decide_each: alerts
  outcomes:                       # first match wins
    - {name: merge,    when: "dup_of(alert) != ''",                   effect: {tool: merge_alert,   args: {into: "dup_of(alert)"}}}
    - {name: escalate, when: "alert.severity == 'critical' or minutes_open(alert) > sla.minutes",
                                                                      effect: {tool: escalate_alert}}
    - {name: close,    when: "alert.source == 'synthetic'",           effect: {tool: close_alert}}
    - {name: route,    default: true,                                 effect: {tool: assign_alert, args: {owner: "owner_for(alert)"}}}
```

- **Grading** generalises directly: the expected outcome per item, the right effect with the right
  arguments (like `amount_rule` today), exactly once.
- **Partial credit:** a confusion matrix of expected vs chosen outcome (e.g. "escalated what should have
  been merged ×2"), which also feeds the "why attempts failed" report.
- **Validation:** the oracle applies the list; a new *first-rule-only* bad player and the existing
  naive player check that the order and exceptions matter.

As built: `task.outcomes` (first match wins; `default: true` for the fallback; `args` are expressions graded
against what the model sends, numbers numerically and text case-blind), `task.group` (items sharing fields,
with the earliest as root: `dup.root`, `dup.is_root`, `dup.size`, `dup.member`; its `where` may use only the
item's own fields), the `time_within_minutes` generator, `minutes_since()` and `{minutes_ago: N}` overrides
(the opening message then includes `now`), `injection_pushes` naming any outcome, and grading details
`wrong_args` and `confusions` (e.g. "route instead of escalate x2" in the report). Challenges written in the
original approve/deny form behave exactly as before.

## 3. Coupled decisions (shared resources)

Add **resources** that effects consume, and constraints the final state must satisfy.

```yaml
resources:
  stock: {table: inventory, key: [warehouse, sku], field: qty}     # effects decrement it
constraints:
  - "all(inventory.qty >= 0)"                                       # never oversell
objective:                                                          # how to pick between valid plans
  priority: "order.priority"                                        # fill high priority first
  tiebreak: "order.placed_at"                                       # then oldest first
```

- **Ground truth** comes from a deterministic allocator in the test type (greedy by the declared
  priority, which the brief states). There's one right plan per world, so grading stays deterministic.
  The model doesn't have to find an optimum, only follow the stated allocation rule. Getting that
  right across 30+ coupled orders is where models should slip.
- **Effects with reservations:** `reserve_stock` is not idempotent (a duplicate reservation strands
  stock), and timeouts hit it like they hit `issue_refund`. Recovery stays a hazard.
- **Grading:** final allocation equals the expected plan; no constraint broken at any point (not only at
  the end); duplicate reservations counted.

As built: `task.resources: {table, key: [location, item], field, item_of}` and `task.order_by` (expressions,
ascending; `-x` for descending). Ground truth replays the stated order with a running stock; outcomes with
`consumes: {at: [...], amount}` take stock (in the live world too: a shipment beyond what's there is refused
with `insufficient_stock`). Rules can call `stock(location, item)` and `pick_source(item, qty, preferred,
allow_fallback)`. A `find` tool op returns every row matching one field (one sku's stock everywhere).
Distractor variants can plant a shortage (`resource: {at: [...], value: n}`), table ranges can differ by
level, and `cross:` tables generate one row per combination. The validator's careful player processes in
the stated order and re-reads stock before each decision; an `unordered` bad player must fail at hard and
expert.

## 4. Mid-task changes (announced)

A `changes` hazard applies scheduled edits to the world after N actions (e.g. a new stock delivery, an
owner-roster update, an alert re-opened), and makes them **discoverable**: a `list_updates` tool, or a
notice in the next tool result ("2 records changed since your last read"). Ground truth is computed on
the final world, and the brief says changes can happen. Silent changes stay out of scope: they would
test luck, not skill.

As built: `hazards.changes` entries fire after N tool calls (per level) and edit one row (`set` values, or
`add` amounts), announced as `notices` in that call's result. Each picks its row by `where`, `lowest`, or, with
`referenced_by` (a field or `join.field` path), a record items depend on: `early` keeps to records the agent
has probably read already, `flips` picks the one whose change flips the most decisions still ahead (tried on a
copy of the world), and records that planted-instruction items depend on are left alone (`injection_where`
does the same for coupled challenges). Grading uses the facts at each decision: independent decisions by the
epoch when the effect landed; coupled ones by replaying the stated order with each change starting at the
first item still undecided when it was announced. Faults go in the first half of the work when changes exist.
The validator's careful player re-reads after a notice (and re-decides if one arrives mid-retry); a `stale`
bad player keeps old records and must fail (up to 15% of seeds tolerated, since whether a stale read matters
depends on that agent's own path). Every per-level setting must name every level (no silent fallback).

## 5. The two challenges on top

| Challenge | Outcomes / coupling | Hazards |
|---|---|---|
| `incident-triage` 1.0.0 | route / merge / escalate / close; merges chain (merge into the *root* alert) | near-duplicate alerts, an owner-roster update mid-task, SLA boundaries, planted "close this" notes |
| `inventory-transfer` 1.0.0 | allocate shared stock across warehouses by stated priority; transfers cost an action each | a delivery mid-task, reservation timeouts (no double reservations), near-tie priorities, a tight action budget |

Both get easy / standard / hard / expert from the start and go through the same validation.

## 6. Decisions for the owner (approved 2026-11-21: multi-outcome first; stated allocation rule; changes always announced)

1. **Scope:** build both features (multi-outcome **and** coupled resources), or multi-outcome first
   (smaller, gives `incident-triage`) and coupling after?
2. **Allocation rule:** OK that coupled challenges state a deterministic allocation rule (priority, then
   age) so grading has one right answer, rather than asking for an optimal plan (which would need an
   optimiser and allow several right answers)?
3. **Mid-task changes:** OK to keep them always *announced* (discoverable by a tool or notice), never
   silent?

## 7. Cost and order

Engine work and validation are offline (no spend). Calibration afterwards uses the existing budgets,
as with refund-queue and access-review. Suggested order: multi-outcome → `incident-triage` → resources
and changes → `inventory-transfer`, each its own PR.
