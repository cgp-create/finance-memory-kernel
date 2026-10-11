"""JSONL fixture loader.

What this module does: reads a fixture file line by line and replays each
operation against a Store through the same public functions a caller
would use, so that fixtures exercise the invariants rather than bypass
them. Each line is one JSON object with an "op" field. A line may name
itself with "as" so later lines can refer to it as "$name". An "expect"
line records an assertion the tests check; an op may carry
"expect_error": true to say the kernel must refuse it.

What this module does not do: it does not fabricate ids outside the file;
with a fixed clock the replay of a fixture is byte-for-byte reproducible,
which is what packet replay tests rely on. It contains no real names,
tickers, accounts or credentials and is checked for that by a test.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

from . import claims, decisions, evidence, receipts, retrieval, underwriting
from .models import InvariantViolation, KernelError, MandateBody
from .store import Store

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def fixture_path(name: str) -> Path:
    p = Path(name)
    if p.suffix != ".jsonl":
        p = FIXTURE_DIR / f"{name}.jsonl"
    return p


def fixture_hash(name: str) -> str:
    return hashlib.sha256(fixture_path(name).read_bytes()).hexdigest()


def read_lines(name: str) -> list[dict]:
    out = []
    for raw in fixture_path(name).read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#"):
            out.append(json.loads(raw))
    return out


class FixtureRun:
    """Result of replaying a fixture: the store, name->id aliases, expectations
    and the errors the kernel raised where the fixture said it should."""

    def __init__(self, store: Store):
        self.store = store
        self.ids: dict[str, str] = {}
        self.expectations: list[dict] = []
        self.failures: list[str] = []
        self.refused: list[dict] = []
        self.results: dict[str, Any] = {}

    def ref(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            name = value[1:]
            if name not in self.ids:
                raise KernelError(f"fixture alias {value} not defined yet")
            return self.ids[name]
        if isinstance(value, list):
            return [self.ref(v) for v in value]
        if isinstance(value, dict):
            return {k: self.ref(v) for k, v in value.items()}
        return value


def fixed_clock(start: str = "2026-10-10T00:00:00Z") -> Callable[[], str]:
    """A clock that returns the same timestamp every call. Fixtures carry their
    own as_of and observed_at values, so the wall clock need not advance."""
    return lambda: start


def load(name: str, store: Optional[Store] = None, clock: Optional[Callable[[], str]] = None,
         stop_at: Optional[int] = None) -> FixtureRun:
    store = store or Store(":memory:", clock=clock or fixed_clock(), deterministic_ids=True)
    run = FixtureRun(store)
    for i, line in enumerate(read_lines(name)):
        if stop_at is not None and i >= stop_at:
            break
        op = line.get("op")
        if op == "expect":
            run.expectations.append(line)
            run.failures.extend(_check(run, line))
            continue
        alias = line.get("as")
        want_error = bool(line.get("expect_error"))
        try:
            result = _apply(run, op, {k: v for k, v in line.items() if k not in ("op", "as", "expect_error")})
        except (InvariantViolation, KernelError) as e:
            if not want_error:
                raise
            run.refused.append({"line": i, "op": op, "error": str(e), "as": alias})
            continue
        if want_error:
            raise AssertionError(f"fixture {name} line {i}: op {op} was expected to be refused but succeeded")
        if alias:
            run.ids[alias] = result if isinstance(result, str) else getattr(result, "id", result)
            run.results[alias] = result
    return run


def _apply(run: FixtureRun, op: str, a: dict) -> Any:
    s = run.store
    a = run.ref(a)
    if op == "subject":
        return evidence.add_subject(s, a["key"], a.get("title", a["key"]), a.get("type", "issuer"),
                                    a.get("namespace", "demo"), subject_id=a.get("id"))
    if op == "evidence":
        return evidence.add_evidence(s, a["subject_id"], a["source_kind"], a["source_ref"], a["observed_at"],
                                     a["authority_tier"], a["payload"], a.get("supersedes_evidence_id"),
                                     evidence_id=a.get("id"))
    if op == "claim":
        return claims.add_claim(s, a["subject_id"], a["evidence_id"], a["aspect"], a["kind"], a["statement"],
                                value=a.get("value"), confidence=a.get("confidence", 0.5),
                                source_strength=a.get("source_strength"), status=a.get("status", "active"),
                                expires_at=a.get("expires_at"), created_at=a.get("created_at"),
                                claim_id=a.get("id"))
    if op == "relation":
        return claims.add_relation(s, a["from"], a["relation"], a["to"], a.get("rationale_evidence_id"))
    if op == "supersede":
        return claims.supersede(s, a["new"], a["old"], a.get("rationale_evidence_id"))
    if op == "correct":
        return claims.correct(s, a["subject_id"], a["old"], a["evidence_id"], a["statement"],
                              value=a.get("value"), confidence=a.get("confidence", 0.9),
                              created_at=a.get("created_at"))
    if op == "contradict":
        return claims.contradict(s, a["a"], a["b"], a.get("rationale_evidence_id"))
    if op == "resolve":
        return claims.resolve_contradiction(s, a["winner"], a["loser"], a["authority_ref"], a["evidence_id"])
    if op == "question":
        return claims.open_question(s, a["subject_id"], a["question"], a["evidence_needed"],
                                    a.get("owner", "research"), a.get("priority", 3), a.get("revisit_at"),
                                    created_at=a.get("created_at"))
    if op == "revision":
        return underwriting.draft_revision(s, a["subject_id"], a["horizon"], a["thesis_summary"], a["cases"],
                                           parent_id=a.get("parent_id"), proposed_by=a.get("proposed_by", "human"),
                                           revision_id=a.get("id"), created_at=a.get("created_at"))
    if op == "review":
        return underwriting.review_revision(s, a["revision_id"], a["authority_ref"], a.get("notes", ""))
    if op == "select":
        return underwriting.select_revision(s, a["revision_id"], a["authority_ref"],
                                            retire_current=a.get("retire_current", True))
    if op == "invalidation":
        return underwriting.add_invalidation(s, a["subject_id"], a["condition"], a["observable_definition"],
                                             a.get("severity", "material"), a.get("case_id"), a.get("evidence_ids"),
                                             created_at=a.get("created_at"))
    if op == "link_against":
        return underwriting.link_evidence_against(s, a["invalidation_id"], a["evidence_id"])
    if op == "trigger":
        return underwriting.add_trigger(s, a["subject_id"], a["name"], a["condition"], a.get("trigger_type", "event"),
                                        a.get("action_type", "review"), a.get("priority", 3))
    if op == "receipt":
        return receipts.add_receipt(s, a["kind"], a["object_ref"], a["authority_ref"], a.get("payload"),
                                    a.get("prior_receipt_id"), a.get("occurred_at"))
    if op == "mandate":
        return decisions.propose_mandate(s, a["scope_ref"], MandateBody(**a["body"]), a.get("parent_id"))
    if op == "activate_mandate":
        return decisions.activate_mandate(s, a["mandate_id"], a["authority_ref"])
    if op == "retire_mandate":
        return decisions.retire_mandate(s, a["mandate_id"], a.get("authority_ref", "direct_user:owner"))
    if op == "snapshot":
        return decisions.add_snapshot(s, a["scope_ref"], a["subject_id"], a["as_of"], a["source_class"],
                                      quantity=a.get("quantity"), mark_price=a.get("mark_price"),
                                      mark_as_of=a.get("mark_as_of"), mark_source_class=a.get("mark_source_class"),
                                      cash_available=a.get("cash_available"), source_ref=a.get("source_ref"),
                                      cost_basis=a.get("cost_basis"), pending_order_refs=a.get("pending_order_refs"))
    if op == "decision":
        m = decisions.active_mandate(s, a["scope_ref"])
        if m is None:
            raise InvariantViolation("no active mandate for scope")
        chk = decisions.check_against_mandate(a.get("proposal", {}), m, nav=a.get("nav", 0.0),
                                              current_subject_exposure_pct=a.get("current_subject_exposure_pct", 0.0),
                                              venue=a.get("venue"), asset_class=a.get("asset_class"),
                                              subject_id=a.get("subject_id"))
        return decisions.propose_decision(s, a["scope_ref"], a["decision_kind"], a["intent_rung"],
                                          a.get("proposal", {}), a["rationale_text"], chk,
                                          subject_id=a.get("subject_id"), evidence_ids=a.get("evidence_ids"),
                                          selected_revision_id=a.get("selected_revision_id"),
                                          position_snapshot_ids=a.get("position_snapshot_ids"),
                                          snapshot_freshness=a.get("snapshot_freshness", "fresh"),
                                          conviction=a.get("conviction"),
                                          change_triggers_text=a.get("change_triggers_text"),
                                          proposed_by=a.get("proposed_by", "model"), intent_ref=a.get("intent_ref"))
    if op == "approve":
        return decisions.approve_decision(s, a["decision_id"], a["authority_ref"], a.get("override"))
    if op == "execute":
        return decisions.record_execution(s, a["decision_id"], a["authority_ref"], a.get("fill", {}))
    if op == "reconcile":
        return decisions.reconcile(s, a["decision_id"], a["authority_ref"], a["snapshot_id"])
    if op == "reject":
        return decisions.reject_decision(s, a["decision_id"], a["authority_ref"], a["reason"])
    if op == "outcome":
        return decisions.attach_outcome(s, a["decision_id"], a["outcome"], a.get("authority_ref", "system:evaluator"))
    if op == "packet":
        return retrieval.build_packet(s, a["subject_id"], a["query"], a["as_of"], a["runtime"],
                                      task_kind=a.get("task_kind", "research_update"), budget=a.get("budget", 12),
                                      packet_id=a.get("id"))
    raise KernelError(f"unknown fixture op {op!r}")


def check_expectations(run: FixtureRun) -> list[str]:
    """Failures from the 'expect' lines, which are evaluated inline during load
    so that a mid-fixture state (a plan before its fill) can be asserted."""
    return list(run.failures)


def _check(run: FixtureRun, e: dict) -> list[str]:
    s = run.store
    failures: list[str] = []

    def _ref(v: Any) -> Any:
        if isinstance(v, str) and "$" in v and not v.startswith("$"):
            # inline alias inside a ref like "case_set:$rev2"
            head, _, tail = v.partition("$")
            return head + run.ids[tail]
        return run.ref(v)

    for e in [e]:
        kind = e["check"]
        try:
            if kind == "selected_revision":
                r = underwriting.selected_revision(s, _ref(e["subject"]), e["horizon"])
                ok = r is not None and r.id == _ref(e["is"])
            elif kind == "selected_count":
                n = len(s.query("case_set_revision", "subject_id=? AND horizon=? AND status='selected'",
                                (_ref(e["subject"]), e["horizon"])))
                ok = n == e["is"]
            elif kind == "claim_status":
                ok = s.get("claim", _ref(e["claim"])).status == e["is"]
            elif kind == "packet_contains":
                ok = _ref(e["ref"]) in retrieval.packet_refs(s.get("context_packet", _ref(e["packet"])))
            elif kind == "packet_omits":
                ok = _ref(e["ref"]) not in retrieval.packet_refs(s.get("context_packet", _ref(e["packet"])))
            elif kind == "packet_warning_contains":
                ok = any(e["text"] in w for w in s.get("context_packet", _ref(e["packet"])).warnings)
            elif kind == "rendered_output_starts_with":
                ok = retrieval.render_output(s.get("context_packet", _ref(e["packet"])), "body").startswith(e["text"])
            elif kind in ("position_warning_contains", "position_status", "position_source_class"):
                cur = decisions.current_position(s, e["scope"], _ref(e["subject"]), now=e.get("now"),
                                                 mandate=decisions.active_mandate(s, e["scope"]))
                if kind == "position_warning_contains":
                    ok = any(e["text"] in w for w in cur.get("warnings", []))
                elif kind == "position_status":
                    ok = cur["status"] == e["is"]
                else:
                    ok = cur.get("source_class") == e["is"]
            elif kind == "decision_status":
                ok = decisions.get_decision(s, _ref(e["decision"])).status == e["is"]
            elif kind == "decision_has_no_approval":
                ok = decisions.get_decision(s, _ref(e["decision"])).approval_receipt_id is None
            elif kind == "mandate_check_status":
                ok = decisions.get_decision(s, _ref(e["decision"])).mandate_check["status"] == e["is"]
            elif kind == "mandate_check_breach_contains":
                ok = any(e["text"] in b for b in decisions.get_decision(s, _ref(e["decision"])).mandate_check["breaches"])
            elif kind == "declined_count":
                ok = len(decisions.declined_decisions(s, e["scope"])) == e["is"]
            else:
                ok = False
                failures.append(f"unknown check {kind}")
                continue
        except Exception as ex:  # noqa: BLE001
            failures.append(f"{kind}: raised {ex!r}")
            continue
        if not ok:
            failures.append(f"{kind}: {e}")
    return failures
