# FOSS SOC Engine

A high-performance, polymorphic log parsing and normalization engine designed for Security Operations Centers (SOC).
Free and open source under the **Apache-2.0** license ([LICENSE](LICENSE)).

The FOSS SOC Engine consumes raw logs from Kafka, dynamically routes them to the correct parser based on log metadata, and normalizes them into structured, ECS-compliant JSON. It supports stateless regex parsing, stateful multi-line log reassembly, and direct JSON field mapping for high-throughput environments.

---

## Core Capabilities

### Polymorphic Routing
Decouples log sources from parsing logic. Multiple source programs (for example,
`mail_auth`, `web_auth`, `linux_auth`) can be routed to a single reusable rule
definition via configuration-based program mapping — and one messy source can be
routed to a **chain** of rules (`webserver01: ["nginx_access", "php_errors"]`):
the engine tries them in order and the first rule that handles a line wins.

### Stateful Parsing
Reassembles fragmented or multi-line logs (such as Postfix email transactions) into a
single coherent event using Redis-backed correlation. The transaction lifetime is per
rule (`state_ttl_sec`, default 300 s); transactions that never complete are emitted as
`event.incomplete: true` events instead of being dropped, so nothing is lost silently.

### Hybrid Parsing Strategies

The engine supports multiple parsing strategies selectable per rule:

- **stateless**  
  Standard regex-based parsing for single-line logs  
  Examples: Apache, Nginx access logs

- **multi_match**  
  Sequential evaluation of multiple regex patterns  
  Examples: Linux authentication logs, SSH, sudo, cron

- **stateful**  
  Correlates multiple log lines using transaction identifiers  
  Examples: Postfix mail flow, WAF transaction logs

- **json_map**  
  High-speed direct mapping of JSON logs using dot-path notation with wildcard support  
  Examples: ModSecurity, structured application logs, cloud audit logs

- **xml_xpath**  
  Element/attribute mapping of XML documents (one event per repeated element)  
  Examples: OpenVAS and Nessus scanner exports, XML audit feeds

### Fast matching at scale (prematch gate)
Every `multi_match`/`stateful` pattern can declare a `prematch:` — a plain substring
checked with a cheap `in` **before** the expensive regex runs. Almost all patterns are
skipped instantly, so a rule can grow to hundreds of patterns without the linear
regex-scan cost (a 500-pattern rule measures ~13× faster with prematch). It is purely
an optimization — rules produce identical output with or without it — and the deploy
validator also runs a **ReDoS lint** on every regex to reject catastrophic-backtracking
shapes before they can stall a worker. See [WRITING_RULES.md](WRITING_RULES.md).

### Auto-Enrichment
Automatically enriches events for public IP addresses — fully **offline** (one-time
MaxMind database download, no per-lookup network calls), with per-process LRU caching:
- **GeoIP** (`GeoLite2-City.mmdb`) → `source.geo.*`: city, country, latitude/longitude
- **ASN** (`GeoLite2-ASN.mmdb`) → `source.as.number` + `source.as.organization.name`:
  which ISP, cloud or hosting provider **owns** the IP — instantly separates
  residential users from VPS/botnet/scanner traffic

Both are switched by `geoip.enabled` in `config.yaml`; omit `asn_db_path` to run
City-only. Missing library or database = enrichment quietly skipped, never a crash.

### Accurate Event Time (two-timestamp model)
`@timestamp` carries the event's **real** time, parsed from the log line itself
(per-rule `timestamp:` declaration — CLF, ISO 8601/RFC5424, RFC3164 syslog, Unix
epoch, and more) and normalized to UTC — so logs delayed by an outage or a Kafka
backlog still land on the correct day in Elasticsearch. Every event also gets:
- `event.ingested` — when the engine parsed it (ingest lag = `event.ingested` − `@timestamp`)
- `event.timestamp_source` — `log` (parsed), `log_assumed_utc` (parsed, zone
  assumed), or `ingest_fallback` (unparseable → stamped at ingest, visibly tagged —
  never silent)

See [WRITING_RULES.md](WRITING_RULES.md) §5 for the `timestamp:` block reference.

### Horizontal Scaling
Runs a pool of worker processes (one per CPU core by default) that share load
through a single Kafka consumer group. Scale up by adding cores or by running the
engine on more machines with the same `group_id` — no code or rule changes.

### Resilience and Observability
- **Per-source Dead Letter Queue**: unparseable logs land in `logs/dlq/<source>.json`
  (nginx, postfix, ... each in their own file) with the failure reason. Files are
  size-capped (`output.dlq_rotate_mb`, default 200 MB) so a broken source can never
  fill the disk, and a rate-limited **"DLQ STORM" warning** fires in `engine.log`
  when one source dead-letters heavily (≥5000/min) — your signal that a rule or
  `program_mapping` broke.
- **At-least-once delivery, hardened**: Kafka offsets are committed only after logs
  are durably flushed to disk. If a flush fails (disk full, I/O error) the offsets
  are NOT advanced — the engine retries and Kafka redelivers. Duplicates are possible
  on a bad-disk day; silent loss is not.
- **Stateful transactions never vanish**: a multi-line transaction that never sees its
  end signal is emitted when its TTL runs out, tagged `event.incomplete: true` /
  `event.reason: transaction_timeout`, and counted per rule (`expired` in the stats
  and the Web UI Monitor) instead of silently disappearing.
- Graceful shutdown: clean flush and commit on `systemctl stop`/restart
- **Live rule reload**: editing a rule file in place is picked up within ~10 seconds —
  no restart needed.
