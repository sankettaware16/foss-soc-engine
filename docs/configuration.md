# Configuration Reference

Everything the engine does is controlled by `config.yaml` (plus a few optional
environment variables). This page is the reference; [installation.md](installation.md)
covers the minimal first-run setup.

## Kafka

```yaml
kafka:
  bootstrap_servers: ["localhost:9092"]
  input_topic: "soc-logs"          # a single topic, OR a regex like "linux|firewall|web"
  group_id: "soc-parser-group"
  auto_offset_reset: "latest"
  # performance (optional):
  max_poll_records: 2000
  fetch_max_bytes: 52428800
  max_partition_fetch_bytes: 1048576
```

**Offset semantics** (`auto_offset_reset`, default `latest`): a **fresh**
`group_id` starts at the newest message and skips any backlog — ideal for "start
clean, real-time". A group that has committed offsets **resumes where it left
off** and processes the gap. To force real-time again after downtime, switch to a
NEW `group_id`. Offsets are only ever committed after events are flushed to disk,
so restarts re-process rather than lose.

Optional: `pip install confluent-kafka` switches to the C Kafka client for the
highest throughput; the pure-Python client is used otherwise.

## Paths

```yaml
paths:
  output_dir: "/var/log/soc_output/"
  rules_dir: "rules/"
```

## Program mapping (routing)

```yaml
program_mapping:
  ssh_server: "linux_auth"          # many programs → one reusable rule
  ftp_server: "linux_auth"
  modsec_audit: "modsec"
  webserver01: ["nginx_access", "php_errors"]   # a CHAIN: first rule that handles a line wins
```

`meta.source_program` from the Kafka envelope is looked up here (falling back to
a rule with exactly that name) to pick the parser. Chains let one messy source be
tried against several rules in order. See [writing-rules.md](writing-rules.md).

## Runtime (workers)

```yaml
runtime:
  workers: auto   # auto = all CPU cores | 1 = single process (debug) | <N> = exact count
  metrics_interval_sec: 2
```

The **shipped** config defaults to `workers: 1` — the friendliest setting for a
first run and debugging. For production, change it to `auto`. Override
per-deployment without editing config:

```bash
SOC_WORKERS=8 sudo systemctl restart foss-soc
```

Partition math and scaling guidance: [deployment.md](deployment.md).

## Batching and output

```yaml
batch:
  size: 1000              # events buffered before a disk write
  timeout_sec: 5          # max seconds before a partial batch is flushed/committed

output:
  fsync: true             # force every batch to physical disk (survives power cuts; slower)
  rotate_mb: 0            # >0 = roll output files at a size; 0 = let logrotate/Filebeat do it
  dlq_rotate_mb: 200      # size cap per dead-letter file (logs/dlq/<source>.json); 0 = unbounded
  include_original: true  # false = drop event.original (raw line) → ~half the ES storage
```

## GeoIP / ASN enrichment

```yaml
geoip:
  enabled: true                              # false = skip BOTH lookups
  db_path: "database/GeoLite2-City.mmdb"     # → source.geo.* (country, city, lat/lon)
  asn_db_path: "database/GeoLite2-ASN.mmdb"  # → source.as.* — comment out to run City-only
```

Databases and licensing: [installation.md](installation.md#geoip--asn-database-requirement-optional-enrichment).
A missing file or library never crashes the engine — enrichment is skipped and
`preflight.py` warns.

## Redis (stateful rules)

Required only when any rule uses `strategy: stateful`. Defaults to localhost;
point at a remote instance with:

```yaml
redis:
  host: "127.0.0.1"
  port: 6379
```

Per-rule transaction lifetime is set in the rule itself (`state_ttl_sec`,
default 300 s); timed-out transactions are emitted with
`event.incomplete: true`, never dropped.

## Web UI authentication

There is **no built-in `admin/admin`** — the console is secure by default.
Credentials are resolved in this priority order (first match wins):

| Priority | Source | You log in as | Set it with |
|---|---|---|---|
| 1 | **Your ELK stack's `.env`** | `elastic` + your `ELASTIC_PASSWORD` (same as Kibana) | `auth.env_file` in config.yaml **or** the `SOC_ENV_FILE` env var |
| 2 | **Fixed credentials** | whatever you choose | `SOC_UI_USER` + `SOC_UI_PASSWORD` env vars |
| 3 | **Generated (default)** | `admin` + a random password created on first start | nothing — automatic |

**Option 1 — use your ELK login (recommended on a stack host).** Note that
`env_file` must be indented **under** `auth:` (a top-level `env_file:` is accepted
as a fallback, but the nested form is the documented one):

```yaml
auth:
  env_file: "/opt/tlsoc-docker-deploy/.env"   # any .env containing ELASTIC_PASSWORD
```

Restart the UI and confirm which credential source it picked — the startup log
always says:

```bash
sudo systemctl restart foss-soc-ui
journalctl -u foss-soc-ui -n 40 --no-pager | grep '\[auth\]'
#  -> [auth] credentials from elk-env:/opt/tlsoc-docker-deploy/.env (user 'elastic')
```

While an ELK `.env` is in use, the generated local login is **disabled** (no
weaker back-door). If the configured path is missing or contains no
`ELASTIC_PASSWORD`, the UI prints a loud `[auth] WARNING` at startup explaining
the fallback. After rotating the elastic password, restart the UI — the `.env` is
re-read on every start.

**Option 3 — the generated password (out-of-the-box default).** On first start
the console prints a banner with a random password for user `admin`, stored
salted-and-hashed in `.soc-ui-auth.json` next to the app. Read it again later
with `journalctl -u foss-soc-ui --no-pager | grep 'password:'`; forgot it or want
a new one — delete `.soc-ui-auth.json` and restart.

> **Running under systemd?** Keep `Environment=PYTHONUNBUFFERED=1` in the unit
> (the shipped [`webui/foss-soc-ui.service`](../webui/foss-soc-ui.service) has
> it) — without it the `[auth]` line / first-run password may not show up in
> `journalctl` right away.

### Web UI environment variables

| Env var | Default | Purpose |
|---|---|---|
| `SOC_UI_PORT` | `8600` | listen port |
| `SOC_UI_HOST` | `127.0.0.1` | bind address (`0.0.0.0` = reachable on the LAN) |
| `SOC_UI_NO_BROWSER` | — | set to `1` to not auto-open a browser |
| `SOC_UI_ALLOW_CONTROL` | — | set to `1` to enable engine start/stop/restart buttons (Linux + systemd) |
| `SOC_UI_NO_AUTH` | — | set to `1` to disable login (local development only; loud warning) |
| `SOC_ENV_FILE` | — | path to an ELK `.env` for credentials (same as `auth.env_file`) |
| `SOC_LOG_DIR` | `<repo>/logs` | where the Monitor reads the engine's live stats |

The login travels over plain HTTP, so run the UI on a trusted LAN or behind an
HTTPS reverse proxy; it binds to `127.0.0.1` by default. Full walkthrough:
[web-ui-guide.md](web-ui-guide.md).

## Validation

Whatever you change, validate before restarting:

```bash
python3 test_config.py --skip-kafka   # static: config + rules + ECS + ReDoS lint
python3 preflight.py                  # static + live Kafka/topics/Redis/partitions
```
