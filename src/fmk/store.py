"""SQLite store: one schema for the research record and the decision layer.

Owns the connection, creates the schema, and enforces every constraint
that can be stated in SQL: foreign keys, enum CHECKs, partial unique
indexes, and triggers that freeze bodies, restrict status transitions,
refuse deletes, require receipts, and keep the receipt chain linked.
These hold against any SQL issued through this connection, not only
against the Python API. They do not hold against someone who edits the
file with triggers dropped; the receipt chain (receipts.verify_chain) is
how that kind of tampering is detected after the fact.

Which invariant is enforced where is listed in SPEC.md section 5.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from .models import (
    REF_PREFIX_TO_TABLE,
    TABLE_TO_CLASS,
    InvariantViolation,
    KernelError,
    Record,
    split_ref,
    new_id,
    utc_now,
)

SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS subject (
  id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  key TEXT NOT NULL,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (namespace, key)
);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  authority_tier TEXT NOT NULL CHECK (authority_tier IN
    ('direct_user','primary','system_measurement','credible_secondary',
     'assistant_inference','social_chatter')),
  content_hash TEXT NOT NULL,
  payload TEXT NOT NULL,
  supersedes_evidence_id TEXT REFERENCES evidence(id)
);
-- Invariant 2: evidence is immutable.
CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'invariant 2: evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'invariant 2: evidence is immutable'); END;

CREATE TABLE IF NOT EXISTS claim (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  -- Invariant 1: every claim names its evidence.
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  aspect TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN
    ('fact','forecast','hypothesis','inference','correction','user_decision')),
  statement TEXT NOT NULL,
  value_json TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  source_strength TEXT NOT NULL CHECK (source_strength IN
    ('direct_user','primary','system_measurement','credible_secondary',
     'assistant_inference','social_chatter')),
  status TEXT NOT NULL CHECK (status IN
    ('active','stale','superseded','contradicted','retracted','unresolved')),
  valid_from TEXT,
  valid_to TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  -- when the kernel wrote the row; set from the store clock, never by the caller
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_claim_subject_aspect ON claim(subject_id, aspect);
-- Only status may change on a claim. Authority, expiry and validity are part
-- of the proposition and are frozen with it.
CREATE TRIGGER IF NOT EXISTS trg_claim_body_frozen BEFORE UPDATE ON claim
WHEN NEW.subject_id IS NOT OLD.subject_id OR NEW.evidence_id IS NOT OLD.evidence_id
  OR NEW.aspect IS NOT OLD.aspect OR NEW.kind IS NOT OLD.kind
  OR NEW.statement IS NOT OLD.statement OR NEW.value_json IS NOT OLD.value_json
  OR NEW.confidence IS NOT OLD.confidence OR NEW.created_at IS NOT OLD.created_at
  OR NEW.source_strength IS NOT OLD.source_strength OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.valid_from IS NOT OLD.valid_from OR NEW.valid_to IS NOT OLD.valid_to
  OR NEW.recorded_at IS NOT OLD.recorded_at
BEGIN SELECT RAISE(ABORT, 'claim body is frozen; add a new claim and a relation'); END;
-- Retracted is terminal; superseded can only be retracted.
CREATE TRIGGER IF NOT EXISTS trg_claim_status_rules BEFORE UPDATE OF status ON claim
WHEN NEW.status IS NOT OLD.status
  AND (OLD.status = 'retracted' OR (OLD.status = 'superseded' AND NEW.status <> 'retracted'))
BEGIN SELECT RAISE(ABORT, 'invariant 8: a retracted or superseded claim does not come back'); END;
CREATE TRIGGER IF NOT EXISTS trg_claim_reaffirm_needs_receipt BEFORE UPDATE OF status ON claim
WHEN OLD.status = 'stale' AND NEW.status = 'active' AND NOT EXISTS (
  SELECT 1 FROM (SELECT payload, authority_ref FROM receipt WHERE object_ref = 'claim:' || NEW.id
                 ORDER BY rowid DESC LIMIT 1) r
  WHERE json_extract(r.payload, '$.reaffirmed') = 1
    AND substr(r.authority_ref, 1, instr(r.authority_ref, ':') - 1) IN ('direct_user','reviewer'))
BEGIN SELECT RAISE(ABORT, 'invariant 18: a stale claim returns to active only with a human reaffirm receipt'); END;
CREATE TRIGGER IF NOT EXISTS trg_claim_no_delete BEFORE DELETE ON claim
BEGIN SELECT RAISE(ABORT, 'claims are never deleted; retract or supersede'); END;

CREATE TABLE IF NOT EXISTS claim_relation (
  id TEXT PRIMARY KEY,
  from_claim_id TEXT NOT NULL REFERENCES claim(id),
  relation TEXT NOT NULL CHECK (relation IN ('supports','contradicts','supersedes','depends_on')),
  to_claim_id TEXT NOT NULL REFERENCES claim(id),
  rationale_evidence_id TEXT REFERENCES evidence(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_relation_to ON claim_relation(to_claim_id, relation);
CREATE TRIGGER IF NOT EXISTS trg_relation_no_update BEFORE UPDATE ON claim_relation
BEGIN SELECT RAISE(ABORT, 'claim relations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_relation_no_delete BEFORE DELETE ON claim_relation
BEGIN SELECT RAISE(ABORT, 'claim relations are append-only'); END;

CREATE TABLE IF NOT EXISTS research_question (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  question TEXT NOT NULL,
  evidence_needed TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 3,
  owner TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','answered','dropped')),
  revisit_at TEXT,
  answer_claim_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_question_frozen BEFORE UPDATE ON research_question
WHEN NEW.subject_id IS NOT OLD.subject_id OR NEW.question IS NOT OLD.question
  OR NEW.evidence_needed IS NOT OLD.evidence_needed OR NEW.created_at IS NOT OLD.created_at
  OR NEW.recorded_at IS NOT OLD.recorded_at OR (OLD.status <> 'open' AND NEW.status IS NOT OLD.status)
BEGIN SELECT RAISE(ABORT, 'a question is frozen once asked; answered or dropped is final'); END;
CREATE TRIGGER IF NOT EXISTS trg_question_no_delete BEFORE DELETE ON research_question
BEGIN SELECT RAISE(ABORT, 'questions are never deleted; drop them'); END;

-- Receipts. authority_ref is '<kind>:<name>'. Each row carries the chain hash
-- of the row before it, so the table is one linked chain from 'genesis'.
CREATE TABLE IF NOT EXISTS receipt (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('review','selection','approval','apply','execution','delivery',
     'mandate_activation','reconcile','health_check')),
  object_ref TEXT NOT NULL,
  authority_ref TEXT NOT NULL CHECK (
    instr(authority_ref, ':') > 1
    AND substr(authority_ref, 1, instr(authority_ref, ':') - 1)
        IN ('direct_user','reviewer','model','system','venue')
    AND length(trim(substr(authority_ref, instr(authority_ref, ':') + 1))) > 0),
  occurred_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  prior_receipt_id TEXT REFERENCES receipt(id),
  prev_chain_hash TEXT NOT NULL UNIQUE,
  chain_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_receipt_object ON receipt(object_ref, kind);
CREATE TRIGGER IF NOT EXISTS trg_receipt_no_update BEFORE UPDATE ON receipt
BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_receipt_no_delete BEFORE DELETE ON receipt
BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
-- The chain cannot fork: each new receipt must link to the current head.
CREATE TRIGGER IF NOT EXISTS trg_receipt_chain_link BEFORE INSERT ON receipt
WHEN NEW.prev_chain_hash IS NOT COALESCE((SELECT chain_hash FROM receipt ORDER BY rowid DESC LIMIT 1), 'genesis')
BEGIN SELECT RAISE(ABORT, 'invariant 6: receipt must link to the current chain head'); END;
-- Receipt time never runs backwards along the chain.
CREATE TRIGGER IF NOT EXISTS trg_receipt_time_forward BEFORE INSERT ON receipt
WHEN NEW.occurred_at < COALESCE((SELECT occurred_at FROM receipt ORDER BY rowid DESC LIMIT 1), '')
BEGIN SELECT RAISE(ABORT, 'invariant 6: a receipt cannot be dated before the previous one'); END;
-- Separation of duties by actor kind.
CREATE TRIGGER IF NOT EXISTS trg_receipt_human_only BEFORE INSERT ON receipt
WHEN NEW.kind IN ('selection','approval','mandate_activation')
  AND substr(NEW.authority_ref, 1, instr(NEW.authority_ref, ':') - 1) NOT IN ('direct_user','reviewer')
BEGIN SELECT RAISE(ABORT, 'invariant 6: selection, approval and activation require a human authority'); END;
CREATE TRIGGER IF NOT EXISTS trg_receipt_activation_direct_user BEFORE INSERT ON receipt
WHEN NEW.kind = 'mandate_activation' AND NEW.authority_ref NOT LIKE 'direct_user:%'
BEGIN SELECT RAISE(ABORT, 'invariant 13: activation requires a direct_user authority'); END;
CREATE TRIGGER IF NOT EXISTS trg_receipt_execution_actor BEFORE INSERT ON receipt
WHEN NEW.kind IN ('execution','reconcile')
  AND substr(NEW.authority_ref, 1, instr(NEW.authority_ref, ':') - 1) NOT IN ('venue','system')
BEGIN SELECT RAISE(ABORT, 'invariant 6: execution and reconcile receipts come from a venue or system'); END;

CREATE TABLE IF NOT EXISTS case_set_revision (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  parent_id TEXT REFERENCES case_set_revision(id),
  horizon TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','reviewed','selected','retired')),
  thesis_summary TEXT NOT NULL,
  assumptions_hash TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  selected_at TEXT,
  retired_at TEXT,
  review_receipt_id TEXT REFERENCES receipt(id),
  selection_receipt_id TEXT REFERENCES receipt(id),
  proposed_by TEXT NOT NULL CHECK (
    instr(proposed_by, ':') > 1
    AND substr(proposed_by, 1, instr(proposed_by, ':') - 1)
        IN ('direct_user','reviewer','model','system','venue')
    AND length(trim(substr(proposed_by, instr(proposed_by, ':') + 1))) > 0),
  recorded_at TEXT NOT NULL
);
-- Invariant 4: at most one selected revision per (subject, horizon).
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_selected_revision
  ON case_set_revision(subject_id, horizon) WHERE status = 'selected';
-- A revision is born a draft.
CREATE TRIGGER IF NOT EXISTS trg_revision_insert_draft BEFORE INSERT ON case_set_revision
WHEN NEW.status <> 'draft' OR NEW.review_receipt_id IS NOT NULL OR NEW.selection_receipt_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'invariant 3: a revision is inserted as a draft'); END;
-- Invariant 3: the body of a revision is frozen once written.
CREATE TRIGGER IF NOT EXISTS trg_revision_body_frozen BEFORE UPDATE ON case_set_revision
WHEN NEW.subject_id IS NOT OLD.subject_id OR NEW.parent_id IS NOT OLD.parent_id
  OR NEW.horizon IS NOT OLD.horizon OR NEW.thesis_summary IS NOT OLD.thesis_summary
  OR NEW.assumptions_hash IS NOT OLD.assumptions_hash
  OR NEW.evidence_hash IS NOT OLD.evidence_hash OR NEW.created_at IS NOT OLD.created_at
  OR NEW.proposed_by IS NOT OLD.proposed_by OR NEW.recorded_at IS NOT OLD.recorded_at
  OR (OLD.review_receipt_id IS NOT NULL AND NEW.review_receipt_id IS NOT OLD.review_receipt_id)
  OR (OLD.selection_receipt_id IS NOT NULL AND NEW.selection_receipt_id IS NOT OLD.selection_receipt_id)
BEGIN SELECT RAISE(ABORT, 'invariant 3: revision body and receipt links are frozen; draft a successor'); END;
-- Status only moves forward: draft -> reviewed -> selected -> retired.
CREATE TRIGGER IF NOT EXISTS trg_revision_status_forward BEFORE UPDATE OF status ON case_set_revision
WHEN (CASE NEW.status WHEN 'draft' THEN 0 WHEN 'reviewed' THEN 1 WHEN 'selected' THEN 2 ELSE 3 END)
   < (CASE OLD.status WHEN 'draft' THEN 0 WHEN 'reviewed' THEN 1 WHEN 'selected' THEN 2 ELSE 3 END)
BEGIN SELECT RAISE(ABORT, 'revision status moves forward only'); END;
CREATE TRIGGER IF NOT EXISTS trg_revision_review_needs_receipt BEFORE UPDATE OF status ON case_set_revision
WHEN NEW.status = 'reviewed' AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.review_receipt_id
    AND r.kind = 'review' AND r.object_ref = 'case_set:' || NEW.id
    AND substr(r.authority_ref, 1, instr(r.authority_ref, ':') - 1) IN ('direct_user','reviewer')
    AND lower(trim(substr(r.authority_ref, instr(r.authority_ref, ':') + 1)))
        <> lower(trim(substr(OLD.proposed_by, instr(OLD.proposed_by, ':') + 1))))
BEGIN SELECT RAISE(ABORT, 'invariant 5: review requires a human review receipt from someone other than the author'); END;
-- Invariant 5: 'selected' requires a selection receipt bound to this revision.
CREATE TRIGGER IF NOT EXISTS trg_revision_select_needs_receipt BEFORE UPDATE OF status ON case_set_revision
WHEN NEW.status = 'selected' AND (OLD.status <> 'reviewed' OR NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.selection_receipt_id
    AND r.kind = 'selection' AND r.object_ref = 'case_set:' || NEW.id))
BEGIN SELECT RAISE(ABORT, 'invariant 5: selection requires a reviewed revision and a selection receipt'); END;
CREATE TRIGGER IF NOT EXISTS trg_revision_no_delete BEFORE DELETE ON case_set_revision
BEGIN SELECT RAISE(ABORT, 'revisions are never deleted; retire them'); END;

CREATE TABLE IF NOT EXISTS research_case (
  id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL REFERENCES case_set_revision(id),
  label TEXT NOT NULL,
  probability REAL NOT NULL CHECK (probability >= 0 AND probability <= 1),
  -- Invariant 11: returns declare basis.
  return_basis TEXT NOT NULL CHECK (return_basis IN ('price_only','total_return')),
  price_target REAL,
  expected_return_pct REAL,
  return_anchor_at TEXT,
  assumptions TEXT NOT NULL,
  evidence_for_ids TEXT NOT NULL DEFAULT '[]',
  evidence_against_ids TEXT NOT NULL DEFAULT '[]',
  invalidation_ids TEXT NOT NULL DEFAULT '[]',
  display_order INTEGER NOT NULL DEFAULT 0,
  CHECK (expected_return_pct IS NULL OR return_anchor_at IS NOT NULL)
);
-- Invariant 3: cases are immutable. A changed probability is a new revision.
CREATE TRIGGER IF NOT EXISTS trg_case_no_update BEFORE UPDATE ON research_case
BEGIN SELECT RAISE(ABORT, 'invariant 3: cases are immutable; draft a successor revision'); END;
CREATE TRIGGER IF NOT EXISTS trg_case_no_delete BEFORE DELETE ON research_case
BEGIN SELECT RAISE(ABORT, 'invariant 3: cases are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_case_only_on_draft BEFORE INSERT ON research_case
WHEN (SELECT status FROM case_set_revision WHERE id = NEW.revision_id) <> 'draft'
BEGIN SELECT RAISE(ABORT, 'invariant 3: cases can only be added to a draft revision'); END;

CREATE TABLE IF NOT EXISTS invalidation (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  case_id TEXT REFERENCES research_case(id),
  condition TEXT NOT NULL,
  severity TEXT NOT NULL,
  observable_definition TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','triggered','closed')),
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  evaluated_at TEXT,
  recorded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_invalidation_frozen BEFORE UPDATE ON invalidation
WHEN NEW.subject_id IS NOT OLD.subject_id OR NEW.case_id IS NOT OLD.case_id
  OR NEW.condition IS NOT OLD.condition OR NEW.observable_definition IS NOT OLD.observable_definition
  OR NEW.created_at IS NOT OLD.created_at OR NEW.recorded_at IS NOT OLD.recorded_at
BEGIN SELECT RAISE(ABORT, 'an invalidation condition is frozen; add a new one'); END;
CREATE TRIGGER IF NOT EXISTS trg_invalidation_no_delete BEFORE DELETE ON invalidation
BEGIN SELECT RAISE(ABORT, 'invalidations are never deleted; close them'); END;

CREATE TABLE IF NOT EXISTS research_trigger (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  name TEXT NOT NULL,
  trigger_type TEXT NOT NULL,
  condition TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('green','amber','red','tripped','suppressed')),
  action_type TEXT NOT NULL,
  decision_packet TEXT NOT NULL DEFAULT '{}',
  priority INTEGER NOT NULL DEFAULT 3,
  status TEXT NOT NULL DEFAULT 'active',
  last_evaluated_at TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_trigger_no_delete BEFORE DELETE ON research_trigger
BEGIN SELECT RAISE(ABORT, 'triggers are never deleted; suppress them'); END;

CREATE TABLE IF NOT EXISTS context_packet (
  id TEXT PRIMARY KEY,
  task_ref TEXT NOT NULL,
  as_of TEXT NOT NULL,
  known_as_of TEXT,
  policy_version TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  task TEXT NOT NULL,
  selected TEXT NOT NULL,
  ranking_explanations TEXT NOT NULL,
  omitted TEXT NOT NULL,
  warnings TEXT NOT NULL,
  runtime TEXT NOT NULL,
  packet_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  -- Invariant 17: every packet carries its runtime.
  CHECK (json_extract(runtime, '$.model_id') IS NOT NULL
         AND json_extract(runtime, '$.policy_version') IS NOT NULL
         AND json_type(runtime, '$.fallback_used') IN ('true','false'))
);
-- Invariant 7: packets are frozen so they can be replayed.
CREATE TRIGGER IF NOT EXISTS trg_packet_no_update BEFORE UPDATE ON context_packet
BEGIN SELECT RAISE(ABORT, 'invariant 7: packets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_packet_no_delete BEFORE DELETE ON context_packet
BEGIN SELECT RAISE(ABORT, 'invariant 7: packets are immutable'); END;

-- ---------------------------------------------------------------- decision layer

CREATE TABLE IF NOT EXISTS mandate (
  id TEXT PRIMARY KEY,
  scope_ref TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','active','retired')),
  parent_id TEXT REFERENCES mandate(id),
  body TEXT NOT NULL,
  activated_by_receipt_id TEXT REFERENCES receipt(id),
  created_at TEXT NOT NULL,
  retired_at TEXT,
  UNIQUE (scope_ref, version)
);
-- Invariant 13: one active mandate per scope.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mandate_active ON mandate(scope_ref) WHERE status = 'active';
CREATE TRIGGER IF NOT EXISTS trg_mandate_insert_draft BEFORE INSERT ON mandate
WHEN NEW.status <> 'draft' OR NEW.activated_by_receipt_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'invariant 13: a mandate is inserted as a draft'); END;
CREATE TRIGGER IF NOT EXISTS trg_mandate_body_frozen BEFORE UPDATE ON mandate
WHEN NEW.body IS NOT OLD.body OR NEW.scope_ref IS NOT OLD.scope_ref OR NEW.version IS NOT OLD.version
  OR NEW.parent_id IS NOT OLD.parent_id OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.activated_by_receipt_id IS NOT NULL AND NEW.activated_by_receipt_id IS NOT OLD.activated_by_receipt_id)
BEGIN SELECT RAISE(ABORT, 'mandates are versioned, never edited in place'); END;
CREATE TRIGGER IF NOT EXISTS trg_mandate_status_forward BEFORE UPDATE OF status ON mandate
WHEN NEW.status IS NOT OLD.status AND NOT (
  (OLD.status = 'draft' AND NEW.status = 'active') OR (OLD.status = 'active' AND NEW.status = 'retired'))
BEGIN SELECT RAISE(ABORT, 'invariant 13: mandate status moves draft -> active -> retired'); END;
CREATE TRIGGER IF NOT EXISTS trg_mandate_active_needs_receipt BEFORE UPDATE OF status ON mandate
WHEN NEW.status = 'active' AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.activated_by_receipt_id
    AND r.kind = 'mandate_activation' AND r.object_ref = 'mandate:' || NEW.id
    AND r.authority_ref LIKE 'direct_user:%')
BEGIN SELECT RAISE(ABORT, 'invariant 13: activation requires a direct_user receipt'); END;
CREATE TRIGGER IF NOT EXISTS trg_mandate_no_delete BEFORE DELETE ON mandate
BEGIN SELECT RAISE(ABORT, 'mandates are never deleted; retire them'); END;

CREATE TABLE IF NOT EXISTS position_snapshot (
  id TEXT PRIMARY KEY,
  scope_ref TEXT NOT NULL,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  as_of TEXT NOT NULL,
  source_class TEXT NOT NULL CHECK (source_class IN
    ('broker_fill','broker_position','reconciled','plan','estimate','stale_cache')),
  source_ref TEXT,
  quantity REAL,
  mark_price REAL,
  mark_as_of TEXT,
  mark_source_class TEXT,
  cash_available REAL,
  content_hash TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshot_scope_subject ON position_snapshot(scope_ref, subject_id, as_of);
CREATE TRIGGER IF NOT EXISTS trg_snapshot_no_update BEFORE UPDATE ON position_snapshot
BEGIN SELECT RAISE(ABORT, 'snapshots are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_snapshot_no_delete BEFORE DELETE ON position_snapshot
BEGIN SELECT RAISE(ABORT, 'snapshots are append-only'); END;

CREATE TABLE IF NOT EXISTS decision_record (
  id TEXT PRIMARY KEY,
  scope_ref TEXT NOT NULL,
  subject_id TEXT REFERENCES subject(id),
  decision_kind TEXT NOT NULL CHECK (decision_kind IN ('trade','rebalance','hold','no_trade','close')),
  intent_ref TEXT,
  intent_rung TEXT NOT NULL CHECK (intent_rung IN
    ('quick_take','ticker_brief','portfolio_fit','deep_dive','formal_thesis','action')),
  proposal TEXT NOT NULL,
  rationale_text TEXT NOT NULL CHECK (length(trim(rationale_text)) > 0),
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  selected_revision_id TEXT REFERENCES case_set_revision(id),
  -- Invariant 16: every decision names the mandate it was checked against.
  mandate_id TEXT NOT NULL REFERENCES mandate(id),
  mandate_check TEXT NOT NULL,
  position_snapshot_ids TEXT NOT NULL DEFAULT '[]',
  snapshot_freshness TEXT NOT NULL CHECK (snapshot_freshness IN ('fresh','stale','mixed','none')),
  conviction TEXT,
  change_triggers_text TEXT,
  status TEXT NOT NULL CHECK (status IN
    ('proposed','approved','executed','rejected','expired','superseded')),
  proposed_by TEXT NOT NULL CHECK (
    instr(proposed_by, ':') > 1
    AND substr(proposed_by, 1, instr(proposed_by, ':') - 1)
        IN ('direct_user','reviewer','model','system','venue')
    AND length(trim(substr(proposed_by, instr(proposed_by, ':') + 1))) > 0),
  proposed_at TEXT NOT NULL,
  approval_receipt_id TEXT REFERENCES receipt(id),
  execution_receipt_id TEXT REFERENCES receipt(id),
  reconcile_receipt_id TEXT REFERENCES receipt(id),
  superseded_by_id TEXT REFERENCES decision_record(id),
  idempotency_key TEXT UNIQUE,
  CHECK (json_extract(mandate_check, '$.mandate_id') = mandate_id
         AND json_extract(mandate_check, '$.status') IN ('pass','fail'))
);
CREATE INDEX IF NOT EXISTS ix_decision_scope_kind ON decision_record(scope_ref, decision_kind);
-- A decision is born proposed, against the active mandate of its own scope,
-- citing (if anything) a selected revision.
CREATE TRIGGER IF NOT EXISTS trg_decision_insert BEFORE INSERT ON decision_record
WHEN NEW.status <> 'proposed'
  OR NEW.approval_receipt_id IS NOT NULL OR NEW.execution_receipt_id IS NOT NULL
  OR NEW.reconcile_receipt_id IS NOT NULL OR NEW.superseded_by_id IS NOT NULL
  OR NOT EXISTS (SELECT 1 FROM mandate m WHERE m.id = NEW.mandate_id
                 AND m.status = 'active' AND m.scope_ref = NEW.scope_ref)
  OR (NEW.selected_revision_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM case_set_revision r WHERE r.id = NEW.selected_revision_id AND r.status = 'selected'
          AND (NEW.subject_id IS NULL OR r.subject_id = NEW.subject_id)))
BEGIN SELECT RAISE(ABORT, 'invariant 16: a decision is inserted as proposed, against the active mandate of its scope'); END;
-- Everything except status and the receipt links is frozen; links are write-once.
CREATE TRIGGER IF NOT EXISTS trg_decision_body_frozen BEFORE UPDATE ON decision_record
WHEN NEW.scope_ref IS NOT OLD.scope_ref OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.decision_kind IS NOT OLD.decision_kind OR NEW.intent_ref IS NOT OLD.intent_ref
  OR NEW.intent_rung IS NOT OLD.intent_rung OR NEW.proposal IS NOT OLD.proposal
  OR NEW.rationale_text IS NOT OLD.rationale_text OR NEW.evidence_ids IS NOT OLD.evidence_ids
  OR NEW.selected_revision_id IS NOT OLD.selected_revision_id
  OR NEW.mandate_id IS NOT OLD.mandate_id OR NEW.mandate_check IS NOT OLD.mandate_check
  OR NEW.position_snapshot_ids IS NOT OLD.position_snapshot_ids
  OR NEW.snapshot_freshness IS NOT OLD.snapshot_freshness OR NEW.conviction IS NOT OLD.conviction
  OR NEW.change_triggers_text IS NOT OLD.change_triggers_text
  OR NEW.proposed_by IS NOT OLD.proposed_by OR NEW.proposed_at IS NOT OLD.proposed_at
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR (OLD.approval_receipt_id IS NOT NULL AND NEW.approval_receipt_id IS NOT OLD.approval_receipt_id)
  OR (OLD.execution_receipt_id IS NOT NULL AND NEW.execution_receipt_id IS NOT OLD.execution_receipt_id)
  OR (OLD.reconcile_receipt_id IS NOT NULL AND NEW.reconcile_receipt_id IS NOT OLD.reconcile_receipt_id)
  OR (OLD.superseded_by_id IS NOT NULL AND NEW.superseded_by_id IS NOT OLD.superseded_by_id)
BEGIN SELECT RAISE(ABORT, 'a decision proposal is frozen and its receipt links are write-once'); END;
-- Invariant 15: status moves forward only, one step at a time, by receipt.
CREATE TRIGGER IF NOT EXISTS trg_decision_status_forward BEFORE UPDATE OF status ON decision_record
WHEN NEW.status IS NOT OLD.status AND (
     OLD.status IN ('rejected','expired','superseded','executed')
  OR (OLD.status = 'proposed' AND NEW.status NOT IN ('approved','rejected','expired','superseded'))
  OR (OLD.status = 'approved' AND NEW.status NOT IN ('executed','expired','superseded')))
BEGIN SELECT RAISE(ABORT, 'invariant 15: illegal decision status transition'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_approved_needs_receipt BEFORE UPDATE OF status ON decision_record
WHEN NEW.status = 'approved' AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.approval_receipt_id
    AND r.kind = 'approval' AND r.object_ref = 'decision:' || NEW.id
    AND lower(trim(substr(r.authority_ref, instr(r.authority_ref, ':') + 1)))
        <> lower(trim(substr(OLD.proposed_by, instr(OLD.proposed_by, ':') + 1))))
BEGIN SELECT RAISE(ABORT, 'invariant 15: approval requires an approval receipt from someone other than the proposer'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_closed_needs_receipt BEFORE UPDATE OF status ON decision_record
WHEN NEW.status IN ('rejected','expired','superseded') AND NEW.status IS NOT OLD.status AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.kind = 'review' AND r.object_ref = 'decision:' || NEW.id
    AND json_extract(r.payload, '$.' || NEW.status) = 1)
BEGIN SELECT RAISE(ABORT, 'invariant 15: rejection, expiry and supersession each need a review receipt saying so'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_executed_needs_receipt BEFORE UPDATE OF status ON decision_record
WHEN NEW.status = 'executed' AND (OLD.decision_kind NOT IN ('trade','rebalance','close') OR NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.execution_receipt_id
    AND r.kind = 'execution' AND r.object_ref = 'decision:' || NEW.id))
BEGIN SELECT RAISE(ABORT, 'invariant 15: execution requires an executable decision and an execution receipt'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_reconcile_needs_receipt BEFORE UPDATE OF reconcile_receipt_id ON decision_record
WHEN NEW.reconcile_receipt_id IS NOT NULL AND (OLD.status <> 'executed' OR NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.reconcile_receipt_id
    AND r.kind = 'reconcile' AND r.object_ref = 'decision:' || NEW.id))
BEGIN SELECT RAISE(ABORT, 'invariant 15: reconcile requires an executed decision and a reconcile receipt'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_no_delete BEFORE DELETE ON decision_record
BEGIN SELECT RAISE(ABORT, 'decisions are never deleted'); END;
"""


