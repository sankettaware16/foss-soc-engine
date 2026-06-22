# CLAUDE.md — Project Memory for the FOSS SOC Engine

> **Purpose of this file.** Any Claude (any model, account, or interface) should read
> this file FIRST to get the full picture of this repo without re-deriving it from
> scratch. It is the stable, durable context. The chronological "what happened and
> why" lives in **[journal.md](journal.md)**.

---

## ⚠️ MAINTENANCE PROTOCOL — READ AND FOLLOW EVERY TURN

1. **At the start of a session/turn:** read this file, then read [journal.md](journal.md)
   (newest entries are at the top) to learn the latest state and recent decisions.
2. **At the END of EVERY chat turn where you did or decided anything:** prepend a new
   dated entry to the **Entries** section of [journal.md](journal.md). One entry per
   turn. Record: what was asked, what you changed (files), key decisions + the *why*,
   and anything left open. This is non-negotiable — it is how the next model keeps
   continuity.
3. **When architecture, constraints, key files, or policies change:** also update the
   relevant section of THIS file (CLAUDE.md), so it stays the accurate "current truth".
4. Keep both files honest. If something was tried and reverted, say so. If a test
   failed, record it. Do not overstate "done".

---

## What this project is

A **custom SIEM log-parsing engine**. It consumes raw logs from **Kafka** topics,
routes each log to the right parser by `source_program`, parses it with a YAML-defined
rule, normalizes the result to **ECS** (Elastic Common Schema) JSON, and writes
NDJSON files (one per module) for downstream SIEM agents (Filebeat/Wazuh) to ship.

Pipeline:  `rsyslog (imfile→omkafka)` → `Kafka topic` → **this engine** → `ECS NDJSON files`.

## Hard constraints (non-negotiable — they drive every design choice)

- **Throughput: must handle 1M+ EPS** with all rules active and 3+ topics. Must never
  be the bottleneck.
- **No GPU, ever.** Pure regex/JSON/dict work.
- **RAM must not spike.** Keep processing self-throttling / bounded batches; prefer
  backpressure over buffering.
- **Must stay simple for non-technical operators**: easy install, easy config edits,
  easy debug, easy to add custom parsers. Favor solutions that keep the YAML-rule
  experience and the one-command deploy intact over clever rewrites. A
  faster-but-complex design is a regression to the owner.

## Architecture & data flow

`main.py` is a **supervisor** that forks N **worker processes** (default = all CPU
cores). Each worker independently joins the **same Kafka consumer group**, so Kafka
auto-balances topic partitions across them (this is how it scales — to more cores, or
to more machines using the same `group_id`). Each worker runs the original
single-threaded loop: poll → for each message `LogInput(envelope)` → route via
`program_mapping` → `UniversalEngine.process()` → batch → write to disk → commit Kafka
offset. Offsets commit **only after** the batch is flushed (at-least-once). SIGTERM
drains cleanly.

### The 5 parsing strategies (set `strategy:` in a rule)
- `stateless` — one regex, single-line logs (apache/nginx access).
- `multi_match` — several regexes, first match wins (linux auth: ssh/sudo/su).
- `stateful` — multi-line events correlated by a transaction id via **Redis** (postfix).
- `json_map` — dot-path mapping of JSON logs; `*` walks lists (modsec, fim).
- `xml_xpath` — element/attribute mapping of XML (openvas, nessus).

## Repository map (key files)

