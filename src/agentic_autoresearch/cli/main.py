"""`autoresearch` CLI."""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import click

from agentic_autoresearch.cli.scaffold import scaffold
from agentic_autoresearch.memory import store
from agentic_autoresearch.memory.schema import init_db
from agentic_autoresearch.orchestrator.loop import LoopOptions, run_loop
from agentic_autoresearch.paths import db_path, home


@click.group()
def cli():
    """plug in a spec.md, get a working repo."""


@cli.command()
@click.argument("name")
@click.option("--parent", type=click.Path(file_okay=False, path_type=Path), default=Path.cwd())
def new(name: str, parent: Path):
    """Scaffold a new problem repo: <parent>/<name>/spec.md + eval/."""
    repo = scaffold(name, parent)
    click.echo(f"scaffolded {repo}")
    click.echo("next steps:")
    click.echo(f"  1. edit {repo}/spec.md")
    click.echo(f"  2. implement {repo}/eval/score.py")
    click.echo(f"  3. run: autoresearch run {repo}")


@cli.command()
@click.argument("problem", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--max-hours", type=float, default=None)
@click.option("--max-iters", type=int, default=None)
@click.option("--target-score", type=float, default=None)
@click.option("--dashboard/--no-dashboard", default=True)
def run(problem: Path, max_hours, max_iters, target_score, dashboard: bool):
    """Run the autoresearch loop on PROBLEM until exit condition."""
    spec_path = problem / "spec.md"
    if not spec_path.exists():
        raise click.UsageError(f"no spec.md in {problem}")
    init_db()
    if dashboard:
        _start_dashboard_thread()
    opts = LoopOptions(
        max_hours=max_hours, max_iters=max_iters, target_score=target_score
    )
    rid = run_loop(spec_path, opts)
    click.echo(f"run finished: {rid}")


@cli.command()
def resume():
    """Reconcile DB state after a crash. Marks orphan iters as abandoned,
    closes dead processes, ends runs whose orchestrator is gone."""
    init_db()
    cleaned_iters = 0
    cleaned_procs = 0
    cleaned_runs = 0

    # 1. orphan iters → abandoned
    for r in store.open_phase_iters():
        store.update_iteration(r["id"], phase="abandoned", kept=0)
        click.echo(f"  iter {r['iter_num']}: abandoned (was {r['phase']})")
        cleaned_iters += 1

    # 2. dead processes → end_process
    for r in store.alive_processes():
        if not _pid_alive(r["pid"]):
            store.end_process(r["pid"])
            click.echo(f"  process pid={r['pid']} ({r['role']}): cleared (dead)")
            cleaned_procs += 1

    # 3. running runs without a live orchestrator → end as 'crashed'
    from agentic_autoresearch.memory.schema import connect

    c = connect()
    try:
        runs = list(
            c.execute("SELECT id FROM runs WHERE status='running'").fetchall()
        )
    finally:
        c.close()
    alive_orch = {r["pid"] for r in store.alive_processes("orchestrator")}
    for r in runs:
        c = connect()
        try:
            has_alive = c.execute(
                "SELECT 1 FROM processes WHERE run_id=? AND role='orchestrator' "
                "AND ended_at IS NULL",
                (r["id"],),
            ).fetchone()
        finally:
            c.close()
        if not has_alive or all(not _pid_alive(p) for p in alive_orch):
            store.end_run(r["id"], exit_reason="crashed", final_score=None)
            click.echo(f"  run {r['id'][:8]}: ended (orchestrator gone)")
            cleaned_runs += 1

    if cleaned_iters == 0 and cleaned_procs == 0 and cleaned_runs == 0:
        click.echo("nothing to resume.")
    else:
        click.echo(
            f"reconciled: {cleaned_iters} iter(s), {cleaned_procs} proc(s), "
            f"{cleaned_runs} run(s). start fresh with `autoresearch run <problem>`."
        )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


@cli.command()
def stop():
    """SIGTERM the live orchestrator. Idempotent."""
    init_db()
    alive = store.alive_processes("orchestrator")
    if not alive:
        click.echo("no orchestrator running.")
        return
    for r in alive:
        try:
            os.kill(r["pid"], signal.SIGTERM)
            click.echo(f"  SIGTERM → pid {r['pid']}")
        except ProcessLookupError:
            store.end_process(r["pid"])
            click.echo(f"  pid {r['pid']} already gone; cleared registry")


@cli.command(name="self-test")
def self_test():
    """Run the bundled hello-world problem to prove the loop is healthy."""
    here = Path(__file__).resolve().parents[3]  # repo root
    problem = here / "problems" / "hello-world"
    if not problem.exists():
        raise click.UsageError(f"hello-world bundle missing at {problem}")
    spec_path = problem / "spec.md"
    init_db()
    rid = run_loop(spec_path, LoopOptions(max_iters=2, max_hours=0.25))
    click.echo(f"self-test run: {rid}")


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8765)
def dashboard(host: str, port: int):
    """Serve the dashboard at http://host:port."""
    init_db()
    from agentic_autoresearch.obs.app import serve

    click.echo(f"dashboard: http://{host}:{port}")
    serve(host=host, port=port)


@cli.command()
def status():
    """One-shot status dump (for terminals without a browser)."""
    init_db()
    click.echo(f"home: {home()}")
    click.echo(f"db:   {db_path()}")
    runs = store.latest_run()
    if runs:
        click.echo(f"latest run: {runs['problem_name']} ({runs['status']}) {runs['started_at']}")
    alive = store.alive_processes("orchestrator")
    if alive:
        for r in alive:
            click.echo(f"orchestrator alive pid={r['pid']} hb={r['last_heartbeat_at']}")
    else:
        click.echo("orchestrator: not running")


