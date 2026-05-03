"""Gemini embeddings (gemini-embedding-001, 768-dim).

Used by REFLECT to populate hypothesis_embed + lesson_embed in the
iterations table. PLANNER then queries by vector similarity.
"""

from __future__ import annotations

import os
from functools import lru_cache

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768  # output_dimensionality we request


def _load_env() -> None:
    """Pick up GEMINI_API_KEY from the problem repo's .env if needed."""
    if os.environ.get("GEMINI_API_KEY"):
        return
    try:
        from dotenv import load_dotenv

        # Walk up from cwd looking for .env
        from pathlib import Path

        for parent in [Path.cwd(), *Path.cwd().parents]:
            env = parent / ".env"
            if env.exists():
                load_dotenv(env)
                if os.environ.get("GEMINI_API_KEY"):
                    return
    except Exception:
        pass


@lru_cache(maxsize=1)
def _client():
    _load_env()
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set (.env or environment)")
    return genai.Client(api_key=api_key)


def embed(text: str) -> list[float]:
    """One-shot embed. Returns 768 floats. Errors → zero vector (caller decides)."""
    if not text or not text.strip():
        return [0.0] * EMBED_DIM
    try:
        from google.genai import types

        client = _client()
        res = client.models.embed_content(
            model=EMBED_MODEL,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
        )
        # `embeddings` is a list of ContentEmbedding; for a single content input,
        # take the first one.
        embs = getattr(res, "embeddings", None) or []
        if not embs:
            return [0.0] * EMBED_DIM
        values = getattr(embs[0], "values", None) or []
        return list(values) if values else [0.0] * EMBED_DIM
    except Exception as e:
        print(f"[embeddings] failed: {e!r}")
        return [0.0] * EMBED_DIM


def is_zero(vec: list[float]) -> bool:
    return not any(v != 0.0 for v in vec)
