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
| `run … is still marked running` | an earlier run was killed | `intelpod run --resume`, or `intelpod run --force` to start fresh |
| `no articles in the lookback window` | ingestion has never succeeded, or `lookback_hours` too small | run ingestion; increase `--lookback-hours` |
| Too many tiny one-article events | thresholds too strict for your feed mix | lower `clustering.similarity_threshold` (e.g. 0.26) or enable embeddings |
| Unrelated stories merged | thresholds too loose | raise `similarity_threshold` / `merge_threshold`, or lower `entity_weight` |
| Selection dominated by one topic | many strong events in one area | lower `ranking.max_per_topic`; adjust weights |
| Few/no events selected | quiet day, few working feeds, or `min_score` too high | check feed health; lower `ranking.min_score`; `intelpod events --all` shows everything scored |
| Ollama embeddings `unavailable` | Ollama not running or model not pulled | `ollama serve`, `ollama pull nomic-embed-text` |
| NewsData `NEWSDATA_API_KEY not set` in the run summary | no key | add it to `.env`; NewsData is optional |
| NewsData `daily quota exhausted` / `rate limited` | 200 credits/day or 30 per 15 min used | wait (the quota reset time isn't documented); lower `max_credits_per_run` or the number of queries |
| NewsData `key rejected (401)` | wrong key, or free key lapsed after ~30 days unused | regenerate the key on newsdata.io |
| NewsData articles look a day old | free plan delays articles ~12 h | expected; RSS/GDELT supply the fresh news |
| Review `NVIDIA_API_KEY is not set` | no key | add it to `.env`, or set `llm.provider: ollama`, or run with `--no-llm` |
| NVIDIA 403 "Authorization failed" but `intelpod llm models` works | key lacks the "Public API Endpoints" entitlement | create a key with that scope at build.nvidia.com/settings/api-keys; if it isn't offered, ask help@build.nvidia.com |
| `no configured model … is available` | every model in `llm.tasks.event_review.models` returned 404/410 (retired) | `intelpod llm models --filter nemotron`, try one with `intelpod llm test --model <id>`, update the list |
| `rate limited by nvidia after retries` | free trial rate limit (varies; can be below 40/min) | lower `llm.providers.nvidia.requests_per_minute` or `llm.event_review.max_events`; rerun later — cached answers are reused |
| `spent its output on reasoning` | a reasoning model used `max_tokens` on thinking | disable thinking for that model (`extra_body`) or raise its `max_tokens` |
| Model titles often "rejected" | model adds names/numbers not in the headlines | expected safety behaviour; the publisher headline is used instead |
| Embeddings `unavailable` | key missing, model retired, or rate limit | clustering falls back to lexical similarity automatically; see the reason in `intelpod runs` / the run's cluster output |
| `database is locked` | two processes writing at once | let the running job finish; SQLite WAL mode and a 5 s busy timeout are already set |

## Resetting

Delete `data/` to start from an empty database (this removes all history).