| File | Role |
|---|---|
| `main.py` | Supervisor + worker loop: multiprocessing, at-least-once commit, graceful shutdown, per-worker output/stats files, stats aggregation. **Also writes `logs/engine.pid` heartbeat + per-worker `stats[.wN].json` every `metrics_interval_sec` (default 2s) for the Web UI Monitor** |
| `core/engine.py` | `UniversalEngine`: the 5 strategies, GeoIP enrich, **cached 1s `@timestamp`**, `fastjson` |
| `core/registry.py` | Loads `rules/*.yaml`, `program_map` routing, 10s mtime watcher (hot-reload) |
| `core/schema.py` | `LogInput`: parses the `{"meta":{...},"raw":...}` envelope (via `fastjson`, accepts bytes) |
| `core/output.py` | `OutputWriter` (keep-open handles, orjson bytes, per-worker `module.wN.json`, optional rotation) + `DlqWriter` |
| `core/ecs_schema.py` | ECS field database + `classify/suggest/search` + `ALIASES` autocorrect map |
| `utils/geoip.py` | `GeoIPClient` singleton + per-process **LRU cache** |
| `utils/fastjson.py` | `orjson` with stdlib `json` fallback (orjson never hard-required) |
| `ecs_helper.py` | ECS "autocorrect" CLI for rule authors: `check` / `fix` / `find` / interactive |
| `test_config.py` | **Static** validator: config shape, paths, rules+regex, program_mapping, ECS compliance |
| `preflight.py` | **Live** pre-run validator: static checks + TCP reachability + Kafka broker/topics/partitions + Redis |
| `replicate.py` | Dry-run the WHOLE rsyslog→Kafka→engine pipeline locally (no Kafka) from an rsyslog conf + sample logs; auto-suggests the right rule |
| `test_file.py` / `test_rules.py` | Parser testers (file-based / interactive) |
| `config.yaml` | `kafka`, `batch`, `runtime.workers`, `output`, `paths`, `program_mapping`, `geoip` |
| `rules/*.yaml` | Parser rules: apache, nginx, auth(linux_auth), postfix, modsec, nessus, openvas, roundcube, suricata, fim |
| `examples/` | `rsyslog_sample.conf` + `samples/*` — ready-to-run input for `replicate.py` |
| `webui/app.py` | **Flask Web UI** server: wraps every *local* capability (test log/parser, rule CRUD, config edit+validate, ECS helper, preflight) **+ a live Monitor** (`/api/monitor` reads `logs/engine.pid`+stats, host CPU/RAM via psutil-or-`/proc`-or-ctypes; `/api/engine/<action>` systemd control gated by `SOC_UI_ALLOW_CONTROL`) as REST endpoints; reuses the real engine + `test_config`/`preflight` (captures their stdout). Runs on **Flask + PyYAML only**; `sys.frozen`/`_MEIPASS` path split for the exe |
| `webui/templates/`, `webui/static/` | Liquid-glass UI (offline, no CDN): `index.html`, `css/style.css`, `js/app.js` |
| `webui/requirements-ui.txt` | Minimal UI deps (Flask, PyYAML — wheels on all OSes) |
| `webui/Start-SOC-UI.bat`, `webui/start-soc-ui.sh` | Auto-venv launchers (need Python) |
| `webui/foss-soc-ui.spec`, `webui/build_exe.py` | PyInstaller one-file build → `release/FOSS-SOC-UI/` (exe + editable rules/config) |
| `WEB_UI_GUIDE.md` | Non-technical **browser usage guide** for the Web UI |
| `WRITING_RULES.md` | Beginner rule-authoring guide + **master AI prompt** to generate rules from raw logs |
| `README.md` | Full user documentation |

## How to run / test / validate

```bash
# Run (foreground / debug)
sudo python3 main.py
# Run (production, systemd)
sudo ./setup_service.sh && sudo systemctl start foss-soc

# Validate BEFORE starting
python3 preflight.py                 # full live check (config + Kafka/Redis/topics/network)
python3 preflight.py --skip-live     # static only
python3 test_config.py --skip-kafka  # static config/rules/ECS only

# Dry-run the whole pipeline locally, no Kafka
python3 replicate.py --rsyslog examples/rsyslog_sample.conf --logs-dir examples/samples

# ECS field help while writing rules
python3 ecs_helper.py check rules/<rule>.yaml
python3 ecs_helper.py fix   rules/<rule>.yaml
python3 ecs_helper.py find  "http status"

# Test parsing of a log file
python3 test_file.py <logfile> <parser|AUTO>

# --- Web UI (browser console for all the LOCAL tools above) ---
python3 webui/app.py                 # dev run -> http://127.0.0.1:8600
# Windows launcher (auto-venv):      webui\Start-SOC-UI.bat
# Linux/macOS launcher:              ./webui/start-soc-ui.sh
# Build the standalone desktop app:  python webui/build_exe.py
#   -> release/FOSS-SOC-UI/FOSS-SOC-UI.exe  (download-and-run, no Python needed)
# Env: SOC_UI_PORT (8600), SOC_UI_HOST (127.0.0.1), SOC_UI_NO_BROWSER=1
```

Workers: `runtime.workers: auto` in config.yaml (= all cores), or `SOC_WORKERS=N` env.
**Kafka topics must have ≥ as many partitions as total workers**, or extras sit idle.

