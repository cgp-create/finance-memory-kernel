"""Shared fixtures for the test suite. All data is synthetic."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fmk import claims, decisions, evidence, underwriting  # noqa: E402
from fmk.models import MandateBody  # noqa: E402
from fmk.store import Store  # noqa: E402

RUNTIME = {"model_id": "synthetic-model", "model_version": "1", "provider": "synthetic",
           "route_id": "r1", "fallback_used": False}


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def acme(store):
    """A subject with one primary evidence item and one active claim."""
    sid = evidence.add_subject(store, "ACME", "Synthetic issuer ACME")
    ev = evidence.add_evidence(store, sid, "ir_release", "synthetic://ACME/outlook", "2026-10-01T12:00:00Z",
                               "primary", {"text": "FY revenue guidance 950 to 980"})
    c = claims.add_claim(store, sid, ev, "guidance.fy_revenue", "fact", "FY revenue guidance 950 to 980",
                         value={"low": 950, "high": 980}, confidence=0.9, created_at="2026-10-01T12:05:00Z")
    return {"subject": sid, "evidence": ev, "claim": c}


def cases_for(ev, bull=0.3, base=0.5, bear=0.2):
    return [
        {"label": "bull", "probability": bull, "return_basis": "price_only", "assumptions": ["growth"],
         "evidence_for_ids": [ev], "price_target": 60, "expected_return_pct": 25,
         "return_anchor_at": "2026-10-01T12:00:00Z"},
        {"label": "base", "probability": base, "return_basis": "price_only", "assumptions": ["guide met"],
         "evidence_for_ids": [ev]},
        {"label": "bear", "probability": bear, "return_basis": "total_return", "assumptions": ["demand softens"],
         "expected_return_pct": -20, "return_anchor_at": "2026-10-01T12:00:00Z"},
    ]


@pytest.fixture
def selected(store, acme):
    rid = underwriting.draft_revision(store, acme["subject"], "12m", "base thesis", cases_for(acme["evidence"]),
                                      created_at="2026-10-02T09:00:00Z")
    underwriting.review_revision(store, rid, "reviewer:synthetic_pm")
    underwriting.select_revision(store, rid, "direct_user:owner")
    return rid


def mandate_body(**kw):
    base = dict(objective_text="growth sleeve", horizon="12m",
                allowed_asset_classes=["equity", "crypto"], allowed_venues=["venue_a", "venue_b"],
                max_position_pct=10.0, max_subject_exposure_pct=15.0, per_trade_notional_cap=50_000)
    base.update(kw)
    return MandateBody(**base)


def flat_snapshot(store, scope_ref, subject_id):
    """A position-grade snapshot showing no holding, reused within a store, so
    test proposals cite a known current position as v0.3 requires."""
    rows = store.raw("SELECT id FROM position_snapshot WHERE scope_ref=? AND subject_id=? AND source_ref='flat'",
                     (scope_ref, subject_id))
    if rows:
        return rows[0][0]
    return decisions.add_snapshot(store, scope_ref, subject_id, store.clock(), "broker_position",
                                  quantity=0, source_ref="flat")


@pytest.fixture
def dstore():
    """Decision-layer store: active mandate on scope_test and a few subjects."""
    s = Store(":memory:")
    for key in ("SUBJ_A", "SUBJ_0", "SUBJ_1", "SUBJ_2", "EQ_1", "EQ_2"):
        evidence.add_subject(s, key, f"Synthetic {key}", "security", subject_id=key)
    mid = decisions.propose_mandate(s, "scope_test", mandate_body())
    decisions.activate_mandate(s, mid, "direct_user:owner")
    return s
