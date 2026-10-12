"""Underwriting: CaseSetRevision, Case, diff, review and the selection guard.

Drafts revisions with their cases, hashes assumptions and evidence, diffs
two revisions, and moves a revision draft -> reviewed -> selected ->
retired. Anyone, including a model, may draft. Review and selection need a
human authority (direct_user or reviewer), and the reviewer cannot be the
author of the draft. Selection is one transaction that writes the selection
receipt and flips the status; the partial unique index is what makes two
concurrent selections impossible (invariants 3, 4, 5, 11).

Nothing here edits a probability in place; a changed probability is a new
draft. Invalidations and triggers live here because they attach to cases.
"""
from __future__ import annotations

from typing import Any, Optional

from .models import (
    RETURN_BASES,
    TRIGGER_STATES,
    Case,
    CaseSetRevision,
    InvariantViolation,
    Invalidation,
    KernelError,
    Trigger,
    content_hash,
    norm_ts,
)
from . import authority
from .receipts import add_receipt, require_receipt
from .store import Store


def draft_revision(store: Store, subject_id: str, horizon: str, thesis_summary: str,
                   cases: list[dict[str, Any]], parent_id: Optional[str] = None,
                   proposed_by: str = "direct_user:owner", revision_id: Optional[str] = None,
                   created_at: Optional[str] = None) -> str:
    """Create a draft revision with its cases in one transaction.

    Each case dict: label, probability, return_basis, assumptions (list),
    optional price_target, expected_return_pct, return_anchor_at,
    evidence_for_ids, evidence_against_ids, invalidation_ids.
    """
    authority.parse(proposed_by)
    if not cases:
        raise KernelError("a revision needs at least one case")
    total = sum(float(c["probability"]) for c in cases)
    if abs(total - 1.0) > 1e-6:
        raise KernelError(f"case probabilities sum to {total:.4f}, not 1")
    for c in cases:
        if c.get("return_basis") not in RETURN_BASES:
            raise InvariantViolation(
                f"invariant 11: case {c.get('label')!r} must declare return_basis in {RETURN_BASES}")
        if c.get("expected_return_pct") is not None and not c.get("return_anchor_at"):
            raise InvariantViolation(
                f"invariant 11: case {c.get('label')!r} has a return but no return_anchor_at")
        for key in ("evidence_for_ids", "evidence_against_ids"):
            for eid in c.get(key, []) or []:
                if not store.exists("evidence", eid):
                    raise InvariantViolation(f"case {c.get('label')!r} cites missing evidence {eid}")
    if parent_id is not None:
        parent = store.get("case_set_revision", parent_id)
        if parent.subject_id != subject_id or parent.horizon != horizon:
            raise InvariantViolation("a successor revision must share subject and horizon with its parent")

    assumptions_hash = content_hash([
        {"label": c["label"], "probability": c["probability"], "assumptions": c.get("assumptions", []),
         "price_target": c.get("price_target"), "expected_return_pct": c.get("expected_return_pct"),
         "return_basis": c["return_basis"]} for c in cases])
    evidence_hash = content_hash(sorted({
        eid for c in cases for key in ("evidence_for_ids", "evidence_against_ids") for eid in (c.get(key) or [])}))
    rid = revision_id or store.new_id("rev")
    rev = CaseSetRevision(
        id=rid, subject_id=subject_id, parent_id=parent_id, horizon=horizon, status="draft",
        thesis_summary=thesis_summary, assumptions_hash=assumptions_hash, evidence_hash=evidence_hash,
        created_at=norm_ts(created_at) if created_at else store.clock(), proposed_by=proposed_by,  # type: ignore[arg-type]
        recorded_at=store.clock(),
    )
    with store.transaction():
        store.insert(rev)
        for i, c in enumerate(cases):
            store.insert(Case(
                id=store.new_id("case"), revision_id=rid, label=c["label"], probability=float(c["probability"]),
                return_basis=c["return_basis"], price_target=c.get("price_target"),
                expected_return_pct=c.get("expected_return_pct"),
                return_anchor_at=norm_ts(c.get("return_anchor_at")),
                assumptions=list(c.get("assumptions", [])),
                evidence_for_ids=list(c.get("evidence_for_ids", []) or []),
                evidence_against_ids=list(c.get("evidence_against_ids", []) or []),
                invalidation_ids=list(c.get("invalidation_ids", []) or []),
                display_order=i,
            ))
    return rid


