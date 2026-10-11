"""Claims, relations, supersession, contradiction and research questions.

What this module does: creates claims (each bound to one Evidence row,
invariant 1), records relations between claims, applies supersession only
within one subject and aspect (invariant 8), records contradictions
without picking a winner (invariant 9), stamps expiry by policy
(invariant 10), and manages ResearchQuestions.

What this module does not do: it never edits a claim's statement or
evidence; only status moves. It never decides which of two contradicting
claims is right. It never touches underwriting.
"""
from __future__ import annotations

from typing import Any, Optional

from .models import (
    CLAIM_KINDS,
    CLAIM_RELATIONS,
    AUTHORITY_RANK,
    Claim,
    ClaimRelation,
    InvariantViolation,
    KernelError,
    ResearchQuestion,
    new_id,
    norm_ts,
)
from .policy import FreshnessPolicy
from .store import Store


def add_claim(store: Store, subject_id: str, evidence_id: str, aspect: str, kind: str,
              statement: str, value: Any = None, confidence: float = 0.5,
              source_strength: Optional[str] = None, status: str = "active",
              valid_from: Optional[str] = None, valid_to: Optional[str] = None,
              expires_at: Optional[str] = None, created_at: Optional[str] = None,
              policy: Optional[FreshnessPolicy] = None, claim_id: Optional[str] = None) -> str:
    """Append a claim. source_strength defaults to the evidence's authority tier."""
    if kind not in CLAIM_KINDS:
        raise KernelError(f"unknown claim kind {kind!r}")
    if not store.exists("evidence", evidence_id):
        raise InvariantViolation(f"invariant 1: evidence {evidence_id} does not exist; a claim needs evidence")
    ev = store.get("evidence", evidence_id)
    if ev.subject_id != subject_id:
        raise InvariantViolation("evidence belongs to a different subject")
    if source_strength is None:
        source_strength = ev.authority_tier
    created = norm_ts(created_at) if created_at else store.clock()
    pol = policy or FreshnessPolicy()
    if expires_at is None:
        expires_at = pol.default_expiry(kind, aspect, created)  # type: ignore[arg-type]
    rec = Claim(
        id=claim_id or store.new_id("clm"),
        subject_id=subject_id,
        evidence_id=evidence_id,
        aspect=aspect,
        kind=kind,
        statement=statement,
        value_json=value,
        confidence=float(confidence),
        source_strength=source_strength,
        status=status,
        valid_from=norm_ts(valid_from),
        valid_to=norm_ts(valid_to),
        created_at=created,  # type: ignore[arg-type]
        expires_at=norm_ts(expires_at),
    )
    return store.insert(rec)


def get_claim(store: Store, claim_id: str) -> Claim:
    return store.get("claim", claim_id)


def set_claim_status(store: Store, claim_id: str, status: str) -> None:
    store.update("claim", claim_id, status=status)


def add_relation(store: Store, from_claim_id: str, relation: str, to_claim_id: str,
                 rationale_evidence_id: Optional[str] = None) -> str:
    if relation not in CLAIM_RELATIONS:
        raise KernelError(f"unknown relation {relation!r}")
    if from_claim_id == to_claim_id:
        raise KernelError("a claim cannot relate to itself")
    rec = ClaimRelation(
        id=store.new_id("rel"),
        from_claim_id=from_claim_id,
        relation=relation,
        to_claim_id=to_claim_id,
        rationale_evidence_id=rationale_evidence_id,
        created_at=store.clock(),
    )
    return store.insert(rec)


def relations_of(store: Store, claim_id: str) -> list[ClaimRelation]:
    return store.query("claim_relation", "from_claim_id = ? OR to_claim_id = ?", (claim_id, claim_id), "created_at")


def supersede(store: Store, new_claim_id: str, old_claim_id: str,
              rationale_evidence_id: Optional[str] = None) -> str:
    """Invariant 8: new claim supersedes old claim for the same subject and aspect.

    The old claim stays in the table with status 'superseded' and the
    relation records the direction. Any other claim is untouched.
    """
    new = get_claim(store, new_claim_id)
    old = get_claim(store, old_claim_id)
    if new.subject_id != old.subject_id:
        raise InvariantViolation("invariant 8: supersession cannot cross subjects")
    if new.aspect != old.aspect:
        raise InvariantViolation(
            f"invariant 8: supersession is scoped to one aspect ({old.aspect!r} vs {new.aspect!r})")
    if old.status == "retracted":
        raise InvariantViolation("a retracted claim is not superseded; it is already out")
    with store.transaction():
        rid = add_relation(store, new_claim_id, "supersedes", old_claim_id, rationale_evidence_id)
        store.update("claim", old_claim_id, status="superseded")
    return rid


