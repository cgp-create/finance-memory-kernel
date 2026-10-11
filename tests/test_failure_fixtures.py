"""Tests 10 and 12 over the ten JSONL fixtures.

Every fixture replays through the public functions, every 'expect' line holds,
and every fixture that claims a failure state actually has one refused op
(invariant 12). The public-safety test scans the fixtures, examples and tests
for personal data, credentials, account identifiers and real tickers."""
import json
import re
from pathlib import Path

import pytest

from fmk import fixtures, receipts
from fmk.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_NAMES = [
    "issuer_guidance_cut",
    "stale_price_conflict",
    "correction_supersession",
    "duplicate_case_set",
    "weekend_mixed_marks",
    "plan_vs_fill",
    "approval_lost",
    "mandate_breach",
    "declined_decisions",
    "model_fallback",
]
# Fixtures whose failure state is a refused operation. The other three carry
# their failure state as a warning or a withheld quantity, asserted by expect lines.
FIXTURES_WITH_REFUSED_OP = {
    "issuer_guidance_cut", "correction_supersession", "duplicate_case_set",
    "approval_lost", "mandate_breach", "declined_decisions",
}
FIXTURES_WITH_WARNING_STATE = {"stale_price_conflict", "weekend_mixed_marks", "plan_vs_fill", "model_fallback"}


def test_all_ten_fixtures_exist():
    present = {p.stem for p in fixtures.FIXTURE_DIR.glob("*.jsonl")}
    assert present == set(FIXTURE_NAMES)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_replays_and_expectations_hold(name):
    run = fixtures.load(name)
    assert run.failures == [], run.failures
    assert run.expectations, f"{name} has no expect lines"


# Invariant 12: fixtures include failure states.
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_includes_a_failure_state(name):
    run = fixtures.load(name)
    if name in FIXTURES_WITH_REFUSED_OP:
        assert run.refused, f"{name} declares a failure state but nothing was refused"
    else:
        assert name in FIXTURES_WITH_WARNING_STATE
        checks = {e["check"] for e in run.expectations}
        assert checks & {"packet_warning_contains", "position_warning_contains", "position_status",
                         "rendered_output_starts_with"}, f"{name} asserts no warning or withheld state"


def test_health_check_receipts_for_fixtures():
    s = Store(":memory:")
    for name in FIXTURE_NAMES:
        run = fixtures.load(name)
        rid = receipts.health_check(s, name, fixtures.fixture_hash(name), passed=not run.failures)
        r = receipts.get_receipt(s, rid)
        assert r.payload["passed"] is True and r.payload["fixture_hash"] == fixtures.fixture_hash(name)
    assert len(s.query("receipt", "kind='health_check'")) == len(FIXTURE_NAMES)


def test_a_wrong_expectation_is_recorded_as_a_failed_health_check():
    run = fixtures.load("correction_supersession")
    wrong = fixtures._check(run, {"check": "claim_status", "claim": "$c_inf", "is": "active"})
    assert wrong, "a false expectation must fail"
    s = Store(":memory:")
    rid = receipts.health_check(s, "correction_supersession", fixtures.fixture_hash("correction_supersession"),
                                passed=not wrong)
    assert receipts.get_receipt(s, rid).payload["passed"] is False


# Test 10: public-safety fixture.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d{1,3}[ -]?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}")
_SECRET = re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token|bearer|aws_access|private[_-]?key)\s*[:=]")
_ACCOUNT = re.compile(r"(?i)\b(account|acct|iban|routing|swift)[_ ]?(no|number|id)?\s*[:=]\s*[A-Z0-9-]{6,}")
_URL_REAL = re.compile(r"https?://")
# A small list of widely known real ticker symbols. The fixtures must not use any.
_REAL_TICKERS = {
    "AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "BRK", "JPM", "NFLX", "AMD", "INTC",
    "BABA", "TSM", "SPY", "QQQ", "BTC", "ETH", "SOL", "COIN", "RBLX", "U", "EA", "TTWO", "NTDOY", "SONY",
}
_ALLOWED_KEYS = re.compile(r"^(ACME|SUBJ_[A-Z0-9]+|EQ_\d+|CX_\d+)$")


def _texts():
    me = Path(__file__).resolve()
    for p in list(fixtures.FIXTURE_DIR.glob("*.jsonl")) + list((ROOT / "examples").glob("*.py")) \
            + list((ROOT / "tests").glob("*.py")):
        if p.resolve() == me:
            continue  # this file holds the patterns it scans for
        yield p, p.read_text(encoding="utf-8")


def test_public_safety_no_personal_data_credentials_or_real_tickers():
    for path, text in _texts():
        assert not _EMAIL.search(text), f"{path}: email address"
        assert not _PHONE.search(text), f"{path}: phone number"
        assert not _SECRET.search(text), f"{path}: credential-like key"
        assert not _ACCOUNT.search(text), f"{path}: account identifier"
        assert not _URL_REAL.search(text), f"{path}: external URL; fixtures use synthetic:// refs"
        words = set(re.findall(r"\b[A-Z]{1,5}\b", text))
        hits = words & _REAL_TICKERS
        assert not hits, f"{path}: real ticker symbols {sorted(hits)}"


def test_fixture_subjects_and_scopes_are_synthetic():
    for p in fixtures.FIXTURE_DIR.glob("*.jsonl"):
        for raw in p.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            line = json.loads(raw)
            if line.get("op") == "subject":
                assert _ALLOWED_KEYS.match(line["key"]), f"{p.name}: subject key {line['key']}"
                assert "synthetic" in line.get("title", "").lower(), f"{p.name}: subject title must say synthetic"
            if "scope_ref" in line:
                assert line["scope_ref"].startswith("scope_"), f"{p.name}: scope {line['scope_ref']}"
            for v in line.values():
                if isinstance(v, str) and "://" in v:
                    assert v.startswith("synthetic://"), f"{p.name}: non-synthetic source ref {v}"
