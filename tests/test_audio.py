"""Phase 6: text preparation, WAV tools, per-line caching and retries, assembly, checks, engines,
pipeline stage, dashboard player and CLI. Real voices are not downloaded in tests: the
deterministic "test" engine stands in, and the Kokoro / Piper adapters run against stand-ins of
their documented APIs."""
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from intelpod.audio import engines
from intelpod.audio.engines import KokoroEngine, PiperEngine, ToneEngine, choose
from intelpod.audio.service import render
from intelpod.audio.textprep import tts_text
from intelpod.audio.wavtools import ffmpeg, read_wav, resample, silence, stats, write_wav
from intelpod.config import AudioConfig
from intelpod.script.models import Episode, EpisodePlan, Pronunciation, ScriptLine, StoryPlan
from intelpod.web.app import create_app

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def episode(texts=None):
    texts = texts or ["Welcome to IntelPod.", "The Reserve Bank of India cut rates by 25 bps.",
                      "Inflation could ease to 4% next year, according to Mint.", "Thanks for listening."]
    segs = ["opening", "story", "story", "closing"]
    lines = [ScriptLine(line_id=f"L{i + 1}", segment=segs[i], event_id="e1" if segs[i] == "story" else None,
                        speaker="A" if i % 2 == 0 else "B", text=t, kind="fact") for i, t in enumerate(texts)]
    return Episode(run_id="run_a", episode_id="ep-1", title="IntelPod daily briefing", hosts={"A": "Maya", "B": "Arjun"},
                   status="validated", lines=lines, plan=EpisodePlan(stories=[StoryPlan(event_id="e1", title="RBI cut")]),
                   pronunciations=[Pronunciation(name="Arjun", say="AR-jun")], created_at=NOW)


def cfg(**kw):
    c = AudioConfig(engine="test", formats=["wav"], line_pause_ms=200, segment_pause_ms=500)
    return c.model_copy(update=kw)


# -- text preparation -----------------------------------------------------------------------------
def test_tts_text_speaks_symbols_and_applies_pronunciations():
    t = tts_text("Rates fell 0.25% to ₹1,200 crore; $5 billion & 25 bps “quoted” [3] Arjun said.", {"Arjun": "AR-jun"})
    assert "0.25 percent" in t and "1200 crore rupees" in t and "5 billion dollars" in t
    assert " and " in t and "25 basis points" in t and "“" not in t and "[3]" not in t and "AR-jun said" in t


# -- wav tools ------------------------------------------------------------------------------------
def test_wav_roundtrip_resample_stats(tmp_path):
    s = silence(0.5, 16000)
    assert len(s) == 8000 and stats(s)["rms"] == 0
    write_wav(tmp_path / "a.wav", s, 16000)
    back, rate = read_wav(tmp_path / "a.wav")
    assert rate == 16000 and len(back) == 8000
    assert abs(len(resample(back, 16000, 24000)) - 12000) <= 1


# -- rendering ------------------------------------------------------------------------------------
def test_render_assembles_caches_and_checks(tmp_path):
    ep = episode()
    ar = render(ep, ToneEngine(cfg(), tmp_path), cfg(), tmp_path, wpm=150)
    assert ar.status == "complete" and ar.files == {"wav": "audio/episode.wav"}
    assert ar.voices == {"A": "low", "B": "high"} and [ln.voice for ln in ar.lines] == ["low", "high", "low", "high"]
    assert [c.title for c in ar.chapters] == ["Opening", "RBI cut", "Closing"]
    assert ar.lines[0].start_s == 0 and all(b.start_s > a.start_s for a, b in zip(ar.lines, ar.lines[1:]))
    assert all(c.ok for c in ar.checks if c.severity == "error")
    samples, rate = read_wav(tmp_path / "audio" / "episode.wav")
    assert abs(len(samples) / rate - ar.duration_s) < 0.2
    # Second render: everything from the cache. A changed line re-renders only that line.
    again = render(ep, ToneEngine(cfg(), tmp_path), cfg(), tmp_path)
    assert all(ln.cached for ln in again.lines)
    ep.lines[1].text = "The Reserve Bank of India cut rates by 25 bps on Friday."
    third = render(ep, ToneEngine(cfg(), tmp_path), cfg(), tmp_path)
    assert [ln.cached for ln in third.lines] == [True, False, True, True]
    redo = render(ep, ToneEngine(cfg(), tmp_path), cfg(), tmp_path, redo={"L4"})
    assert [ln.cached for ln in redo.lines] == [True, True, True, False]


