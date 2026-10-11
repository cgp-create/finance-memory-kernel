"""Types, enums, errors and small helpers shared by every fmk module.

What this module does: defines the vocabulary of the kernel (the fifteen
record types, their enums, the ordering of authority tiers and position
source classes), the two exception classes, and helpers for ids, UTC
timestamps and content hashing.

What this module does not do: it holds no persistence and no business
rules. Constraints live in store.py (schema) and in the domain modules.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class KernelError(Exception):
    """Base error for the kernel. Bad arguments, unknown ids, unknown enums."""


class InvariantViolation(KernelError):
    """Raised when an operation would break one of invariants 1 to 17."""


# --------------------------------------------------------------------------
# enums (kept as tuples and dicts; names may change, separations must not)
# --------------------------------------------------------------------------

AUTHORITY_RANK: dict[str, int] = {
    "direct_user": 6,
    "primary": 5,
    "system_measurement": 4,
    "credible_secondary": 3,
    "assistant_inference": 2,
    "social_chatter": 1,
}
AUTHORITY_TIERS = tuple(AUTHORITY_RANK)

CLAIM_KINDS = ("fact", "forecast", "hypothesis", "inference", "correction", "user_decision")
CLAIM_STATUSES = ("active", "stale", "superseded", "contradicted", "retracted", "unresolved")
CLAIM_RELATIONS = ("supports", "contradicts", "supersedes", "depends_on")
CASE_SET_STATUSES = ("draft", "reviewed", "selected", "retired")
TRIGGER_STATES = ("green", "amber", "red", "tripped", "suppressed")
RETURN_BASES = ("price_only", "total_return")
QUESTION_STATUSES = ("open", "answered", "dropped")

RECEIPT_KINDS = (
    "review",
    "selection",
    "approval",
    "apply",
    "execution",
    "delivery",
    "mandate_activation",
    "reconcile",
    "health_check",
)

# decision layer
SOURCE_CLASS_RANK: dict[str, int] = {
    "reconciled": 5,
    "broker_position": 4,
    "broker_fill": 3,
    "plan": 2,
    "estimate": 1,
    "stale_cache": 0,
}
POSITION_GRADE = frozenset({"reconciled", "broker_position", "broker_fill"})
MANDATE_STATUSES = ("draft", "active", "retired")
DECISION_KINDS = ("trade", "rebalance", "hold", "no_trade", "close")
INTENT_RUNGS = ("quick_take", "ticker_brief", "portfolio_fit", "deep_dive", "formal_thesis", "action")
DECISION_STATUS_ORDER = ("proposed", "approved", "executed")
TERMINAL_DECISION_STATUSES = frozenset({"rejected", "expired", "superseded"})
DECISION_STATUSES = DECISION_STATUS_ORDER + tuple(sorted(TERMINAL_DECISION_STATUSES))
SNAPSHOT_FRESHNESS = ("fresh", "stale", "mixed")

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(value: str) -> datetime:
    """Parse an ISO 8601 timestamp. Naive values are treated as UTC."""
    if not isinstance(value, str) or not value:
        raise KernelError(f"bad timestamp {value!r}")
    v = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError as e:
            raise KernelError(f"bad timestamp {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def norm_ts(value: Optional[str]) -> Optional[str]:
    """Normalise to 'YYYY-MM-DDTHH:MM:SSZ' so that string order equals time order."""
    if value is None:
        return None
    return parse_ts(value).strftime(_TS_FMT)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def content_hash(obj: Any) -> str:
    """Stable sha256 over canonical JSON. Used for evidence, packets, receipts."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def split_ref(ref: str) -> tuple[str, str]:
    """'claim:abc' -> ('claim', 'abc')."""
    if ":" not in ref:
        raise KernelError(f"bad object ref {ref!r}")
    kind, _, ident = ref.partition(":")
    return kind, ident


# --------------------------------------------------------------------------
# record dataclasses
# --------------------------------------------------------------------------


@dataclass
class Record:
    """Base for rows. Subclasses list which columns hold JSON text."""

    _json_fields: ClassVar[tuple[str, ...]] = ()
    _table: ClassVar[str] = ""

    @classmethod
    def from_row(cls, row: Any) -> "Record":
        d = dict(row)
        for f in cls._json_fields:
            if f in d and isinstance(d[f], str):
                d[f] = json.loads(d[f])
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_row(self) -> dict[str, Any]:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        for f in self._json_fields:
            d[f] = json.dumps(d[f], sort_keys=True, default=str)
        return d


