"""Canonical required tests 1, 2, 3, 4 and 6, plus direct checks on invariants 1, 2, 8, 11."""
import sqlite3
import threading

import pytest

from conftest import RUNTIME, cases_for
from fmk import claims, evidence, receipts, retrieval, underwriting
from fmk.models import InvariantViolation
from fmk.store import Store


# Invariant 1: a claim needs evidence.
def test_claim_requires_existing_evidence(store, acme):
    with pytest.raises(InvariantViolation, match="invariant 1"):
        claims.add_claim(store, acme["subject"], "ev_does_not_exist", "x", "fact", "unsupported")


# Invariant 2: evidence is immutable, even via raw SQL.
def test_evidence_immutable(store, acme):
    with pytest.raises(sqlite3.IntegrityError, match="invariant 2"):
        store.db.execute("UPDATE evidence SET payload = '{}' WHERE id = ?", (acme["evidence"],))
    with pytest.raises(sqlite3.IntegrityError, match="invariant 2"):
        store.db.execute("DELETE FROM evidence WHERE id = ?", (acme["evidence"],))
    assert evidence.verify_hash(evidence.get_evidence(store, acme["evidence"]))


# Test 1: correction supersession keeps history.
def test_correction_supersession(store, acme):
    sid = acme["subject"]
    ev_inf = evidence.add_evidence(store, sid, "assistant_note", "synthetic://assistant/shares", "2026-09-20T10:00:00Z",
                                   "assistant_inference", {"text": "about 210m shares"})
    old = claims.add_claim(store, sid, ev_inf, "capital.diluted_shares", "inference", "about 210m shares", value=210)
    unrelated = claims.add_claim(store, sid, ev_inf, "capital.net_debt", "inference", "net debt 300m", value=300)
    ev_fil = evidence.add_evidence(store, sid, "regulatory_filing", "synthetic://ACME/q3", "2026-10-02T08:00:00Z",
                                   "primary", {"text": "198.4m diluted shares"})
    new = claims.correct(store, sid, old, ev_fil, "198.4m diluted shares per filing", value=198.4)
    assert claims.get_claim(store, old).status == "superseded"
    assert claims.get_claim(store, old).statement == "about 210m shares"  # history intact
    assert claims.get_claim(store, new).status == "active"
    assert claims.get_claim(store, unrelated).status == "active"  # invariant 8: scoped
    rels = claims.relations_of(store, old)
    assert [(r.from_claim_id, r.relation, r.to_claim_id) for r in rels] == [(new, "supersedes", old)]
    assert claims.current_claim(store, sid, "capital.diluted_shares").id == new


def test_lower_authority_cannot_correct(store, acme):
    sid = acme["subject"]
    ev_social = evidence.add_evidence(store, sid, "social_post", "synthetic://forum/x", "2026-10-03T08:00:00Z",
                                      "social_chatter", {"text": "guide is actually 800"})
    with pytest.raises(InvariantViolation, match="cannot supersede"):
        claims.correct(store, sid, acme["claim"], ev_social, "guide 800")


# Invariant 8: supersession cannot cross aspects or subjects.
def test_supersession_scoped_to_subject_and_aspect(store, acme):
    sid = acme["subject"]
    other = claims.add_claim(store, sid, acme["evidence"], "guidance.op_margin", "fact", "margin 15 to 16")
    with pytest.raises(InvariantViolation, match="invariant 8"):
        claims.supersede(store, other, acme["claim"])
    sid2 = evidence.add_subject(store, "SUBJ_B", "other")
    ev2 = evidence.add_evidence(store, sid2, "ir_release", "synthetic://B", "2026-10-01T00:00:00Z", "primary", {})
    c2 = claims.add_claim(store, sid2, ev2, "guidance.fy_revenue", "fact", "B guide")
    with pytest.raises(InvariantViolation, match="invariant 8"):
        claims.supersede(store, c2, acme["claim"])


