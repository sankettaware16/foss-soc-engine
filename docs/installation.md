# Installation

Full installation guide for TLSOC Engine on a Linux host.

> **Already running an old version?** Don't install over it — follow
> [upgrading.md](upgrading.md): a battle-tested, zero-data-loss migration
> (backup → side-by-side shadow test against live traffic → one-minute cutover
> with instant rollback).

## Prerequisites

### Software

- Python 3.8+
- Apache Kafka (input source) — for example via
  [TLSOCDockerDeploy](https://github.com/sankettaware16/TLSOCDockerDeploy)
- Redis (required only for `stateful` parsing rules)

### System

- Linux environment — Ubuntu / Debian recommended for systemd service integration

## GeoIP + ASN database requirement (optional enrichment)

The engine uses two MaxMind GeoLite2 databases for IP enrichment. Both are
**totally offline**: a one-time download, then every lookup is a local file read
(no network calls at parse time, ever):

| Database | File | Adds to events |
|---|---|---|
| GeoLite2 **City** | `database/GeoLite2-City.mmdb` | `source.geo.*` (country, city, lat/lon) |
| GeoLite2 **ASN** | `database/GeoLite2-ASN.mmdb` | `source.as.number`, `source.as.organization.name` (which ISP/cloud/hosting company owns the IP) |

Due to MaxMind licensing restrictions, the databases are **not stored in the
repository**. `install.sh` downloads both automatically (the same free MaxMind
account/key works for both). Before running it, export your license key:

```bash
export MAXMIND_LICENSE_KEY=YOUR_MAXMIND_KEY
```

Manual download (if you skip install.sh): log in at maxmind.com → *Download Files*
→ grab **GeoLite2 City** and **GeoLite2 ASN** (mmdb format) and place the two
`.mmdb` files in the `database/` folder. MaxMind updates weekly; re-download
whenever you like — the engine picks the new file up on restart.

Enable / disable in `config.yaml` — no code changes:

```yaml
geoip:
  enabled: true                              # false = skip BOTH lookups
  db_path: "database/GeoLite2-City.mmdb"     # geo (country/city/coords)
  asn_db_path: "database/GeoLite2-ASN.mmdb"  # ASN (IP owner) — comment out to run City-only
```

A missing file or a missing `geoip2` library never crashes the engine — that
enrichment is simply skipped (and `preflight.py` warns you about it).

## Step 1 — Clone the repository

```bash
git clone https://github.com/sankettaware16/foss-soc-engine.git
sudo mv foss-soc-engine /etc/
cd /etc/foss-soc-engine
```

## Step 2 — Run the installer

The installer performs exactly two things:

- Installs the Python dependencies (`pip3 install -r requirements.txt`)
- Creates the runtime directories (`logs/`, `database/`) — and, if you exported
  `MAXMIND_LICENSE_KEY`, downloads the GeoIP databases into `database/`

It does **not** change file permissions or install a service — use
`setup_service.sh` for systemd (step 6).

```bash
chmod +x install.sh
./install.sh
```

## Step 3 — Configure the engine

Edit `config.yaml` to match your environment. The shipped file is already a
working template — you mainly change the Kafka connection and `program_mapping`:

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

`program_mapping` lets multiple source programs reuse a single rule (or a list of
rules — see [chains](writing-rules.md)). The full reference for every block is in
[configuration.md](configuration.md).

## Step 4 — Install Redis *(only for `stateful` rules, e.g. postfix)*

```bash
sudo apt install redis-server -y
sudo systemctl enable redis-server
sudo systemctl start redis-server
```

Point the engine at a non-local Redis with the `redis:` block in `config.yaml`.

## Step 5 — Create the output directory

```bash
sudo mkdir -p /var/log/soc_output/
sudo chown -R username:username /var/log/soc_output/   # if required
```

## Step 6 — Run it

Pre-flight check first — it validates config, rules, ECS fields, Kafka
reachability, topics, Redis, and partition counts in one command
(see [development.md](development.md#preflight)):

```bash
python3 preflight.py
```

Then either run in the foreground (debug):

```bash
sudo python3 main.py
```

or install the systemd service (production):

```bash
sudo ./setup_service.sh
sudo systemctl status foss-soc     # the service keeps its historical name
journalctl -u foss-soc -f
```

## Step 7 — (Recommended) Load the Elasticsearch index template

If you ship to Elasticsearch, load the bundled index template **before the first
event is indexed**, so every field gets the right type (dates, IPs, `geo_point`,
numbers) from day one and mapping conflicts can't happen. Full instructions:
[`elasticsearch/README.md`](../elasticsearch/README.md).

## Optional front-ends

- **Web UI** — browser console for testing logs, editing rules/config, and live
  monitoring: [web-ui-guide.md](web-ui-guide.md). Quick start:

  ```bash
  pip install -r webui/requirements-ui.txt
  python3 webui/app.py            # open http://127.0.0.1:8600
  ```

  Sign-in credentials are resolved securely by default — see
  [configuration.md — Web UI authentication](configuration.md#web-ui-authentication).

- **Kibana plugin** — the same console as a native Kibana left-nav app:
  [elk-plugin/INSTALL.md](../elk-plugin/INSTALL.md).
