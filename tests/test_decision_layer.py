"""Decision-layer tests 11 to 17 (SPEC.md section 11), plus the decision
fixtures as in-code scenarios. All scopes and subjects are synthetic."""
import sqlite3

import pytest

from conftest import flat_snapshot, mandate_body
from fmk import decisions, receipts, retrieval
from fmk.models import InvariantViolation


# 11. Single active mandate
def test_single_active_mandate(dstore):
    second = decisions.propose_mandate(dstore, "scope_test", mandate_body(max_position_pct=5.0))
    with pytest.raises(InvariantViolation, match="already has an active mandate"):
        decisions.activate_mandate(dstore, second, "direct_user:owner")
    # the refused activation left no receipt behind
    assert len(receipts.receipts_for(dstore, f"mandate:{second}")) == 0
    # raw SQL cannot bypass the partial unique index either
    with pytest.raises(sqlite3.IntegrityError):
        dstore.db.execute("UPDATE mandate SET status='active' WHERE id=?", (second,))
    decisions.retire_mandate(dstore, decisions.active_mandate(dstore, "scope_test").id, "direct_user:owner")
    decisions.activate_mandate(dstore, second, "direct_user:owner")
    assert decisions.active_mandate(dstore, "scope_test").id == second
    assert decisions.active_mandate(dstore, "scope_test").version == 2


def test_mandate_activation_needs_direct_user(store):
    mid = decisions.propose_mandate(store, "scope_x", mandate_body())
    with pytest.raises(InvariantViolation, match="direct_user"):
        decisions.activate_mandate(store, mid, "model:assistant")
    assert decisions.active_mandate(store, "scope_x") is None


def test_mandate_body_is_versioned_not_edited(dstore):
    m = decisions.active_mandate(dstore, "scope_test")
    with pytest.raises(sqlite3.IntegrityError, match="never edited in place"):
        dstore.db.execute("UPDATE mandate SET body='{}' WHERE id=?", (m.id,))


# 12. Plan is not position
def test_plan_is_not_position(dstore):
    decisions.add_snapshot(dstore, "scope_test", "SUBJ_A", "2026-10-09T10:00:00Z", "plan", quantity=100)
    cur = decisions.current_position(dstore, "scope_test", "SUBJ_A", now="2026-10-09T11:00:00Z")
    assert cur["status"] == "unverified"
    assert cur["quantity"] is None
    assert any("not position-grade" in w for w in cur["warnings"])


def test_estimate_and_stale_cache_are_not_positions(dstore):
    decisions.add_snapshot(dstore, "scope_test", "SUBJ_A", "2026-10-09T10:00:00Z", "estimate", quantity=90)
    decisions.add_snapshot(dstore, "scope_test", "SUBJ_A", "2026-10-09T10:05:00Z", "stale_cache", quantity=95)
    cur = decisions.current_position(dstore, "scope_test", "SUBJ_A", now="2026-10-09T11:00:00Z")
    assert cur["status"] == "unverified" and cur["quantity"] is None


# Fixture scenario: plan_vs_fill
def test_plan_vs_fill(dstore):
    decisions.add_snapshot(dstore, "scope_test", "SUBJ_A", "2026-10-09T10:00:00Z", "plan", quantity=100)
    decisions.add_snapshot(dstore, "scope_test", "SUBJ_A", "2026-10-09T10:30:00Z", "broker_fill", quantity=100,
                           mark_price=50.0, mark_as_of="2026-10-09T10:30:00Z")
    cur = decisions.current_position(dstore, "scope_test", "SUBJ_A", now="2026-10-09T10:45:00Z",
                                     mandate=decisions.active_mandate(dstore, "scope_test"))
    assert cur["status"] == "ok" and cur["source_class"] == "broker_fill" and cur["quantity"] == 100
    assert any("do not report as pending" in w for w in cur["warnings"])


