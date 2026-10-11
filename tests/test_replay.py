"""SPEC test 7: rebuilding a packet from frozen fixture data yields the same
object references, warnings and hash. Also: a changed input changes the hash,
and the hash covers ids and source hashes rather than rendered prose."""
from fmk import fixtures, retrieval


def _packet_from(name: str, alias: str):
    run = fixtures.load(name)
    assert run.failures == []
    return run, run.store.get("context_packet", run.ids[alias])


def test_packet_replay_same_hash_from_frozen_fixture():
    run1, p1 = _packet_from("issuer_guidance_cut", "pkt_after")
    run2, p2 = _packet_from("issuer_guidance_cut", "pkt_after")
    assert p1.packet_hash == p2.packet_hash
    assert retrieval.packet_refs(p1) == retrieval.packet_refs(p2)
    assert p1.warnings == p2.warnings
    assert p1.omitted == p2.omitted
    assert p1.selected == p2.selected
    assert p1.query_hash == p2.query_hash
    # rebuilding the packet again against the same store gives the same hash
    p3 = retrieval.build_packet(run1.store, run1.ids["acme"], p1.task["query"], p1.as_of, p1.runtime,
                                task_kind=p1.task["kind"])
    assert p3.packet_hash == p1.packet_hash and p3.id != p1.id


def test_replay_is_stable_across_all_packet_fixtures():
    for name, alias in (("stale_price_conflict", "pkt"), ("model_fallback", "pkt_fb")):
        _, a = _packet_from(name, alias)
        _, b = _packet_from(name, alias)
        assert a.packet_hash == b.packet_hash
        assert a.selected == b.selected and a.warnings == b.warnings


def test_hash_changes_when_inputs_change():
    run, p = _packet_from("issuer_guidance_cut", "pkt_after")
    later = retrieval.build_packet(run.store, run.ids["acme"], p.task["query"], "2026-10-06T00:00:00Z", p.runtime)
    assert later.packet_hash != p.packet_hash  # as_of is part of the hash
    other_q = retrieval.build_packet(run.store, run.ids["acme"], "a different question", p.as_of, p.runtime)
    assert other_q.packet_hash != p.packet_hash
    rt = dict(p.runtime, model_id="other-synthetic-model")
    other_rt = retrieval.build_packet(run.store, run.ids["acme"], p.task["query"], p.as_of, rt)
    assert other_rt.packet_hash != p.packet_hash  # runtime is part of the hash


def test_packet_stores_refs_and_hashes_not_prose():
    _, p = _packet_from("issuer_guidance_cut", "pkt_after")
    for s in p.selected:
        assert set(s) >= {"ref", "why", "as_of", "hash"}
        assert ":" in s["ref"]
        assert len(s["hash"]) == 64
    assert len(p.packet_hash) == 64
    assert p.runtime["model_id"] == "synthetic-model"