def _translate(e: sqlite3.Error) -> Exception:
    """Turn SQLite constraint failures into kernel exceptions with the DB message."""
    msg = str(e)
    if isinstance(e, sqlite3.IntegrityError):
        if "ux_one_selected_revision" in msg or "case_set_revision.subject_id, case_set_revision.horizon" in msg:
            return InvariantViolation("invariant 4: a selected revision already exists for this subject and horizon")
        if "ux_mandate_active" in msg or "mandate.scope_ref" in msg:
            return InvariantViolation("invariant 13: scope already has an active mandate; retire it first")
        if "invariant" in msg or "append-only" in msg or "immutable" in msg or "frozen" in msg \
                or "never deleted" in msg or "forward" in msg or "requires" in msg:
            return InvariantViolation(msg)
        if "receipt.prev_chain_hash" in msg or "receipt.chain_hash" in msg:
            return InvariantViolation("invariant 6: receipt chain fork refused")
        if "FOREIGN KEY" in msg:
            return InvariantViolation("referenced object does not exist: " + msg)
        if "CHECK" in msg or "NOT NULL" in msg:
            return InvariantViolation(msg)
        return InvariantViolation(msg)
    return KernelError(msg)


class Store:
    """SQLite-backed kernel store. Use ':memory:' for tests or a file path.

    `clock` returns the current UTC timestamp string; tests and fixtures can
    inject a fixed clock so that ids and hashes are reproducible.

    `authorize`, if given, is called as authorize(kind, object_ref,
    authority_ref, payload) before every receipt is written and must return
    True. This is where an application proves that "direct_user:owner" is
    the owner (a session check, a signature). The kernel itself checks only
    the declared actor kind.

    One Store is one connection. Do not share a Store across threads; open
    one per thread against the same file.
    """

    def __init__(self, path: str = ":memory:", clock: Optional[Callable[[], str]] = None,
                 timeout: float = 5.0, deterministic_ids: bool = False,
                 authorize: Optional[Callable[[str, str, str, dict], bool]] = None):
        self.path = path
        self.clock = clock or utc_now
        self.authorize = authorize
        self._deterministic = deterministic_ids
        self._counter = 0
        self.db = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            try:
                self.db.execute("PRAGMA journal_mode = WAL")
            except sqlite3.Error:
                pass
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def new_id(self, prefix: str) -> str:
        """Random ids by default. With deterministic_ids=True (fixture replay),
        ids are a counter so two replays of one fixture produce identical rows."""
        if not self._deterministic:
            return new_id(prefix)
        self._counter += 1
        return f"{prefix}_{self._counter:06d}"

    # ---------------------------------------------------------------- tx
    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """One write transaction. BEGIN IMMEDIATE takes the write lock up front,
        so two writers serialise instead of both reading a stale state. A nested
        call joins the outer transaction."""
        if self.db.in_transaction:
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.db
        except sqlite3.Error as e:
            self.db.execute("ROLLBACK")
            raise _translate(e) from e
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            try:
                self.db.execute("COMMIT")
            except sqlite3.Error as e:
                self.db.execute("ROLLBACK")
                raise _translate(e) from e

    # ---------------------------------------------------------------- generic io
    def insert(self, rec: Record) -> str:
        if rec._table == "receipt" and self.authorize is not None and not self.authorize(
                rec.kind, rec.object_ref, rec.authority_ref, rec.payload):  # type: ignore[attr-defined]
            raise InvariantViolation(
                f"authorize hook refused {rec.authority_ref!r} for {rec.kind} on {rec.object_ref}")  # type: ignore[attr-defined]
        row = rec.to_row()
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self.transaction():
            self.db.execute(f"INSERT INTO {rec._table} ({cols}) VALUES ({qs})", tuple(row.values()))
        return rec.id  # type: ignore[attr-defined]

    def update(self, table: str, ident: str, **changes: Any) -> None:
        """Column update. The triggers decide whether it is allowed."""
        if not changes:
            return
        cls = TABLE_TO_CLASS[table]
        vals = []
        for k, v in changes.items():
            vals.append(json.dumps(v, sort_keys=True, default=str) if k in cls._json_fields else v)
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self.transaction():
            cur = self.db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*vals, ident))
            if cur.rowcount == 0:
                raise KernelError(f"no row {ident} in {table}")

    def get(self, table: str, ident: str) -> Any:
        row = self.db.execute(f"SELECT * FROM {table} WHERE id = ?", (ident,)).fetchone()
        if row is None:
            raise KernelError(f"no {table} with id {ident}")
        return TABLE_TO_CLASS[table].from_row(row)

    def try_get(self, table: str, ident: str) -> Any:
        try:
            return self.get(table, ident)
        except KernelError:
            return None

    def get_ref(self, ref: str) -> Any:
        kind, ident = split_ref(ref)
        table = REF_PREFIX_TO_TABLE.get(kind)
        if table is None:
            raise KernelError(f"unknown ref kind {kind!r}")
        return self.get(table, ident)

    def query(self, table: str, where: str = "1=1", params: tuple = (), order: str = "") -> list:
        sql = f"SELECT * FROM {table} WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        rows = self.db.execute(sql, params).fetchall()
        cls = TABLE_TO_CLASS[table]
        return [cls.from_row(r) for r in rows]

    def exists(self, table: str, ident: str) -> bool:
        return self.db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (ident,)).fetchone() is not None

    def raw(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    def snapshot_tables(self, *tables: str) -> dict[str, list[tuple]]:
        """Exact row image of the named tables, for 'no silent mutation' checks."""
        out: dict[str, list[tuple]] = {}
        for t in tables:
            rows = self.db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            out[t] = [tuple(r) for r in rows]
        return out