def test_failed_line_blocks_assembly_until_retried(tmp_path):
    ep = episode()
    eng = ToneEngine(cfg(), tmp_path)
    eng.fail_texts = {"Inflation"}
    ar = render(ep, eng, cfg(max_retries=1), tmp_path)
    assert ar.status == "incomplete" and not ar.files and not (tmp_path / "audio" / "episode.wav").exists()
    bad = [ln for ln in ar.lines if ln.status == "failed"]
    assert [ln.line_id for ln in bad] == ["L3"] and bad[0].attempts == 2
    assert any("only the missing lines" in lim for lim in ar.limitations)
    eng.fail_texts = set()
    ok = render(ep, eng, cfg(), tmp_path)
    assert ok.status == "complete" and [ln.cached for ln in ok.lines] == [True, True, False, True]


def test_same_voice_and_missing_intro_are_reported(tmp_path):
    c = cfg(voices={"test": {"A": "low", "B": "low"}}, intro_file=str(tmp_path / "nope.wav"))
    ar = render(episode(), ToneEngine(c, tmp_path), c, tmp_path)
    assert ar.status == "complete"
    assert any("same voice" in lim for lim in ar.limitations) and any("not found" in lim for lim in ar.limitations)


def test_intro_wav_is_prepended(tmp_path):
    write_wav(tmp_path / "intro.wav", silence(1.0, 8000), 8000)
    c = cfg(intro_file=str(tmp_path / "intro.wav"))
    ar = render(episode(), ToneEngine(c, tmp_path), c, tmp_path)
    assert ar.lines[0].start_s >= 1.4                       # 1 s intro + 0.5 s gap


@pytest.mark.skipif(ffmpeg() is None, reason="FFmpeg not installed")
def test_mp3_conversion_and_duration_check(tmp_path):
    c = cfg(formats=["wav", "mp3"])
    ar = render(episode(), ToneEngine(c, tmp_path), c, tmp_path)
    assert "mp3" in ar.files and (tmp_path / ar.files["mp3"]).stat().st_size > 1000
    assert next(ch for ch in ar.checks if ch.name == "MP3 duration matches").ok


def test_empty_episode_is_skipped(tmp_path):
    ep = episode()
    ep.lines = []
    assert render(ep, ToneEngine(cfg(), tmp_path), cfg(), tmp_path).status == "skipped"


# -- engines --------------------------------------------------------------------------------------
def test_auto_reports_why_engines_are_unavailable(tmp_path, monkeypatch):
    for cls in (engines.KokoroEngine, engines.PiperEngine, engines.WindowsEngine, engines.EspeakEngine):
        monkeypatch.setattr(cls, "available", lambda self: (False, f"{self.name} missing"))
    eng, notes = choose(AudioConfig(), tmp_path)
    assert eng is None and set(notes) == {"kokoro", "piper", "windows", "espeak"}


def test_kokoro_adapter_uses_documented_api(tmp_path, monkeypatch):
    calls = {}

    class FakeKokoro:
        def __init__(self, model_path, voices_path):
            calls["paths"] = (Path(model_path).name, Path(voices_path).name)

        def create(self, text, voice, speed=1.0, lang="en-us"):
            calls["create"] = (voice, speed, lang)
            return [0.1, -0.1] * 2400, 24000

        def get_voices(self):
            return ["am_michael", "af_heart"]

    monkeypatch.setitem(sys.modules, "kokoro_onnx", types.SimpleNamespace(Kokoro=FakeKokoro))
    monkeypatch.setattr(engines.importlib.util, "find_spec", lambda name: object())
    c = AudioConfig()
    d = tmp_path / "kokoro"
    d.mkdir()
    (d / c.kokoro_model).write_bytes(b"x")
    (d / c.kokoro_voices).write_bytes(b"x")
    eng = KokoroEngine(c, tmp_path)
    assert eng.available() == (True, "ok") and eng.voices() == ["af_heart", "am_michael"]
    eng.synth("Hello", "bm_george", 1.1, tmp_path / "k.wav")
    assert calls["paths"] == ("kokoro-v1.0.onnx", "voices-v1.0.bin") and calls["create"] == ("bm_george", 1.1, "en-gb")
    samples, rate = read_wav(tmp_path / "k.wav")
    assert rate == 24000 and len(samples) == 4800


