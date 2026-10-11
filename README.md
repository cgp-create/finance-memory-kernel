# finance-memory-kernel

A small reference implementation of an investor's research and decision record, for agents that act on one investor's money over time. Python standard library and SQLite. Synthetic data only.

Version 0.2, October 2026. Published for others to fork and criticise. Not maintained.

## What it is

Four things get mixed up when an agent works on a portfolio for months: what a source said (evidence), what was concluded from it (claims), what is being underwritten (a selected set of cases and probabilities), and what was decided and done (decisions, approvals, fills). The kernel keeps them apart and keeps their history.

- Evidence is append-only and hashed. A correction is a new row that points at the old one.
- A claim names its evidence and cannot claim more authority than that evidence has. Its text is frozen; it changes state only by being superseded, contradicted or retracted. Claims can declare what they rest on. When a basis is superseded or retracted, every claim resting on it is marked stale in the same transaction, with a receipt, and is flagged in every packet until it is replaced or a human reaffirms it. A reaffirmed claim is re-pointed at the new basis, so it goes stale again if that changes.
- Underwriting is versioned. Anyone, including a model, may draft a revision. Only a human may review or select one, the reviewer cannot be the author, and only one revision per subject and horizon can be selected.
- Every context handed to a model is stored as a packet with its hash, the model and route that used it, and the time cuts it was built at. Given `known_as_of`, a packet is rebuilt from what the store held at that moment: later backfills and backdated rows are left out, and which revision was selected, which claims were superseded, contradicted, retracted or stale are reconstructed as they were then. Question, invalidation and trigger states are not reconstructed.
- Every decision, including the decision not to trade, is checked by the kernel against the investor's active mandate when proposed and again when approved, and moves from proposed to approved to executed only with receipts. The approver must be human and must not be the proposer, with names compared ignoring case and kind, so `direct_user:owner` cannot approve as `reviewer:owner`. A sell must cite a holding, so a "sell" cannot open an uncapped short. Fills record the expected price, so slippage is measured per trade.
- Receipts form one hash chain, and approval, execution, activation, selection and reconcile receipts carry hashes of the rows they vouch for. `verify_chain()` recomputes both.

## How much of that is enforced, and where

**In the SQLite schema (46 triggers, 2 partial unique indexes, CHECK constraints).** These hold for raw SQL sent through the connection as well as for the Python API: evidence, receipts, packets, cases and relations cannot be edited or deleted; claim bodies, authority and expiry are frozen; retracted and superseded claims cannot come back, and a stale claim returns to active only right after a human reaffirm receipt; revisions, mandates and decisions can only be inserted in their first state and only move forward; a revision cannot become reviewed or selected, a mandate active, or a decision approved, executed or reconciled without a matching receipt; selection, approval and activation receipts must name a human authority, and activation must be `direct_user`; execution and reconciliation receipts must come from a `venue` or `system`; the reviewer of a revision and the approver of a decision cannot be its proposer, comparing names case-insensitively and ignoring kind; rejecting, expiring or superseding a decision needs a receipt saying so; a decision must cite the active mandate of its own scope and, if it cites a revision, a selected one; each receipt must link to the current chain head, so the chain cannot fork, and cannot be dated before the receipt before it.

**In the Python API.** The mandate check is computed by `propose_decision` from the active mandate; a caller cannot pass one in. Review by the draft's author is refused. Freshness, dependency staleness, the authority-before-similarity ranking and the plan-is-not-a-position rule are applied in Python. Someone who writes raw INSERTs can fabricate a check that the schema will accept, as long as it names the right mandate.

**Not enforced.** Authority references are `<kind>:<name>` strings. The kernel checks the kind of actor and has no way to check identity: it cannot tell whether `direct_user:owner` was typed by the owner. Pass `Store(authorize=...)` to plug in real authentication; it is called before every receipt the Python API writes, and raw SQL does not go through it. Receipts are not signed. Anyone holding the database file can drop the triggers and rewrite rows. `verify_chain(expected_head=...)` detects a rewritten receipt, or a rewritten proposal, mandate, revision or snapshot that a receipt vouches for, provided the head hash was stored somewhere they cannot write. Evidence and claims are protected only by the triggers. Nothing here blocks an order at a venue. NAV and current exposure are inputs from the caller, recorded as such in every check.

## What it does not do

- Size trades. The notional is an input; the kernel checks it.
- Value a portfolio. NAV is an input.
- Classify intent. `intent_rung` is stored as given.
- Check sector, gross exposure or minimum cash limits. If a mandate sets them, the check lists them under `not_checked` so a pass is not read as covering them.
- Handle tax lots, wash sales, calendars or corporate actions.
- Rank by embeddings. `retrieval.VectorIndex` is an interface whose only implementation returns nothing.
- Evaluate anything. No claim is made that this improves research or returns. See SPEC.md section 15.

## The ten problems

From "10 Hard Problems in Agentic Investment Research and Trading" by Carlos Pereira (October 2026). Where a status is between two labels, it gets the lower one.