# 17 and fixture scenario: weekend_mixed_marks
def test_stale_mark_fresh_position(dstore):
    decisions.add_snapshot(dstore, "scope_test", "EQ_1", "2026-10-11T09:00:00Z", "broker_position", quantity=10,
                           mark_price=200.0, mark_as_of="2026-10-09T20:00:00Z")  # Friday close
    cur = decisions.current_position(dstore, "scope_test", "EQ_1", now="2026-10-11T09:05:00Z",
                                     mandate=decisions.active_mandate(dstore, "scope_test"))
    assert cur["status"] == "ok"
    assert any("mark is older than position" in w for w in cur["warnings"])
    decisions.add_snapshot(dstore, "scope_test", "EQ_2", "2026-10-11T09:00:00Z", "broker_position", quantity=2,
                           mark_price=1500.0, mark_as_of="2026-10-11T08:59:00Z")
    live = decisions.current_position(dstore, "scope_test", "EQ_2", now="2026-10-11T09:05:00Z",
                                      mandate=decisions.active_mandate(dstore, "scope_test"))
    assert live["warnings"] == []


def test_snapshot_freshness_policy(dstore):
    decisions.add_snapshot(dstore, "scope_test", "EQ_2", "2026-10-09T09:00:00Z", "broker_position", quantity=10)
    cur = decisions.current_position(dstore, "scope_test", "EQ_2", now="2026-10-09T12:00:00Z",
                                     mandate=decisions.active_mandate(dstore, "scope_test"))
    assert cur["status"] == "ok" and cur["quantity"] == 10  # served, with a warning, never withheld
    assert any("exceeds policy" in w for w in cur["warnings"])


