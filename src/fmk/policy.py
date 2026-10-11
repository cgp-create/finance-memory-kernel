"""Freshness and authority policies, as plain versioned data.

How long each claim kind or aspect stays current (invariant 10), how
position snapshots age per source class, and how far a mark may lag its
position. Ages run from when the underlying evidence was observed (or a
claim's valid_from), not from when someone typed the claim, so a week-old
price entered today is a week old.

Stale means served with a warning. Nothing here hides or deletes data, and
nothing here decides which claim is true.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from .models import AUTHORITY_RANK, Claim, parse_ts

POLICY_VERSION = "v1"

# Default time-to-live per (aspect prefix or claim kind). Aspects are looked
# up first so that "price" can be tighter than a generic "fact". None means
# no automatic expiry (a user-stated investing principle, for example).
DEFAULT_TTL_SECONDS: dict[str, Optional[int]] = {
    # by aspect prefix
    "price": 15 * 60,
    "quote": 15 * 60,
    "holdings": 24 * 3600,
    "estimate": 30 * 86400,
    "guidance": 120 * 86400,
    "calendar": 24 * 3600,
    # by claim kind
    "fact": 365 * 86400,
    "forecast": 90 * 86400,
    "hypothesis": 180 * 86400,
    "inference": 90 * 86400,
    "correction": None,
    "user_decision": None,
}


@dataclass(frozen=True)
class FreshnessPolicy:
    version: str = POLICY_VERSION
    ttl_seconds: dict = field(default_factory=lambda: dict(DEFAULT_TTL_SECONDS))

    def ttl_for(self, claim: Claim) -> Optional[int]:
        aspect_head = claim.aspect.split(".")[0].split("_")[0]
        if aspect_head in self.ttl_seconds:
            return self.ttl_seconds[aspect_head]
        return self.ttl_seconds.get(claim.kind)

    def default_expiry(self, claim_kind: str, aspect: str, created_at: str) -> Optional[str]:
        """Expiry to stamp on a claim at creation when the caller gave none."""
        head = aspect.split(".")[0].split("_")[0]
        ttl = self.ttl_seconds.get(head, self.ttl_seconds.get(claim_kind))
        if ttl is None:
            return None
        return (parse_ts(created_at) + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def verdict(self, claim: Claim, as_of: str, basis: Optional[str] = None) -> tuple[str, Optional[str]]:
        """Return ('fresh'|'expired'|'degraded', warning_or_None) for a claim at as_of.

        basis is when the claim's content was true of the world: valid_from,
        else the evidence's observed_at. Callers that only have the claim may
        omit it and the claim's created_at is used.

        expired:  expires_at is at or before as_of.
        degraded: older than half its TTL, so still usable but flagged.
        """
        now = parse_ts(as_of)
        start = parse_ts(basis or claim.valid_from or claim.created_at)
        if claim.expires_at is not None and parse_ts(claim.expires_at) <= now:
            return "expired", (
                f"claim:{claim.id} ({claim.aspect}) expired at {claim.expires_at}; "
                f"not current as of {as_of}"
            )
        ttl = self.ttl_for(claim)
        if ttl:
            age = (now - start).total_seconds()
            if age > ttl / 2:
                return "degraded", (
                    f"claim:{claim.id} ({claim.aspect}) is {int(age)}s old against a {ttl}s clock; "
                    f"verify before relying on it"
                )
        return "fresh", None


def authority_rank(tier: str) -> int:
    return AUTHORITY_RANK.get(tier, 0)


def snapshot_age_warning(source_class: str, as_of: str, now: str,
                         freshness_seconds: dict) -> Optional[str]:
    """Position snapshot freshness per the active Mandate's policy."""
    limit = freshness_seconds.get(source_class, 0)
    if not limit:
        return None
    age = (parse_ts(now) - parse_ts(as_of)).total_seconds()
    if age > limit:
        return f"snapshot age {int(age)}s exceeds policy {limit}s for {source_class}"
    return None


# A mark may lag its position by this much before it is called stale. A Friday
# equity close next to a Sunday crypto position is the case this exists for.
DEFAULT_MARK_LAG_SECONDS = 3600


def mark_staleness_warning(mark_as_of: Optional[str], position_as_of: str,
                           mark_source_class: Optional[str], freshness_seconds: dict) -> Optional[str]:
    """Position fresh, mark stale: warn, do not withhold (SPEC test 17)."""
    if not mark_as_of:
        return None
    lag = (parse_ts(position_as_of) - parse_ts(mark_as_of)).total_seconds()
    limit = freshness_seconds.get("mark", DEFAULT_MARK_LAG_SECONDS)
    if mark_source_class == "stale_cache" or lag > limit:
        return (f"mark is older than position by {int(lag)}s (mark source {mark_source_class or 'unknown'}); "
                "weights blend live and stale marks")
    return None