def get_revision(store: Store, revision_id: str) -> CaseSetRevision:
    return store.get("case_set_revision", revision_id)


def cases_of(store: Store, revision_id: str) -> list[Case]:
    return store.query("research_case", "revision_id = ?", (revision_id,), "display_order")


def selected_revision(store: Store, subject_id: str, horizon: str) -> Optional[CaseSetRevision]:
    rows = store.query("case_set_revision", "subject_id = ? AND horizon = ? AND status = 'selected'",
                       (subject_id, horizon))
    if len(rows) > 1:  # cannot happen with the index; belt and braces
        raise InvariantViolation("invariant 4 violated in storage")
    return rows[0] if rows else None


def review_revision(store: Store, revision_id: str, authority_ref: str, notes: str = "") -> str:
    rev = get_revision(store, revision_id)
    if rev.status != "draft":
        raise InvariantViolation(f"only a draft can be reviewed; this one is {rev.status}")
    authority.require_human(authority_ref, "invariant 5: review")
    authority.require_distinct(authority_ref, rev.proposed_by, "invariant 5: review")
    with store.transaction():
        rcpt = add_receipt(store, "review", f"case_set:{revision_id}", authority_ref, {"notes": notes})
        store.update("case_set_revision", revision_id, status="reviewed", review_receipt_id=rcpt)
    return rcpt


def select_revision(store: Store, revision_id: str, authority_ref: str,
                    retire_current: bool = True, notes: str = "") -> str:
    """Invariants 4 and 5 in one transaction.

    Writes a selection receipt naming the authority and the reviewed
    revision, retires the currently selected revision for the same
    (subject, horizon) if asked, and flips status to 'selected'. The
    transaction starts with BEGIN IMMEDIATE so a second caller waits, then
    sees the first selection and fails on the partial unique index.
    """
    rev = get_revision(store, revision_id)
    if rev.status == "draft":
        raise InvariantViolation("invariant 5: a draft must be reviewed before it can be selected")
    if rev.status != "reviewed":
        raise InvariantViolation(f"cannot select a revision in status {rev.status}")
    authority.require_human(authority_ref, "invariant 5: selection")
    with store.transaction(immediate=True):
        current = selected_revision(store, rev.subject_id, rev.horizon)
        if current is not None:
            if not retire_current:
                raise InvariantViolation(
                    "invariant 4: a selected revision already exists for this subject and horizon")
            store.update("case_set_revision", current.id, status="retired", retired_at=store.clock())
        rcpt = add_receipt(store, "selection", f"case_set:{revision_id}", authority_ref,
                           {"reviewed_revision": revision_id, "review_receipt_id": rev.review_receipt_id,
                            "assumptions_hash": rev.assumptions_hash, "evidence_hash": rev.evidence_hash,
                            "replaces": current.id if current else None, "notes": notes})
        store.update("case_set_revision", revision_id, status="selected",
                     selected_at=store.clock(), selection_receipt_id=rcpt)
    return rcpt


def retire_revision(store: Store, revision_id: str, authority_ref: str, reason: str = "") -> str:
    rev = get_revision(store, revision_id)
    if rev.status == "retired":
        raise InvariantViolation("already retired")
    authority.require_human(authority_ref, "retiring a revision")
    with store.transaction():
        rcpt = add_receipt(store, "review", f"case_set:{revision_id}", authority_ref,
                           {"retired": True, "reason": reason})
        store.update("case_set_revision", revision_id, status="retired", retired_at=store.clock())
    return rcpt


def diff_revisions(store: Store, old_id: str, new_id_: str) -> dict:
    """Case-by-case diff keyed on label. Pure read."""
    old = {c.label: c for c in cases_of(store, old_id)}
    new = {c.label: c for c in cases_of(store, new_id_)}
    out: dict[str, Any] = {"old": old_id, "new": new_id_, "changed": [], "added": [], "removed": []}
    for label in sorted(set(old) | set(new)):
        if label in old and label in new:
            o, n = old[label], new[label]
            delta: dict[str, Any] = {}
            for f in ("probability", "price_target", "expected_return_pct", "return_basis", "assumptions"):
                if getattr(o, f) != getattr(n, f):
                    delta[f] = {"from": getattr(o, f), "to": getattr(n, f)}
            if delta:
                out["changed"].append({"label": label, **delta})
        elif label in new:
            out["added"].append(label)
        else:
            out["removed"].append(label)
    return out


