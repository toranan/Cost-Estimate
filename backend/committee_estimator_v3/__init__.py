"""Evidence-gated estimator for statutory committee meeting costs.

This package is intentionally isolated from ``backend.estimator_v2``.  It
reuses the local TAG index and PDF parser, but owns its extraction, evidence
gates, review contract, and calculation result.
"""

from .pipeline import estimate_committee_from_pdf

__all__ = ["estimate_committee_from_pdf"]
