"""Pure-Python WAV handling (16-bit PCM mono): read, write, silence, resample, concatenate,
level statistics. Works without FFmpeg or NumPy; uses NumPy when installed (faster)."""
from __future__ import annotations

import array
import math
import shutil
import subprocess
import wave
from pathlib import Path

try:  # optional speed-up
    import numpy as np
except ImportError:  # pragma: no cover - numpy is present whenever kokoro/piper are
    np = None


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def read_wav(path: Path) -> tuple[array.array, int]:
    """Samples as int16 array (mono; stereo is down-mixed) and the sample rate."""
    with wave.open(str(path), "rb") as w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError(f"{path.name}: {8 * width}-bit audio; 16-bit PCM expected")
    samples = array.array("h")
    samples.frombytes(raw)
    if ch == 2:
        samples = array.array("h", ((samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)))
    elif ch != 1:
        raise ValueError(f"{path.name}: {ch} channels not supported")
    return samples, rate


def write_wav(path: Path, samples: array.array, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def float_to_int16(values) -> array.array:
    if np is not None:
        a = np.clip(np.asarray(values, dtype=np.float32), -1.0, 1.0)
        return array.array("h", (a * 32767).astype(np.int16).tobytes())
    return array.array("h", (int(max(-1.0, min(1.0, v)) * 32767) for v in values))


def silence(seconds: float, rate: int) -> array.array:
    return array.array("h", bytes(2 * int(seconds * rate)))


def resample(samples: array.array, src: int, dst: int) -> array.array:
    """Linear-interpolation resampling (adequate for speech; FFmpeg is not required)."""
    if src == dst or not samples:
        return samples
    n_out = int(len(samples) * dst / src)
    if np is not None:
        x = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float32)
        out = np.interp(np.arange(n_out) * (src / dst), np.arange(len(x)), x)
        return array.array("h", out.astype(np.int16).tobytes())
    out = array.array("h")
    step = src / dst
    last = len(samples) - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = samples[min(j, last)]
        b = samples[min(j + 1, last)]
        out.append(int(a + (b - a) * frac))
    return out


def stats(samples: array.array) -> dict:
    """Peak and RMS (0..1) and the share of clipped samples."""
    if not samples:
        return {"peak": 0.0, "rms": 0.0, "clipped": 0.0}
    if np is not None:
        x = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0
        return {"peak": float(np.max(np.abs(x))), "rms": float(np.sqrt(np.mean(x * x))),
                "clipped": float(np.mean(np.abs(x) >= 0.999))}
    peak = max(abs(s) for s in samples) / 32768.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    clipped = sum(1 for s in samples if abs(s) >= 32735) / len(samples)
    return {"peak": peak, "rms": rms, "clipped": clipped}


def scale(samples: array.array, factor: float) -> array.array:
    if abs(factor - 1.0) < 1e-3:
        return samples
    if np is not None:
        x = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float32) * factor
        return array.array("h", np.clip(x, -32768, 32767).astype(np.int16).tobytes())
    return array.array("h", (max(-32768, min(32767, int(s * factor))) for s in samples))


def convert_with_ffmpeg(src: Path, dst: Path, bitrate: str, lufs: float, title: str) -> str | None:
    """Encode (e.g. to MP3) with loudness normalisation. Returns an error message, or None."""
    exe = ffmpeg()
    if exe is None:
        return "FFmpeg is not installed"
    cmd = [exe, "-y", "-loglevel", "error", "-i", str(src), "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11",
           "-ar", "44100", "-ac", "1", "-b:a", bitrate, "-metadata", f"title={title}", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"FFmpeg failed: {e}"
    return None if r.returncode == 0 and dst.exists() else f"FFmpeg failed: {r.stderr.strip()[:300]}"


def decode_with_ffmpeg(src: Path, dst_wav: Path, rate: int) -> str | None:
    exe = ffmpeg()
    if exe is None:
        return "FFmpeg is not installed (needed for non-WAV intro/outro files)"
    r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(src), "-ar", str(rate), "-ac", "1",
                        "-sample_fmt", "s16", str(dst_wav)], capture_output=True, text=True, timeout=300)
    return None if r.returncode == 0 else f"FFmpeg could not decode {src.name}: {r.stderr.strip()[:200]}"


def probe_duration(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
