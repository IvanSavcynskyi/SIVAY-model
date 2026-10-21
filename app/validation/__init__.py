"""Offline, reproducible diagnostics; these do not establish agronomic accuracy."""

from .offline import ValidationCase, ablate, audit_cutoff, backtest, blind_package

__all__ = ["ValidationCase", "ablate", "audit_cutoff", "backtest", "blind_package"]
