#!/usr/bin/env python3
"""
sources.py — one table, one row per log source: is it flowing and parsed?

For every Kafka topic the engine consumes (kafka.input_topic) plus every
source name a rule declares, it shows:

  topic        tlsoc.logs.<source> (or your legacy topic), partitions
  rate         messages/sec arriving right now (two samples, --sample secs)
  lag          messages waiting for the engine (consumer group kafka.group_id)
  parser       the rule chain this source resolves to (or NO PARSER)
  output       age of the last parsed event written (<source>.json)
  workers      which engine workers own which of its partitions (w0: 0,3)
  dlq          size of this source's dead-letter file (logs/dlq/<source>*)

and a STATUS: OK, WAITING (no data yet), NO PARSER, BEHIND,
DLQ (recent dead letters), IDLE (had data, nothing arriving now), SHARED (an
older shared topic whose lines route by their own source_program).

    python3 sources.py                 # table
    python3 sources.py --sample 10     # steadier rate numbers
    python3 sources.py --json          # machine-readable
    python3 sources.py --no-kafka      # rules / files only
    python3 sources.py --all           # also parsers whose source never sent

Exit code: 0 when no source is in NO PARSER or BEHIND, else 1.

The Web UI's Sources page uses collect() from this file, so both always agree.
"""
import argparse
import glob
import json
import os
import re
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core.registry import RuleRegistry, source_from_topic  # noqa: E402

BEHIND_LAG = 100000      # messages
RECENT_SEC = 900         # "recent" for output / DLQ activity


def human_age(sec):
    if sec is None:
        return "-"
    if sec < 90:
        return f"{int(sec)}s"
    if sec < 5400:
        return f"{int(sec // 60)}m"
    if sec < 172800:
        return f"{int(sec // 3600)}h"
    return f"{int(sec // 86400)}d"


def human_size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def newest(paths):
    """(age_seconds, total_bytes) over existing files, or (None, 0)."""
    now = time.time()
    stats = [os.stat(p) for p in paths if os.path.isfile(p)]
    if not stats:
        return None, 0
    return now - max(s.st_mtime for s in stats), sum(s.st_size for s in stats)


def kafka_view(cfg, sample_sec, prev=None):
    """{topic: {"partitions": n, "end": sum, "lag": n, "rate": msg/s}}

    The rate comes from two end-offset samples `sample_sec` apart. With
    sample_sec 0 and `prev` (an earlier result's {"t": time, "ends": {topic:
    end}}), it is measured against that earlier sample instead, without waiting."""
    from kafka import KafkaConsumer, KafkaAdminClient, TopicPartition
    k = cfg["kafka"]
    servers = k["bootstrap_servers"]
    topic_re = re.compile(k["input_topic"])
    consumer = KafkaConsumer(bootstrap_servers=servers, request_timeout_ms=15000,
                             api_version_auto_timeout_ms=10000)
    try:
        topics = sorted(t for t in consumer.topics() if topic_re.match(t))
        parts = {t: [TopicPartition(t, p) for p in sorted(consumer.partitions_for_topic(t) or [])]
                 for t in topics}
        all_tp = [tp for tps in parts.values() for tp in tps]
        end1 = consumer.end_offsets(all_tp) if all_tp else {}
        t0 = time.time()
        if sample_sec > 0 and all_tp:
            time.sleep(sample_sec)
        end2 = consumer.end_offsets(all_tp) if all_tp else {}
        dt = max(time.time() - t0, 1e-6)
    finally:
        consumer.close()
    committed = {}
    if k.get("group_id"):
        admin = KafkaAdminClient(bootstrap_servers=servers, request_timeout_ms=15000)
        try:
            for tp, om in (admin.list_consumer_group_offsets(k["group_id"]) or {}).items():
                committed[tp] = om.offset
        finally:
            admin.close()
    now = time.time()
    view = {}
    for t, tps in parts.items():
        end = sum(end2.get(tp, 0) for tp in tps)
        lag = sum(max(0, end2.get(tp, 0) - committed[tp]) for tp in tps if tp in committed)
        if sample_sec > 0:
            rate = (end - sum(end1.get(tp, 0) for tp in tps)) / dt
        elif prev and t in prev.get("ends", {}) and now > prev.get("t", now):
            rate = max(0, end - prev["ends"][t]) / (now - prev["t"])
        else:
            rate = None
        view[t] = {
            "partitions": len(tps),
            "end": end,
            "lag": lag if any(tp in committed for tp in tps) else None,
            "rate": rate,
        }
    return view


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def engine_workers(log_dir, max_age=60):
    """(workers, {topic: {"w0": [partitions], ...}}) of the running engine.

    Read from the heartbeat (logs/engine.pid) and each worker's stats file,
    which lists the partitions that worker owns. Files older than `max_age`
    seconds (a stopped engine, a dead worker) are ignored."""
    hb = read_json(os.path.join(log_dir, "engine.pid")) or {}
    n = int(hb.get("workers") or 0)
    if hb.get("role") == "supervisor" and n > 1:
        files = [os.path.join(log_dir, f"stats.w{i}.json") for i in range(n)]
    elif hb:
        files = [os.path.join(log_dir, "stats.json")]
    else:
        return 0, {}
    owners = {}
    now = time.time()
    for path in files:
        try:
            if now - os.stat(path).st_mtime > max_age:
                continue
        except OSError:
            continue
        st = read_json(path) or {}
        label = f"w{st.get('worker_id') or 0}"
        for topic, plist in (st.get("assignment") or {}).items():
            owners.setdefault(topic, {})[label] = plist
    return n, owners


