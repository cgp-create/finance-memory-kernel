# Finance Memory Kernel: Specification

**Version:** 0.3, frozen
**As of:** 2026-10-11
**Scope:** a durable research and decision record for an agent that acts on one investor's money over time. It does not manage portfolios or execute orders.

The implementation in `src/fmk/` is the reference. Where this document and the code differ, the code is right and this document has a bug.

## 0. Why this exists

This is the record model behind a personal agentic investing system, rebuilt from scratch on SQLite with synthetic data so that it can be published. It is published so that others can criticise and fork the schema. The narrow hypothesis it is built for is that a structured, provenance-aware record produces more reproducible and better-grounded research updates than stateless prompting or a chronological chat history, without silently changing what the investor has decided. That hypothesis has not been tested (section 15).

## 1. The problem

An agent working on a portfolio for months is exposed to four kinds of object that are easy to blur:

- **evidence**: what a source said or a measurement observed, and when;
- **claims**: attributable propositions derived from evidence;
- **underwriting**: a selected set of assumptions, cases and probabilities;
- **action**: a decision, its approval, and what was actually executed.

Ordinary retrieval collapses all four into relevant text. A new earnings release may support one claim, challenge another, open a question and justify a proposed revision. It should not change the active probabilities, the price target or a pending order without a review, however well it ranked.

## 2. Design principles

1. Every derived proposition has a source, an observed time, an authority tier and a content hash.
2. New information adds rows and relations. History stays inspectable.
3. Truth is scoped to a subject and an aspect. There is no global "latest note wins".
4. Anyone, including a model, may propose. Review, selection and approval need a named human.
5. One selected revision per subject and horizon at a time.
6. A context packet records what was supplied, why it ranked, what was left out, under which policy, at which time cuts, for which model.
7. Unresolved contradictions and stale dependencies stay visible.
8. Section 5 marks each invariant as enforced by the schema, by the Python API, or only by tests.

## 3. Vocabulary

| Object | Meaning |
|---|---|
| `Evidence` | Append-only record of a source or measurement, with observed and ingested times. |
| `Claim` | An attributable proposition bound to one Evidence row. Can declare what it depends on. |
| `Subject` | A security, issuer, sector, macro driver, fund or thesis. |
| `ResearchQuestion` | An open research need with the evidence that would answer it. |
| `CaseSetRevision` | A versioned underwriting package for one subject and horizon. |
| `Case` | One probabilistic outcome inside a revision, with assumptions and evidence. |
| `Invalidation` | An observable condition that weakens a case. |
| `Trigger` | A condition that calls for review. It does not authorise a trade. |
| `ContextPacket` | The exact selection supplied to a model step, with its runtime and time cuts. |
| `Receipt` | A hash-linked record that a review, selection, approval, execution, delivery, activation, reconciliation or health check happened, and on whose authority. |
| `Mandate` | The investor's standing constraints for a scope. Versioned. |
| `PositionSnapshot` | What is believed to be held, typed by source class and time. |
| `DecisionRecord` | One decision, including the decision to do nothing, with the kernel's check against the mandate. |

### 3.1 Authority references

Every receipt and every proposer is named as `<kind>:<name>`, with kind one of `direct_user`, `reviewer`, `model`, `system`, `venue`. `direct_user` and `reviewer` are human. The kernel enforces rules by kind (section 5), and the schema refuses anything not of this form. For separation of duties, two references name the same person if their name parts match ignoring case and surrounding spaces, whatever the kind. The kernel does not authenticate names. `Store(authorize=fn)` is called before every receipt written through the Python API and can refuse; that is where an application checks identity. Raw SQL does not pass through it.

## 4. Logical model

Table and column names match `src/fmk/store.py`.

