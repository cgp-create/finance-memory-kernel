"""Authority references: "<kind>:<name>", checked by role, not authenticated.

Every receipt names who did the thing it records. An authority reference
is a string of the form "<kind>:<name>" where kind is one of ACTOR_KINDS
(direct_user, reviewer, model, system, venue). The kernel uses the kind to
enforce separation of duties: a model may propose but never review,
select, approve or activate; only a direct_user may activate a mandate.
Separately, the person who reviews a draft or approves a decision must not
be the person who proposed it, comparing names case-insensitively and
ignoring the kind, so "direct_user:owner" and "reviewer:Owner" are one
person.

What the kernel cannot do is prove that "direct_user:owner" was typed by
the owner. That is authentication, and it belongs to the application that
embeds the kernel. Pass `authorize=` to Store to plug it in: it is called
before every receipt the Python API writes, and can refuse. Raw SQL on the
connection does not go through it.
"""
from __future__ import annotations

from typing import Iterable

from .models import ACTOR_KINDS, HUMAN_ACTOR_KINDS, InvariantViolation


def parse(authority_ref: str) -> tuple[str, str]:
    """Split and validate "<kind>:<name>". Raises on anything else."""
    if not isinstance(authority_ref, str) or ":" not in authority_ref:
        raise InvariantViolation(
            f"authority {authority_ref!r} must look like '<kind>:<name>' with kind in {ACTOR_KINDS}")
    kind, _, name = authority_ref.partition(":")
    if kind not in ACTOR_KINDS or not name.strip():
        raise InvariantViolation(
            f"authority {authority_ref!r} must look like '<kind>:<name>' with kind in {ACTOR_KINDS}")
    return kind, name


def kind_of(authority_ref: str) -> str:
    return parse(authority_ref)[0]


def is_human(authority_ref: str) -> bool:
    return kind_of(authority_ref) in HUMAN_ACTOR_KINDS


def require(authority_ref: str, allowed: Iterable[str], action: str) -> str:
    """Refuse unless the authority's kind is in `allowed`."""
    kind = kind_of(authority_ref)
    allowed = tuple(allowed)
    if kind not in allowed:
        raise InvariantViolation(f"{action} requires a {' or '.join(allowed)} authority, not {authority_ref!r}")
    return authority_ref


def require_human(authority_ref: str, action: str) -> str:
    return require(authority_ref, sorted(HUMAN_ACTOR_KINDS), action)


def person(authority_ref: str) -> str:
    """The name part, case-folded and trimmed. 'direct_user:Owner' and
    'reviewer:owner ' are the same person for separation of duties."""
    return parse(authority_ref)[1].strip().casefold()


def require_distinct(actor: str, other: str, action: str) -> None:
    """Separation of duties: the same name under a different kind is the same person."""
    if person(actor) == person(other):
        raise InvariantViolation(f"{action}: {actor!r} is the same person as the proposer {other!r}")
