"""Attempts to break the record, each of which must now be refused or flagged.

These came from an adversarial review of v0.1, which got through with every
one of them. Most go around the Python API with raw SQL, because the
claim is that the schema holds on its own.
"""
import sqlite3

import pytest

from conftest import RUNTIME, cases_for, mandate_body
from fmk import claims, decisions, evidence, receipts, retrieval, underwriting
from fmk.fixtures import fixed_clock
from fmk.models import DecisionRecord, InvariantViolation, Mandate
from fmk.store import Store


def _decision_store(clock="2026-10-10T12:00:00Z"):
    s = Store(":memory:", clock=fixed_clock(clock))
    evidence.add_subject(s, "SUBJ_A", "Synthetic A", "security", subject_id="SUBJ_A")
    evidence.add_subject(s, "SUBJ_B", "Synthetic B", "security", subject_id="SUBJ_B")
    mid = decisions.propose_mandate(s, "scope_h", mandate_body())
    decisions.activate_mandate(s, mid, "direct_user:owner")
    return s, mid


def _propose(s, notional=1_000, proposed_by="model:assistant", **kw):
    return decisions.propose_decision(s, "scope_h", "trade", "action",
                                      {"direction": "buy", "notional": notional, "venue": "venue_a"}, "r",
                                      subject_id="SUBJ_A", nav=100_000, asset_class="equity",
                                      proposed_by=proposed_by, **kw)


# ------------------------------------------------------------- claims

def test_claim_authority_and_expiry_are_frozen(store, acme):
    for col, val in (("source_strength", "direct_user"), ("expires_at", "2099-01-01T00:00:00Z"),
                     ("valid_from", "2020-01-01T00:00:00Z")):
        with pytest.raises(sqlite3.IntegrityError, match="frozen"):
            store.db.execute(f"UPDATE claim SET {col}=? WHERE id=?", (val, acme["claim"]))


def test_retracted_and_superseded_claims_do_not_come_back(store, acme):
    ev = acme["evidence"]
    claims.retract(store, acme["claim"], "reviewer:pm", ev)
    with pytest.raises(sqlite3.IntegrityError, match="does not come back"):
        store.db.execute("UPDATE claim SET status='active' WHERE id=?", (acme["claim"],))
    a = claims.add_claim(store, acme["subject"], ev, "guidance.op_margin", "fact", "margin 15")
    b = claims.add_claim(store, acme["subject"], ev, "guidance.op_margin", "fact", "margin 16")
    claims.supersede(store, b, a)
    with pytest.raises(sqlite3.IntegrityError, match="does not come back"):
        store.db.execute("UPDATE claim SET status='active' WHERE id=?", (a,))


def test_a_claim_cannot_outrank_its_evidence(store, acme):
    social = evidence.add_evidence(store, acme["subject"], "social_post", "synthetic://forum/1",
                                   "2026-10-02T00:00:00Z", "social_chatter", {"text": "guide is 2000"})
    with pytest.raises(InvariantViolation, match="cannot outrank its evidence"):
        claims.add_claim(store, acme["subject"], social, "guidance.fy_revenue", "fact", "guide 2000",
                         source_strength="direct_user")


def test_low_authority_claim_cannot_supersede_high(store, acme):
    social = evidence.add_evidence(store, acme["subject"], "social_post", "synthetic://forum/2",
                                   "2026-10-02T00:00:00Z", "social_chatter", {"text": "guide is 2000"})
    c = claims.add_claim(store, acme["subject"], social, "guidance.fy_revenue", "fact", "guide 2000")
    with pytest.raises(InvariantViolation, match="record a contradiction instead"):
        claims.supersede(store, c, acme["claim"])


