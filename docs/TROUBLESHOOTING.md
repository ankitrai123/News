# Troubleshooting

Start with `intelpod doctor` (environment and reachability) and `intelpod runs` (run and stage
status). The dashboard's **Runs & status** page lists recent source failures. Logs are written
to `data/logs/intelpod.log` (set `app.log_json: true` for JSON lines).

| Symptom | Likely cause | Fix |
|---|---|---|
| `Run failed: every provider request failed` | no internet, firewall/proxy blocking news hosts, or all sources down | `intelpod doctor`; check proxy settings (`HTTPS_PROXY` is honoured); try `intelpod feeds check` |
| `ProxyError: 403` in fetch log | a proxy or network policy blocks that host | allow the host in your proxy/firewall; IntelPod does not retry these |
| a feed shows `FAIL` / `HTTP 404` in `feeds check` | the publisher moved or removed the feed | find the current feed URL on the publisher's site or set `enabled: false` |
| `unparseable feed` | URL returns an HTML page, not RSS | wrong URL; fix in `config/feeds.yaml` |
| `robots.txt disallows …` | the publisher forbids automated access to that path | disable that feed; do not turn off `respect_robots_txt` to get around it |
| GDELT `non-JSON response: 'Please limit requests…'` | requests too frequent | raise `ingestion.providers.gdelt.min_interval_seconds`; reduce the number of queries |
| GDELT returns 0 articles | query too narrow, or no matching coverage in `timespan` | widen the query or timespan (e.g. `3d`) |
| `run … is in progress in another process` | another `intelpod run` or the dashboard is running one | wait for it. A run whose process was killed or crashed is detected automatically (marked failed, "interrupted") and can be resumed with `intelpod run --resume` or the dashboard's Resume button; `--force` overrides the check if you are sure nothing is running |
| `no articles in the lookback window` | ingestion has never succeeded, or `lookback_hours` too small | run ingestion; increase `--lookback-hours` |
| Too many tiny one-article events / one story split into many events | headlines word the story differently; no embeddings | set `NVIDIA_API_KEY` (or use Ollama) so embeddings run — the briefing shows `Embeddings: used (…)`; check `clustering.country_tokens: canonical` (older `config.yaml` files lack it but get it by default); then lower `clustering.similarity_threshold` (e.g. 0.27) |
| Unrelated stories merged | thresholds too loose | raise `similarity_threshold` / `merge_threshold`, or lower `entity_weight` / `country_token_weight` |
| Same story in several briefing slots | the story was split into several events | see "one story split" above; `ranking.duplicate_similarity` (default 0.45) skips the clearest repeats — lower it (e.g. 0.35) to skip more, at the risk of dropping a related but distinct story |
| Selection dominated by one topic | many strong events in one area | lower `ranking.max_per_topic`; adjust weights |
| Few/no events selected | quiet day, few working feeds, or `min_score` too high | check feed health; lower `ranking.min_score`; `intelpod events --all` shows everything scored |
| Ollama embeddings `unavailable` | Ollama not running or model not pulled | `ollama serve`, `ollama pull nomic-embed-text` |
| NewsData `NEWSDATA_API_KEY not set` in the run summary | no key | add it to `.env`; NewsData is optional |
| NewsData `daily quota exhausted` / `rate limited` | 200 credits/day or 30 per 15 min used | wait (the quota reset time isn't documented); lower `max_credits_per_run` or the number of queries |
| NewsData `key rejected (401)` | wrong key, or free key lapsed after ~30 days unused | regenerate the key on newsdata.io |
| NewsData articles look a day old | free plan delays articles ~12 h | expected; RSS/GDELT supply the fresh news |
| Review `NVIDIA_API_KEY is not set` | no key | add it to `.env`, or set `llm.provider: ollama`, or run with `--no-llm` |
| NVIDIA 403 "Authorization failed" but `intelpod llm models` works | key lacks the "Public API Endpoints" entitlement | create a key with that scope at build.nvidia.com/settings/api-keys; if it isn't offered, ask help@build.nvidia.com |
| `no configured model … is available` | every model in `llm.tasks.event_review.models` failed: 410 = retired; 404 "not found for account" = retired **or** your key lacks the "Public API Endpoints" entitlement; 5xx/timeouts = service trouble | if `intelpod llm models` still lists them and all answered 404, it is the entitlement (help@build.nvidia.com); otherwise try ids with `intelpod llm test --model <id>` and update the list, or rerun later |
| `rate limited by nvidia after retries` | free trial rate limit (varies; can be below 40/min) | lower `llm.providers.nvidia.requests_per_minute` or `llm.event_review.max_events`; rerun later — cached answers are reused |
| `spent its output on reasoning` | a reasoning model used `max_tokens` on thinking | disable thinking for that model (`extra_body`) or raise its `max_tokens` |
| Model titles often "rejected" | model adds names/numbers not in the headlines | expected safety behaviour; the publisher headline is used instead |
| Embeddings `unavailable` | key missing, model retired, or rate limit | clustering falls back to lexical similarity automatically; see the reason in `intelpod runs` / the run's cluster output |
| Dashboard says "cross-origin request refused" / "Invalid host header" | request came from another site, or you browse via a name other than 127.0.0.1/localhost | open it at http://127.0.0.1:8000; when serving on another host add `--allow-host <name>` |
| Model "outlier" verdict ignored (`rejected.outlier_ids`) | model tried to remove half or more of an event's reports | safety cap against manipulated headlines; the event is kept whole |
| Research package says "Only N relevant sources found" | little coverage in the window, GDELT refused requests, or pages blocked/paywalled | expected on some stories; check the "Searches run" list on the event page; add a Guardian key; add research feeds; widen `research.window_days` (≤ 21 recommended) |
| Research: `GDELT kept refusing requests` | GDELT rate limit (shared with ingestion) | research continues with the other providers; rerun later (`intelpod research --refresh`), or lower `research.gdelt.max_requests_per_run` |
| Research: `query not sent (invalid for GDELT)` | a generated query broke GDELT's rules (too long, 2-letter word…) | the query is skipped, never sent; report it with the event title so the template can be improved |
| Research: many sources "metadata only" | publishers opt out of text use, PDFs, or `no_extract_domains` | by design; the headline, date and link are still used |
| `intelpod research` says "has no ranked events yet" | Phase 1 has not finished for that run | `intelpod run --resume` (or `--stop-after review`) first |
| Debate: "No model available … claim map is complete" (status `partial`) | no model configured, `--no-llm`, or the provider failed | expected; the claim map, diversity and searches still run. Configure NVIDIA or Ollama and run `intelpod debate --refresh` |
| Debate: "No meaningful disagreement was found" | the sources agree, or only one side is evidenced | by design: IntelPod does not manufacture a debate. Check "Who is represented" for coverage gaps |
| Debate: many "rejected" model items | the model named people/numbers not in the cited sources | expected safety behaviour; the rest of the package is kept |
| Debate: "Looked for … no relevant, accessible source was found" | the perspective is not covered by the archive, feeds, GDELT or Guardian (or GDELT's budget ran out) | add research feeds for that country/stakeholder, a Guardian key, or raise `debate.gdelt_requests_per_run` |
| `intelpod debate` says "has not been researched yet" | Phase 2 has not run for that run | `intelpod research --run-id <id>` first |
| Impact: "Causal chains … were not assessed" (status `partial`) | no model, `--no-llm`, or budget used | expected; evidence tables are complete. Configure a model and run `intelpod impact --refresh` |
| Impact: an effect chain or group was "rejected" | it named a person, company or number not in the statements it cites | expected safety behaviour; see "Model output rejected" on the event page |
| Impact: "No retrieved source discusses India" | no statement mentions India/RBI/rupee etc. | by design: India implications are not invented. Add Indian research feeds or a Guardian key for wider coverage |
| Few or no "Connections between events" | no source of one event mentions another | by design: events are not linked on timing alone |
| `intelpod impact` says "has no completed rank stage" | Phase 1 not finished for that run | `intelpod run --resume` first |
| "GDELT skipped until HH:MM UTC" | GDELT refused every request in a recent run; IntelPod waits 30 min (doubling up to 6 h) before asking again, so it does not extend GDELT's block | nothing to do: RSS, NewsData and the Guardian keep working. Shared networks (campus or office Wi-Fi, VPNs) are often rate-limited by GDELT; a home connection usually works better |
| Script: "Script is N words, short of the target" | not enough verified material (few stories passed Phases 3–4) | by design: IntelPod does not pad. More sources (Guardian key, research feeds) give more material |
| Script: many lines removed | the model named people/numbers not in the fact sheet, or stated forecasts as fact | see `evidence_report.md`; try another model (`llm.tasks.script_writing.models`) |
| Script: "template" instead of dialogue | no model, `--no-llm`, or the model failed | configure NVIDIA or Ollama and run `intelpod script --refresh` |
| Audio: "no text-to-speech engine available" | no engine set up | `intelpod tts list` shows why each is unavailable; `pip install -e ".[audio]"` then `intelpod tts setup kokoro` |
| `pip install -e ".[audio]"` fails on onnxruntime | no onnxruntime wheel for your Python version | create the venv with Python 3.12/3.13, or use the Windows voices (no install) |
| Audio "incomplete" | a line failed to synthesise after retries | run `intelpod audio` again: only missing lines are rendered; see the failed line ids |
| Only WAV, no MP3 | FFmpeg not installed | install FFmpeg (winget install ffmpeg / brew install ffmpeg / apt install ffmpeg) and run `intelpod audio` again (lines come from the cache) |
| A name is mispronounced | the engine guessed | add or fix a respelling in the script's pronunciation notes, or turn off `audio.apply_pronunciations` if a suggestion is wrong; `intelpod audio --redo L5` |
| `database is locked` | two processes writing at once | let the running job finish; SQLite WAL mode and a 5 s busy timeout are already set |

## Resetting

Delete `data/` to start from an empty database (this removes all history).
