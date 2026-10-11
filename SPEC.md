# Finance Memory Kernel: Specification

**Status:** reference implementation, frozen
**As of:** 2026-10-10
**Scope:** a durable investment research and decision record. Not a portfolio management system, an execution engine, or a generic personal memory product.

This document merges the canonical kernel specification (sections 0 to 11, invariants 1 to 12) with the decision-layer extension (section 12 onward, invariants 13 to 17). Where the two differ in wording, the implementation in `src/fmk/` is the reference.

## 0. Why this document exists

This specification was derived from the finance research and memory contracts of one production system as they stood on 2026-10-10. It separates:

1. **the public kernel**: a portable model, interfaces, invariants and fixtures suitable for an open repository; from
2. **one production implementation**: a PostgreSQL-backed system, which is evidence that some primitives exist, not proof that the kernel improves decisions.

The central claim to test is narrow: structured, provenance-aware investment memory should produce more reproducible and better-grounded research updates than stateless prompting or chronological history, without silently mutating active underwriting. That claim has not been tested. See section 15.

## 1. The problem: investment research is a changing belief system

Investment work does not fail because an agent lacks a place to put notes. It fails because new information is allowed to blur four different objects:

- **evidence**: what a source said or what a measurement observed;
- **claims**: an attributable proposition derived from evidence;
- **underwriting**: a selected set of assumptions, cases and probabilities;
- **action**: a human-approved decision or execution.

Ordinary retrieval collapses these into "relevant text". A finance kernel must preserve the distinction across time. A new earnings release may support a claim, challenge an old claim, open a research question and justify a proposed model revision. It must not silently revise an active price target, probability distribution or action recommendation because it retrieved well.

The kernel therefore treats memory as an append-oriented evidence and belief graph with an explicit selection boundary.

## 2. Design principles

1. **Source before summary.** Every material derived proposition has a source reference, observed time, authority tier and content hash.
2. **Append, do not overwrite.** New information adds records and relations; historical claims and revisions remain inspectable.
3. **Truth is scoped.** A claim can be current for a subject and aspect, stale for a current-price question, contradicted, or unresolved. There is no universal "latest note wins".
4. **Evidence may propose; only authority may select or apply.** Retrieval and model outputs cannot update canonical underwriting by themselves.
5. **One selected live model per subject and horizon.** Draft, reviewed and retired revisions may coexist. Exactly one selected revision governs a subject and horizon at a time.
6. **Provenance travels with retrieval.** A context packet records what was supplied, why it ranked, what was excluded and the policy version used.
7. **Uncertainty is a first-class output.** Unresolved contradictions and missing evidence remain visible; the system must not invent a winner.
8. **Finance is a specialized stream, not a blank slate.** Subjects, cases, open questions, invalidations, triggers and synthesis already exist in investment research. The kernel formalizes them.
9. **Evaluation is outcome-oriented.** A deployed table or gate is not proof of better research. This repository makes no such claim.

## 3. Kernel vocabulary

| Object | Meaning | What it must never be confused with |
|---|---|---|
| `Evidence` | Immutable record of an observed source or measurement. | A conclusion or target price. |
| `Claim` | Atomic, attributable proposition supported or challenged by evidence. | A free-form note without provenance. |
| `Subject` | Research object: security, issuer, sector, macro driver, fund or thesis. | A ticker string only. |
| `Question` | Explicit unresolved research need with a revisit condition. | A rhetorical checklist item. |
| `CaseSetRevision` | Versioned underwriting package for one subject and horizon. | A mutable "current model" row. |
| `Case` | One probabilistic outcome inside a revision. | A prediction detached from assumptions and evidence. |
| `Invalidation` | Observable condition that weakens or breaks an assumption or case. | Generic downside prose. |
| `Trigger` | Machine- or human-evaluable condition that calls for review or action. | Permission to trade automatically. |
| `Synthesis` | Reproducible snapshot of current research state and open tensions. | Canonical evidence. |
| `ContextPacket` | The exact selected context supplied to a research or answer step, with the runtime that produced it. | A transcript dump. |
| `Receipt` | Immutable evidence that a review, selection, approval, execution, delivery, activation, reconciliation or health check happened. | An intention to do so. |
| `Mandate` | The investor's standing constraints for a scope. Versioned, never edited in place. | A preference note. |
| `PositionSnapshot` | What the system believes it holds, typed by source class and time. | A plan or an estimate. |
| `DecisionRecord` | One decision, including the decision to do nothing. The object an approval attaches to. | A chat message saying "yes". |