def test_contradiction_resolution_needs_a_human(store, acme):
    ev2 = evidence.add_evidence(store, acme["subject"], "broker_note", "synthetic://note/1",
                                "2026-10-02T00:00:00Z", "credible_secondary", {"text": "guide 900"})
    other = claims.add_claim(store, acme["subject"], ev2, "guidance.fy_revenue", "fact", "guide 900")
    claims.contradict(store, acme["claim"], other)
    for who in ("primaryish_bot", "primary:filing", "model:assistant", "system:resolver"):
        with pytest.raises(InvariantViolation):
            claims.resolve_contradiction(store, acme["claim"], other, who, acme["evidence"])
    claims.resolve_contradiction(store, acme["claim"], other, "reviewer:pm", acme["evidence"])
    assert claims.get_claim(store, other).status == "superseded"


def test_dependents_go_stale_when_their_basis_changes(store, acme):
    sid, ev = acme["subject"], acme["evidence"]
    per_share = claims.add_claim(store, sid, ev, "valuation.revenue_per_share", "inference", "4.8 per share",
                                 created_at="2026-10-01T13:00:00Z")
    multiple = claims.add_claim(store, sid, ev, "valuation.multiple", "inference", "12x on 4.8",
                                created_at="2026-10-01T13:00:00Z")
    claims.add_dependency(store, per_share, acme["claim"])
    claims.add_dependency(store, multiple, per_share)
    with pytest.raises(InvariantViolation, match="cycle"):
        claims.add_dependency(store, acme["claim"], multiple)
    new_ev = evidence.add_evidence(store, sid, "ir_release", "synthetic://ACME/outlook-2", "2026-10-05T00:00:00Z",
                                   "primary", {"text": "FY revenue guidance 900 to 920"})
    claims.correct(store, sid, acme["claim"], new_ev, "FY revenue guidance 900 to 920")
    assert claims.get_claim(store, per_share).status == "stale"
    assert claims.get_claim(store, multiple).status == "stale"      # transitive
    pkt = retrieval.build_packet(store, sid, "revenue per share multiple", "2026-10-06T00:00:00Z", RUNTIME)
    assert any(f"claim:{multiple} rests on" in w for w in pkt.warnings)
    with pytest.raises(InvariantViolation):
        claims.reaffirm(store, multiple, "model:assistant")
    claims.reaffirm(store, multiple, "reviewer:pm", "multiple is on next year's number; unaffected")
    assert claims.get_claim(store, multiple).status == "active"


# ------------------------------------------------------------- time

def test_week_old_price_typed_today_is_not_fresh(store, acme):
    ev = evidence.add_evidence(store, acme["subject"], "market_data", "synthetic://feed/old", "2026-10-02T15:00:00Z",
                               "system_measurement", {"last": 51.0})
    c = claims.add_claim(store, acme["subject"], ev, "price.last", "fact", "last 51.0",
                         created_at="2026-10-09T15:00:00Z")
    pkt = retrieval.build_packet(store, acme["subject"], "last price", "2026-10-09T15:01:00Z", RUNTIME)
    assert {s["ref"]: s for s in pkt.selected}[f"claim:{c}"]["freshness"] == "expired"


def test_known_as_of_excludes_backfills_and_backdated_claims():
    s = Store(":memory:", clock=fixed_clock("2026-10-05T00:00:00Z"))
    sid = evidence.add_subject(s, "ACME", "Synthetic ACME")
    evidence.add_evidence(s, sid, "ir_release", "synthetic://ACME/a", "2026-10-01T00:00:00Z", "primary",
                          {"text": "guidance 950"})
    before = retrieval.build_packet(s, sid, "guidance", "2026-10-03T00:00:00Z", RUNTIME,
                                    known_as_of="2026-10-05T00:00:00Z")
    s.clock = fixed_clock("2026-10-08T00:00:00Z")   # later, someone backfills and backdates
    ev2 = evidence.add_evidence(s, sid, "ir_release", "synthetic://ACME/b", "2026-10-02T00:00:00Z", "primary",
                                {"text": "guidance 900"})
    claims.add_claim(s, sid, ev2, "guidance.fy_revenue", "fact", "guidance 900", created_at="2026-10-02T00:00:00Z")
    replay = retrieval.build_packet(s, sid, "guidance", "2026-10-03T00:00:00Z", RUNTIME,
                                    known_as_of="2026-10-05T00:00:00Z")
    assert replay.packet_hash == before.packet_hash
    now = retrieval.build_packet(s, sid, "guidance", "2026-10-03T00:00:00Z", RUNTIME)
    assert now.packet_hash != before.packet_hash