@dataclass
class Subject(Record):
    _table: ClassVar[str] = "subject"
    id: str
    namespace: str
    key: str
    type: str
    title: str
    status: str
    created_at: str
    updated_at: str


@dataclass
class Evidence(Record):
    _table: ClassVar[str] = "evidence"
    _json_fields: ClassVar[tuple[str, ...]] = ("payload",)
    id: str
    subject_id: str
    source_kind: str
    source_ref: str
    observed_at: str
    ingested_at: str
    authority_tier: str
    content_hash: str
    payload: dict
    supersedes_evidence_id: Optional[str] = None


@dataclass
class Claim(Record):
    _table: ClassVar[str] = "claim"
    _json_fields: ClassVar[tuple[str, ...]] = ("value_json",)
    id: str
    subject_id: str
    evidence_id: str
    aspect: str
    kind: str
    statement: str
    value_json: Any
    confidence: float
    source_strength: str
    status: str
    valid_from: Optional[str]
    valid_to: Optional[str]
    created_at: str
    expires_at: Optional[str]


@dataclass
class ClaimRelation(Record):
    _table: ClassVar[str] = "claim_relation"
    id: str
    from_claim_id: str
    relation: str
    to_claim_id: str
    rationale_evidence_id: Optional[str]
    created_at: str


@dataclass
class ResearchQuestion(Record):
    _table: ClassVar[str] = "research_question"
    _json_fields: ClassVar[tuple[str, ...]] = ("answer_claim_ids",)
    id: str
    subject_id: str
    question: str
    evidence_needed: str
    priority: int
    owner: str
    status: str
    revisit_at: Optional[str]
    answer_claim_ids: list
    created_at: str
    updated_at: str


@dataclass
class CaseSetRevision(Record):
    _table: ClassVar[str] = "case_set_revision"
    id: str
    subject_id: str
    parent_id: Optional[str]
    horizon: str
    status: str
    thesis_summary: str
    assumptions_hash: str
    evidence_hash: str
    created_at: str
    selected_at: Optional[str] = None
    retired_at: Optional[str] = None
    review_receipt_id: Optional[str] = None
    selection_receipt_id: Optional[str] = None
    proposed_by: str = "human"


@dataclass
class Case(Record):
    _table: ClassVar[str] = "research_case"
    _json_fields: ClassVar[tuple[str, ...]] = (
        "assumptions",
        "evidence_for_ids",
        "evidence_against_ids",
        "invalidation_ids",
    )
    id: str
    revision_id: str
    label: str
    probability: float
    return_basis: str
    price_target: Optional[float]
    expected_return_pct: Optional[float]
    return_anchor_at: Optional[str]
    assumptions: list
    evidence_for_ids: list
    evidence_against_ids: list
    invalidation_ids: list
    display_order: int


@dataclass
class Invalidation(Record):
    _table: ClassVar[str] = "invalidation"
    _json_fields: ClassVar[tuple[str, ...]] = ("evidence_ids",)
    id: str
    subject_id: str
    case_id: Optional[str]
    condition: str
    severity: str
    observable_definition: str
    status: str
    evidence_ids: list
    created_at: str
    evaluated_at: Optional[str]


@dataclass
class Trigger(Record):
    _table: ClassVar[str] = "research_trigger"
    _json_fields: ClassVar[tuple[str, ...]] = ("decision_packet",)
    id: str
    subject_id: str
    name: str
    trigger_type: str
    condition: str
    state: str
    action_type: str
    decision_packet: dict
    priority: int
    status: str
    last_evaluated_at: Optional[str]


@dataclass
class Synthesis(Record):
    _table: ClassVar[str] = "synthesis"
    _json_fields: ClassVar[tuple[str, ...]] = (
        "claim_ids",
        "evidence_ids",
        "open_question_ids",
        "warning_ids",
    )
    id: str
    subject_id: str
    as_of: str
    policy_version: str
    claim_ids: list
    evidence_ids: list
    selected_revision_id: Optional[str]
    summary: str
    open_question_ids: list
    warning_ids: list
    content_hash: str


@dataclass
class ContextPacket(Record):
    _table: ClassVar[str] = "context_packet"
    _json_fields: ClassVar[tuple[str, ...]] = (
        "task",
        "selected",
        "ranking_explanations",
        "omitted",
        "warnings",
        "runtime",
    )
    id: str
    task_ref: str
    as_of: str
    policy_version: str
    query_hash: str
    task: dict
    selected: list
    ranking_explanations: list
    omitted: list
    warnings: list
    runtime: dict
    packet_hash: str
    created_at: str