def _start_dashboard_thread():
    from agentic_autoresearch.obs.app import serve

    t = threading.Thread(target=lambda: serve(), daemon=True)
    t.start()
    time.sleep(0.3)
    click.echo("dashboard: http://127.0.0.1:8765")


@cli.group()
def credentials():
    """Manage ~/.agentic-autoresearch/credentials.toml (chmod 600)."""


@credentials.command(name="init")
def credentials_init():
    """Create credentials.toml from template if missing."""
    from agentic_autoresearch.credentials import credentials_path, init_file

    if credentials_path().exists():
        click.echo(f"already exists: {credentials_path()}")
        return
    p = init_file()
    click.echo(f"created (chmod 600): {p}")
    click.echo("edit it to fill in your secrets.")


@credentials.command(name="list")
def credentials_list():
    """Show which credentials are configured (values redacted)."""
    from agentic_autoresearch.credentials import CredentialError, credentials_path, load

    try:
        c = load()
    except CredentialError as e:
        raise click.ClickException(str(e))
    click.echo(f"file: {credentials_path()}")
    click.echo(f"  runpod:      {'configured' if c.runpod else 'missing'}")
    if c.runpod:
        click.echo(f"    volume:    {c.runpod.volume_id} @ {c.runpod.datacenter}")
        click.echo(f"    gpus:      {', '.join(c.runpod.gpus)}")
    click.echo(f"  anthropic:   {'configured' if c.anthropic else 'missing'}")
    click.echo(f"  huggingface: {'configured' if c.huggingface else 'missing'}")


@cli.command(name="ingest")
@click.argument("problem", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--project", default=None, help="DB project name (default: derived from spec.md frontmatter)")
@click.option("--dry-run", is_flag=True)
@click.option("--verbose", is_flag=True)
def ingest_cmd(problem: Path, project, dry_run: bool, verbose: bool):
    """Ingest external knowledge into the world model from <problem>/sources.yaml."""
    from agentic_autoresearch.agents.ingest import ingest_from_sources

    sources = problem / "sources.yaml"
    if not sources.exists():
        raise click.UsageError(f"no sources.yaml in {problem}")
    # crude: derive project name from spec.md frontmatter if not given
    if project is None:
        spec_text = (problem / "spec.md").read_text() if (problem / "spec.md").exists() else ""
        import re
        m = re.search(r"^name:\s*(\S+)", spec_text, re.MULTILINE)
        project = m.group(1) if m else problem.name
    click.echo(f"project: {project}")
    summary = ingest_from_sources(sources, project=project, dry_run=dry_run, verbose=verbose)
    click.echo("---")
    for k, v in summary.items():
        click.echo(f"  {k}: {v}")


@cli.group()
def pods():
    """RunPod pod management (start/poll/stop, leak detection)."""


@pods.command(name="list")
@click.option("--project", default="comfyui-character", show_default=True)
def pods_list(project: str):
    """Show pods registered locally + their remote status."""
    from agentic_autoresearch.agents.runpod import list_orphans
    from agentic_autoresearch.credentials import load
    from agentic_autoresearch.paths import project_dir

    c = load()
    c.require("runpod")
    pd = project_dir(project)
    orphans = list_orphans(c.runpod.api_key, pd, max_age_seconds=0)
    if not orphans:
        click.echo("no pods registered for this project.")
        return
    for o in orphans:
        click.echo(
            f"  {o['pod_id']}  gpu={o.get('gpu')}  dc={o.get('datacenter')}  "
            f"age={int(o['age_seconds'])}s  remote={o.get('remote_status')}"
        )


@pods.command(name="reap")
@click.option("--project", default="comfyui-character", show_default=True)
@click.option("--max-age-hours", type=float, default=3.0, show_default=True)
def pods_reap(project: str, max_age_hours: float):
    """Stop pods older than --max-age-hours or remotely dead."""
    from agentic_autoresearch.agents.runpod import reap_orphans
    from agentic_autoresearch.credentials import load
    from agentic_autoresearch.paths import project_dir

    c = load()
    c.require("runpod")
    stopped = reap_orphans(c.runpod.api_key, project_dir(project), max_age_hours * 3600)
    click.echo(f"stopped {len(stopped)} pod(s): {stopped}")


@pods.command(name="volumes")
def pods_volumes():
    """List your RunPod network volumes."""
    from agentic_autoresearch.agents.runpod import list_volumes
    from agentic_autoresearch.credentials import load

    c = load()
    c.require("runpod")
    for v in list_volumes(c.runpod.api_key):
        click.echo(f"  {v.get('id')}  dc={v.get('dataCenterId')}  size={v.get('size')}GB  name={v.get('name')!r}")


@credentials.command(name="verify")
def credentials_verify():
    """Make one cheap API call per credential to verify it works."""
    from agentic_autoresearch.credentials import CredentialError, load, validate_all

    try:
        c = load()
    except CredentialError as e:
        raise click.ClickException(str(e))
    results = validate_all(c)
    any_fail = False
    for name, (ok, msg) in results.items():
        marker = "OK" if ok else "FAIL"
        click.echo(f"  {name:12s} {marker} {msg}")
        if not ok:
            any_fail = True
    if any_fail:
        raise click.ClickException("one or more credentials failed validation")