def test_superseded_evidence_is_omitted_with_its_successor_named(store, acme):
    fix = evidence.add_evidence(store, acme["subject"], "ir_release", "synthetic://ACME/outlook-fix",
                                "2026-10-01T13:00:00Z", "primary", {"text": "FY revenue guidance 950 to 990"},
                                supersedes_evidence_id=acme["evidence"])
    pkt = retrieval.build_packet(store, acme["subject"], "revenue guidance", "2026-10-02T00:00:00Z", RUNTIME)
    assert {"ref": f"evidence:{acme['evidence']}", "why": f"superseded by evidence:{fix}"} in pkt.omitted


def test_position_ignores_future_snapshots_and_prefers_fresh():
    s, _ = _decision_store()
    m = decisions.active_mandate(s, "scope_h")
    decisions.add_snapshot(s, "scope_h", "SUBJ_A", "2026-09-10T00:00:00Z", "reconciled", quantity=100)
    decisions.add_snapshot(s, "scope_h", "SUBJ_A", "2026-10-10T11:59:00Z", "broker_position", quantity=0)
    decisions.add_snapshot(s, "scope_h", "SUBJ_A", "2026-10-11T00:00:00Z", "broker_position", quantity=55)
    cur = decisions.current_position(s, "scope_h", "SUBJ_A", now="2026-10-10T12:00:00Z", mandate=m)
    assert cur["quantity"] == 0 and cur["source_class"] == "broker_position"   # fresh beats stale reconciled
    stale_first = decisions.current_position(s, "scope_h", "SUBJ_A", now="2026-09-10T00:00:00Z", mandate=m)
    assert stale_first["quantity"] == 100    # nothing from the future


def test_newer_disagreeing_snapshot_is_warned():
    s, _ = _decision_store()
    m = decisions.active_mandate(s, "scope_h")
    decisions.add_snapshot(s, "scope_h", "SUBJ_A", "2026-10-10T11:00:00Z", "reconciled", quantity=100)
    decisions.add_snapshot(s, "scope_h", "SUBJ_A", "2026-10-10T11:30:00Z", "broker_fill", quantity=60)
    cur = decisions.current_position(s, "scope_h", "SUBJ_A", now="2026-10-10T12:00:00Z", mandate=m)
    assert cur["quantity"] == 100
    assert any("reports quantity 60" in w for w in cur["warnings"])


def test_contradiction_parties_survive_a_tight_budget_without_gaining_authority(store, acme):
    sid = acme["subject"]
    for i in range(6):
        e = evidence.add_evidence(store, sid, "ir_release", f"synthetic://ACME/n{i}", "2026-10-01T00:00:00Z",
                                  "primary", {"text": f"note {i} guidance"})
        claims.add_claim(store, sid, e, f"misc.n{i}", "fact", f"note {i} guidance", created_at="2026-10-01T01:00:00Z")
    weak = evidence.add_evidence(store, sid, "broker_note", "synthetic://note/x", "2026-10-02T00:00:00Z",
                                 "credible_secondary", {"text": "guide 900"})
    other = claims.add_claim(store, sid, weak, "guidance.fy_revenue", "fact", "guide 900",
                             created_at="2026-10-02T01:00:00Z")
    claims.contradict(store, acme["claim"], other)
    pkt = retrieval.build_packet(store, sid, "guidance", "2026-10-03T00:00:00Z", RUNTIME, budget=3)
    refs = {s["ref"] for s in pkt.selected}
    assert {f"claim:{acme['claim']}", f"claim:{other}"} <= refs
    auth = {e["ref"]: e["authority"] for e in pkt.ranking_explanations}
    assert auth[f"claim:{other}"] == 3      # still credible_secondary


