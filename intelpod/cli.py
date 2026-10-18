"""Command-line interface.

  intelpod doctor                 inspect hardware, tools, Ollama and source reachability
  intelpod init                   create config/config.yaml and the database
  intelpod feeds check            fetch every feed once and record its health
  intelpod run                    full pipeline (ingest -> cluster -> rank -> review -> research -> debate -> impact -> report)
  intelpod run --stop-after review   Phase 1 only (then `intelpod run --resume` or `intelpod research`)
  intelpod run --no-research      Phase 1 and the report, without Phase 2
  intelpod research [--run-id ID] [--refresh]   Phase 2 on an existing run's selected events
  intelpod feeds check --research   check the research feeds (config/research_feeds.yaml)
  intelpod debate [--run-id ID] [--refresh]     Phase 3 (claims, viewpoints, disagreement) on a researched run
  intelpod sources [--gaps]       the source registry (config/sources.yaml) and what it lacks
  intelpod impact [--run-id ID] [--refresh]     Phase 4 (context, impact, connections) -> intelligence brief
  intelpod forecasts [--status open]            the forecast ledger
  intelpod forecasts resolve ID --verdict correct|incorrect|partly|unresolvable --outcome "..."
  intelpod run --resume [--run-id ID]  continue an interrupted/paused run
  intelpod events [--run ID] [--all]
  intelpod runs
  intelpod serve                  local dashboard on http://127.0.0.1:8000
  intelpod llm models [--filter x]  models your NVIDIA key can use
  intelpod llm test               one tiny structured call to check key + model
  intelpod newsdata test          one NewsData.io request (uses 1 credit)
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import platform
import shutil
import sys

from . import __version__
from .config import PROJECT_ROOT, Settings, load_settings
from .db import Database
from .logging_setup import read_secret, setup_logging
from .pipeline.runner import API_USER_AGENT
from .repository import Repository


def _settings(args) -> Settings:
    overrides = {}
    if getattr(args, "no_gdelt", False):
        overrides["ingestion.providers.gdelt.enabled"] = False
    if getattr(args, "no_rss", False):
        overrides["ingestion.providers.rss.enabled"] = False
    if getattr(args, "no_newsdata", False):
        overrides["ingestion.providers.newsdata.enabled"] = False
    if getattr(args, "no_llm", False):
        overrides["llm.enabled"] = False
    if getattr(args, "no_research", False):
        overrides["research.enabled"] = False
    if getattr(args, "no_debate", False):
        overrides["debate.enabled"] = False
    if getattr(args, "no_impact", False):
        overrides["impact.enabled"] = False
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
    if sys.platform == "win32":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MemoryStatus()
            st.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return round(st.ullTotalPhys / 1024 ** 3, 1)
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
    report["api_keys"] = {
        "NEWSDATA_API_KEY": "set" if read_secret(s.ingestion.providers.newsdata.api_key_env) else "not set (or malformed)",
        "NVIDIA_API_KEY": "set" if read_secret("NVIDIA_API_KEY") else "not set (or malformed)",
    }
    repo = Repository(Database(s.db_file))
    report["newsdata_requests_today_utc"] = repo.requests_today("newsdata")
    from .llm.client import LLMClient, LLMError
    try:
        client = LLMClient(s.llm, repo=repo, user_agent=API_USER_AGENT)
        ok, why = client.availability()
        if ok:
            models = client.list_models()
            wanted = [m.id for m in s.llm.tasks["event_review"].models]
            report["llm"] = {"provider": s.llm.provider, "models_listed": len(models),
                             "event_review_models": {m: ("listed" if m in models else "NOT listed") for m in wanted},
                             "note": "listing does not prove a model can be called; run `intelpod llm test`"}
        else:
            report["llm"] = {"provider": s.llm.provider, "available": False, "reason": why}
    except (LLMError, Exception) as e:  # noqa: BLE001 - doctor must always print a report
        report["llm"] = {"provider": s.llm.provider, "available": False, "reason": f"{type(e).__name__}: {str(e)[:200]}"}
    from .config import dotenv_path
    env = dotenv_path()
    report["env_file"] = (str(env) if env else
                          f"not found: create {PROJECT_ROOT / '.env'} (copy .env.example), or set keys as "
                          f"environment variables")
    if env is None and (PROJECT_ROOT / ".env.example").exists():
        ex = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8", errors="replace")
        if any(line.split("=", 1)[1].strip().strip('"\'') for line in ex.splitlines()
               if "_KEY=" in line and not line.lstrip().startswith("#")):
            report["env_file"] += " - .env.example contains a key value: it is never read; copy it to .env"
    ram = report["ram_gb"]
    report["recommendation"] = "RAM could not be detected; see docs/OLLAMA.md to choose a model size" if ram is None else (
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
    feeds_file = s.research.feeds.feeds_file if getattr(args, "research", False) else s.ingestion.feeds_file
    feeds = load_feeds(s.resolve(feeds_file), include_disabled=args.include_disabled)
    ok_count = 0
    with HttpClient(s.http, s.app.user_agent) as http:
        provider = RssProvider(http, feeds, use_conditional_get=False)
        if getattr(args, "research", False):
            provider.name = "research_feeds"
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
          f"{feeds_file} accordingly.")
    return 0 if ok_count else 1


def cmd_run(args) -> int:
    from .pipeline.runner import NothingToResume, Phase1Pipeline, RunInProgress, StageError

    s = _settings(args)
    pipe = Phase1Pipeline(s)
    try:
        run_id = pipe.run(run_id=args.run_id, resume=args.resume, stop_after=args.stop_after, force=args.force)
    except NothingToResume as e:
        print(str(e), file=sys.stderr)
        return 1
    except (StageError, RunInProgress, ValueError) as e:
        print(f"\nRun failed: {e}", file=sys.stderr)
        if not isinstance(e, RunInProgress):
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


def cmd_research(args) -> int:
    """Phase 2 on its own: research the selected events of an existing run (default: latest
    completed run), then rewrite its report."""
    import time

    from .pipeline.runner import Phase1Pipeline, RunContext, RUN_ID_RE
    from .research.runner import run_research
    from .research.store import ResearchStore

    s = _settings(args)
    pipe = Phase1Pipeline(s)
    if args.run_id and not RUN_ID_RE.fullmatch(args.run_id):
        print("invalid run id", file=sys.stderr)
        return 1
    row = pipe.repo.get_run(args.run_id) if args.run_id else pipe.repo.latest_run(status=None)
    if row is None:
        print("no run found; run `intelpod run --stop-after review` first", file=sys.stderr)
        return 1
    stages = pipe.repo.stages(row.run_id)
    if not (stages.get("rank") and stages["rank"].status == "completed"):
        print(f"run {row.run_id} has no ranked events yet; finish Phase 1 first", file=sys.stderr)
        return 1
    if pipe.repo.active_run() is not None:
        print("a run is in progress; wait for it to finish", file=sys.stderr)
        return 2
    s.research.enabled = True
    ctx = RunContext(run_id=row.run_id, now=datetime.fromisoformat(row.params["now"]), params=row.params,
                     outputs={k: v.output or {} for k, v in stages.items() if v.status == "completed"})
    pipe.repo.stage_start(row.run_id, "research")
    t0 = time.monotonic()
    try:
        out = run_research(pipe, ctx, refresh=args.refresh)
    except Exception as e:  # noqa: BLE001
        pipe.repo.stage_finish(row.run_id, "research", "failed", error=f"{type(e).__name__}: {e}")
        print(f"research failed: {e}", file=sys.stderr)
        return 2
    out["duration_s"] = round(time.monotonic() - t0, 2)
    pipe.repo.stage_finish(row.run_id, "research", "completed", output=out)
    ctx.outputs["research"] = out
    pipe.repo.stage_start(row.run_id, "report")
    rep = pipe.stage_report(ctx)
    pipe.repo.stage_finish(row.run_id, "report", "completed", output=rep)
    store = ResearchStore(pipe.db)
    print(f"run: {row.run_id}")
    for r in pipe.repo.scores_for_run(row.run_id, selected_only=True):
        pkg = store.package(row.run_id, r.event_id)
        if pkg is None:
            continue
        print(f"\n#{r.rank} {r.title}\n   {len(pkg.result_ids)} sources ({pkg.status}); "
              + ", ".join(f"{k}: {v}" for k, v in sorted(pkg.content_mix.items())))
        for lim in pkg.limitations[:4]:
            print(f"   - {lim}")
    for name, note in (out.get("provider_notes") or {}).items():
        print(f"note ({name}): {note}")
    for kind, path in (rep.get("paths") or {}).items():
        print(f"{kind} report: {path}")
    if s.debate.enabled:
        print(f"\nviewpoint analysis for re-researched events was cleared; run `intelpod debate --run-id {row.run_id}`")
    return 0


def cmd_debate(args) -> int:
    """Phase 3 on its own for an existing, researched run (default: latest), then rewrite its report."""
    import time

    from .debate.runner import run_debate
    from .debate.store import DebateStore
    from .pipeline.runner import Phase1Pipeline, RunContext, RUN_ID_RE

    s = _settings(args)
    pipe = Phase1Pipeline(s)
    if args.run_id and not RUN_ID_RE.fullmatch(args.run_id):
        print("invalid run id", file=sys.stderr)
        return 1
    row = pipe.repo.get_run(args.run_id) if args.run_id else pipe.repo.latest_run(status=None)
    if row is None:
        print("no run found; run `intelpod run` first", file=sys.stderr)
        return 1
    stages = pipe.repo.stages(row.run_id)
    if not (stages.get("research") and stages["research"].status == "completed"):
        print(f"run {row.run_id} has not been researched yet; run `intelpod research --run-id {row.run_id}`",
              file=sys.stderr)
        return 1
    if pipe.repo.active_run() is not None:
        print("a run is in progress; wait for it to finish", file=sys.stderr)
        return 2
    s.debate.enabled = True
    ctx = RunContext(run_id=row.run_id, now=datetime.fromisoformat(row.params["now"]), params=row.params,
                     outputs={k: v.output or {} for k, v in stages.items() if v.status == "completed"})
    pipe.repo.stage_start(row.run_id, "debate")
    t0 = time.monotonic()
    try:
        out = run_debate(pipe, ctx, refresh=args.refresh)
    except Exception as e:  # noqa: BLE001
        pipe.repo.stage_finish(row.run_id, "debate", "failed", error=f"{type(e).__name__}: {e}")
        print(f"debate analysis failed: {e}", file=sys.stderr)
        return 2
    out["duration_s"] = round(time.monotonic() - t0, 2)
    pipe.repo.stage_finish(row.run_id, "debate", "completed", output=out)
    ctx.outputs["debate"] = out
    pipe.repo.stage_start(row.run_id, "report")
    rep = pipe.stage_report(ctx)
    pipe.repo.stage_finish(row.run_id, "report", "completed", output=rep)
    store = DebateStore(pipe.db)
    print(f"run: {row.run_id}")
    if out.get("status") == "skipped":
        print(f"skipped: {out.get('reason')}")
    for r in pipe.repo.scores_for_run(row.run_id, selected_only=True):
        pkg = store.package(row.run_id, r.event_id)
        if pkg is None:
            continue
        print(f"\n#{r.rank} {r.title}\n   {pkg.status}; confidence {pkg.overall_confidence}; {len(pkg.claims)} claims "
              f"({len(pkg.consensus)} agreed, {len(pkg.contested)} contested, {len(pkg.unverified)} single-source); "
              f"{len(pkg.questions)} debated question(s)")
        if pkg.no_disagreement_note:
            print(f"   {pkg.no_disagreement_note}")
        for v in pkg.viewpoint_searches:
            print(f"   searched: {v.perspective} -> {'found ' + str(len(v.added_source_ids)) if v.found else 'nothing found'}")
        for lim in pkg.limitations[:4]:
            print(f"   - {lim}")
    for name, note in (out.get("provider_notes") or {}).items():
        print(f"note ({name}): {note}")
    for kind, path in (rep.get("paths") or {}).items():
        print(f"{kind} report: {path}")
    return 0


def _existing_run(pipe, run_id: str | None, needs: str):
    from .pipeline.runner import RUN_ID_RE
    if run_id and not RUN_ID_RE.fullmatch(run_id):
        print("invalid run id", file=sys.stderr)
        return None, 1
    row = pipe.repo.get_run(run_id) if run_id else pipe.repo.latest_run(status=None)
    if row is None:
        print("no run found; run `intelpod run` first", file=sys.stderr)
        return None, 1
    stages = pipe.repo.stages(row.run_id)
    if not (stages.get(needs) and stages[needs].status == "completed"):
        print(f"run {row.run_id} has no completed {needs} stage yet", file=sys.stderr)
        return None, 1
    if pipe.repo.active_run() is not None:
        print("a run is in progress; wait for it to finish", file=sys.stderr)
        return None, 2
    return (row, stages), 0


def cmd_impact(args) -> int:
    """Phase 4 on its own for an existing run (default: latest), then rewrite its reports."""
    import time

    from .impact.runner import run_impact
    from .impact.store import ImpactStore
    from .pipeline.runner import Phase1Pipeline, RunContext

    s = _settings(args)
    pipe = Phase1Pipeline(s)
    found, code = _existing_run(pipe, args.run_id, "rank")
    if found is None:
        return code
    row, stages = found
    s.impact.enabled = True
    ctx = RunContext(run_id=row.run_id, now=datetime.fromisoformat(row.params["now"]), params=row.params,
                     outputs={k: v.output or {} for k, v in stages.items() if v.status == "completed"})
    pipe.repo.stage_start(row.run_id, "impact")
    t0 = time.monotonic()
    try:
        out = run_impact(pipe, ctx, refresh=args.refresh)
    except Exception as e:  # noqa: BLE001
        pipe.repo.stage_finish(row.run_id, "impact", "failed", error=f"{type(e).__name__}: {e}")
        print(f"impact analysis failed: {e}", file=sys.stderr)
        return 2
    out["duration_s"] = round(time.monotonic() - t0, 2)
    pipe.repo.stage_finish(row.run_id, "impact", "completed", output=out)
    ctx.outputs["impact"] = out
    pipe.repo.stage_start(row.run_id, "report")
    rep = pipe.stage_report(ctx)
    pipe.repo.stage_finish(row.run_id, "report", "completed", output=rep)
    store = ImpactStore(pipe.db)
    print(f"run: {row.run_id}")
    if out.get("status") == "skipped":
        print(f"skipped: {out.get('reason')}")
    for r in pipe.repo.scores_for_run(row.run_id, selected_only=True):
        a = store.analysis(row.run_id, r.event_id)
        if a is None:
            continue
        print(f"\n#{r.rank} {r.title}\n   {a.status}; dimensions: {', '.join(d.label for d in a.dimensions[:4]) or '-'}; "
              f"{len(a.chains)} effect chain(s); India: {'yes' if a.india else 'not discussed'}")
        for lim in a.limitations[:3]:
            print(f"   - {lim}")
    if out.get("status") == "completed":
        print(f"\n{out['links']} connection(s) ({out['evidenced_links']} evidenced); {out['new_forecasts']} new forecast(s)")
    for name, note in (out.get("notes") or {}).items():
        print(f"note ({name}): {note}")
    for kind, path in (rep.get("paths") or {}).items():
        print(f"{kind} report: {path}")
    return 0


def cmd_forecasts(args) -> int:
    from .impact.store import ImpactStore

    s = _settings(args)
    store = ImpactStore(Database(s.db_file))
    if args.action == "resolve":
        if not (args.id and args.verdict and args.outcome):
            print("usage: intelpod forecasts resolve ID --verdict correct|incorrect|partly|unresolvable "
                  "--outcome \"what happened\"", file=sys.stderr)
            return 1
        try:
            f = store.resolve_forecast(args.id, args.verdict, args.outcome, amend=args.amend)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        if f is None:
            print(f"no forecast {args.id}", file=sys.stderr)
            return 1
        print(f"resolved {f.forecast_id}: {f.verdict} — {f.outcome}")
        return 0
    items = store.forecasts(status=None if args.status == "all" else args.status)
    for f in items:
        print(f"{f.forecast_id}  [{f.status}{': ' + f.verdict if f.verdict else ''}]  {f.first_seen_at:%Y-%m-%d}  "
              f"{f.speaker or f.publisher or 'unknown'}"
              + (f"  (timeframe: {f.timeframe})" if f.timeframe else "")
              + f"\n    “{f.text}”" + (f"\n    outcome: {f.outcome}" if f.outcome else ""))
    print(f"\n{len(items)} forecast(s). Forecasts are statements about the future, not facts.")
    return 0


def cmd_sources(args) -> int:
    """List the source registry; with --gaps, show which feed publishers it does not describe."""
    from .debate.registry import Registry, economy
    from .providers.rss import load_feeds

    s = _settings(args)
    reg = Registry.load(s.resolve(s.debate.sources_file))
    if not args.gaps:
        for p in reg.profiles:
            orient = [f"{f.split('_')[0]}={getattr(p, f)}" for f in
                      ("editorial_orientation", "economic_orientation", "foreign_policy_orientation") if p.documented(f)]
            print(f"{p.publisher:<28} {p.country or '?':<3} {economy(p.country):<9} {p.source_type or '?':<18} "
                  f"{p.ownership or 'ownership unknown':<28} orientation: {', '.join(orient) or 'unknown'}"
                  f"{' (disputed)' if p.disputed else ''}")
        print(f"\n{len(reg.profiles)} sources in {s.debate.sources_file}. Orientation is shown only where documented "
              f"with references.")
        return 0
    missing = []
    for path in (s.ingestion.feeds_file, s.research.feeds.feeds_file):
        if not s.resolve(path).exists():
            continue
        for f in load_feeds(s.resolve(path), include_disabled=True):
            if reg.lookup(f.domain, f.publisher) is None:
                missing.append(f"{f.publisher} ({f.domain})")
    missing = sorted(set(missing))
    print(f"{len(missing)} feed publisher(s) not in the registry:" if missing else "every feed publisher is in the registry")
    for m in missing:
        print(f"  - {m}")
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
        notes = []
        cl = stages.get("cluster")
        if cl and cl.output and cl.output.get("embeddings"):
            notes.append(f"embeddings: {cl.output['embeddings']}")
        rv = stages.get("review")
        if rv and rv.output:
            notes.append(f"review: {rv.output.get('status')}" + (f" ({rv.output['reason']})" if rv.output.get("reason") else
                                                                  f" ({rv.output.get('reviewed')} events, {', '.join(rv.output.get('models') or [])})"))
        ing = stages.get("ingest")
        for name, v in ((ing.output or {}).get("providers", {}) if ing else {}).items():
            if v.get("note"):
                notes.append(f"{name}: {v['note']}")
        for n in notes:
            print(f"    {n}")
    usage = repo.provider_usage()
    if usage:
        print("\nrequests in the last 24h: " + ", ".join(f"{u['provider']}={u['requests']} ({u['ok']} ok)" for u in usage))
    print(f"NewsData.io requests today (UTC): {repo.requests_today('newsdata')} "
          f"of budget {s.ingestion.providers.newsdata.daily_credit_budget}")
    for u in repo.llm_usage():
        print(f"LLM last 24h: {u['provider']} · {u['model']}: {u['calls']} calls ({u['cached']} cached, {u['ok']} ok), "
              f"{u['prompt_tokens']}+{u['completion_tokens']} tokens")
    return 0


def cmd_llm_models(args) -> int:
    from .llm.client import LLMClient, LLMError

    s = _settings(args)
    try:
        models = LLMClient(s.llm, user_agent=API_USER_AGENT).list_models()
    except LLMError as e:
        print(f"cannot list models: {e}", file=sys.stderr)
        return 2
    for m in models:
        if not args.filter or args.filter.lower() in m.lower():
            print(m)
    print(f"\n{len(models)} models listed by {s.llm.provider}. Listing does not guarantee a model is callable "
          f"on the free tier; check with `intelpod llm test --model <id>`.", file=sys.stderr)
    return 0


def cmd_llm_test(args) -> int:
    import time as _time

    from pydantic import BaseModel

    from .llm.client import LLMClient, LLMError

    class Probe(BaseModel):
        ok: bool
        capital_of_france: str

    s = _settings(args)
    if args.model:
        from .config import LLMModelChoice
        current = {m.id: m for m in s.llm.tasks["event_review"].models}
        s.llm.tasks["event_review"].models = [current.get(args.model) or LLMModelChoice(id=args.model)]
    s.llm.cache = False
    client = LLMClient(s.llm, repo=Repository(Database(s.db_file)), user_agent=API_USER_AGENT)
    t0 = _time.monotonic()
    try:
        obj, model = client.chat_json("event_review", [
            {"role": "user", "content": 'Reply with JSON {"ok": true, "capital_of_france": "<city>"}.'}], Probe)
    except LLMError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    print(f"OK: {model} answered {obj.model_dump()} in {_time.monotonic() - t0:.1f}s")
    return 0


def cmd_newsdata_test(args) -> int:
    from .http import HttpClient
    from .providers.newsdata import NewsdataProvider

    s = _settings(args)
    cfg = s.ingestion.providers.newsdata
    if not cfg.queries:
        print("no newsdata queries configured", file=sys.stderr)
        return 2
    repo = Repository(Database(s.db_file))
    with HttpClient(s.http, API_USER_AGENT) as http:
        p = NewsdataProvider(http, cfg, credits_used_today=lambda: repo.requests_today("newsdata"))
        if not p.api_key:
            print(f"{cfg.api_key_env} is not set", file=sys.stderr)
            return 2
        items, rec, _, _ = p.fetch_page(cfg.queries[0], None)
    repo.log_fetches(None, [rec])
    if not rec.ok:
        print(f"FAILED: {rec.error}", file=sys.stderr)
        return 2
    print(f"OK: {len(items)} articles; credits remaining today: {p.last_credits_remaining if p.last_credits_remaining is not None else 'not reported'}")
    for it in items[:5]:
        print(f"  - {it.published_at:%Y-%m-%d %H:%M} {it.source_name}: {it.title}" if it.published_at else f"  - {it.title}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    s = _settings(args)
    from .web.app import create_app
    extra_hosts = list(args.allow_host or [])
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        extra_hosts.append(args.host)
        print(f"warning: serving on {args.host}; the dashboard has no login. Add the name you browse to with "
              f"--allow-host.", file=sys.stderr)
    uvicorn.run(create_app(s, allowed_hosts=extra_hosts), host=args.host, port=args.port, log_level="info")
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
    chk.add_argument("--research", action="store_true", help="check config/research_feeds.yaml instead")
    chk.set_defaults(fn=cmd_feeds_check)

    run = sub.add_parser("run", help="run the pipeline (Phase 1 selection, Phase 2 research, Phase 3 viewpoints)")
    run.add_argument("--resume", action="store_true", help="resume the given (or latest) run")
    run.add_argument("--run-id", help="run id to resume")
    run.add_argument("--stop-after", choices=["ingest", "cluster", "rank", "review", "research", "debate", "impact",
                                              "report"])
    run.add_argument("--force", action="store_true", help="start even if another run looks active")
    run.add_argument("--no-gdelt", action="store_true")
    run.add_argument("--no-rss", action="store_true")
    run.add_argument("--no-newsdata", action="store_true")
    run.add_argument("--no-llm", action="store_true", help="skip the model review (keyword ranking only)")
    run.add_argument("--lookback-hours", type=int)
    run.add_argument("--no-research", action="store_true", help="skip Phase 2 research (and so Phase 3)")
    run.add_argument("--no-debate", action="store_true", help="skip Phase 3 viewpoint and claim analysis")
    run.add_argument("--no-impact", action="store_true", help="skip Phase 4 impact analysis")
    run.set_defaults(fn=cmd_run)

    rs = sub.add_parser("research", help="Phase 2: research the selected events of an existing run")
    rs.add_argument("--run-id", help="run id (default: latest run)")
    rs.add_argument("--refresh", action="store_true", help="research again events already researched in that run")
    rs.add_argument("--no-llm", action="store_true", help="no model keywords or notes")
    rs.set_defaults(fn=cmd_research)

    db = sub.add_parser("debate", help="Phase 3: claims, viewpoints and disagreement for a researched run")
    db.add_argument("--run-id", help="run id (default: latest run)")
    db.add_argument("--refresh", action="store_true", help="analyse again events already analysed in that run")
    db.add_argument("--no-llm", action="store_true", help="claim map only; no model questions or positions")
    db.set_defaults(fn=cmd_debate)

    im = sub.add_parser("impact", help="Phase 4: context, business impact and connections -> intelligence brief")
    im.add_argument("--run-id", help="run id (default: latest run)")
    im.add_argument("--refresh", action="store_true", help="analyse again events already analysed in that run")
    im.add_argument("--no-llm", action="store_true", help="evidence tables only; no model effect chains")
    im.set_defaults(fn=cmd_impact)

    fc = sub.add_parser("forecasts", help="the forecast ledger (list, or resolve one)")
    fc.add_argument("action", nargs="?", choices=["list", "resolve"], default="list")
    fc.add_argument("id", nargs="?", help="forecast id (for resolve)")
    fc.add_argument("--status", choices=["open", "resolved", "all"], default="all")
    fc.add_argument("--verdict", choices=["correct", "incorrect", "partly", "unresolvable"])
    fc.add_argument("--outcome", help="what actually happened, with a source if possible")
    fc.add_argument("--amend", action="store_true", help="change an outcome already recorded (the old one is kept)")
    fc.set_defaults(fn=cmd_forecasts)

    so = sub.add_parser("sources", help="show the source registry (config/sources.yaml)")
    so.add_argument("--gaps", action="store_true", help="list feed publishers the registry does not describe")
    so.set_defaults(fn=cmd_sources)

    ev = sub.add_parser("events", help="show ranked events of a run")
    ev.add_argument("--run", help="run id (default: latest completed)")
    ev.add_argument("--all", action="store_true", help="include events that were not selected")
    ev.set_defaults(fn=cmd_events)

    runs = sub.add_parser("runs", help="list runs and stage status")
    runs.add_argument("--limit", type=int, default=15)
    runs.set_defaults(fn=cmd_runs)

    llm = sub.add_parser("llm", help="language-model tools")
    lsub = llm.add_subparsers(dest="llm_cmd", required=True)
    lm = lsub.add_parser("models", help="list models available to your key")
    lm.add_argument("--filter")
    lm.set_defaults(fn=cmd_llm_models)
    lt = lsub.add_parser("test", help="one small structured-output call")
    lt.add_argument("--model", help="model id to try instead of llm.tasks.event_review.model")
    lt.set_defaults(fn=cmd_llm_test)

    nd = sub.add_parser("newsdata", help="NewsData.io tools")
    ndsub = nd.add_subparsers(dest="nd_cmd", required=True)
    ndsub.add_parser("test", help="one request with the first configured query (1 credit)").set_defaults(fn=cmd_newsdata_test)

    srv = sub.add_parser("serve", help="start the local dashboard")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.add_argument("--allow-host", action="append", help="extra host name allowed in requests (repeatable)")
    srv.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
