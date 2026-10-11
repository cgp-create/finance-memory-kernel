"""Subjects and evidence: append-only, content-hashed.

Evidence is what a source said, with when it was observed, when the store
ingested it, an authority tier, and a sha256 over the payload. A
correction is a new Evidence row pointing at the one it corrects through
supersedes_evidence_id; the old row is never edited (invariant 2).
Nothing here interprets evidence or fetches anything.
"""
from __future__ import annotations

from typing import Any, Optional

from .models import (
    AUTHORITY_TIERS,
    Evidence,
    InvariantViolation,
    KernelError,
    Subject,
    content_hash,
    norm_ts,
)
from .store import Store


def add_subject(store: Store, key: str, title: str, type: str = "issuer",
                namespace: str = "demo", subject_id: Optional[str] = None) -> str:
    now = store.clock()
    rec = Subject(
        id=subject_id or store.new_id("subj"),
        namespace=namespace,
        key=key,
        type=type,
        title=title,
        status="active",
        created_at=now,
        updated_at=now,
    )
    return store.insert(rec)


def get_subject_by_key(store: Store, key: str, namespace: str = "demo") -> Optional[Subject]:
    rows = store.query("subject", "namespace = ? AND key = ?", (namespace, key))
    return rows[0] if rows else None


def add_evidence(store: Store, subject_id: str, source_kind: str, source_ref: str,
                observed_at: str, authority_tier: str, payload: dict[str, Any],
                supersedes_evidence_id: Optional[str] = None,
                evidence_id: Optional[str] = None) -> str:
    """Append one immutable evidence record and return its id."""
    if authority_tier not in AUTHORITY_TIERS:
        raise KernelError(f"unknown authority tier {authority_tier!r}")
    if not isinstance(payload, dict):
        raise KernelError("payload must be a dict")
    if supersedes_evidence_id is not None and not store.exists("evidence", supersedes_evidence_id):
        raise InvariantViolation(f"supersedes_evidence_id {supersedes_evidence_id} does not exist")
    rec = Evidence(
        id=evidence_id or store.new_id("ev"),
        subject_id=subject_id,
        source_kind=source_kind,
        source_ref=source_ref,
        observed_at=norm_ts(observed_at),  # type: ignore[arg-type]
        ingested_at=store.clock(),
        authority_tier=authority_tier,
        content_hash=content_hash({"source_ref": source_ref, "observed_at": norm_ts(observed_at), "payload": payload}),
        payload=payload,
        supersedes_evidence_id=supersedes_evidence_id,
    )
    return store.insert(rec)


def get_evidence(store: Store, evidence_id: str) -> Evidence:
    return store.get("evidence", evidence_id)


def evidence_for_subject(store: Store, subject_id: str, as_of: Optional[str] = None) -> list[Evidence]:
    """Evidence observed at or before as_of. The cutoff is what blocks future leakage."""
    if as_of is None:
        return store.query("evidence", "subject_id = ?", (subject_id,), "observed_at")
    return store.query("evidence", "subject_id = ? AND observed_at <= ?", (subject_id, norm_ts(as_of)), "observed_at")


def verify_hash(ev: Evidence) -> bool:
    return ev.content_hash == content_hash(
        {"source_ref": ev.source_ref, "observed_at": ev.observed_at, "payload": ev.payload}
    )