# ------------------------------------------------------------- authority

def test_authority_must_be_kind_colon_name_at_the_schema():
    s, _ = _decision_store()
    for bad in ("anything", "admin:root", "direct_user:", ":x"):
        with pytest.raises((InvariantViolation, sqlite3.IntegrityError)):
            receipts.add_receipt(s, "review", "decision:x", bad, {})


def test_models_cannot_review_select_approve_or_activate(store, acme):
    rid = underwriting.draft_revision(store, acme["subject"], "12m", "m", cases_for(acme["evidence"]),
                                      proposed_by="model:assistant")
    for who in ("model:assistant", "llm:gpt", "Model:x", "agent:a", "system:bot"):
        with pytest.raises(InvariantViolation):
            underwriting.review_revision(store, rid, who)
    underwriting.review_revision(store, rid, "reviewer:pm")
    for who in ("model:assistant", "system:bot", "venue:v"):
        with pytest.raises(InvariantViolation):
            underwriting.select_revision(store, rid, who)
    s, _ = _decision_store()
    did = _propose(s)
    with pytest.raises(InvariantViolation, match="requires a"):
        decisions.approve_decision(s, did, "model:assistant")
    # the schema refuses the receipt even if Python is bypassed
    with pytest.raises(InvariantViolation, match="human authority"):
        receipts.add_receipt(s, "approval", f"decision:{did}", "model:assistant", {})


def test_reviewer_cannot_review_own_draft_and_approver_is_not_proposer(store, acme):
    rid = underwriting.draft_revision(store, acme["subject"], "12m", "mine", cases_for(acme["evidence"]),
                                      proposed_by="reviewer:pm")
    for same in ("reviewer:pm", "direct_user:PM", "reviewer: pm "):
        with pytest.raises(InvariantViolation, match="same person"):
            underwriting.review_revision(store, rid, same)
    s, _ = _decision_store()
    did = _propose(s, proposed_by="direct_user:owner")
    for same in ("direct_user:owner", "reviewer:owner", "reviewer:Owner"):   # switching kind is not a second person
        with pytest.raises(InvariantViolation, match="same person"):
            decisions.approve_decision(s, did, same)
    # and the schema agrees, if Python is bypassed
    forged = receipts.add_receipt(s, "approval", f"decision:{did}", "reviewer:OWNER", {})
    with pytest.raises(sqlite3.IntegrityError, match="other than the proposer"):
        s.db.execute("UPDATE decision_record SET status='approved', approval_receipt_id=? WHERE id=?", (forged, did))
    decisions.approve_decision(s, did, "reviewer:risk")


def test_authorize_hook_can_refuse():
    calls = []

    def only_alice(kind, obj, who, payload):
        calls.append(who)
        return who != "direct_user:mallory"
    s = Store(":memory:", authorize=only_alice)
    mid = decisions.propose_mandate(s, "scope_z", mandate_body())
    with pytest.raises(InvariantViolation, match="authorize hook refused"):
        decisions.activate_mandate(s, mid, "direct_user:mallory")
    decisions.activate_mandate(s, mid, "direct_user:alice")
    assert calls == ["direct_user:mallory", "direct_user:alice"]


# ------------------------------------------------------------- inserts and deletes

def test_raw_inserts_cannot_skip_the_lifecycle():
    s, mid = _decision_store()
    m = decisions.active_mandate(s, "scope_h")
    d = DecisionRecord(id="dec_x", scope_ref="scope_h", subject_id="SUBJ_A", decision_kind="trade", intent_ref=None,
                       intent_rung="action", proposal={"notional": 10**9}, rationale_text="x", evidence_ids=[],
                       selected_revision_id=None, mandate_id=mid,
                       mandate_check={"status": "pass", "mandate_id": mid, "breaches": []},
                       position_snapshot_ids=[], snapshot_freshness="fresh", conviction=None,
                       change_triggers_text=None, status="executed", proposed_by="model:x",
                       proposed_at="2026-10-10T00:00:00Z")
    with pytest.raises(InvariantViolation, match="inserted as proposed"):
        s.insert(d)
    rogue = Mandate(id="mnd_x", scope_ref="scope_other", version=1, status="active", parent_id=None,
                    body=m.body, activated_by_receipt_id=None, created_at="2026-10-10T00:00:00Z", retired_at=None)
    with pytest.raises(InvariantViolation, match="inserted as a draft"):
        s.insert(rogue)