- Continuous health monitoring with throughput (EPS), error rate, and uptime tracking
- Every event carries `ecs.version`, `event.ingested` and `event.timestamp_source`
- Optional `orjson` acceleration (installed automatically; falls back to stdlib if absent)

---

## Three ways to use it

The parsing engine is the same in every case — these are just different front-ends
for **writing/testing rules, editing config, and watching the engine run**. Pick
whichever fits your team. You are never locked into one.

| # | Interface | Best for | Needs a terminal? |
|---|---|---|---|
| 1 | **Command line** (`main.py` + the `test_*` / `preflight` / `replicate` tools) | production servers, CI/CD, automation, power users | yes |
| 2 | **Web UI** — a point-and-click browser console (secure login) | operators who want no terminal; a standalone box; a quick pilot | no |
| 3 | **Kibana plugin** — the same console *inside* Kibana | teams already living in the ELK stack | no |

- **The engine itself always runs from the command line** (`main.py`, usually under
  systemd) — that is what consumes Kafka and writes ECS output. Interfaces 2 and 3 do
  **not** run a second engine; they are consoles that edit the *same* rules/config and
  read the *same* live stats. See [**Web UI**](#web-ui--browser-console-no-terminal)
  and [**Kibana plugin**](#use-it-inside-kibana-elk-plugin) below.

---

## Prerequisites

### Software
- Python 3.8+
- Apache Kafka (input source)
- Redis (required for stateful parsing)

### System
- Linux environment  
  Ubuntu / Debian recommended for systemd service integration

---

## Installation
### GeoIP + ASN Database Requirement

This project uses two MaxMind GeoLite2 databases for IP enrichment. Both are
**totally offline**: a one-time download, then every lookup is a local file read
(no network calls at parse time, ever):

| Database | File | Adds to events |
|---|---|---|
| GeoLite2 **City** | `database/GeoLite2-City.mmdb` | `source.geo.*` (country, city, lat/lon) |
| GeoLite2 **ASN** | `database/GeoLite2-ASN.mmdb` | `source.as.number`, `source.as.organization.name` (which ISP/cloud/hosting company owns the IP) |

Due to MaxMind licensing restrictions, the databases are **not stored in the
repository**. `install.sh` downloads both automatically (same free MaxMind
account/key works for both).

Before running `install.sh`, export your MaxMind license key:

```bash
export MAXMIND_LICENSE_KEY=YOUR_MAXMIND_KEY
```

Manual download (if you skip install.sh): log in at maxmind.com → *Download
Files* → grab **GeoLite2 City** and **GeoLite2 ASN** (mmdb format), and place
the two `.mmdb` files in the `database/` folder. Refresh them whenever you like
(MaxMind updates weekly) by re-downloading — the engine picks the new file up on
restart.

Enable / disable in `config.yaml` — no code changes:

```yaml
geoip:
  enabled: true                              # false = skip BOTH lookups
  db_path: "database/GeoLite2-City.mmdb"     # geo (country/city/coords)
  asn_db_path: "database/GeoLite2-ASN.mmdb"  # ASN (IP owner) — comment out to
                                             # run City-only
```

A missing file or a missing `geoip2` library never crashes the engine — that
enrichment is simply skipped (and `preflight.py` warns you about it).

### 1. Clone the Repository

```bash
git clone https://github.com/sankettaware16/foss-soc-engine.git
sudo mv foss-soc-engine /etc/
cd /etc/foss-soc-engine
```
2. Run the Installer

The installer performs exactly two things:

- Installs the Python dependencies (`pip3 install -r requirements.txt`)
- Creates the runtime directories (`logs/`, `database/`) — and, if you export
  `MAXMIND_LICENSE_KEY`, downloads the GeoIP databases into `database/`

(It does **not** change file permissions or install a service — use
`setup_service.sh` for systemd.)
```
chmod +x install.sh
./install.sh
```
3. (Optional) GeoIP / ASN databases

Enrichment is **optional** — the engine runs fine without it (geo/ASN fields are
simply skipped, never a crash). To enable it, put the MaxMind `.mmdb` files in
`database/` as described in **[GeoIP + ASN Database Requirement](#geoip--asn-database-requirement)**
above (`install.sh` fetches them for you if you export `MAXMIND_LICENSE_KEY`), then
set `geoip.enabled: true` in `config.yaml`.

```
# manual placement (if you didn't let install.sh download them):
mv /path/to/GeoLite2-City.mmdb ./database/
mv /path/to/GeoLite2-ASN.mmdb  ./database/
```

4. Configure the engine

Edit `config.yaml` to match your environment (the shipped file is already a working
template — you mainly change the Kafka connection and `program_mapping`):
```yaml
kafka:
  bootstrap_servers: ["localhost:9092"]
  input_topic: "soc-logs"          # a single topic, OR a regex like "linux|firewall|web"
  group_id: "soc-parser-group"
  auto_offset_reset: "latest"      # fresh group_id starts at newest (skips backlog)

paths:
  output_dir: "/var/log/soc_output/"
  rules_dir: "rules/"

program_mapping:
  ssh_server: "linux_auth"
  ftp_server: "linux_auth"
  modsec_audit: "modsec"

```
`program_mapping` lets multiple source programs reuse a single rule (or a list
of rules — see [chains](WRITING_RULES.md)).

5. Install Redis  *(only needed if you use `stateful` rules, e.g. postfix)*
```
sudo apt install redis-server -y
sudo systemctl enable redis-server
sudo systemctl start redis-server
```
(Point the engine at a non-local Redis with the `redis:` block in `config.yaml`.)

6. Create the output directory
```
sudo mkdir -p /var/log/soc_output/
sudo chown -R username:username /var/log/soc_output/   # if required
```

7. (Recommended) Load the Elasticsearch index template

If you ship to Elasticsearch, load the bundled index template **before the first
event is indexed**, so every field gets the right type (dates, IPs, `geo_point`,
numbers) from day one and mapping conflicts can't happen. Full instructions:
[`elasticsearch/README.md`](elasticsearch/README.md).

---

## Usage

### Pre-flight check (run this before starting the engine)

`preflight.py` validates everything that can stop the engine from working — in one
command — so you catch problems before going live instead of after:

```
python3 preflight.py                 # full check (config + live infrastructure)
python3 preflight.py --skip-live     # static checks only (no network calls)
python3 preflight.py --config /path/to/config.yaml --timeout 6
```

It checks, in order:

1. `config.yaml` exists and is structurally correct
2. paths (rules dir, output dir) and the GeoIP database
3. rules load and every regex compiles
4. every rule field is valid ECS
5. `program_mapping` points at rules that exist
6. the Kafka `host:port` is actually reachable **from this server** (raw TCP)
7. the broker really speaks Kafka, and your configured topics **exist** (with partition counts)
8. Redis is reachable — only required if you use any `stateful` rule
9. you have enough Kafka partitions for the number of workers

Exit code `0` = safe to start; non-zero = fix the reported errors first (so it can
also gate a deploy script). Example of a clean run:

```
=== 7. Kafka broker & topics ===
[OK] Kafka broker confirmed, 8 topic(s) on cluster
[OK] 3 topic(s) match input_topic: web-logs[12p], firewall-logs[12p], linux-logs[6p]

============================================================
  RESULT: PASS  -  0 errors, 0 warning(s)
  Safe to start:  sudo systemctl start foss-soc   (or  python3 main.py)
============================================================
```

> `test_config.py` does the static config/rules/ECS checks (plus a ReDoS lint
> on every regex); `preflight.py` reuses those and adds the live network /
> Kafka / topic / Redis / partition checks. Run
> `python3 test_config.py --skip-kafka` on machines with no broker in reach
> (CI does exactly that).

The regression battery (CI runs all of these on every push/PR):

| Suite | Proves |
|---|---|
| `test_timestamps.py` | every rule's `timestamp:` block parses its format, normalizes to UTC, and unparseable times fall back **visibly** (`ingest_fallback`) — 49 cases |
| `test_enrichment.py` | GeoIP + ASN enrichment plumbing, real-database lookups (auto-skip if the mmdb files are absent), and that `geoip.enabled: false` really disables both |
| `test_golden.py` | every rule's **golden-sample exam** (`tests/samples/<rule>/input.log` vs `expected.ndjson`): any rule edit that changes any answer fails with a diff; refresh intentionally with `--update <rule>` and review |

### Replicate the full pipeline (dry-run, no Kafka needed)

Before you point a real server at the engine, `replicate.py` mimics the **entire**
`rsyslog → Kafka → engine → ECS` flow **locally, with no Kafka and no second
server**. It reads your rsyslog imfile config to learn which log file becomes
which `source_program` (Tag), wraps a small sample (~50 lines) of each file in the
exact Kafka envelope your template produces, runs it through the real parser, and
tells you precisely where the pipeline would break.

```
# Try it immediately with the bundled example (no setup needed):
python3 replicate.py --rsyslog examples/rsyslog_sample.conf --logs-dir examples/samples

# Then point it at your own rsyslog config + logs:
python3 replicate.py --rsyslog /etc/rsyslog.d/90-mailserver-kafka.conf
python3 replicate.py --rsyslog conf.conf --logs-dir ./samples   # override file paths
python3 replicate.py --file /var/log/postfix.log --program postfix   # one source
```

A runnable example lives in [`examples/`](examples/): `rsyslog_sample.conf` (a
template you can copy) and `samples/` (sample log files named to match it).

For each source it checks: would rsyslog **forward** that tag to Kafka at all; does
the engine **subscribe** to the Kafka topic rsyslog sends to; do rsyslog and the
engine point at the **same broker**; is there a **`program_mapping`** for that
`source_program`; and does the mapped rule's **regex actually match** the lines.
When a mapping is missing or a rule doesn't fit, it **auto-detects the correct
rule** from the sample and prints the exact `program_mapping` line to add — e.g.:

```
SOURCE  mail_apache_access   (/var/log/apache2/access.log)
   [OK] rsyslog forwards tag 'mail_apache_access' to Kafka
   [ERROR] no rule for source_program 'mail_apache_access' - engine would DLQ every line
   -> suggestion: these lines best match rule 'nginx_access' (15/15). Add to config.yaml:
        program_mapping:
          mail_apache_access: "nginx_access"
```

**Deploy-gate safe:** the exit code is trustworthy. Any `[ERROR]` — a broken
source *or* a pipeline-level fault like an rsyslog→engine **topic mismatch** —
makes `replicate.py` exit **1**; a clean run exits **0**. You can wire it into a
deploy script or CI: `python3 replicate.py --rsyslog ... && systemctl start foss-soc`.

This is the fastest way to debug a new log source end-to-end before any real logs
flow. Exit code `0` = every source is healthy.

### Manual Execution (Debug / Development)

Run the engine in the foreground:
```
sudo python3 main.py
```
Running as a System Service (Production)

Generate and enable the systemd service:
```
sudo ./setup_service.sh
```

Check service status:
```
sudo systemctl status foss-soc
```

View live logs:
```
journalctl -u foss-soc -f
```

---

## Web UI — browser console (no terminal)

Everything you can do from the command line — test a log file, add/test a parser,
edit and validate the config, ECS lookup, preflight, and a **live Monitor** of the
running engine — is also available as a point-and-click browser console. It reuses
the *real* engine code (never a second parser), so the UI and the CLI can never
disagree. Full walkthrough for non-technical operators: **[WEB_UI_GUIDE.md](WEB_UI_GUIDE.md)**.

**Start it (pick one):**

```bash
# A) Developers / Linux / macOS — from the repo:
pip install -r webui/requirements-ui.txt
python3 webui/app.py                     # then open http://127.0.0.1:8600

# B) Auto-venv launchers (installs Flask+PyYAML on first run):
./webui/start-soc-ui.sh                  # Linux / macOS
webui\Start-SOC-UI.bat                   # Windows (double-click)

# C) Standalone Windows app (no Python needed) — build once:
python webui/build_exe.py                # -> release/FOSS-SOC-UI/FOSS-SOC-UI.exe
```

**Secure by default — the login (audit-hardened):** there is **no built-in
`admin/admin`**. Credentials are resolved in priority order:

1. **A TLSOCDocker ELK `.env`** (point at it with `SOC_ENV_FILE=/path/to/.env` or
   `auth.env_file` in `config.yaml`) → log in with the **same `elastic` user and
   password you use for Kibana**. While an `.env` is found, the local login is disabled
   (no weaker back-door).
2. **`SOC_UI_USER` / `SOC_UI_PASSWORD`** environment variables → your own credentials.
3. **Otherwise, on first start** the console **generates a random password** for user
   `admin` and prints it **once** in the terminal window. It is stored
   salted-and-hashed in `.soc-ui-auth.json` next to the app — delete that file and
   restart to rotate it.

The engine start/stop/restart buttons in the Monitor are **off by default**; enable
with `SOC_UI_ALLOW_CONTROL=1` (Linux+systemd). The login travels over plain HTTP, so
run it on a trusted LAN or behind an HTTPS reverse proxy; it binds to `127.0.0.1` by
default (set `SOC_UI_HOST=0.0.0.0` to expose it — see WEB_UI_GUIDE §11). For local
development only, `SOC_UI_NO_AUTH=1` disables the login (it prints a loud warning).

| Env var | Default | Purpose |
|---|---|---|
| `SOC_UI_PORT` | `8600` | listen port |
| `SOC_UI_HOST` | `127.0.0.1` | bind address (`0.0.0.0` = reachable on the LAN) |
| `SOC_UI_NO_BROWSER` | — | set to `1` to not auto-open a browser |
| `SOC_UI_ALLOW_CONTROL` | — | set to `1` to enable engine start/stop/restart buttons |
| `SOC_LOG_DIR` | `<repo>/logs` | where the Monitor reads the engine's live stats (set if UI and engine are in different folders) |

---

## Use it inside Kibana (ELK plugin)

If your team already lives in Kibana, the **entire console can run as a native Kibana
plugin** — Test / Rules / Config / ECS / Monitor as a left-nav item, with **no
separate login** (Kibana authenticates you). It is an *optional* front-end; the
standalone Web UI above still works exactly the same.

How it fits together: the plugin's server side is a thin proxy that forwards
`/api/tlsoc_parser/*` to a headless copy of the same Flask backend running as a
container (`tlsoc-parser-ui`), bind-mounted to the **real engine's** `rules/`,
`config.yaml`, and `logs/`. So editing a rule in Kibana edits the production rule (the
engine hot-reloads it) and the Monitor shows the live engine's real metrics — one API
contract, never a second engine.

**Quick start** (full steps in [elk-plugin/INSTALL.md](elk-plugin/INSTALL.md);
requires a Kibana **8.19.12** build tree):

```bash
# 1. Build the backend image (from the repo root):
docker build -f elk-plugin/backend/Dockerfile -t tlsoc-parser-ui:1.0.0 .

# 2. Build the Kibana plugin against a Kibana 8.19.12 dev tree -> a folder/zip,
#    then drop it in ./kibana/installed_plugins/tlsocParser

# 3. Add the tlsoc-parser-ui service + kibana volume + TLSOCPARSER_BACKENDURL
#    to your compose (see elk-plugin/deploy/docker-compose.snippet.yml), then:
docker compose up -d --build tlsoc-parser-ui kibana
```

Then open **Kibana → left nav → TLSOC Parser**. Rebuild the backend image whenever the
engine's Python changes; the plugin proxy is generic, so new engine endpoints need no
plugin server change. Architecture and feature-parity table:
**[elk-plugin/README.md](elk-plugin/README.md)**.

---

## Production Deployment and Scaling

The engine is built to use every CPU core and to survive restarts without losing
logs. None of this changes how you write rules or map programs.

### Parallel workers (use all your cores)

On startup, `main.py` launches a pool of worker processes. Every worker joins the
**same Kafka consumer group**, so Kafka automatically spreads the topic partitions
across them. This is how one machine scales, and it is also how you scale across
several machines: run the engine on each box with the same `group_id`.

Control it in `config.yaml`:
```yaml
runtime:
  workers: auto   # auto = all CPU cores | 1 = single process (debug) | <N> = exact count
```
> The **shipped** `config.yaml` defaults to `workers: 1` — the friendliest
> setting for a first run and for debugging (single process, console logs).
> For production, change it to `auto`.

Or override per-deployment without editing config:
```bash
SOC_WORKERS=8 sudo systemctl restart foss-soc
```

**One rule to remember:** create at least as many **partitions** per Kafka topic as
you have total workers. A partition is only ever read by one worker, so extra
workers beyond the partition count sit idle.

```bash
# Example: 12 partitions so up to 12 workers (or machines) share the load
kafka-topics.sh --alter --topic web-logs --partitions 12 --bootstrap-server localhost:9092
```

**The per-source ceiling:** the example rsyslog config keys Kafka messages by
`%programname%`, so **all lines from one source land in one partition** — which
is exactly what stateful (transaction) correlation requires, but it also means
one source is processed by at most **one worker**. Many sources spread across
workers beautifully; a single monster source does not. If you need to shard one
enormous *stateless* source, key by something finer (e.g. hostname+program) —
but never split a *stateful* source across partitions, or transaction lines
will land on different workers and correlation breaks.

### The Kafka envelope contract (bring your own producer)

The engine consumes JSON messages of this shape — rsyslog, Filebeat, Vector,
or your own script can all produce it:

```json
{"meta": {"source_program": "nginx_access"}, "raw": "the original log line"}
```

- **`meta.source_program`** (required): the routing key — it is matched against
  `program_mapping` in `config.yaml` (falling back to a rule with exactly that
  name) to pick the parser.
- **`raw`** (required): the log line itself, as a JSON string.
- Anything else under `meta` (host, org, env, ...) is carried along untouched.

Example Vector sink producing the envelope:

```toml
[transforms.envelope]
type = "remap"
inputs = ["my_source"]
source = '''
. = {"meta": {"source_program": "nginx_access"}, "raw": .message}
'''

[sinks.kafka]
type = "kafka"
inputs = ["envelope"]
bootstrap_servers = "localhost:9092"
topic = "soc-logs"
key_field = "meta.source_program"   # keeps one source on one partition
encoding.codec = "json"
```

(Filebeat: use the `kafka` output with a `script`/`decode_json_fields`-style
processor building the same object, and `key: '%{[fields.program]}'`.)

**Offset semantics** (`kafka.auto_offset_reset`, default `latest`): a **fresh**
`group_id` starts at the newest message and skips any backlog — ideal for
"start clean, real-time". A group that has committed offsets **resumes where it
left off** and processes the gap. To force real-time again after downtime,
switch to a NEW `group_id`. Offsets are only ever committed after events are
flushed to disk (see below), so restarts re-process rather than lose.

### Delivery guarantees (no silent data loss)

Workers commit Kafka offsets **only after** a batch is safely written to disk
(at-least-once). On a crash or restart, the worker resumes from the last committed
offset, so in-flight logs are reprocessed rather than dropped. A clean
`systemctl stop` / `restart` flushes and commits before exiting.

For maximum durability on power loss, force every batch to physical disk:
```yaml
output:
  fsync: true    # slower, but survives a hard power cut
```

### Throughput expectations (measured, and how they were measured)

Honest numbers, with the hardware named — parsing-only micro-benchmarks, i.e.
raw line → ECS event in memory. They **exclude** Kafka consumption and disk
writes, which cost extra (batching keeps that overhead small but not zero):

- **Desktop i5-13400 (14 threads):** ~**90k-150k events/sec per core** on
  typical web access logs.
- **Power-limited laptop:** ~**45-60k events/sec per core** on the same rules,
  full parse path (regex + timestamp + enrichment with a warm GeoIP cache).

GeoIP-cold-cache, Redis-backed stateful rules, and very long lines all cost
more. Multiply per-core by your worker count for a machine estimate, then add
machines to the consumer group to go higher. Reaching a clean 1M EPS on one
box realistically also wants the optional C Kafka client
(`pip install confluent-kafka`). Benchmark YOUR rules on YOUR logs with
`python3 test_file.py <logfile> <rule>` before capacity planning.

### Performance knobs (all optional, all in `config.yaml`)

```yaml
batch:
  size: 1000          # events buffered before a disk write
  timeout_sec: 5      # max seconds before a partial batch is flushed/committed
kafka:
  max_poll_records: 2000
  fetch_max_bytes: 52428800
  max_partition_fetch_bytes: 1048576
output:
  rotate_mb: 0        # >0 to roll output files at a size; 0 = let logrotate/Filebeat do it
  dlq_rotate_mb: 200  # size cap per dead-letter file (logs/dlq/<source>.json); 0 = unbounded
  include_original: true  # false = drop event.original (raw line) -> ~half the ES storage
```

---

Development and Testing
Interactive Rule Tester

Test regex patterns and JSON mappings without Kafka ingestion:
```
python3 test_rules.py
```

Options:

Auto-detect: scans all rules to find a matching parser

Explicit parser selection for targeted testing

File-Based Testing

Process a file containing raw logs to validate bulk parsing behavior:
```
python3 test_file.py sample_logs.txt postfix
```

Auto-detect the best rule per line:
```
python3 test_file.py sample_logs.txt AUTO
```

Dump matched events as JSON:
```
python3 test_file.py sample_logs.txt postfix --show-success
```

Dump every parsed line with the matched rule:
```
python3 test_file.py sample_logs.txt AUTO --show-parsed
```

Dump every unparsed line with the reason (`no_match`, `buffered`, `errors`):
```
python3 test_file.py sample_logs.txt AUTO --show-unparsed
```

Tune sample size for each unparsed bucket in the summary:
```
python3 test_file.py sample_logs.txt postfix --samples 20
```

The script prints a summary with parsed vs unparsed counts, plus reason buckets
(`no_match`, `buffered`, `errors`) and sample lines for each bucket. In AUTO mode,
it also shows a per-rule parsed count to help identify gaps.

```
Directory Structure
├── config.yaml          # Main runtime configuration
├── core/
│   ├── engine.py        # Parsing strategies and execution engine
│   ├── registry.py      # Rule loading and routing logic
│   ├── schema.py        # Input validation and normalization
│   ├── output.py        # Buffered, per-worker output + DLQ writers
│   └── ecs_schema.py    # ECS field knowledge base + suggestion engine
├── rules/               # YAML parsing rule definitions (one per log source)
│   ├── apache.yaml
│   ├── nginx.yaml
│   ├── modsec.yaml
│   └── postfix.yaml
├── utils/
│   ├── geoip.py         # GeoIP enrichment (cached)
│   └── fastjson.py      # orjson with stdlib fallback
├── logs/                # Runtime logs (per worker: engine.wN.log, stats.wN.json)
│   ├── engine.log       # Engine logs
│   ├── dlq/             # Dead Letter Queue, one file per source (nginx.wN.json, postfix.wN.json, ...)
│   └── stats.json       # Health metrics (aggregated EPS across workers)
├── elasticsearch/       # Generated ES index template + loader doc (see its README)
├── tests/samples/       # Golden-sample exams, one folder per rule
├── ecs_helper.py        # ECS "autocorrect" for rule authors (check / fix / find)
├── preflight.py         # Pre-run validator: config + live Kafka/Redis/topics/network
├── replicate.py         # Dry-run the rsyslog->Kafka->engine pipeline locally (no Kafka)
├── test_config.py       # Static validator (config + rules + ECS + ReDoS lint)
├── test_*.py            # Regression battery (timestamps, enrichment, golden samples)
├── WRITING_RULES.md     # How to write/modify parsers (incl. AI master prompt)
└── main.py              # Application entry point (worker supervisor)
```
Monitoring

The engine writes live health metrics every `runtime.metrics_interval_sec`
(default 2 s) to `logs/stats.json`. When running multiple workers, each writes
its own `logs/stats.w<N>.json`, and the supervisor rolls them up into a single
`logs/stats.json` with the **combined** EPS across all workers. This file can
be ingested by external monitoring or SIEM agents (Filebeat, Wazuh).

Illustrative example of the aggregated shape (not a benchmark claim —
`errors_window` counts errors within the last `window_sec`-sized stats window):
```
{
  "timestamp": "2026-01-27T10:00:00",
  "workers": 12,
  "eps": 250000.0,
  "total_processed": 1500000000,
  "errors_window": 0
}
```

Per-worker log files follow the same pattern: `logs/engine.w<N>.log` and, for
dead-letters, one file **per source** per worker under
`logs/dlq/<source>.w<N>.json` (size-capped by `output.dlq_rotate_mb`). In
single-worker (debug) mode the files keep their plain names (`stats.json`,
`engine.log`, `logs/dlq/<source>.json`). Per-rule stats include an `expired`
counter — stateful transactions emitted because they timed out.
Writing and Updating Parsing Rules

Use this section when you need to support a new log source or refine an existing parser.

> **Full guide:** see **[WRITING_RULES.md](WRITING_RULES.md)** for a step-by-step,
> beginner-friendly walkthrough — including how to let an AI write a rule for you
> from raw log samples (the "master prompt").

**Every field a rule produces must be a valid ECS field.** You don't need to know
ECS by heart — the built-in helper checks your rule and tells you the correct
field name (like spell-check for log fields):

```bash
python3 ecs_helper.py check rules/myrule.yaml   # flag non-ECS fields + show the fix
python3 ecs_helper.py fix   rules/myrule.yaml   # auto-apply safe corrections
python3 ecs_helper.py find  "http status"       # search ECS fields by plain words
python3 ecs_helper.py                            # interactive lookup
```

The deploy validator (`python3 test_config.py`) runs the same ECS check, so a rule
with bad field names cannot ship. Genuine custom fields (where ECS has no
equivalent) are allowed.

### Generate a parser with AI (no regex or ECS knowledge needed)

The fastest way to add a new parser: copy the **master prompt** below into any AI
chatbot, paste **5–20 raw log lines** of your source
where it says so, and send. The AI returns a finished, ECS-compliant YAML rule.
Save it as `rules/<name>.yaml`, run `python3 ecs_helper.py check rules/<name>.yaml`
to confirm it's clean, add the `program_mapping` line it suggests, and restart.

> The same prompt also lives in **[WRITING_RULES.md](WRITING_RULES.md)** (§10).

````text
You are an expert log-parsing engineer for the "FOSS SOC Engine". Your job: read
the RAW LOG SAMPLES I paste at the end and output ONE ready-to-use YAML parser
rule for this engine. Output ONLY the YAML inside a single code block — no
explanation before or after.

# OUTPUT FORMAT
Produce a YAML file with these keys:
- pattern_name: a short unique snake_case name for this log source (string)
- strategy: one of stateless | multi_match | stateful | json_map | xml_xpath
- the strategy-specific keys (below)
- timestamp: where the event's REAL time lives in the log and how to parse it
  (see TIMESTAMP RULES). Include it whenever the log carries a time — almost always.
- mapping: maps each captured value to an ECS field (see ECS RULES)
- static: (optional) fixed ECS fields added to every event

Choose the strategy by the shape of the logs:
- stateless  : single-line, one consistent format. Keys: regex, mapping, static.
- multi_match: one source with several line formats. Keys: patterns: a list of
               {name, prematch, regex, mapping, static}. Order matters; first
               match wins.
- stateful   : one event spread over multiple lines sharing a transaction id.
               Keys: id_regex (MUST contain a named group `id`), end_signal
               (a substring that marks the final line), patterns: list of
               {prematch, regex, mapping, static}.
- json_map   : logs are JSON. mapping keys are dot-paths into the JSON; use `*`
               to walk every element of a list (e.g. items.*.id). Keys: mapping, static.
- xml_xpath  : logs are XML. Keys: items_xpath (element repeated per event),
               mapping where keys are element paths or `tag/@attr` for attributes.

# REGEX RULES
- Use Python `re` syntax with NAMED groups: (?P<name>...).
- Make patterns specific; escape literals; prefer [^"]* / \S+ over greedy .*.
- The group name (left side of mapping) is arbitrary; the ECS field (right side)
  must be valid ECS.
- PERFORMANCE: give every multi_match/stateful pattern a `prematch:` — a plain
  case-sensitive substring (NOT a regex) that is always present in lines the
  regex matches (e.g. prematch: "Failed password"). The engine checks it with
  a cheap `in` before running the regex; this is what keeps rules with many
  patterns fast. A list means any-of: prematch: ["timeout", "timed out"].

# TIMESTAMP RULES (critical — this drives Elasticsearch index routing)
The engine fills @timestamp from the `timestamp:` block. Without it, events are
stamped with parse-time instead of the event's real time and delayed logs land on
the wrong day. Whenever the log lines contain a date/time, capture it and declare:
  timestamp:
    group: <named regex group>   # regex strategies: capture the time in the pattern
    # or field: <dot.path>       # json_map: dot-path / xml_xpath: element path
    # or regex: '^(?P<ts>...)'   # independent regex on the raw line (e.g. syslog prefix)
    format: <see below>
    tz: "+05:30"                 # ONLY if the format has no zone AND the zone is known
format must be one of these named formats (match by the sample's shape) or an
explicit Python strptime string:
  clf         -> 09/Jul/2026:13:31:48 +0530     (apache/nginx access; AM/PM ok)
  iso8601     -> 2026-07-09T13:31:48.123456+05:30 or ...Z (also RFC5424 syslog)
  rfc3164     -> Jul  9 13:31:48                (classic syslog: no year, no zone)
  epoch       -> 1594282308                     (Unix seconds/ms/us, auto-detected)
  suricata    -> 07/09/2023-13:31:48.123456
  nginx_error -> 2026/07/09 13:31:48
  asctime     -> Tue Jul  9 13:31:48 2026       (ModSecurity)
  roundcube   -> 09-Jul-2026 13:31:48 +0530
For multi_match/stateful put one timestamp block at the TOP level; add a
per-pattern timestamp block only for patterns with a different time format.
Never map a time into "@timestamp" or "event.created" via mapping as a substitute
for this block — mapped values are raw unparsed strings.
Never emit tz as an abbreviation (IST/EST/CET are ambiguous and rejected);
use a numeric offset like "+05:30". If the samples show no timezone and none is
known, omit tz (the engine assumes UTC and tags the event log_assumed_utc).

# ECS RULES (critical)
Every value on the RIGHT side of mapping, and every key under static, MUST be a
valid Elastic Common Schema (ECS) field. Add `|int` or `|float` to the ECS field
to coerce numbers, e.g. "http.response.status_code|int".
Use these common ECS fields where they fit:
  source.ip, source.port, source.domain, destination.ip, destination.port,
  host.name, user.name, user_agent.original,
  http.request.method, http.response.status_code, http.request.referrer,
  http.response.body.bytes, url.path, url.query, url.domain,
  event.action, event.category, event.type, event.kind, event.outcome,
  event.reason, event.code, event.severity, event.created, log.level,
  network.protocol, network.transport, tls.version, tls.cipher,
  file.path, file.owner, file.mode, file.uid,
  process.pid, process.name, process.command_line,
  email.from.address, email.to.address, email.message_id,
  rule.id, rule.name, vulnerability.id, vulnerability.severity,
  vulnerability.score.base, observer.vendor, observer.product, service.type
Common fixes you must apply (never output the left form):
  srcip/client_ip -> source.ip ; dstip -> destination.ip ;
  status/status_code -> http.response.status_code ; method -> http.request.method ;
  useragent/ua -> user_agent.original ; referer -> http.request.referrer ;
  username -> user.name ; hostname -> host.name ; cve -> vulnerability.id ;
  proto -> network.protocol ; uri -> url.path
event.outcome must be one of: success, failure, unknown.
If — and only if — ECS has NO suitable field for a value, create a custom field
under a namespace named after the product (e.g. myapp.session_token). Never invent
new sub-fields inside ECS field sets like event.* or source.*.

# AFTER THE YAML
On the final commented line of the YAML, suggest the config.yaml program_mapping
entry, e.g.:  # program_mapping:  myapp_prod: "<pattern_name>"

Here are my RAW LOG SAMPLES:
<<< PASTE 5–20 RAW LOG LINES HERE >>>
````

Quick steps
1. Create or edit a YAML file in the rules/ directory.
2. Set `pattern_name` (must be unique). If missing, the file name is used.
3. Choose a `strategy` (see below) and define its fields.
4. Add `mapping` (and optional `static`) to normalize fields.
5. Map the source program to the rule in config.yaml.
6. Restart the service (or restart the process).

```
sudo systemctl restart foss-soc
```

Choosing the best strategy
- stateless: Best for consistent single-line logs (access logs, IDS alerts).
- multi_match: Best when one source emits multiple line formats (auth, ssh, sudo).
  Add a `prematch:` substring to every pattern — a cheap gate tried before the
  regex, which keeps rules with dozens/hundreds of patterns fast (a 500-pattern
  rule measures ~13x faster with prematch).
- stateful: Best for multi-line transactions correlated by ID. Also supports non-ID lines
  through pattern fallback (connect, TLS, disconnect, NOQUEUE).
- json_map: Best when raw logs are already JSON (WAF, cloud audit, app logs).
- xml_xpath: Best when raw logs are XML (scanner exports, XML audit feeds).

Common fields
- pattern_name: Name of the rule (used by program mapping).
- strategy: One of stateless, multi_match, stateful, json_map, xml_xpath.
- timestamp: (recommended) declares where the event's real time is in the log
  (`group`/`field`/`regex`) + its `format` (+ optional `tz`); the engine parses it
  into `@timestamp` normalized to UTC. Without it, events are stamped at parse
  time and tagged `event.timestamp_source: ingest_fallback`.
  See [WRITING_RULES.md](WRITING_RULES.md) §5.
- mapping: Maps regex group names or JSON/XML paths to ECS-like targets.
- static: Fixed fields added to every event.
- regex: Required for stateless.
- patterns: Required for multi_match and stateful.
- id_regex, end_signal: Required for stateful.
- items_xpath: Required for xml_xpath.

Mapping syntax notes
- Every strategy supports the type suffix: `field.path|int` or `field.path|float`
  (safe coercion — a non-numeric value stays a string, the event survives).
- These suffixes are the **only** value transforms the engine has: there is no
  lookup-table, string manipulation, or conditional logic in mappings. If a
  value needs reshaping beyond int/float, do it downstream (Logstash/ES ingest
  pipeline) or extract it differently in the regex.
- json_map uses dot paths and supports wildcards with `*` (returns a list).
- xml_xpath uses ElementTree paths and supports attributes via `/@`.
- Repeated mappings to the same field are merged into lists automatically.
- An optional regex group that does not participate in a match is omitted from
  the event (never emitted as `null`).

Examples

Stateless (single regex)
```yaml
pattern_name: "apache_access"
strategy: "stateless"
regex: '^(?P<ip>[\d\.]+) - - \[(?P<timestamp>[^\]]+)\] "(?P<method>\w+) (?P<path>[^\?\s]+)(?:\?(?P<query>[^\s]+))? HTTP/(?P<http_version>[\d\.]+)" (?P<status>\d+) (?P<body_bytes>\d+) "(?P<referrer>[^"]*)" "(?P<user_agent>[^"]*)"'
timestamp:                 # the event's REAL time -> @timestamp (UTC)
  group: timestamp
  format: clf
mapping:
  ip: "source.ip"
  method: "http.request.method"
  status: "http.response.status_code|int"
  body_bytes: "http.response.body.bytes|int"
  user_agent: "user_agent.original"
  referrer: "http.request.referrer"
  path: "url.path"
  query: "url.query"
static:
  event.category: "web"
```

Multi-match (multiple regexes)
```yaml
pattern_name: "linux_auth"
strategy: "multi_match"
patterns:
  - name: "ssh_success"
    regex: 'sshd\[\d+\]: Accepted password for (?P<user>\w+) from (?P<ip>[\d\.]+) port (?P<port>\d+)'
    mapping:
      user: "user.name"
      ip: "source.ip"
      port: "source.port"
    static:
      event.action: "login"
      event.outcome: "success"

  - name: "ssh_failed"
    regex: 'sshd\[\d+\]: Failed password for (invalid user )?(?P<user>\w+) from (?P<ip>[\d\.]+)'
    mapping:
      user: "user.name"
      ip: "source.ip"
    static:
      event.action: "login"
      event.outcome: "failure"
```

Stateful (transaction correlation with fallback)
```yaml
pattern_name: "postfix"
strategy: "stateful"
id_regex: '(?P<id>[A-Z0-9]{10,12}):'
end_signal: "removed"
state_ttl_sec: 300         # optional: how long a transaction may stay open (default 300);
                           # on timeout it is emitted with event.incomplete: true
timestamp:                 # event time = the transaction's FIRST line (rsyslog ISO prefix)
  regex: '^(?P<ts>\d{4}-\d{2}-\d{2}T\S+)'
  format: iso8601
patterns:
  - regex: 'client=(?P<host>.*?)\[(?P<ip>[\d\.]+)\]'
    mapping: { "ip": "source.ip" }

  - regex: 'from=<(?P<sender>[^@]+@(?P<s_domain>example\.com))>'
    mapping: { "sender": "email.from", "s_domain": "email.sender_domain" }
    static: { "email.sender_type": "internal" }

  # This will still parse connect/TLS/NOQUEUE lines without a queue id
  - regex: 'connect from (?P<host>.*?)\[(?P<ip>[\d\.]+)\]'
    mapping: { "ip": "source.ip" }
```

JSON map (direct field mapping)
```yaml
pattern_name: "modsec"
strategy: "json_map"
mapping:
  transaction.client_ip: "source.ip"
  transaction.request.method: "http.request.method"
  transaction.messages.*.details.ruleId: "rule.id"
  transaction.messages.*.message: "event.reason"
static:
  event.kind: "alert"
  event.category: "web"
  event.type: "waf"
```

XML XPath (structured XML)
```yaml
pattern_name: "openvas"
strategy: "xml_xpath"
items_xpath: ".//result"
mapping:
  nvt/@oid: "vulnerability.id"
  host: "destination.ip"
  severity: "event.severity|float"
static:
  event.category: "vulnerability"
```

Hooking a source program to a rule
Add a program mapping in config.yaml:
```yaml
program_mapping:
  postfix: "postfix"
  nginx_access_log: "apache_access"
  modsecurity_log: "modsec"
```

Testing your rule
- Interactive: `python3 test_rules.py`
- File-based: `python3 test_file.py sample_logs.txt AUTO`
- Golden exam: `python3 test_golden.py --update <rule>` then `python3 test_golden.py`

---

## License

FOSS SOC Engine is free and open source software, licensed under the
**Apache License 2.0** — see [LICENSE](LICENSE). You may use, modify, and
redistribute it freely (commercially or not); contributions are accepted
under the same license.

Note: the MaxMind GeoLite2 databases the engine can use for GeoIP/ASN
enrichment are **not** part of this project and are distributed by MaxMind
under their own license (free account required).


