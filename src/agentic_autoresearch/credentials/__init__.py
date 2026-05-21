"""Credentials loader.

Reads ~/.agentic-autoresearch/credentials.toml (chmod 600 required).
Validates each credential by making one cheap API call before declaring
it usable. Never logs values; redacts on str().
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from agentic_autoresearch.paths import home


CREDENTIALS_FILE = "credentials.toml"


class CredentialError(Exception):
    """Raised when credentials are missing, malformed, or fail validation."""


@dataclass
class RunPodCreds:
    api_key: str
    volume_id: str
    datacenter: str
    gpus: list[str] = field(default_factory=lambda: ["A40", "A100", "L40S", "L40", "A6000"])

    def __repr__(self) -> str:
        return f"RunPodCreds(api_key=<redacted>, volume_id={self.volume_id!r}, datacenter={self.datacenter!r}, gpus={self.gpus!r})"


@dataclass
class AnthropicCreds:
    api_key: str

    def __repr__(self) -> str:
        return "AnthropicCreds(api_key=<redacted>)"


@dataclass
class HuggingFaceCreds:
    token: str

    def __repr__(self) -> str:
        return "HuggingFaceCreds(token=<redacted>)"


@dataclass
class Credentials:
    runpod: RunPodCreds | None = None
    anthropic: AnthropicCreds | None = None
    huggingface: HuggingFaceCreds | None = None

    def require(self, *names: str) -> None:
        missing = [n for n in names if getattr(self, n, None) is None]
        if missing:
            raise CredentialError(
                f"missing required credentials: {', '.join(missing)}. "
                f"add them with: autoresearch credentials add <name>"
            )


def credentials_path() -> Path:
    return home() / CREDENTIALS_FILE


def load() -> Credentials:
    """Load credentials.toml. Verifies file permissions are 600."""
    p = credentials_path()
    if not p.exists():
        raise CredentialError(
            f"no credentials file at {p}. "
            f"run: autoresearch credentials init"
        )
    # permission check
    st = p.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialError(
            f"{p} is world/group readable (mode={oct(mode)}). "
            f"fix: chmod 600 {p}"
        )
    with open(p, "rb") as f:
        data = tomllib.load(f)

    creds = Credentials()
    if "runpod" in data:
        r = data["runpod"]
        if not r.get("api_key"):
            raise CredentialError("[runpod] api_key is empty")
        if not r.get("volume_id"):
            raise CredentialError("[runpod] volume_id is empty")
        if not r.get("datacenter"):
            raise CredentialError("[runpod] datacenter is empty")
        creds.runpod = RunPodCreds(
            api_key=r["api_key"],
            volume_id=r["volume_id"],
            datacenter=r["datacenter"],
            gpus=r.get("gpus", RunPodCreds.__dataclass_fields__["gpus"].default_factory()),
        )
    if "anthropic" in data:
        a = data["anthropic"]
        if a.get("api_key"):
            creds.anthropic = AnthropicCreds(api_key=a["api_key"])
    if "huggingface" in data:
        h = data["huggingface"]
        if h.get("token"):
            creds.huggingface = HuggingFaceCreds(token=h["token"])
    return creds


# ─── validators ──────────────────────────────────────────────────────────


def validate_anthropic(c: AnthropicCreds, timeout: float = 10.0) -> tuple[bool, str]:
    """One cheap call: list models, limit 1. No token spend."""
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models?limit=1",
            headers={"x-api-key": c.api_key, "anthropic-version": "2023-06-01"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
            n = len(body.get("data", []))
            return True, f"ok (returned {n} model)"
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}: {e.read()[:200].decode(errors='replace')!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def validate_huggingface(c: HuggingFaceCreds, timeout: float = 10.0) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            "https://huggingface.co/api/whoami-v2",
            headers={"authorization": f"Bearer {c.token}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
            return True, f"ok (user={body.get('name')})"
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def validate_runpod(c: RunPodCreds, timeout: float = 15.0) -> tuple[bool, str]:
    """List network volumes, verify declared volume_id exists and matches datacenter."""
    try:
        req = urllib.request.Request(
            "https://rest.runpod.io/v1/networkvolumes",
            headers={"Authorization": f"Bearer {c.api_key}", "User-Agent": "aar/0.1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            vols = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}: {e.read()[:200].decode(errors='replace')!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    match = [v for v in vols if v.get("id") == c.volume_id]
    if not match:
        listing = ", ".join(f"{v.get('id')}@{v.get('dataCenterId')}({v.get('size')}GB)" for v in vols)
        return False, f"volume_id {c.volume_id!r} not in your account. yours: {listing or '<none>'}"
    v = match[0]
    actual_dc = v.get("dataCenterId")
    if actual_dc != c.datacenter:
        return False, f"declared datacenter={c.datacenter!r} but volume is in {actual_dc!r}"
    return True, f"ok (volume={v.get('id')} dc={actual_dc} size={v.get('size')}GB)"


def validate_all(creds: Credentials) -> dict[str, tuple[bool, str]]:
    """Validate every present credential. Returns name -> (ok, message)."""
    out: dict[str, tuple[bool, str]] = {}
    if creds.anthropic:
        out["anthropic"] = validate_anthropic(creds.anthropic)
    if creds.huggingface:
        out["huggingface"] = validate_huggingface(creds.huggingface)
    if creds.runpod:
        out["runpod"] = validate_runpod(creds.runpod)
    return out


# ─── env-var assembly for subprocesses ─────────────────────────────────


def env_for(creds: Credentials, services: list[str]) -> dict[str, str]:
    """Return only the env vars a subprocess needs for the services it calls.

    Never returns the parent env — caller composes it with what they want
    inherited from os.environ.
    """
    env: dict[str, str] = {}
    for svc in services:
        if svc == "anthropic":
            if not creds.anthropic:
                raise CredentialError("anthropic credentials not loaded")
            env["ANTHROPIC_API_KEY"] = creds.anthropic.api_key
        elif svc == "huggingface":
            if not creds.huggingface:
                raise CredentialError("huggingface credentials not loaded")
            env["HF_TOKEN"] = creds.huggingface.token
            env["HUGGING_FACE_HUB_TOKEN"] = creds.huggingface.token
        elif svc == "runpod":
            if not creds.runpod:
                raise CredentialError("runpod credentials not loaded")
            env["RUNPOD_API_KEY"] = creds.runpod.api_key
        else:
            raise CredentialError(f"unknown service: {svc}")
    return env


# ─── template for init ─────────────────────────────────────────────────


CREDENTIALS_TEMPLATE = """\
# ~/.agentic-autoresearch/credentials.toml — chmod 600
# Never commit. Never paste contents in chat.

[runpod]
api_key    = ""
volume_id  = ""
datacenter = ""                    # e.g. "EU-RO-1" — must match volume's region
gpus       = ["A40", "A100", "L40S", "L40", "A6000"]

[anthropic]
api_key    = ""                    # sk-ant-...

[huggingface]
token      = ""                    # hf_...
"""


def init_file() -> Path:
    """Create credentials.toml if missing, with 600 perms."""
    p = credentials_path()
    if p.exists():
        return p
    p.write_text(CREDENTIALS_TEMPLATE)
    os.chmod(p, 0o600)
    return p