@dataclass
class Receipt(Record):
    _table: ClassVar[str] = "receipt"
    _json_fields: ClassVar[tuple[str, ...]] = ("payload",)
    id: str
    kind: str
    object_ref: str
    authority_ref: str
    occurred_at: str
    payload: dict
    payload_hash: str
    prior_receipt_id: Optional[str]


# ---- decision layer -------------------------------------------------------


@dataclass
class MandateBody:
    """The constraint fields of a Mandate. Stored as one JSON document."""

    objective_text: str
    horizon: str
    allowed_asset_classes: list = field(default_factory=list)
    allowed_venues: list = field(default_factory=list)
    blocked_subjects: list = field(default_factory=list)
    max_position_pct: float = 100.0
    max_subject_exposure_pct: float = 100.0
    max_sector_exposure_pct: float = 100.0
    max_gross_exposure_pct: float = 100.0
    min_cash_pct: float = 0.0
    per_trade_notional_cap: Optional[float] = None
    daily_notional_cap: Optional[float] = None
    requires_approval: str = "always"  # always | above_notional | never
    approval_notional_threshold: Optional[float] = None
    funding_rule_text: str = ""
    # freshness policy in seconds by snapshot source class; 0 means never fresh
    freshness_seconds: dict = field(
        default_factory=lambda: {
            "reconciled": 86400,
            "broker_position": 3600,
            "broker_fill": 3600,
            "plan": 0,
            "estimate": 900,
            "stale_cache": 0,
            "mark": 3600,
        }
    )


@dataclass
class Mandate(Record):
    _table: ClassVar[str] = "mandate"
    _json_fields: ClassVar[tuple[str, ...]] = ("body",)
    id: str
    scope_ref: str
    version: int
    status: str
    parent_id: Optional[str]
    body: dict
    activated_by_receipt_id: Optional[str]
    created_at: str
    retired_at: Optional[str]


@dataclass
class PositionSnapshot(Record):
    _table: ClassVar[str] = "position_snapshot"
    _json_fields: ClassVar[tuple[str, ...]] = ("pending_order_refs",)
    id: str
    scope_ref: str
    subject_id: str
    as_of: str
    source_class: str
    source_ref: Optional[str]
    quantity: Optional[float]
    cost_basis: Optional[float]
    mark_price: Optional[float]
    mark_as_of: Optional[str]
    mark_source_class: Optional[str]
    cash_available: Optional[float]
    pending_order_refs: list
    content_hash: str
    ingested_at: str


@dataclass
class DecisionRecord(Record):
    _table: ClassVar[str] = "decision_record"
    _json_fields: ClassVar[tuple[str, ...]] = (
        "proposal",
        "evidence_ids",
        "mandate_check",
        "position_snapshot_ids",
    )
    id: str
    scope_ref: str
    subject_id: Optional[str]
    decision_kind: str
    intent_ref: Optional[str]
    intent_rung: str
    proposal: dict
    rationale_text: str
    evidence_ids: list
    selected_revision_id: Optional[str]
    mandate_id: str
    mandate_check: dict
    position_snapshot_ids: list
    snapshot_freshness: str
    conviction: Optional[str]
    change_triggers_text: Optional[str]
    status: str
    proposed_by: str
    proposed_at: str
    approval_receipt_id: Optional[str] = None
    execution_receipt_id: Optional[str] = None
    reconcile_receipt_id: Optional[str] = None
    superseded_by_id: Optional[str] = None


REF_PREFIX_TO_TABLE: dict[str, str] = {
    "subject": "subject",
    "evidence": "evidence",
    "claim": "claim",
    "relation": "claim_relation",
    "question": "research_question",
    "case_set": "case_set_revision",
    "case": "research_case",
    "invalidation": "invalidation",
    "trigger": "research_trigger",
    "synthesis": "synthesis",
    "packet": "context_packet",
    "receipt": "receipt",
    "mandate": "mandate",
    "position": "position_snapshot",
    "decision": "decision_record",
}

TABLE_TO_CLASS: dict[str, type] = {
    "subject": Subject,
    "evidence": Evidence,
    "claim": Claim,
    "claim_relation": ClaimRelation,
    "research_question": ResearchQuestion,
    "case_set_revision": CaseSetRevision,
    "research_case": Case,
    "invalidation": Invalidation,
    "research_trigger": Trigger,
    "synthesis": Synthesis,
    "context_packet": ContextPacket,
    "receipt": Receipt,
    "mandate": Mandate,
    "position_snapshot": PositionSnapshot,
    "decision_record": DecisionRecord,
}