def test_decision_cannot_cite_a_draft_or_another_scopes_mandate(store, acme):
    rid = underwriting.draft_revision(store, acme["subject"], "12m", "d", cases_for(acme["evidence"]))
    mid = decisions.propose_mandate(store, "scope_q", mandate_body())
    decisions.activate_mandate(store, mid, "direct_user:owner")
    with pytest.raises(InvariantViolation, match="selected revision"):
        decisions.propose_decision(store, "scope_q", "hold", "portfolio_fit", {}, "r",
                                   subject_id=acme["subject"], selected_revision_id=rid)


@pytest.fixture
def full_store():
    """One row in every table that must never lose one."""
    s, _ = _decision_store()
    ev = evidence.add_evidence(s, "SUBJ_A", "ir_release", "synthetic://A/1", "2026-10-01T00:00:00Z", "primary",
                               {"text": "x"})
    c = claims.add_claim(s, "SUBJ_A", ev, "guidance.x", "fact", "x")
    claims.add_dependency(s, claims.add_claim(s, "SUBJ_A", ev, "guidance.y", "fact", "y"), c)
    claims.open_question(s, "SUBJ_A", "why", "filing")
    underwriting.add_invalidation(s, "SUBJ_A", "guide cut", "filing shows cut")
    underwriting.add_trigger(s, "SUBJ_A", "t", "price below 10")
    underwriting.draft_revision(s, "SUBJ_A", "12m", "d", cases_for(ev))
    decisions.add_snapshot(s, "scope_h", "SUBJ_A", "2026-10-10T00:00:00Z", "broker_position", quantity=1)
    _propose(s)
    retrieval.build_packet(s, "SUBJ_A", "x", "2026-10-10T00:00:00Z", RUNTIME)
    return s


NEVER_DELETED = ["evidence", "claim", "claim_relation", "research_question", "receipt", "case_set_revision",
                 "research_case", "invalidation", "research_trigger", "context_packet", "mandate",
                 "position_snapshot", "decision_record"]


@pytest.mark.parametrize("table", NEVER_DELETED)
def test_no_deletes(full_store, table):
    assert full_store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
    with pytest.raises(sqlite3.IntegrityError):
        full_store.db.execute(f"DELETE FROM {table}")


def test_decision_links_are_write_once():
    s, _ = _decision_store()
    did = _propose(s)
    decisions.approve_decision(s, did, "direct_user:owner")
    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        s.db.execute("UPDATE decision_record SET approval_receipt_id=NULL WHERE id=?", (did,))
    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        s.db.execute("UPDATE decision_record SET proposed_by='direct_user:owner' WHERE id=?", (did,))


# ------------------------------------------------------------- receipt chain

def test_chain_verifies_and_detects_a_rewrite(tmp_path):
    path = str(tmp_path / "k.sqlite")
    s = Store(path, clock=fixed_clock("2026-10-10T12:00:00Z"))
    evidence.add_subject(s, "SUBJ_A", "Synthetic A", "security", subject_id="SUBJ_A")
    mid = decisions.propose_mandate(s, "scope_h", mandate_body())
    decisions.activate_mandate(s, mid, "direct_user:owner")
    did = decisions.propose_decision(s, "scope_h", "trade", "action",
                                     {"direction": "buy", "notional": 1_000, "venue": "venue_a"}, "r",
                                     subject_id="SUBJ_A", nav=100_000, asset_class="equity")
    rid = decisions.approve_decision(s, did, "direct_user:owner")
    head = receipts.chain_head(s)
    assert receipts.verify_chain(s, head) == []
    # someone with the file drops the trigger and rewrites who approved
    s.db.execute("DROP TRIGGER trg_receipt_no_update")
    s.db.execute("UPDATE receipt SET authority_ref='reviewer:someone_else' WHERE id=?", (rid,))
    problems = receipts.verify_chain(s, head)
    assert any(rid in p for p in problems)


