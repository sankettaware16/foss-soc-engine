# Development and Testing

The engine ships a complete validation toolchain: static checks, live
infrastructure checks, a local pipeline dry-run, interactive testers, and a CI
regression battery. Exit codes are trustworthy everywhere, so every tool can gate
a deploy script.

## Preflight

`preflight.py` validates everything that can stop the engine from working — in
one command — so you catch problems before going live instead of after:

```bash
python3 preflight.py                 # full check (config + live infrastructure)
python3 preflight.py --skip-live     # static checks only (no network calls)
python3 preflight.py --config /path/to/config.yaml --timeout 6
```

It checks, in order:

1. `config.yaml` exists and is structurally correct
2. paths (rules dir, output dir) and the GeoIP database
3. rules load and every regex compiles
4. every rule field is valid ECS
5. `program_mapping` points at rules that exist, and every rule's `source:` /
   `aliases:` name is valid and claimed by one rule only
6. the Kafka `host:port` is actually reachable **from this server** (raw TCP)
7. the broker really speaks Kafka, and your configured topics **exist** (with
   partition counts)
8. Redis is reachable — only required if you use any `stateful` rule
9. you have enough Kafka partitions for the number of workers

Exit code `0` = safe to start; non-zero = fix the reported errors first. Example
of a clean run:

```
=== 7. Kafka broker & topics ===
[OK] Kafka broker confirmed, 8 topic(s) on cluster
[OK] 3 topic(s) match input_topic: web-logs[12p], firewall-logs[12p], linux-logs[6p]

============================================================
  RESULT: PASS  -  0 errors, 0 warning(s)
  Safe to start:  sudo systemctl start foss-soc   (or  python3 main.py)
============================================================
```

> `test_config.py` does the static config/rules/ECS checks (plus a ReDoS lint on
> every regex); `preflight.py` reuses those and adds the live network / Kafka /
> topic / Redis / partition checks. Run `python3 test_config.py --skip-kafka` on
> machines with no broker in reach (CI does exactly that).

## Replicate — dry-run the full pipeline (no Kafka needed)

Before you point a real server at the engine, `replicate.py` mimics the
**entire** `rsyslog → Kafka → engine → ECS` flow **locally, with no Kafka and no
second server**. It reads your rsyslog imfile config to learn which log file
becomes which `source_program` (Tag), wraps a small sample (~50 lines) of each
file in the exact Kafka envelope your template produces, runs it through the real
parser, and tells you precisely where the pipeline would break.

```bash
# Try it immediately with the bundled example (no setup needed):
python3 replicate.py --rsyslog examples/rsyslog_sample.conf --logs-dir examples/samples

# Then point it at your own rsyslog config + logs:
python3 replicate.py --rsyslog /etc/rsyslog.d/90-mailserver-kafka.conf
python3 replicate.py --rsyslog conf.conf --logs-dir ./samples   # override file paths
python3 replicate.py --file /var/log/postfix.log --program postfix   # one source
```

A runnable example lives in [`examples/`](../examples/): `rsyslog_sample.conf`
(a template you can copy) and `samples/` (sample log files named to match it).

For each source it checks: would rsyslog **forward** that tag to Kafka at all;
does the engine **subscribe** to the Kafka topic rsyslog sends to; do rsyslog and
the engine point at the **same broker**; is there a **`program_mapping`** for
that `source_program`; and does the mapped rule's **regex actually match** the
lines. When a mapping is missing or a rule doesn't fit, it **auto-detects the
correct rule** from the sample and prints the exact `program_mapping` line to
add — e.g.:

```
SOURCE  mail_apache_access   (/var/log/apache2/access.log)
   [OK] rsyslog forwards tag 'mail_apache_access' to Kafka
   [ERROR] no rule for source_program 'mail_apache_access' - engine would DLQ every line
   -> suggestion: these lines best match rule 'nginx_access' (15/15). Add to config.yaml:
        program_mapping:
          mail_apache_access: "nginx_access"
```

**Deploy-gate safe:** any `[ERROR]` — a broken source *or* a pipeline-level fault
like an rsyslog→engine **topic mismatch** — makes `replicate.py` exit **1**; a
clean run exits **0**:
`python3 replicate.py --rsyslog ... && systemctl start foss-soc`.

## Source status — `sources.py`