# Test 2: conflict preservation.
def test_conflict_preservation(store, acme):
    sid = acme["subject"]
    ev_a = evidence.add_evidence(store, sid, "sell_side_note", "synthetic://a", "2026-10-02T09:00:00Z",
                                 "credible_secondary", {"text": "margin positive"})
    ev_b = evidence.add_evidence(store, sid, "channel_check", "synthetic://b", "2026-10-03T09:00:00Z",
                                 "credible_secondary", {"text": "margin negative"})
    a = claims.add_claim(store, sid, ev_a, "unit_economics.cm", "forecast", "contribution margin positive", value=12,
                         created_at="2026-10-02T09:05:00Z")
    b = claims.add_claim(store, sid, ev_b, "unit_economics.cm", "forecast", "contribution margin negative", value=-3,
                         created_at="2026-10-03T09:05:00Z")
    claims.contradict(store, b, a)
    assert claims.get_claim(store, a).status == "contradicted"
    assert claims.get_claim(store, b).status == "contradicted"
    assert claims.unresolved_contradictions(store, sid) == [(b, a)]
    pkt = retrieval.build_packet(store, sid, "what is the contribution margin", "2026-10-05T00:00:00Z", RUNTIME)
    refs = retrieval.packet_refs(pkt)
    assert f"claim:{a}" in refs and f"claim:{b}" in refs
    assert any("unresolved contradiction" in w for w in pkt.warnings)
    # similarity is not an authority
    with pytest.raises(InvariantViolation):
        claims.resolve_contradiction(store, b, a, "similarity:0.93", ev_b)
    # an authorised, evidence-backed resolution works and leaves history
    ev_c = evidence.add_evidence(store, sid, "regulatory_filing", "synthetic://c", "2026-10-04T09:00:00Z", "primary", {})
    claims.resolve_contradiction(store, b, a, "direct_user:owner", ev_c)
    assert claims.get_claim(store, a).status == "superseded"
    assert claims.get_claim(store, b).status == "active"
    assert claims.unresolved_contradictions(store, sid) == []


# Test 3: freshness.
def test_expired_price_needs_warning(store, acme):
    sid = acme["subject"]
    ev = evidence.add_evidence(store, sid, "market_data", "synthetic://feed/last", "2026-10-09T15:59:00Z",
                               "system_measurement", {"last": 48.2})
    c = claims.add_claim(store, sid, ev, "price.last", "fact", "last price 48.2", value=48.2,
                         created_at="2026-10-09T15:59:30Z")
    assert claims.get_claim(store, c).expires_at == "2026-10-09T16:14:30Z"  # 15 minute clock from policy
    pkt = retrieval.build_packet(store, sid, "last price", "2026-10-10T09:00:00Z", RUNTIME)
    sel = {s["ref"]: s for s in pkt.selected}
    assert sel[f"claim:{c}"]["freshness"] == "expired"
    assert any("expired" in w for w in pkt.warnings)
    fresh = retrieval.build_packet(store, sid, "last price", "2026-10-09T16:05:00Z", RUNTIME)
    assert {s["ref"]: s for s in fresh.selected}[f"claim:{c}"]["freshness"] == "fresh"


def test_user_principle_does_not_expire(store, acme):
    ev = evidence.add_evidence(store, acme["subject"], "conversation", "synthetic://conv/1", "2026-10-01T00:00:00Z",
                               "direct_user", {"text": "never more than 10 percent in one name"})
    c = claims.add_claim(store, acme["subject"], ev, "principle.concentration", "user_decision", "max 10 percent per name")
    assert claims.get_claim(store, c).expires_at is None


# Test 4: selected-revision uniqueness, including a concurrent double select.
def test_selected_revision_uniqueness(store, acme):
    sid = acme["subject"]
    a = underwriting.draft_revision(store, sid, "12m", "A", cases_for(acme["evidence"]))
    b = underwriting.draft_revision(store, sid, "12m", "B", cases_for(acme["evidence"], 0.2, 0.5, 0.3))
    underwriting.review_revision(store, a, "reviewer:pm")
    underwriting.review_revision(store, b, "reviewer:pm")
    underwriting.select_revision(store, a, "direct_user:owner")
    with pytest.raises(InvariantViolation, match="invariant 4"):
        underwriting.select_revision(store, b, "direct_user:owner", retire_current=False)
    # raw SQL cannot bypass the partial unique index either
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE case_set_revision SET status='selected' WHERE id=?", (b,))
    assert underwriting.selected_revision(store, sid, "12m").id == a
    # replacing is allowed and retires the old one
    underwriting.select_revision(store, b, "direct_user:owner")
    assert underwriting.selected_revision(store, sid, "12m").id == b
    assert underwriting.get_revision(store, a).status == "retired"


