# Design notes

> **Rough first draft.** Questions 1 and 2 are the positions I'd defend.
> Question 3 is notes, not a finished answer. Each section ends with the
> open questions to discuss.

Questions 1 and 2 follow directly from the Part 1 bug, where
`record_decision` was a multi-step write with nothing holding the steps
together.

## 1. Where does downstream orchestration belong?

**Position: the API service owns it, split into two pieces. It's not in the
agent loop and not in the policy.**
- **The API's transaction** guarantees that every decision produces an
  event. `POST /decisions` already commits the decision, the case close and
  the audit entry together. It also writes a `decision.recorded` event in
  that same commit.
- **A worker deployed alongside the API** carries out the steps: freeze,
  notify, file, route to a queue. It reads the same database and knows
  nothing about models, prompts or MCP.

The agent's job ends when a decision is durably recorded. Freezing the
account, notifying the customer, filing the report and routing to a queue are
all *consequences of a decision*, not part of making one. To the worker, the
agent is just one writer of decisions, alongside the analyst console and
batch jobs. The steps don't run inside the request handler either (see
below).

**What breaks if it lives in the policy (the model sequences it):**
- Nothing guarantees it happens. The model can skip a step, reorder steps, or
  stop early when it hits `MAX_TURNS`. "Freeze, then notify, then report" is a
  hard ordering requirement, and prose can only ask for it.
- Irreversible external actions (customer notification, regulatory filing)
  end up one bad generation or prompt injection away. A transaction
  description is attacker-controlled text sitting in the model's context.
- Every tool needed to freeze or file has to be exposed to the model, which
  widens what a confused agent can do.
- You can't tell "the model chose not to freeze" apart from "the model forgot
  to freeze".

**What breaks if it lives in the agent loop (deterministic code after
`record_decision`):**
- This is better than the policy, but it's the wrong process. The agent isn't
  the only writer. Analysts close cases through the ops console (`case_506`
  was blocked by `analyst:jrivera`), and batch jobs write too. If the
  orchestration lives in the agent loop, a human `block` never freezes the
  account.
- The agent process is short-lived and can crash partway through. A crash
  after the freeze but before the report leaves partial side effects and no
  record of how far it got. A retry then double-notifies the customer. This
  is the same failure Part 1 had, where the decision was committed, the audit
  entry was lost, and the retry wrote a duplicate.

**What breaks if it's a synchronous call inside the API handler:**
- The decision commit becomes hostage to the notification provider and the
  regulator's endpoint. Either a slow or failing downstream rolls back a valid
  decision, or the decision commits and the freeze silently doesn't happen.
  The handler can't be atomic across systems it doesn't own.

**What I'd build:**
- **Transactional outbox.** The same transaction that inserts the decision
  and audit row (as it does now after Part 1) also inserts a
  `decision.recorded` event. That gives you no decision without an event and
  no event without a decision.
- **A durable workflow per decision** (Temporal, Step Functions, or a plain
  worker with a state table) consumes the event, keyed by `decision_id`.
  Each step is idempotent on that key: `freeze(account, decision_id)`,
  `notify(…, decision_id)`, `file_report(…, decision_id)`. Each step's
  progress is recorded, so a crash resumes the workflow instead of
  restarting it.
- **Ordering for `block`:** freeze first, since it protects money and can be
  undone. Then notify and file in parallel. The regulatory filing gets its
  own deadline timer and alerting, because missing it is a compliance
  failure, not a bug.
- **`needs_review`:** enqueue for a human with the SLA clock starting at
  enqueue. The workflow owns the escalation timer. The agent knows nothing
  about SLAs.
- **Gate irreversible steps by actor while trust is low.** An agent `block`
  can freeze immediately, since freezing is cheap and reversible. The
  customer notice and regulatory filing wait for analyst confirmation until
  the eval and production data justify removing that gate. This is
  configured per actor in the workflow. It never goes in the prompt.
- Every step writes to `audit_log`, so the ledger records what *happened*,
  not just what was decided.

**Open questions:**
- What happens when an analyst overturns an agent `block` after the freeze?
  The workflow needs a compensation path (unfreeze, retract the notice). Who
  owns that?
- Does `needs_review` count as "decided"? Right now it closes the case, so
  the 409 rule would stop the analyst from recording the real outcome. It
  probably needs its own status, or decisions need to allow supersession by
  a human.
- Is an outbox worker enough, or does the regulatory deadline justify a real
  workflow engine on day one?

## 2. Where is the line between code and prompt?

**My rule:** if a rule can be checked deterministically from data the system
already has, and breaking it causes harm you can't undo or explain away, it
goes in code. The prompt gets judgment, meaning weighing evidence that can't
be reduced to a predicate. A secondary test: if you'd page someone when the
rule is broken, it can't depend on a model choosing to comply.