def test_snapshots_are_append_only(dstore):
    sid = decisions.add_snapshot(dstore, "scope_test", "SUBJ_A", "2026-10-09T10:00:00Z", "broker_position", quantity=1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        dstore.db.execute("UPDATE position_snapshot SET quantity=999 WHERE id=?", (sid,))


# 13. Forward-only decisions by receipt
def _propose(store, kind="trade", notional=5_000, nav=100_000, subject="SUBJ_A", rung="action", **proposal):
    prop = {"direction": "buy", "notional": notional, "venue": "venue_a", **proposal}
    return decisions.propose_decision(store, "scope_test", kind, rung, prop, "test rationale",
                                      subject_id=subject, nav=nav, asset_class="equity",
                                      position_snapshot_ids=[flat_snapshot(store, "scope_test", subject)])


def test_forward_only(dstore):
    did = _propose(dstore)
    with pytest.raises(InvariantViolation, match="no approval receipt"):
        decisions.record_execution(dstore, did, "venue:venue_a", {"qty": 1, "px": 50})  # skip approval
    decisions.approve_decision(dstore, did, "direct_user:owner")
    decisions.record_execution(dstore, did, "venue:venue_a", {"qty": 1, "px": 50})
    assert decisions.get_decision(dstore, did).status == "executed"
    with pytest.raises(InvariantViolation):
        decisions.approve_decision(dstore, did, "direct_user:owner")  # backwards
    # raw SQL cannot move it backwards or skip either
    with pytest.raises(sqlite3.IntegrityError, match="invariant 15"):
        dstore.db.execute("UPDATE decision_record SET status='proposed' WHERE id=?", (did,))
    other = _propose(dstore)
    with pytest.raises(sqlite3.IntegrityError, match="invariant 15"):
        dstore.db.execute("UPDATE decision_record SET status='executed' WHERE id=?", (other,))
    with pytest.raises(sqlite3.IntegrityError, match="invariant 15"):
        dstore.db.execute("UPDATE decision_record SET status='approved' WHERE id=?", (other,))
    chain = decisions.decision_chain(dstore, did)
    assert [c["kind"] for c in chain] == ["approval", "execution"]


def test_receipts_must_bind_to_the_decision(dstore):
    did = _propose(dstore)
    decisions.approve_decision(dstore, did, "direct_user:owner")
    other = _propose(dstore)
    foreign = receipts.add_receipt(dstore, "execution", f"decision:{other}", "venue:venue_a", {})
    with pytest.raises(sqlite3.IntegrityError, match="invariant 15"):
        dstore.db.execute("UPDATE decision_record SET status='executed', execution_receipt_id=? WHERE id=?",
                          (foreign, did))
    # a selection receipt bound to the right object is still the wrong kind (invariant 6)
    wrong_kind = receipts.add_receipt(dstore, "selection", f"decision:{did}", "direct_user:owner", {})
    with pytest.raises(sqlite3.IntegrityError, match="invariant 15"):
        dstore.db.execute("UPDATE decision_record SET status='executed', execution_receipt_id=? WHERE id=?",
                          (wrong_kind, did))
    with pytest.raises(InvariantViolation, match="not interchangeable"):
        receipts.require_receipt(dstore, wrong_kind, "execution", f"decision:{did}")


def test_reconcile_binds_a_reconciled_snapshot(dstore):
    did = _propose(dstore)
    with pytest.raises(InvariantViolation, match="requires an executed decision"):
        decisions.reconcile(dstore, did, "system:reconciler", "pos_none")
    decisions.approve_decision(dstore, did, "direct_user:owner")
    decisions.record_execution(dstore, did, "venue:venue_a", {"qty": 100, "px": 50})
    plan = decisions.add_snapshot(dstore, "scope_test", "SUBJ_A", "2026-10-09T10:00:00Z", "plan", quantity=100)
    with pytest.raises(InvariantViolation, match="reconciled"):
        decisions.reconcile(dstore, did, "system:reconciler", plan)
    elsewhere = decisions.add_snapshot(dstore, "scope_test", "SUBJ_0", "2026-10-09T11:00:00Z", "reconciled", quantity=7)
    with pytest.raises(InvariantViolation, match="another scope or subject"):
        decisions.reconcile(dstore, did, "system:reconciler", elsewhere)
    rec = decisions.add_snapshot(dstore, "scope_test", "SUBJ_A", "2026-10-09T11:00:00Z", "reconciled", quantity=100)
    rid = decisions.reconcile(dstore, did, "system:reconciler", rec)
    d = decisions.get_decision(dstore, did)
    assert d.reconcile_receipt_id == rid and d.status == "executed"
    assert receipts.get_receipt(dstore, rid).prior_receipt_id == d.execution_receipt_id
    with pytest.raises(InvariantViolation, match="already reconciled"):
        decisions.reconcile(dstore, did, "system:reconciler", rec)


# 14. Mandate breach recorded, not hidden; approval needs override
def test_mandate_breach(dstore):
    did = _propose(dstore, notional=12_000, nav=100_000)  # 12 percent against a 10 percent cap
    d = decisions.get_decision(dstore, did)
    assert d.mandate_check["status"] == "fail"
    assert any("max_position_pct" in b for b in d.mandate_check["breaches"])
    assert d.mandate_check["mandate_version"] == 1
    assert d.mandate_check["inputs"]["nav"] == 100_000 and d.mandate_check["inputs_supplied_by"] == "model:assistant"
    with pytest.raises(InvariantViolation, match="names every breach"):
        decisions.approve_decision(dstore, did, "direct_user:owner")
    with pytest.raises(InvariantViolation, match="names every breach"):
        decisions.approve_decision(dstore, did, "direct_user:owner", override={"overrides_breaches": ["something else"]})
    with pytest.raises(InvariantViolation, match="names every breach"):  # no reason
        decisions.approve_decision(dstore, did, "direct_user:owner",
                                   override={"overrides_breaches": d.mandate_check["breaches"]})
    assert decisions.get_decision(dstore, did).status == "proposed"
    rid = decisions.approve_decision(dstore, did, "direct_user:owner",
                                     override={"overrides_breaches": d.mandate_check["breaches"],
                                               "reason": "deliberate concentration"})
    r = receipts.get_receipt(dstore, rid)
    assert r.kind == "approval" and r.payload["override"]["reason"] == "deliberate concentration"
    assert decisions.get_decision(dstore, did).status == "approved"


def test_mandate_check_other_limits(dstore):
    m = decisions.active_mandate(dstore, "scope_test")
    chk = decisions.check_against_mandate("trade", {"direction": "buy", "notional": 60_000, "venue": "venue_z"}, m,
                                          nav=1_000_000, asset_class="bond", subject_id="SUBJ_A")
    texts = " ".join(chk["breaches"])
    assert chk["status"] == "fail"
    assert "per_trade_notional_cap" in texts and "venue_z" in texts and "bond" in texts


def test_the_caller_cannot_supply_the_check(dstore):
    """The check is computed by the kernel from the active mandate. A caller
    that claims 'pass' for 900 percent of NAV gets a recorded fail."""
    did = _propose(dstore, notional=900_000, nav=100_000)
    d = decisions.get_decision(dstore, did)
    assert d.mandate_check["status"] == "fail"
    assert d.mandate_id == decisions.active_mandate(dstore, "scope_test").id
    # no nav means position limits cannot be checked, which is itself a breach
    d2 = decisions.get_decision(dstore, _propose(dstore, nav=None))
    assert any("nav not supplied" in b for b in d2.mandate_check["breaches"])
    # no active mandate, no decision
    with pytest.raises(InvariantViolation, match="no active mandate"):
        decisions.propose_decision(dstore, "scope_nowhere", "trade", "action",
                                   {"direction": "buy", "notional": 1}, "r", nav=1)


def test_daily_notional_cap_and_net_edge(store):
    from conftest import mandate_body
    from fmk import evidence
    evidence.add_subject(store, "SUBJ_A", "Synthetic A", "security", subject_id="SUBJ_A")
    mid = decisions.propose_mandate(store, "scope_d", mandate_body(daily_notional_cap=8_000, min_net_edge_bps=20))
    decisions.activate_mandate(store, mid, "direct_user:owner")

    def prop(notional, edge, cost):
        return decisions.propose_decision(
            store, "scope_d", "trade", "action",
            {"direction": "buy", "notional": notional, "venue": "venue_a", "expected_edge_bps": edge,
             "estimated_cost_bps": cost}, "r", subject_id="SUBJ_A", nav=1_000_000, asset_class="equity",
            position_snapshot_ids=[flat_snapshot(store, "scope_d", "SUBJ_A")])
    first = prop(5_000, 60, 15)
    assert decisions.get_decision(store, first).mandate_check["status"] == "pass"
    decisions.approve_decision(store, first, "direct_user:owner")
    second = decisions.get_decision(store, prop(5_000, 30, 15))
    texts = " ".join(second.mandate_check["breaches"])
    assert "daily_notional_cap" in texts and "min_net_edge_bps" in texts


def test_snapshot_freshness_is_computed_and_stale_is_a_breach(store):
    from conftest import mandate_body
    from fmk import evidence
    from fmk.fixtures import fixed_clock
    from fmk.store import Store
    s = Store(":memory:", clock=fixed_clock("2026-10-09T12:00:00Z"))
    evidence.add_subject(s, "SUBJ_A", "Synthetic A", "security", subject_id="SUBJ_A")
    mid = decisions.propose_mandate(s, "scope_f", mandate_body())
    decisions.activate_mandate(s, mid, "direct_user:owner")
    old = decisions.add_snapshot(s, "scope_f", "SUBJ_A", "2026-10-09T09:00:00Z", "broker_position", quantity=10)
    did = decisions.propose_decision(s, "scope_f", "trade", "action",
                                     {"direction": "buy", "notional": 100, "venue": "venue_a"}, "r",
                                     subject_id="SUBJ_A", nav=100_000, asset_class="equity",
                                     position_snapshot_ids=[old])
    d = decisions.get_decision(s, did)
    assert d.snapshot_freshness == "stale"
    assert any("position snapshots are stale" in b for b in d.mandate_check["breaches"])


# 15. No-trade is a record
def test_no_trade_is_a_record(dstore):
    ids = []
    for i, why in enumerate(["price already moved 18 percent on the news",
                             "evidence is one secondary source; need primary",
                             "thesis unchanged; nothing to do"]):
        ids.append(decisions.propose_decision(dstore, "scope_test", "no_trade", "portfolio_fit", {}, why,
                                              subject_id=f"SUBJ_{i}"))
    declined = decisions.declined_decisions(dstore, "scope_test")
    assert [d.id for d in declined] == ids
    assert all(d.rationale_text for d in declined)
    with pytest.raises(InvariantViolation, match="requires a rationale"):
        decisions.propose_decision(dstore, "scope_test", "no_trade", "quick_take", {}, "  ")
    # later pairing with an outcome, without changing the decision row
    before = dstore.snapshot_tables("decision_record")
    decisions.attach_outcome(dstore, ids[0], {"horizon_days": 20, "subject_move_pct": -6})
    assert dstore.snapshot_tables("decision_record") == before
    chain = decisions.decision_chain(dstore, ids[0])
    assert chain and chain[-1]["kind"] == "review"
    # a no_trade can be acknowledged but never executed
    decisions.approve_decision(dstore, ids[1], "direct_user:owner")
    with pytest.raises(InvariantViolation, match="not executable"):
        decisions.record_execution(dstore, ids[1], "venue:venue_a", {"qty": 1, "px": 1})


# 16. Runtime required; fallback reaches output
def test_packet_runtime_required(store, acme):
    with pytest.raises(InvariantViolation, match="missing model_id"):
        retrieval.build_packet(store, acme["subject"], "guidance", "2026-10-10T00:00:00Z", {"fallback_used": False})
    with pytest.raises(InvariantViolation, match="fallback_used must be"):
        retrieval.build_packet(store, acme["subject"], "guidance", "2026-10-10T00:00:00Z",
                               {"model_id": "m", "model_version": "1", "provider": "p", "route_id": "r"})
    # the schema refuses a runtime-less packet even when the python guard is bypassed
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.db.execute(
            "INSERT INTO context_packet (id, task_ref, as_of, policy_version, query_hash, task, selected, "
            "ranking_explanations, omitted, warnings, runtime, packet_hash, created_at) "
            "VALUES ('p','t','2026-10-10T00:00:00Z','v1','q','{}','[]','[]','[]','[]','{}','h','2026-10-10T00:00:00Z')")


def test_fallback_flag_reaches_output(store, acme):
    pkt = retrieval.build_packet(store, acme["subject"], "guidance", "2026-10-10T00:00:00Z",
                                 {"model_id": "small-model", "model_version": "3", "provider": "p",
                                  "route_id": "r2", "fallback_used": True, "fallback_reason": "primary model timeout"})
    out = retrieval.render_output(pkt, "Portfolio brief body.")
    assert out.startswith("[MODEL FALLBACK IN USE: primary model timeout]")
    assert pkt.runtime["fallback_used"] is True


# Fixture scenario: approval_lost
def test_approval_lost_is_visible(dstore):
    did = _propose(dstore)
    d = decisions.get_decision(dstore, did)
    assert d.status == "proposed" and d.approval_receipt_id is None
    # "the user said yes in chat" is not a receipt; the system sees proposed and says so
    with pytest.raises(InvariantViolation, match="no approval receipt found"):
        decisions.record_execution(dstore, did, "venue:venue_a", {"qty": 1, "px": 50})
    assert receipts.status_of(dstore, f"decision:{did}")["approved"] is False


# Health check receipts
def test_health_check_receipt(dstore):
    rid = receipts.health_check(dstore, "known_answer_portfolio_value", "abc123", passed=False)
    r = receipts.get_receipt(dstore, rid)
    assert r.kind == "health_check" and r.payload == {"fixture_hash": "abc123", "passed": False}


# Execution cost: expected price next to every fill (essay problem 8)
def test_execution_records_slippage_and_partial_fills(store):
    from conftest import mandate_body
    from fmk import evidence
    evidence.add_subject(store, "SUBJ_A", "Synthetic A", "security", subject_id="SUBJ_A")
    mid = decisions.propose_mandate(store, "scope_e", mandate_body(max_slippage_bps=25))
    decisions.activate_mandate(store, mid, "direct_user:owner")
    did = decisions.propose_decision(store, "scope_e", "trade", "action",
                                     {"direction": "buy", "notional": 5_000, "venue": "venue_a",
                                      "quantity": 100, "expected_price": 50.0}, "r",
                                     subject_id="SUBJ_A", nav=100_000, asset_class="equity",
                                     position_snapshot_ids=[flat_snapshot(store, "scope_e", "SUBJ_A")])
    decisions.approve_decision(store, did, "direct_user:owner")
    rid = decisions.record_execution(store, did, "venue:venue_a", {"qty": 60, "px": 50.25})
    p = receipts.get_receipt(store, rid).payload
    assert p["slippage_bps"] == 50.0 and p["slippage_breach"] is True
    assert p["partial"] is True and p["unfilled_qty"] == 40
    costs = decisions.execution_costs(store, "scope_e")
    assert costs["measured"] == 1 and costs["weighted_slippage_bps"] == 50.0
