"""Download TTS model files on request (`intelpod tts setup ...`). Nothing is downloaded silently."""
from __future__ import annotations

from pathlib import Path

import httpx

KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = {"full": "kokoro-v1.0.onnx", "int8": "kokoro-v1.0.int8.onnx", "voices": "voices-v1.0.bin"}


def _download(url: str, dest: Path, progress=print) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done, last = 0, -10
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                pct = int(100 * done / total) if total else 0
                if total and pct >= last + 10:
                    progress(f"  {dest.name}: {pct}% of {total / 1e6:.0f} MB")
                    last = pct
    tmp.replace(dest)


def setup_kokoro(models_dir: Path, small: bool = False, progress=print) -> Path:
    d = models_dir / "kokoro"
    model = KOKORO_FILES["int8" if small else "full"]
    for name in (model, KOKORO_FILES["voices"]):
        if (d / name).exists():
            progress(f"  {name}: already present")
            continue
        _download(KOKORO_BASE + name, d / name, progress)
    return d / model


def setup_piper(models_dir: Path, voices: list[str], progress=print) -> list[Path]:
    from piper.download_voices import download_voice
    d = models_dir / "piper"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for v in voices:
        progress(f"  downloading Piper voice {v}")
        download_voice(v, d)
        out.append(d / f"{v}.onnx")
    return out
