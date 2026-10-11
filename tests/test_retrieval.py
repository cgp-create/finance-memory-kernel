"""Tests 8 (authority before similarity) and 9 (future leakage), plus
the section 7.2 filters: retracted out, superseded behind active, budget
without stripping provenance, drafts labelled as proposals."""
from conftest import RUNTIME, cases_for
from fmk import claims, evidence, retrieval, underwriting


def _social_vs_primary(store, sid):
    ev_p = evidence.add_evidence(store, sid, "regulatory_filing", "synthetic://ACME/filing", "2026-10-02T08:00:00Z",
                                 "primary", {"text": "quarterly revenue 240; cash 95"})
    # worded differently from the query on purpose: it should still win
    c_p = claims.add_claim(store, sid, ev_p, "financials.revenue", "fact", "Revenue for the period: 240", value=240,
                           created_at="2026-10-02T08:05:00Z")
    # the social post repeats the query words almost verbatim
    ev_s = evidence.add_evidence(store, sid, "social_post", "synthetic://forum/ACME", "2026-10-03T08:00:00Z",
                                 "social_chatter", {"text": "what was the quarterly revenue? quarterly revenue was huge, quarterly revenue to the moon"})
    c_s = claims.add_claim(store, sid, ev_s, "financials.revenue", "hypothesis",
                           "What was the quarterly revenue? Quarterly revenue was huge", confidence=0.1,
                           created_at="2026-10-03T08:05:00Z")
    return ev_p, c_p, ev_s, c_s


# Test 8: authority before similarity.
def test_authority_before_similarity_on_factual_query(store, acme):
    sid = acme["subject"]
    ev_p, c_p, ev_s, c_s = _social_vs_primary(store, sid)
    pkt = retrieval.build_packet(store, sid, "what was the quarterly revenue", "2026-10-05T00:00:00Z", RUNTIME,
                                 task_kind="factual_state")
    refs = retrieval.packet_refs(pkt)
    assert f"evidence:{ev_p}" in refs and f"claim:{c_p}" in refs
    assert f"claim:{c_s}" not in refs and f"evidence:{ev_s}" not in refs
    omitted = {o["ref"]: o["why"] for o in pkt.omitted}
    assert "outranked by primary" in omitted[f"claim:{c_s}"]
    # the social post was the closer lexical match, and still lost
    lex = {c.ref: c.lexical for c in retrieval.generate_candidates(store, sid, "what was the quarterly revenue",
                                                                   "2026-10-05T00:00:00Z")}
    assert lex[f"claim:{c_s}"] > lex[f"claim:{c_p}"]
    # and ranking is authority first
    auth = [e["authority"] for e in pkt.ranking_explanations]
    assert auth == sorted(auth, reverse=True)


def test_sentiment_query_keeps_social_but_ranks_primary_first(store, acme):
    sid = acme["subject"]
    ev_p, c_p, ev_s, c_s = _social_vs_primary(store, sid)
    pkt = retrieval.build_packet(store, sid, "what is the chatter sentiment on quarterly revenue",
                                 "2026-10-05T00:00:00Z", RUNTIME)
    refs = retrieval.packet_refs(pkt)
    assert f"claim:{c_s}" in refs  # the question asked for chatter
    assert refs.index(f"evidence:{ev_p}") < refs.index(f"claim:{c_s}")