```text
subject            id, namespace, key, type, title, status, created_at, updated_at
evidence           id, subject_id, source_kind, source_ref, observed_at, ingested_at,
                   authority_tier, content_hash, payload, supersedes_evidence_id?
claim              id, subject_id, evidence_id, aspect, kind, statement, value_json, confidence,
                   source_strength, status, valid_from?, valid_to?, created_at, expires_at?, recorded_at
claim_relation     id, from_claim_id, relation(supports|contradicts|supersedes|depends_on),
                   to_claim_id, rationale_evidence_id?, created_at
research_question  id, subject_id, question, evidence_needed, priority, owner, status,
                   revisit_at?, answer_claim_ids, created_at, updated_at
case_set_revision  id, subject_id, parent_id?, horizon, status(draft|reviewed|selected|retired),
                   thesis_summary, assumptions_hash, evidence_hash, created_at, selected_at?,
                   retired_at?, review_receipt_id?, selection_receipt_id?, proposed_by
research_case      id, revision_id, label, probability, return_basis, price_target?,
                   expected_return_pct?, return_anchor_at?, assumptions, evidence_for_ids,
                   evidence_against_ids, invalidation_ids, display_order
invalidation       id, subject_id, case_id?, condition, severity, observable_definition,
                   status(active|triggered|closed), evidence_ids, created_at, evaluated_at?
research_trigger   id, subject_id, name, trigger_type, condition, state, action_type,
                   decision_packet, priority, status, last_evaluated_at?
context_packet     id, task_ref, as_of, known_as_of?, policy_version, query_hash, task, selected,
                   ranking_explanations, omitted, warnings, runtime, packet_hash, created_at
receipt            id, kind, object_ref, authority_ref, occurred_at, payload, payload_hash,
                   prior_receipt_id?, prev_chain_hash, chain_hash
mandate            id, scope_ref, version, status(draft|active|retired), parent_id?, body,
                   activated_by_receipt_id?, created_at, retired_at?
position_snapshot  id, scope_ref, subject_id, as_of, source_class, source_ref?, quantity?,
                   mark_price?, mark_as_of?, mark_source_class?, cash_available?,
                   content_hash, ingested_at
decision_record    id, scope_ref, subject_id?, decision_kind, intent_ref?, intent_rung, proposal,
                   rationale_text, evidence_ids, selected_revision_id?, mandate_id, mandate_check,
                   position_snapshot_ids, snapshot_freshness(fresh|stale|mixed|none),
                   conviction?, change_triggers_text?,
                   status(proposed|approved|executed|rejected|expired|superseded),
                   proposed_by, proposed_at, approval_receipt_id?, execution_receipt_id?,
                   reconcile_receipt_id?, superseded_by_id?, idempotency_key?
```

Mandate body fields: `objective_text`, `horizon`, `allowed_asset_classes`, `allowed_venues`, `blocked_subjects`, `max_position_pct` (post-trade position: held + pending + order), `max_subject_exposure_pct`, `max_order_pct`, `max_sector_exposure_pct`, `max_gross_exposure_pct`, `min_cash_pct`, `per_trade_notional_cap`, `daily_notional_cap`, `funding_rule_text`, `min_net_edge_bps`, `max_slippage_bps`, `approval_ttl_seconds`, `proposal_ttl_seconds`, `nav_tolerance_pct`, `freshness_seconds{source_class: seconds, mark: seconds}`.

A decision proposal may carry `direction` (buy, short, sell, cover, reduce, close), `notional`, `quantity`, `venue`, `expected_price` or `limit_price`, `expected_edge_bps`, `estimated_cost_bps`.

### 4.1 Enums

```text
authority_tier:  direct_user > primary > system_measurement > credible_secondary
                 > assistant_inference > social_chatter
claim.kind:      fact | forecast | hypothesis | inference | correction | user_decision
claim.status:    active | stale | superseded | contradicted | retracted | unresolved
source_class:    reconciled > broker_position > broker_fill > plan > estimate > stale_cache
                 (the first three are position-grade)
receipt.kind:    review | selection | approval | apply | execution | delivery |
                 mandate_activation | reconcile | health_check
decision_kind:   trade | rebalance | close (executable) | hold | no_trade
intent_rung:     quick_take | ticker_brief | portfolio_fit | deep_dive | formal_thesis | action
```

### 4.2 Time

Three times matter and are kept apart. `observed_at` on evidence (and `valid_from` on a claim, when given) is when something was true of the world. `created_at` on a claim is when it was asserted, and may be supplied by the caller. `ingested_at` on evidence and `recorded_at` on claims, revisions, questions and invalidations are set from the store clock and cannot be supplied. Freshness runs from observation. A packet's `as_of` cuts by valid time. Its optional `known_as_of` also cuts by recording time and rebuilds statuses as they were then: a claim's superseded, retracted, contradicted (until resolved) and stale states from relation and receipt times, and a revision's draft, reviewed, selected or retired state from its receipt and selection times. A trigger's state is rebuilt from its append-only state events, and a trigger created after either cut is left out. Question and invalidation states are shown as they are now. A stored packet records the status of each object it selected, and `summarize` renders that status, so a saved packet keeps its meaning after later changes. Two events at the same timestamp are treated as simultaneous.

## 5. Invariants, and where each is enforced

S = SQLite schema (holds for any SQL through the connection). P = Python API only. T = tests.

