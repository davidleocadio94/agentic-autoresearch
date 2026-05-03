"""Git worktree per ACT phase. Failures = `rm -rf`. Never `git reset --hard`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agentic_autoresearch.paths import worktrees_dir


def create_worktree(repo: Path, iter_num: int) -> tuple[Path, str]:
    """Create .agentic-autoresearch-worktrees/iter-NNN/ on a new branch.

    Symlinks the parent repo's .venv (if present) so the actor doesn't
    reinstall everything per iter.
    """
    branch = f"iter-{iter_num:04d}"
    wt_dir = worktrees_dir(repo) / branch
    if wt_dir.exists():
        # leftover from a crashed prior iter; nuke it
        shutil.rmtree(wt_dir, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=repo, check=False)
    # Also delete any lingering branch from prior crashed iter (otherwise
    # `git worktree add -b` fails with "already exists").
    subprocess.run(
        ["git", "branch", "-D", branch], cwd=repo, check=False, capture_output=True
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt_dir), "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    # Share .venv with the parent so eval/imports work without uv sync.
    # Only symlink if the parent .venv is a REAL directory (not a symlink),
    # otherwise we can create circular references.
    parent_venv = repo / ".venv"
    wt_venv = wt_dir / ".venv"
    if (
        parent_venv.is_dir()
        and not parent_venv.is_symlink()
        and not wt_venv.exists()
    ):
        try:
            wt_venv.symlink_to(parent_venv.resolve(), target_is_directory=True)
        except OSError:
            pass
    # Same for .env (so worktree code finds GEMINI_API_KEY)
    parent_env = repo / ".env"
    wt_env = wt_dir / ".env"
    if (
        parent_env.is_file()
        and not parent_env.is_symlink()
        and not wt_env.exists()
    ):
        try:
            wt_env.symlink_to(parent_env.resolve())
        except OSError:
            pass
    return wt_dir, branch


def discard_worktree(repo: Path, wt_dir: Path, branch: str) -> None:
    if wt_dir.exists():
        shutil.rmtree(wt_dir, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=repo, check=False)
    # delete the branch (force; it's a failed experiment)
    subprocess.run(["git", "branch", "-D", branch], cwd=repo, check=False, capture_output=True)


def merge_worktree(repo: Path, wt_dir: Path, branch: str) -> str:
    """Fast-forward main → keep the branch's commits. Returns merged HEAD."""
    subprocess.run(
        ["git", "merge", "--ff-only", branch],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    if wt_dir.exists():
        shutil.rmtree(wt_dir, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=repo, check=False)
    return head


def diff_against_main(repo: Path, branch: str, max_chars: int = 4000) -> str:
    res = subprocess.run(
        ["git", "diff", f"main...{branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    out = res.stdout
    return out[:max_chars] + ("\n... (truncated)" if len(out) > max_chars else "")


def ensure_initial_commit(repo: Path) -> None:
    """Make sure the repo has at least one commit on `main`."""
    res = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        # not a git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    # Empty repo? make a starter commit
    has_commits = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=False
    )
    if has_commits.returncode != 0:
        subprocess.run(["git", "add", "-A"], cwd=repo, check=False)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
