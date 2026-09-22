"""Jevmulator: a local daemon that serves the pinned TypeSafe Jev wire surface.

The judgments come from a configurable OpenAI-compatible upstream model, not from
TypeSafe weights. Schema parity is not calibration parity; see docs/compatibility.md.
"""

__version__ = "0.1.0"

CONTRACT_PIN_DATE = "2026-09-22"
CONTRACT_OPENAPI_SHA256 = "a191f8a7df6bd6fedced8120dd0fd106f88575d1d1c8360d08900a6c7c0360d5"
CONTRACT_OPENAPI_VERSION = "0.2.0"

__all__ = [
    "__version__",
    "CONTRACT_PIN_DATE",
    "CONTRACT_OPENAPI_SHA256",
    "CONTRACT_OPENAPI_VERSION",
]