## 4. Public logical model

The reference implementation uses SQLite. JSON columns hold structured domain payloads, not unbounded prose. The table and column names below match `src/fmk/store.py`.

```text
subject
  id, namespace, key, type, title, status, created_at, updated_at

evidence
  id, subject_id, source_kind, source_ref, observed_at, ingested_at,
  authority_tier, content_hash, payload, supersedes_evidence_id?

claim
  id, subject_id, evidence_id, aspect, kind, statement, value_json,
  confidence, source_strength, status, valid_from, valid_to,
  created_at, expires_at

claim_relation
  id, from_claim_id, relation(supports|contradicts|supersedes|depends_on),
  to_claim_id, rationale_evidence_id?, created_at

research_question
  id, subject_id, question, evidence_needed, priority, owner,
  status, revisit_at, answer_claim_ids, created_at, updated_at

case_set_revision
  id, subject_id, parent_id?, horizon, status(draft|reviewed|selected|retired),
  thesis_summary, assumptions_hash, evidence_hash, created_at, selected_at?,
  retired_at?, review_receipt_id?, selection_receipt_id?, proposed_by

research_case
  id, revision_id, label, probability, return_basis, price_target?,
  expected_return_pct?, return_anchor_at?, assumptions, evidence_for_ids,
  evidence_against_ids, invalidation_ids, display_order

invalidation
  id, subject_id, case_id?, condition, severity, observable_definition,
  status, evidence_ids, created_at, evaluated_at?

research_trigger
  id, subject_id, name, trigger_type, condition, state,
  action_type, decision_packet, priority, status, last_evaluated_at

synthesis
  id, subject_id, as_of, policy_version, claim_ids, evidence_ids,
  selected_revision_id?, summary, open_question_ids, warning_ids, content_hash

context_packet
  id, task_ref, as_of, policy_version, query_hash, task, selected,
  ranking_explanations, omitted, warnings, runtime, packet_hash, created_at

receipt
  id, kind, object_ref, authority_ref, occurred_at, payload, payload_hash,
  prior_receipt_id?

mandate
  id, scope_ref, version, status(draft|active|retired), parent_id?,
  body (objective_text, horizon, allowed_asset_classes[], allowed_venues[],
        blocked_subjects[], max_position_pct, max_subject_exposure_pct,
        max_sector_exposure_pct, max_gross_exposure_pct, min_cash_pct,
        per_trade_notional_cap, daily_notional_cap,
        requires_approval(always|above_notional|never),
        approval_notional_threshold, funding_rule_text, freshness_seconds{}),
  activated_by_receipt_id?, created_at, retired_at?

position_snapshot
  id, scope_ref, subject_id, as_of, source_class, source_ref,
  quantity?, cost_basis?, mark_price?, mark_as_of?, mark_source_class?,
  cash_available?, pending_order_refs[], content_hash, ingested_at

decision_record
  id, scope_ref, subject_id?, decision_kind, intent_ref, intent_rung,
  proposal{direction, notional, quantity?, venue?, order_type?, funding_source?},
  rationale_text, evidence_ids[], selected_revision_id?,
  mandate_id, mandate_check{status, breaches[], mandate_id, mandate_version},
  position_snapshot_ids[], snapshot_freshness(fresh|stale|mixed),
  conviction?, change_triggers_text?,
  status(proposed|approved|rejected|executed|expired|superseded),
  proposed_by(model|user), proposed_at,
  approval_receipt_id?, execution_receipt_id?, reconcile_receipt_id?,
  superseded_by_id?
```

### 4.1 Enums

```text
authority_tier: direct_user | primary | system_measurement | credible_secondary |
                assistant_inference | social_chatter
claim.kind:     fact | forecast | hypothesis | inference | correction | user_decision
claim.status:   active | stale | superseded | contradicted | retracted | unresolved
case_set.status: draft | reviewed | selected | retired
trigger.state:  green | amber | red | tripped | suppressed
return_basis:   price_only | total_return
receipt.kind:   review | selection | approval | apply | execution | delivery |
                mandate_activation | reconcile | health_check
source_class:   broker_fill | broker_position | reconciled | plan | estimate | stale_cache
decision_kind:  trade | rebalance | hold | no_trade | close
intent_rung:    quick_take | ticker_brief | portfolio_fit | deep_dive | formal_thesis | action
```

Source classes are ordered by authority for current-state questions: `reconciled > broker_position > broker_fill > plan > estimate > stale_cache`. Only the first three are position-grade.

