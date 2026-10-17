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
| Embeddings `unavailable` | Ollama not running or model not pulled | `ollama serve`, `ollama pull nomic-embed-text` |
| `database is locked` | two processes writing at once | let the running job finish; SQLite WAL mode and a 5 s busy timeout are already set |

## Resetting

Delete `data/` to start from an empty database (this removes all history).