def assert_selection_is_proven(store: Store, revision_id: str) -> None:
    """SPEC section 8: 'this is the active underwriting' needs the revision in
    status selected AND a matching selection receipt."""
    rev = get_revision(store, revision_id)
    if rev.status != "selected" or not rev.selection_receipt_id:
        raise InvariantViolation("not the active underwriting: no selection")
    require_receipt(store, rev.selection_receipt_id, "selection", f"case_set:{revision_id}")


# ------------------------------------------------------------- invalidations


def add_invalidation(store: Store, subject_id: str, condition: str, observable_definition: str,
                     severity: str = "material", case_id: Optional[str] = None,
                     evidence_ids: Optional[list[str]] = None, created_at: Optional[str] = None) -> str:
    rec = Invalidation(
        id=store.new_id("inv"), subject_id=subject_id, case_id=case_id, condition=condition,
        severity=severity, observable_definition=observable_definition, status="active",
        evidence_ids=list(evidence_ids or []),
        created_at=norm_ts(created_at) if created_at else store.clock(), evaluated_at=None,  # type: ignore[arg-type]
        recorded_at=store.clock(),
    )
    return store.insert(rec)


def link_evidence_against(store: Store, invalidation_id: str, evidence_id: str) -> None:
    """Attach evidence to an invalidation (SPEC 6.1 step 3). Changes no probability."""
    inv = store.get("invalidation", invalidation_id)
    if not store.exists("evidence", evidence_id):
        raise InvariantViolation("evidence does not exist")
    ids = list(inv.evidence_ids)
    if evidence_id not in ids:
        ids.append(evidence_id)
    store.update("invalidation", invalidation_id, evidence_ids=ids, evaluated_at=store.clock(),
                 status="triggered")


def active_invalidations(store: Store, subject_id: str) -> list[Invalidation]:
    return store.query("invalidation", "subject_id = ? AND status <> 'closed'", (subject_id,), "created_at")


# ------------------------------------------------------------------ triggers


def add_trigger(store: Store, subject_id: str, name: str, condition: str, trigger_type: str = "event",
                action_type: str = "review", priority: int = 3, decision_packet: Optional[dict] = None) -> str:
    rec = Trigger(
        id=store.new_id("trg"), subject_id=subject_id, name=name, trigger_type=trigger_type,
        condition=condition, state="green", action_type=action_type,
        decision_packet=dict(decision_packet or {}), priority=priority, status="active",
        last_evaluated_at=None, created_at=store.clock(), recorded_at=store.clock(),
    )
    return store.insert(rec)


def set_trigger_state(store: Store, trigger_id: str, state: str) -> None:
    """A trigger calls for review. It does not authorise a trade."""
    if state not in TRIGGER_STATES:
        raise KernelError(f"unknown trigger state {state!r}")
    now = store.clock()
    with store.transaction():
        store.db.execute("INSERT INTO trigger_state_event (id, trigger_id, state, occurred_at) VALUES (?,?,?,?)",
                         (store.new_id("tse"), trigger_id, state, now))
        store.update("research_trigger", trigger_id, state=state, last_evaluated_at=now)


def trigger_state_at(store: Store, trigger_id: str, known: Optional[str]) -> str:
    """The state a trigger had at recording time `known`; current state if None."""
    if known is None:
        return store.get("research_trigger", trigger_id).state
    rows = store.raw("SELECT state FROM trigger_state_event WHERE trigger_id = ? AND occurred_at <= ? "
                     "ORDER BY rowid DESC LIMIT 1", (trigger_id, known))
    return rows[0][0] if rows else "green"


def active_triggers(store: Store, subject_id: str) -> list[Trigger]:
    return store.query("research_trigger", "subject_id = ? AND status = 'active'", (subject_id,), "priority")