The names may change in code. The separations must not.

## 5. Hard invariants

These are database and application constraints, not prompt suggestions. The right-hand note says where the implementation enforces each one.

1. **Every Claim has at least one Evidence reference.** A human may add a claim, but its source must be declared; an unsupported agent inference is marked as such. (`claim.evidence_id NOT NULL` with a foreign key; `claims.add_claim`.)
2. **Evidence is immutable.** Corrections create a new evidence item and a claim relation; they never edit an old source into agreement. (Triggers refuse UPDATE and DELETE on `evidence`.)
3. **No retrieval may mutate CaseSetRevision, Case, selection or probability.** Retrieval can create a draft only. (Triggers freeze case rows and revision bodies; cases may only be added to a draft; `retrieval.py` writes only the packet row.)
4. **At most one selected revision exists per `(subject, horizon)`.** (Partial unique index `ux_one_selected_revision`; selection runs under `BEGIN IMMEDIATE`.)
5. **Selecting a revision requires a `Receipt(kind=selection)` that names the authority and the reviewed revision.** (Trigger checks the receipt before status may become `selected`; a model or retrieval authority is refused; a draft must be reviewed first.)
6. **Applying an action requires a separate receipt.** `selection` is not `approval`; `approval` is not `execution`; `delivery` is not "message drafted". (`receipts.require_receipt` checks kind and object binding; receipts are append-only.)
7. **Every context packet is replayable.** Store ids, source hashes, as-of timestamps, policy version, runtime and a packet hash, not rendered prose. (Packet rows are immutable; the hash covers the canonical JSON body.)
8. **Supersession is directional and scoped.** A new claim may supersede a prior claim for `subject + aspect`; it cannot erase unrelated claims. (`claims.supersede` refuses a different subject or aspect.)
9. **Contradiction can remain unresolved.** The system represents competing claims; it never auto-resolves a material conflict with a similarity score. (`claims.contradict` marks both; `resolve_contradiction` requires a named human or primary authority and evidence.)
10. **Time-sensitive facts expire or degrade.** Price, holdings, estimates and calendar facts have tighter clocks than a user-stated investing principle. (`policy.FreshnessPolicy`; retrieval warns and labels, never silently drops.)
11. **Returns declare basis.** Any return field carries price-only versus total-return basis and an anchor time. (CHECK constraints on `research_case`.)
12. **Fixtures include failure states.** A kernel tested only on clean, confirming evidence has not been tested. (Every fixture carries a refused operation or a warning state; `tests/test_failure_fixtures.py` asserts it.)
13. **One active Mandate per scope.** (Partial unique index `ux_mandate_active`; activation requires a `mandate_activation` receipt with `direct_user` authority.)
14. **A plan is never a position.** A current-holdings answer must cite a snapshot with source class in `{reconciled, broker_position, broker_fill}` or withhold the quantity with a warning. (`decisions.current_position`.)
15. **Decisions move forward only, by receipt.** No status transition without the matching receipt bound to that decision; no skipping. (Triggers on `decision_record`; `decisions._advance`.)
16. **Every DecisionRecord names the Mandate version it was checked against** and stores the check result. A decision with a failed check can be recorded but cannot be approved without an override payload that names every breach. (CHECK on `mandate_check`; `decisions.approve_decision`.)
17. **Every packet carries its runtime.** A packet without `runtime.model_id` and `policy_version` is invalid. `fallback_used` cannot be null. (CHECK on `context_packet.runtime`; `retrieval.validate_runtime`.)

## 6. Lifecycle

```text
source arrives
  -> Evidence (immutable)
  -> Claim(s) with authority, aspect, confidence and expiry
  -> relations to prior claims (support / contradiction / supersession)
  -> question / invalidation / trigger updates
  -> draft CaseSetRevision proposal, if warranted
  -> human or authorized review
  -> selected CaseSetRevision + selection receipt
  -> answer or research step receives ContextPacket (with runtime block)
  -> intent_rung recorded on a DecisionRecord(status=proposed)
  -> proposal filled from selected revision + current PositionSnapshots
  -> mandate_check computed against the active Mandate, stored
  -> approval -> Receipt(approval) -> status=approved
     | or rejection / no_trade recorded with rationale
  -> venue or wallet executes (outside the kernel) -> Receipt(execution)
  -> reconcile -> PositionSnapshot(source_class=reconciled) + Receipt(reconcile)
  -> later: outcome attached for evaluation, including for no_trade records
```

