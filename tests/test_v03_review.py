"""Regression tests for the four defects found in external review of v0.2
(October 2026). Each test states the failure it guards against."""
import sqlite3

import pytest

from conftest import RUNTIME, flat_snapshot, mandate_body
from fmk import claims, decisions, evidence, receipts, retrieval, underwriting
from fmk.models import InvariantViolation
from fmk.store import Store


class Clock:
    def __init__(self, t: str):
        self.t = t

    def __call__(self) -> str:
        return self.t


def _store(t="2026-10-10T12:00:00Z", **body):
    clock = Clock(t)
    s = Store(":memory:", clock=clock)
    evidence.add_subject(s, "SUBJ_A", "Synthetic A", "security", subject_id="SUBJ_A")
    evidence.add_subject(s, "SUBJ_B", "Synthetic B", "security", subject_id="SUBJ_B")
    mid = decisions.propose_mandate(s, "scope_r", mandate_body(**body))
    decisions.activate_mandate(s, mid, "direct_user:owner")
    return s, clock


def _held(s, qty, px=100.0, as_of="2026-10-10T11:30:00Z"):
    return decisions.add_snapshot(s, "scope_r", "SUBJ_A", as_of, "broker_position", quantity=qty,
                                  mark_price=px, mark_as_of=as_of, mark_source_class="broker_position")


def _buy(s, notional, snaps, nav=100_000):
    did = decisions.propose_decision(s, "scope_r", "trade", "action",
                                     {"direction": "buy", "notional": notional, "venue": "venue_a"}, "r",
                                     subject_id="SUBJ_A", nav=nav, asset_class="equity",
                                     position_snapshot_ids=snaps)
    return decisions.get_decision(s, did)


# ------------------------------------------- 1. position limit is post-trade

def test_position_cap_counts_what_is_already_held():
    s, _ = _store(max_subject_exposure_pct=100.0)
    snap = _held(s, 90)                                   # 9% of 100k
    big = _buy(s, 9_000, [snap])                          # +9% -> 18%
    assert big.mandate_check["status"] == "fail"
    assert any("post-trade position 18.0%" in b for b in big.mandate_check["breaches"])
    small = _buy(s, 1_000, [snap])                        # +1% -> 10%
    assert small.mandate_check["status"] == "pass"


def test_pending_buys_count_toward_the_position():
    s, _ = _store(max_subject_exposure_pct=100.0)
    snap = _held(s, 50)                                   # 5%
    first = _buy(s, 4_000, [snap])
    decisions.approve_decision(s, first.id, "direct_user:owner")   # approved, not filled: 9%
    second = _buy(s, 2_000, [snap])                       # would be 11%
    assert second.mandate_check["inputs"]["pending_notional"] == 4_000
    assert any("max_position_pct" in b for b in second.mandate_check["breaches"])


def test_unknown_current_position_fails_closed():
    s, _ = _store()
    d = _buy(s, 1_000, [])
    assert any("current position not established" in b for b in d.mandate_check["breaches"])
    unmarked = decisions.add_snapshot(s, "scope_r", "SUBJ_A", "2026-10-10T11:30:00Z", "broker_position", quantity=10)
    d2 = _buy(s, 1_000, [unmarked])
    assert any("current position not established" in b for b in d2.mandate_check["breaches"])


def test_order_cap_is_separate_from_position_cap():
    s, _ = _store(max_order_pct=2.0)
    d = _buy(s, 3_000, [flat_snapshot(s, "scope_r", "SUBJ_A")])
    assert any("max_order_pct" in b for b in d.mandate_check["breaches"])


# ------------------------------------------- 2. approval uses current state

def test_newer_position_snapshot_blocks_approval():
    s, clock = _store()
    snap = _held(s, 10)
    d = _buy(s, 1_000, [snap])
    assert d.mandate_check["status"] == "pass"
    _held(s, 95, as_of="2026-10-10T11:50:00Z")           # the account moved
    with pytest.raises(InvariantViolation, match="account state changed"):
        decisions.approve_decision(s, d.id, "direct_user:owner")


def test_old_proposal_cannot_be_approved():
    s, clock = _store(proposal_ttl_seconds=600)
    d = _buy(s, 1_000, [_held(s, 10)])
    clock.t = "2026-10-10T12:20:00Z"
    with pytest.raises(InvariantViolation, match="proposal_ttl_seconds"):
        decisions.approve_decision(s, d.id, "direct_user:owner")