def test_bindings_detect_a_rewritten_proposal_after_approval():
    s, _ = _decision_store()
    did = _propose(s)
    decisions.approve_decision(s, did, "direct_user:owner")
    head = receipts.chain_head(s)
    assert receipts.verify_chain(s, head) == []
    s.db.execute("DROP TRIGGER trg_decision_body_frozen")
    s.db.execute("""UPDATE decision_record SET proposal='{"direction":"buy","notional":5000000}' WHERE id=?""", (did,))
    assert any("proposal no longer matches" in p for p in receipts.verify_chain(s, head))


def test_receipt_time_cannot_run_backwards():
    s, _ = _decision_store()
    receipts.add_receipt(s, "health_check", "fixture:x", "system:health", {"passed": True})
    s.clock = fixed_clock("2020-01-01T00:00:00Z")
    with pytest.raises(InvariantViolation, match="dated before"):
        receipts.add_receipt(s, "health_check", "fixture:y", "system:health", {"passed": True})


def test_chain_cannot_fork():
    s, _ = _decision_store()
    receipts.add_receipt(s, "health_check", "fixture:x", "system:health", {"passed": True})
    with pytest.raises(sqlite3.IntegrityError, match="chain head"):
        s.db.execute("INSERT INTO receipt (id, kind, object_ref, authority_ref, occurred_at, payload, payload_hash, "
                     "prior_receipt_id, prev_chain_hash, chain_hash) VALUES "
                     "('r_fork','review','x:y','system:a','2026-10-11T00:00:00Z','{}','h',NULL,'genesis','c')")


# ------------------------------------------------------------- decisions

def test_stale_approvals_cannot_execute():
    s, _ = _decision_store()
    did = _propose(s)
    decisions.approve_decision(s, did, "direct_user:owner")
    s.clock = fixed_clock("2026-10-12T12:00:00Z")    # two days later; default ttl is one day
    with pytest.raises(InvariantViolation, match="approval_ttl_seconds"):
        decisions.record_execution(s, did, "venue:venue_a", {"qty": 1, "px": 1})
    decisions.expire_decision(s, did, "system:scheduler", "approval lapsed")
    assert decisions.get_decision(s, did).status == "expired"


def test_idempotent_proposals():
    s, _ = _decision_store()
    a = _propose(s, idempotency_key="req-1")
    b = _propose(s, idempotency_key="req-1")
    assert a == b and len(s.query("decision_record")) == 1
    with pytest.raises(InvariantViolation, match="different proposal"):
        _propose(s, notional=2_000, idempotency_key="req-1")


def test_executions_and_reconciles_come_from_venues_or_systems():
    s, _ = _decision_store()
    did = _propose(s)
    decisions.approve_decision(s, did, "direct_user:owner")
    with pytest.raises(InvariantViolation):
        decisions.record_execution(s, did, "model:assistant", {"qty": 1, "px": 1})
    with pytest.raises(InvariantViolation, match="venue or system"):
        receipts.add_receipt(s, "execution", f"decision:{did}", "direct_user:owner", {})


def test_sells_need_a_holding_and_do_not_count_as_adding_exposure():
    s, _ = _decision_store()
    snap = decisions.add_snapshot(s, "scope_h", "SUBJ_A", "2026-10-10T11:50:00Z", "broker_position",
                                  quantity=300, mark_price=50.0, mark_as_of="2026-10-10T11:50:00Z")

    def sell(notional, snaps):
        return decisions.get_decision(s, decisions.propose_decision(
            s, "scope_h", "trade", "action", {"direction": "sell", "notional": notional, "venue": "venue_a"}, "r",
            subject_id="SUBJ_A", nav=100_000, asset_class="equity", position_snapshot_ids=snaps)).mandate_check
    assert sell(12_000, [snap])["status"] == "pass"          # 12% of NAV, but it reduces exposure
    assert any("opens a short" in b for b in sell(20_000, [snap])["breaches"])   # holding is worth 15,000
    assert any("would open a short" in b for b in sell(1_000, [])["breaches"])  # no holding cited