The critical boundary is between proposal and selection. A strong model may explain why a revision should change. It is not thereby entitled to change it. The same boundary applies between a proposed decision and an approved one.

### 6.1 Example: synthetic issuer event

`examples/synthetic_issuer_update.py` runs this end to end.

1. A company reports a guidance cut. Store the release as `Evidence` with observed time and primary authority.
2. Add claims: "FY revenue guide reduced", "margin assumption under pressure", and an inference that the bull case needs reassessment.
3. Link the evidence against the relevant invalidation. Do not revise probabilities in place.
4. Open a question: "Is the guide cut demand-led, supply-led, or deliberate pruning?" Set evidence needed and a revisit date.
5. Create a draft successor revision with changed probabilities and a diff against the selected revision.
6. On review, select the new revision or reject it. Record a selection receipt.
7. A later research answer receives a packet containing the selected revision, current claims, the open question and the primary evidence, not a blend of all historical probabilities.
8. A decision is proposed against the active mandate, approved, executed and reconciled, each with its own receipt.

## 7. Retrieval and context assembly

Retrieval is a selection problem, not a truth machine.

### 7.1 Candidate generation

The reference implementation generates candidates from direct subject match and lexical overlap over evidence, claims, questions, invalidations, triggers and revisions, everything cut at the packet's `as_of`. Each candidate keeps its lane, authority rank, lexical score and a rank explanation. An embedding lane is declared as an interface (`retrieval.VectorIndex`) and not implemented; the shipped `NoVectorIndex` returns nothing. Rank order is deterministic: authority first, then lexical overlap, then object reference.

### 7.2 Deterministic filters before model synthesis

Applied in this order by `retrieval.apply_filters`:

1. Remove retracted items.
2. Prefer active over superseded for the same subject and aspect, recording the supersession link in the omission.
3. Do not promote low-authority commentary over primary evidence because it is semantically closer. On a factual query, social chatter is omitted whenever primary or system evidence is present.
4. Apply freshness policy by claim kind and aspect; warn and label rather than silently use stale data.
5. Keep unresolved contradictions visible when both sides remain material.
6. Include exactly one selected case revision for underwriting questions; drafts are labelled proposals.
7. Enforce a budget by dropping lower-ranked candidates, never by stripping provenance.

### 7.3 Context packet contract

```json
{
  "as_of": "2026-10-10T00:00:00Z",
  "policy_version": "v1",
  "task": {"kind": "research_update", "subject": "ACME", "query": "..."},
  "selected": [
    {"ref": "evidence:...", "why": "primary evidence observed ...", "as_of": "...", "hash": "..."},
    {"ref": "claim:...", "why": "fact claim on guidance.fy_revenue, status active", "as_of": "...", "hash": "..."},
    {"ref": "case_set:...", "why": "selected underwriting revision", "as_of": "...", "hash": "..."}
  ],
  "ranking_explanations": [{"ref": "...", "lane": "lexical", "authority": 5, "lexical": 0.67}],
  "warnings": ["unresolved contradiction on unit_economics.contribution_margin: claim:... vs claim:..."],
  "omitted": [{"ref": "claim:...", "why": "superseded by claim:..."}],
  "runtime": {
    "model_id": "...", "model_version": "...", "provider": "...", "route_id": "...",
    "policy_version": "v1", "tool_versions": {}, "source_feed_as_of": {},
    "fallback_used": false, "fallback_reason": null
  },
  "packet_hash": "..."
}
```

Rules for the runtime block:

- `fallback_used=true` must be surfaced in the delivered output, not only logged. `retrieval.render_output` prefixes the output with the flag.
- A scheduled job records one packet per run; a run with no packet is a failed run, whether or not something rendered.
- Known-answer fixtures run through the same packet path and their results are stored as `Receipt(kind=health_check)` with the fixture hash and pass or fail.

The response model may reason over this packet. It must cite uncertainty and should identify what evidence would change its view. It must not represent the packet as exhaustive.

## 8. Review, approval and execution controls

The kernel is not a governor. Hard gates are few and receipt-based:

| Claim or action | Required proof |
|---|---|
| "This is the active underwriting" | revision in status `selected` plus a selection receipt bound to it |
| "The model was updated" | revision diff plus selection receipt |
| "This is the investor's constraint" | `Mandate` in status `active` plus a `mandate_activation` receipt with `direct_user` authority |
| "This is what we hold" | `PositionSnapshot` with a position-grade source class, or a warning |
| "This trade or action is approved" | `approval` receipt bound to the exact `DecisionRecord` |
| "This executed" | `execution` receipt from the venue, bound to the same decision |
| "The book reflects it" | `reconcile` receipt binding a `reconciled` snapshot |
| "This delivered" | `delivery` receipt from the relevant system |
| "This is live or monitored" | current input, evaluation and a monitoring receipt |

