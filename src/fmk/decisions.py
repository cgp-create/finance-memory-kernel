"""Decision layer: Mandate, PositionSnapshot, DecisionRecord.

Versions and activates mandates (invariant 13); appends position snapshots
and answers "what do we hold" without treating a plan as a position
(invariant 14); computes the mandate check itself when a decision is
proposed, against the active mandate of that scope (invariant 16); and
moves decisions forward only by receipt, with the approver distinct from
the proposer (invariant 15). Executions record the expected price next to
the fill, so slippage is measured per trade. Declined decisions are
queryable so that restraint can be graded later.

Two inputs to the check come from the caller and are recorded as such in
the check: NAV and the subject's current exposure. The kernel does not
value a portfolio. It does not size trades, classify intent, or block
orders: a failed check is a record that needs a named override to approve,
and the venue or wallet still has to refuse an order on its own.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Optional

from . import authority
from .models import (
    DECISION_KINDS,
    EXECUTABLE_DECISION_KINDS,
    INTENT_RUNGS,
    POSITION_GRADE,
    SOURCE_CLASS_RANK,
    DecisionRecord,
    InvariantViolation,
    KernelError,
    Mandate,
    MandateBody,
    PositionSnapshot,
    content_hash,
    norm_ts,
    parse_ts,
)
from .policy import mark_staleness_warning, snapshot_age_warning
from .receipts import add_receipt
from .store import Store

INCREASING = frozenset({"buy", "short"})
DECREASING = frozenset({"sell", "cover", "reduce", "close"})
BUY_SIDE = frozenset({"buy", "cover"})
SELL_SIDE = frozenset({"sell", "short"})

# ------------------------------------------------------------------ mandate


def propose_mandate(store: Store, scope_ref: str, body: MandateBody | dict,
                    parent_id: Optional[str] = None) -> str:
    body_d = asdict(body) if isinstance(body, MandateBody) else dict(body)
    MandateBody(**body_d)  # validates field names
    with store.transaction():
        ver = store.raw("SELECT COALESCE(MAX(version),0)+1 FROM mandate WHERE scope_ref=?", (scope_ref,))[0][0]
        rec = Mandate(id=store.new_id("mnd"), scope_ref=scope_ref, version=int(ver), status="draft",
                      parent_id=parent_id, body=body_d, activated_by_receipt_id=None,
                      created_at=store.clock(), retired_at=None)
        store.insert(rec)
    return rec.id


def activate_mandate(store: Store, mandate_id: str, authority_ref: str) -> str:
    """Invariant 13: a direct_user receipt, and only one active mandate per scope.
    A second activation for the same scope fails on the partial unique index
    and its receipt rolls back with it."""
    authority.require(authority_ref, ("direct_user",), "invariant 13: mandate activation")
    m = store.get("mandate", mandate_id)
    if m.status != "draft":
        raise InvariantViolation(f"only a draft mandate can be activated; this one is {m.status}")
    with store.transaction():
        rcpt = add_receipt(store, "mandate_activation", f"mandate:{mandate_id}", authority_ref,
                           {"version": m.version, "scope_ref": m.scope_ref, "body_hash": content_hash(m.body)})
        store.update("mandate", mandate_id, status="active", activated_by_receipt_id=rcpt)
    return rcpt


def retire_mandate(store: Store, mandate_id: str, authority_ref: str) -> str:
    authority.require(authority_ref, ("direct_user",), "retiring a mandate")
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


def check_against_mandate(decision_kind: str, proposal: dict, mandate: Mandate, *,
                          nav: Optional[float] = None, current_subject_exposure_pct: float = 0.0,
                          asset_class: Optional[str] = None, subject_id: Optional[str] = None,
                          day_notional_before: float = 0.0, snapshot_freshness: str = "none",
                          held_quantity: Optional[float] = None, held_value: Optional[float] = None,
                          pending_notional: float = 0.0) -> dict:
    """Pure function: name every breach of one mandate version. Blocks nothing.

    The result also lists which mandate limits this function does not check,
    so a 'pass' is never read as covering more than it did.
    """
    b = mandate.body
    out: dict[str, Any] = {"mandate_id": mandate.id, "mandate_version": mandate.version,
                           "inputs": {"nav": nav, "current_subject_exposure_pct": current_subject_exposure_pct,
                                      "asset_class": asset_class, "day_notional_before": day_notional_before,
                                      "snapshot_freshness": snapshot_freshness,
                                      "held_quantity": held_quantity, "held_value": held_value,
                                      "pending_notional": pending_notional}}
    not_checked = [k for k, default in (("max_sector_exposure_pct", 100.0), ("max_gross_exposure_pct", 100.0),
                                        ("min_cash_pct", 0.0)) if b.get(k, default) != default]
    out["not_checked"] = not_checked
    if decision_kind not in EXECUTABLE_DECISION_KINDS:
        out.update(status="pass", breaches=[], note="no change in exposure; limits not applicable")
        return out

    breaches: list[str] = []
    direction = proposal.get("direction")
    if direction not in INCREASING | DECREASING:
        breaches.append(f"direction {direction!r} not one of {sorted(INCREASING | DECREASING)}")
    increasing = direction in INCREASING
    notional = proposal.get("notional")
    if not isinstance(notional, (int, float)) or notional <= 0:
        breaches.append("notional missing or not positive")
        notional = 0.0
    notional = float(notional)

    if increasing:
        if not nav or nav <= 0:
            breaches.append("nav not supplied; position limits cannot be checked")
        else:
            pct = 100.0 * notional / nav
            pending_pct = 100.0 * pending_notional / nav
            if b.get("max_order_pct") is not None and pct > b["max_order_pct"]:
                breaches.append(f"order {pct:.1f}% of NAV > max_order_pct {b['max_order_pct']}%")
            if held_value is None:
                breaches.append("current position not established: cite a position-grade snapshot with a mark "
                                "(quantity 0 for a new position)")
            else:
                held_pct = 100.0 * held_value / nav
                post = held_pct + pending_pct + pct
                if post > b["max_position_pct"]:
                    breaches.append(f"post-trade position {post:.1f}% (held {held_pct:.1f}% + pending "
                                    f"{pending_pct:.1f}% + order {pct:.1f}%) > max_position_pct {b['max_position_pct']}%")
            subj = current_subject_exposure_pct + pending_pct + pct
            if subj > b["max_subject_exposure_pct"]:
                breaches.append(f"subject exposure {subj:.1f}% > "
                                f"max_subject_exposure_pct {b['max_subject_exposure_pct']}%")
        if subject_id and subject_id in b["blocked_subjects"]:
            breaches.append(f"subject {subject_id} is blocked")
    elif direction == "cover":
        if held_quantity is None or held_quantity >= 0:
            breaches.append("cover without a cited short position")
    elif direction in DECREASING:
        if held_quantity is None or held_quantity <= 0:
            breaches.append("sell without a cited position-grade holding; it would open a short")
        elif held_value is not None and notional > held_value * 1.001:
            breaches.append(f"sell notional {notional:g} exceeds holding value {held_value:g}; the excess opens a short")
    if b.get("per_trade_notional_cap") and notional > b["per_trade_notional_cap"]:
        breaches.append(f"notional {notional:g} > per_trade_notional_cap {b['per_trade_notional_cap']:g}")
    if b.get("daily_notional_cap") and day_notional_before + notional > b["daily_notional_cap"]:
        breaches.append(f"day notional {day_notional_before + notional:g} > daily_notional_cap {b['daily_notional_cap']:g}")
    venue = proposal.get("venue")
    if b["allowed_venues"] and venue not in b["allowed_venues"]:
        breaches.append(f"venue {venue!r} not in allowed_venues")
    if b["allowed_asset_classes"] and asset_class not in b["allowed_asset_classes"]:
        breaches.append(f"asset_class {asset_class!r} not in allowed_asset_classes")
    if b.get("min_net_edge_bps") is not None:
        edge, cost = proposal.get("expected_edge_bps"), proposal.get("estimated_cost_bps")
        if edge is None or cost is None:
            breaches.append("expected_edge_bps and estimated_cost_bps not stated")
        elif edge - cost < b["min_net_edge_bps"]:
            breaches.append(f"net edge {edge - cost:g}bps < min_net_edge_bps {b['min_net_edge_bps']:g}")
    if snapshot_freshness in ("stale", "mixed"):
        breaches.append(f"position snapshots are {snapshot_freshness}")
    out.update(status="fail" if breaches else "pass", breaches=breaches)
    return out


# ---------------------------------------------------------- position snapshot


def add_snapshot(store: Store, scope_ref: str, subject_id: str, as_of: str, source_class: str,
                 quantity: Optional[float] = None, mark_price: Optional[float] = None,
                 mark_as_of: Optional[str] = None, mark_source_class: Optional[str] = None,
                 cash_available: Optional[float] = None, source_ref: Optional[str] = None) -> str:
    if source_class not in SOURCE_CLASS_RANK:
        raise KernelError(f"unknown source_class {source_class!r}")
    body = dict(scope_ref=scope_ref, subject_id=subject_id, as_of=norm_ts(as_of), source_class=source_class,
                quantity=quantity, mark_price=mark_price, mark_as_of=norm_ts(mark_as_of),
                mark_source_class=mark_source_class, cash_available=cash_available, source_ref=source_ref)
    rec = PositionSnapshot(id=store.new_id("pos"), content_hash=content_hash(body), ingested_at=store.clock(), **body)
    return store.insert(rec)


def _is_fresh(snap: PositionSnapshot, now: str, policy: dict) -> bool:
    limit = policy.get(snap.source_class)
    if limit is None:
        return True
    return (parse_ts(now) - parse_ts(snap.as_of)).total_seconds() <= limit


def current_position(store: Store, scope_ref: str, subject_id: str, now: Optional[str] = None,
                     mandate: Optional[Mandate] = None) -> dict:
    """Invariant 14: a plan is never a position.

    Only snapshots at or before `now` count. Among position-grade snapshots,
    fresh ones (per the mandate's freshness policy) win over stale ones; then
    the higher source class; then the newer. If nothing position-grade
    exists, the quantity is withheld. Warnings cover age, a stale mark, a
    newer position-grade snapshot that disagrees, and plans overtaken by
    execution.
    """
    now_s = norm_ts(now) if now else store.clock()
    rows = store.query("position_snapshot", "scope_ref = ? AND subject_id = ? AND as_of <= ?",
                       (scope_ref, subject_id, now_s), "as_of DESC")
    if not rows:
        return {"status": "unknown", "quantity": None, "warnings": ["no snapshot"], "snapshot_id": None}
    policy = mandate.body.get("freshness_seconds", {}) if mandate is not None else {}
    grade = [r for r in rows if r.source_class in POSITION_GRADE]
    if not grade:
        best = max(rows, key=lambda r: (SOURCE_CLASS_RANK[r.source_class], r.as_of))
        return {"snapshot_id": best.id, "source_class": best.source_class, "as_of": best.as_of,
                "mark_price": best.mark_price, "mark_as_of": best.mark_as_of, "status": "unverified",
                "quantity": None,
                "warnings": [f"best available snapshot is '{best.source_class}'; not position-grade. quantity withheld."]}
    best = max(grade, key=lambda r: (_is_fresh(r, now_s, policy), SOURCE_CLASS_RANK[r.source_class], r.as_of))
    warnings: list[str] = []
    if mandate is not None:
        w = snapshot_age_warning(best.source_class, best.as_of, now_s, policy)
        if w:
            warnings.append(w)
    w = mark_staleness_warning(best.mark_as_of, best.as_of, best.mark_source_class, policy)
    if w:
        warnings.append(w)
    for r in grade:
        if r.as_of > best.as_of and r.quantity != best.quantity:
            warnings.append(f"newer {r.source_class} snapshot at {r.as_of} reports quantity {r.quantity}, "
                            f"not {best.quantity}; reconcile before acting")
    overtaken = [r for r in rows if r.source_class == "plan" and r.as_of < best.as_of]
    if overtaken:
        warnings.append(f"{len(overtaken)} older plan snapshot(s) overtaken by execution; do not report as pending")
    return {"snapshot_id": best.id, "source_class": best.source_class, "as_of": best.as_of,
            "mark_price": best.mark_price, "mark_as_of": best.mark_as_of, "status": "ok",
            "quantity": best.quantity, "warnings": warnings}


def _snapshot_freshness(store: Store, scope_ref: str, subject_id: Optional[str], snapshot_ids: list[str],
                        now: str, mandate: Mandate) -> str:
    if not snapshot_ids:
        return "none"
    policy = mandate.body.get("freshness_seconds", {})
    flags = []
    for sid in snapshot_ids:
        snap = store.try_get("position_snapshot", sid)
        if snap is None:
            raise InvariantViolation(f"decision cites missing snapshot {sid}")
        if snap.scope_ref != scope_ref or (subject_id and snap.subject_id != subject_id):
            raise InvariantViolation(f"snapshot {sid} belongs to another scope or subject")
        flags.append(snap.source_class in POSITION_GRADE and snap.as_of <= now and _is_fresh(snap, now, policy))
    if all(flags):
        return "fresh"
    return "stale" if not any(flags) else "mixed"


# ------------------------------------------------------------------ decisions


def _held(store: Store, snapshot_ids: list[str], now: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Quantity, absolute value and as_of of the best cited position-grade snapshot."""
    grade = [store.get("position_snapshot", sid) for sid in snapshot_ids]
    grade = [g for g in grade if g.source_class in POSITION_GRADE and g.as_of <= now]
    if not grade:
        return None, None, None
    best = max(grade, key=lambda g: (SOURCE_CLASS_RANK[g.source_class], g.as_of))
    q = best.quantity
    if q is None:
        return None, None, best.as_of
    if q == 0:
        return q, 0.0, best.as_of
    return q, (abs(q * best.mark_price) if best.mark_price else None), best.as_of


def _pending_notional(store: Store, scope_ref: str, subject_id: Optional[str], since: Optional[str],
                      exclude: str = "") -> float:
    """Exposure-increasing commitments the cited snapshot cannot show yet:
    decisions approved and not executed, plus executions after the snapshot.
    Pending sells are not netted off; until they fill they reduce nothing."""
    if subject_id is None:
        return 0.0
    rows = store.raw(
        "SELECT d.proposal, d.status, r.occurred_at FROM decision_record d "
        "LEFT JOIN receipt r ON r.id = d.execution_receipt_id "
        "WHERE d.scope_ref = ? AND d.subject_id = ? AND d.status IN ('approved','executed') AND d.id <> ?",
        (scope_ref, subject_id, exclude))
    total = 0.0
    for proposal, status, executed_at in rows:
        p = json.loads(proposal)
        if p.get("direction") not in INCREASING:
            continue
        if status == "approved" or since is None or (executed_at and executed_at > since):
            total += float(p.get("notional") or 0)
    return total


def propose_decision(store: Store, scope_ref: str, decision_kind: str, intent_rung: str, proposal: dict,
                     rationale_text: str, *, proposed_by: str = "model:assistant",
                     subject_id: Optional[str] = None, nav: Optional[float] = None,
                     current_subject_exposure_pct: float = 0.0, asset_class: Optional[str] = None,
                     evidence_ids: Optional[list] = None, selected_revision_id: Optional[str] = None,
                     position_snapshot_ids: Optional[list] = None, conviction: Optional[str] = None,
                     change_triggers_text: Optional[str] = None, intent_ref: Optional[str] = None,
                     idempotency_key: Optional[str] = None) -> str:
    """Record a proposed decision and the kernel's own check of it against the
    active mandate for scope_ref. The caller cannot supply the check.

    With an idempotency_key, a retry of the same proposal returns the
    existing decision instead of creating a second one."""
    authority.parse(proposed_by)
    if idempotency_key is not None:
        prior = store.query("decision_record", "idempotency_key = ?", (idempotency_key,))
        if prior:
            if prior[0].proposal != dict(proposal) or prior[0].scope_ref != scope_ref:
                raise InvariantViolation("idempotency_key reused for a different proposal")
            return prior[0].id
    if decision_kind not in DECISION_KINDS:
        raise KernelError(f"unknown decision_kind {decision_kind!r}")
    if intent_rung not in INTENT_RUNGS:
        raise KernelError(f"unknown intent_rung {intent_rung!r}")
    if not rationale_text or not rationale_text.strip():
        raise InvariantViolation("a decision, including no_trade, requires a rationale")
    mandate = active_mandate(store, scope_ref)
    if mandate is None:
        raise InvariantViolation(f"invariant 16: scope {scope_ref} has no active mandate")
    for eid in evidence_ids or []:
        if not store.exists("evidence", eid):
            raise InvariantViolation(f"decision cites missing evidence {eid}")
    if selected_revision_id is not None:
        rev = store.try_get("case_set_revision", selected_revision_id)
        if rev is None or rev.status != "selected" or (subject_id and rev.subject_id != subject_id):
            raise InvariantViolation("a decision can only cite the selected revision for its subject")
    now = store.clock()
    snaps = list(position_snapshot_ids or [])
    freshness = _snapshot_freshness(store, scope_ref, subject_id, snaps, now, mandate)
    held_q, held_v, held_as_of = _held(store, snaps, now)
    pending = _pending_notional(store, scope_ref, subject_id, held_as_of)
    day_before = _day_notional(store, scope_ref, now[:10])
    check = check_against_mandate(decision_kind, proposal, mandate, nav=nav,
                                  current_subject_exposure_pct=current_subject_exposure_pct,
                                  asset_class=asset_class, subject_id=subject_id,
                                  day_notional_before=day_before, snapshot_freshness=freshness,
                                  held_quantity=held_q, held_value=held_v, pending_notional=pending)
    check["inputs_supplied_by"] = proposed_by
    check["kernel_computed"] = ["held_quantity", "held_value", "pending_notional", "snapshot_freshness",
                                "day_notional_before"]
    rec = DecisionRecord(
        id=store.new_id("dec"), scope_ref=scope_ref, subject_id=subject_id, decision_kind=decision_kind,
        intent_ref=intent_ref, intent_rung=intent_rung, proposal=dict(proposal), rationale_text=rationale_text,
        evidence_ids=list(evidence_ids or []), selected_revision_id=selected_revision_id,
        mandate_id=mandate.id, mandate_check=check, position_snapshot_ids=snaps, snapshot_freshness=freshness,
        conviction=conviction, change_triggers_text=change_triggers_text, status="proposed",
        proposed_by=proposed_by, proposed_at=now, idempotency_key=idempotency_key,
    )
    return store.insert(rec)


def get_decision(store: Store, decision_id: str) -> DecisionRecord:
    return store.get("decision_record", decision_id)


def _day_notional(store: Store, scope_ref: str, day: str, exclude: str = "") -> float:
    """Notional of decisions in this scope approved on this UTC date (and not since expired)."""
    rows = store.raw(
        "SELECT d.proposal FROM decision_record d JOIN receipt r ON r.id = d.approval_receipt_id "
        "WHERE d.scope_ref = ? AND d.status IN ('approved','executed') AND substr(r.occurred_at,1,10) = ? "
        "AND d.id <> ?", (scope_ref, day, exclude))
    return sum(float(json.loads(r[0]).get("notional") or 0) for r in rows)


def approve_decision(store: Store, decision_id: str, authority_ref: str,
                     override: Optional[dict] = None, nav: Optional[float] = None) -> str:
    """A human other than the proposer approves.

    Approval is a new calculation against the state now, inside the write
    lock. For a trade it refuses, and asks for a new proposal, when the
    proposal is older than the mandate's proposal_ttl_seconds, when a newer
    position-grade snapshot of the subject has arrived since the ones cited,
    or when a NAV restated at approval differs from the proposal's by more
    than nav_tolerance_pct. It then rechecks the mandate with freshness,
    pending commitments and the day's approved notional as of now, so a set
    of proposals that each passed cannot together breach a cap. A failed
    check needs an override with a reason that names every breach. NAV is
    still the caller's number; the receipt records which NAV was used."""
    authority.require_human(authority_ref, "invariant 15: approval")
    d = get_decision(store, decision_id)
    authority.require_distinct(authority_ref, d.proposed_by, "invariant 15: approval")
    if d.status != "proposed":
        raise InvariantViolation(f"invariant 15: cannot approve from {d.status}")
    with store.transaction():
        m = active_mandate(store, d.scope_ref)
        if m is None or m.id != d.mandate_id:
            raise InvariantViolation("the mandate this decision was checked against is no longer active; propose again")
        i = d.mandate_check.get("inputs", {})
        now = store.clock()
        basis: dict[str, Any] = {"nav_proposed": i.get("nav"), "nav_restated": nav,
                                 "snapshot_ids": list(d.position_snapshot_ids)}
        use_nav = i.get("nav")
        freshness, held_q, held_v, pending = (d.snapshot_freshness, i.get("held_quantity"),
                                              i.get("held_value"), i.get("pending_notional", 0.0))
        if d.decision_kind in EXECUTABLE_DECISION_KINDS:
            ttl = m.body.get("proposal_ttl_seconds")
            age = (parse_ts(now) - parse_ts(d.proposed_at)).total_seconds()
            if ttl is not None and age > ttl:
                raise InvariantViolation(f"proposal is {age:.0f}s old, past proposal_ttl_seconds {ttl}; propose again")
            if d.subject_id:
                cited = [store.get("position_snapshot", sid).as_of for sid in d.position_snapshot_ids]
                newer = store.raw(
                    "SELECT id, as_of FROM position_snapshot WHERE scope_ref = ? AND subject_id = ? "
                    "AND source_class IN (%s) AND as_of <= ? AND as_of > ? ORDER BY as_of DESC"
                    % ",".join("?" for _ in POSITION_GRADE),
                    (d.scope_ref, d.subject_id, *sorted(POSITION_GRADE), now, max(cited) if cited else ""))
                if newer:
                    raise InvariantViolation(f"account state changed since the proposal: snapshot {newer[0][0]} "
                                             f"at {newer[0][1]} is newer than those cited; propose again")
            if nav is not None:
                if not use_nav or abs(nav - use_nav) / use_nav * 100.0 > m.body.get("nav_tolerance_pct", 1.0):
                    raise InvariantViolation(f"NAV {nav:g} at approval differs from {use_nav} at proposal by more "
                                             f"than nav_tolerance_pct; propose again")
                use_nav = nav
            freshness = _snapshot_freshness(store, d.scope_ref, d.subject_id, list(d.position_snapshot_ids), now, m)
            held_q, held_v, held_as_of = _held(store, list(d.position_snapshot_ids), now)
            pending = _pending_notional(store, d.scope_ref, d.subject_id, held_as_of, exclude=d.id)
        check = check_against_mandate(d.decision_kind, d.proposal, m, nav=use_nav,
                                      current_subject_exposure_pct=i.get("current_subject_exposure_pct", 0.0),
                                      asset_class=i.get("asset_class"), subject_id=d.subject_id,
                                      day_notional_before=_day_notional(store, d.scope_ref, now[:10], d.id),
                                      snapshot_freshness=freshness,
                                      held_quantity=held_q, held_value=held_v, pending_notional=pending)
        check["state_basis"] = basis
        if check["status"] == "fail":
            breaches = set(check["breaches"])
            overridden = set((override or {}).get("overrides_breaches", []))
            if not breaches.issubset(overridden) or not (override or {}).get("reason", "").strip():
                raise InvariantViolation(
                    "mandate_check failed; approval requires an override with a reason that names every breach: "
                    + "; ".join(sorted(breaches - overridden)))
        rcpt = add_receipt(store, "approval", f"decision:{decision_id}", authority_ref,
                           {"override": override or None, "approval_check": check,
                            "proposal_hash": content_hash(d.proposal),
                            "mandate_check_hash": content_hash(d.mandate_check)})
        store.update("decision_record", decision_id, status="approved", approval_receipt_id=rcpt)
    return rcpt


def record_execution(store: Store, decision_id: str, authority_ref: str, fill: dict) -> str:
    """The venue reports a fill. The receipt stores the expected price next to
    the fill price, the slippage in basis points (positive is a cost), and
    whether the fill was partial. Slippage beyond the mandate's limit is
    flagged in the receipt; the fill already happened, so it is not refused."""
    authority.require(authority_ref, ("venue", "system"), "invariant 15: execution")
    d = get_decision(store, decision_id)
    if d.decision_kind not in EXECUTABLE_DECISION_KINDS:
        raise InvariantViolation(f"a {d.decision_kind} decision is not executable")
    if d.status != "approved":
        raise InvariantViolation(f"invariant 15: cannot execute from {d.status}; no approval receipt found")
    ttl = store.get("mandate", d.mandate_id).body.get("approval_ttl_seconds")
    approved_at = store.get("receipt", d.approval_receipt_id).occurred_at
    if ttl is not None and (parse_ts(store.clock()) - parse_ts(approved_at)).total_seconds() > ttl:
        raise InvariantViolation(f"approval from {approved_at} is older than approval_ttl_seconds {ttl}; "
                                 "expire the decision and propose again")
    qty, px = fill.get("qty"), fill.get("px")
    if not isinstance(qty, (int, float)) or not isinstance(px, (int, float)) or qty <= 0 or px <= 0:
        raise KernelError("fill needs positive numeric qty and px")
    expected = d.proposal.get("expected_price") or d.proposal.get("limit_price")
    direction = d.proposal.get("direction")
    side = "buy" if direction in BUY_SIDE else "sell" if direction in SELL_SIDE else d.proposal.get("side")
    sign = {"buy": 1, "sell": -1}.get(side)
    slippage_bps = round(sign * (px - expected) / expected * 1e4, 2) if expected and sign else None
    wanted = d.proposal.get("quantity")
    m = store.get("mandate", d.mandate_id)
    limit = m.body.get("max_slippage_bps")
    payload = {"fill": dict(fill), "side": side, "expected_price": expected, "slippage_bps": slippage_bps,
               "proposal_hash": content_hash(d.proposal),
               "partial": bool(wanted and qty < wanted), "unfilled_qty": (wanted - qty) if wanted and qty < wanted else 0,
               "slippage_breach": bool(limit is not None and slippage_bps is not None and slippage_bps > limit)}
    if expected is None or sign is None:
        payload["note"] = ("slippage not measurable: the proposal needs expected_price or limit_price, "
                           "and a side ('buy' or 'sell') when the direction is reduce or close")
    with store.transaction():
        rcpt = add_receipt(store, "execution", f"decision:{decision_id}", authority_ref, payload,
                           prior_receipt_id=d.approval_receipt_id)
        store.update("decision_record", decision_id, status="executed", execution_receipt_id=rcpt)
    return rcpt


def reconcile(store: Store, decision_id: str, authority_ref: str, snapshot_id: str) -> str:
    """Bind an executed decision to a reconciled snapshot of the same scope and subject. Once."""
    authority.require(authority_ref, ("venue", "system"), "reconcile")
    d = get_decision(store, decision_id)
    if d.status != "executed":
        raise InvariantViolation("reconcile requires an executed decision")
    if d.reconcile_receipt_id:
        raise InvariantViolation("decision is already reconciled")
    snap = store.get("position_snapshot", snapshot_id)
    if snap.source_class != "reconciled":
        raise InvariantViolation("reconcile must bind a snapshot with source_class 'reconciled'")
    if snap.scope_ref != d.scope_ref or snap.subject_id != d.subject_id:
        raise InvariantViolation("reconcile snapshot belongs to another scope or subject")
    with store.transaction():
        rcpt = add_receipt(store, "reconcile", f"decision:{decision_id}", authority_ref,
                           {"snapshot_id": snapshot_id, "snapshot_hash": snap.content_hash},
                           prior_receipt_id=d.execution_receipt_id)
        store.update("decision_record", decision_id, reconcile_receipt_id=rcpt)
    return rcpt


def reject_decision(store: Store, decision_id: str, authority_ref: str, reason: str) -> str:
    authority.require_human(authority_ref, "rejecting a decision")
    d = get_decision(store, decision_id)
    if d.status != "proposed":
        raise InvariantViolation(f"cannot reject from {d.status}")
    with store.transaction():
        rcpt = add_receipt(store, "review", f"decision:{decision_id}", authority_ref,
                           {"rejected": True, "reason": reason})
        store.update("decision_record", decision_id, status="rejected")
    return rcpt


def expire_decision(store: Store, decision_id: str, authority_ref: str, reason: str) -> str:
    """A proposal or an unexecuted approval lapses. A human or a system
    scheduler may record this; a model may not."""
    authority.require(authority_ref, ("direct_user", "reviewer", "system"), "expiring a decision")
    d = get_decision(store, decision_id)
    if d.status not in ("proposed", "approved"):
        raise InvariantViolation(f"cannot expire from {d.status}")
    with store.transaction():
        rcpt = add_receipt(store, "review", f"decision:{decision_id}", authority_ref,
                           {"expired": True, "reason": reason})
        store.update("decision_record", decision_id, status="expired")
    return rcpt


def declined_decisions(store: Store, scope_ref: str) -> list[DecisionRecord]:
    """Every hold and no_trade with its rationale, for grading restraint later."""
    return store.query("decision_record", "scope_ref = ? AND decision_kind IN ('hold','no_trade')",
                       (scope_ref,), "proposed_at, rowid")


def attach_outcome(store: Store, decision_id: str, outcome: dict, authority_ref: str = "system:evaluator") -> str:
    """An outcome is a review receipt on the decision; the decision row does not change."""
    if not store.exists("decision_record", decision_id):
        raise KernelError(f"no decision {decision_id}")
    return add_receipt(store, "review", f"decision:{decision_id}", authority_ref, {"outcome": dict(outcome)})


def execution_costs(store: Store, scope_ref: str) -> dict:
    """Slippage per executed decision and the notional-weighted average."""
    rows = []
    for d in store.query("decision_record", "scope_ref = ? AND status = 'executed'", (scope_ref,), "proposed_at, rowid"):
        r = store.get("receipt", d.execution_receipt_id)
        rows.append({"decision_id": d.id, "notional": float(d.proposal.get("notional") or 0),
                     "slippage_bps": r.payload.get("slippage_bps"), "partial": r.payload.get("partial")})
    measured = [x for x in rows if x["slippage_bps"] is not None and x["notional"]]
    total = sum(x["notional"] for x in measured)
    avg = round(sum(x["slippage_bps"] * x["notional"] for x in measured) / total, 2) if total else None
    return {"executions": rows, "measured": len(measured), "weighted_slippage_bps": avg}


def decision_chain(store: Store, decision_id: str) -> list[dict]:
    """Every receipt bound to a decision, in the order written."""
    rows = store.query("receipt", "object_ref = ?", (f"decision:{decision_id}",), "rowid")
    return [{"id": r.id, "kind": r.kind, "authority": r.authority_ref, "at": r.occurred_at} for r in rows]
