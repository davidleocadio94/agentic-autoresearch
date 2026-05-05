"""Compatibility shim: the eval imports `transcribe` from this module.

The real pipeline lives in `transcribe.py` (Gemini call + page stitch)
and `prompts.py` (the staged-protocol prompt). This module just
re-exports so the immutable eval contract keeps working.
"""

from __future__ import annotations

from .transcribe import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_RENDER_DPIS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_MS,
    DEFAULT_VERIFY_DPI,
    GeminiOmrError,
    PRICE_INPUT_PER_M,
    PRICE_OUTPUT_PER_M,
    reset_usage,
    transcribe,
    usage_cost,
)

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_RENDER_DPIS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_MS",
    "DEFAULT_VERIFY_DPI",
    "GeminiOmrError",
    "PRICE_INPUT_PER_M",
    "PRICE_OUTPUT_PER_M",
    "reset_usage",
    "transcribe",
    "usage_cost",
]