# Test 9: future leakage.
def test_future_evidence_cannot_enter_earlier_packet(store, acme):
    sid = acme["subject"]
    ev_late = evidence.add_evidence(store, sid, "ir_release", "synthetic://ACME/later", "2026-10-08T12:00:00Z",
                                    "primary", {"text": "FY revenue guidance cut again to 900"})
    c_late = claims.add_claim(store, sid, ev_late, "guidance.fy_revenue", "fact", "guidance cut to 900",
                              created_at="2026-10-08T12:05:00Z")
    claims.supersede(store, c_late, acme["claim"], ev_late)
    rid = underwriting.draft_revision(store, sid, "12m", "later proposal", cases_for(ev_late),
                                      created_at="2026-10-09T00:00:00Z", proposed_by="model:assistant")
    q = claims.open_question(store, sid, "why the second cut", "call transcript", created_at="2026-10-08T13:00:00Z")
    early = retrieval.build_packet(store, sid, "FY revenue guidance", "2026-10-05T00:00:00Z", RUNTIME)
    refs = retrieval.packet_refs(early)
    for leaked in (f"evidence:{ev_late}", f"claim:{c_late}", f"case_set:{rid}", f"question:{q}"):
        assert leaked not in refs
        assert leaked not in {o["ref"] for o in early.omitted}  # not even mentioned as omitted
    # the old claim is superseded in storage, but inside the early window it is the current one
    assert f"claim:{acme['claim']}" in refs
    assert all(s["as_of"] <= "2026-10-05T00:00:00Z" for s in early.selected)
    late = retrieval.build_packet(store, sid, "FY revenue guidance", "2026-10-09T01:00:00Z", RUNTIME)
    assert f"claim:{c_late}" in retrieval.packet_refs(late)
    assert {o["ref"]: o["why"] for o in late.omitted}[f"claim:{acme['claim']}"].startswith("superseded by")


def test_retracted_claims_are_omitted(store, acme):
    sid = acme["subject"]
    claims.retract(store, acme["claim"], "reviewer:pm", acme["evidence"])
    pkt = retrieval.build_packet(store, sid, "FY revenue guidance", "2026-10-05T00:00:00Z", RUNTIME)
    assert f"claim:{acme['claim']}" not in retrieval.packet_refs(pkt)
    assert {o["ref"]: o["why"] for o in pkt.omitted}[f"claim:{acme['claim']}"] == "retracted"


def test_budget_drops_lowest_ranked_and_keeps_provenance(store, acme):
    sid = acme["subject"]
    for i in range(6):
        claims.add_claim(store, sid, acme["evidence"], f"misc.item_{i}", "inference", f"minor note {i}",
                         source_strength="assistant_inference", created_at="2026-10-01T12:06:00Z")
    pkt = retrieval.build_packet(store, sid, "FY revenue guidance", "2026-10-05T00:00:00Z", RUNTIME, budget=3)
    assert len(pkt.selected) == 3
    assert all(s["hash"] and s["as_of"] and s["why"] for s in pkt.selected)
    over = [o for o in pkt.omitted if o["why"].startswith("over budget")]
    assert len(over) == 8 - 3
    # primary evidence and the primary-backed claim survive the budget cut
    assert f"evidence:{acme['evidence']}" in retrieval.packet_refs(pkt)
    assert f"claim:{acme['claim']}" in retrieval.packet_refs(pkt)


def test_drafts_are_labelled_proposals(store, acme):
    sid = acme["subject"]
    rid = underwriting.draft_revision(store, sid, "12m", "proposal only", cases_for(acme["evidence"]),
                                      created_at="2026-10-02T00:00:00Z", proposed_by="model:assistant")
    pkt = retrieval.build_packet(store, sid, "current underwriting", "2026-10-05T00:00:00Z", RUNTIME)
    sel = {s["ref"]: s for s in pkt.selected}
    assert "proposal, not underwriting" in sel[f"case_set:{rid}"]["why"]
    assert any("no selected underwriting revision" in w for w in pkt.warnings)


def test_vector_lane_is_a_stub(store, acme):
    idx = retrieval.NoVectorIndex()
    assert idx.similar("anything", "2026-10-05T00:00:00Z", 10) == []
    pkt = retrieval.build_packet(store, acme["subject"], "guidance", "2026-10-05T00:00:00Z", RUNTIME, vector=idx)
    assert all("vector" not in s["why"] for s in pkt.selected)


def test_runtime_policy_version_travels_with_packet(store, acme):
    pkt = retrieval.build_packet(store, acme["subject"], "guidance", "2026-10-05T00:00:00Z", RUNTIME)
    assert pkt.policy_version == "v1" and pkt.runtime["policy_version"] == "v1"
    assert pkt.runtime["fallback_used"] is False
