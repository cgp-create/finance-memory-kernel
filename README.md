# finance-memory-kernel

A provenance-preserving, revision-aware research-and-decision record for investment agents.

Standard library and SQLite only. One package (`fmk`), one schema, seventeen enforced invariants, ten synthetic fixtures, no network.

**Reference implementation, October 2026. Frozen. Not maintained.**

## What it is

Investment research breaks when new information is allowed to blur four different things: what a source said (evidence), what we concluded from it (claims), what we are underwriting (a selected set of cases and probabilities), and what we decided to do (an approved, executed action). This kernel keeps them separate across time.

It is an append-oriented store with an explicit selection boundary. Evidence is immutable. Claims point at evidence and are superseded, contradicted or retracted, never edited. Underwriting is versioned, and exactly one revision per subject and horizon can be selected, by a receipt that names who selected it. Every context handed to a model is a replayable packet with a hash and the runtime that produced it. Every decision, including the decision to do nothing, is a record checked against a versioned mandate and moved forward only by receipts.

## The six questions it answers

1. **What did we know, and when?** Evidence with observed time, authority tier and content hash, cut at any `as_of`.
2. **What do we currently believe about this aspect, and what did we believe before?** Claims per subject and aspect with supersession links and expiry.
3. **What is the active underwriting, and who selected it?** One selected `CaseSetRevision` per subject and horizon, with its selection receipt and a diff to its parent.
4. **What exactly was the model shown, and why?** A `ContextPacket` with selected refs, rank explanations, omissions, warnings, runtime block and hash.
5. **What do we hold, how sure are we, and is the mark fresh?** `PositionSnapshot` typed by source class; a plan is never a position.
6. **What was decided, against which constraints, and what proves each step?** `DecisionRecord` with a stored mandate check and a receipt chain for approval, execution and reconciliation.

## What it deliberately does not do

- It does not size a trade. `proposal.notional` is written by a model or a human; the kernel checks it against the mandate and records the result.
- It does not classify intent. `intent_rung` is stored as given, never inferred.
- It does not set the bar for action. A `no_trade` record is written by whoever decided.
- It does not block orders. A failed mandate check is a record. The venue or wallet has to refuse.
- It does not evaluate performance. It makes the chain reconstructable so that an evaluation could be run. None has been.
- It does not rank by embeddings. `retrieval.VectorIndex` is an interface; the only implementation returns nothing.

## The ten problems

The problem titles are from "10 Hard Problems in Agentic Investment Research and Trading" (BITKRAFT, October 2026).

| # | Problem | Addressed by | Not addressed |
|---|---|---|---|
| 1 | A portfolio is a set of claims, not a single fact | `PositionSnapshot` with ordered source classes; invariant 14 (a plan is never a position); mark freshness separate from position freshness | Reconciliation against a broker is outside the kernel; it only records that a `reconciled` snapshot and receipt exist |
| 2 | Memory earns its place only if it changes answers | `Claim` supersession scoped to subject and aspect (invariant 8); packets record what was selected and omitted and why (invariant 7) | Whether the answer actually changed is not measured |
| 3 | Every number needs a source, time, and purpose | authority tiers; freshness policy by claim kind and aspect (invariant 10); authority before similarity in retrieval; unresolved contradictions kept visible (invariant 9) | No source is ever judged true; the kernel only labels and warns |
| 4 | Agentic systems can degrade while still sounding competent | `runtime` block on every packet (invariant 17); `fallback_used` surfaced in output; `health_check` receipts | Detecting degradation is left to whoever reads the receipts; nothing alerts |
| 5 | A prompt is rarely the complete task | `intent_rung` stored on every `DecisionRecord`; approval is an object, not a chat turn | The classification itself; the kernel stores what it was told |
| 6 | Research has to identify the variables that decide the investment | `Case` assumptions with evidence for and against; `Invalidation` conditions; `ResearchQuestion` with evidence needed; the proposal and selection boundary (invariants 3, 4, 5) | Which driver matters is a research judgement, not a kernel function |
| 7 | An idea can only fit into a portfolio when it has a relative size | proposal fields and `check_against_mandate`, which records breaches against a named mandate version (invariant 16) | Sizing. The notional is an input |
| 8 | Restraint is a design requirement | `no_trade` and `hold` records with rationale and evidence; `declined_decisions` report; outcomes attached later as receipts | The decision to hold; whether holding was right |
| 9 | Authority must be enforceable | `Mandate` versioned and activated by a `direct_user` receipt (invariant 13); every decision stores its check; a failed check cannot be approved without an override naming each breach | Enforcement. The kernel cannot stop an order; it records that the control existed |
| 10 | Evaluation has to cover the whole chain | receipt chain: selection, approval, execution, reconcile, delivery, health check; replayable packets; declined decisions queryable | Any outcome or performance measurement. None was run |

## Layout

```text
src/fmk/        models, store (schema and triggers), evidence, claims, underwriting,
                retrieval, receipts, policy, decisions, fixtures
examples/       synthetic_issuer_update.py (section 6.1 end to end), research_review.py
fixtures/       ten JSONL fixtures, all synthetic, each with a failure state
tests/          73 tests covering canonical tests 1 to 10 and extension tests 11 to 17
SPEC.md         the merged specification
NOTES.md        ambiguities resolved during implementation
```

## Running

Requires Python 3.10 or later and pytest.

```sh
python -m pytest -q
python examples/synthetic_issuer_update.py
python examples/research_review.py
```

No installation is needed; tests and examples add `src/` to the path. `pip install -e .` also works.

## Fixtures

Every fixture is synthetic: subject `ACME` or `SUBJ_x`, scope `scope_demo`, source references under `synthetic://`. A test scans fixtures, tests and examples for email addresses, phone numbers, credential-like keys, account identifiers, external URLs and a list of well-known real tickers.

| Fixture | Scenario | Failure state |
|---|---|---|
| `issuer_guidance_cut` | the section 6.1 lifecycle | a model tries to select its own draft |
| `stale_price_conflict` | expired price plus two credible claims that disagree | expired warning; contradiction left unresolved; social chatter omitted |
| `correction_supersession` | primary filing corrects an assistant inference | a social post may not correct a primary-backed claim |
| `duplicate_case_set` | two reviewed revisions for one subject and horizon | second selection refused by the partial unique index |
| `weekend_mixed_marks` | equity marked Friday, crypto marked live | mark staleness warning on the equity |
| `plan_vs_fill` | approved plan older than a broker fill | plan withheld as a position; fill wins; plan flagged as overtaken |
| `approval_lost` | user said yes in chat, no receipt written | execution refused; decision stays `proposed` |
| `mandate_breach` | 12 percent proposal against a 10 percent cap | check recorded as `fail`; approval without override refused |
| `declined_decisions` | three no-trade records with later outcomes | a no-trade without a rationale is refused |
| `model_fallback` | packet built on a fallback model | the flag reaches the rendered output |

## Status

Reference implementation, October 2026. Frozen. Not maintained.

No behavioural or outcome claims are made. See SPEC.md section 15.

## License

MIT. Copyright 2026 Carlos Pereira.