def correct(store: Store, subject_id: str, old_claim_id: str, evidence_id: str, statement: str,
            value: Any = None, confidence: float = 0.9, **kw: Any) -> str:
    """A correction is a new claim of kind 'correction' plus a supersession.

    The correcting evidence must outrank or equal the old claim's source;
    a lower-authority source does not get to overwrite, it contradicts.
    """
    old = get_claim(store, old_claim_id)
    ev = store.get("evidence", evidence_id)
    if AUTHORITY_RANK[ev.authority_tier] < AUTHORITY_RANK.get(old.source_strength, 0):
        raise InvariantViolation(
            f"correction from '{ev.authority_tier}' cannot supersede a '{old.source_strength}' claim; "
            "record a contradiction instead")
    new_id_ = add_claim(store, subject_id, evidence_id, old.aspect, "correction", statement,
                        value=value, confidence=confidence, **kw)
    supersede(store, new_id_, old_claim_id, rationale_evidence_id=evidence_id)
    return new_id_


def contradict(store: Store, claim_a: str, claim_b: str, rationale_evidence_id: Optional[str] = None) -> str:
    """Invariant 9: record that two claims conflict. Both stay visible as
    'contradicted'; nothing here chooses between them."""
    a = get_claim(store, claim_a)
    b = get_claim(store, claim_b)
    if a.subject_id != b.subject_id:
        raise KernelError("contradiction must be within one subject")
    with store.transaction():
        rid = add_relation(store, claim_a, "contradicts", claim_b, rationale_evidence_id)
        for cid, c in ((claim_a, a), (claim_b, b)):
            if c.status == "active":
                store.update("claim", cid, status="contradicted")
    return rid


def resolve_contradiction(store: Store, winner_id: str, loser_id: str, authority_ref: str,
                          rationale_evidence_id: str) -> str:
    """An authorised resolution: the winner is superseding the loser for the
    aspect, backed by new evidence, and a review receipt names who decided.
    Similarity scores are not an authority and never reach this function."""
    from .receipts import add_receipt

    if not authority_ref.startswith(("direct_user", "primary", "reviewer")):
        raise InvariantViolation("resolving a contradiction requires a named human or primary authority")
    with store.transaction():
        add_receipt(store, "review", f"claim:{winner_id}", authority_ref,
                    {"resolves_contradiction_with": loser_id, "evidence_id": rationale_evidence_id})
        rid = supersede(store, winner_id, loser_id, rationale_evidence_id)
        store.update("claim", winner_id, status="active")
    return rid


def retract(store: Store, claim_id: str, rationale_evidence_id: str) -> None:
    """Retraction needs evidence that the retraction happened; the row stays."""
    if not store.exists("evidence", rationale_evidence_id):
        raise InvariantViolation("retraction requires evidence")
    store.update("claim", claim_id, status="retracted")


def claims_for(store: Store, subject_id: str, aspect: Optional[str] = None,
               statuses: Optional[tuple[str, ...]] = None, as_of: Optional[str] = None) -> list[Claim]:
    where = "subject_id = ?"
    params: list[Any] = [subject_id]
    if aspect:
        where += " AND aspect = ?"
        params.append(aspect)
    if statuses:
        where += " AND status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    if as_of:
        where += " AND created_at <= ?"
        params.append(norm_ts(as_of))
    return store.query("claim", where, tuple(params), "created_at, id")


def current_claim(store: Store, subject_id: str, aspect: str, as_of: Optional[str] = None) -> Optional[Claim]:
    """The newest non-superseded, non-retracted claim for an aspect, or None."""
    rows = claims_for(store, subject_id, aspect, ("active", "stale", "contradicted", "unresolved"), as_of)
    return rows[-1] if rows else None


def unresolved_contradictions(store: Store, subject_id: str) -> list[tuple[str, str]]:
    """Pairs of claim ids that contradict each other and are both still contradicted."""
    rows = store.raw(
        """SELECT r.from_claim_id, r.to_claim_id FROM claim_relation r
           JOIN claim a ON a.id = r.from_claim_id JOIN claim b ON b.id = r.to_claim_id
           WHERE r.relation = 'contradicts' AND a.subject_id = ?
             AND a.status = 'contradicted' AND b.status = 'contradicted'""",
        (subject_id,),
    )
    return [(r[0], r[1]) for r in rows]


# ------------------------------------------------------------------ questions


def open_question(store: Store, subject_id: str, question: str, evidence_needed: str,
                  owner: str = "research", priority: int = 3, revisit_at: Optional[str] = None,
                  created_at: Optional[str] = None) -> str:
    now = norm_ts(created_at) if created_at else store.clock()
    rec = ResearchQuestion(
        id=store.new_id("q"), subject_id=subject_id, question=question, evidence_needed=evidence_needed,
        priority=priority, owner=owner, status="open", revisit_at=norm_ts(revisit_at),
        answer_claim_ids=[], created_at=now, updated_at=now,
    )
    return store.insert(rec)


def answer_question(store: Store, question_id: str, answer_claim_ids: list[str]) -> None:
    for cid in answer_claim_ids:
        if not store.exists("claim", cid):
            raise InvariantViolation(f"answer claim {cid} does not exist")
    store.update("research_question", question_id, status="answered",
                 answer_claim_ids=list(answer_claim_ids), updated_at=store.clock())


def open_questions(store: Store, subject_id: str) -> list[ResearchQuestion]:
    return store.query("research_question", "subject_id = ? AND status = 'open'", (subject_id,), "priority, created_at")
