# FOSS SOC Engine

A high-performance, polymorphic log parsing and normalization engine designed for Security Operations Centers (SOC).

The FOSS SOC Engine consumes raw logs from Kafka, dynamically routes them to the correct parser based on log metadata, and normalizes them into structured, ECS-compliant JSON. It supports stateless regex parsing, stateful multi-line log reassembly, and direct JSON field mapping for high-throughput environments.

---

## Core Capabilities

### Polymorphic Routing
Decouples log sources from parsing logic. Multiple source programs (for example, `mail_auth`, `web_auth`, `linux_auth`) can be routed to a single reusable rule definition via configuration-based program mapping.

### Stateful Parsing
Reassembles fragmented or multi-line logs (such as Postfix email transactions) into a single coherent event using Redis-backed correlation with TTL-based cleanup.

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

### Auto-Enrichment
Automatically enriches events with GeoIP metadata (city, country, latitude, longitude) for public IP addresses.

### Horizontal Scaling
Runs a pool of worker processes (one per CPU core by default) that share load
through a single Kafka consumer group. Scale up by adding cores or by running the
engine on more machines with the same `group_id` — no code or rule changes.

### Resilience and Observability
- Dead Letter Queue (DLQ) for logs that fail parsing
- At-least-once delivery: Kafka offsets are committed only after logs are written to disk
- Graceful shutdown: clean flush and commit on `systemctl stop`/restart
- Continuous health monitoring with throughput (EPS), error rate, and uptime tracking
- Optional `orjson` acceleration (installed automatically; falls back to stdlib if absent)

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
### GeoIP Database Requirement

This project uses the MaxMind GeoLite2 City database for IP enrichment.

Due to MaxMind licensing restrictions, the database is **not stored in the repository**.
Instead, it is automatically downloaded during installation.

Before running `install.sh`, export your MaxMind license key:

```bash
export MAXMIND_LICENSE_KEY=YOUR_MAXMIND_KEY
```

### 1. Clone the Repository

```bash
git clone https://github.com/sankettaware16/foss-soc-engine.git
sudo mv foss-soc-engine /etc/
cd /etc/foss-soc-engine
```
2. Run the Installer

The installer performs the following:

Installs Python dependencies

Creates runtime directories (logs/, database/)

Sets required permissions
```
chmod +x install.sh
./install.sh
```
3. Configure GeoIP Database

The engine requires the MaxMind GeoLite2 City database.

Download GeoLite2-City.mmdb from MaxMind

Place it in the database/ directory or you it can be directly installed using install.sh if you provide keys to it

```
mv /path/to/GeoLite2-City.mmdb ./database/
```

Configuration

Edit config.yaml to match your environment.
```
kafka:
  bootstrap_servers: ["localhost:9092"]
  input_topic: "^(syslog|waf-logs|.*)$"
  group_id: "soc-parser-v1"

paths:
  output_dir: "/var/log/soc_output/"
  rules_dir: "rules/"

program_mapping:
  ssh_server: "linux_auth"
  ftp_server: "linux_auth"
  modsec_audit: "modsec"

```
install and setup redis
```
sudo apt install redis-server -y
sudo systemctl enable redis-server
sudo systemctl start redis-server
```
create log dir
```
sudo mkdir -p /var/log/soc_output/
sudo chown -R username:username /var/log/soc_output/ #if required
```
Program mapping allows multiple source programs to reuse a single rule definition.

Usage

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

> `test_config.py` does the static config/rules/ECS checks; `preflight.py` reuses
> those and adds the live network / Kafka / topic / Redis / partition checks.

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

### Throughput expectations

