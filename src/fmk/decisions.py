"""Decision layer: Mandate, PositionSnapshot, DecisionRecord.

What this module does: versions and activates mandates (invariant 13),
appends position snapshots and answers current-holdings questions without
ever treating a plan as a position (invariant 14), computes and stores a
mandate check on every proposal (invariant 16), moves decisions forward by
receipt only (invariant 15), and lists declined decisions so that
restraint is visible later.

What this module does not do: it does not size a trade, classify intent,
set an action bar, or block an order. A failed mandate check is a stored
record; the venue or wallet still has to refuse. intent_rung is stored as
the caller gave it, never inferred here.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from .models import (
    DECISION_KINDS,
    DECISION_STATUS_ORDER,
    INTENT_RUNGS,
    POSITION_GRADE,
    SNAPSHOT_FRESHNESS,
    SOURCE_CLASS_RANK,
    TERMINAL_DECISION_STATUSES,
    DecisionRecord,
    InvariantViolation,
    KernelError,
    Mandate,
    MandateBody,
    PositionSnapshot,
    content_hash,
    new_id,
    norm_ts,
)
from .policy import mark_staleness_warning, snapshot_age_warning
from .receipts import add_receipt, require_receipt
from .store import Store

# ------------------------------------------------------------------ mandate


def propose_mandate(store: Store, scope_ref: str, body: MandateBody | dict,
                    parent_id: Optional[str] = None) -> str:
    body_d = asdict(body) if isinstance(body, MandateBody) else dict(body)
    MandateBody(**body_d)  # validates field names
    with store.transaction(immediate=True):
        ver = store.raw("SELECT COALESCE(MAX(version),0)+1 FROM mandate WHERE scope_ref=?", (scope_ref,))[0][0]
        rec = Mandate(id=store.new_id("mnd"), scope_ref=scope_ref, version=int(ver), status="draft",
                      parent_id=parent_id, body=body_d, activated_by_receipt_id=None,
                      created_at=store.clock(), retired_at=None)
        store.insert(rec)
    return rec.id


def activate_mandate(store: Store, mandate_id: str, authority_ref: str) -> str:
    """Invariant 13: direct_user receipt, and only one active per scope.

    The second condition is a partial unique index; a second activation for
    the same scope fails inside the transaction and the receipt is rolled
    back with it.
    """
    m = store.get("mandate", mandate_id)
    if m.status != "draft":
        raise InvariantViolation(f"only a draft mandate can be activated; this one is {m.status}")
    if not authority_ref.startswith("direct_user"):
        raise InvariantViolation("invariant 13: mandate activation requires direct_user authority")
    with store.transaction(immediate=True):
        rcpt = add_receipt(store, "mandate_activation", f"mandate:{mandate_id}", authority_ref,
                           {"version": m.version, "scope_ref": m.scope_ref})
        store.update("mandate", mandate_id, status="active", activated_by_receipt_id=rcpt)
    return rcpt


def retire_mandate(store: Store, mandate_id: str, authority_ref: str = "direct_user:owner") -> str:
    m = store.get("mandate", mandate_id)
    if m.status != "active":
        raise InvariantViolation("only an active mandate can be retired")
    with store.transaction():
        rcpt = add_receipt(store, "review", f"mandate:{mandate_id}", authority_ref, {"retired": True})
        store.update("mandate", mandate_id, status="retired", retired_at=store.clock())
    return rcpt


def active_mandate(store: Store, scope_ref: str) -> Optional[Mandate]:
    rows = store.query("mandate", "scope_ref = ? AND status = 'active'", (scope_ref,))
    return rows[0] if rows else None


def check_against_mandate(proposal: dict, mandate: Mandate, nav: float,
                          current_subject_exposure_pct: float = 0.0,
                          venue: Optional[str] = None, asset_class: Optional[str] = None,
                          subject_id: Optional[str] = None) -> dict:
    """Pure function. Names breaches against one mandate version. Blocks nothing."""
    b = mandate.body
    breaches: list[str] = []
    notional = float(proposal.get("notional") or 0.0)
    if nav > 0 and notional:
        pct = 100.0 * notional / nav
        if pct > b["max_position_pct"]:
            breaches.append(f"position {pct:.1f}% > max_position_pct {b['max_position_pct']}%")
        if current_subject_exposure_pct + pct > b["max_subject_exposure_pct"]:
            breaches.append(f"subject exposure {current_subject_exposure_pct + pct:.1f}% > "
                            f"max_subject_exposure_pct {b['max_subject_exposure_pct']}%")
    if b.get("per_trade_notional_cap") and notional > b["per_trade_notional_cap"]:
        breaches.append(f"notional {notional} > per_trade_notional_cap {b['per_trade_notional_cap']}")
    if venue and b["allowed_venues"] and venue not in b["allowed_venues"]:
        breaches.append(f"venue {venue} not in allowed_venues")
    if asset_class and b["allowed_asset_classes"] and asset_class not in b["allowed_asset_classes"]:
        breaches.append(f"asset_class {asset_class} not allowed")
    if subject_id and subject_id in b["blocked_subjects"]:
        breaches.append(f"subject {subject_id} is blocked")
    return {"status": "fail" if breaches else "pass", "breaches": breaches,
            "mandate_id": mandate.id, "mandate_version": mandate.version}


# ---------------------------------------------------------- position snapshot


def add_snapshot(store: Store, scope_ref: str, subject_id: str, as_of: str, source_class: str,
                 quantity: Optional[float] = None, mark_price: Optional[float] = None,
                 mark_as_of: Optional[str] = None, mark_source_class: Optional[str] = None,
                 cash_available: Optional[float] = None, source_ref: Optional[str] = None,
                 cost_basis: Optional[float] = None, pending_order_refs: Optional[list] = None) -> str:
    if source_class not in SOURCE_CLASS_RANK:
        raise KernelError(f"unknown source_class {source_class!r}")
    body = dict(scope_ref=scope_ref, subject_id=subject_id, as_of=norm_ts(as_of), source_class=source_class,
                quantity=quantity, mark_price=mark_price, mark_as_of=norm_ts(mark_as_of),
                mark_source_class=mark_source_class, cash_available=cash_available,
                source_ref=source_ref, cost_basis=cost_basis, pending_order_refs=pending_order_refs or [])
    rec = PositionSnapshot(id=store.new_id("pos"), content_hash=content_hash(body), ingested_at=store.clock(), **body)
    return store.insert(rec)


def current_position(store: Store, scope_ref: str, subject_id: str, now: Optional[str] = None,
                     mandate: Optional[Mandate] = None) -> dict:
    """Invariant 14: a plan is never a position.

    Picks the highest-authority snapshot (ties broken by newest as_of). If
    that snapshot is not position-grade, quantity is withheld and the answer
    is 'unverified' with a warning. Position-grade answers still carry
    warnings for age (per mandate policy), for a mark older than the
    position, and for older plans that an execution has overtaken.
    """
    rows = store.query("position_snapshot", "scope_ref = ? AND subject_id = ?", (scope_ref, subject_id), "as_of DESC")
    now_s = norm_ts(now) if now else store.clock()
    if not rows:
        return {"status": "unknown", "quantity": None, "warnings": ["no snapshot"], "snapshot_id": None}
    best = max(rows, key=lambda r: (SOURCE_CLASS_RANK[r.source_class], r.as_of))
    out: dict[str, Any] = {"snapshot_id": best.id, "source_class": best.source_class, "as_of": best.as_of,
                           "mark_price": best.mark_price, "mark_as_of": best.mark_as_of}
    if best.source_class not in POSITION_GRADE:
        out.update(status="unverified", quantity=None, warnings=[
            f"best available snapshot is '{best.source_class}'; not position-grade. quantity withheld."])
        return out
    warnings: list[str] = []
    out.update(status="ok", quantity=best.quantity)
    if mandate is not None:
        w = snapshot_age_warning(best.source_class, best.as_of, now_s, mandate.body.get("freshness_seconds", {}))  # type: ignore[arg-type]
        if w:
            warnings.append(w)
    policy = (mandate.body if mandate is not None else {}).get("freshness_seconds", {})
    w = mark_staleness_warning(best.mark_as_of, best.as_of, best.mark_source_class, policy)
    if w:
        warnings.append(w)
    older_plans = [r for r in rows if r.source_class == "plan" and r.as_of < best.as_of]
    if older_plans:
        warnings.append(f"{len(older_plans)} older plan snapshot(s) superseded by execution; do not report as pending")
    out["warnings"] = warnings
    return out


def snapshot_freshness(positions: list[dict]) -> str:
    """fresh if no warnings anywhere, stale if every position warns, else mixed."""
    if not positions:
        return "fresh"
    flags = [bool(p.get("warnings")) or p.get("status") != "ok" for p in positions]
    if not any(flags):
        return "fresh"
    if all(flags):
        return "stale"
    return "mixed"


# ------------------------------------------------------------------ decisions


def propose_decision(store: Store, scope_ref: str, decision_kind: str, intent_rung: str, proposal: dict,
                     rationale_text: str, mandate_check: dict, subject_id: Optional[str] = None,
                     evidence_ids: Optional[list] = None, selected_revision_id: Optional[str] = None,
                     position_snapshot_ids: Optional[list] = None, snapshot_freshness: str = "fresh",
                     conviction: Optional[str] = None, change_triggers_text: Optional[str] = None,
                     proposed_by: str = "model", intent_ref: Optional[str] = None) -> str:
    if decision_kind not in DECISION_KINDS:
        raise KernelError(f"unknown decision_kind {decision_kind!r}")
    if intent_rung not in INTENT_RUNGS:
        raise KernelError(f"unknown intent_rung {intent_rung!r}")
    if snapshot_freshness not in SNAPSHOT_FRESHNESS:
        raise KernelError(f"unknown snapshot_freshness {snapshot_freshness!r}")
    if not rationale_text or not rationale_text.strip():
        raise InvariantViolation("a decision, including no_trade, requires a rationale")
    if "mandate_id" not in mandate_check or "status" not in mandate_check:
        raise InvariantViolation("invariant 16: mandate_check must name mandate_id and status")
    if not store.exists("mandate", mandate_check["mandate_id"]):
        raise InvariantViolation("invariant 16: mandate_check names a mandate that does not exist")
    for eid in evidence_ids or []:
        if not store.exists("evidence", eid):
            raise InvariantViolation(f"decision cites missing evidence {eid}")
    rec = DecisionRecord(
        id=store.new_id("dec"), scope_ref=scope_ref, subject_id=subject_id, decision_kind=decision_kind,
        intent_ref=intent_ref, intent_rung=intent_rung, proposal=dict(proposal), rationale_text=rationale_text,
        evidence_ids=list(evidence_ids or []), selected_revision_id=selected_revision_id,
        mandate_id=mandate_check["mandate_id"], mandate_check=dict(mandate_check),
        position_snapshot_ids=list(position_snapshot_ids or []), snapshot_freshness=snapshot_freshness,
        conviction=conviction, change_triggers_text=change_triggers_text, status="proposed",
        proposed_by=proposed_by, proposed_at=store.clock(),
    )
    return store.insert(rec)


def get_decision(store: Store, decision_id: str) -> DecisionRecord:
    return store.get("decision_record", decision_id)


def _advance(store: Store, decision_id: str, to_status: str, receipt_id: str, column: str) -> None:
    """Invariant 15: forward only, one step, by a receipt bound to this decision."""
    d = get_decision(store, decision_id)
    if d.status in TERMINAL_DECISION_STATUSES:
        raise InvariantViolation(f"decision is {d.status}; terminal")
    ci, ti = DECISION_STATUS_ORDER.index(d.status), DECISION_STATUS_ORDER.index(to_status)
    if ti != ci + 1:
        raise InvariantViolation(f"invariant 15: illegal transition {d.status} -> {to_status}")
    kind = {"approved": "approval", "executed": "execution"}[to_status]
    r = store.try_get("receipt", receipt_id)
    if r is None or r.object_ref != f"decision:{decision_id}":
        raise InvariantViolation("receipt missing or not bound to this decision")
    require_receipt(store, receipt_id, kind, f"decision:{decision_id}")
    store.update("decision_record", decision_id, status=to_status, **{column: receipt_id})


def approve_decision(store: Store, decision_id: str, authority_ref: str,
                     override: Optional[dict] = None) -> str:
    """Invariant 16: a failed mandate check can only be approved with an
    override payload that names every breach."""
    d = get_decision(store, decision_id)
    if d.status != "proposed":
        raise InvariantViolation(f"invariant 15: cannot approve from {d.status}")
    if d.mandate_check["status"] == "fail":
        breaches = set(d.mandate_check["breaches"])
        overridden = set((override or {}).get("overrides_breaches", []))
        if not breaches.issubset(overridden):
            raise InvariantViolation(
                "mandate_check failed; approval requires override naming every breach: "
                + "; ".join(sorted(breaches - overridden)))
    with store.transaction():
        rcpt = add_receipt(store, "approval", f"decision:{decision_id}", authority_ref,
                           {"override": override or None, "mandate_check": d.mandate_check})
        _advance(store, decision_id, "approved", rcpt, "approval_receipt_id")
    return rcpt


def record_execution(store: Store, decision_id: str, authority_ref: str, fill: dict) -> str:
    d = get_decision(store, decision_id)
    if d.status != "approved":
        raise InvariantViolation(f"invariant 15: cannot execute from {d.status}; no approval receipt found")
    with store.transaction():
        rcpt = add_receipt(store, "execution", f"decision:{decision_id}", authority_ref, {"fill": dict(fill)},
                           prior_receipt_id=d.approval_receipt_id)
        _advance(store, decision_id, "executed", rcpt, "execution_receipt_id")
    return rcpt


def reconcile(store: Store, decision_id: str, authority_ref: str, snapshot_id: str) -> str:
    d = get_decision(store, decision_id)
    if d.status != "executed":
        raise InvariantViolation("reconcile requires an executed decision")
    snap = store.get("position_snapshot", snapshot_id)
    if snap.source_class != "reconciled":
        raise InvariantViolation("reconcile must bind a snapshot with source_class 'reconciled'")
    with store.transaction():
        rcpt = add_receipt(store, "reconcile", f"decision:{decision_id}", authority_ref,
                           {"snapshot_id": snapshot_id}, prior_receipt_id=d.execution_receipt_id)
        store.update("decision_record", decision_id, reconcile_receipt_id=rcpt)
    return rcpt


def reject_decision(store: Store, decision_id: str, authority_ref: str, reason: str) -> str:
    d = get_decision(store, decision_id)
    if d.status != "proposed":
        raise InvariantViolation(f"cannot reject from {d.status}")
    with store.transaction():
        rcpt = add_receipt(store, "review", f"decision:{decision_id}", authority_ref,
                           {"rejected": True, "reason": reason})
        store.update("decision_record", decision_id, status="rejected")
    return rcpt


def declined_decisions(store: Store, scope_ref: str) -> list[DecisionRecord]:
    """Restraint report: every hold / no_trade with its rationale. Outcomes are
    attached later by whoever evaluates; the kernel only guarantees these exist."""
    return store.query("decision_record", "scope_ref = ? AND decision_kind IN ('hold','no_trade')",
                       (scope_ref,), "proposed_at, rowid")


def attach_outcome(store: Store, decision_id: str, outcome: dict, authority_ref: str = "system:evaluator") -> str:
    """An outcome is a review receipt on the decision. The decision row itself
    does not change; what happened afterwards is evidence, not a status."""
    return add_receipt(store, "review", f"decision:{decision_id}", authority_ref, {"outcome": dict(outcome)})


def decision_chain(store: Store, decision_id: str) -> list[dict]:
    """Every receipt bound to a decision, in time order. The reconstructable chain."""
    rows = store.query("receipt", "object_ref = ?", (f"decision:{decision_id}",), "occurred_at, rowid")
    return [{"id": r.id, "kind": r.kind, "authority": r.authority_ref, "at": r.occurred_at} for r in rows]
