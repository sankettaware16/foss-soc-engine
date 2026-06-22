# journal.md — Project Diary

> **How to use this file.** This is the chronological memory of the project: what was
> done, what was decided, and *why*. Read [CLAUDE.md](CLAUDE.md) first for stable
> context, then read the latest entries here.
>
> **Every chat turn that changes or decides anything, prepend a new entry at the TOP
> of the "Entries" section** using the template below. Newest entries stay on top.
> Keep it honest (record reverts, failures, open questions).
>
> ```
> ## YYYY-MM-DD — <short title>
> **Asked:** <what the user wanted this turn>
> **Did:** <files created/changed, commands run>
> **Decided:** <key decisions + why>
> **Verified:** <how it was tested / results>
> **Open:** <anything left for next time>
> ```

---

## Entries

## 2026-06-20 — README Web UI section + removed AI co-author trailers from commits
**Asked:** README was ~90% terminal-only with no Web UI docs — add how to run/use the
UI. Also: the public-repo commits showed an AI co-author; remove that (keep CLAUDE.md /
journal.md as LLM project context, which the owner is fine with — just no AI attribution
in git history).
**Did:** Added a prominent "Web UI — run everything from your browser" section to
`README.md` (local run, network/server run, the 5 tabs, env knobs, security note, link
to WEB_UI_GUIDE.md) + a Core-Capabilities bullet. Rewrote the branch's commits to drop
the `Co-Authored-By` trailer (`git filter-branch --msg-filter`) and force-pushed
`feature/web-ui`.
**Decided:** Honor the repo owner's request — omit AI co-author trailers on this repo
from now on. CLAUDE.md + journal.md stay (project context for future LLMs).
**Open:** None.