Per CPU core, the engine sustains roughly **90k-150k events/sec** for typical web
access logs (less for GeoIP-on or Redis-backed stateful streams). Multiply by your
worker count for a machine estimate, then add machines to the consumer group to go
higher. To push a single box past a few hundred thousand EPS, install the optional
C-based Kafka client (`pip install confluent-kafka`) and keep GeoIP enabled so its
per-IP cache stays warm.

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
├── logs/                # Runtime logs (per worker: engine.wN.log, dlq.wN.json, stats.wN.json)
│   ├── engine.log       # Engine logs
│   ├── dlq.json         # Dead Letter Queue
│   └── stats.json       # Health metrics (aggregated EPS across workers)
├── ecs_helper.py        # ECS "autocorrect" for rule authors (check / fix / find)
├── preflight.py         # Pre-run validator: config + live Kafka/Redis/topics/network
├── replicate.py         # Dry-run the rsyslog->Kafka->engine pipeline locally (no Kafka)
├── test_config.py       # Static validator (config + rules + ECS compliance)
├── WRITING_RULES.md     # How to write/modify parsers (incl. AI master prompt)
├── CLAUDE.md            # Project memory for AI assistants (stable context)
├── journal.md           # Project diary (chronological history + decisions)
└── main.py              # Application entry point (worker supervisor)
```
Monitoring

The engine writes health metrics every 60 seconds to `logs/stats.json`.
When running multiple workers, each writes its own `logs/stats.w<N>.json`, and the
supervisor rolls them up into a single `logs/stats.json` with the **combined** EPS
across all workers. This file can be ingested by external monitoring or SIEM agents
(Filebeat, Wazuh).

Example (aggregated across workers):
```
{
  "timestamp": "2026-01-27T10:00:00",
  "workers": 12,
  "eps": 980500.0,
  "total_processed": 1500000000,
  "errors_last_min": 0
}
```

Per-worker log files follow the same pattern: `logs/engine.w<N>.log` and
`logs/dlq.w<N>.json`. In single-worker (debug) mode the files keep their plain
names (`stats.json`, `engine.log`, `dlq.json`).
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
chat (Claude, ChatGPT, Gemini, Grok…), paste **5–20 raw log lines** of your source
where it says so, and send. The AI returns a finished, ECS-compliant YAML rule.
Save it as `rules/<name>.yaml`, run `python3 ecs_helper.py check rules/<name>.yaml`
to confirm it's clean, add the `program_mapping` line it suggests, and restart.

> The same prompt also lives in **[WRITING_RULES.md](WRITING_RULES.md)** (§9).

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
- mapping: maps each captured value to an ECS field (see ECS RULES)
- static: (optional) fixed ECS fields added to every event

Choose the strategy by the shape of the logs:
- stateless  : single-line, one consistent format. Keys: regex, mapping, static.
- multi_match: one source with several line formats. Keys: patterns: a list of
               {name, regex, mapping, static}. Order matters; first match wins.
- stateful   : one event spread over multiple lines sharing a transaction id.
               Keys: id_regex (MUST contain a named group `id`), end_signal
               (a substring that marks the final line), patterns: list of
               {regex, mapping, static}.
- json_map   : logs are JSON. mapping keys are dot-paths into the JSON; use `*`
               to walk every element of a list (e.g. items.*.id). Keys: mapping, static.
- xml_xpath  : logs are XML. Keys: items_xpath (element repeated per event),
               mapping where keys are element paths or `tag/@attr` for attributes.

# REGEX RULES
- Use Python `re` syntax with NAMED groups: (?P<name>...).
- Make patterns specific; escape literals; prefer [^"]* / \S+ over greedy .*.
- The group name (left side of mapping) is arbitrary; the ECS field (right side)
  must be valid ECS.

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
- stateful: Best for multi-line transactions correlated by ID. Also supports non-ID lines
  through pattern fallback (connect, TLS, disconnect, NOQUEUE).
- json_map: Best when raw logs are already JSON (WAF, cloud audit, app logs).
- xml_xpath: Best when raw logs are XML (scanner exports, XML audit feeds).

Common fields
- pattern_name: Name of the rule (used by program mapping).
- strategy: One of stateless, multi_match, stateful, json_map, xml_xpath.
- mapping: Maps regex group names or JSON/XML paths to ECS-like targets.
- static: Fixed fields added to every event.
- regex: Required for stateless.
- patterns: Required for multi_match and stateful.
- id_regex, end_signal: Required for stateful.
- items_xpath: Required for xml_xpath.

Mapping syntax notes
- Regex strategies support type suffix: `field.path|int` or `field.path|float`.
- json_map uses dot paths and supports wildcards with `*` (returns a list).
- xml_xpath uses ElementTree paths and supports attributes via `/@`.
- Repeated mappings to the same field are merged into lists automatically.

Examples

Stateless (single regex)
```yaml
pattern_name: "apache_access"
strategy: "stateless"
regex: '(?P<ip>[\d\.]+) - - \[(?P<timestamp>[^\]]+)\] "(?P<method>\w+) (?P<path>[^\?\s]+)(?:\?(?P<query>[^\s]+))? HTTP/(?P<http_version>[\d\.]+)" (?P<status>\d+) (?P<body_bytes>\d+) "(?P<referrer>[^"]*)" "(?P<user_agent>[^"]*)"'
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


