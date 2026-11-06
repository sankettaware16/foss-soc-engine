# Production Deployment and Scaling

The engine is built to use every CPU core and to survive restarts without losing
logs. None of this changes how you write rules or map programs.

## Parallel workers (use all your cores)

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
> setting for a first run and for debugging. For production, change it to `auto`.

Or override per-deployment without editing config:

```bash
SOC_WORKERS=8 sudo systemctl restart foss-soc
```

**One rule to remember:** a partition is only ever read by one worker, so the
partitions of all topics together cap how many workers can be busy. TLSOC uses
one topic per source (`tlsoc.logs.<source>`), created by the source's first
line with 1 partition. TLSOCDockerDeploy's cron job then raises busy sources to
their count in `kafka/topics.conf` (6 for squid and dns, 3 for other busy
sources); everything else stays at 1. Raise a source yourself with:

```bash
kafka-topics.sh --alter --topic tlsoc.logs.squid --partitions 12 --bootstrap-server localhost:9092
```

Measured on one laptop with the production source mix (squid 56 %, dns 37 %,
sso 5 %, rest 2 %): one shared single-partition topic and per-source topics both
drain ~15k events/s with 1 worker; per-source topics with 4 workers drain
~37k events/s. During a 400k-line squid burst, sso events waited up to 24 s
behind squid on the shared topic and ≤ 1.4 s on their own topic.

**Spreading one source over partitions:** `tlsoc-onboard.sh` sets
`partitions.auto="on"` on the omkafka action, so lines of one source spread over
all partitions of its topic and several workers share a busy source. (Without
it, omkafka writes every message to partition 0.) **Stateful** rules (postfix:
multi-line transactions correlated in Redis) need the lines of one transaction
in order, so their topic must keep **one** partition: the default for every new
topic. Never list such a source in `kafka/topics.conf` with more.

## The Kafka envelope contract (bring your own producer)

The engine consumes JSON messages of this shape — rsyslog, Filebeat, Vector, or
your own script can all produce it:

```json
{"meta": {"source_program": "nginx"}, "raw": "the original log line"}
```

- **`meta.source_program`** (required): the source name — the rule that
  declares it (`source:` / `aliases:`) parses it; `program_mapping` in
  `config.yaml` can override. On a `tlsoc.logs.<source>` topic the envelope is
  optional: a plain line is parsed as that source.
- **`raw`** (required): the log line itself, as a JSON string.
- Anything else under `meta` (host, org, env, …) is carried along untouched.

Example Vector sink producing the envelope:

```toml
[transforms.envelope]
type = "remap"
inputs = ["my_source"]
source = '''
. = {"meta": {"source_program": "nginx"}, "raw": .message}
'''

[sinks.kafka]
type = "kafka"
inputs = ["envelope"]
bootstrap_servers = "localhost:9092"
topic = "tlsoc.logs.nginx"           # one topic per source
encoding.codec = "json"
```

(Filebeat: use the `kafka` output with a `script`/`decode_json_fields`-style
processor building the same object, and `key: '%{[fields.program]}'`.)