def test_concurrent_double_select(tmp_path):
    path = str(tmp_path / "k.sqlite")
    s = Store(path)
    sid = evidence.add_subject(s, "ACME", "ACME")
    ev = evidence.add_evidence(s, sid, "ir_release", "synthetic://x", "2026-10-01T00:00:00Z", "primary", {})
    revs = [underwriting.draft_revision(s, sid, "12m", f"R{i}", cases_for(ev)) for i in range(4)]
    for r in revs:
        underwriting.review_revision(s, r, "reviewer:pm")
    s.close()
    results: list = []
    barrier = threading.Barrier(4)

    def worker(rid):
        st = Store(path, timeout=10)
        barrier.wait()
        try:
            underwriting.select_revision(st, rid, "direct_user:owner", retire_current=False)
            results.append(("ok", rid))
        except InvariantViolation as e:
            results.append(("refused", str(e)))
        finally:
            st.close()

    threads = [threading.Thread(target=worker, args=(r,)) for r in revs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = Store(path)
    selected = st.query("case_set_revision", "subject_id=? AND horizon='12m' AND status='selected'", (sid,))
    assert len(selected) == 1
    assert sum(1 for r in results if r[0] == "ok") == 1
    assert sum(1 for r in results if r[0] == "refused") == 3
    # exactly one selection receipt exists; the refused attempts rolled theirs back
    assert len(st.query("receipt", "kind='selection'")) == 1


# Invariant 5: no selection without review and receipt.
def test_selection_requires_review_and_receipt(store, acme):
    rid = underwriting.draft_revision(store, acme["subject"], "12m", "A", cases_for(acme["evidence"]))
    with pytest.raises(InvariantViolation, match="invariant 5"):
        underwriting.select_revision(store, rid, "direct_user:owner")
    underwriting.review_revision(store, rid, "reviewer:pm")
    with pytest.raises(InvariantViolation, match="invariant 5"):
        underwriting.select_revision(store, rid, "model:assistant")
    with pytest.raises(sqlite3.IntegrityError, match="invariant 5"):
        store.db.execute("UPDATE case_set_revision SET status='selected' WHERE id=?", (rid,))
    underwriting.select_revision(store, rid, "direct_user:owner")
    underwriting.assert_selection_is_proven(store, rid)


# Test 6: receipt separation.
def test_receipt_separation(store, acme, selected):
    rev = underwriting.get_revision(store, selected)
    ref = f"case_set:{selected}"
    with pytest.raises(InvariantViolation, match="not 'execution'"):
        receipts.require_receipt(store, rev.selection_receipt_id, "execution", ref)
    with pytest.raises(InvariantViolation, match="not 'approval'"):
        receipts.require_receipt(store, rev.selection_receipt_id, "approval", ref)
    st = receipts.status_of(store, ref)
    assert st["selected"] and not st["approved"] and not st["executed"] and not st["delivered"]
    # a drafted message is not delivered until a delivery receipt exists
    msg_ref = "message:draft_1"
    assert receipts.status_of(store, msg_ref)["delivered"] is False
    receipts.add_receipt(store, "delivery", msg_ref, "system:mailer", {"channel": "synthetic"})
    assert receipts.status_of(store, msg_ref)["delivered"] is True
    # receipts cannot be edited into a different kind
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.db.execute("UPDATE receipt SET kind='execution' WHERE id=?", (rev.selection_receipt_id,))


# Invariant 11: returns declare basis and anchor.
def test_returns_declare_basis(store, acme):
    bad = cases_for(acme["evidence"])
    bad[0] = dict(bad[0], return_basis="unspecified")
    with pytest.raises(InvariantViolation, match="invariant 11"):
        underwriting.draft_revision(store, acme["subject"], "12m", "x", bad)
    bad2 = cases_for(acme["evidence"])
    bad2[0] = dict(bad2[0]); bad2[0].pop("return_anchor_at")
    with pytest.raises(InvariantViolation, match="invariant 11"):
        underwriting.draft_revision(store, acme["subject"], "12m", "x", bad2)