def test_daily_cap_is_rechecked_at_approval():
    s = Store(":memory:", clock=fixed_clock("2026-10-10T12:00:00Z"))
    evidence.add_subject(s, "SUBJ_A", "Synthetic A", "security", subject_id="SUBJ_A")
    mid = decisions.propose_mandate(s, "scope_c", mandate_body(daily_notional_cap=50_000, max_position_pct=100.0,
                                                               max_subject_exposure_pct=100.0))
    decisions.activate_mandate(s, mid, "direct_user:owner")
    ids = [decisions.propose_decision(s, "scope_c", "trade", "action",
                                      {"direction": "buy", "notional": 30_000, "venue": "venue_a"}, "r",
                                      subject_id="SUBJ_A", nav=1_000_000, asset_class="equity") for _ in range(3)]
    assert all(decisions.get_decision(s, i).mandate_check["status"] == "pass" for i in ids)  # each alone passes
    decisions.approve_decision(s, ids[0], "direct_user:owner")
    with pytest.raises(InvariantViolation, match="daily_notional_cap"):
        decisions.approve_decision(s, ids[1], "direct_user:owner")


def test_slippage_sign_follows_the_side_of_the_trade():
    s = Store(":memory:", clock=fixed_clock("2026-10-10T12:00:00Z"))
    evidence.add_subject(s, "SUBJ_A", "Synthetic A", "security", subject_id="SUBJ_A")
    mid = decisions.propose_mandate(s, "scope_s", mandate_body(max_position_pct=100.0, max_subject_exposure_pct=100.0))
    decisions.activate_mandate(s, mid, "direct_user:owner")
    short_pos = decisions.add_snapshot(s, "scope_s", "SUBJ_A", "2026-10-10T11:59:00Z", "broker_position",
                                       quantity=-100, mark_price=50.0)

    def fill(direction, px, snaps=None):
        did = decisions.propose_decision(s, "scope_s", "trade", "action",
                                         {"direction": direction, "notional": 5_000, "venue": "venue_a",
                                          "expected_price": 50.0}, "r", subject_id="SUBJ_A", nav=100_000,
                                         asset_class="equity", position_snapshot_ids=snaps or [])
        decisions.approve_decision(s, did, "direct_user:owner")
        return receipts.get_receipt(s, decisions.record_execution(s, did, "venue:v", {"qty": 100, "px": px})).payload
    assert fill("buy", 50.5)["slippage_bps"] == 100.0       # paid more: cost
    assert fill("short", 50.5)["slippage_bps"] == -100.0    # sold higher: gain
    assert fill("cover", 49.5, [short_pos])["slippage_bps"] == -100.0   # bought back lower: gain


def test_reaffirm_clears_the_warning_and_tracks_the_new_basis(store, acme):
    sid, ev = acme["subject"], acme["evidence"]
    est = claims.add_claim(store, sid, ev, "valuation.ebit", "inference", "EBIT 190", created_at="2026-10-01T13:00:00Z")
    claims.add_dependency(store, est, acme["claim"])
    ev2 = evidence.add_evidence(store, sid, "ir_release", "synthetic://ACME/o2", "2026-10-02T00:00:00Z", "primary",
                                {"text": "guide 900"})
    c2 = claims.correct(store, sid, acme["claim"], ev2, "guide 900", created_at="2026-10-02T00:05:00Z")
    assert claims.get_claim(store, est).status == "stale"
    claims.reaffirm(store, est, "reviewer:pm", "EBIT unaffected")
    pkt = retrieval.build_packet(store, sid, "ebit", "2026-10-03T00:00:00Z", RUNTIME)
    assert not any(f"claim:{est} rests on" in w for w in pkt.warnings)
    # the reaffirmed claim now rests on the correction; when that changes, it goes stale again
    ev3 = evidence.add_evidence(store, sid, "ir_release", "synthetic://ACME/o3", "2026-10-04T00:00:00Z", "primary",
                                {"text": "guide 850"})
    claims.correct(store, sid, c2, ev3, "guide 850", created_at="2026-10-04T00:05:00Z")
    assert claims.get_claim(store, est).status == "stale"
    with pytest.raises(sqlite3.IntegrityError, match="reaffirm receipt"):
        store.db.execute("UPDATE claim SET status='active' WHERE id=?", (est,))


