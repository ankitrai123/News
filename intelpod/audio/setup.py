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


def piper_urls(voice: str) -> dict[str, str]:
    """Download URLs for a Piper voice, using piper-tts's own naming scheme."""
    from piper.download_voices import URL_FORMAT, VOICE_PATTERN
    m = VOICE_PATTERN.match(voice.strip())
    if not m:
        raise ValueError(f"'{voice}' is not a Piper voice name like en_US-lessac-medium")
    fam = m.group("lang_family")
    args = dict(lang_family=fam, lang_code=f"{fam}_{m.group('lang_region')}", voice_name=m.group("voice_name"),
                voice_quality=m.group("voice_quality"))
    return {ext: URL_FORMAT.format(extension=ext, **args) for ext in (".onnx", ".onnx.json")}


def setup_piper(models_dir: Path, voices: list[str], progress=print) -> list[Path]:
    """Each file is downloaded to a temporary name and only then moved into place, so an
    interrupted download never leaves a truncated voice that looks installed."""
    d = models_dir / "piper"
    out = []
    for v in voices:
        progress(f"  Piper voice {v}")
        for ext, url in piper_urls(v).items():
            dest = d / f"{v.strip()}{ext}"
            if dest.exists() and dest.stat().st_size > 0:
                progress(f"  {dest.name}: already present")
                continue
            _download(url, dest, progress)
        out.append(d / f"{v.strip()}.onnx")
    return out