1. **Every claim names evidence and cannot outrank it.** S: `evidence_id NOT NULL` with a foreign key. P: `source_strength` may not exceed the evidence tier.
2. **Evidence is immutable.** S.
3. **Retrieval cannot change underwriting.** S: revisions are inserted as drafts, bodies and receipt links are frozen, cases are immutable and can only be added to a draft. P: `retrieval.py` writes only the packet row.
4. **At most one selected revision per subject and horizon.** S: partial unique index; selection runs under `BEGIN IMMEDIATE`.
5. **Review and selection are human, and the author does not review their own draft.** S: `reviewed` needs a human review receipt; `selected` needs a reviewed revision and a selection receipt, and selection receipts must name a human. P: reviewer differs from `proposed_by`.
6. **Receipts are distinct, append-only and chained.** S: kind enum, no update or delete, each must link to the current chain head (no forks) and be dated no earlier than it, with the time taken from the store clock, human-only kinds, `venue`/`system`-only kinds, authority format. This part detects tampering after the fact and cannot prevent it: `verify_chain()` recomputes payload hashes and links and compares the head with one stored elsewhere.
7. **Packets are replayable.** S: immutable, runtime required. P: the hash covers ids, object hashes, both time cuts, policy version and runtime.
8. **Supersession is scoped and one-way.** S: superseded and retracted claims cannot return; retraction needs a human review receipt immediately before it. P: same subject and aspect; the new claim's authority must be at least the old one's, or it is a contradiction. Retraction needs a human, evidence about the same subject, and evidence at least as strong as the claim's source; weaker evidence contradicts.
9. **Contradictions stay open until a human resolves them.** P: both sides are kept in every packet regardless of budget, without changing their authority; resolution needs a human authority and evidence.
10. **Time-sensitive facts expire or degrade, from when they were observed.** S: expiry is frozen with the claim. P: packets label each claim fresh, degraded or expired and warn.
11. **Returns declare basis and anchor.** S: CHECK constraints.
12. **Fixtures include failure states.** T.
13. **One active mandate per scope, activated by the investor.** S: partial unique index; inserted as draft; draft -> active -> retired; activation needs a `direct_user` receipt; body frozen; no deletes.
14. **A plan is never a position.** P: `current_position` withholds quantity unless the best snapshot is position-grade, ignores snapshots after `now`, prefers fresh over stale, and warns when a newer snapshot disagrees.
15. **Decisions move forward by receipt, and the approver is not the proposer.** S: inserted as `proposed` with no receipt links; body frozen; links write-once; one step at a time; approval receipt must be human and from a different person than the proposer; rejection, expiry and supersession each need a receipt saying so; only `trade`, `rebalance` and `close` can be executed; reconcile needs an executed decision and a reconcile receipt. P: approval of a trade is refused past `proposal_ttl_seconds`, after a newer position-grade snapshot of the subject, or on a restated NAV outside `nav_tolerance_pct`; otherwise the mandate check is rerun inside the write lock against the mandate active then, snapshot freshness, pending commitments and the day's approved notional as of approval; approvals expire after `approval_ttl_seconds`; models cannot expire decisions.
16. **Every decision is checked by the kernel against the active mandate of its scope.** S: the cited mandate must be active and of the same scope, and the stored check must name it. P: `propose_decision` computes the check; there is no parameter for passing one in. A raw INSERT can still store a fabricated check that names the right mandate. A failed check can be approved only with an override that names every breach and gives a reason.
17. **Every packet carries its runtime.** S: CHECK on `runtime`. P: `fallback_used` is printed in the rendered output.
18. **Claims that rest on a changed claim go stale.** P: superseding or retracting a claim marks its transitive `depends_on` dependents stale in the same transaction, each with a `system:kernel` receipt; packets warn on any claim whose basis changed after it was last reaffirmed; reaffirming re-points the claim at the successors of the changed bases; a winner of a resolved contradiction whose basis changed comes back stale. Cycles are refused. S: a stale claim returns to active only when the latest receipt on it is a human reaffirmation.

## 6. Lifecycle

```text
source arrives
  -> Evidence (append-only, hashed)
  -> Claims, with authority, aspect, expiry from observation, and declared dependencies
  -> relations: support, contradiction, supersession; dependents of a superseded claim go stale
  -> question, invalidation and trigger updates
  -> draft CaseSetRevision (anyone, including a model), with a diff to the selected one
  -> human review by someone other than the author
  -> human selection + selection receipt
  -> ContextPacket for the next model step, with runtime and time cuts
  -> DecisionRecord(proposed): the kernel checks it against the active mandate
  -> human approval (not the proposer) -> approval receipt, valid for approval_ttl_seconds
     | or rejection, expiry, or a no_trade / hold recorded with a rationale
  -> venue executes, outside the kernel -> execution receipt with expected price and slippage
  -> reconcile -> reconciled PositionSnapshot + reconcile receipt
  -> later: outcomes attached as review receipts, including on no_trade records
```