def test_snapshot_that_goes_stale_before_approval_is_a_breach():
    s, clock = _store(proposal_ttl_seconds=None)
    d = _buy(s, 1_000, [_held(s, 10)])                    # broker_position fresh for 3600s
    clock.t = "2026-10-10T13:00:00Z"
    with pytest.raises(InvariantViolation, match="position snapshots are stale"):
        decisions.approve_decision(s, d.id, "direct_user:owner")


def test_restated_nav_outside_tolerance_blocks_approval_and_inside_is_recorded():
    s, _ = _store()
    d = _buy(s, 1_000, [_held(s, 10)])
    with pytest.raises(InvariantViolation, match="nav_tolerance_pct"):
        decisions.approve_decision(s, d.id, "direct_user:owner", nav=90_000)
    rid = decisions.approve_decision(s, d.id, "direct_user:owner", nav=100_500)
    basis = receipts.get_receipt(s, rid).payload["approval_check"]["state_basis"]
    assert basis["nav_restated"] == 100_500 and basis["nav_proposed"] == 100_000


def test_approval_after_mandate_replacement_is_refused():
    s, _ = _store()
    d = _buy(s, 1_000, [_held(s, 10)])
    old = decisions.active_mandate(s, "scope_r")
    decisions.retire_mandate(s, old.id, "direct_user:owner")
    new = decisions.propose_mandate(s, "scope_r", mandate_body(max_position_pct=5.0))
    decisions.activate_mandate(s, new, "direct_user:owner")
    with pytest.raises(InvariantViolation, match="no longer active"):
        decisions.approve_decision(s, d.id, "direct_user:owner")


# ------------------------------------------- 3. replay does not see the future

def _acme(s):
    sid = evidence.add_subject(s, "ACME", "Synthetic issuer ACME")
    ev = evidence.add_evidence(s, sid, "ir_release", "synthetic://ACME/outlook", "2025-12-01T12:00:00Z",
                               "primary", {"text": "FY revenue guidance 950 to 980"})
    c = claims.add_claim(s, sid, ev, "guidance.fy_revenue", "fact", "FY revenue guidance 950 to 980",
                         confidence=0.9, created_at="2025-12-01T12:05:00Z")
    return sid, ev, c


def test_trigger_created_later_is_not_in_an_earlier_packet():
    s = Store(":memory:", clock=Clock("2026-10-10T00:00:00Z"))
    sid, _, _ = _acme(s)
    underwriting.add_trigger(s, sid, "guide cut", "revenue guidance cut")
    early = retrieval.generate_candidates(s, sid, "revenue guidance", "2026-01-01T00:00:00Z")
    assert not any(c.kind == "trigger" for c in early)
    later = retrieval.generate_candidates(s, sid, "revenue guidance", "2026-10-11T00:00:00Z")
    assert any(c.kind == "trigger" for c in later)


def test_later_trigger_state_does_not_rewrite_replay():
    clock = Clock("2026-10-10T00:00:00Z")
    s = Store(":memory:", clock=clock)
    sid, _, _ = _acme(s)
    tid = underwriting.add_trigger(s, sid, "guide cut", "revenue guidance cut")
    clock.t = "2026-10-10T01:00:00Z"
    underwriting.set_trigger_state(s, tid, "amber")
    clock.t = "2026-10-10T02:00:00Z"
    underwriting.set_trigger_state(s, tid, "tripped")
    then = retrieval.generate_candidates(s, sid, "guidance", "2026-10-10T01:30:00Z", known_as_of="2026-10-10T01:30:00Z")
    assert [c.status for c in then if c.kind == "trigger"] == ["amber"]
    now = retrieval.generate_candidates(s, sid, "guidance", "2026-10-10T02:30:00Z")
    assert [c.status for c in now if c.kind == "trigger"] == ["tripped"]