## 2026-06-20 — Pushed everything to GitHub branch `feature/web-ui`
**Asked:** Push this fully-working dir to GitHub as a new branch with proper commit history.
**Did:** Created branch `feature/web-ui` off the local `rule/apache` base (8461bdd) and
organized the whole session's work into **5 logical commits**: (1) perf+reliability
(multiprocessing workers, at-least-once, orjson/caches), (2) ECS tooling (schema DB,
ecs_helper, validator, rule migrations, guide), (3) ops tooling (preflight, replicate,
examples), (4) web UI (Flask console, monitor, packaging), (5) docs+memory (README,
CLAUDE.md, journal.md). Pushed to origin. Added a `.gitignore` negation
`!examples/samples/*.log` so the replicate.py sample logs survive clone (else `*.log`
would drop them). Verified no large files committed (nginx.log/mmdb/release zip/build_out
all ignored). Left stray `ngnix_test.txt` untracked. 48 files, +7307/-197.
**Decided:** Branched from local 8461bdd (behind origin/rule/apache); the content that
matters is the working tree (v4), so the new branch is an independent, clean snapshot.
**Open:** PR not opened — `https://github.com/sankettaware16/foss-soc-engine/pull/new/feature/web-ui`.
(This journal entry was written after the push, so it's an uncommitted change on top.)

## 2026-06-20 — Merged the Web UI version into this working dir (v3 → v4)
**Asked:** Upgrade this dir to the latest "v4" version (the one that added a Web UI),
from a parallel copy at `~/code/projectTLSOC/foss-soc-engine`.
**Did:** Diffed v4 vs this dir; confirmed v4 = this session's v3 + a UI/observability
layer (additive, no regression). Brought in the new `webui/` Flask app (app.py +
templates/static + launchers + PyInstaller spec), `FOSS SOC Console.html` (design
mockup), `WEB_UI_GUIDE.md`. Took v4's versions of `main.py` (engine.pid heartbeat + 2s
stats), `core/engine.py` + `utils/geoip.py` (lazy redis/geoip2 imports so the UI runs
on Flask+PyYAML only), `rules/nginx.yaml` (log timestamp → `event.original_time`),
`CLAUDE.md` (UI sections), `.gitignore` (build_out/release). Merged `config.yaml` (added
`runtime.metrics_interval_sec: 2`; KEPT this repo's generic localhost/auto defaults, not
v4's elkcc production values). Merged this journal (v4's 4 UI entries + kept my elkcc
entry below). **Skipped** the generated `build_out/`+`release/` artifacts (now gitignored).
**Decided:** Repo stays a clean template — did NOT bake production broker/topic/workers
into config.yaml; build artifacts are reproducible via `webui/build_exe.py`.
**Verified:** imports OK, `ecs_helper check rules/` clean, `test_config --skip-kafka`
passes, `webui/app.py` compiles.
**Open:** `rules/nginx.yaml` now emits `event.original_time` (custom, not core ECS — the
validator allows it as custom). README has no Web UI section yet (v4 didn't add one).

## 2026-06-20 — Web UI: live Monitor tab (real-time engine observability)
**Asked:** Add a separate, real-time monitoring dashboard for when the engine runs as a
service — EPS/per-second counts, is-it-working, uptime, performance metrics, which rules
are in use, RAM/memory, a restart button — auto-updating (no page refresh), for tracing/
observability so errors are easy to find.
**Did (instrumented `main.py`, observability only — did NOT touch the 5 strategies/rule
format/deploy):**
- `HealthMonitor`: stats now written every `runtime.metrics_interval_sec` (default **2s**,
  was hardcoded 60s) with richer payload (pid, worker_id, total_errors, window_sec); the
  human "Stats:" log line is throttled to 60s so engine.log stays clean.
- New `logs/engine.pid` heartbeat (`write_runtime`/`clear_runtime`): role, pid,
  worker_pids, start_time, kafka topic/group, output_dir. Supervisor writes/refreshes it
  on spawn/respawn and each aggregate cycle (now every `max(2,interval)`s, was 60s);
  single-worker mode writes it from run_worker. Removed on clean shutdown.
- `aggregate_stats` carries `total_errors`. Added `config.yaml runtime.metrics_interval_sec`.
**Did (Web UI):**
- `webui/app.py`: `/api/monitor` (engine status from pid liveness + freshness; self-sums
  per-worker stats each poll for real-time EPS; per-rule + per-worker tables; host CPU/RAM
  via psutil→/proc→Windows ctypes, load avg, engine RSS; DLQ size), `/api/monitor/dlq`
  (tail recent unparsed), `/api/engine/<start|stop|restart|status>` (systemd; **gated by
  `SOC_UI_ALLOW_CONTROL=1`**, Linux only). `SOC_LOG_DIR` override for UI↔engine log path.
- New **Monitor** nav tab: status light (running/starting/stopped) + uptime/workers/topic/
  pid, live stat tiles, an EPS **canvas sparkline** (client rolling history), system meters,
  Rules-in-use table, Workers table, DLQ panel. Polls `/api/monitor` every 2s only while the
  tab is active (no page reload). Light-glass styled to match.
**Decided:** Drive stat-file selection from the heartbeat's worker count (not a glob) +
drop stale files — fixes phantom workers from leftover `stats.wN.json` of an earlier run,
and ensures single-worker mode reads `stats.json` even when old per-worker files exist.
Engine control off by default (a networked UI must not be able to stop the engine).
**Verified:** Engine can't run here (no Kafka), so tested with synthesized heartbeat+stats:
multi-worker → status=running, EPS summed (48213+51120=99333), rules merged across workers,
mem via ctypes (85.5%), pid-liveness true, **no phantom workers** (4-day-old leftover
stats.w2/w3 correctly ignored); single-worker → reads stats.json, ignores stale w-files;
stale data → status correctly "starting"; control disabled → 403. Rebuilt exe + refreshed
zip (8.7 MB). Cleaned synthetic/stale stat files from logs/.
**Open:** Per-process CPU% not shown (host CPU% + engine RSS only); psutil optional (Linux
uses /proc, Windows ctypes for RAM, CPU% best-effort). Real end-to-end test needs a live
Kafka feed. systemd control needs a sudoers/root grant for the UI user.

## 2026-06-20 — Web UI: Ubuntu server run + network (LAN IP) access
**Asked:** How does someone run this on an Ubuntu server, and how is the UI reached
over the network (real IP instead of 127.0.0.1)?
**Did:** It was already supported via `SOC_UI_HOST=0.0.0.0`; made it operator-friendly:
- `webui/app.py` `main()`: added `_lan_ip()` and a smarter banner — when bound to all
  interfaces it prints BOTH `http://127.0.0.1:PORT/` and `http://<lan-ip>:PORT/` plus a
  firewall reminder; auto-suppresses browser-open on a server (0.0.0.0 ⇒ no_browser
  unless `SOC_UI_NO_BROWSER=0`).
- Added `webui/foss-soc-ui.service` (systemd unit template: User/WorkingDirectory/IP/port
  placeholders, `SOC_UI_HOST=0.0.0.0`, venv ExecStart, Restart=on-failure, light hardening).
- Expanded `WEB_UI_GUIDE.md` with section 10 "Run it on an Ubuntu server & open it over
  the network" (copy code → `start-soc-ui.sh` → `hostname -I` → bind 0.0.0.0 → `ufw allow`
  → browse `http://<server-ip>:8600`; systemd install; nohup/screen alt; note to build a
  native Linux binary with `build_exe.py` ON Linux). Added a no-auth/trusted-network
  security warning.
