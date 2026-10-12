"""Retrieval: deterministic candidates, filters, packet assembly and replay.

Generates candidates by subject match and lexical overlap over evidence,
claims, questions, invalidations, triggers and revisions, cut at `as_of`
(valid time: what was true of the world by then). Pass `known_as_of`
(transaction time: what the store had recorded by then) to rebuild a
packet as it would have been built at that moment, with later backfills
and later supersessions left out.

Filters run in a fixed order (SPEC 7.2): retracted out, superseded behind
its successor, superseded evidence behind its correction, authority before
similarity, freshness labels, stale dependencies flagged, contradictions
kept visible, one selected revision, then a budget that drops the
lowest-ranked objects but never a contradiction party or the selected
revision. The packet hash covers ids, hashes, both cut times, policy
version and the runtime block (invariants 7 and 17).

The only write here is the packet row. Vector similarity is an interface
(VectorIndex) whose shipped implementation returns nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .models import (
    AUTHORITY_RANK,
    ContextPacket,
    InvariantViolation,
    content_hash,
    norm_ts,
)
from .claims import claim_status_at, stale_dependencies
from .underwriting import trigger_state_at
from .policy import POLICY_VERSION, FreshnessPolicy
from .store import Store

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "is", "to", "in", "and", "or", "for", "on", "what", "did", "does",
         "current", "now", "latest", "we", "our", "do", "be", "it", "this", "that", "with", "at", "by"}


def tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP and len(t) > 1}


class VectorIndex(Protocol):
    """Interface for an embedding lane. Not implemented in this repository."""

    def similar(self, query: str, as_of: str, limit: int) -> list[tuple[str, float]]:
        """Return (object_ref, score) pairs. Must respect as_of."""


class NoVectorIndex:
    """The only VectorIndex shipped here. It returns nothing, so the packet
    contains only lexical and entity candidates."""

    def similar(self, query: str, as_of: str, limit: int) -> list[tuple[str, float]]:
        return []


@dataclass
class Candidate:
    ref: str
    kind: str  # evidence | claim | case_set | question | invalidation | trigger
    lane: str  # subject | lexical | underwriting | question | invalidation | trigger
    lexical: float
    authority: int
    as_of: str
    status: str
    aspect: str = ""
    why: list[str] = field(default_factory=list)
    hash: str = ""
    extra: dict = field(default_factory=dict)

    def rank_key(self) -> tuple:
        # SPEC 7.2 step 4: authority is ordered before similarity.
        return (-self.authority, -round(self.lexical, 6), self.ref)


def _authority_of_claim(store: Store, claim) -> int:
    return AUTHORITY_RANK.get(claim.source_strength, 0)


def generate_candidates(store: Store, subject_id: str, query: str, as_of: str,
                        vector: Optional[VectorIndex] = None,
                        known_as_of: Optional[str] = None) -> list[Candidate]:
    """SPEC 7.1 without the embedding lane. Everything is cut at as_of,
    and at known_as_of by recording time when it is given."""
    as_of_n = norm_ts(as_of)
    known = norm_ts(known_as_of) or "9999-12-31T23:59:59Z"
    q = tokens(query)
    out: list[Candidate] = []

    def lex(text: str) -> float:
        t = tokens(text)
        return len(q & t) / len(q) if q else 0.0

    for ev in store.query("evidence", "subject_id = ? AND observed_at <= ? AND ingested_at <= ?",
                          (subject_id, as_of_n, known), "observed_at"):
        text = " ".join(str(v) for v in ev.payload.values()) + " " + ev.source_kind
        out.append(Candidate(ref=f"evidence:{ev.id}", kind="evidence", lane="lexical", lexical=lex(text),
                             authority=AUTHORITY_RANK[ev.authority_tier], as_of=ev.observed_at,
                             status="active", hash=ev.content_hash,
                             why=[f"{ev.authority_tier} evidence observed {ev.observed_at}"],
                             extra={"supersedes_evidence_id": ev.supersedes_evidence_id}))
    for c in store.query("claim", "subject_id = ? AND created_at <= ? AND recorded_at <= ?",
                         (subject_id, as_of_n, known), "created_at"):
        basis = c.valid_from or store.get("evidence", c.evidence_id).observed_at
        c.status = claim_status_at(store, c, known_as_of and known)
        out.append(Candidate(ref=f"claim:{c.id}", kind="claim", lane="lexical",
                             lexical=lex(c.statement + " " + c.aspect), authority=_authority_of_claim(store, c),
                             as_of=c.created_at, status=c.status, aspect=c.aspect,
                             hash=content_hash({"statement": c.statement, "value": c.value_json, "evidence": c.evidence_id}),
                             why=[f"{c.kind} claim on {c.aspect}, status {c.status}"],
                             extra={"kind": c.kind, "expires_at": c.expires_at, "evidence_id": c.evidence_id,
                                    "basis": basis}))
    for qst in store.query("research_question", "subject_id = ? AND created_at <= ? AND recorded_at <= ?",
                           (subject_id, as_of_n, known)):
        out.append(Candidate(ref=f"question:{qst.id}", kind="question", lane="question", lexical=lex(qst.question),
                             authority=AUTHORITY_RANK["system_measurement"], as_of=qst.created_at,
                             status=qst.status, hash=content_hash(qst.question),
                             why=[f"research question, status {qst.status}"]))
    for inv in store.query("invalidation", "subject_id = ? AND created_at <= ? AND recorded_at <= ?",
                           (subject_id, as_of_n, known)):
        out.append(Candidate(ref=f"invalidation:{inv.id}", kind="invalidation", lane="invalidation",
                             lexical=lex(inv.condition), authority=AUTHORITY_RANK["system_measurement"],
                             as_of=inv.created_at, status=inv.status, hash=content_hash(inv.condition),
                             why=[f"invalidation, status {inv.status}"]))
    for trg in store.query("research_trigger", "subject_id = ? AND status = 'active' AND created_at <= ? "
                           "AND recorded_at <= ?", (subject_id, as_of_n, known)):
        state = trigger_state_at(store, trg.id, known_as_of and known)
        out.append(Candidate(ref=f"trigger:{trg.id}", kind="trigger", lane="trigger", lexical=lex(trg.condition),
                             authority=AUTHORITY_RANK["system_measurement"], as_of=trg.created_at, status=state,
                             hash=content_hash(trg.condition), why=[f"trigger state {state}"]))
    for rev in store.query("case_set_revision", "subject_id = ? AND created_at <= ? AND recorded_at <= ?",
                           (subject_id, as_of_n, known), "created_at"):
        rev.status = _revision_status_at(store, rev, known_as_of and known)
        if rev.status == "retired":
            continue
        label = "selected underwriting revision" if rev.status == "selected" else f"{rev.status} revision (proposal, not underwriting)"
        out.append(Candidate(ref=f"case_set:{rev.id}", kind="case_set", lane="underwriting", lexical=lex(rev.thesis_summary),
                             authority=AUTHORITY_RANK["direct_user"] if rev.status == "selected" else AUTHORITY_RANK["assistant_inference"],
                             as_of=rev.created_at, status=rev.status,
                             hash=content_hash([rev.assumptions_hash, rev.evidence_hash]),
                             why=[label], extra={"horizon": rev.horizon}))
    if vector is not None:
        for ref, score in vector.similar(query, as_of_n or "", 20):  # pragma: no cover - stub returns nothing
            for c in out:
                if c.ref == ref:
                    c.why.append(f"vector lane score {score:.3f}")
    return out


def _relations(store: Store, relation: str, known: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in store.raw(
        "SELECT from_claim_id, to_claim_id FROM claim_relation WHERE relation = ? AND created_at <= ?",
        (relation, known))]


def _revision_status_at(store: Store, rev, known: Optional[str]) -> str:
    """The status a revision had at recording time `known` (current status if None)."""
    if known is None:
        return rev.status
    if rev.retired_at and rev.retired_at <= known:
        return "retired"
    if rev.selected_at and rev.selected_at <= known:
        return "selected"
    if rev.review_receipt_id and store.get("receipt", rev.review_receipt_id).occurred_at <= known:
        return "reviewed"
    return "draft"


def apply_filters(store: Store, cands: list[Candidate], as_of: str, policy: FreshnessPolicy,
                  budget: int, factual_query: bool,
                  known_as_of: Optional[str] = None) -> tuple[list[Candidate], list[dict], list[str]]:
    """SPEC 7.2, in order. Returns (selected, omitted, warnings)."""

    known = norm_ts(known_as_of) or "9999-12-31T23:59:59Z"
    warnings: list[str] = []
    omitted: list[dict] = []

    # 1. retracted out
    keep: list[Candidate] = []
    for c in cands:
        if c.status == "retracted":
            omitted.append({"ref": c.ref, "why": "retracted"})
        else:
            keep.append(c)

    # 2a. active before superseded for the same subject and aspect; keep the link
    superseded_by = {f"claim:{old}": f"claim:{new}" for new, old in _relations(store, "supersedes", known)}
    present = {c.ref for c in keep}
    kept: list[Candidate] = []
    for c in keep:
        if c.kind == "claim" and c.status == "superseded":
            newer = superseded_by.get(c.ref)
            if newer and newer in present:
                omitted.append({"ref": c.ref, "why": f"superseded by {newer}"})
                continue
            c.why.append("superseded; successor not in window")
            warnings.append(f"{c.ref} is superseded and its successor is not in the window")
        kept.append(c)
    keep = kept

    # 2b. evidence corrected by later evidence in the window
    corrected = {f"evidence:{c.extra['supersedes_evidence_id']}": c.ref
                 for c in keep if c.kind == "evidence" and c.extra.get("supersedes_evidence_id")}
    kept = []
    for c in keep:
        if c.kind == "evidence" and c.ref in corrected:
            omitted.append({"ref": c.ref, "why": f"superseded by {corrected[c.ref]}"})
            continue
        kept.append(c)
    keep = kept

    # 3. authority before similarity: on a factual query, social chatter is
    #    left out whenever primary or system evidence is present.
    if factual_query:
        has_primary = any(c.authority >= AUTHORITY_RANK["system_measurement"] and c.kind in ("evidence", "claim") for c in keep)
        if has_primary:
            kept = []
            for c in keep:
                if c.kind in ("evidence", "claim") and c.authority <= AUTHORITY_RANK["social_chatter"]:
                    omitted.append({"ref": c.ref, "why": "social_chatter outranked by primary evidence on a factual query"})
                else:
                    kept.append(c)
            keep = kept

    # 4. freshness, measured from when the evidence was observed: warn, never silently use
    for c in keep:
        if c.kind == "claim":
            claim = store.get("claim", c.ref.split(":", 1)[1])
            verdict, w = policy.verdict(claim, as_of, basis=c.extra.get("basis"))
            c.extra["freshness"] = verdict
            if verdict == "expired":
                c.why.append("EXPIRED; shown for history only, not current")
            if w:
                warnings.append(w)

    # 5. stale dependencies: a claim resting on a superseded or retracted claim
    for c in keep:
        if c.kind == "claim":
            cid = c.ref.split(":", 1)[1]
            deps = stale_dependencies(store, cid, known_as_of and known)
            if c.status == "stale" or deps:
                c.why.append("rests on a claim that changed; refresh before use")
                c.extra["freshness"] = "dependency_stale"
                warnings.append(f"{c.ref} rests on " + ", ".join(f"claim:{d}" for d in deps or ["(changed claim)"])
                                + "; refresh or reaffirm before use")

    # 6. unresolved contradictions stay visible, both sides, whatever their rank
    refs = {c.ref for c in keep}
    for a, b in _relations(store, "contradicts", known):
        ra, rb = f"claim:{a}", f"claim:{b}"
        if ra in refs and rb in refs:
            ca = next(c for c in keep if c.ref == ra)
            cb = next(c for c in keep if c.ref == rb)
            if ca.status == "contradicted" and cb.status == "contradicted":
                warnings.append(f"unresolved contradiction on {ca.aspect}: {ra} vs {rb}")
                for c in (ca, cb):
                    c.why.append("party to an unresolved contradiction; kept visible")
                    c.extra["pinned"] = True

    # 7. exactly one selected revision; drafts labelled proposals
    selected = [c for c in keep if c.kind == "case_set" and c.status == "selected"]
    if len(selected) > 1:
        raise InvariantViolation("invariant 4: more than one selected revision reached retrieval")
    for c in selected:
        c.extra["pinned"] = True
    if not selected and any(c.kind == "case_set" for c in keep):
        warnings.append("no selected underwriting revision in window; only proposals")

    # 8. budget: pinned objects first, then by rank; never strip provenance
    keep.sort(key=Candidate.rank_key)
    pinned = [c for c in keep if c.extra.get("pinned")]
    rest = [c for c in keep if not c.extra.get("pinned")]
    room = max(budget - len(pinned), 0)
    for c in rest[room:]:
        omitted.append({"ref": c.ref, "why": "over budget; lower ranked"})
    keep = sorted(pinned + rest[:room], key=Candidate.rank_key)
    return keep, omitted, warnings


def validate_runtime(runtime: dict) -> dict:
    """Invariant 17."""
    if not isinstance(runtime, dict):
        raise InvariantViolation("invariant 17: packet runtime block is required")
    for k in ("model_id", "model_version", "provider", "route_id"):
        if not runtime.get(k):
            raise InvariantViolation(f"invariant 17: packet runtime missing {k}")
    if not isinstance(runtime.get("fallback_used"), bool):
        raise InvariantViolation("invariant 17: runtime.fallback_used must be true or false, not null")
    rt = dict(runtime)
    rt.setdefault("policy_version", POLICY_VERSION)
    rt.setdefault("tool_versions", {})
    rt.setdefault("source_feed_as_of", {})
    return rt


def build_packet(store: Store, subject_id: str, query: str, as_of: str, runtime: dict,
                 task_kind: str = "research_update", task_ref: Optional[str] = None,
                 budget: int = 12, policy: Optional[FreshnessPolicy] = None,
                 vector: Optional[VectorIndex] = None, factual_query: Optional[bool] = None,
                 packet_id: Optional[str] = None, known_as_of: Optional[str] = None) -> ContextPacket:
    """Assemble and persist a replayable packet. The only write is the packet row.

    `budget` is a count of objects, not tokens."""
    pol = policy or FreshnessPolicy()
    rt = validate_runtime(runtime)
    as_of_n = norm_ts(as_of)
    if factual_query is None:
        factual_query = task_kind in ("factual_state", "research_update") and not re.search(
            r"\b(sentiment|opinion|chatter|mood)\b", query.lower())
    known_n = norm_ts(known_as_of)
    cands = generate_candidates(store, subject_id, query, as_of_n, vector, known_n)  # type: ignore[arg-type]
    chosen, omitted, warnings = apply_filters(store, cands, as_of_n, pol, budget, factual_query, known_n)  # type: ignore[arg-type]
    if rt["fallback_used"]:
        warnings.append(f"MODEL FALLBACK IN USE: {rt.get('fallback_reason', 'unspecified')}")
    subj = store.get("subject", subject_id)
    task = {"kind": task_kind, "subject": subj.key, "query": query}
    selected = [{"ref": c.ref, "why": "; ".join(c.why), "as_of": c.as_of, "hash": c.hash, "status": c.status,
                 **({"freshness": c.extra["freshness"]} if "freshness" in c.extra else {})} for c in chosen]
    explanations = [{"ref": c.ref, "lane": c.lane, "authority": c.authority, "lexical": round(c.lexical, 4)} for c in chosen]
    omitted_sorted = sorted(omitted, key=lambda o: (o["ref"], o["why"]))
    warnings_sorted = sorted(set(warnings))
    query_hash = content_hash({"subject": subject_id, "query": query, "as_of": as_of_n, "known_as_of": known_n})
    body = {"as_of": as_of_n, "known_as_of": known_n, "policy_version": pol.version, "task": task, "query_hash": query_hash,
            "selected": selected, "omitted": omitted_sorted, "warnings": warnings_sorted, "runtime": rt}
    pkt = ContextPacket(
        id=packet_id or store.new_id("pkt"), task_ref=task_ref or f"{task_kind}:{subj.key}", as_of=as_of_n,  # type: ignore[arg-type]
        policy_version=pol.version, query_hash=query_hash, task=task, selected=selected,
        ranking_explanations=explanations, omitted=omitted_sorted, warnings=warnings_sorted, runtime=rt,
        packet_hash=content_hash(body), created_at=store.clock(), known_as_of=known_n,
    )
    store.insert(pkt)
    return pkt


def get_packet(store: Store, packet_id: str) -> ContextPacket:
    return store.get("context_packet", packet_id)


def packet_refs(pkt: ContextPacket) -> list[str]:
    return [s["ref"] for s in pkt.selected]


def render_output(pkt: ContextPacket, body_text: str) -> str:
    """Delivered text. A fallback flag reaches the output, not only the log.
    The output states that the packet is a selection, not everything."""
    lines: list[str] = []
    if pkt.runtime.get("fallback_used"):
        flag = next((w for w in pkt.warnings if w.startswith("MODEL FALLBACK")), "MODEL FALLBACK IN USE")
        lines.append(f"[{flag}]")
    lines.append(body_text)
    if pkt.warnings:
        lines.append("Warnings: " + " | ".join(pkt.warnings))
    lines.append(f"Context: {len(pkt.selected)} objects selected, {len(pkt.omitted)} omitted; "
                 f"as_of {pkt.as_of}; policy {pkt.policy_version}; packet {pkt.packet_hash[:12]}. "
                 "This packet is a selection, not an exhaustive record.")
    return "\n".join(lines)


def summarize(store: Store, pkt: ContextPacket) -> str:
    """Plain text summary of a packet. Pure read.

    Statuses are the ones the packet recorded when it was built, so a saved
    packet reads the same after later retractions or supersessions. The text
    shown (statements, theses, questions) is frozen by the schema. Packets
    from before v0.3 did not record status; for those the current status is
    shown and marked as such."""
    parts = []
    for s in pkt.selected:
        obj = store.get_ref(s["ref"])
        kind = s["ref"].split(":")[0]
        status = s["status"] if "status" in s else f"{obj.status}, current"
        if kind == "claim":
            parts.append(f"- {obj.statement} [{status}{', ' + s['freshness'] if 'freshness' in s else ''}]")
        elif kind == "evidence":
            parts.append(f"- evidence {obj.source_kind} ({obj.authority_tier}) {obj.observed_at}")
        elif kind == "case_set":
            parts.append(f"- {status} revision {obj.id}: {obj.thesis_summary}")
        elif kind == "question":
            parts.append(f"- question [{status}]: {obj.question}")
        else:
            parts.append(f"- {s['ref']}: {s['why']}")
    return "\n".join(parts)


def propose_draft_from_packet(store: Store, pkt: ContextPacket, horizon: str, thesis_summary: str,
                              cases: list[dict], proposed_by: str = "model:assistant") -> str:
    """What a model is allowed to do with a packet: draft a successor revision.
    Status is 'draft'. Nothing selected is touched (invariant 3)."""
    from .underwriting import draft_revision, selected_revision

    subj_key = pkt.task["subject"]
    subj = store.query("subject", "key = ?", (subj_key,))[0]
    parent = selected_revision(store, subj.id, horizon)
    return draft_revision(store, subj.id, horizon, thesis_summary, cases,
                          parent_id=parent.id if parent else None, proposed_by=proposed_by)