def test_piper_adapter_uses_documented_api(tmp_path, monkeypatch):
    seen = {}

    class FakeVoice:
        @staticmethod
        def load(path):
            seen["model"] = Path(path).name
            return FakeVoice()

        def synthesize_wav(self, text, wav_file, syn_config=None):
            seen["length_scale"] = syn_config.length_scale
            wav_file.setframerate(22050)
            wav_file.setsampwidth(2)
            wav_file.setnchannels(1)
            wav_file.writeframes(b"\x00\x01" * 2205)

    class SynthesisConfig:
        def __init__(self, length_scale=None):
            self.length_scale = length_scale

    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=FakeVoice, SynthesisConfig=SynthesisConfig))
    monkeypatch.setattr(engines.importlib.util, "find_spec", lambda name: object())
    (tmp_path / "piper").mkdir()
    (tmp_path / "piper" / "en_US-amy-medium.onnx").write_bytes(b"x")
    eng = PiperEngine(AudioConfig(), tmp_path)
    assert eng.available()[0] is False                      # model without its .onnx.json: partial download
    (tmp_path / "piper" / "en_US-amy-medium.onnx.json").write_text("{}")
    assert eng.available() == (True, "ok")
    eng.synth("Hello", "en_US-amy-medium", 1.25, tmp_path / "p.wav")
    assert seen == {"model": "en_US-amy-medium.onnx", "length_scale": 0.8}
    assert read_wav(tmp_path / "p.wav")[1] == 22050


# -- pipeline, dashboard, CLI ---------------------------------------------------------------------
def test_audio_stage_player_media_and_cli(settings, tmp_path, monkeypatch, capsys):
    from test_debate_pipeline import run_pipeline
    settings.llm.enabled = False
    settings.audio.enabled = True
    settings.audio.engine = "test"
    settings.audio.formats = ["wav"]
    pipe, run_id = run_pipeline(settings, tmp_path)
    out = pipe.repo.stages(run_id)["audio"].output
    assert out["status"] == "completed" and out["audio_status"] == "complete" and out["engine"] == "test"
    d = settings.data_path / "runs" / run_id
    assert (d / "audio" / "episode.wav").exists()
    meta = json.loads((d / "episode.json").read_text())
    assert meta["files"]["wav"] == "audio/episode.wav" and meta["chapters"] and meta["lines"]
    tj = json.loads((d / "transcript.json").read_text())
    assert all("start_s" in line for line in tj["lines"])

    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    page = client.get("/episode")
    assert '<audio id="player"' in page.text and f"/media/{run_id}/episode.wav" in page.text
    r = client.get(f"/media/{run_id}/episode.wav")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    assert client.get(f"/media/{run_id}/script.md").status_code == 404          # only audio files
    assert client.get("/media/..%2F..%2Fetc/episode.wav").status_code == 404
    assert client.get(f"/api/runs/{run_id}/audio").json()["status"] == "complete"

    from intelpod import cli
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    assert cli.main(["audio", "--run-id", run_id, "--redo", "L2"]) == 0
    printed = capsys.readouterr().out
    assert "1 line(s) rendered" in printed and "wav:" in printed
    assert cli.main(["tts", "list"]) == 0


def test_engine_missing_from_config_keeps_default_voices(tmp_path):
    c = cfg(voices={"kokoro": {"A": "af_heart", "B": "am_michael"}})       # no "test" entry
    ar = render(episode(), ToneEngine(c, tmp_path), c, tmp_path)
    assert ar.voices == {"A": "low", "B": "high"}



# -- review regressions ---------------------------------------------------------------------------
class EmptyEngine(ToneEngine):
    """Writes a header-only WAV for one line, or raises odd exception types."""
    empty_for = "Inflation"
    raise_type = None

    def synth(self, text, voice, speed, out):
        if self.raise_type and self.empty_for in text:
            raise self.raise_type("boom")
        if self.empty_for in text:
            write_wav(out, silence(0, self.rate), self.rate)
            return
        super().synth(text, voice, speed, out)