## Key decisions & policies (the "why")

- **Never change the engine's "core definition"** — the 5 strategies, the YAML rule
  format, and the one-command deploy must stay intact. Optimizations are internal only.
- **Optional-deps policy (for the Web UI / portable builds):** `redis` (core/engine.py)
  and `geoip2` (utils/geoip.py) are imported **lazily** — absent = `r=None` / geo skipped,
  never an import crash. So the engine + Web UI run on **Flask + PyYAML only**. Behavior is
  identical when the libs ARE installed. orjson was already optional via `utils/fastjson`.
  Keep new heavy deps optional the same way so plug-and-play installs never error.
- **Web UI = local tools only, never a second engine.** `webui/app.py` reuses the real
  `UniversalEngine`, `test_config`, `preflight`, and `ecs_schema` (capturing their stdout
  into JSON) so the UI and CLI can never diverge. It does NOT run Kafka/workers — that's
  still `main.py`. UI is single-user/local (no auth); bind to 0.0.0.0 only on trusted LANs.
- **Monitor reads what `main.py` writes; it never invents metrics.** `main.py` writes
  `logs/engine.pid` (role/pids/start_time/kafka) + `stats[.wN].json` (eps/totals/errors/
  per-rule `parser_stats`) every `runtime.metrics_interval_sec` (default 2s; the human
  log line is still throttled to 60s). The Monitor self-aggregates per-worker files each
  2s poll for real-time EPS, picks files by the heartbeat's worker count (ignores leftover
  `stats.wN.json` from a prior run), drops individually-stale files, and checks PID
  liveness. UI/engine must share the same `logs/` (override via `SOC_LOG_DIR`). Engine
  start/stop/restart is **off by default** — needs `SOC_UI_ALLOW_CONTROL=1` + systemd.
- **ECS policy:** every rule field must be ECS *where ECS has a field*; **custom fields
  are kept where ECS has none**. The validator ERRORs on a known-wrong/typo field
  (with the fix shown) and ALLOWS genuine custom fields. ~70 custom fields are kept
  intentionally (e.g. `email.dsn`, `nginx.*`, `vulnerability.cve`, `authentication.method`).
- **4 fields were migrated to real ECS:** `email.from`→`email.from.address`,
  `email.to`→`email.to.address`, `file.owner_uid`→`file.uid`,
  `vulnerability.cvss.base_score`→`vulnerability.score.base`.
- **Delivery = at-least-once:** `enable_auto_commit=False`; commit after disk flush.
  (v1/v2 used auto-commit = at-most-once = silent loss on crash.)
- **Scaling = static parallelism**, not load-reactive: one worker per core at startup,
  more machines via the same `group_id`. Not auto-discovered across machines.
- **Real-time start:** `auto_offset_reset: "latest"` means a **fresh `group_id`** starts
  at newest and skips backlog. A group with committed offsets resumes (processes the
  gap). To force real-time again, use a NEW `group_id`.
- **Redis** host is currently hardcoded `localhost:6379` in `core/engine.py`; only the
  `stateful` strategy needs it. (Candidate to make configurable.)
- **orjson is optional** — `utils/fastjson.py` falls back to stdlib `json` if absent.

## Performance facts (measured on an i5-13400, this repo)

- Per-core effective: **~48k EPS (v1/v2)** → **~97k EPS (v3)** after orjson + cached
  timestamp + cached geoip. Per-stage: envelope parse 1.53→0.61µs; output serialize
  4.91→0.72µs.
- Uses **1 core (v1/v2) → all cores (v3)**. Aggregate on a 12-thread box: ~50k EPS →
  **~0.5–1M EPS**. Reaching a clean 1M on one box may want `confluent-kafka` (C client)
  instead of `kafka-python-ng`.

## Known caveats / open ideas (future plug-and-play; not yet built)

Pluggable output sinks (OpenSearch/Kafka re-emit/S3); runtime auto-parser-detection
(no `program_mapping` needed); Docker-compose bundle; auto-create Kafka topics/partitions;
built-in syslog/CEF/LEEF parsers; make Redis configurable; `confluent-kafka`; HTTP
`/metrics` + status CLI; DLQ-driven "unparsed sources" report. See journal for context.

---

*Keep this file current. Append to [journal.md](journal.md) every turn.*
