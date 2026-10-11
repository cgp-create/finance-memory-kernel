"""finance-memory-kernel (fmk).

A provenance-preserving, revision-aware research-and-decision record for
investment agents. Standard library and SQLite only.

What it does: stores evidence, claims, questions, underwriting revisions,
context packets, receipts, mandates, position snapshots and decision
records under seventeen enforced invariants.

What it does not do: it does not size, classify intent, set an action
bar, block orders, rank by embeddings, or evaluate performance.
"""
from .models import InvariantViolation, KernelError, MandateBody  # noqa: F401
from .store import Store  # noqa: F401

__version__ = "0.1.0"
__all__ = ["Store", "InvariantViolation", "KernelError", "MandateBody", "__version__"]
