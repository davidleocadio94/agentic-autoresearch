"""World-model write API + retrieval queries.

These are the tools the reflector calls (and the planner reads from).
The shape is MCP-tool-like: each function is one typed operation that
the LLM could call as a tool. Real MCP-protocol exposure is a future
add-on; today these are direct Python calls invoked by the reflector.

Embeddings are computed server-side (here), never in the LLM's head.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agentic_autoresearch.memory.schema import EMBED_DIM, connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _embed_text(text: str) -> bytes:
    """Deterministic placeholder text embedding.

    Replaced with a real text embedder (gemini-embedding-001 or local
    sentence-transformer) when the framework wires those up. For now,
    a hashed-feature vector lets the schema + retrieval path work
    without a network dependency.
    """
    import struct

    h = hashlib.sha256(text.encode("utf-8")).digest()
    # repeat the 32-byte hash to fill EMBED_DIM*4 bytes, then unpack as floats
    needed = EMBED_DIM * 4
    repeat = (h * ((needed // len(h)) + 1))[:needed]
    vec = struct.unpack(f"<{EMBED_DIM}f", repeat)
    # normalize to unit length so cosine works
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return struct.pack(f"<{EMBED_DIM}f", *(v / norm for v in vec))


# ─── Data classes (returned from queries) ────────────────────────────


@dataclass
class Thought:
    id: str
    content: str
    status: str
    parent_belief_ids: list[str]
    iter_proposed: str | None
    iter_resolved: str | None
    refute_reason: str | None
    created_at: str
    resolved_at: str | None


@dataclass
class Belief:
    id: str
    content: str
    confidence: float
    evidence_iters: list[str]
    status: str
    source_url: str | None
    parent_thought_id: str | None
    created_at: str
    last_validated_at: str


@dataclass
class Configuration:
    id: str
    name: str
    parent_config_id: str | None
    workflow_json: str
    param_dict: dict
    best_score: float
    best_score_std: float | None
    arcface_mean: float | None
    composite_mean: float | None
    replicates: int | None
    provenance_iter_id: str | None
    sample_image_path: str | None
    created_at: str


# ─── Write API (the reflector's "tools") ────────────────────────────


class WorldModel:
    """All world-model operations, scoped to one project's DB."""

    def __init__(self, project: str):
        self.project = project
        self._conn: sqlite3.Connection | None = None

    def _get(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect(project=self.project)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── thoughts ──────────────────────────────────────────────────

    def propose_thought(
        self,
        content: str,
        parent_belief_ids: list[str] | None = None,
        iter_id: str | None = None,
    ) -> str:
        c = self._get()
        tid = _id()
        c.execute(
            "INSERT INTO thoughts (id, content, status, parent_belief_ids, iter_proposed, created_at) VALUES (?, ?, 'proposed', ?, ?, ?)",
            (tid, content, json.dumps(parent_belief_ids or []), iter_id, _now()),
        )
        c.execute("INSERT INTO thoughts_embed (id, embedding) VALUES (?, ?)", (tid, _embed_text(content)))
        return tid

    def confirm_thought(self, thought_id: str, iter_id: str | None = None) -> None:
        c = self._get()
        c.execute(
            "UPDATE thoughts SET status='confirmed', iter_resolved=?, resolved_at=? WHERE id=?",
            (iter_id, _now(), thought_id),
        )

    def refute_thought(
        self, thought_id: str, reason: str, iter_id: str | None = None
    ) -> None:
        c = self._get()
        c.execute(
            "UPDATE thoughts SET status='refuted', refute_reason=?, iter_resolved=?, resolved_at=? WHERE id=?",
            (reason, iter_id, _now(), thought_id),
        )

    def open_thoughts(self) -> list[Thought]:
        c = self._get()
        rows = c.execute(
            "SELECT * FROM thoughts WHERE status IN ('proposed', 'testing') ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_thought(r) for r in rows]

    def refuted_thoughts(self, limit: int = 50) -> list[Thought]:
        """Refuted thoughts the planner should avoid re-proposing."""
        c = self._get()
        rows = c.execute(
            "SELECT * FROM thoughts WHERE status='refuted' ORDER BY resolved_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_thought(r) for r in rows]

    # ── beliefs ───────────────────────────────────────────────────

    def add_belief(
        self,
        content: str,
        confidence: float,
        evidence_iters: list[str],
        parent_thought_id: str | None = None,
        source_url: str | None = None,
    ) -> str:
        """Add a confirmed belief OR an external_claim (when source_url is set)."""
        c = self._get()
        bid = _id()
        status = "external_claim" if source_url else "active"
        now = _now()
        c.execute(
            "INSERT INTO beliefs (id, content, confidence, evidence_iters, status, source_url, parent_thought_id, created_at, last_validated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bid,
                content,
                confidence,
                json.dumps(evidence_iters),
                status,
                source_url,
                parent_thought_id,
                now,
                now,
            ),
        )
        c.execute("INSERT INTO beliefs_embed (id, embedding) VALUES (?, ?)", (bid, _embed_text(content)))
        return bid

    def confirm_belief(self, belief_id: str, new_evidence_iter: str | None = None) -> None:
        c = self._get()
        row = c.execute("SELECT confidence, evidence_iters FROM beliefs WHERE id=?", (belief_id,)).fetchone()
        if row is None:
            raise KeyError(belief_id)
        ev = json.loads(row["evidence_iters"])
        if new_evidence_iter and new_evidence_iter not in ev:
            ev.append(new_evidence_iter)
        new_conf = min(1.0, row["confidence"] + 0.05)
        c.execute(
            "UPDATE beliefs SET confidence=?, evidence_iters=?, last_validated_at=? WHERE id=?",
            (new_conf, json.dumps(ev), _now(), belief_id),
        )

    def contradict_belief(self, belief_id: str, reason: str) -> None:
        c = self._get()
        c.execute(
            "UPDATE beliefs SET status='contradicted', contradiction_reason=?, last_validated_at=? WHERE id=?",
            (reason, _now(), belief_id),
        )

    def active_beliefs(self, limit: int = 50) -> list[Belief]:
        c = self._get()
        rows = c.execute(
            "SELECT * FROM beliefs WHERE status='active' ORDER BY confidence DESC, last_validated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_belief(r) for r in rows]

    def external_claims(self, limit: int = 50) -> list[Belief]:
        c = self._get()
        rows = c.execute(
            "SELECT * FROM beliefs WHERE status='external_claim' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_belief(r) for r in rows]

    def find_beliefs(self, query: str, k: int = 5) -> list[Belief]:
        """Semantic search via embedding."""
        c = self._get()
        vec = _embed_text(query)
        rows = c.execute(
            "SELECT b.* FROM beliefs_embed e JOIN beliefs b ON b.id = e.id "
            "WHERE e.embedding MATCH ? AND k=? "
            "AND b.status IN ('active', 'external_claim') "
            "ORDER BY e.distance",
            (vec, k),
        ).fetchall()
        return [_row_to_belief(r) for r in rows]

    # ── understandings ────────────────────────────────────────────

    def add_understanding(
        self, content: str, supporting_belief_ids: list[str], confidence: float
    ) -> str:
        c = self._get()
        uid = _id()
        now = _now()
        c.execute(
            "INSERT INTO understandings (id, content, supporting_belief_ids, confidence, created_at, last_validated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, content, json.dumps(supporting_belief_ids), confidence, now, now),
        )
        c.execute(
            "INSERT INTO understandings_embed (id, embedding) VALUES (?, ?)",
            (uid, _embed_text(content)),
        )
        return uid

    # ── intuitions ────────────────────────────────────────────────

    def add_intuition(
        self, content: str, weight: float, supporting_understanding_ids: list[str]
    ) -> str:
        c = self._get()
        iid = _id()
        c.execute(
            "INSERT INTO intuitions (id, content, weight, supporting_understanding_ids, created_at) VALUES (?, ?, ?, ?, ?)",
            (iid, content, weight, json.dumps(supporting_understanding_ids), _now()),
        )
        c.execute(
            "INSERT INTO intuitions_embed (id, embedding) VALUES (?, ?)", (iid, _embed_text(content))
        )
        return iid

    def active_intuitions(self) -> list[dict]:
        c = self._get()
        rows = c.execute(
            "SELECT * FROM intuitions WHERE weight > 0.3 ORDER BY weight DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── configurations ────────────────────────────────────────────

    def save_configuration(
        self,
        name: str,
        workflow_json: str,
        param_dict: dict,
        best_score: float,
        best_score_std: float | None,
        composite_mean: float | None,
        arcface_mean: float | None,
        replicates: int,
        provenance_iter_id: str | None,
        sample_image_path: str | None,
        sample_image_embedding: bytes | None,
        parent_config_id: str | None = None,
    ) -> str:
        c = self._get()
        cid = _id()
        c.execute(
            "INSERT INTO configurations (id, name, parent_config_id, workflow_json, param_dict, best_score, best_score_std, arcface_mean, composite_mean, replicates, provenance_iter_id, sample_image_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                name,
                parent_config_id,
                workflow_json,
                json.dumps(param_dict),
                best_score,
                best_score_std,
                arcface_mean,
                composite_mean,
                replicates,
                provenance_iter_id,
                sample_image_path,
                _now(),
            ),
        )
        if sample_image_embedding is not None:
            c.execute(
                "INSERT INTO configurations_sample_embed (id, embedding) VALUES (?, ?)",
                (cid, sample_image_embedding),
            )
        return cid

    def best_configuration(self) -> Configuration | None:
        c = self._get()
        r = c.execute("SELECT * FROM configurations ORDER BY best_score DESC LIMIT 1").fetchone()
        return _row_to_config(r) if r else None

    def configurations_near(
        self, sample_image_embedding: bytes, k: int = 3
    ) -> list[Configuration]:
        c = self._get()
        rows = c.execute(
            "SELECT c.* FROM configurations_sample_embed e JOIN configurations c ON c.id = e.id "
            "WHERE e.embedding MATCH ? AND k=? "
            "ORDER BY e.distance",
            (sample_image_embedding, k),
        ).fetchall()
        return [_row_to_config(r) for r in rows]

    # ── snapshot (for planner prompt) ─────────────────────────────

    def snapshot(self, top_k_beliefs: int = 8) -> dict[str, Any]:
        """Compact dict the planner can read to build its context."""
        c = self._get()
        beliefs = self.active_beliefs(limit=top_k_beliefs)
        externals = self.external_claims(limit=5)
        intuitions = self.active_intuitions()
        best = self.best_configuration()
        refuted = self.refuted_thoughts(limit=20)
        return {
            "beliefs": [b.__dict__ for b in beliefs],
            "external_claims": [b.__dict__ for b in externals],
            "intuitions": intuitions,
            "best_configuration": best.__dict__ if best else None,
            "refuted_thoughts": [t.__dict__ for t in refuted],
        }


# ─── row → dataclass helpers ────────────────────────────────────────


def _row_to_thought(r: sqlite3.Row) -> Thought:
    return Thought(
        id=r["id"],
        content=r["content"],
        status=r["status"],
        parent_belief_ids=json.loads(r["parent_belief_ids"] or "[]"),
        iter_proposed=r["iter_proposed"],
        iter_resolved=r["iter_resolved"],
        refute_reason=r["refute_reason"],
        created_at=r["created_at"],
        resolved_at=r["resolved_at"],
    )


def _row_to_belief(r: sqlite3.Row) -> Belief:
    return Belief(
        id=r["id"],
        content=r["content"],
        confidence=r["confidence"],
        evidence_iters=json.loads(r["evidence_iters"] or "[]"),
        status=r["status"],
        source_url=r["source_url"],
        parent_thought_id=r["parent_thought_id"],
        created_at=r["created_at"],
        last_validated_at=r["last_validated_at"],
    )


def _row_to_config(r: sqlite3.Row) -> Configuration:
    return Configuration(
        id=r["id"],
        name=r["name"],
        parent_config_id=r["parent_config_id"],
        workflow_json=r["workflow_json"],
        param_dict=json.loads(r["param_dict"]),
        best_score=r["best_score"],
        best_score_std=r["best_score_std"],
        arcface_mean=r["arcface_mean"],
        composite_mean=r["composite_mean"],
        replicates=r["replicates"],
        provenance_iter_id=r["provenance_iter_id"],
        sample_image_path=r["sample_image_path"],
        created_at=r["created_at"],
    )
