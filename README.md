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

### Resilience and Observability
- Dead Letter Queue (DLQ) for logs that fail parsing
- Continuous health monitoring with throughput (EPS), error rate, and uptime tracking

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
Manual Execution (Debug / Development)

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

```
Directory Structure
├── config.yaml          # Main runtime configuration
├── core/
│   ├── engine.py        # Parsing strategies and execution engine
│   ├── registry.py     # Rule loading and routing logic
│   └── schema.py       # Input validation and normalization
├── rules/              # YAML parsing rule definitions
│   ├── apache.yaml
│   ├── linux_auth.yaml
│   ├── modsec.yaml
│   └── postfix.yaml
├── logs/               # Runtime logs
│   ├── engine.log      # Engine logs
│   ├── dlq.json        # Dead Letter Queue
│   └── stats.json      # Health metrics
└── main.py             # Application entry point
```
Monitoring

The engine writes health metrics every 60 seconds to logs/stats.json.
This file can be ingested by external monitoring or SIEM agents (Filebeat, Wazuh).

Example:
```
{
  "timestamp": "2026-01-27T10:00:00",
  "uptime_sec": 3600,
  "eps": 450.5,
  "total_processed": 1500000,
  "errors_last_min": 0
}
```
Adding New Parsing Rules

Create a new .yaml file in the rules/ directory

Define the parsing strategy (stateless, multi_match, stateful, or json_map)

Add regex patterns or JSON field mappings

Map the source program to the rule in config.yaml

Restart the service
```
sudo systemctl restart foss-soc
```


