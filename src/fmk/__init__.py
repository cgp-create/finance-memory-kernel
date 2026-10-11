"""finance-memory-kernel (fmk).

A provenance-preserving, revision-aware research-and-decision record for
investment agents, on SQLite and the standard library.

It stores evidence, claims, questions, underwriting revisions, context
packets, receipts, mandates, position snapshots and decision records, and
enforces the invariants listed in SPEC.md section 5, most of them in the
SQLite schema itself. It does not size trades, classify intent, block
orders, rank by embeddings, or evaluate performance.
"""
from .models import InvariantViolation, KernelError, MandateBody  # noqa: F401
from .store import Store  # noqa: F401

__version__ = "0.2.0"
__all__ = ["Store", "InvariantViolation", "KernelError", "MandateBody", "__version__"]
