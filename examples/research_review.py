"""Research review on frozen fixture data.

Replays the stale_price_conflict fixture, builds a context packet for a
factual question, prints rank explanations and warnings, shows that a
rebuild gives the same hash, and shows what a model may do with the
packet (draft a proposal) and what it may not (review or select it, even
after a human has reviewed it). The contradiction in the fixture is left
unresolved: both claims are shown and the packet says so.

Run: python examples/research_review.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fmk import fixtures, retrieval, underwriting  # noqa: E402
from fmk.models import InvariantViolation  # noqa: E402

RUNTIME = {"model_id": "synthetic-model", "model_version": "1", "provider": "synthetic",
           "route_id": "r1", "fallback_used": False}


def main() -> None:
    run = fixtures.load("stale_price_conflict")
    store, acme = run.store, run.ids["acme"]
    print(f"fixture stale_price_conflict sha256 {fixtures.fixture_hash('stale_price_conflict')[:16]}")

    q = "What is the last price and the contribution margin per unit?"
    p1 = retrieval.build_packet(store, acme, q, "2026-10-10T09:00:00Z", RUNTIME, task_kind="factual_state")
    p2 = retrieval.build_packet(store, acme, q, "2026-10-10T09:00:00Z", RUNTIME, task_kind="factual_state")
    print(f"packet hash {p1.packet_hash[:16]}; rebuilt {p2.packet_hash[:16]}; equal {p1.packet_hash == p2.packet_hash}")

    print("\nselected, in rank order (authority first, then lexical overlap):")
    for s, e in zip(p1.selected, p1.ranking_explanations):
        print(f"  {s['ref']:<22} authority {e['authority']} lexical {e['lexical']:.2f}  {s['why']}")
    print("\nomitted:")
    for o in p1.omitted:
        print(f"  {o['ref']:<22} {o['why']}")
    print("\nwarnings:")
    for w in p1.warnings:
        print(f"  {w}")

    print("\nsummary (a pure read):")
    print(retrieval.summarize(store, p1))

    print("\nwhat a model may do with this packet: draft a proposal")
    draft = retrieval.propose_draft_from_packet(store, p1, "12m", "proposal from packet", [
        {"label": "base", "probability": 1.0, "return_basis": "price_only", "assumptions": ["margin question open"]},
    ])
    print(f"  draft {draft} status {underwriting.get_revision(store, draft).status}")
    print("what it may not do: review it, or select it once a human has")
    try:
        underwriting.review_revision(store, draft, "model:assistant")
    except InvariantViolation as e:
        print(f"  review refused: {e}")
    underwriting.review_revision(store, draft, "reviewer:synthetic_pm")
    try:
        underwriting.select_revision(store, draft, "model:assistant")
    except InvariantViolation as e:
        print(f"  selection refused: {e}")
    print(f"\nselected revision for ACME 12m: {underwriting.selected_revision(store, acme, '12m')}")


if __name__ == "__main__":
    main()
