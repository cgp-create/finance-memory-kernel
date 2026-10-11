"""Append-only receipts, linked into one hash chain.

A receipt records that something happened (a review, selection, approval,
execution, delivery, mandate activation, reconciliation or health check),
to which object, on whose authority, and when. Receipts are the only way a
status in this kernel moves. Kinds are not interchangeable: a selection
receipt proves selection and nothing else (invariant 6).

Each receipt stores the chain hash of the receipt before it and its own
chain hash over (id, kind, object_ref, authority_ref, occurred_at,
payload_hash, prior_receipt_id, prev_chain_hash). The schema refuses a
receipt that does not link to the current head, so the chain cannot fork.
verify_chain() recomputes every link and payload hash. Store chain_head()
somewhere the database owner cannot write (a log, an email, a commit) and
any later rewrite of history shows up as a mismatch against it. Receipts
also carry hashes of what they vouch for (an approved proposal, an
activated mandate body, a selected revision, a reconciled snapshot), and
verify_chain checks those too. Rows that no receipt vouches for, such as
evidence and claims, are protected only by the triggers.

Receipts are not signed. A receipt proves what was recorded and in what
order, not who typed it; see authority.py and Store.authorize.

Writing an execution receipt does not execute anything. The venue does
that; this module records that it said so.
"""
from __future__ import annotations

from typing import Optional

from . import authority
from .models import RECEIPT_KINDS, InvariantViolation, KernelError, Receipt, content_hash
from .store import Store

GENESIS = "genesis"


def _link_hash(r: Receipt) -> str:
    return content_hash({
        "id": r.id, "kind": r.kind, "object_ref": r.object_ref, "authority_ref": r.authority_ref,
        "occurred_at": r.occurred_at, "payload_hash": r.payload_hash,
        "prior_receipt_id": r.prior_receipt_id, "prev_chain_hash": r.prev_chain_hash,
    })


def chain_head(store: Store) -> str:
    rows = store.raw("SELECT chain_hash FROM receipt ORDER BY rowid DESC LIMIT 1")
    return rows[0][0] if rows else GENESIS


def add_receipt(store: Store, kind: str, object_ref: str, authority_ref: str,
                payload: Optional[dict] = None, prior_receipt_id: Optional[str] = None) -> str:
    """Write a receipt. Its time is the store clock; callers cannot set it."""
    if kind not in RECEIPT_KINDS:
        raise KernelError(f"unknown receipt kind {kind!r}")
    authority.parse(authority_ref)
    if ":" not in object_ref:
        raise KernelError("object_ref must look like 'kind:id'")
    payload = dict(payload or {})
    with store.transaction(immediate=True):
        rec = Receipt(
            id=store.new_id("rcpt"), kind=kind, object_ref=object_ref, authority_ref=authority_ref,
            occurred_at=store.clock(), payload=payload, payload_hash=content_hash(payload),
            prior_receipt_id=prior_receipt_id, prev_chain_hash=chain_head(store),
        )
        rec.chain_hash = _link_hash(rec)
        store.insert(rec)
    return rec.id


def verify_bindings(store: Store) -> list[str]:
    """Check that the objects receipts vouch for still match the hashes the
    receipts recorded: the approved and executed proposal, the activated
    mandate body, the selected revision's assumptions and evidence, and the
    reconciled snapshot. The chain protects the receipts; this extends that
    protection to the rows they approved."""
    problems: list[str] = []
    for r in store.query("receipt", "1=1", (), "rowid"):
        kind, _, ident = r.object_ref.partition(":")
        p = r.payload
        if r.kind in ("approval", "execution") and "proposal_hash" in p:
            d = store.try_get("decision_record", ident)
            if d is None or content_hash(d.proposal) != p["proposal_hash"]:
                problems.append(f"{r.id}: decision {ident} proposal no longer matches its {r.kind} receipt")
        elif r.kind == "mandate_activation" and "body_hash" in p:
            m = store.try_get("mandate", ident)
            if m is None or content_hash(m.body) != p["body_hash"]:
                problems.append(f"{r.id}: mandate {ident} body no longer matches what was activated")
        elif r.kind == "selection" and "assumptions_hash" in p:
            v = store.try_get("case_set_revision", ident)
            if v is None or (v.assumptions_hash, v.evidence_hash) != (p["assumptions_hash"], p["evidence_hash"]):
                problems.append(f"{r.id}: revision {ident} no longer matches what was selected")
        elif r.kind == "reconcile" and "snapshot_hash" in p:
            snap = store.try_get("position_snapshot", p.get("snapshot_id", ""))
            if snap is None or snap.content_hash != p["snapshot_hash"]:
                problems.append(f"{r.id}: reconciled snapshot no longer matches")
    return problems