### 6.1 Example: synthetic issuer event

`examples/synthetic_issuer_update.py` runs this with a fixed clock.

1. A company cuts guidance. The release is stored as primary evidence.
2. New guidance claims supersede the old ones. An EBIT estimate that rested on the old guidance goes stale.
3. The evidence is linked against the bull case's invalidation. No probability changes.
4. A question is opened: is the cut demand-led, supply-led or deliberate?
5. A model drafts a successor revision with a diff. It tries to review its own draft and is refused.
6. A human reviews. The model tries to select the reviewed draft and is refused. A human selects it.
7. A packet for a later question contains the selected revision, current claims, the open question and the stale EBIT estimate with a warning.
8. A model proposes a trim. The kernel checks it against the mandate. Execution before approval is refused; the model approving its own proposal is refused.
9. A human approves; the venue reports a fill with slippage; the position is reconciled.
10. The receipt chain is verified.

## 7. Retrieval and context assembly

### 7.1 Candidates

Direct subject match and lexical overlap over evidence, claims, questions, invalidations, triggers and revisions, all cut at `as_of`, and at `known_as_of` when given. Each candidate keeps its lane, authority rank, lexical score and reasons. An embedding lane is declared (`retrieval.VectorIndex`) and not implemented. Order: authority, then lexical overlap, then reference.

### 7.2 Filters, in order

1. Remove retracted items.
2. Omit a superseded claim when its successor is in the window, naming the successor; otherwise keep it with a warning.
3. Omit evidence corrected by later evidence in the window, naming the correction.
4. On a factual query, omit social chatter whenever primary or system evidence is present.
5. Label each claim fresh, degraded or expired, with age measured from observation, and warn.
6. Warn on any claim that is stale or rests on a superseded, retracted or stale claim.
7. Keep both sides of an unresolved contradiction and warn. Their authority is not changed.
8. Allow at most one selected revision; label drafts as proposals.
9. Apply the budget, which counts objects (it is not a token count): contradiction parties and the selected revision are kept first, then the highest ranked. Provenance is never stripped.

### 7.3 Packet

```json
{
  "as_of": "2026-10-10T00:00:00Z",
  "known_as_of": null,
  "policy_version": "v1",
  "task": {"kind": "research_update", "subject": "ACME", "query": "..."},
  "selected": [{"ref": "claim:...", "why": "...", "as_of": "...", "hash": "...", "freshness": "fresh"}],
  "ranking_explanations": [{"ref": "...", "lane": "lexical", "authority": 5, "lexical": 0.67}],
  "omitted": [{"ref": "claim:...", "why": "superseded by claim:..."}],
  "warnings": ["claim:... rests on claim:...; refresh or reaffirm before use"],
  "runtime": {"model_id": "...", "model_version": "...", "provider": "...", "route_id": "...",
              "policy_version": "v1", "tool_versions": {}, "source_feed_as_of": {},
              "fallback_used": false, "fallback_reason": null},
  "packet_hash": "..."
}
```

`render_output` prefixes delivered text with the fallback flag when one was used, lists the warnings, and states that the packet is a selection.

## 8. What counts as proof

| Statement | Required |
|---|---|
| This is the active underwriting | revision `selected` with a selection receipt bound to it |
| This is the investor's constraint | mandate `active` with a `direct_user` activation receipt |
| This is what we hold | a position-grade snapshot at or before now, or a warning and no quantity |
| This was approved | an approval receipt from a human other than the proposer, bound to the decision |
| This executed | an execution receipt from a venue or system, bound to the same decision |
| The book reflects it | a reconcile receipt binding a reconciled snapshot of the same scope and subject |
| Nothing was rewritten | `verify_chain(expected_head)` returns no problems against a head stored elsewhere; this covers receipts and the rows they vouch for; evidence and claims rely on the triggers alone |

## 9. Origin

The record model comes from a private system built by one of the essay's authors for their own investing. That system's code, data and history are not in this repository, and nothing here makes claims about it.

## 10. Repository

See README.md for the layout. Standard library and SQLite only.

## 11. Tests