One row per log source — the first thing to run when "logs are not showing up":

```bash
python3 sources.py              # table (samples the arrival rate over 5 s)
python3 sources.py --json       # for scripts / monitoring
python3 sources.py --no-kafka   # rules and files only
```

```
SOURCE   STATUS    TOPIC              PART  RATE/s   LAG  PARSER                        OUTPUT  DLQ
auth     OK        tlsoc.logs.auth       3      12     0  linux_auth                       2s    -
moodle   NO PARSER tlsoc.logs.moodle     3       4     0  NO PARSER                         -   6.4K
nginx    OK        tlsoc.logs.nginx      3     310     0  nginx_access > apache_access     1s    -
squid    WAITING   tlsoc.logs.squid      6       -     -  squid_proxy_access                -    -
```

`STATUS`: **OK**; **WAITING** (no data yet); **NO PARSER** (data arrives, no rule
declares this source — its lines are kept in `logs/dlq/<source>.json`; write a
rule with `source: <source>`); **BEHIND** (more than 100k messages waiting —
add workers/partitions); **DLQ** (recent dead letters — a rule does not match
every line); **IDLE** (had data, nothing arriving now). Exit code 1 when a
source is NO PARSER or BEHIND.

## Interactive rule tester

Test regex patterns and JSON mappings without Kafka ingestion:

```bash
python3 test_rules.py
```

Options: auto-detect (scans all rules to find a matching parser) or explicit
parser selection for targeted testing.

## File-based testing

Process a file containing raw logs to validate bulk parsing behavior:

```bash
python3 test_file.py sample_logs.txt postfix          # one rule
python3 test_file.py sample_logs.txt AUTO             # auto-detect per line
python3 test_file.py sample_logs.txt postfix --show-success   # dump matched events
python3 test_file.py sample_logs.txt AUTO --show-parsed       # every parsed line + rule
python3 test_file.py sample_logs.txt AUTO --show-unparsed     # every unparsed line + reason
python3 test_file.py sample_logs.txt postfix --samples 20     # bigger summary samples
```

The summary shows parsed vs unparsed counts, reason buckets (`no_match`,
`buffered`, `errors`) with sample lines, and — in AUTO mode — a per-rule parsed
count to identify gaps.

## ECS helper

```bash
python3 ecs_helper.py check rules/myrule.yaml   # flag non-ECS fields + show the fix
python3 ecs_helper.py fix   rules/myrule.yaml   # auto-apply safe corrections
python3 ecs_helper.py find  "http status"       # search ECS fields by plain words
python3 ecs_helper.py                           # interactive lookup
```

## The regression battery (CI)

CI ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) runs all of these on
every push and pull request, on Python 3.10 and 3.12:

| Suite | Proves |
|---|---|
| `test_config.py --skip-kafka` | config + rules + ECS validation + ReDoS lint |
| `test_timestamps.py` | every rule's `timestamp:` block parses its format, normalizes to UTC, and unparseable times fall back **visibly** (`ingest_fallback`) — 49 cases |
| `test_enrichment.py` | GeoIP + ASN enrichment plumbing, real-database lookups (auto-skip if the mmdb files are absent), and that `geoip.enabled: false` really disables both |
| `test_golden.py` | every rule's **golden-sample exam** (`tests/samples/<rule>/input.log` vs `expected.ndjson`): any rule edit that changes any answer fails with a diff |
| `test_sources.py` | source names and topic routing: every rule's `source:`/`aliases:`/`fallback:`, `program_mapping` still winning, name conflicts, plain lines and unknown tags routed by their `tlsoc.logs.<source>` topic, the old shared-topic behaviour unchanged, and that a newly created small topic is read from its first message while a big never-read topic is not replayed |

This is what makes rule contributions safe to accept: if a change to any rule
alters any golden-sample answer, the build fails with a diff. Refresh a golden
sample intentionally with `python3 test_golden.py --update <rule>` and review the
diff.

## Contributing workflow

1. Open an issue (bug or feature) before larger changes.
2. Branch from the default branch; keep changes focused.
3. For rule changes: run `ecs_helper.py check`, add/refresh the golden sample,
   and run the full battery locally.
4. Open a PR using the template — CI must pass.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full guide, and
[writing-rules.md](writing-rules.md) for rule authoring.
