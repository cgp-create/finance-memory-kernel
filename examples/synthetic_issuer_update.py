"""Section 6.1 lifecycle end to end, on synthetic data.

What this script does: walks one synthetic issuer event through the kernel
in order: evidence, claims and supersession, an open question, a draft
successor revision with its diff, review and a selection receipt, a
context packet with its runtime block, a decision checked against an
active mandate, approval, execution, reconciliation, and the receipt
chain that proves each step. It prints the chain as it goes.

What this script does not do: it uses no network, no real issuer, no real
ticker and no account. Every number is made up. It does not size the
trade; the notional is a literal in this file.

Run: python examples/synthetic_issuer_update.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fmk import claims, decisions, evidence, receipts, retrieval, underwriting  # noqa: E402
from fmk.models import InvariantViolation, MandateBody  # noqa: E402
from fmk.store import Store  # noqa: E402

RUNTIME = {"model_id": "synthetic-model", "model_version": "1", "provider": "synthetic",
           "route_id": "r1", "fallback_used": False}


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def main() -> None:
    store = Store(":memory:")
    acme = evidence.add_subject(store, "ACME", "Synthetic issuer ACME", "issuer")

    step(1, "Prior state: initial guidance as primary evidence, claims, a selected revision")
    ev_prior = evidence.add_evidence(store, acme, "ir_release", "synthetic://ACME/fy-outlook-initial",
                                     "2026-07-15T12:00:00Z", "primary",
                                     {"text": "FY revenue guidance 1000 to 1040; operating margin 18 to 19 percent"})
    c_guide_old = claims.add_claim(store, acme, ev_prior, "guidance.fy_revenue", "fact",
                                   "FY revenue guidance is 1000 to 1040", value={"low": 1000, "high": 1040},
                                   confidence=0.9, created_at="2026-07-15T12:05:00Z")
    c_margin_old = claims.add_claim(store, acme, ev_prior, "guidance.op_margin", "fact",
                                    "Operating margin guidance 18 to 19 percent", value={"low": 18, "high": 19},
                                    confidence=0.9, created_at="2026-07-15T12:05:00Z")
    rev1 = underwriting.draft_revision(store, acme, "12m", "Base case: guidance met, multiple holds", [
        {"label": "bull", "probability": 0.3, "return_basis": "price_only", "assumptions": ["revenue at top of guide"],
         "evidence_for_ids": [ev_prior], "price_target": 60, "expected_return_pct": 25,
         "return_anchor_at": "2026-07-15T12:00:00Z"},
        {"label": "base", "probability": 0.5, "return_basis": "price_only", "assumptions": ["guidance met"],
         "evidence_for_ids": [ev_prior], "price_target": 50, "expected_return_pct": 4,
         "return_anchor_at": "2026-07-15T12:00:00Z"},
        {"label": "bear", "probability": 0.2, "return_basis": "total_return", "assumptions": ["demand softens"],
         "price_target": 38, "expected_return_pct": -20, "return_anchor_at": "2026-07-15T12:00:00Z"},
    ], created_at="2026-07-16T09:00:00Z")
    underwriting.review_revision(store, rev1, "reviewer:synthetic_pm", "initial underwriting")
    sel1 = underwriting.select_revision(store, rev1, "direct_user:owner")
    inv_bull = underwriting.add_invalidation(store, acme, "FY revenue guidance cut below 1000",
                                             "primary issuer release with FY revenue high end below 1000",
                                             created_at="2026-07-16T10:00:00Z")
    print(f"  evidence {ev_prior}; claims {c_guide_old}, {c_margin_old}")
    print(f"  revision {rev1} selected; selection receipt {sel1}")

    step(2, "Source arrives: the issuer cuts guidance. Stored as immutable primary evidence")
    ev_cut = evidence.add_evidence(store, acme, "ir_release", "synthetic://ACME/fy-outlook-revised",
                                   "2026-10-01T12:00:00Z", "primary",
                                   {"text": "FY revenue guidance reduced to 950 to 980; operating margin now 15 to 16 percent"})
    print(f"  evidence {ev_cut} hash {evidence.get_evidence(store, ev_cut).content_hash[:12]}")
    try:
        store.db.execute("UPDATE evidence SET payload='{}' WHERE id=?", (ev_prior,))
    except Exception as e:  # noqa: BLE001
        print(f"  attempt to edit old evidence refused: {e}")

    step(3, "Claims with authority, aspect and expiry; supersession scoped to subject and aspect")
    c_guide_new = claims.add_claim(store, acme, ev_cut, "guidance.fy_revenue", "fact",
                                   "FY revenue guidance reduced to 950 to 980", value={"low": 950, "high": 980},
                                   confidence=0.95, created_at="2026-10-01T12:10:00Z")
    claims.supersede(store, c_guide_new, c_guide_old, ev_cut)
    c_margin_new = claims.add_claim(store, acme, ev_cut, "guidance.op_margin", "fact",
                                    "Operating margin assumption under pressure: 15 to 16 percent",
                                    value={"low": 15, "high": 16}, confidence=0.95, created_at="2026-10-01T12:10:00Z")
    claims.supersede(store, c_margin_new, c_margin_old, ev_cut)
    c_infer = claims.add_claim(store, acme, ev_cut, "thesis.bull_case", "inference",
                               "Bull case needs reassessment after the guidance cut", confidence=0.6,
                               source_strength="assistant_inference", created_at="2026-10-01T12:15:00Z")
    for cid in (c_guide_old, c_guide_new, c_margin_old, c_infer):
        c = claims.get_claim(store, cid)
        print(f"  claim {cid} [{c.aspect}] {c.status}, expires {c.expires_at}")
    underwriting.link_evidence_against(store, inv_bull, ev_cut)
    print(f"  evidence linked against invalidation {inv_bull}; selected revision probabilities unchanged")

    step(4, "Open a research question with evidence needed and a revisit date")
    q = claims.open_question(store, acme, "Is the guide cut demand-led, supply-led, or deliberate pruning?",
                             "management commentary on segment mix; channel data", priority=1,
                             revisit_at="2026-10-20T00:00:00Z", created_at="2026-10-01T13:00:00Z")
    print(f"  question {q}")

    step(5, "Model drafts a successor revision; diff against the selected one")
    rev2 = underwriting.draft_revision(store, acme, "12m", "Proposed: guidance cut lowers base and bull", [
        {"label": "bull", "probability": 0.15, "return_basis": "price_only", "assumptions": ["cut is deliberate pruning"],
         "evidence_against_ids": [ev_cut], "price_target": 55, "expected_return_pct": 15,
         "return_anchor_at": "2026-10-01T12:00:00Z"},
        {"label": "base", "probability": 0.5, "return_basis": "price_only", "assumptions": ["new guidance met"],
         "evidence_for_ids": [ev_cut], "price_target": 45, "expected_return_pct": -6,
         "return_anchor_at": "2026-10-01T12:00:00Z"},
        {"label": "bear", "probability": 0.35, "return_basis": "total_return", "assumptions": ["demand-led, further cuts"],
         "evidence_for_ids": [ev_cut], "price_target": 34, "expected_return_pct": -29,
         "return_anchor_at": "2026-10-01T12:00:00Z"},
    ], parent_id=rev1, proposed_by="model", created_at="2026-10-02T09:00:00Z")
    diff = underwriting.diff_revisions(store, rev1, rev2)
    for ch in diff["changed"]:
        print(f"  {ch['label']}: " + ", ".join(f"{k} {v['from']} -> {v['to']}" for k, v in ch.items() if k != "label"))
    try:
        underwriting.select_revision(store, rev2, "model:assistant")
    except InvariantViolation as e:
        print(f"  model tried to select its own draft: refused ({e})")
    print(f"  selected revision is still {underwriting.selected_revision(store, acme, '12m').id}")

    step(6, "Human review and selection; receipt names the authority and the reviewed revision")
    underwriting.review_revision(store, rev2, "reviewer:synthetic_pm", "diff reviewed against rev1")
    sel2 = underwriting.select_revision(store, rev2, "direct_user:owner")
    r = receipts.get_receipt(store, sel2)
    print(f"  selection receipt {sel2}: {r.authority_ref} selected {r.object_ref}, replaces {r.payload['replaces']}")
    print(f"  rev1 status {underwriting.get_revision(store, rev1).status}; rev2 status {underwriting.get_revision(store, rev2).status}")

    step(7, "Context packet for a later research answer, with runtime block")
    pkt = retrieval.build_packet(store, acme, "What is the FY revenue guidance and what is the current underwriting?",
                                 "2026-10-05T00:00:00Z", RUNTIME)
    for s in pkt.selected:
        print(f"  + {s['ref']}: {s['why']}")
    for o in pkt.omitted:
        print(f"  - {o['ref']}: {o['why']}")
    print(f"  warnings: {pkt.warnings or 'none'}")
    print(f"  packet {pkt.id} hash {pkt.packet_hash[:16]} runtime {pkt.runtime['model_id']} fallback {pkt.runtime['fallback_used']}")

    step(8, "Decision: mandate, position snapshot, proposal, mandate check")
    mid = decisions.propose_mandate(store, "scope_demo", MandateBody(
        objective_text="growth sleeve", horizon="12m", allowed_asset_classes=["equity"], allowed_venues=["venue_a"],
        max_position_pct=10.0, max_subject_exposure_pct=15.0, per_trade_notional_cap=50_000))
    act = decisions.activate_mandate(store, mid, "direct_user:owner")
    m = decisions.active_mandate(store, "scope_demo")
    snap = decisions.add_snapshot(store, "scope_demo", acme, "2026-10-05T09:00:00Z", "broker_position",
                                  quantity=100, mark_price=46.0, mark_as_of="2026-10-05T08:59:00Z")
    pos = decisions.current_position(store, "scope_demo", acme, now="2026-10-05T09:05:00Z", mandate=m)
    proposal = {"direction": "sell", "notional": 2300, "quantity": 50, "venue": "venue_a"}
    chk = decisions.check_against_mandate(proposal, m, nav=100_000, venue="venue_a", asset_class="equity", subject_id=acme)
    did = decisions.propose_decision(store, "scope_demo", "trade", "action", proposal,
                                     "trim half the position after the guidance cut; bear probability now 35 percent",
                                     chk, subject_id=acme, evidence_ids=[ev_cut], selected_revision_id=rev2,
                                     position_snapshot_ids=[snap], snapshot_freshness=decisions.snapshot_freshness([pos]))
    print(f"  mandate {mid} v{m.version} active by receipt {act}")
    print(f"  position {pos['status']} qty {pos['quantity']} from {pos['source_class']}; warnings {pos['warnings'] or 'none'}")
    print(f"  decision {did} status proposed; mandate_check {chk['status']} against mandate v{chk['mandate_version']}")
    try:
        decisions.record_execution(store, did, "venue_a", {"qty": 50, "px": 46.1})
    except InvariantViolation as e:
        print(f"  execution before approval refused: {e}")

    step(9, "Approval, execution and reconcile: three separate receipts")
    appr = decisions.approve_decision(store, did, "direct_user:owner")
    exe = decisions.record_execution(store, did, "venue_a", {"qty": 50, "px": 46.1, "fill_ref": "synthetic://venue_a/fill/1"})
    snap2 = decisions.add_snapshot(store, "scope_demo", acme, "2026-10-05T09:30:00Z", "reconciled",
                                   quantity=50, mark_price=46.1, mark_as_of="2026-10-05T09:30:00Z",
                                   source_ref="synthetic://venue_a/fill/1")
    rec = decisions.reconcile(store, did, "system:reconciler", snap2)
    d = decisions.get_decision(store, did)
    print(f"  decision status {d.status}; approval {appr}; execution {exe}; reconcile {rec}")
    pos2 = decisions.current_position(store, "scope_demo", acme, now="2026-10-05T09:35:00Z", mandate=m)
    print(f"  position now qty {pos2['quantity']} from {pos2['source_class']}")

    step(10, "The chain, from receipts alone")
    print(f"  case_set:{rev2} -> {receipts.status_of(store, f'case_set:{rev2}')}")
    print(f"  decision:{did} -> {receipts.status_of(store, f'decision:{did}')}")
    for link in decisions.decision_chain(store, did):
        print(f"  {link['at']} {link['kind']:<10} by {link['authority']} ({link['id']})")
    print("\nDone. Nothing in this run touched a real market, issuer, account or model.")


if __name__ == "__main__":
    main()