`python -m pytest` runs 138 tests. The numbered scenarios below are referenced from test docstrings. `tests/test_hardening.py` adds attempts to break the record, mostly through raw SQL, each of which must be refused or detected.

1. **Correction supersession:** a primary correction replaces an older inference for the same subject and aspect without deleting history, and claims resting on the old one go stale.
2. **Conflict preservation:** two credible, incompatible claims stay visible until a human resolves them.
3. **Freshness:** an expired price or estimate cannot appear as current without a warning.
4. **Selected-revision uniqueness:** concurrent selections cannot produce two selected revisions for one subject and horizon.
5. **No silent mutation:** retrieval, summarisation and draft generation cannot modify selected underwriting or cases.
6. **Receipt separation:** a selection receipt cannot serve as an execution receipt; a drafted message only counts as delivered once a delivery receipt exists.
7. **Packet replay:** rebuilding a packet from frozen fixture data gives the same references, warnings and hash; with `known_as_of`, later backfills, supersessions, resolutions and selections do not change it.
8. **Authority before similarity:** primary evidence outranks a lexically closer social post on a factual query.
9. **Future leakage:** later evidence cannot enter an earlier packet.
10. **Public safety:** no fixture, test or example contains personal data, credentials, account identifiers, real URLs or real tickers.
11. **Single active mandate:** a second activation for a scope fails until the first is retired.
12. **Plan is not position:** with only `plan` snapshots, holdings come back as a warning with no quantity.
13. **Forward-only decisions:** `proposed -> executed` without approval is refused, and so is any backward move, through the API or raw SQL.
14. **Mandate breach recorded:** a proposal that would take the position over `max_position_pct`, counting what is held and pending, stores a failed check naming the breach; approval without a reasoned override naming it is refused.
15. **No-trade is a record:** a `no_trade` with a rationale can be written, queried, acknowledged and paired with an outcome, and cannot be executed.
16. **Runtime required:** a packet without `runtime.model_id` is refused; a fallback appears in the rendered output.
17. **Stale mark, fresh position:** a snapshot with a current `as_of` and an old `mark_as_of` is served with a warning.

Fixture replay is deterministic: two loads of a fixture give identical rows, hashes and receipt chains.

## 12. Decision layer notes

- A `no_trade` or `hold` is a first-class record with a rationale and the evidence considered. It can be acknowledged by approval but not executed. Its outcome can be attached later, which makes restraint gradable.
- The mandate check is computed at proposal time and again at approval. It records its inputs (NAV, current subject exposure, asset class, the day's approved notional, snapshot freshness, the cited holding, pending commitments), who supplied them, which the kernel computed, and which mandate limits it did not check. Direction matters: selling or covering does not count as adding exposure, and a blocked subject can still be sold. A sell must cite a position-grade holding and cannot exceed its value, and a cover must cite a short, so a mislabelled direction shows up as a breach.
- Snapshot freshness on a decision is computed from the cited snapshots against the mandate's policy. Stale or mixed snapshots on an executable decision are a breach. A buy that cites no snapshot is recorded with freshness `none`, so the current-exposure input is then the caller's word alone.
- `intent_rung` is stored as the caller classified it, so misclassifications are visible later.
- The kernel treats a decision as approved only when an approval receipt exists. A "yes" in chat that never became one leaves the decision `proposed`.
- `idempotency_key` makes a retried proposal return the existing decision.

## 13. The ten problems

The current mapping is in README.md, with a status for each problem. In short: the kernel addresses parts of problems 1, 2, 3, 4, 5, 7 and 10; touches 6 and 8 only through limits and slippage measurement; and does not address 9, which belongs at the account, wallet or venue.

## 14. What this does not do

Size trades; value portfolios; classify intent; check sector, gross or cash limits; handle tax lots, wash sales, market calendars or corporate actions; authenticate or sign anything; block an order at a venue; rank by embeddings; resolve contradictions or update probabilities on its own; evaluate performance.

## 15. Evaluation (not done)

No behavioural or outcome evaluation has been run. The repository makes no claim that the kernel produces better research, better decisions or better returns than stateless prompting or chronological history. The tests check that the invariants hold on synthetic data; they do not measure research quality. Such a claim would need a separate experiment with pinned inputs, a strong simple baseline and reported dispersion, none of which exists here.

## 16. Not carried into this repository

Account identifiers, broker adapters, live valuation, production data, operational controls and any real investor content. Fixtures use the synthetic scope `scope_demo`, synthetic subjects `ACME`, `SUBJ_*`, `EQ_*`, and `synthetic://` sources.
