# Upgrading an old deployment to the current version

This guide is for anyone running an **early copy of the engine** (no
`preflight.py`, no `test_config.py`, no Web UI, config without `runtime:` /
`output:` / `redis:` blocks) who wants to move to the current version
**without losing a single log line and without breaking the running system**.

The whole procedure was executed end-to-end on a live production deployment
(2026-07-16): total processing pause was under one minute, zero data loss,
and the shadow phase caught two real bugs before they could touch production.

**The strategy in one line:** backup → fresh clone *side-by-side* → migrate
config → *shadow-run against live traffic* → triage the DLQ → cut over →
keep the old install as instant rollback.

Never upgrade in place. The old install keeps running (and stays untouched)
until the new one has proven itself on your real logs.

---

## 0. Check the ground first

```bash
systemctl status foss-soc --no-pager | head -8   # what runs today, from where
git --version; python3 --version                 # need git + Python 3.10+
redis-cli ping                                   # PONG (needed by stateful rules, e.g. postfix)
df -h /opt                                       # a few GB free
```

## 1. Back up the current system

```bash
sudo mkdir -p /opt/backups
sudo tar -czf /opt/backups/foss-soc-engine-old-$(date +%F).tar.gz -C /etc foss-soc-engine
sudo cp /etc/systemd/system/foss-soc.service /opt/backups/foss-soc.service.old-$(date +%F)
```

(Adjust `/etc/foss-soc-engine` to wherever your old copy lives.)

## 2. Install the new version side-by-side

```bash
sudo mkdir -p /opt/foss-soc-engine && sudo chown $USER: /opt/foss-soc-engine
git clone https://github.com/sankettaware16/tlsoc-engine.git /opt/foss-soc-engine
cd /opt/foss-soc-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` includes **python-snappy** — if your Kafka producers use
snappy compression (rsyslog omkafka commonly does), the consumer dies with
`UnsupportedCodecError` without it. Old installs only worked because the
system Python happened to have it.

Copy your GeoIP databases (they are never in git):

```bash
mkdir -p database && cp /etc/foss-soc-engine/database/*.mmdb database/ 2>/dev/null
```

## 3. Migrate `config.yaml`

Start from the shipped `config.yaml` and port your old values:

| Old config | New config | Notes |
|---|---|---|
| `kafka.*` | same | bootstrap_servers, input_topic, auto_offset_reset unchanged |
| `kafka.group_id` | **use a NEW id for the shadow test** (e.g. `<old>-shadow`) | joining the old group would steal partitions from the running engine. You switch back at cutover. |
| — | `runtime.workers: 1` | **start with 1**: output filenames stay identical to the old engine (`postfix.json`, …). With N>1 they become `<module>.wN.json` and your Filebeat/shipper glob must change. Scale up later. |
| `paths.output_dir` | **a test dir for the shadow phase** (e.g. `/var/log/soc_output_test/`) | switched to the real dir at cutover |
| `program_mapping` | port every entry | **verify each rule name really exists** — `test_config.py` catches dangling names. (The reference migration found a mapping to a rule that never existed; that source had been silently dropped for months.) |
| — | `output:` block | `dlq_rotate_mb: 200` default is good; `include_original: false` halves ES storage if you don't need raw lines |
| — | `redis:` block | only needed by stateful rules; defaults to localhost:6379 |
| — | `auth.env_file` | Web UI login via your ELK `.env` (see README "Signing in") |
| `geoip` | same + optional `asn_db_path` | comment `asn_db_path` out if you don't have the ASN mmdb |

**Site-specific rule values:** old rule files had site values (e.g. mail
domains) hardcoded in regexes. Current rules take them as variables — edit the
`vars:` block at the top of `rules/postfix.yaml`:

```yaml
vars:
  internal_domains: ["your-domain.example"]
```

## 4. Validate before running anything

```bash
.venv/bin/python test_config.py --skip-kafka   # static: config, rules, ECS
.venv/bin/python preflight.py                  # live: Kafka topics, Redis, partitions
```