**Decided:** Linux = run from Python (`start-soc-ui.sh`, needs only Flask+PyYAML); the
Windows `.exe` is PE-only and won't run on Linux — build a native binary on the Ubuntu
box if a single-file binary is wanted. Keep the dev server (Werkzeug, threaded) — fine
for an internal testing tool; reverse-proxy/auth only if exposed wider.
**Verified:** Ran `SOC_UI_HOST=0.0.0.0` locally — banner printed `From the network:
http://172.20.10.6:8604/`, listener bound on all interfaces, `/api/health` reachable
(10 rules). Rebuilt the Windows exe so the shipped artifact carries the new banner.
**Open:** No built-in auth (documented). systemd unit paths are placeholders the operator
must edit.

## 2026-06-20 — Web UI re-skin to match design reference (light liquid glass)
**Asked:** Use `FOSS SOC Console.html` (a bundled design mockup at repo root) as the
reference for colour grade, gradients, fonts and icons, and bring those into the real
Web UI.
**Did:** Extracted the design tokens from the reference's embedded `__bundler/template`
(it stores the markup/CSS as JSON text). Findings: a **light** frosted-white glass
theme in the **OKLCH** colour space (not my original dark theme); fonts **Hanken
Grotesk** (UI) + **IBM Plex Mono** (code); a **shield+check** logo on a blue→purple
gradient; **Lucide-style** stroke-line icons.
- Rewrote `webui/static/css/style.css` to the exact reference tokens: page bg = base
  `oklch(0.965 0.013 245)` + blue `oklch(0.86 0.06 232)` / lavender `oklch(0.87 0.055
  285)` / soft `oklch(0.93 0.03 250)` radial glows; glass = white `oklch(1 0 0 /0.7)`,
  `backdrop-filter: blur(26px) saturate(1.4)`, stroke `oklch(0.55 0.03 258 /0.1–0.14)`,
  radius 22; signature shadow `0 24px 60px -28px oklch(0.5 0.07 262 /0.18)` + inset
  highlight; accents blue `oklch(0.55 0.13 248)` / purple `oklch(0.55 0.16 288)` /
  green `oklch(0.7 0.14 152)`; light-readable JSON syntax tints. Kept every class name
  so app.js/HTML were untouched logically.
