"""Evidence-first experimental estimator.

This package is intentionally separate from ``backend.estimator`` and the
production API.  It keeps the same broad detect -> retrieve -> calculate
workflow while making evidence, additionality, recurrence, and reproducibility
explicit.
"""

from .pipeline import estimate_from_pdf

__all__ = ["estimate_from_pdf"]
