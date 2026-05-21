"""Ingest layer.

Reads `sources.yaml` from a problem directory and pulls items from
reddit / arxiv / curated web domains. Each item is written to the
project's world model as a belief with status='external_claim',
confidence=0.3 (low — unverified), source_url set.

The planner reads these alongside internal beliefs but knows they
haven't been confirmed by the loop's own experiments.

Goals over time:
  - cite_count : N internal beliefs that cited this external_claim
  - confirmation: external_claim becomes a real `active` belief once
                  the loop runs a hypothesis it inspired and that
                  hypothesis is confirmed.

This file deliberately does the minimum: fetch + extract + insert.
No long-running daemon, no fancy scheduling. Run once at project
init, run again whenever you want fresh signal (e.g. weekly cron).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentic_autoresearch.memory.schema import init_db
from agentic_autoresearch.memory.world_model import WorldModel


USER_AGENT = "aar-ingest/0.1 (research; +https://github.com/davidleocadio94/agentic-autoresearch)"


@dataclass
class IngestItem:
    title: str
    body: str
    url: str
    source: str             # "reddit" | "arxiv" | "web"
    posted_at: str | None   # iso8601 if known


# ─── fetchers ──────────────────────────────────────────────────────


def _http(url: str, headers: dict | None = None, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_reddit(sub: str, query: str | None = None, limit: int = 25,
                 since_days: int = 90) -> list[IngestItem]:
    """Reddit JSON API. No auth needed for read."""
    # Use /new.json then filter by created_utc, OR /search.json if a query is set.
    if query:
        q = urllib.parse.quote(query)
        url = f"https://www.reddit.com/r/{sub}/search.json?q={q}&restrict_sr=1&sort=new&limit={limit}"
    else:
        url = f"https://www.reddit.com/r/{sub}/new.json?limit={limit}"
    raw = _http(url)
    data = json.loads(raw)
    cutoff = time.time() - since_days * 86400
    out: list[IngestItem] = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("created_utc", 0) < cutoff:
            continue
        body = (d.get("selftext") or "")
        out.append(IngestItem(
            title=d.get("title", ""),
            body=body[:4000],   # cap
            url="https://www.reddit.com" + d.get("permalink", ""),
            source="reddit",
            posted_at=dt.datetime.utcfromtimestamp(d.get("created_utc", 0)).isoformat() + "Z",
        ))
    return out


def fetch_arxiv(query: str, max_results: int = 20, since_days: int = 180) -> list[IngestItem]:
    """arxiv.org Atom feed. No auth needed."""
    q = urllib.parse.quote(query)
    url = f"http://export.arxiv.org/api/query?search_query=all:{q}&start=0&max_results={max_results}&sortBy=submittedDate&sortOrder=descending"
    raw = _http(url).decode(errors="replace")
    # Minimal Atom parsing: regex out <entry>...</entry> blocks. Fragile but
    # avoids a feed-parser dep.
    cutoff = time.time() - since_days * 86400
    out: list[IngestItem] = []
    for m in re.finditer(r"<entry>(.*?)</entry>", raw, flags=re.DOTALL):
        e = m.group(1)
        t = _xml_first(e, "title") or ""
        s = _xml_first(e, "summary") or ""
        link = _xml_first(e, "id") or ""
        pub = _xml_first(e, "published")
        if pub:
            try:
                pts = dt.datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
                if pts < cutoff:
                    continue
            except ValueError:
                pass
        out.append(IngestItem(
            title=t.strip().replace("\n", " "),
            body=s.strip()[:4000],
            url=link.strip(),
            source="arxiv",
            posted_at=pub,
        ))
    return out


def _xml_first(blob: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", blob, flags=re.DOTALL)
    return m.group(1) if m else None


def fetch_web(url: str, max_chars: int = 6000) -> list[IngestItem]:
    """One-page fetch. Returns one item with stripped text."""
    try:
        raw = _http(url).decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return []
    # crude readability: strip tags, collapse whitespace
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    title = ""
    m = re.search(r"<title>(.*?)</title>", raw, flags=re.DOTALL | re.IGNORECASE)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    return [IngestItem(
        title=title or url,
        body=text[:max_chars],
        url=url,
        source="web",
        posted_at=None,
    )]


# ─── extractor (text → structured claim) ──────────────────────────


def extract_claim(item: IngestItem) -> str:
    """Heuristic single-sentence claim from a fetched item.

    A future version would call claude -p with a prompt like:
      "Read this and return ONE sentence stating any technique,
       parameter recipe, or claim about character consistency / image
       generation. If nothing relevant, return EMPTY."
    For now, just use the title + first 200 chars of body.
    """
    head = item.title.strip()
    snippet = item.body.strip()[:300]
    if snippet and not snippet.startswith(head):
        return f"{head} — {snippet}"
    return head


# ─── ingest entrypoint ────────────────────────────────────────────


def ingest_from_sources(sources_yaml: Path, project: str, dry_run: bool = False,
                         verbose: bool = False) -> dict[str, Any]:
    """Read sources.yaml, fetch, write external_claim beliefs.

    Returns a dict of {source: N items inserted}.
    """
    if not sources_yaml.exists():
        raise FileNotFoundError(sources_yaml)
    with sources_yaml.open() as f:
        cfg = yaml.safe_load(f) or {}
    items: list[IngestItem] = []
    summary: dict[str, int] = {}

    if "reddit" in cfg:
        for sub in cfg["reddit"].get("subs", []):
            try:
                fetched = fetch_reddit(
                    sub,
                    query=cfg["reddit"].get("query"),
                    limit=cfg["reddit"].get("limit", 25),
                    since_days=cfg["reddit"].get("since_days", 90),
                )
                items.extend(fetched)
                summary[f"reddit:{sub}"] = len(fetched)
                if verbose:
                    print(f"  reddit/{sub}: {len(fetched)} posts")
            except Exception as e:
                summary[f"reddit:{sub}"] = 0
                if verbose:
                    print(f"  reddit/{sub}: FAIL {type(e).__name__}: {e}")
    if "arxiv" in cfg:
        for q in cfg["arxiv"].get("queries", []):
            try:
                fetched = fetch_arxiv(
                    q,
                    max_results=cfg["arxiv"].get("max_results", 20),
                    since_days=cfg["arxiv"].get("since_days", 180),
                )
                items.extend(fetched)
                summary[f"arxiv:{q}"] = len(fetched)
                if verbose:
                    print(f"  arxiv/{q!r}: {len(fetched)} papers")
            except Exception as e:
                summary[f"arxiv:{q}"] = 0
                if verbose:
                    print(f"  arxiv/{q!r}: FAIL {e}")
    if "web" in cfg:
        for u in cfg["web"].get("urls", []):
            try:
                fetched = fetch_web(u, max_chars=cfg["web"].get("max_chars", 6000))
                items.extend(fetched)
                summary[f"web:{u}"] = len(fetched)
                if verbose:
                    print(f"  web/{u}: {len(fetched)} item(s)")
            except Exception as e:
                summary[f"web:{u}"] = 0
                if verbose:
                    print(f"  web/{u}: FAIL {e}")

    if dry_run:
        return {"summary": summary, "items_total": len(items)}

    init_db(project=project)
    inserted = 0
    with WorldModel(project) as wm:
        # Skip duplicates by URL.
        existing_urls = {b.source_url for b in wm.external_claims(limit=10_000) if b.source_url}
        for it in items:
            if it.url in existing_urls:
                continue
            claim = extract_claim(it)
            if not claim or len(claim) < 12:
                continue
            wm.add_belief(
                content=claim,
                confidence=0.3,
                evidence_iters=[],
                source_url=it.url,
            )
            inserted += 1
    summary["_inserted"] = inserted
    summary["_items_fetched"] = len(items)
    return summary
