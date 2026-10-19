"""Phase 6 orchestration: render each script line with the host's voice (cached per line), retry
failures, assemble with pauses and optional intro/outro, convert, and check the result.

Re-running renders only lines that are missing, failed or changed (text, voice, speed or engine
are part of each line's cache key), so a failed line never forces a whole new episode.
An episode with any line that could not be rendered is NOT assembled: a missing line could drop a
hedge or a correction and change what the episode says. It is reported as incomplete instead.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..script.models import Episode
from ..textutil import stable_hash
from .engines import Engine, EngineError
from .models import AudioLine, AudioRender, Chapter, Check
from .textprep import tts_text
from .wavtools import (convert_with_ffmpeg, decode_with_ffmpeg, ffmpeg, probe_duration, read_wav, resample,
                       scale, silence, stats, write_wav)

log = logging.getLogger(__name__)


def render(ep: Episode, engine: Engine, cfg, run_dir: Path, redo: set[str] | None = None,
           wpm: int = 150) -> AudioRender:
    audio_dir = run_dir / "audio"
    from ..config import AudioConfig
    defaults = AudioConfig().voices.get(engine.name, {})
    voices = {**defaults, **(cfg.voices.get(engine.name) or {})}   # a config missing an engine keeps its defaults
    ar = AudioRender(run_id=ep.run_id, episode_id=ep.episode_id, title=ep.title, engine=engine.name, voices=voices,
                     speed=cfg.speed, status="complete", created_at=datetime.now(timezone.utc))
    if not ep.lines:
        ar.status = "skipped"
        ar.limitations.append("The episode has no verified script lines; nothing to speak.")
        return ar
    if voices.get("A") == voices.get("B"):
        ar.limitations.append("Both hosts use the same voice; set two different voices in audio.voices.")
    prons = {p.name: p.say for p in ep.pronunciations} if cfg.apply_pronunciations else {}

    # 1. per-line synthesis, cached
    for x in ep.lines:
        voice = voices.get(x.speaker) or ""
        spoken = tts_text(x.text, prons)
        key = stable_hash(engine.name, voice, f"{cfg.speed:.2f}", spoken)[:12]
        rel = f"audio/lines/{x.line_id}-{key}.wav"
        line = AudioLine(line_id=x.line_id, speaker=x.speaker, voice=voice, text=spoken, file=rel)
        path = run_dir / rel
        if path.exists() and path.stat().st_size > 44 and not (redo and x.line_id in redo):
            line.status, line.cached = "ok", True
        else:
            for attempt in range(1, cfg.max_retries + 2):
                line.attempts = attempt
                try:
                    engine.synth(spoken, voice, cfg.speed, path)
                    read_wav(path)                      # must be a readable 16-bit WAV
                    line.status, line.error = "ok", None
                    break
                except (EngineError, OSError, ValueError, RuntimeError) as e:
                    line.status, line.error = "failed", f"{type(e).__name__}: {str(e)[:200]}"
                    path.unlink(missing_ok=True)
                    log.warning("TTS failed for %s (attempt %d): %s", x.line_id, attempt, e)
        ar.lines.append(line)

    failed = [ln for ln in ar.lines if ln.status != "ok"]
    if failed:
        ar.status = "incomplete"
        ar.checks.append(Check(name="all lines rendered", ok=False,
                               detail=f"{len(failed)} line(s) failed: " + ", ".join(f.line_id for f in failed[:10])))
        ar.limitations.append(f"{len(failed)} line(s) could not be spoken after retries; the episode was not "
                              f"assembled (a missing line could change its meaning). Run `intelpod audio` again: "
                              f"only the missing lines are retried.")
        return ar

    # 2. assembly
    rate = 0
    parts = []
    for ln in ar.lines:
        samples, r = read_wav(run_dir / ln.file)
        rate = rate or r
        parts.append(resample(samples, r, rate) if r != rate else samples)
    ar.sample_rate = rate
    import array
    out = array.array("h")
    intro = _extra(cfg.intro_file, rate, audio_dir, ar)
    if intro:
        out.extend(intro)
        out.extend(silence(0.5, rate))
    seg_of = {x.line_id: (x.segment, x.event_id) for x in ep.lines}
    titles = {s.event_id: s.title for s in ep.plan.stories}
    prev = None
    for ln, samples in zip(ar.lines, parts):
        seg = seg_of[ln.line_id]
        if prev is not None:
            out.extend(silence((cfg.segment_pause_ms if seg != prev else cfg.line_pause_ms) / 1000, rate))
        if seg != prev:
            ar.chapters.append(Chapter(title=titles.get(seg[1]) if seg[0] == "story" else seg[0].capitalize(),
                                       start_s=round(len(out) / rate, 2)))
        ln.start_s = round(len(out) / rate, 2)
        ln.duration_s = round(len(samples) / rate, 2)
        out.extend(samples)
        prev = seg
    outro = _extra(cfg.outro_file, rate, audio_dir, ar)
    if outro:
        out.extend(silence(0.5, rate))
        out.extend(outro)
    st = stats(out)
    if 0 < st["peak"] < 0.5 or st["peak"] > 0.99:      # bring quiet or hot renders to a sane level
        out = scale(out, 0.89 / st["peak"])
    wav = audio_dir / "episode.wav"
    write_wav(wav, out, rate)
    ar.files["wav"] = "audio/episode.wav"
    ar.duration_s = round(len(out) / rate, 1)
    if "mp3" in cfg.formats:
        if ffmpeg():
            err = convert_with_ffmpeg(wav, audio_dir / "episode.mp3", cfg.mp3_bitrate, cfg.loudness_lufs, ep.title)
            if err:
                ar.limitations.append(f"MP3 not created: {err}")
            else:
                ar.files["mp3"] = "audio/episode.mp3"
        else:
            ar.limitations.append("FFmpeg is not installed: WAV only (no MP3, no loudness normalisation).")
    if "wav" not in cfg.formats and "mp3" in ar.files:
        wav.unlink(missing_ok=True)
        del ar.files["wav"]
    _checks(ar, ep, cfg, run_dir, wpm)
    return ar


def _extra(path: str | None, rate: int, audio_dir: Path, ar: AudioRender):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        ar.limitations.append(f"intro/outro file not found: {p}")
        return None
    try:
        if p.suffix.lower() != ".wav":
            tmp = audio_dir / f"_{p.stem}.wav"
            err = decode_with_ffmpeg(p, tmp, rate)
            if err:
                ar.limitations.append(err)
                return None
            p = tmp
        samples, r = read_wav(p)
        return resample(samples, r, rate)
    except (OSError, ValueError, EOFError) as e:
        ar.limitations.append(f"intro/outro file unreadable ({p.name}): {e}")
        return None


def _checks(ar: AudioRender, ep: Episode, cfg, run_dir: Path, wpm: int) -> None:
    add = ar.checks.append
    add(Check(name="all lines rendered", ok=True, detail=f"{len(ar.lines)} lines"))
    words = sum(len(x.text.split()) for x in ep.lines)
    ar.expected_s = round(words / (wpm * cfg.speed) * 60 + len(ar.lines) * cfg.line_pause_ms / 1000, 1)
    lo, hi = 0.6 * ar.expected_s, 1.6 * ar.expected_s
    add(Check(name="duration plausible", ok=lo <= ar.duration_s <= hi, severity="warning",
              detail=f"{ar.duration_s:.0f} s for {words} words (expected about {ar.expected_s:.0f} s)"))
    odd = []
    for ln in ar.lines:
        n = max(1, len(ln.text.split()))
        per_word = ln.duration_s / n
        if not (0.12 <= per_word <= 1.2):
            odd.append(f"{ln.line_id} ({per_word:.2f} s/word)")
    add(Check(name="speech rate per line", ok=not odd, severity="warning",
              detail="all lines within 0.12-1.2 s per word" if not odd else "unusual: " + ", ".join(odd[:8])))
    try:
        samples, rate = read_wav(run_dir / ar.files.get("wav", "audio/episode.wav")) if "wav" in ar.files else (None, 0)
    except (OSError, ValueError, EOFError) as e:
        add(Check(name="final file readable", ok=False, detail=str(e)))
        ar.status = "failed"
        return
    if samples is not None:
        st = stats(samples)
        add(Check(name="final file readable", ok=len(samples) > 0 and rate == ar.sample_rate,
                  detail=f"{len(samples) / rate:.1f} s at {rate} Hz, mono 16-bit"))
        add(Check(name="not silent", ok=st["rms"] > 0.005, detail=f"RMS level {st['rms']:.3f}"))
        add(Check(name="no clipping", ok=st["clipped"] < 0.001, severity="warning",
                  detail=f"{100 * st['clipped']:.3f}% of samples at full scale"))
    if "mp3" in ar.files:
        d = probe_duration(run_dir / ar.files["mp3"])
        add(Check(name="MP3 duration matches", ok=d is not None and abs(d - ar.duration_s) <= max(1.0, 0.02 * ar.duration_s),
                  detail=f"MP3 {d:.1f} s vs WAV {ar.duration_s:.1f} s" if d is not None else "ffprobe unavailable",
                  severity="warning" if d is None else "error"))
    if any(not c.ok and c.severity == "error" for c in ar.checks):
        ar.status = "failed"