def test_empty_or_silent_line_is_a_failure_not_a_complete_episode(tmp_path):
    ar = render(episode(), EmptyEngine(cfg(), tmp_path), cfg(max_retries=0), tmp_path)
    assert ar.status == "incomplete" and "unusable audio" in next(ln for ln in ar.lines if ln.line_id == "L3").error


@pytest.mark.parametrize("exc", [__import__("wave").Error, EOFError, ImportError, KeyError,
                                 __import__("subprocess").TimeoutExpired])
def test_any_engine_exception_is_a_per_line_failure(tmp_path, exc):
    eng = EmptyEngine(cfg(), tmp_path)
    eng.raise_type = (lambda m: exc("cmd", 1)) if exc.__name__ == "TimeoutExpired" else exc
    ar = render(episode(), eng, cfg(max_retries=1), tmp_path)
    assert ar.status == "incomplete" and [ln.line_id for ln in ar.lines if ln.status == "failed"] == ["L3"]


def test_truncated_cached_line_is_rerendered_and_failed_redo_keeps_old(tmp_path):
    ep = episode()
    ar = render(ep, ToneEngine(cfg(), tmp_path), cfg(), tmp_path)
    l2 = tmp_path / ar.lines[1].file
    write_wav(l2, silence(0, 16000), 16000)                 # killed mid-write: header only
    again = render(ep, ToneEngine(cfg(), tmp_path), cfg(), tmp_path)
    assert again.status == "complete" and not again.lines[1].cached
    eng = EmptyEngine(cfg(), tmp_path)
    eng.empty_for = "Reserve Bank"
    redo = render(ep, eng, cfg(max_retries=0), tmp_path, redo={"L2"})
    assert redo.status == "complete" and redo.lines[1].cached and any("earlier recording" in x for x in redo.limitations)


def test_missing_voice_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(ToneEngine, "voices", lambda self: ["low"])
    ar = render(episode(), ToneEngine(cfg(), tmp_path), cfg(), tmp_path)
    assert ar.status == "failed" and "not installed" in ar.limitations[0] and "high" in ar.limitations[0]


@pytest.mark.skipif(ffmpeg() is None, reason="FFmpeg not installed")
def test_float_intro_is_converted_and_mp3_only_still_checked(tmp_path):
    import subprocess
    intro = tmp_path / "intro.wav"
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=1", "-c:a", "pcm_f32le",
                    str(intro)], check=True)
    c = cfg(intro_file=str(intro), formats=["mp3"])
    ar = render(episode(), ToneEngine(c, tmp_path), c, tmp_path)
    assert ar.status == "complete" and ar.lines[0].start_s >= 1.4 and "wav" not in ar.files
    assert {ch.name for ch in ar.checks} >= {"final file readable", "not silent", "MP3 duration matches"}


def test_numbers_spoken_without_changing_them():
    assert tts_text("1,200,000 tonnes, $1.2bn, ₹500cr") == "1200000 tonnes, 1.2 billion dollars, 500 crore rupees"


def test_stale_audio_never_shown_or_timed_for_a_changed_script(tmp_path):
    from intelpod.audio.service import audio_matches
    ep = episode()
    ar = render(ep, ToneEngine(cfg(), tmp_path), cfg(), tmp_path)
    assert audio_matches(ar, ep)
    ep.lines[0].text = "Welcome back to IntelPod."
    assert not audio_matches(ar, ep)


def test_windows_quoting_handles_all_single_quote_styles(monkeypatch, tmp_path):
    from intelpod.audio.engines import WindowsEngine
    seen = {}

    def fake_ps(self, script, timeout=120):
        seen["script"] = script
        (tmp_path / "w.wav").write_bytes(b"x")
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(WindowsEngine, "_ps", fake_ps)
    WindowsEngine(AudioConfig(), tmp_path).synth("Hi", "Voice ’Odd' Name", 1.0, tmp_path / "w.wav")
    assert "'Voice ’’Odd'' Name'" in seen["script"]