The prompt can still *mention* a code-enforced rule so the model doesn't
waste turns finding it out. But the prompt is never the enforcement.

**Applied to the two examples:**
- **"Never decide a case a human already closed"** → **code.** It's a
  predicate on the case and decision rows, and breaking it overwrites a
  regulatory record. It's now enforced: `POST /decisions` returns 409 on an
  already-decided case. Before Part 1 the only thing stopping this was policy
  prose, and the reconciliation test showed it happening.
- **"A block requires affirmative evidence"** → **mostly prompt, with a code
  backstop.** Deciding whether evidence is "affirmative" is the judgment we
  are paying the model for. But the part that *can* be checked should be:
  - The rationale must cite ids that **exist and belong to this case's
    account** (txn ids, merchant ids). That's checkable, and it stops a block
    resting on hallucinated evidence. Today the evals check citations, but
    nothing does in production.
  - "Required evidence was unavailable → `needs_review`" can also be partly
    enforced. The API (or the MCP layer) can see which reads the agent made
    for this case. A `block` or `approve` with no prior-cases read can be
    rejected or downgraded.

**Other places this repo puts things on the wrong side:**
- `decision` is a free `str` in `DecisionIn`. The allowed values are written
  only in the tool docstring and the policy. They should be a
  `Literal[...]` / enum in code.
- "Call `record_decision` exactly once" is policy prose. The idempotency and
  409 behaviour added in Part 1 now enforce it in code, so the prose is just
  a hint.
- The MCP tool retried 4xx errors. That's retry logic tuned for a model that
  will "try again", applied to errors that can't succeed on retry. Retrying
  belongs in code and should depend on the error class.
- In the other direction, the policy hardcodes "prior cases are the single
  strongest predictor". That's a modelling claim, and it should be backed by
  data rather than asserted. It belongs in the prompt (it's judgment), but it
  should be measured by evals that include accounts whose history is
  misleading.

**Open questions:**
- Where does the citation check live: in the API (which then has to
  understand rationale text) or as a structured `evidence: [ids]` field on
  `DecisionIn`? Leaning toward the structured field, with the prose
  rationale kept for humans.
- Should "required reads were made" be enforced, or only measured? Enforcing
  it couples the API to the agent's tool sequence, which an analyst console
  wouldn't follow.

## 3. What would you have to build to trust this in production? *(rough notes)*

**What grading only the final decision misses:**
- **Right answer, wrong reasons.** The agent can match `expect_decision`
  while skipping required steps (e.g. never fetching prior cases). The grade
  passes and the process is broken. It will fail on the next case where
  that step mattered.
- **Unfaithful rationales.** `must_cite` checks that an id *appears* in the
  text, not that the claim about it is true, or that the agent actually
  retrieved it.
- **Variance.** Each scenario runs once. A 70%-reliable behaviour passes
  most days.
- **Gaming via `needs_review`.** An agent that sends everything to humans
  is never wrong about a block, and it quietly floods the review queue.
  Accuracy per scenario doesn't show this; the decision mix does.
- **Coverage.** Six hand-written scenarios. There are none for prompt
  injection in transaction descriptions, tool failures partway through, or
  the degraded shard.
- **Real outcomes.** Expected values are what we *think* is right. In
  production, the truth arrives later: chargebacks on approves, reversed
  blocks.

**What I'd add (offline):**
- Trace-level graders that need no model: required tools called before
  `record_decision`, every cited id actually returned by a tool in this
  trace, no errors swallowed.
- Repeat each scenario N times and report pass *rate*, not pass/fail.
- An LLM judge for rationale faithfulness, calibrated against a small
  human-labelled set before anyone trusts its scores.
- Adversarial and failure scenarios: injected instructions in
  descriptions, a 5xx from each tool, empty history, conflicting signals.

**What I'd add (production, 10k/day, no transcript reading):**
- **Shadow mode first.** Agent decides, analysts decide independently, and
  we measure agreement per decision type before the agent's decisions take
  effect.
- **Stamp every decision** with model version, policy version, and trace id,
  so a regression can be traced to a change.
- **Decision-mix monitoring.** Block/approve/needs_review rates per flag
  reason and segment, with drift alerts. A sudden jump in `approve` on
  `mrc_giftmax` is the signal, not any single transcript.
- **Outcome feedback loop.** Join decisions to chargebacks and analyst
  reversals; that gives real false-negative and false-positive rates, and
  new eval cases come from the misses.
- **Stratified human audit.** Sample a fixed number per day, weighted
  toward blocks and toward cases where the agent's evidence was thin.
- **Keep the nightly ledger reconciliation** (the Part 1 test) running in
  production, alongside cost, latency, and context-size budgets per case.

**Open questions:**
- What agreement rate in shadow mode is good enough to go live, and is it
  the same bar for `approve` as for `block`?
- How long is the feedback lag on chargebacks, and what do we watch in the
  meantime?
