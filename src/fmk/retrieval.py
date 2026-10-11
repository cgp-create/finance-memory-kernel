"""Retrieval: deterministic candidates, filters, packet assembly and replay.

What this module does: generates candidates by subject match and lexical
overlap over evidence, claims, questions, invalidations, triggers and the
selected revision, all cut at an as_of time; applies the section 7.2
filters in order (retracted out, superseded behind active, authority
before similarity, freshness warnings, contradictions kept visible,
exactly one selected revision, budget by dropping the lowest-ranked
candidates while keeping provenance); and writes a ContextPacket whose
hash covers the ids, hashes, as_of, policy version and runtime block
(invariants 7 and 17). It also renders a packet into text and surfaces
fallback_used in that text.

What this module does not do: it never writes to evidence, claims,
revisions, cases, receipts or decisions. Every function here is a read
except the single INSERT of the packet row. Vector similarity is an
interface (VectorIndex) with a stub that returns nothing; it is not
implemented, and ranking is deterministic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from .models import (
    AUTHORITY_RANK,
    ContextPacket,
    InvariantViolation,
    KernelError,
    content_hash,
    new_id,
    norm_ts,
)
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
        # Section 7.2.3: authority is ordered before similarity.
        return (-self.authority, -round(self.lexical, 6), self.ref)


def _authority_of_claim(store: Store, claim) -> int:
    return AUTHORITY_RANK.get(claim.source_strength, 0)


def generate_candidates(store: Store, subject_id: str, query: str, as_of: str,
                        vector: Optional[VectorIndex] = None) -> list[Candidate]:
    """Section 7.1 without the embedding lane. Everything is cut at as_of."""
    as_of_n = norm_ts(as_of)
    q = tokens(query)
    out: list[Candidate] = []

    def lex(text: str) -> float:
        t = tokens(text)
        return len(q & t) / len(q) if q else 0.0

    for ev in store.query("evidence", "subject_id = ? AND observed_at <= ?", (subject_id, as_of_n), "observed_at"):
        text = " ".join(str(v) for v in ev.payload.values()) + " " + ev.source_kind
        out.append(Candidate(ref=f"evidence:{ev.id}", kind="evidence", lane="lexical", lexical=lex(text),
                             authority=AUTHORITY_RANK[ev.authority_tier], as_of=ev.observed_at,
                             status="active", hash=ev.content_hash,
                             why=[f"{ev.authority_tier} evidence observed {ev.observed_at}"]))
    for c in store.query("claim", "subject_id = ? AND created_at <= ?", (subject_id, as_of_n), "created_at"):
        out.append(Candidate(ref=f"claim:{c.id}", kind="claim", lane="lexical",
                             lexical=lex(c.statement + " " + c.aspect), authority=_authority_of_claim(store, c),
                             as_of=c.created_at, status=c.status, aspect=c.aspect,
                             hash=content_hash({"statement": c.statement, "value": c.value_json, "evidence": c.evidence_id}),
                             why=[f"{c.kind} claim on {c.aspect}, status {c.status}"],
                             extra={"kind": c.kind, "expires_at": c.expires_at, "evidence_id": c.evidence_id}))
    for qst in store.query("research_question", "subject_id = ? AND created_at <= ?", (subject_id, as_of_n)):
        out.append(Candidate(ref=f"question:{qst.id}", kind="question", lane="question", lexical=lex(qst.question),
                             authority=AUTHORITY_RANK["system_measurement"], as_of=qst.created_at,
                             status=qst.status, hash=content_hash(qst.question),
                             why=[f"research question, status {qst.status}"]))
    for inv in store.query("invalidation", "subject_id = ? AND created_at <= ?", (subject_id, as_of_n)):
        out.append(Candidate(ref=f"invalidation:{inv.id}", kind="invalidation", lane="invalidation",
                             lexical=lex(inv.condition), authority=AUTHORITY_RANK["system_measurement"],
                             as_of=inv.created_at, status=inv.status, hash=content_hash(inv.condition),
                             why=[f"invalidation, status {inv.status}"]))
    for trg in store.query("research_trigger", "subject_id = ? AND status = 'active'", (subject_id,)):
        out.append(Candidate(ref=f"trigger:{trg.id}", kind="trigger", lane="trigger", lexical=lex(trg.condition),
                             authority=AUTHORITY_RANK["system_measurement"], as_of=as_of_n or "", status=trg.state,
                             hash=content_hash(trg.condition), why=[f"trigger state {trg.state}"]))
    for rev in store.query("case_set_revision", "subject_id = ? AND created_at <= ? AND status IN ('selected','draft','reviewed')",
                           (subject_id, as_of_n), "created_at"):
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


def apply_filters(store: Store, cands: list[Candidate], as_of: str, policy: FreshnessPolicy,
                  budget: int, factual_query: bool) -> tuple[list[Candidate], list[dict], list[str]]:
    """Section 7.2, in order. Returns (selected, omitted, warnings)."""
    warnings: list[str] = []
    omitted: list[dict] = []
    keep: list[Candidate] = []

    # 1. retracted out
    for c in cands:
        if c.status == "retracted":
            omitted.append({"ref": c.ref, "why": "retracted"})
        else:
            keep.append(c)

    # 2. active before superseded for the same subject/aspect; keep the link
    sup_rows = store.raw("SELECT from_claim_id, to_claim_id FROM claim_relation WHERE relation='supersedes'")
    superseded_by = {f"claim:{r[1]}": f"claim:{r[0]}" for r in sup_rows}
    present = {c.ref for c in keep}
    kept2: list[Candidate] = []
    for c in keep:
        if c.kind == "claim" and c.status == "superseded":
            newer = superseded_by.get(c.ref)
            if newer and newer in present:
                omitted.append({"ref": c.ref, "why": f"superseded by {newer}"})
                continue
            c.why.append("superseded; successor not in window")
        kept2.append(c)
    keep = kept2

    # 3. authority before similarity: a factual query never lets social
    #    chatter outrank primary evidence. rank_key already orders by
    #    authority first; for factual queries low-authority commentary is
    #    additionally pushed out whenever primary evidence is present.
    if factual_query:
        has_primary = any(c.authority >= AUTHORITY_RANK["system_measurement"] and c.kind in ("evidence", "claim") for c in keep)
        if has_primary:
            kept3 = []
            for c in keep:
                if c.kind in ("evidence", "claim") and c.authority <= AUTHORITY_RANK["social_chatter"]:
                    omitted.append({"ref": c.ref, "why": "social_chatter outranked by primary evidence on a factual query"})
                else:
                    kept3.append(c)
            keep = kept3

    # 4. freshness by claim kind: warn, never silently use
    for c in keep:
        if c.kind == "claim":
            claim = store.get("claim", c.ref.split(":", 1)[1])
            verdict, w = policy.verdict(claim, as_of)
            if verdict == "expired":
                c.why.append("EXPIRED; shown for history only, not current")
                c.extra["freshness"] = "expired"
                warnings.append(w)  # type: ignore[arg-type]
            elif verdict == "degraded":
                c.extra["freshness"] = "degraded"
                warnings.append(w)  # type: ignore[arg-type]
            else:
                c.extra["freshness"] = "fresh"

    # 5. unresolved contradictions stay visible
    con_rows = store.raw("SELECT from_claim_id, to_claim_id FROM claim_relation WHERE relation='contradicts'")
    for a, b in con_rows:
        ra, rb = f"claim:{a}", f"claim:{b}"
        if ra in {c.ref for c in keep} and rb in {c.ref for c in keep}:
            ca = store.get("claim", a)
            cb = store.get("claim", b)
            if ca.status == "contradicted" and cb.status == "contradicted":
                warnings.append(f"unresolved contradiction on {ca.aspect}: {ra} vs {rb}")
                for c in keep:
                    if c.ref in (ra, rb):
                        c.why.append("party to an unresolved contradiction; kept visible")
                        c.authority = max(c.authority, AUTHORITY_RANK["system_measurement"])

    # 6. exactly one selected revision; drafts labelled proposals
    selected = [c for c in keep if c.kind == "case_set" and c.status == "selected"]
    if len(selected) > 1:
        raise InvariantViolation("invariant 4: more than one selected revision reached retrieval")
    if not selected and any(c.kind == "case_set" for c in keep):
        warnings.append("no selected underwriting revision in window; only proposals")

    # 7. budget: drop lowest-ranked, never strip provenance
    keep.sort(key=Candidate.rank_key)
    if len(keep) > budget:
        for c in keep[budget:]:
            omitted.append({"ref": c.ref, "why": "over token budget; lower ranked"})
        keep = keep[:budget]
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
                 packet_id: Optional[str] = None) -> ContextPacket:
    """Assemble and persist a replayable packet. The only write is the packet row."""
    pol = policy or FreshnessPolicy()
    rt = validate_runtime(runtime)
    as_of_n = norm_ts(as_of)
    if factual_query is None:
        factual_query = task_kind in ("factual_state", "research_update") and not re.search(
            r"\b(sentiment|opinion|chatter|mood)\b", query.lower())
    cands = generate_candidates(store, subject_id, query, as_of_n, vector)  # type: ignore[arg-type]
    chosen, omitted, warnings = apply_filters(store, cands, as_of_n, pol, budget, factual_query)  # type: ignore[arg-type]
    if rt["fallback_used"]:
        warnings.append(f"MODEL FALLBACK IN USE: {rt.get('fallback_reason', 'unspecified')}")
    subj = store.get("subject", subject_id)
    task = {"kind": task_kind, "subject": subj.key, "query": query}
    selected = [{"ref": c.ref, "why": "; ".join(c.why), "as_of": c.as_of, "hash": c.hash,
                 **({"freshness": c.extra["freshness"]} if "freshness" in c.extra else {})} for c in chosen]
    explanations = [{"ref": c.ref, "lane": c.lane, "authority": c.authority, "lexical": round(c.lexical, 4)} for c in chosen]
    omitted_sorted = sorted(omitted, key=lambda o: (o["ref"], o["why"]))
    warnings_sorted = sorted(set(warnings))
    query_hash = content_hash({"subject": subject_id, "query": query, "as_of": as_of_n})
    body = {"as_of": as_of_n, "policy_version": pol.version, "task": task, "query_hash": query_hash,
            "selected": selected, "omitted": omitted_sorted, "warnings": warnings_sorted, "runtime": rt}
    pkt = ContextPacket(
        id=packet_id or store.new_id("pkt"), task_ref=task_ref or f"{task_kind}:{subj.key}", as_of=as_of_n,  # type: ignore[arg-type]
        policy_version=pol.version, query_hash=query_hash, task=task, selected=selected,
        ranking_explanations=explanations, omitted=omitted_sorted, warnings=warnings_sorted, runtime=rt,
        packet_hash=content_hash(body), created_at=store.clock(),
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
    """Plain text summary of a packet. Pure read; a model would do this better,
    but it would have the same write permissions, which is none."""
    parts = []
    for s in pkt.selected:
        obj = store.get_ref(s["ref"])
        kind = s["ref"].split(":")[0]
        if kind == "claim":
            parts.append(f"- {obj.statement} [{obj.status}{', ' + s['freshness'] if 'freshness' in s else ''}]")
        elif kind == "evidence":
            parts.append(f"- evidence {obj.source_kind} ({obj.authority_tier}) {obj.observed_at}")
        elif kind == "case_set":
            parts.append(f"- {obj.status} revision {obj.id}: {obj.thesis_summary}")
        elif kind == "question":
            parts.append(f"- open question: {obj.question}")
        else:
            parts.append(f"- {s['ref']}: {s['why']}")
    return "\n".join(parts)


def propose_draft_from_packet(store: Store, pkt: ContextPacket, horizon: str, thesis_summary: str,
                              cases: list[dict], proposed_by: str = "model") -> str:
    """What a model is allowed to do with a packet: draft a successor revision.
    Status is 'draft'. Nothing selected is touched (invariant 3)."""
    from .underwriting import draft_revision, selected_revision

    subj_key = pkt.task["subject"]
    subj = store.query("subject", "key = ?", (subj_key,))[0]
    parent = selected_revision(store, subj.id, horizon)
    return draft_revision(store, subj.id, horizon, thesis_summary, cases,
                          parent_id=parent.id if parent else None, proposed_by=proposed_by)
