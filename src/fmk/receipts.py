"""Append-only receipt contract.

What this module does: writes receipts (review, selection, approval, apply,
execution, delivery, mandate_activation, reconcile, health_check) and
answers "does a receipt of kind K exist for object X". It is the only way
a status in this kernel is allowed to move. Each kind is distinct: a
selection receipt proves selection and nothing else (invariant 6).

What this module does not do: it does not perform the action it records.
Writing an execution receipt does not execute anything; the venue does
that, and this module records that it said so.
"""
from __future__ import annotations

from typing import Optional

from .models import RECEIPT_KINDS, InvariantViolation, KernelError, Receipt, content_hash, new_id
from .store import Store


def add_receipt(store: Store, kind: str, object_ref: str, authority_ref: str,
                payload: Optional[dict] = None, prior_receipt_id: Optional[str] = None,
                occurred_at: Optional[str] = None) -> str:
    if kind not in RECEIPT_KINDS:
        raise KernelError(f"unknown receipt kind {kind!r}")
    if not authority_ref or not authority_ref.strip():
        raise InvariantViolation("a receipt must name its authority")
    if ":" not in object_ref:
        raise KernelError("object_ref must look like 'kind:id'")
    payload = dict(payload or {})
    rec = Receipt(
        id=store.new_id("rcpt"),
        kind=kind,
        object_ref=object_ref,
        authority_ref=authority_ref,
        occurred_at=occurred_at or store.clock(),
        payload=payload,
        payload_hash=content_hash(payload),
        prior_receipt_id=prior_receipt_id,
    )
    return store.insert(rec)


def get_receipt(store: Store, receipt_id: str) -> Receipt:
    return store.get("receipt", receipt_id)


def receipts_for(store: Store, object_ref: str, kind: Optional[str] = None) -> list[Receipt]:
    if kind:
        return store.query("receipt", "object_ref = ? AND kind = ?", (object_ref, kind), "occurred_at, rowid")
    return store.query("receipt", "object_ref = ?", (object_ref,), "occurred_at, rowid")


def has_receipt(store: Store, object_ref: str, kind: str) -> bool:
    return bool(receipts_for(store, object_ref, kind))


def require_receipt(store: Store, receipt_id: str, kind: str, object_ref: str) -> Receipt:
    """Invariant 6: the receipt must exist, be of exactly this kind and be
    bound to exactly this object. A selection receipt never satisfies an
    execution check, and a receipt for another object never satisfies this one."""
    r = store.try_get("receipt", receipt_id)
    if r is None:
        raise InvariantViolation(f"receipt {receipt_id} not found")
    if r.kind != kind:
        raise InvariantViolation(f"receipt {receipt_id} is '{r.kind}', not '{kind}'; receipt kinds are not interchangeable")
    if r.object_ref != object_ref:
        raise InvariantViolation(f"receipt {receipt_id} is bound to {r.object_ref}, not {object_ref}")
    return r


def status_of(store: Store, object_ref: str) -> dict:
    """What can be proven about an object from receipts alone (spec section 8).

    'drafted' is deliberately absent: a draft is not delivered until a
    delivery receipt exists.
    """
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
    """Known-answer fixture result stored as a receipt."""
    return add_receipt(store, "health_check", f"fixture:{fixture_name}", authority_ref,
                       {"fixture_hash": fixture_hash, "passed": bool(passed)})