- Bundled the fonts **locally** (latin-subset woff2, ~180 KB total) under
  `webui/static/fonts/` + a `webui/static/css/fonts.css` with `@font-face` — so the app
  stays **fully offline** (no Google Fonts CDN). Downloaded once from fonts.gstatic.com.
- `webui/templates/index.html`: linked fonts.css, replaced the brand glyph with an
  inline **shield-check SVG** on the gradient mark, replaced all nav emoji with inline
  **stroke SVG icons** (grid / zap / file-code / sliders / sparkles / send).
- Rebuilt `FOSS-SOC-UI.exe` (PyInstaller picks up the new static/ automatically) and
  re-zipped `release/FOSS-SOC-UI-windows.zip` (8.6 MB).
**Decided:** Keep the reference's **light** theme rather than my earlier dark one — the
ref is the source of truth for the look. Bundle fonts locally (not CDN) to honour the
offline / no-dependency requirement while staying faithful.
**Verified:** Dev server + the **frozen exe** both serve the shield logo, `fonts.css`,
and the woff2 files (200, correct bytes), and style.css carries the OKLCH tokens. All
API behaviour unchanged (no JS/route edits).
**Open:** Only the latin font subset is bundled (fine for English UI; non-latin glyphs
fall back to system fonts). Button trailing glyphs (⚡/✈) left as emoji; could swap to
SVG too. No visual screenshot captured in-session (no browser-automation tool) — verified
via served asset bytes.

## 2026-06-20 — Web UI (Flask, liquid-glass) + Windows .exe packaging
**Asked:** Turn the engine into a downloadable, plug-and-play desktop app with a
classy "liquid glass" browser UI that exposes every local capability (test a log
file, add/test a parser, validate config, ECS help) so operators never run a
python file by hand. Must give **no dependency error** on Windows/Linux/Mac;
Windows must be true "download and run". Add a browser-usage .md. Release for
testing only (not git).
**Did:**
- Made heavy imports optional so the whole app runs on **Flask + PyYAML only**:
  `core/engine.py` now imports `redis` lazily (`r=None` if absent); `utils/geoip.py`
  imports `geoip2` lazily and skips enrichment when missing. Behavior identical
  when the libs ARE present — did NOT touch the 5 strategies, rule format, or deploy.
- New `webui/` package: `app.py` (Flask, ~30 routes), `templates/index.html`,
  `static/css/style.css` (glassmorphism, offline — no CDN/fonts), `static/js/app.js`
  (vanilla). Sections: Dashboard, Test Log (paste/upload, AUTO or specific parser),
  Rules (view/edit/create/delete + per-strategy templates + live ECS check), Config
  (edit+validate), ECS Helper (classify/find), Preflight (static always; live Kafka/
  Redis/TCP optional). Reuses the real engine + `test_config`/`preflight`/`ecs_schema`
  functions (captures their stdout into structured JSON) so the UI never diverges.
- Graceful degradation: Redis/GeoIP/Kafka/orjson all show "available/not installed"
  badges; stateful-without-Redis and geo-without-mmdb show a notice instead of erroring.
- Packaging: `webui/requirements-ui.txt` (Flask+PyYAML, wheels on all OSes),
  `webui/Start-SOC-UI.bat` + `webui/start-soc-ui.sh` (auto-venv launchers),
  `webui/foss-soc-ui.spec` + `webui/build_exe.py` (PyInstaller one-file build that
  bundles code+templates; rules/config stay editable next to the exe via
  `sys.frozen`/`_MEIPASS` path split). `.gitignore` now ignores `build_out/`,
  `release/`, `.venv-ui/`.
- Wrote `WEB_UI_GUIDE.md` (non-technical browser guide: 3 ways to start, every tab,
  troubleshooting, what works with nothing installed).