The reference agentless producer — rsyslog + omkafka with a hardened,
rotation-safe configuration — is provided by
[TLSOC Docker Deploy — onboarding](https://github.com/sankettaware16/TLSOCDockerDeploy/blob/main/docs/onboarding.md).

## Delivery guarantees (no silent data loss)

Workers commit Kafka offsets **only after** a batch is safely written to disk
(at-least-once). On a crash or restart, the worker resumes from the last
committed offset, so in-flight logs are reprocessed rather than dropped. A clean
`systemctl stop` / `restart` flushes and commits before exiting.

For maximum durability on power loss, force every batch to physical disk:

```yaml
output:
  fsync: true    # slower, but survives a hard power cut
```

Additional guarantees:

- **Per-source dead letter queue** — unparseable logs land in
  `logs/dlq/<source>.json` with the failure reason. Files are size-capped
  (`output.dlq_rotate_mb`, default 200 MB) so a broken source can never fill the
  disk, and a rate-limited **"DLQ STORM" warning** fires in `engine.log` when one
  source dead-letters heavily (≥ 5000/min) — your signal that a rule or
  `program_mapping` broke.
- **Stateful transactions never vanish** — a multi-line transaction that never
  sees its end signal is emitted when its TTL runs out, tagged
  `event.incomplete: true` / `event.reason: transaction_timeout`, and counted per
  rule (`expired` in the stats and the Web UI Monitor).
- **Live rule reload** — editing a rule file in place is picked up within ~10
  seconds, no restart needed.

## Throughput expectations (measured, and how they were measured)

Honest numbers, with the hardware named — parsing-only micro-benchmarks, i.e.
raw line → ECS event in memory. They **exclude** Kafka consumption and disk
writes, which cost extra (batching keeps that overhead small but not zero):

- **Desktop i5-13400 (14 threads):** ~**90k–150k events/sec per core** on typical
  web access logs.
- **Power-limited laptop:** ~**45–60k events/sec per core** on the same rules,
  full parse path (regex + timestamp + enrichment with a warm GeoIP cache).

GeoIP-cold-cache, Redis-backed stateful rules, and very long lines all cost more.
Multiply per-core by your worker count for a machine estimate, then add machines
to the consumer group to go higher. Reaching a clean 1M EPS on one box
realistically also wants the optional C Kafka client
(`pip install confluent-kafka`).

**Don't estimate — measure your own deployment.** `benchmark.py` runs on the
machine, with the config and rules you actually deploy:

```bash
python3 benchmark.py               # per-rule EPS/core + parse latency (avg/p50/p95/p99),
                                   # blended mix, and a projection for your worker count
python3 benchmark.py --seconds 3   # longer window = steadier numbers
python3 benchmark.py --rule myrule --file /var/log/mysample.log   # YOUR logs

python3 benchmark.py --live        # the RUNNING pipeline: per-module lag
                                   # (event.ingested − @timestamp) from the output files

python3 benchmark.py --history --index "fosstlsoc-logs-squid-*" \
    --es https://localhost:9200 --user elastic --days 4 --interval 1h
                                   # RETROSPECTIVE: lag/EPS timeline computed by
                                   # Elasticsearch from the stored events — shows how
                                   # the pipeline behaved during a past traffic spike
                                   # or mass onboarding (a lag hump that recovers =
                                   # it caught up; lag that keeps growing = bottleneck)
```

The default mode answers "how much can this box parse" (slowest rules float to
the top — that's where optimization pays). `--live` answers "how far behind is
my data right now, and which module" — steady seconds-level lag is normal
batching; growing lag means the consumer is behind (more workers/partitions);
negative or UTC-offset-sized lag means a **source host's** clock or timezone
label is wrong, not the engine.

## Performance knobs (all optional, all in `config.yaml`)

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
  dlq_rotate_mb: 200  # size cap per dead-letter file; 0 = unbounded
  include_original: true  # false = drop event.original (raw line) → ~half the ES storage
```

## Monitoring

The engine writes live health metrics every `runtime.metrics_interval_sec`
(default 2 s) to `logs/stats.json`. When running multiple workers, each writes
its own `logs/stats.w<N>.json`, and the supervisor rolls them up into a single
`logs/stats.json` with the **combined** EPS across all workers. This file can be
ingested by external monitoring or SIEM agents (Filebeat, Wazuh).

Illustrative example of the aggregated shape (not a benchmark claim —
`errors_window` counts errors within the last `window_sec`-sized stats window):

```json
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

The [Web UI](web-ui-guide.md) Monitor renders the same stats live in a browser;
engine start/stop/restart buttons are off by default
(`SOC_UI_ALLOW_CONTROL=1` enables them).
