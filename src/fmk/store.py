"""SQLite store: one schema for the base kernel and the decision layer.

What this module does: owns the connection, creates the schema, and
enforces the constraints that can be stated in SQL (foreign keys, enum
CHECKs, partial unique indexes, immutability triggers, the selection and
decision-status triggers). It exposes small generic insert/get/query
helpers that the domain modules build on.

What this module does not do: it carries no research logic, no ranking
and no policy. It never decides what a claim means or which revision
should be selected; it only refuses writes that would break an invariant.

Constraint map (spec numbering):
  1  claim.evidence_id NOT NULL + FK                 -> schema
  2  evidence immutable                              -> trigger
  3  research_case immutable; revision bodies frozen -> triggers
  4  one selected revision per (subject, horizon)    -> partial unique index
  5  selection requires selection receipt            -> trigger
  6  receipt kinds are distinct values; immutable    -> schema + trigger
  7  packets immutable, carry hash                   -> trigger + column
  13 one active mandate per scope                    -> partial unique index
  15 decision status forward only                    -> trigger
  17 packet runtime present                          -> CHECK on json_extract
The remaining invariants are enforced in the domain modules.
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
  source_strength TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('active','stale','superseded','contradicted','retracted','unresolved')),
  valid_from TEXT,
  valid_to TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_subject_aspect ON claim(subject_id, aspect);
-- Only status may change on a claim; the proposition itself is append-only.
CREATE TRIGGER IF NOT EXISTS trg_claim_body_frozen BEFORE UPDATE ON claim
WHEN NEW.subject_id IS NOT OLD.subject_id OR NEW.evidence_id IS NOT OLD.evidence_id
  OR NEW.aspect IS NOT OLD.aspect OR NEW.kind IS NOT OLD.kind
  OR NEW.statement IS NOT OLD.statement OR NEW.value_json IS NOT OLD.value_json
  OR NEW.confidence IS NOT OLD.confidence OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'claim body is append-only; add a new claim and a relation'); END;
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
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipt (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('review','selection','approval','apply','execution','delivery',
     'mandate_activation','reconcile','health_check')),
  object_ref TEXT NOT NULL,
  authority_ref TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  prior_receipt_id TEXT REFERENCES receipt(id)
);
CREATE INDEX IF NOT EXISTS ix_receipt_object ON receipt(object_ref, kind);
CREATE TRIGGER IF NOT EXISTS trg_receipt_no_update BEFORE UPDATE ON receipt
BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_receipt_no_delete BEFORE DELETE ON receipt
BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;

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
  proposed_by TEXT NOT NULL DEFAULT 'human'
);
-- Invariant 4: at most one selected revision per (subject, horizon).
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_selected_revision
  ON case_set_revision(subject_id, horizon) WHERE status = 'selected';
-- Invariant 3: the body of a revision is frozen once written.
CREATE TRIGGER IF NOT EXISTS trg_revision_body_frozen BEFORE UPDATE ON case_set_revision
WHEN NEW.subject_id IS NOT OLD.subject_id OR NEW.parent_id IS NOT OLD.parent_id
  OR NEW.horizon IS NOT OLD.horizon OR NEW.thesis_summary IS NOT OLD.thesis_summary
  OR NEW.assumptions_hash IS NOT OLD.assumptions_hash
  OR NEW.evidence_hash IS NOT OLD.evidence_hash OR NEW.created_at IS NOT OLD.created_at
  OR NEW.proposed_by IS NOT OLD.proposed_by
BEGIN SELECT RAISE(ABORT, 'invariant 3: revision body is frozen; draft a successor'); END;
-- Status only moves forward: draft -> reviewed -> selected -> retired.
CREATE TRIGGER IF NOT EXISTS trg_revision_status_forward BEFORE UPDATE OF status ON case_set_revision
WHEN (CASE NEW.status WHEN 'draft' THEN 0 WHEN 'reviewed' THEN 1 WHEN 'selected' THEN 2 ELSE 3 END)
   < (CASE OLD.status WHEN 'draft' THEN 0 WHEN 'reviewed' THEN 1 WHEN 'selected' THEN 2 ELSE 3 END)
BEGIN SELECT RAISE(ABORT, 'revision status moves forward only'); END;
-- Invariant 5: 'selected' requires a selection receipt bound to this revision.
CREATE TRIGGER IF NOT EXISTS trg_revision_select_needs_receipt BEFORE UPDATE OF status ON case_set_revision
WHEN NEW.status = 'selected' AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.selection_receipt_id
    AND r.kind = 'selection' AND r.object_ref = 'case_set:' || NEW.id)
BEGIN SELECT RAISE(ABORT, 'invariant 5: selection requires a selection receipt'); END;
CREATE TRIGGER IF NOT EXISTS trg_revision_insert_select_needs_receipt BEFORE INSERT ON case_set_revision
WHEN NEW.status = 'selected' AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.selection_receipt_id
    AND r.kind = 'selection' AND r.object_ref = 'case_set:' || NEW.id)
BEGIN SELECT RAISE(ABORT, 'invariant 5: selection requires a selection receipt'); END;
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
-- Cases can only be added while the revision is a draft.
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
  status TEXT NOT NULL,
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  evaluated_at TEXT
);

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

CREATE TABLE IF NOT EXISTS synthesis (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  as_of TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  claim_ids TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  selected_revision_id TEXT REFERENCES case_set_revision(id),
  summary TEXT NOT NULL,
  open_question_ids TEXT NOT NULL,
  warning_ids TEXT NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_synthesis_no_update BEFORE UPDATE ON synthesis
BEGIN SELECT RAISE(ABORT, 'a synthesis is a snapshot; write a new one'); END;

CREATE TABLE IF NOT EXISTS context_packet (
  id TEXT PRIMARY KEY,
  task_ref TEXT NOT NULL,
  as_of TEXT NOT NULL,
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
CREATE TRIGGER IF NOT EXISTS trg_mandate_body_frozen BEFORE UPDATE ON mandate
WHEN NEW.body IS NOT OLD.body OR NEW.scope_ref IS NOT OLD.scope_ref OR NEW.version IS NOT OLD.version
BEGIN SELECT RAISE(ABORT, 'mandates are versioned, never edited in place'); END;
CREATE TRIGGER IF NOT EXISTS trg_mandate_active_needs_receipt BEFORE UPDATE OF status ON mandate
WHEN NEW.status = 'active' AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.activated_by_receipt_id
    AND r.kind = 'mandate_activation' AND r.object_ref = 'mandate:' || NEW.id
    AND r.authority_ref LIKE 'direct_user%')
BEGIN SELECT RAISE(ABORT, 'invariant 13: activation requires a direct_user receipt'); END;

CREATE TABLE IF NOT EXISTS position_snapshot (
  id TEXT PRIMARY KEY,
  scope_ref TEXT NOT NULL,
  subject_id TEXT NOT NULL REFERENCES subject(id),
  as_of TEXT NOT NULL,
  source_class TEXT NOT NULL CHECK (source_class IN
    ('broker_fill','broker_position','reconciled','plan','estimate','stale_cache')),
  source_ref TEXT,
  quantity REAL,
  cost_basis REAL,
  mark_price REAL,
  mark_as_of TEXT,
  mark_source_class TEXT,
  cash_available REAL,
  pending_order_refs TEXT NOT NULL DEFAULT '[]',
  content_hash TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshot_scope_subject ON position_snapshot(scope_ref, subject_id, as_of);
CREATE TRIGGER IF NOT EXISTS trg_snapshot_no_update BEFORE UPDATE ON position_snapshot
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
  snapshot_freshness TEXT NOT NULL CHECK (snapshot_freshness IN ('fresh','stale','mixed')),
  conviction TEXT,
  change_triggers_text TEXT,
  status TEXT NOT NULL CHECK (status IN
    ('proposed','approved','executed','rejected','expired','superseded')),
  proposed_by TEXT NOT NULL,
  proposed_at TEXT NOT NULL,
  approval_receipt_id TEXT REFERENCES receipt(id),
  execution_receipt_id TEXT REFERENCES receipt(id),
  reconcile_receipt_id TEXT REFERENCES receipt(id),
  superseded_by_id TEXT REFERENCES decision_record(id),
  CHECK (json_extract(mandate_check, '$.mandate_id') IS NOT NULL
         AND json_extract(mandate_check, '$.status') IN ('pass','fail','warn'))
);
CREATE INDEX IF NOT EXISTS ix_decision_scope_kind ON decision_record(scope_ref, decision_kind);
-- The proposal itself is frozen; only status and receipt links move.
CREATE TRIGGER IF NOT EXISTS trg_decision_body_frozen BEFORE UPDATE ON decision_record
WHEN NEW.proposal IS NOT OLD.proposal OR NEW.rationale_text IS NOT OLD.rationale_text
  OR NEW.mandate_id IS NOT OLD.mandate_id OR NEW.mandate_check IS NOT OLD.mandate_check
  OR NEW.decision_kind IS NOT OLD.decision_kind OR NEW.intent_rung IS NOT OLD.intent_rung
BEGIN SELECT RAISE(ABORT, 'a decision proposal is frozen; propose a successor'); END;
-- Invariant 15: status moves forward only, one step at a time, by receipt.
CREATE TRIGGER IF NOT EXISTS trg_decision_status_forward BEFORE UPDATE OF status ON decision_record
WHEN OLD.status IN ('rejected','expired','superseded')
  OR (OLD.status = 'proposed' AND NEW.status NOT IN ('approved','rejected','expired','superseded'))
  OR (OLD.status = 'approved' AND NEW.status NOT IN ('executed','expired','superseded'))
  OR (OLD.status = 'executed' AND NEW.status <> 'executed')
BEGIN SELECT RAISE(ABORT, 'invariant 15: illegal decision status transition'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_approved_needs_receipt BEFORE UPDATE OF status ON decision_record
WHEN NEW.status = 'approved' AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.approval_receipt_id
    AND r.kind = 'approval' AND r.object_ref = 'decision:' || NEW.id)
BEGIN SELECT RAISE(ABORT, 'invariant 15: approval requires an approval receipt'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_executed_needs_receipt BEFORE UPDATE OF status ON decision_record
WHEN NEW.status = 'executed' AND NOT EXISTS (
  SELECT 1 FROM receipt r WHERE r.id = NEW.execution_receipt_id
    AND r.kind = 'execution' AND r.object_ref = 'decision:' || NEW.id)
BEGIN SELECT RAISE(ABORT, 'invariant 15: execution requires an execution receipt'); END;
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
    """

    def __init__(self, path: str = ":memory:", clock: Optional[Callable[[], str]] = None,
                 timeout: float = 5.0, deterministic_ids: bool = False):
        self.path = path
        self.clock = clock or utc_now
        self._deterministic = deterministic_ids
        self._counter = 0
        self.db = sqlite3.connect(path, timeout=timeout, isolation_level=None,
                                  check_same_thread=False)
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
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """One write transaction. `immediate` takes the write lock up front so
        two writers serialise instead of both reading a stale state."""
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