Everything else is advisory. The kernel flags stale data, unresolved conflict or missing source; it does not manufacture extra conversational turns.

## 9. Relation to the production system

This specification was derived from a private production system. Its internals, contents and counts are not part of this repository and are not claims about outcome quality. The decision layer (sections 12 to 14) had no production counterpart when this repository was frozen; its only implementation is here.

## 10. Public reference repository

```text
finance-memory-kernel/
  README.md
  SPEC.md                         # this document
  NOTES.md                        # ambiguities resolved during implementation
  LICENSE
  pyproject.toml
  src/fmk/
    models.py                     # dataclasses, enums, errors, hashing
    store.py                      # SQLite schema, triggers, indexes, generic io
    evidence.py                   # subjects, evidence append, content hashing
    claims.py                     # claims, relations, supersession, contradiction, questions
    underwriting.py               # revisions, cases, diff, review, selection guard
    retrieval.py                  # candidates, filters, packet assembly, replay, render
    receipts.py                   # append-only receipt contract
    policy.py                     # freshness and authority policies
    decisions.py                  # Mandate, PositionSnapshot, DecisionRecord
    fixtures.py                   # JSONL fixture replay through the public functions
  examples/
    synthetic_issuer_update.py    # section 6.1 end to end
    research_review.py            # packet review on frozen fixture data
  fixtures/                       # ten JSONL fixtures, all synthetic
  tests/
    test_invariants.py
    test_retrieval.py
    test_replay.py
    test_no_silent_model_mutation.py
    test_failure_fixtures.py
    test_decision_layer.py
```

The implementation is standard library plus SQLite. Vector search is an interface only.

## 11. Required tests

Canonical:

1. **Correction supersession:** a direct user or primary correction replaces an older inference for the same subject and aspect without deleting history.
2. **Conflict preservation:** two credible but incompatible claims remain visible until an authorized relation resolves them.
3. **Freshness:** an expired price or estimate cannot appear as current without a warning.
4. **Selected-revision uniqueness:** concurrent selection attempts cannot yield two selected revisions for one subject and horizon.
5. **No silent mutation:** retrieval, summarization and draft generation cannot modify selected underwriting or cases.
6. **Receipt separation:** selection cannot be used as execution; draft delivery cannot be reported as delivered.
7. **Packet replay:** rebuilding a packet from frozen fixture data yields the same object references, warnings and hash.
8. **Authority before similarity:** primary evidence ranks ahead of semantically close social commentary when the query asks for factual state.
9. **Future leakage:** a later evidence item cannot enter an earlier packet.
10. **Public safety:** no test or example requires personal data, credentials, broker data or production identifiers.

Extension:

11. **Single active mandate:** activating a second Mandate for the same scope without retiring the first fails.
12. **Plan is not position:** a current-holdings query against a scope with only `plan` snapshots returns a warning, never a quantity as fact.
13. **Forward-only decisions:** `proposed -> executed` without an approval receipt is rejected; `executed -> proposed` is rejected.
14. **Mandate breach recorded, not hidden:** a proposal exceeding `max_position_pct` stores `mandate_check.status='fail'` with the breach named; approval without an override payload is rejected.
15. **No-trade is a record:** a `no_trade` DecisionRecord with rationale and evidence can be written, queried, and later paired with an outcome.
16. **Runtime required:** a packet without `runtime.model_id` is rejected; `fallback_used=true` appears in the rendered output.
17. **Stale mark, fresh position:** a snapshot with current `as_of` and stale `mark_as_of` is served with a mark-staleness warning.

## 12. Decision layer: the four objects

### 12.1 Mandate

The investor's standing constraints for a scope (an account, a sleeve, a strategy). Versioned, never edited in place.

- Exactly one `active` Mandate per `scope_ref` at a time. Activation requires `Receipt(kind=mandate_activation)` with `direct_user` authority.
- A Mandate is a claim-like object with `authority_tier=direct_user` and no expiry; it is the one kind of memory that constrains rather than informs.
- Limits are declared in one place so a proposal can be checked against a single named version. A check result names the Mandate id and version it was checked against.
- The body carries a `freshness_seconds` policy per snapshot source class, and a `mark` entry for how far a mark may lag its position.