def test_known_as_of_rebuilds_statuses_as_they_were():
    """Replay reconstructs which revision was selected, which claims were
    contradicted, and which were stale at the time, not as they are now."""
    clock = {"t": "2026-10-05T00:00:00Z"}
    s = Store(":memory:", clock=lambda: clock["t"])
    sid = evidence.add_subject(s, "ACME", "Synthetic ACME")
    ev = evidence.add_evidence(s, sid, "ir_release", "synthetic://ACME/a", "2026-10-01T00:00:00Z", "primary",
                               {"text": "guide 950"})
    base = claims.add_claim(s, sid, ev, "guidance.fy_revenue", "fact", "guide 950", created_at="2026-10-01T00:00:00Z")
    est = claims.add_claim(s, sid, ev, "valuation.ebit", "inference", "EBIT 190", created_at="2026-10-01T00:00:00Z")
    claims.add_dependency(s, est, base)
    ev_b = evidence.add_evidence(s, sid, "broker_note", "synthetic://n/1", "2026-10-02T00:00:00Z",
                                 "credible_secondary", {"text": "margin 12"})
    m1 = claims.add_claim(s, sid, ev_b, "unit.margin", "forecast", "margin 12", created_at="2026-10-02T00:00:00Z")
    m2 = claims.add_claim(s, sid, ev_b, "unit.margin", "forecast", "margin -3", created_at="2026-10-02T00:00:00Z")
    claims.contradict(s, m1, m2)
    r1 = underwriting.draft_revision(s, sid, "12m", "r1", cases_for(ev), created_at="2026-10-02T00:00:00Z")
    underwriting.review_revision(s, r1, "reviewer:pm")
    underwriting.select_revision(s, r1, "direct_user:owner")
    then = "2026-10-05T00:00:00Z"
    before = retrieval.build_packet(s, sid, "guide ebit margin r1", "2026-10-06T00:00:00Z", RUNTIME, known_as_of=then)

    clock["t"] = "2026-10-09T00:00:00Z"   # later: correction, resolution, a new selection
    ev2 = evidence.add_evidence(s, sid, "ir_release", "synthetic://ACME/b", "2026-10-08T00:00:00Z", "primary",
                                {"text": "guide 900"})
    claims.correct(s, sid, base, ev2, "guide 900", created_at="2026-10-08T00:00:00Z")
    claims.resolve_contradiction(s, m1, m2, "reviewer:pm", ev_b)
    r2 = underwriting.draft_revision(s, sid, "12m", "r2", cases_for(ev), created_at="2026-10-08T00:00:00Z")
    underwriting.review_revision(s, r2, "reviewer:pm")
    underwriting.select_revision(s, r2, "direct_user:owner")

    replay = retrieval.build_packet(s, sid, "guide ebit margin r1", "2026-10-06T00:00:00Z", RUNTIME, known_as_of=then)
    assert replay.packet_hash == before.packet_hash
    sel = {x["ref"]: x for x in replay.selected}
    assert f"case_set:{r1}" in sel and "selected underwriting" in sel[f"case_set:{r1}"]["why"]
    assert f"case_set:{r2}" not in sel
    assert any("unresolved contradiction" in w for w in replay.warnings)
    assert not any(f"claim:{est} rests on" in w for w in replay.warnings)