**Decided:** PyInstaller **one-file .exe** as the primary Windows artifact (8.6 MB,
console kept so users see the URL + can Ctrl+C). UI defaults to 127.0.0.1:8600,
overridable via `SOC_UI_PORT/HOST/NO_BROWSER`. Because redis/geoip2/kafka aren't
installed in this build env, the exe bundles only Flask+PyYAML — exactly the
minimal local-testing footprint, which is what we want.
**Verified:** Ran the app via `python webui/app.py` — `/api/health` (10 rules,
all optional caps False yet fully working), `/api/test` (apache line → correct ECS,
status 401/403 via AUTO routing), `/api/config/validate` (passed), `/api/preflight`
(static passed), ECS classify (`srcip`→`source.ip`) + find, rules save/delete,
index+css+js serve (200). Built `FOSS-SOC-UI.exe` and ran the **standalone exe**:
`frozen=True`, `data_root`=release folder, parsed a log line, served the UI page.
Zipped to `release/FOSS-SOC-UI-windows.zip` (8.4 MB).
**Open:** exe is OS-specific — Linux/macOS binaries need building on those OSes
(build_exe.py is cross-platform; just run it there). Unsigned → Windows SmartScreen
"Run anyway" on first launch. Could add an app icon and a `--onedir` faster-start
option. UI is single-user/local-only (no auth) — fine for testing, note before LAN use.

## 2026-06-16 — Production deploy support (elkcc): worker/partition → filenames
**Asked:** On elkcc (consuming topic `logservercc`, 1 partition, from logserver's
rsyslog), preflight passed and the engine runs, but postfix seemed to parse only 1
event, and output files had `.w0.json` suffixes — user wants plain `nginx.json`.
**Diagnosed:** `workers: auto` (=4) on a **1-partition** topic → only worker 0 can
consume, and the engine writes per-worker files (`nginx.w0.json`, `postfix.w0.json`)
so 4 processes never corrupt one file. The plain `nginx.json`/`postfix.json` they were
tailing were **stale leftovers from an earlier run** — that's the "1 postfix event".
**Advised:** Set `runtime.workers: 1` (correct for a 1-partition topic anyway) → suffix
becomes "" → plain `nginx.json`/`postfix.json`, clean console logs, no idle workers.
Delete stale `/var/log/soc_output/*.json`, restart, tail the single file. Noted postfix
is stateful (correlates by queue-id, emits on `removed`) so it's far lower-volume than
nginx — sparse is expected.
**Decided/known:** Per-worker file suffixes are by-design for safe concurrent writes;
single filename requires `workers: 1` (or scale partitions+workers and let downstream
glob `*.json`).
**Open:** If postfix stays nearly empty or every event only has `host.name` +
`event.original` after the fix, tune `rules/postfix.yaml` — its first sub-pattern
`'\s(?P<hostname>...)\spostfix/'` matches every line and can short-circuit the stateless
fallback before richer patterns run.

## 2026-06-16 — Added portable memory (CLAUDE.md + journal.md)
**Asked:** Create a CLAUDE.md so any model/account/interface gets full history without
starting from scratch, and a journal.md used like a diary updated every chat turn.
**Did:** Created `CLAUDE.md` (stable project context: what it is, hard constraints,
architecture, file map, run/test commands, decisions/policies, perf facts, open ideas,
and a prominent MAINTENANCE PROTOCOL) and this `journal.md` (backfilled with the whole
session). CLAUDE.md instructs every future turn to prepend a journal entry.
**Decided:** Two-file split — CLAUDE.md = durable truth, journal.md = chronological
log. In-repo (travels with git) rather than relying on tool-specific memory.
**Verified:** File inventory listed to keep the map accurate.
**Open:** Nothing committed yet — all session work is staged in the working tree.