def test_trigger_state_cannot_change_without_an_event():
    s = Store(":memory:", clock=Clock("2026-10-10T00:00:00Z"))
    sid, _, _ = _acme(s)
    tid = underwriting.add_trigger(s, sid, "guide cut", "revenue guidance cut")
    with pytest.raises(sqlite3.DatabaseError, match="trigger_state_event"):
        s.db.execute("UPDATE research_trigger SET state='red' WHERE id=?", (tid,))
    with pytest.raises(sqlite3.DatabaseError, match="frozen"):
        s.db.execute("UPDATE research_trigger SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (tid,))


def test_saved_packet_reads_the_same_after_a_retraction():
    clock = Clock("2026-10-01T00:00:00Z")
    s = Store(":memory:", clock=clock)
    sid, ev, c = _acme(s)
    pkt = retrieval.build_packet(s, sid, "FY revenue guidance", "2026-10-01T00:00:00Z", RUNTIME)
    before = retrieval.summarize(s, pkt)
    assert "[active" in before
    clock.t = "2026-10-05T00:00:00Z"
    claims.retract(s, c, "reviewer:pm", ev)
    saved = retrieval.get_packet(s, pkt.id)
    assert retrieval.summarize(s, saved) == before                      # history keeps its meaning
    assert claims.get_claim(s, c).status == "retracted"                  # current state moved on
    fresh = retrieval.build_packet(s, sid, "FY revenue guidance", "2026-10-05T00:00:00Z", RUNTIME)
    assert f"claim:{c}" not in retrieval.packet_refs(fresh)


# ------------------------------------------- 4. retraction needs authority

def test_model_cannot_retract():
    s = Store(":memory:", clock=Clock("2026-10-10T00:00:00Z"))
    _, ev, c = _acme(s)
    with pytest.raises(InvariantViolation):
        claims.retract(s, c, "model:assistant", ev)
    assert claims.get_claim(s, c).status == "active"


def test_evidence_about_another_subject_cannot_retract():
    s = Store(":memory:", clock=Clock("2026-10-10T00:00:00Z"))
    _, _, c = _acme(s)
    other = evidence.add_subject(s, "OTHER", "Synthetic other")
    ev_other = evidence.add_evidence(s, other, "filing", "synthetic://OTHER/10q", "2026-10-09T00:00:00Z",
                                     "primary", {"text": "unrelated"})
    with pytest.raises(InvariantViolation, match="different subject"):
        claims.retract(s, c, "reviewer:pm", ev_other)


def test_weaker_evidence_cannot_retract_but_can_contradict():
    s = Store(":memory:", clock=Clock("2026-10-10T00:00:00Z"))
    sid, _, c = _acme(s)
    chatter = evidence.add_evidence(s, sid, "social_post", "synthetic://social/1", "2026-10-09T00:00:00Z",
                                    "social_chatter", {"text": "heard guidance is wrong"})
    with pytest.raises(InvariantViolation, match="record a contradiction"):
        claims.retract(s, c, "reviewer:pm", chatter)
    assert claims.get_claim(s, c).status == "active"
    rumour = claims.add_claim(s, sid, chatter, "guidance.fy_revenue", "hypothesis", "guidance is wrong",
                              created_at="2026-10-09T00:05:00Z")
    claims.contradict(s, c, rumour, chatter)
    assert claims.get_claim(s, c).status == "contradicted"


def test_retraction_writes_a_human_receipt_and_raw_sql_cannot_skip_it():
    s = Store(":memory:", clock=Clock("2026-10-10T00:00:00Z"))
    _, ev, c = _acme(s)
    claims.retract(s, c, "reviewer:pm", ev)
    r = receipts.receipts_for(s, f"claim:{c}", "review")[-1]
    assert r.payload == {"retracted": True, "evidence_id": ev} and r.authority_ref == "reviewer:pm"
    _, ev2, c2 = _acme_again(s)
    with pytest.raises(sqlite3.DatabaseError, match="human review receipt"):
        s.db.execute("UPDATE claim SET status='retracted' WHERE id=?", (c2,))


def _acme_again(s):
    sid = evidence.add_subject(s, "ACME2", "Synthetic issuer ACME2")
    ev = evidence.add_evidence(s, sid, "ir_release", "synthetic://ACME2/outlook", "2025-12-01T12:00:00Z",
                               "primary", {"text": "guidance"})
    c = claims.add_claim(s, sid, ev, "guidance.fy_revenue", "fact", "guidance", created_at="2025-12-01T12:05:00Z")
    return sid, ev, c