Both must pass. Fix what they report — that's the point of running them now.

## 5. Shadow soak (the safety net)

```bash
.venv/bin/python main.py     # foreground; old engine keeps running untouched
```

The shadow engine consumes the **same live traffic** in parallel (its own
consumer group starts at "latest"). Let it run 30+ minutes and watch:

```bash
ls -ltrh /var/log/soc_output_test/    # per-source files appear and grow
cat logs/stats.json                   # per-rule parsed / no_match / buffered counts
ls -ltrh logs/dlq/                    # dead-letters, one file per source
```

**Triage every DLQ file** — each entry carries the raw line that failed:

- `no_matching_rule` → the source isn't in `program_mapping`. Add it.
- `no_match` → a log shape the rule doesn't cover. Fix/extend the rule (the
  engine hot-reloads rule edits within ~10s — no restart) or accept the noise.
- A giant JSON line cut off mid-field → rsyslog truncation; raise
  `$MaxMessageSize` (e.g. `64k`) on the log-producing host.

Repeat until the error counters stay flat. Only then continue.

## 6. Cutover (about one minute, zero loss)

```bash
cd /opt/foss-soc-engine
# 1. Point at production: resume the OLD consumer group (its committed offsets
#    mean the stop→start gap is consumed, not skipped) and the real output dir
sed -i 's/<shadow-group-id>/<old-group-id>/; s|/var/log/soc_output_test/|/var/log/soc_output/|' config.yaml

# 2. Swap
sudo systemctl stop foss-soc
sudo mv /etc/foss-soc-engine /etc/foss-soc-engine.old
sudo mv /opt/foss-soc-engine /etc/foss-soc-engine

# 3. A venv hardcodes its absolute path - rebuild it at the final location
cd /etc/foss-soc-engine
rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 4. Point the service at the new install (note: venv python, not system python)
sudo tee /etc/systemd/system/foss-soc.service > /dev/null <<'EOF'
[Unit]
Description=FOSS SOC Parsing Engine
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/etc/foss-soc-engine
ExecStart=/etc/foss-soc-engine/.venv/bin/python3 /etc/foss-soc-engine/main.py
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload

# 5. Final check, then go
.venv/bin/python preflight.py
sudo systemctl start foss-soc
journalctl -u foss-soc -f
```

Verify: output files in the real dir grow again, `logs/stats.json` counts
climb, the DLQ stays quiet.

**Rollback** (any time, under a minute): stop the service, `mv` the two
directories back, restore the unit file from `/opt/backups`, daemon-reload,
start.

## 7. After the cutover

- **Renamed ECS fields** — update any dashboards/alerts using the old names:
  `email.from` → `email.from.address`, `email.to` → `email.to.address`,
  `file.owner_uid` → `file.uid`,
  `vulnerability.cvss.base_score` → `vulnerability.score.base`.
- **Timestamps**: `@timestamp` is now the event's real time when the rule can
  parse it (`event.timestamp_source: log`); `event.ingested` is always the
  parse time. Old versions only had ingest time.
- **Load the ES index template** (`elasticsearch/README.md`) so every field is
  typed correctly from the first document.
- Scale `runtime.workers` up (remember the shipper glob: `<module>*.json`).
- Clean up: the shadow test output dir, and — once you trust the new engine —
  `/etc/foss-soc-engine.old` (the tar in `/opt/backups` remains).
- The shadow consumer group left committed offsets in Kafka; it is harmless,
  or delete it with `kafka-consumer-groups --delete --group <shadow-id>`.

## What you gain (why this upgrade matters)

At-least-once delivery (no silent loss on crash/disk-full), one worker per
core, a per-source dead-letter queue (unparsed lines become visible instead of
vanishing), rule hot-reload, `prematch` fast-paths, site variables in rules,
real event timestamps, GeoIP+ASN enrichment, golden-sample tests + CI, the
browser Web UI, and the optional Kibana plugin (`elk-plugin/`).
