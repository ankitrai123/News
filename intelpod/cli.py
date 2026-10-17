"""Command-line interface.

  intelpod doctor                 inspect hardware, tools, Ollama and source reachability
  intelpod init                   create config/config.yaml and the database
  intelpod feeds check            fetch every feed once and record its health
  intelpod run                    full Phase 1 pipeline (ingest -> cluster -> rank -> report)
  intelpod run --stop-after ingest
  intelpod run --resume [--run-id ID]  continue an interrupted/paused run
  intelpod events [--run ID] [--all]
  intelpod runs
  intelpod serve                  local dashboard on http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys

from . import __version__
from .config import PROJECT_ROOT, Settings, load_settings
from .db import Database
from .logging_setup import setup_logging
from .repository import Repository


def _settings(args) -> Settings:
    overrides = {}
    if getattr(args, "no_gdelt", False):
        overrides["ingestion.providers.gdelt.enabled"] = False
    if getattr(args, "no_rss", False):
        overrides["ingestion.providers.rss.enabled"] = False
    if getattr(args, "lookback_hours", None):
        overrides["ingestion.lookback_hours"] = args.lookback_hours
    s = load_settings(args.config, overrides)
    setup_logging(s.app.log_level, s.data_path / "logs", s.app.log_json)
    return s


def cmd_init(args) -> int:
    target = PROJECT_ROOT / "config" / "config.yaml"
    if not target.exists():
        shutil.copy(PROJECT_ROOT / "config" / "config.example.yaml", target)
        print(f"created {target.relative_to(PROJECT_ROOT)} - edit user_agent to include your contact")
    else:
        print(f"{target.relative_to(PROJECT_ROOT)} already exists")
    s = _settings(args)
    db = Database(s.db_file)
    print(f"database ready: {s.db_file} (schema v{db.schema_version()})")
    return 0


def _mem_gb() -> float | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            import subprocess
            return round(int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 1024 ** 3, 1)
        except Exception:  # noqa: BLE001
            return None
    return None


def cmd_doctor(args) -> int:
    import httpx

    s = _settings(args)
    report: dict = {
        "intelpod": __version__, "python": platform.python_version(), "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(), "cpus": os.cpu_count(), "ram_gb": _mem_gb(),
        "disk_free_gb": round(shutil.disk_usage(PROJECT_ROOT).free / 1024 ** 3, 1),
        "ffmpeg": shutil.which("ffmpeg"), "nvidia_smi": shutil.which("nvidia-smi"),
    }
    try:
        r = httpx.get(s.clustering.embeddings.ollama_url.rstrip("/") + "/api/tags", timeout=3, trust_env=False)
        report["ollama"] = {"reachable": True, "models": [m.get("name") for m in r.json().get("models", [])]}
    except Exception as e:  # noqa: BLE001
        report["ollama"] = {"reachable": False, "error": type(e).__name__}
    probes = {"gdelt": "https://api.gdeltproject.org/api/v2/doc/doc?query=economy%20sourcelang:english&mode=ArtList&format=json&maxrecords=1&timespan=1d"}
    from .providers import load_feeds
    feeds = load_feeds(s.resolve(s.ingestion.feeds_file))
    if feeds:
        probes[f"rss:{feeds[0].name}"] = feeds[0].url
    report["network"] = {}
    for name, url in probes.items():
        try:
            r = httpx.get(url, timeout=15, headers={"User-Agent": s.app.user_agent}, follow_redirects=True)
            report["network"][name] = f"HTTP {r.status_code}, {len(r.content)} bytes"
        except Exception as e:  # noqa: BLE001
            report["network"][name] = f"FAILED: {type(e).__name__}: {str(e)[:120]}"
    ram = report["ram_gb"] or 0
    report["recommendation"] = (
        "lightweight mode: lexical clustering only; for later phases use a 1-3B model (e.g. qwen2.5:3b / llama3.2:3b)"
        if ram < 12 else
        "standard mode: 7-8B instruct model for analysis (e.g. qwen2.5:7b / llama3.1:8b); nomic-embed-text for clustering"
        if ram < 32 else "large mode: 14B+ models are feasible for synthesis and script writing")
    print(json.dumps(report, indent=2))
    return 0


def cmd_feeds_check(args) -> int:
    from .http import HttpClient
    from .providers import RssProvider, load_feeds

    s = _settings(args)
    repo = Repository(Database(s.db_file))
    feeds = load_feeds(s.resolve(s.ingestion.feeds_file), include_disabled=args.include_disabled)
    ok_count = 0
    with HttpClient(s.http, s.app.user_agent) as http:
        provider = RssProvider(http, feeds, use_conditional_get=False)
        print(f"{'STATUS':8} {'ITEMS':>5}  {'NEWEST ITEM':17} FEED")
        for feed in feeds:
            items, rec = provider.fetch_feed(feed)
            repo.log_fetches(None, [rec])
            ok_count += rec.ok and rec.item_count > 0
            status = "OK" if rec.ok and rec.item_count else ("EMPTY" if rec.ok else "FAIL")
            newest = rec.newest_item_at.strftime("%Y-%m-%d %H:%M") if rec.newest_item_at else "-"
            extra = "" if rec.ok else f"  <- {rec.error}"
            print(f"{status:8} {rec.item_count:>5}  {newest:17} {feed.name}{'' if feed.enabled else ' (disabled)'}{extra}")
    print(f"\n{ok_count}/{len(feeds)} feeds returned items. Set `verified: true` (or disable) in "
          f"{s.ingestion.feeds_file} accordingly.")
    return 0 if ok_count else 1


def cmd_run(args) -> int:
    from .pipeline.runner import Phase1Pipeline, RunInProgress, StageError

    s = _settings(args)
    pipe = Phase1Pipeline(s)
    try:
        run_id = pipe.run(run_id=args.run_id, resume=args.resume, stop_after=args.stop_after, force=args.force)
    except (StageError, RunInProgress, ValueError) as e:
        print(f"\nRun failed: {e}", file=sys.stderr)
        print("Inspect with `intelpod runs`; fix the cause, then `intelpod run --resume`.", file=sys.stderr)
        return 2
    print(f"\nrun: {run_id}")
    if args.stop_after and args.stop_after != "report":
        stages = pipe.repo.stages(run_id)
        print(json.dumps(stages[args.stop_after].output, indent=2, default=str)[:4000])
        return 0
    _print_events(pipe.repo, run_id, show_all=False)
    out = pipe.repo.stages(run_id)["report"].output or {}
    for kind, path in (out.get("paths") or {}).items():
        print(f"{kind} report: {path}")
    return 0


def _print_events(repo: Repository, run_id: str, show_all: bool) -> None:
    ranked = repo.scores_for_run(run_id, selected_only=not show_all)
    if not ranked:
        print("no events for this run")
        return
    for r in ranked:
        tag = f"#{r.rank}" if r.selected else "  -"
        print(f"\n{tag:>3} [{r.importance:5.1f}] {r.title}")
        print(f"      topic={r.topic}{'/' + r.subtopic if r.subtopic else ''}  countries={', '.join(r.countries[:5]) or '-'}  "
              f"reports={r.article_count}  independent={r.independent_sources}")
        print(f"      {r.rationale}")


def cmd_events(args) -> int:
    s = _settings(args)
    repo = Repository(Database(s.db_file))
    run = repo.get_run(args.run) if args.run else repo.latest_run()
    if not run:
        print("no completed runs yet - try `intelpod run`")
        return 1
    print(f"run {run.run_id} ({run.status}, started {run.started_at:%Y-%m-%d %H:%M} UTC)")
    _print_events(repo, run.run_id, show_all=args.all)
    return 0


def cmd_runs(args) -> int:
    s = _settings(args)
    repo = Repository(Database(s.db_file))
    for run in repo.list_runs(args.limit):
        stages = repo.stages(run.run_id)
        st = " ".join(f"{k}:{v.status}" for k, v in stages.items())
        print(f"{run.run_id}  {run.status:9}  {run.started_at:%Y-%m-%d %H:%M}  {st}" + (f"  ERROR: {run.error}" if run.error else ""))
    usage = repo.provider_usage()
    if usage:
        print("\nrequests in the last 24h: " + ", ".join(f"{u['provider']}={u['requests']} ({u['ok']} ok)" for u in usage))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    s = _settings(args)
    from .web.app import create_app
    uvicorn.run(create_app(s), host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="intelpod", description="Geopolitical & business intelligence podcast agent")
    p.add_argument("--config", help="path to config YAML (default: config/config.yaml or the example)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create config and database").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="inspect environment, Ollama and network").set_defaults(fn=cmd_doctor)

    feeds = sub.add_parser("feeds", help="feed registry tools")
    fsub = feeds.add_subparsers(dest="feeds_cmd", required=True)
    chk = fsub.add_parser("check", help="fetch each feed once and report health")
    chk.add_argument("--include-disabled", action="store_true")
    chk.set_defaults(fn=cmd_feeds_check)

    run = sub.add_parser("run", help="run the Phase 1 pipeline")
    run.add_argument("--resume", action="store_true", help="resume the given (or latest) run")
    run.add_argument("--run-id", help="run id to resume")
    run.add_argument("--stop-after", choices=["ingest", "cluster", "rank", "report"])
    run.add_argument("--force", action="store_true", help="start even if another run looks active")
    run.add_argument("--no-gdelt", action="store_true")
    run.add_argument("--no-rss", action="store_true")
    run.add_argument("--lookback-hours", type=int)
    run.set_defaults(fn=cmd_run)

    ev = sub.add_parser("events", help="show ranked events of a run")
    ev.add_argument("--run", help="run id (default: latest completed)")
    ev.add_argument("--all", action="store_true", help="include events that were not selected")
    ev.set_defaults(fn=cmd_events)

    runs = sub.add_parser("runs", help="list runs and stage status")
    runs.add_argument("--limit", type=int, default=15)
    runs.set_defaults(fn=cmd_runs)

    srv = sub.add_parser("serve", help="start the local dashboard")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