## 2026-06-16 — replicate.py + bundled runnable example
**Asked:** A script to replicate the full rsyslog→Kafka→engine flow locally with no
Kafka, using ~50 sample lines, to surface pipeline issues before real logs flow; then
help on how to run/test it with samples.
**Did:** Created `replicate.py` — parses an rsyslog imfile/omkafka conf (File→Tag,
forwarding filter, topic, broker), wraps sample lines in the exact Kafka envelope, runs
them through the real engine, and reports per-source: tag-not-forwarded, topic mismatch,
broker mismatch, missing `program_mapping` (with **auto-suggested rule + exact config
line**), and regex match rate. Added `examples/rsyslog_sample.conf` +
`examples/samples/{postfix.log,access.log,auth.log,fim.json}`. Documented in README.
**Decided:** Match-counting uses a side-effect-free "recognizer" (no Kafka/Redis needed)
so it's a pure local simulator. Single-file mode (`--file/--program`) for quick tests;
`--logs-dir` redirects conf paths to a sample folder by basename.
**Verified:** Bundled example runs green (4 sources, exit 0); also tested against a
replica of the owner's real mailserver conf — correctly caught `mail_smtp` missing from
the forward filter, broker mismatch, and 6 missing mappings with correct rule suggestions.
**Open:** examples/samples covers 4 of the source types; could add suricata/roundcube/modsec.

## 2026-06-13/16 — v1→v2→v3 evolution analysis (no code change)
**Asked:** Compare GitHub `main` (v1), GitHub `rule/apache` (v2), and local (v3); how
much it improved.
**Did:** Fetched remotes, diffed trees + capability markers. Summarized: all 5 parsing
strategies existed in **v1** already; v1→v2 was incremental (test_file.py, more rules,
docs, engine refinements, still single-core/auto-commit/stdlib-json); **v2→v3 is the
architectural leap** (multiprocessing, at-least-once, orjson, caches, ECS tooling,
preflight, replicate). Efficiency: ~50k→0.5–1M EPS aggregate (~10–20×); per-core ~2×.
**Decided:** N/A (analysis only). Note: local v3 is based on an older commit than pushed
v2, so it doesn't include a separate remote `testing/dashboard` branch.
**Open:** —

## 2026-06-13/16 — Plug-and-play roadmap (discussion only, no code)
**Asked:** What else would make it more plug-and-play.
**Did:** Recommended, ranked: (1) pluggable output sinks (OpenSearch/Kafka re-emit/S3),
(2) runtime auto-parser-detection (drop `program_mapping`), (3) Docker-compose bundle,
(4) auto-create Kafka topics/partitions, (5) built-in syslog/CEF/LEEF parsers, (6) make
Redis configurable, (7) tolerate raw non-enveloped Kafka strings, (8) HTTP `/metrics` +
status CLI, (9) DLQ-driven "what's unparsed" report, (10) `confluent-kafka`.
**Decided:** Top 3 = sinks + auto-detect + Docker. Caveat: keep bounded-batch
backpressure so RAM stays flat. None implemented yet.
**Open:** All of the above are future work.

