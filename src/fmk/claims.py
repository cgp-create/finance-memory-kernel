"""Claims, relations, supersession, contradiction, dependencies and questions.

Each claim is bound to one Evidence row (invariant 1) and can never claim
more authority than that evidence carries. Supersession is scoped to one
subject and aspect (invariant 8). Contradictions are recorded without
picking a winner (invariant 9); only a named human resolves one. Expiry is
stamped by policy from when the evidence was observed, not from when the
claim was typed (invariant 10).

Dependencies are explicit: add_dependency(a, b) says claim a rests on
claim b. When b is superseded or retracted, every current claim that rests
on it, directly or through other claims, is marked 'stale' in the same
transaction. A stale claim stays visible with a warning until someone
supersedes it or a human reaffirms it. This is the kernel's answer to a
correction that is stored but not used.

Nothing here edits a claim's statement or evidence, and nothing here
touches underwriting.
"""
from __future__ import annotations

from typing import Any, Optional

from . import authority
from .models import (
    AUTHORITY_RANK,
    CLAIM_KINDS,
    CLAIM_RELATIONS,
    Claim,
    ClaimRelation,
    InvariantViolation,
    KernelError,
    ResearchQuestion,
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
    """Append a claim.

    source_strength defaults to the evidence's authority tier and may be set
    lower, never higher: a claim cannot be more authoritative than its source.
    Default expiry runs from valid_from if given, else from the evidence's
    observed_at.
    """
    if kind not in CLAIM_KINDS:
        raise KernelError(f"unknown claim kind {kind!r}")
    if status not in ("active", "unresolved"):
        raise InvariantViolation("a new claim starts active or unresolved; other states are reached by relations")
    ev = store.try_get("evidence", evidence_id)
    if ev is None:
        raise InvariantViolation(f"invariant 1: evidence {evidence_id} does not exist; a claim needs evidence")
    if ev.subject_id != subject_id:
        raise InvariantViolation("evidence belongs to a different subject")
    if source_strength is None:
        source_strength = ev.authority_tier
    if source_strength not in AUTHORITY_RANK:
        raise KernelError(f"unknown source_strength {source_strength!r}")
    if AUTHORITY_RANK[source_strength] > AUTHORITY_RANK[ev.authority_tier]:
        raise InvariantViolation(
            f"a claim cannot outrank its evidence: '{source_strength}' on '{ev.authority_tier}' evidence")
    created = norm_ts(created_at) if created_at else store.clock()
    basis = norm_ts(valid_from) or ev.observed_at
    pol = policy or FreshnessPolicy()
    if expires_at is None:
        expires_at = pol.default_expiry(kind, aspect, basis)  # type: ignore[arg-type]
    rec = Claim(
        id=claim_id or store.new_id("clm"), subject_id=subject_id, evidence_id=evidence_id,
        aspect=aspect, kind=kind, statement=statement, value_json=value, confidence=float(confidence),
        source_strength=source_strength, status=status, valid_from=norm_ts(valid_from),
        valid_to=norm_ts(valid_to), created_at=created,  # type: ignore[arg-type]
        expires_at=norm_ts(expires_at), recorded_at=store.clock(),
    )
    return store.insert(rec)


def get_claim(store: Store, claim_id: str) -> Claim:
    return store.get("claim", claim_id)


def add_relation(store: Store, from_claim_id: str, relation: str, to_claim_id: str,
                 rationale_evidence_id: Optional[str] = None) -> str:
    if relation not in CLAIM_RELATIONS:
        raise KernelError(f"unknown relation {relation!r}")
    if from_claim_id == to_claim_id:
        raise KernelError("a claim cannot relate to itself")
    rec = ClaimRelation(id=store.new_id("rel"), from_claim_id=from_claim_id, relation=relation,
                        to_claim_id=to_claim_id, rationale_evidence_id=rationale_evidence_id,
                        created_at=store.clock())
    return store.insert(rec)


def relations_of(store: Store, claim_id: str) -> list[ClaimRelation]:
    return store.query("claim_relation", "from_claim_id = ? OR to_claim_id = ?", (claim_id, claim_id), "created_at, rowid")


# ------------------------------------------------------------- dependencies

_FOREVER = "9999-12-31T23:59:59Z"


def add_dependency(store: Store, dependent_claim_id: str, on_claim_id: str) -> str:
    """Record that one claim rests on another. Cycles are refused."""
    if on_claim_id in _dependents(store, dependent_claim_id) | {dependent_claim_id}:
        raise InvariantViolation("dependency would create a cycle")
    return add_relation(store, dependent_claim_id, "depends_on", on_claim_id)


def _direct_deps(store: Store, claim_id: str, known: str) -> list[str]:
    return [r[0] for r in store.raw(
        "SELECT to_claim_id FROM claim_relation WHERE relation='depends_on' AND from_claim_id=? "
        "AND created_at <= ? ORDER BY rowid", (claim_id, known))]


def _dependents(store: Store, claim_id: str) -> set[str]:
    """Every claim that rests on claim_id, directly or transitively."""
    seen: set[str] = set()
    frontier = [claim_id]
    while frontier:
        cur = frontier.pop()
        for r in store.raw("SELECT from_claim_id FROM claim_relation WHERE relation='depends_on' AND to_claim_id=?", (cur,)):
            if r[0] not in seen:
                seen.add(r[0])
                frontier.append(r[0])
    return seen


def changed_at(store: Store, claim_id: str, known: Optional[str] = None) -> Optional[str]:
    """When the claim stopped being current (superseded or retracted), if by `known`."""
    known = known or _FOREVER
    rows = store.raw(
        "SELECT MIN(t) FROM ("
        " SELECT created_at AS t FROM claim_relation WHERE relation='supersedes' AND to_claim_id=? AND created_at <= ?"
        " UNION ALL"
        " SELECT occurred_at AS t FROM receipt WHERE kind='review' AND object_ref=? "
        "   AND json_extract(payload,'$.retracted') = 1 AND occurred_at <= ?)",
        (claim_id, known, f"claim:{claim_id}", known))
    return rows[0][0]


def _last_reaffirmed(store: Store, claim_id: str, known: str) -> Optional[str]:
    rows = store.raw("SELECT MAX(occurred_at) FROM receipt WHERE kind='review' AND object_ref=? "
                     "AND json_extract(payload,'$.reaffirmed') = 1 AND occurred_at <= ?",
                     (f"claim:{claim_id}", known))
    return rows[0][0]


def _stale_deps_with_time(store: Store, claim_id: str, known: str, seen: set[str]) -> list[tuple[str, str]]:
    """(dependency, when it changed) for every basis that changed after this
    claim was last reaffirmed. A basis that is itself resting on a changed
    claim counts, with the time of that deeper change."""
    if claim_id in seen:
        return []
    seen = seen | {claim_id}
    reaffirmed = _last_reaffirmed(store, claim_id, known)
    out: list[tuple[str, str]] = []
    for dep in _direct_deps(store, claim_id, known):
        t = changed_at(store, dep, known)
        if t is not None:
            if reaffirmed is None or t > reaffirmed:
                out.append((dep, t))
            continue
        for deep, td in _stale_deps_with_time(store, dep, known, seen):
            if reaffirmed is None or td > reaffirmed:
                out.append((deep, td))
    return out


def stale_dependencies(store: Store, claim_id: str, known: Optional[str] = None) -> list[str]:
    """Claims this one rests on, directly or transitively, that were superseded
    or retracted after this claim was last reaffirmed (all as of `known`)."""
    return sorted({d for d, _ in _stale_deps_with_time(store, claim_id, known or _FOREVER, set())})


def _mark_dependents_stale(store: Store, claim_id: str) -> list[str]:
    """Mark current dependents stale, each with a system receipt saying why."""
    from .receipts import add_receipt

    marked = []
    for cid in sorted(_dependents(store, claim_id)):
        if get_claim(store, cid).status in ("active", "unresolved"):
            add_receipt(store, "review", f"claim:{cid}", "system:kernel", {"stale": True, "because_of": claim_id})
            store.update("claim", cid, status="stale")
            marked.append(cid)
    return marked


def current_successor(store: Store, claim_id: str) -> Optional[str]:
    """Follow supersession to the newest claim that replaced this one."""
    cur, seen = claim_id, set()
    while cur not in seen:
        seen.add(cur)
        rows = store.raw("SELECT from_claim_id FROM claim_relation WHERE relation='supersedes' AND to_claim_id=? "
                         "ORDER BY rowid DESC LIMIT 1", (cur,))
        if not rows:
            return cur if cur != claim_id else None
        cur = rows[0][0]
    return None


def reaffirm(store: Store, claim_id: str, authority_ref: str, note: str = "") -> str:
    """A human looked at a stale claim and says it still holds.

    Writes a review receipt and returns the claim to active. The claim is
    re-pointed at the successors of the bases that changed, so that it goes
    stale again if those successors change. Packets stop warning about
    changes older than the reaffirmation."""
    from .receipts import add_receipt

    authority.require_human(authority_ref, "reaffirming a stale claim")
    c = get_claim(store, claim_id)
    if c.status != "stale":
        raise InvariantViolation(f"only a stale claim can be reaffirmed; this one is {c.status}")
    changed = stale_dependencies(store, claim_id)
    with store.transaction():
        repointed = []
        for dep in changed:
            succ = current_successor(store, dep)
            if succ and succ not in _direct_deps(store, claim_id, _FOREVER) and succ != claim_id \
                    and claim_id not in _dependents(store, succ) | {succ}:
                add_relation(store, claim_id, "depends_on", succ)
                repointed.append(succ)
        rcpt = add_receipt(store, "review", f"claim:{claim_id}", authority_ref,
                           {"reaffirmed": True, "changed_bases": changed, "now_rests_on": repointed, "note": note})
        store.update("claim", claim_id, status="active")
    return rcpt


def claim_status_at(store: Store, claim: Claim, known: Optional[str]) -> str:
    """The status a claim had at recording time `known`, rebuilt from relation
    and receipt times. With known=None this is the current status."""
    if known is None:
        return claim.status
    cid = claim.id
    t = changed_at(store, cid, known)
    if t is not None:
        retracted = store.raw("SELECT 1 FROM receipt WHERE kind='review' AND object_ref=? "
                              "AND json_extract(payload,'$.retracted') = 1 AND occurred_at <= ?",
                              (f"claim:{cid}", known))
        return "retracted" if retracted else "superseded"
    contested = store.raw("SELECT 1 FROM claim_relation WHERE relation='contradicts' AND created_at <= ? "
                          "AND (from_claim_id = ? OR to_claim_id = ?)", (known, cid, cid))
    resolved = store.raw("SELECT 1 FROM receipt WHERE kind='review' AND object_ref=? AND occurred_at <= ? "
                         "AND json_extract(payload,'$.resolves_contradiction_with') IS NOT NULL",
                         (f"claim:{cid}", known))
    if contested and not resolved:
        return "contradicted"
    if stale_dependencies(store, cid, known):
        return "stale"
    return "unresolved" if claim.status == "unresolved" else "active"


# ---------------------------------------------------- supersession and conflict


def supersede(store: Store, new_claim_id: str, old_claim_id: str,
              rationale_evidence_id: Optional[str] = None) -> str:
    """Invariant 8: the new claim supersedes the old for the same subject and
    aspect. The old row stays, marked superseded; claims that depend on it
    are marked stale in the same transaction (invariant 18)."""
    new = get_claim(store, new_claim_id)
    old = get_claim(store, old_claim_id)
    if new.subject_id != old.subject_id:
        raise InvariantViolation("invariant 8: supersession cannot cross subjects")
    if new.aspect != old.aspect:
        raise InvariantViolation(
            f"invariant 8: supersession is scoped to one aspect ({old.aspect!r} vs {new.aspect!r})")
    if old.status in ("retracted", "superseded"):
        raise InvariantViolation(f"claim is already {old.status}")
    if AUTHORITY_RANK[new.source_strength] < AUTHORITY_RANK[old.source_strength]:
        raise InvariantViolation(
            f"a '{new.source_strength}' claim cannot supersede a '{old.source_strength}' claim; "
            "record a contradiction instead")
    with store.transaction():
        rid = add_relation(store, new_claim_id, "supersedes", old_claim_id, rationale_evidence_id)
        store.update("claim", old_claim_id, status="superseded")
        _mark_dependents_stale(store, old_claim_id)
    return rid


def correct(store: Store, subject_id: str, old_claim_id: str, evidence_id: str, statement: str,
            value: Any = None, confidence: float = 0.9, **kw: Any) -> str:
    """A correction is a new claim of kind 'correction' plus a supersession.
    The correcting evidence must outrank or equal the old claim's source; a
    lower-authority source does not overwrite, it contradicts."""
    old = get_claim(store, old_claim_id)
    ev = store.get("evidence", evidence_id)
    if AUTHORITY_RANK[ev.authority_tier] < AUTHORITY_RANK[old.source_strength]:
        raise InvariantViolation(
            f"correction from '{ev.authority_tier}' cannot supersede a '{old.source_strength}' claim; "
            "record a contradiction instead")
    with store.transaction():
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
            if c.status in ("active", "stale", "unresolved"):
                store.update("claim", cid, status="contradicted")
    return rid


def resolve_contradiction(store: Store, winner_id: str, loser_id: str, authority_ref: str,
                          rationale_evidence_id: str) -> str:
    """A human resolves a contradiction: the winner supersedes the loser for
    the aspect, backed by evidence, with a review receipt naming who decided.
    If the winner rests on a claim that has since changed, it comes back as
    stale rather than active."""
    from .receipts import add_receipt

    authority.require_human(authority_ref, "resolving a contradiction")
    if not store.exists("evidence", rationale_evidence_id):
        raise InvariantViolation("resolving a contradiction requires evidence")
    w, l = get_claim(store, winner_id), get_claim(store, loser_id)
    if w.status != "contradicted" or l.status != "contradicted":
        raise InvariantViolation("both claims must be in an unresolved contradiction")
    with store.transaction():
        add_receipt(store, "review", f"claim:{winner_id}", authority_ref,
                    {"resolves_contradiction_with": loser_id, "evidence_id": rationale_evidence_id})
        rid = supersede(store, winner_id, loser_id, rationale_evidence_id)
        store.update("claim", winner_id, status="stale" if stale_dependencies(store, winner_id) else "active")
    return rid


def retract(store: Store, claim_id: str, authority_ref: str, rationale_evidence_id: str) -> list[str]:
    """Retraction needs evidence and a named authority; the row stays and a
    review receipt records when. Returns the dependent claims marked stale."""
    from .receipts import add_receipt

    if not store.exists("evidence", rationale_evidence_id):
        raise InvariantViolation("retraction requires evidence")
    with store.transaction():
        add_receipt(store, "review", f"claim:{claim_id}", authority_ref,
                    {"retracted": True, "evidence_id": rationale_evidence_id})
        store.update("claim", claim_id, status="retracted")
        return _mark_dependents_stale(store, claim_id)


# ------------------------------------------------------------------ reads


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
    """The newest claim for an aspect that has not been superseded or retracted, or None."""
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
        answer_claim_ids=[], created_at=now, updated_at=now, recorded_at=store.clock(),  # type: ignore[arg-type]
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
