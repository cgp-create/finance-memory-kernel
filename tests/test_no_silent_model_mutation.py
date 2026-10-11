"""Canonical test 5: retrieval, summarisation and draft generation cannot modify
selected underwriting or cases. Also invariant 3 at the SQL level."""
import sqlite3

import pytest

from conftest import RUNTIME, cases_for
from fmk import retrieval, underwriting
from fmk.models import InvariantViolation

UNDERWRITING_TABLES = ("case_set_revision", "research_case", "receipt")


def test_retrieve_summarize_draft_leave_underwriting_untouched(store, acme, selected):
    before = store.snapshot_tables(*UNDERWRITING_TABLES)
    pkt = retrieval.build_packet(store, acme["subject"], "current underwriting and guidance", "2026-10-05T00:00:00Z", RUNTIME)
    text = retrieval.summarize(store, pkt)
    assert "selected revision" in text
    rendered = retrieval.render_output(pkt, text)
    assert "not an exhaustive record" in rendered
    draft = retrieval.propose_draft_from_packet(store, pkt, "12m", "model proposes lower bull",
                                                cases_for(acme["evidence"], 0.15, 0.5, 0.35), proposed_by="model")
    after = store.snapshot_tables("case_set_revision", "research_case", "receipt")
    # receipts unchanged, selected revision row unchanged, cases of selected revision unchanged
    assert after["receipt"] == before["receipt"]
    sel_before = [r for r in before["case_set_revision"] if r[0] == selected]
    sel_after = [r for r in after["case_set_revision"] if r[0] == selected]
    assert sel_before == sel_after
    cases_before = [r for r in before["research_case"] if r[1] == selected]
    cases_after = [r for r in after["research_case"] if r[1] == selected]
    assert cases_before == cases_after
    # the only new rows are the draft and its cases
    new_revs = [r for r in after["case_set_revision"] if r[0] not in {x[0] for x in before["case_set_revision"]}]
    assert [r[0] for r in new_revs] == [draft]
    d = underwriting.get_revision(store, draft)
    assert d.status == "draft" and d.parent_id == selected and d.proposed_by == "model"
    assert underwriting.selected_revision(store, acme["subject"], "12m").id == selected
    # the diff is a read
    diff = underwriting.diff_revisions(store, selected, draft)
    assert {c["label"] for c in diff["changed"]} == {"bull", "bear"}


def test_cases_and_revision_bodies_are_frozen(store, acme, selected):
    case = underwriting.cases_of(store, selected)[0]
    with pytest.raises(sqlite3.IntegrityError, match="invariant 3"):
        store.db.execute("UPDATE research_case SET probability = 0.9 WHERE id = ?", (case.id,))
    with pytest.raises(sqlite3.IntegrityError, match="invariant 3"):
        store.db.execute("DELETE FROM research_case WHERE id = ?", (case.id,))
    with pytest.raises(sqlite3.IntegrityError, match="invariant 3"):
        store.db.execute("UPDATE case_set_revision SET thesis_summary = 'edited' WHERE id = ?", (selected,))
    with pytest.raises(sqlite3.IntegrityError, match="invariant 3"):
        store.db.execute(
            "INSERT INTO research_case (id, revision_id, label, probability, return_basis, assumptions) "
            "VALUES ('x', ?, 'extra', 0.1, 'price_only', '[]')", (selected,))
    with pytest.raises(InvariantViolation):
        store.update("research_case", case.id, probability=0.9)


def test_revision_status_moves_forward_only(store, acme, selected):
    with pytest.raises(sqlite3.IntegrityError, match="forward only"):
        store.db.execute("UPDATE case_set_revision SET status='draft' WHERE id=?", (selected,))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.db.execute("DELETE FROM case_set_revision WHERE id=?", (selected,))


def test_packet_is_immutable(store, acme):
    pkt = retrieval.build_packet(store, acme["subject"], "guidance", "2026-10-05T00:00:00Z", RUNTIME)
    with pytest.raises(sqlite3.IntegrityError, match="invariant 7"):
        store.db.execute("UPDATE context_packet SET warnings='[]' WHERE id=?", (pkt.id,))