## 2026-06-13 — preflight.py (full pre-run validator)
**Asked:** One script to fully validate before running: config correct, IP reachable
from server, Kafka port reachable, topics exist, rules mappings correct.
**Did:** Created `preflight.py` — reuses `test_config.py`'s static validators and adds
live checks: raw TCP reachability to each broker, Kafka broker handshake + topic
existence with partition counts, Redis ping (only if a stateful rule exists), and
workers-vs-partitions. Per-section `[OK]`, exit 0 = safe to start. Documented in README.
**Verified:** With Kafka down it correctly FAILed (#6 TCP, #7 handshake) while confirming
Redis up; `--skip-live` passed static checks.
**Open:** —

## 2026-06-12/13 — Q&A: real-time start & auto-scaling (no code change)
**Asked:** Will a fresh start process old backlog? Is multi-scale automatic?
**Decided/answered:** `auto_offset_reset: "latest"` → a **fresh `group_id`** starts at
newest and skips backlog; a group with committed offsets resumes (processes the gap);
use a new `group_id` to force real-time. Scaling within a box is automatic
(`workers: auto`); across boxes = same `group_id` on each; need partitions ≥ workers;
it's static parallelism, not load-reactive.
**Open:** —

## 2026-06-12 — ECS rule-authoring system + AI master prompt
**Asked:** Rules must follow ECS; build a helper/autocorrect for non-ECS-experts; fix my
rules to real ECS where wrong but keep customs; add a validator that blocks non-ECS;
write a rule guide + a master prompt so any AI can generate a compliant rule from raw logs.
**Did:** Created `core/ecs_schema.py` (ECS field DB + `classify/suggest/search` +
`ALIASES` autocorrect), `ecs_helper.py` (`check`/`fix`/`find`/interactive), wired ECS
checks into `test_config.py` (alias/typo = ERROR with fix; custom = allowed), migrated 4
rule fields to real ECS via the helper's own `fix` (email.from→email.from.address,
email.to→email.to.address, file.owner_uid→file.uid,
vulnerability.cvss.base_score→vulnerability.score.base), wrote `WRITING_RULES.md` (5
strategies + cheat-sheet + **master AI prompt**), and inlined the master prompt into
README too.
**Decided:** Policy = enforce ECS where it exists, **keep ~70 custom fields** where ECS
has none (don't strip the owner's domain fields). Left `vulnerability.cve` custom (would
collide with the OID already mapped to `vulnerability.id`).
**Verified:** `ecs_helper check rules/` clean; `test_config.py` passes; a deliberately
bad rule (`srcip`/`status`) is rejected (exit 2); nginx 5000/5000 + postfix 17 parsed —
no regression; migrated `email.from.address` produces correct nested ECS.
**Open:** Offered to move `vulnerability.cve`→`vulnerability.id` (+ OID→reference) if wanted.

## 2026-06-12 — Production hardening (v2→v3 leap)
**Asked:** Make it production-grade and able to handle 1M+ EPS without changing the
engine's core definition or the simple deploy.
**Did:** Rewrote `main.py` into a multiprocessing supervisor (forks N workers, default
all cores, same Kafka consumer group; auto-restart; graceful SIGTERM drain; per-worker
output/stats files + aggregated stats.json). Switched to **at-least-once** (manual commit
after flush). Added `core/output.py` (keep-open buffered writers, orjson bytes,
per-worker files), `utils/fastjson.py` (orjson + stdlib fallback), GeoIP LRU cache in
`utils/geoip.py`, cached 1s `@timestamp` + `fastjson` in `core/engine.py`. Updated
`config.yaml` (`runtime.workers`, `output`), `requirements.txt` (orjson),
`setup_service.sh` (graceful stop, LimitNOFILE), README.
**Decided:** Scale via **Kafka consumer group**, not a rewrite — keeps the simple YAML/
deploy UX. orjson optional. Don't touch the 5 strategies or rule format.
**Verified:** Benchmarks on i5-13400: per-core ~48k→97k EPS; serialize 4.91→0.72µs;
envelope parse 1.53→0.61µs. Supervisor fork/restart/graceful-shutdown test passed;
end-to-end write path produced valid NDJSON; no parsing regression.
**Open:** `confluent-kafka` is the next lever for a clean 1M-on-one-box; Redis host still
hardcoded.

## 2026-06-12 — Initial throughput analysis
**Asked:** Can the engine (as written) survive 1M+ EPS with all rules + 3+ topics, no
GPU, no RAM spikes, staying simple?
**Did:** Read the whole repo; benchmarked the real hot path on real nginx logs.
**Found:** As-written it was **single-process / single-core (GIL-bound)** → ~40–90k EPS
(~50k typical), ~10–20k for stateful (Redis round-trip per line) — i.e. ~5% of 1M.
Identified bottlenecks: single core, per-event geoip (no cache), per-event
`datetime.now()`, file open/close per flush, per-event DLQ open, auto-commit
(at-most-once data loss). RAM was already safe (synchronous/bounded). GPU never needed.
**Decided:** The fix is parallelism (consumer group) + hot-path caching + at-least-once
— which became the hardening work above.
**Open:** Led directly into the production-hardening turn.
