# Phase 6: local audio

The `audio` stage speaks the verified script (Phase 5) with two local voices, assembles the
episode and checks it. It runs after `script` and before `report`:

```
ingest → cluster → rank → review → research → debate → impact → script → audio → report
```

Nothing is sent anywhere: synthesis runs on your machine.

## Quick start (Windows, macOS, Linux)

```bash
pip install -e ".[audio]"          # Kokoro engine (kokoro-onnx + onnxruntime)
intelpod tts setup kokoro          # downloads the model (~310 MB; --small for the ~88 MB int8 model)
intelpod tts test                  # writes data/tts_test.wav: one sentence per host - listen to it
intelpod audio                     # speak the latest run's script
intelpod serve                     # Episode page: player, chapters, click a line to jump to it
```

No setup at all on Windows: if no other engine is installed, IntelPod uses Windows' built-in
voices (robotic, but always there). `intelpod tts list` shows what is available and why not.

## Engines (evaluated October 2026)

| Engine | Quality | Licence | Maintenance | Needs | CPU speed |
|---|---|---|---|---|---|
| **Kokoro** (default) | natural, several English voices (US and UK) | model Kokoro-82M: Apache-2.0; `kokoro-onnx`: MIT | kokoro-onnx 0.5 (Jan 2026) / 0.6 | `pip install kokoro-onnx` (pulls onnxruntime, espeak-ng loader); model files via `intelpod tts setup kokoro` | 82M parameters: typically faster than real time on a modern laptop CPU; no GPU needed |
| **Piper** | good, many voices | `piper-tts`: **GPL-3.0** (Open Home Foundation, after the original MIT repo was archived in Oct 2025); **each voice has its own licence** — read its MODEL_CARD on huggingface.co/rhasspy/piper-voices before publishing | piper-tts 1.8 (2026) | `pip install piper-tts`; voices via `intelpod tts setup piper en_US-amy-medium en_US-ryan-medium` | very fast on CPU, runs on Raspberry Pi-class hardware |
| **Windows** | robotic | voices included with Windows | part of Windows | Windows (PowerShell + System.Speech), no download | instant |
| **espeak-ng** | robotic | GPL-3.0 | active | the `espeak-ng` program (Linux/macOS packages) | instant |

Selection: `audio.engine: auto` tries Kokoro, Piper, Windows, espeak-ng in that order and uses
the first available; set an engine name to force one. Both hosts must use **different voices**
(`audio.voices`); IntelPod warns if they are the same.

Hardware and dependency notes:
* Kokoro and Piper need `onnxruntime`. If your Python version has no onnxruntime wheel yet
  (very new Python releases sometimes lag), use Python 3.12/3.13 for the virtual environment, or
  use the Windows / espeak-ng engines.
* Models are stored in `data/models/tts/` (Kokoro ~340 MB with voices; each Piper voice 60-75 MB).
* The build environment could not download voice models (network policy), so real voices were
  not heard during development: the engine adapters are tested against their documented APIs,
  and the pipeline with a deterministic test engine. Run `intelpod tts test` first.

## How an episode is rendered

1. **Text for speech.** Each line's text with the script's pronunciation respellings applied
   (`apply_pronunciations`), and symbols spoken as words: `0.25%` → "0.25 percent", `₹1,200 crore`
   → "1200 crore rupees", `$5 billion` → "5 billion dollars", `25 bps` → "25 basis points", `&` → "and";
   quotation marks and citation markers are not spoken. The meaning is never changed.
2. **One file per line**, voice by speaker (A anchor, B analyst), cached under
   `data/runs/<run>/audio/lines/` with a key made from engine, voice, speed and text. Re-running
   renders only lines that are new, changed or failed; `--redo L3,L7` forces specific lines.
3. **Retries.** A failing line is retried `max_retries` times. If any line still fails, the
   episode is **not assembled** (a missing line could drop a hedge or a correction) and is marked
   incomplete; the next `intelpod audio` retries only the missing lines.
4. **Assembly** in pure Python (no FFmpeg needed): pauses between lines (`line_pause_ms`) and
   longer ones between segments (`segment_pause_ms`), optional intro/outro (WAV; other formats need
   FFmpeg), level normalisation. Chapters are recorded at each segment.
5. **Conversion.** With FFmpeg installed: an MP3 with loudness normalised to −16 LUFS (podcast
   standard) and the episode title as metadata. Without FFmpeg: WAV only, and the episode says so.
6. **Checks.**

   | Check | Rule | On failure |
   |---|---|---|
   | all lines rendered | every script line has audio | incomplete, not assembled |
   | final file readable | the WAV re-reads with the expected rate and length | failed |
   | not silent | RMS level above 0.005 | failed |
   | MP3 duration matches | ffprobe duration within 2% (or 1 s) of the WAV | failed |
   | duration plausible | within 60-160% of the estimate from word count and speed | warning |
   | speech rate per line | 0.12-1.2 s per word | warning (lists the lines) |
   | no clipping | under 0.1% of samples at full scale | warning |

Safeguards:
* **Each line is validated** before it is cached: a file that is empty, unreadable, shorter than
  0.2 s or near-silent counts as a failure (it is retried, never cached). Lines are written to a
  temporary file and moved into place, so an interrupted run never leaves a half-written line.
* A **failed `--redo`** keeps the earlier recording of that line.
* **Missing voices** stop the render with a clear message (e.g. a Windows voice that is not
  installed, a Piper voice without its `.onnx.json`) instead of silently using a default voice.
* **Engine load errors** (onnxruntime DLL problems, a corrupt model) are reported per line with a
  hint to re-run `intelpod tts setup`.
* **Stale audio.** The audio records a fingerprint of the script it spoke. If the script is
  rewritten later, the Episode page and the report stop using the old audio and timings and say
  to run `intelpod audio` again (unchanged lines come from the cache).
* Model downloads (`intelpod tts setup`) go to a `.part` file first, so a broken download is
  never mistaken for a model.

## Output

In `data/runs/<run>/`:
* `audio/episode.wav` (and `audio/episode.mp3` with FFmpeg) — the episode;
* `episode.json` — metadata: title, duration, files, hosts and voices, engine and its licence,
  stories, chapters, per-line timings, checks, limitations;
* `transcript.json` — every line with speaker, sources and its start time in the audio;
* `script.md`, `episode_notes.md` (sources), `evidence_report.md` — from Phase 5.

Dashboard: the **Episode** page has a player, chapter buttons, the check results, and every
script line is clickable to jump to it. API: `GET /api/runs/{run_id}/audio`; audio files are served
at `/media/{run_id}/episode.mp3|wav` (only those two names).

## Configuration (`audio:`)

See `config/config.example.yaml`: engine, speed, voices per engine and host, Kokoro model file,
pauses, intro/outro, formats, MP3 bitrate, loudness target, retries.

## Licences and publishing

Audio made with Kokoro is fine to publish under the Apache-2.0 model licence. For Piper, check the
licence of the specific voice. NVIDIA's free tier (if it wrote the script) is for evaluation, not
production: see docs/PRIVACY.md. The episode notes say how the episode was made.