def verify_chain(store: Store, expected_head: Optional[str] = None, bindings: bool = True) -> list[str]:
    """Recompute every payload hash and chain link, and (by default) the
    bindings from receipts to the rows they vouch for. Returns a list of
    problems; empty means intact, and ending at expected_head if given."""
    problems: list[str] = []
    prev = GENESIS
    rows = store.query("receipt", "1=1", (), "rowid")
    for r in rows:
        if r.payload_hash != content_hash(r.payload):
            problems.append(f"{r.id}: payload does not match payload_hash")
        if r.prev_chain_hash != prev:
            problems.append(f"{r.id}: links to {r.prev_chain_hash[:12]}, expected {prev[:12]}")
        if r.chain_hash != _link_hash(r):
            problems.append(f"{r.id}: chain_hash does not match its contents")
        prev = r.chain_hash
    if expected_head is not None and prev != expected_head:
        problems.append(f"chain ends at {prev[:12]}, expected {expected_head[:12]}")
    if bindings:
        problems.extend(verify_bindings(store))
    return problems


def get_receipt(store: Store, receipt_id: str) -> Receipt:
    return store.get("receipt", receipt_id)


def receipts_for(store: Store, object_ref: str, kind: Optional[str] = None) -> list[Receipt]:
    if kind:
        return store.query("receipt", "object_ref = ? AND kind = ?", (object_ref, kind), "rowid")
    return store.query("receipt", "object_ref = ?", (object_ref,), "rowid")


def has_receipt(store: Store, object_ref: str, kind: str) -> bool:
    return bool(receipts_for(store, object_ref, kind))


def require_receipt(store: Store, receipt_id: str, kind: str, object_ref: str) -> Receipt:
    """Invariant 6: the receipt must exist, be of exactly this kind and be
    bound to exactly this object."""
    r = store.try_get("receipt", receipt_id)
    if r is None:
        raise InvariantViolation(f"receipt {receipt_id} not found")
    if r.kind != kind:
        raise InvariantViolation(f"receipt {receipt_id} is '{r.kind}', not '{kind}'; receipt kinds are not interchangeable")
    if r.object_ref != object_ref:
        raise InvariantViolation(f"receipt {receipt_id} is bound to {r.object_ref}, not {object_ref}")
    return r


def status_of(store: Store, object_ref: str) -> dict:
    """What can be shown about an object from receipts alone. A draft is not
    'delivered' until a delivery receipt exists."""
    kinds = {r.kind for r in receipts_for(store, object_ref)}
    return {
        "reviewed": "review" in kinds,
        "selected": "selection" in kinds,
        "approved": "approval" in kinds,
        "applied": "apply" in kinds,
        "executed": "execution" in kinds,
        "delivered": "delivery" in kinds,
        "reconciled": "reconcile" in kinds,
    }


def health_check(store: Store, fixture_name: str, fixture_hash: str, passed: bool,
                 authority_ref: str = "system:health") -> str:
    """Known-answer fixture result stored as a receipt. Nothing here schedules it."""
    return add_receipt(store, "health_check", f"fixture:{fixture_name}", authority_ref,
                       {"fixture_hash": fixture_hash, "passed": bool(passed)})