| # | Problem | What the kernel does | Status |
|---|---|---|---|
| 1 | Your portfolio is a series of statements with different clocks | Every fact carries observed and recorded times. Freshness runs from observation. Position snapshots are typed by source; a plan is never a position; a fresh broker position beats a stale reconciled one; a stale mark next to a fresh position is warned; a newer snapshot that disagrees is warned. Packets can be rebuilt as known at a past moment. | Partial. No market calendars, cash ledger, tax lots or portfolio-level weights. |
| 2 | Agents remember more than they use | Explicit `depends_on` relations. Superseding or retracting a claim marks everything resting on it stale, transitively, and every packet warns until it is refreshed or a human reaffirms it. | Partial. The kernel makes the stale claim visible in every packet; whether the model acts on the warning is outside it. |
| 3 | Agents can break and still sound fine | Every packet records model, version, provider, route and fallback, and a fallback is printed in the output. Known-answer fixtures can be stored as health-check receipts. | Partial. Nothing schedules checks or alerts. |
| 4 | Prompts may leave out most of the task | `intent_rung` is stored on every decision. An approval is a receipt; a "yes" in chat that never became one leaves the decision proposed, and the system can see that. A mandate is the rule object a phrase like "never do X" should become. | Partial. Classifying the request is the caller's job. |
| 5 | Research has to find the few variables that decide the investment | Cases carry assumptions, evidence for and against, and invalidation conditions. New evidence can only create a draft with a diff; selection needs a human. | Partial. Finding the variables is the research itself; the kernel stores the result. |
| 6 | An idea needs a size before it is a trade | Position and subject-exposure limits, per-trade and daily caps, checked against a caller-supplied NAV. | Mostly not addressed. No sizing, pro forma portfolio or tax logic. |
| 7 | Agents trade too much | `no_trade` and `hold` are first-class records with rationales and later outcomes. Mandates can require net edge after estimated cost above a threshold and cap daily notional, rechecked at approval. | Partial. Edge and cost estimates come from the caller. |
| 8 | Execution is harder than it looks | Fills record expected price, slippage in basis points and partial fills; a mandate can flag slippage above a limit; `execution_costs()` reports it by scope. | Mostly not addressed. No order types, routing or execution policy. |
| 9 | Limits have to be enforced outside the model | The record enforces separation of duties in the schema. It is not the enforcement point for orders. | Not addressed by design. Enforcement belongs at the account, wallet or venue. |
| 10 | Returns alone won't tell you if an agent is good | The chain from packet to mandate check to approval, fill and reconciliation is reconstructable and hash-linked; declined decisions are queryable; packets replay as known at the time. | Partial. No evaluation has been run. |

## Layout

```text
src/fmk/
  store.py         schema, triggers, connection
  models.py        record types, enums, hashing
  authority.py     "<kind>:<name>" authority references and separation of duties
  evidence.py      subjects and evidence
  claims.py        claims, supersession, contradiction, dependencies, questions
  underwriting.py  revisions, cases, review, selection, invalidations, triggers
  retrieval.py     candidates, filters, packets, replay
  receipts.py      receipts, hash chain, verify_chain
  policy.py        freshness policy
  decisions.py     mandates, position snapshots, decisions, execution costs
  fixtures.py      JSONL fixture replay
  data/fixtures/   ten synthetic fixtures, each with a failure state
examples/          synthetic_issuer_update.py, research_review.py
tests/             121 tests; test_hardening.py holds the bypass attempts
SPEC.md            the specification, with where each invariant is enforced
NOTES.md           design decisions
```

## Running

Python 3.10 or later.

```sh
pip install -e ".[test]"
python -m pytest
python examples/synthetic_issuer_update.py
python examples/research_review.py
```

## Fixtures

All synthetic: subjects `ACME`, `SUBJ_*`, `EQ_*`; scope `scope_demo`; sources under `synthetic://`. A test scans fixtures, tests and examples for email addresses, phone numbers, credentials, account numbers, real URLs and a list of real tickers.

| Fixture | Scenario | Failure state |
|---|---|---|
| `issuer_guidance_cut` | guidance cut, draft successor, review and selection | a model tries to select the reviewed draft |
| `stale_price_conflict` | expired price; two credible claims disagree | expired warning; contradiction left open; social chatter left out |
| `correction_supersession` | a filing corrects an inference that a valuation rested on | the valuation goes stale; a social post may not correct the filing |
| `duplicate_case_set` | two reviewed revisions, one subject and horizon | the second selection is refused by the unique index |
| `weekend_mixed_marks` | equity marked Friday, crypto marked live | mark staleness warning on the equity |
| `plan_vs_fill` | an approved plan older than a broker fill | the plan is withheld as a position; the fill wins |
| `approval_lost` | the user said yes in chat; no receipt was written | execution refused; the decision stays proposed |
| `mandate_breach` | 12 percent of NAV against a 10 percent cap | the kernel's check fails; approval without an override is refused |
| `declined_decisions` | three no-trade records with later outcomes | a no-trade without a rationale is refused |
| `model_fallback` | a packet built on a fallback model | the flag reaches the rendered output |

## License

MIT. Copyright 2026 Carlos Pereira.