### 12.2 PositionSnapshot

What the system believes it holds, typed by source class and time.

- A `plan` may never be reported as a position. Neither may an `estimate` or a `stale_cache`.
- `mark_as_of` is separate from `as_of`. A position can be current while its mark is stale (the weekend case).
- A snapshot older than the Mandate's freshness policy for its source class is served with a warning, never silently.
- An older `plan` that a fill or position has overtaken is flagged so it is not reported as pending.

### 12.3 DecisionRecord

One decision, including the decision to do nothing.

- A `no_trade` or `hold` record is first class, with a rationale and the evidence considered. This makes restraint gradable: the set of declined decisions and what happened to those subjects afterwards.
- `status` transitions only forward and only via receipts: `proposed -> approved` requires `Receipt(kind=approval, object_ref=decision:<id>)`; `approved -> executed` requires `Receipt(kind=execution)`; a later `Receipt(kind=reconcile)` binds the fill to a new `reconciled` PositionSnapshot.
- `mandate_check` is computed at proposal time against the active Mandate and stored. It is a record of the check, not enforcement. The venue or wallet still has to refuse the order.
- `intent_rung` records which rung of the decision ladder the request was classified as. The classification is the caller's; the kernel stores it so misclassifications are visible afterwards.
- An approval that cannot be found as a receipt does not exist. The failure "approval turned back into clarification" is the system looking for this object and not finding it.

### 12.4 ContextPacket runtime block

See section 7.3. The block is required on every packet (invariant 17).

## 13. What the decision layer does for the ten problems

| Problem | Base kernel | With the decision layer |
|---|---|---|
| 1 State is a claim | filter heuristics | typed `PositionSnapshot` with source class and mark freshness |
| 2 Memory binds | supersession, packets | unchanged |
| 3 Which numbers | freshness by claim kind | plus snapshot freshness and mark provenance |
| 4 Silent degradation | not addressed | `runtime` block, fallback flag, health-check receipts |
| 5 Question vs trade | not addressed | `intent_rung` recorded; approval is an object |
| 6 Research drivers | proposal and selection boundary | unchanged |
| 7 Sizing | not addressed | proposal fields plus the mandate check (the check, not the judgement) |
| 8 Restraint | not addressed | `no_trade` and `hold` records with rationale; gradable |
| 9 Enforceable boundaries | not addressed | Mandate as the readable authoritative input; check result stored. Enforcement stays in the venue or wallet. |
| 10 Prove it worked | receipts for selection, execution, delivery | full chain: approval, execution, reconcile, health check, plus declined decisions |

## 14. What this deliberately does not do

- It does not decide size. `proposal.notional` is written by a model or a human; the kernel checks it against the Mandate and records the result.
- It does not classify intent. `intent_rung` is stored, not inferred.
- It does not set the bar for action. A `no_trade` record is written by whoever decided; the kernel makes the decision visible and comparable later.
- It does not block anything. A failed `mandate_check` is a record. If the venue accepts the order anyway, the kernel has evidence that the control was a convention.
- It does not evaluate performance. It makes the chain reconstructable so that evaluation is possible.
- It does not rank by embeddings. The vector lane is an interface with no implementation.
- It does not auto-resolve contradictions, update probabilities from a summary, or run a hidden router.

## 15. Evaluation (not done)

No behavioural or outcome evaluation has been run on this implementation. The repository makes no claim that the kernel produces better research, better decisions, lower regression or any performance gain over stateless prompting or chronological history. The tests in `tests/` verify that the invariants hold on synthetic data. They do not measure research quality. Any such claim would require a separate experiment with pinned inputs, a strong simple baseline and reported dispersion, none of which exists here.

## 16. Implementation sequence (as followed)

1. Public types, append-only SQLite store and invariant tests.
2. Revision selection and receipt checks before any retrieval.
3. Deterministic lexical and entity retrieval with replayable packets.
4. Decision layer: Mandate, PositionSnapshot, DecisionRecord, runtime block.
5. Synthetic fixtures that include contradictions, stale facts, correction events, a double selection, a lost approval, a mandate breach, declined decisions and a model fallback.

An embedding lane behind the same packet contract was left as an interface.

## 17. Production assumptions not carried into this repository

Account identifiers, broker adapters, live valuation paths, production database contents, operational controls and any `direct_user` content. Fixtures use the synthetic scope `scope_demo` and synthetic subjects `ACME`, `SUBJ_*`, `EQ_*` only. Every source reference uses the `synthetic://` scheme.
