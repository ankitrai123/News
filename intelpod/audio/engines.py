"""Local text-to-speech engines. Each writes a 16-bit mono WAV for one script line.

| engine  | quality | licence | needs |
|---------|---------|---------|-------|
| kokoro  | natural | model Apache-2.0, kokoro-onnx MIT | `pip install kokoro-onnx`, ~310 MB model (88 MB int8) via `intelpod tts setup kokoro`; CPU is enough |
| piper   | good    | piper-tts GPL-3.0; each voice has its own licence (see its MODEL_CARD) | `pip install piper-tts`, voices via `intelpod tts setup piper` |
| windows | robotic | voices ship with Windows | Windows only; no download (PowerShell + System.Speech) |
| espeak  | robotic | espeak-ng GPL-3.0 | the `espeak-ng` program (Linux/macOS packages) |
| test    | tones   | -       | nothing; for automated tests only, never chosen by `auto` |
"""
from __future__ import annotations

import importlib.util
import logging
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from .wavtools import float_to_int16, write_wav

log = logging.getLogger(__name__)
AUTO_ORDER = ("kokoro", "piper", "windows", "espeak")


class EngineError(Exception):
    pass


class Engine:
    name = "base"
    licence = ""

    def __init__(self, cfg, models_dir: Path):
        self.cfg = cfg
        self.models_dir = models_dir

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def voices(self) -> list[str]:
        return []

    def synth(self, text: str, voice: str, speed: float, out: Path) -> None:
        raise NotImplementedError


class KokoroEngine(Engine):
    name = "kokoro"
    licence = "Kokoro-82M model: Apache-2.0; kokoro-onnx: MIT"

    def __init__(self, cfg, models_dir):
        super().__init__(cfg, models_dir)
        self._k = None

    @property
    def dir(self) -> Path:
        return self.models_dir / "kokoro"

    def available(self):
        if importlib.util.find_spec("kokoro_onnx") is None:
            return False, "kokoro-onnx is not installed (pip install kokoro-onnx)"
        model, voices = self.dir / self.cfg.kokoro_model, self.dir / self.cfg.kokoro_voices
        if not model.exists() or not voices.exists():
            return False, f"Kokoro model files missing in {self.dir} (run: intelpod tts setup kokoro)"
        return True, "ok"

    def _load(self):
        if self._k is None:
            from kokoro_onnx import Kokoro
            self._k = Kokoro(str(self.dir / self.cfg.kokoro_model), str(self.dir / self.cfg.kokoro_voices))
        return self._k

    def voices(self):
        try:
            return sorted(self._load().get_voices())
        except Exception:  # noqa: BLE001
            return []

    def synth(self, text, voice, speed, out):
        k = self._load()
        lang = "en-gb" if voice.startswith(("bf_", "bm_")) else "en-us"
        samples, rate = k.create(text, voice=voice, speed=speed, lang=lang)
        write_wav(out, float_to_int16(samples), rate)


class PiperEngine(Engine):
    name = "piper"
    licence = "piper-tts: GPL-3.0; voice licences vary (check each voice's MODEL_CARD)"

    def __init__(self, cfg, models_dir):
        super().__init__(cfg, models_dir)
        self._voices: dict = {}

    @property
    def dir(self) -> Path:
        return self.models_dir / "piper"

    def available(self):
        if importlib.util.find_spec("piper") is None:
            return False, "piper-tts is not installed (pip install piper-tts)"
        found = self.voices()
        if not found:
            return False, f"no Piper voices in {self.dir} (run: intelpod tts setup piper)"
        return True, "ok"

    def voices(self):
        return sorted(p.stem for p in self.dir.glob("*.onnx")) if self.dir.exists() else []

    def synth(self, text, voice, speed, out):
        from piper import PiperVoice, SynthesisConfig
        model = self.dir / f"{voice}.onnx"
        if not model.exists():
            raise EngineError(f"Piper voice {voice} not found in {self.dir}")
        if voice not in self._voices:
            self._voices[voice] = PiperVoice.load(model)
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            self._voices[voice].synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1.0 / speed))


class WindowsEngine(Engine):
    """Windows' built-in voices through PowerShell and System.Speech (no install, no download)."""
    name = "windows"
    licence = "voices included with Windows"

    def available(self):
        if sys.platform != "win32":
            return False, "only on Windows"
        if not shutil.which("powershell"):
            return False, "PowerShell not found"
        return True, "ok"

    def _ps(self, script: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=timeout)

    def voices(self):
        r = self._ps("Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                     ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }", timeout=60)
        return [v.strip() for v in r.stdout.splitlines() if v.strip()]

    def synth(self, text, voice, speed, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        txt = out.with_suffix(".txt")
        txt.write_text(text, encoding="utf-8")       # text passed by file: never interpolated into the command
        rate = max(-10, min(10, round((speed - 1.0) * 10)))
        q = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731 - PowerShell single-quoted literal
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.SelectVoice({q(voice)}); $s.Rate = {rate}; $s.SetOutputToWaveFile({q(out)}); "
                  f"$s.Speak([IO.File]::ReadAllText({q(txt)})); $s.Dispose()")
        r = self._ps(script)
        txt.unlink(missing_ok=True)
        if r.returncode != 0 or not out.exists():
            raise EngineError(f"Windows speech failed: {r.stderr.strip()[:200]}")


class EspeakEngine(Engine):
    name = "espeak"
    licence = "espeak-ng: GPL-3.0"

    def exe(self):
        return shutil.which("espeak-ng") or shutil.which("espeak")

    def available(self):
        return (True, "ok") if self.exe() else (False, "espeak-ng is not installed")

    def synth(self, text, voice, speed, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([self.exe(), "-v", voice, "-s", str(int(165 * speed)), "-w", str(out), "--", text],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise EngineError(f"espeak failed: {r.stderr.strip()[:200]}")


class ToneEngine(Engine):
    """Deterministic stand-in for tests: a soft tone per word, low or high pitch per voice."""
    name = "test"
    licence = "-"
    rate = 16000
    fail_texts: set[str] = set()

    def available(self):
        return True, "ok"

    def voices(self):
        return ["low", "high"]

    def synth(self, text, voice, speed, out):
        if any(t in text for t in self.fail_texts):
            raise EngineError("simulated synthesis failure")
        freq = 180.0 if voice == "low" else 260.0
        words = max(1, len(text.split()))
        n = int(self.rate * 0.36 * words / speed)
        vals = [0.3 * math.sin(2 * math.pi * freq * i / self.rate) * (0.6 + 0.4 * math.sin(i / 900)) for i in range(n)]
        write_wav(out, float_to_int16(vals), self.rate)


ENGINES = {"kokoro": KokoroEngine, "piper": PiperEngine, "windows": WindowsEngine, "espeak": EspeakEngine,
           "test": ToneEngine}


def choose(cfg, models_dir: Path) -> tuple[Engine | None, dict[str, str]]:
    """The configured engine (or the first available for `auto`) and why others were not used."""
    notes: dict[str, str] = {}
    names = AUTO_ORDER if cfg.engine == "auto" else (cfg.engine,)
    for name in names:
        eng = ENGINES[name](cfg, models_dir)
        ok, why = eng.available()
        if ok:
            return eng, notes
        notes[name] = why
    return None, notes