def collect(cfg, base, sample_sec=5.0, use_kafka=True, prev=None, log_dir=None):
    """Everything the table shows, as data:
    {"kafka_error", "workers", "snapshot", "sources": [row, ...]}.
    `snapshot` can be passed back as `prev` to measure rates without waiting."""
    rules_dir = cfg.get("paths", {}).get("rules_dir", "rules/")
    rules_dir = rules_dir if os.path.isabs(rules_dir) else os.path.join(base, rules_dir)
    output_dir = cfg.get("paths", {}).get("output_dir", "/var/log/soc_output/")
    log_dir = log_dir or os.path.join(base, "logs")
    dlq_dir = os.path.join(log_dir, "dlq")

    import logging
    logging.getLogger("soc-engine").setLevel(logging.CRITICAL)
    registry = RuleRegistry(rules_dir, cfg.get("program_mapping") or {})

    kafka_error = None
    topics = {}
    if use_kafka:
        try:
            topics = kafka_view(cfg, sample_sec, prev)
        except Exception as e:  # unreachable broker must not hide the rest
            kafka_error = str(e) or e.__class__.__name__
    workers, owners = engine_workers(log_dir)

    known = dict(topics)
    for topic, by_worker in owners.items():  # no Kafka: the workers still name their topics
        if topic not in known:
            known[topic] = {"partitions": len({p for plist in by_worker.values() for p in plist}),
                            "end": None, "lag": None, "rate": None}

    rows = {}
    for topic, info in known.items():
        name = source_from_topic(topic)
        if name is None:  # an older shared topic: its lines route by their own tag
            rows[topic] = {"source": topic, "topic": topic, "shared": True, **info}
        else:
            rows[name] = {"source": name, "topic": topic, **info}
    for eng in registry.engines.values():
        src = (getattr(eng, "config", None) or {}).get("source")
        if src and src not in rows:
            rows[src] = {"source": src, "topic": None, "partitions": None,
                         "end": None, "lag": None, "rate": None}

    for name, r in sorted(rows.items()):
        procs = [] if r.get("shared") else registry.get_processors(name)
        r["parser"] = "(by source_program)" if r.get("shared") else (
            " > ".join(n for n, _ in procs) if procs else None)
        r["workers"] = owners.get(r["topic"], {}) if r["topic"] else {}
        out_age, out_size = newest(glob.glob(os.path.join(output_dir, f"{name}.json*")) +
                                   glob.glob(os.path.join(output_dir, f"{name}.w*.json*")))
        dlq_age, dlq_size = newest(glob.glob(os.path.join(dlq_dir, f"{name}.json*")) +
                                   glob.glob(os.path.join(dlq_dir, f"{name}.w*.json*")))
        r.update(output_age=out_age, output_bytes=out_size, dlq_age=dlq_age, dlq_bytes=dlq_size)
        has_data = bool(r["end"]) or out_age is not None
        if r.get("shared"):
            status = "BEHIND" if (r["lag"] or 0) > BEHIND_LAG else (
                "IDLE" if r["rate"] == 0 else "SHARED")
        elif not has_data:
            status = "WAITING"
        elif not r["parser"]:
            status = "NO PARSER"
        elif r["lag"] is not None and r["lag"] > BEHIND_LAG:
            status = "BEHIND"
        elif dlq_age is not None and dlq_age < RECENT_SEC:
            status = "DLQ"
        elif r["rate"] == 0 and (out_age is None or out_age > RECENT_SEC):
            status = "IDLE"
        else:
            status = "OK"
        r["status"] = status

    order = sorted(rows.values(), key=lambda r: (r["topic"] is None, r["source"]))
    snapshot = {"t": time.time(), "ends": {t: i["end"] for t, i in topics.items()}}
    return {"kafka_error": kafka_error, "workers": workers, "snapshot": snapshot,
            "sources": order}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--sample", type=float, default=5.0,
                    help="seconds between the two offset samples used for the rate (0 = skip)")
    ap.add_argument("--no-kafka", action="store_true", help="skip Kafka (rules and files only)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="also list parsers whose source has not sent a line yet (no topic)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config)) or {}
    base = os.path.dirname(os.path.abspath(args.config))
    data = collect(cfg, base, args.sample, use_kafka=not args.no_kafka)
    rows = data["sources"]
    worst = int(any(r["status"] in ("NO PARSER", "BEHIND") for r in rows))
    ready = [r["source"] for r in rows if r["status"] == "WAITING" and not r["topic"]]
    if not args.all:
        rows = [r for r in rows if not (r["status"] == "WAITING" and not r["topic"])]

    if args.json:
        data.pop("snapshot")
        print(json.dumps(data, indent=2, default=str))
        return worst

    if data["kafka_error"]:
        print(f"[WARN] Kafka not reachable ({data['kafka_error']}) - showing rules and files only\n")
    w = max([14] + [len(r["source"]) for r in rows])
    tw = max([24] + [len(r["topic"] or "") for r in rows])
    hdr = (f"{'SOURCE':<{w}} {'STATUS':<9} {'TOPIC':<{tw}} {'PART':>4} {'RATE/s':>7} {'LAG':>9}  "
           f"{'WORKERS':<8} {'PARSER':<30} {'OUTPUT':>6} {'DLQ':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        rate = "-" if r["rate"] is None else f"{r['rate']:.0f}"
        lag = "-" if r["lag"] is None else str(r["lag"])
        part = "-" if r["partitions"] is None else str(r["partitions"])
        dlq = human_size(r["dlq_bytes"]) if r["dlq_bytes"] else "-"
        wk = ",".join(sorted(r["workers"])) or "-"
        print(f"{r['source']:<{w}} {r['status']:<9} {(r['topic'] or '(no data yet)'):<{tw}} {part:>4} "
              f"{rate:>7} {lag:>9}  {wk:<8} {(r['parser'] or 'NO PARSER'):<30} "
              f"{human_age(r['output_age']):>6} {dlq:>7}")
    if not rows:
        print("(no source has sent a line yet; each source's topic appears with its first line)")
    if ready and not args.all:
        more = f" and {len(ready) - 12} more" if len(ready) > 12 else ""
        print(f"\n{len(ready)} more parsers ready, waiting for a first line (no topic until then): "
              f"{', '.join(ready[:12])}{more}  (--all lists them)")
    if data["workers"]:
        print(f"\nEngine workers: {data['workers']}")
    print("\nNO PARSER: add rules/<source>.yaml with `source: <source>` (docs/writing-rules.md); "
          "lines wait in logs/dlq/<source>.json.")
    return worst


if __name__ == "__main__":
    sys.exit(main())
