#!/usr/bin/env python3
"""
benchmark.py — measure THIS deployment's parsing capacity and latency.

Two questions, two modes:

1. "How many EPS can my setup handle?"  (default mode)
   Runs every rule against its own sample corpus (tests/samples/<rule>/
   input.log) on one core and reports events/sec plus per-event parse
   latency (avg/p50/p95/p99). Then projects aggregate capacity from
   runtime.workers in config.yaml. No Kafka or Redis needed — stateful
   rules run against an in-memory Redis stand-in (real Redis adds a
   network round-trip per line; the report says so).

       python3 benchmark.py                 # all rules, ~1s each
       python3 benchmark.py --seconds 3     # steadier numbers
       python3 benchmark.py --rule nginx_access
       python3 benchmark.py --rule myrule --file /var/log/mysample.log

2. "How far behind is my LIVE pipeline, and where?"  (--live)
   Reads the tail of each module's output NDJSON in paths.output_dir and
   reports, per module, the pipeline lag  event.ingested − @timestamp
   (that difference spans rsyslog → Kafka → engine batch, so it is the
   end-to-end freshness of your data) and the event.timestamp_source mix.
   Negative lag beyond a couple of seconds means a SOURCE host's clock or
   timezone label is wrong — the engine is not slow, the log is lying.

       python3 benchmark.py --live
       python3 benchmark.py --live --sample 2000

3. "How did the pipeline perform LAST week / during that big onboarding?"
   (--history)  Every indexed event stores both @timestamp and
   event.ingested, so Elasticsearch can reconstruct the lag timeline
   after the fact: per time bucket, event rate (EPS) and lag avg/p95/max.
   A mass onboarding that briefly outran the pipeline shows up as a lag
   hump that then recovers; a real bottleneck shows lag growing and
   never recovering.

       python3 benchmark.py --history --index "fosstlsoc-logs-squid-*" \
           --es https://localhost:9200 --user elastic --days 4
       (password: --password, env ES_PASSWORD, or interactive prompt)
"""
import argparse
import fnmatch
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import yaml
import core.engine as _engine_mod
from core.engine import UniversalEngine
from core.schema import LogInput

RULES_DIR = os.path.join(HERE, "rules")
SAMPLES_DIR = os.path.join(HERE, "tests", "samples")
CONFIG_PATH = os.path.join(HERE, "config.yaml")


class FakeRedis:
    """In-memory stand-in so stateful rules bench without a server
    (same behavior contract as the one test_golden.py uses)."""
    def __init__(self):
        self.store = {}
    def get(self, k):
        return self.store.get(k)
    def set(self, k, v, ex=None):
        self.store[k] = v
    def delete(self, k):
        self.store.pop(k, None)
    def scan_iter(self, match=None, count=None):
        for k in list(self.store):
            if match is None or fnmatch.fnmatch(k, match):
                yield k
    def ttl(self, k):
        return 300
    def eval(self, script, numkeys, key, *args):
        if "EXISTS" in script and "SET" in script:
            if key in self.store:
                self.store[key] = args[0]
                return 1
            return 0
        return self.store.pop(key, None)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def cpu_model():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def resolved_workers(config):
    w = ((config.get("runtime") or {}).get("workers", "auto"))
    if str(w).lower() == "auto":
        return os.cpu_count() or 1
    try:
        return max(1, int(w))
    except (TypeError, ValueError):
        return 1


def percentile(sorted_ns, pct):
    if not sorted_ns:
        return 0
    idx = min(len(sorted_ns) - 1, int(len(sorted_ns) * pct / 100.0))
    return sorted_ns[idx]


# --------------------------------------------------------------------------- #
# Mode 1: synthetic throughput + parse latency per rule
# --------------------------------------------------------------------------- #

def bench_rule(rule, raw_lines, seconds):
    """Run raw_lines through a fresh engine repeatedly for ~`seconds`,
    timing each process() call. Returns a stats dict or None."""
    program = rule.get("pattern_name", "x")
    envelopes = [
        json.dumps({"meta": {"source_program": program}, "raw": ln})
        for ln in raw_lines
    ]
    if not envelopes:
        return None

    _engine_mod.r = FakeRedis()          # fresh transaction state per rule
    engine = UniversalEngine(rule)
    if getattr(engine, "disabled", False):
        return None

    # Warmup pass: regex/GeoIP caches, and the corpus parse quality stats.
    events = no_match = buffered = 0
    for m in envelopes:
        out = engine.process(LogInput(m))
        if out is not None:
            events += 1
        elif getattr(engine, "last_buffered", False):
            buffered += 1
        else:
            no_match += 1

    lat_ns = []
    t_end = time.perf_counter() + seconds
    clock = time.perf_counter_ns
    n = len(envelopes)
    i = 0
    while time.perf_counter() < t_end:
        m = envelopes[i % n]
        i += 1
        t0 = clock()
        engine.process(LogInput(m))
        lat_ns.append(clock() - t0)

    total_s = sum(lat_ns) / 1e9
    lat_ns.sort()
    return {
        "lines": len(envelopes),
        "events": events, "no_match": no_match, "buffered": buffered,
        "timed": len(lat_ns),
        "eps": len(lat_ns) / total_s if total_s else 0.0,
        "avg_us": (sum(lat_ns) / len(lat_ns)) / 1000.0,
        "p50_us": percentile(lat_ns, 50) / 1000.0,
        "p95_us": percentile(lat_ns, 95) / 1000.0,
        "p99_us": percentile(lat_ns, 99) / 1000.0,
        "stateful": rule.get("strategy") == "stateful",
    }


def run_synthetic(args, config):
    try:
        from utils import fastjson
        orjson_on = "orjson" in str(getattr(fastjson, "dumps", "")).lower() or \
                    getattr(fastjson, "orjson", None) is not None
    except Exception:
        orjson_on = False

    workers = resolved_workers(config)
    print("=" * 74)
    print("  FOSS SOC Engine — capacity benchmark (this machine, this config)")
    print("=" * 74)
    print(f"  CPU            : {cpu_model()}")
    print(f"  Cores          : {os.cpu_count()}   configured workers: {workers}")
    print(f"  Python         : {sys.version.split()[0]}   orjson: {'yes' if orjson_on else 'NO (pip install orjson for ~2x serialize)'}")
    print(f"  Measure window : {args.seconds:.1f}s per rule   (parse latency = engine only;")
    print(f"                   output write ≈ +1µs/event, Kafka client overhead excluded)")
    print(f"  How to read    : latency = µs per event. p50 = the typical (median) event;")
    print(f"                   p95/p99 = the slowest 1-in-20 / 1-in-100 events.")
    print()

    results = []
    for fname in sorted(os.listdir(RULES_DIR)):
        if not fname.endswith(".yaml"):
            continue
        with open(os.path.join(RULES_DIR, fname), encoding="utf-8") as fh:
            rule = yaml.safe_load(fh)
        name = rule.get("pattern_name", fname[:-5])
        if args.rule and args.rule != name:
            continue

        if args.file and args.rule == name:
            corpus_path = args.file
        else:
            corpus_path = os.path.join(SAMPLES_DIR, name, "input.log")
        if not os.path.isfile(corpus_path):
            print(f"  {name:<22} (skipped: no sample corpus at tests/samples/{name}/input.log)")
            continue
        with open(corpus_path, encoding="utf-8") as f:
            raw_lines = [l.rstrip("\n") for l in f if l.strip()]

        stats = bench_rule(rule, raw_lines, args.seconds)
        if stats is None:
            print(f"  {name:<22} (skipped: rule disabled or empty corpus)")
            continue
        stats["name"] = name
        results.append(stats)

    if not results:
        print("Nothing measured.")
        return

    print(f"  {'RULE':<22}{'EPS/core':>10}{'avg µs':>9}{'p50':>8}{'p95':>8}{'p99':>8}   notes")
    print("  " + "-" * 70)
    for s in sorted(results, key=lambda x: -x["eps"]):
        notes = []
        if s["stateful"]:
            notes.append("stateful: in-mem redis; real Redis adds RTT/line")
        if s["no_match"]:
            notes.append(f"{s['no_match']}/{s['lines']} corpus lines no_match")
        print(f"  {s['name']:<22}{s['eps']:>10,.0f}{s['avg_us']:>9.1f}"
              f"{s['p50_us']:>8.1f}{s['p95_us']:>8.1f}{s['p99_us']:>8.1f}   {'; '.join(notes)}")

    blended = sum(s["timed"] for s in results) / sum(
        s["timed"] / s["eps"] for s in results if s["eps"])
    print("  " + "-" * 70)
    print(f"  Blended (equal-weight mix of the rules above): ~{blended:,.0f} EPS/core")
    print(f"  Projection with {workers} worker(s): ~{blended * workers:,.0f} EPS aggregate")
    print()
    print("  Caveats for the projection:")
    print("   - Kafka topics need >= as many partitions as workers, or extras idle.")
    print("   - Your real traffic mix matters: weight by YOUR per-source volumes.")
    print("   - Stateful rules pay a real-Redis round trip per line and share one")
    print("     Redis across workers; heavy stateful load scales sub-linearly.")

    print_utilization(results, blended, workers)


def _bar(pct, width=24):
    filled = min(width, max(1 if pct > 0 else 0, round(pct / 100 * width)))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def print_utilization(results, blended, workers):
    """Compare live engine load (logs/stats.json, written every couple of
    seconds by main.py) against the capacity just measured."""
    log_dir = os.environ.get("SOC_LOG_DIR") or os.path.join(HERE, "logs")
    stats_path = os.path.join(log_dir, "stats.json")
    try:
        with open(stats_path, encoding="utf-8") as f:
            stats = json.load(f)
    except (OSError, ValueError):
        print()
        print(f"  Utilization: engine not running here (no {stats_path}) —")
        print("  start it and re-run to see live load vs this capacity.")
        return

    now_eps = float(stats.get("eps") or 0.0)
    uptime = max(1, int(stats.get("uptime_sec") or 0))
    ts = parse_ts(stats.get("timestamp"))
    age = None
    if ts:
        age = (datetime.now(ts.tzinfo) - ts).total_seconds()

    capacity = blended * workers
    overall = min(999.9, 100.0 * now_eps / capacity) if capacity else 0.0
    print()
    print("  " + "=" * 70)
    print("  CURRENT UTILIZATION  (live engine load vs the capacity above)")
    print("  " + "=" * 70)
    if age is not None and age > 60:
        print(f"  NOTE: stats file is {age:.0f}s old — engine may be stopped;")
        print("        numbers below reflect its last written state.")
    print(f"  Engine now     : {now_eps:,.1f} EPS   capacity: ~{capacity:,.0f} EPS")
    print(f"  Overall        : {_bar(overall)} {overall:.2f}% used "
          f"-> ~{max(0.0, 100 - overall):.0f}% headroom")

    caps = {s["name"]: s["eps"] * workers for s in results}
    rows = []
    for rule, st in (stats.get("parser_stats") or {}).items():
        avg_rate = (st.get("parsed_events") or st.get("events") or 0) / uptime
        if avg_rate <= 0 or rule not in caps or not caps[rule]:
            continue
        rows.append((rule, avg_rate, 100.0 * avg_rate / caps[rule]))
    if rows:
        print()
        print(f"  Per rule (average rate since engine start, {uptime}s ago):")
        for rule, rate, pct in sorted(rows, key=lambda r: -r[2]):
            print(f"    {rule:<20}{rate:>9,.1f} EPS  {_bar(pct)} {pct:.2f}% of capacity")
    print()
    print("  Rule of thumb: worry when sustained utilization passes ~50% —")
    print("  that is the moment to add partitions + workers (or a machine).")


# --------------------------------------------------------------------------- #
# Mode 2: live pipeline lag from the output NDJSON files
# --------------------------------------------------------------------------- #

def tail_lines(path, max_lines, max_bytes=4 * 1024 * 1024):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        chunk = f.read()
    lines = chunk.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]                # first line is probably cut
    return [l for l in lines if l.strip()][-max_lines:]


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def run_live(args, config):
    out_dir = ((config.get("paths") or {}).get("output_dir")) or "./"
    if not os.path.isdir(out_dir):
        sys.exit(f"output_dir not found: {out_dir} (paths.output_dir in config.yaml)")

    # module.wN.json worker files fold into their module
    by_module = {}
    for fname in sorted(os.listdir(out_dir)):
        if not fname.endswith(".json"):
            continue
        mod = fname[:-5]
        parts = mod.rsplit(".", 1)
        if len(parts) == 2 and parts[1].startswith("w") and parts[1][1:].isdigit():
            mod = parts[0]
        by_module.setdefault(mod, []).append(os.path.join(out_dir, fname))

    print("=" * 74)
    print(f"  Live pipeline lag  (event.ingested − @timestamp), last {args.sample} events/module")
    print(f"  Output dir: {out_dir}")
    print("=" * 74)
    print("  Lag spans the WHOLE pipeline: source host -> rsyslog -> Kafka ->")
    print("  engine batch (batch.timeout_sec alone allows a few seconds).")
    print()
    print(f"  {'MODULE':<18}{'events':>8}{'avg':>11}{'p50':>11}{'p95':>11}{'max':>11}   timestamp_source / flags")
    print("  " + "-" * 70)

    for mod, paths in sorted(by_module.items()):
        lags, sources = [], {}
        for p in paths:
            for line in tail_lines(p, args.sample):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                ts = parse_ts(ev.get("@timestamp"))
                ing = parse_ts((ev.get("event") or {}).get("ingested"))
                src = (ev.get("event") or {}).get("timestamp_source", "?")
                sources[src] = sources.get(src, 0) + 1
                if ts and ing:
                    lags.append((ing - ts).total_seconds())
        if not lags:
            print(f"  {mod:<18}{'0':>8}   (no parseable events)")
            continue
        lags.sort()
        n = len(lags)
        avg = sum(lags) / n
        p50 = lags[n // 2]
        p95 = lags[min(n - 1, int(n * 0.95))]
        srctxt = " ".join(f"{k}:{v}" for k, v in sorted(sources.items()))
        flags = []
        if lags[0] < -2:
            flags.append("NEGATIVE lag -> source clock/timezone label wrong")
        if p95 > 60:
            flags.append("p95 > 60s -> backlog or slow pipeline stage")
        if sources.get("ingest_fallback"):
            flags.append("ingest_fallback present -> rule has no/failed timestamp block")
        print(f"  {mod:<18}{n:>8}{avg:>10.1f}s{p50:>10.1f}s{p95:>10.1f}s{lags[-1]:>10.1f}s   "
              + srctxt + ((" | " + "; ".join(flags)) if flags else ""))

    print()
    print("  Reading the numbers: steady seconds-level lag = healthy (batching).")
    print("  Growing lag = consumer behind (add workers/partitions). Negative or")
    print("  offset-sized lag (~hours) = fix the SOURCE host clock, not the engine.")


# --------------------------------------------------------------------------- #
# Mode 3: historical lag timeline straight from Elasticsearch
# --------------------------------------------------------------------------- #

_LAG_SCRIPT = ("(doc['event.ingested'].value.toInstant().toEpochMilli() - "
               "doc['@timestamp'].value.toInstant().toEpochMilli()) / 1000.0")

_INTERVAL_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _interval_secs(interval):
    try:
        return int(interval[:-1]) * _INTERVAL_SECONDS[interval[-1]]
    except (KeyError, ValueError, IndexError):
        sys.exit(f"--interval must look like 30m / 1h / 1d, got: {interval}")


def run_history(args):
    import ssl
    import base64
    import getpass
    import urllib.request
    import urllib.error

    password = args.password or os.environ.get("ES_PASSWORD") \
        or getpass.getpass(f"password for {args.user}: ")
    auth = base64.b64encode(f"{args.user}:{password}".encode()).decode()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    body = {
        "size": 0,
        "query": {"bool": {"filter": [
            {"range": {"@timestamp": {"gte": f"now-{args.days}d"}}},
            {"exists": {"field": "event.ingested"}},
        ]}},
        "aggs": {"timeline": {
            "date_histogram": {"field": "@timestamp",
                               "fixed_interval": args.interval,
                               "min_doc_count": 1},
            "aggs": {
                "lag_avg": {"avg": {"script": {"source": _LAG_SCRIPT}}},
                "lag_pct": {"percentiles": {"script": {"source": _LAG_SCRIPT},
                                            "percents": [95]}},
                "lag_max": {"max": {"script": {"source": _LAG_SCRIPT}}},
            },
        }},
    }
    url = f"{args.es}/{args.index}/_search"
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": "Basic " + auth,
                 "Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, context=ctx, timeout=180))
    except urllib.error.HTTPError as e:
        sys.exit(f"Elasticsearch error {e.code}: {e.read().decode()[:400]}")
    except Exception as e:
        sys.exit(f"Cannot reach {url}: {e}")

    buckets = resp.get("aggregations", {}).get("timeline", {}).get("buckets", [])
    took_note = f"(query took {resp.get('took', 0)/1000.0:.1f}s over the cluster)"
    secs = _interval_secs(args.interval)

    print("=" * 76)
    print(f"  Pipeline lag HISTORY  —  {args.index}, last {args.days}d, "
          f"{args.interval} buckets  {took_note}")
    print("=" * 76)
    print("  Lag = event.ingested − @timestamp per event, aggregated per bucket.")
    print("  A hump that recovers = pipeline caught up; steadily growing lag =")
    print("  real bottleneck; negative = source clock/timezone label wrong.")
    print("  Bucket times are UTC.")
    print()
    print(f"  {'BUCKET (UTC)':<18}{'events':>12}{'EPS':>9}{'lag avg':>10}"
          f"{'p95':>9}{'max':>10}   flag")
    print("  " + "-" * 72)
    for b in buckets:
        n = b["doc_count"]
        eps = n / secs
        avg = b["lag_avg"]["value"] or 0.0
        p95 = b["lag_pct"]["values"].get("95.0") or 0.0
        mx = b["lag_max"]["value"] or 0.0
        flag = ""
        if avg < -2:
            flag = "clock/tz?"
        elif p95 > 300:
            flag = "BEHIND"
        elif p95 > 30:
            flag = "lagging"
        key = b.get("key_as_string", "")[:16].replace("T", " ")
        print(f"  {key:<18}{n:>12,}{eps:>9,.1f}{avg:>9.1f}s{p95:>8.1f}s"
              f"{mx:>9.1f}s   {flag}")
    if not buckets:
        print("  (no documents matched — check --index pattern and --days)")


def main():
    ap = argparse.ArgumentParser(description="Benchmark this deployment: per-rule EPS + parse latency, live pipeline lag, or historical lag timeline.")
    ap.add_argument("--seconds", type=float, default=1.0, help="measure window per rule (default 1.0)")
    ap.add_argument("--rule", help="bench only this pattern_name")
    ap.add_argument("--file", help="custom corpus file (raw log lines) for --rule")
    ap.add_argument("--live", action="store_true", help="report pipeline lag from output NDJSON instead")
    ap.add_argument("--sample", type=int, default=500, help="--live: events per module to sample (default 500)")
    ap.add_argument("--history", action="store_true", help="lag/EPS timeline from Elasticsearch (needs --index)")
    ap.add_argument("--es", default="https://localhost:9200", help="--history: Elasticsearch URL (default https://localhost:9200)")
    ap.add_argument("--user", default="elastic", help="--history: ES username (default elastic)")
    ap.add_argument("--password", help="--history: ES password (or env ES_PASSWORD, or interactive prompt)")
    ap.add_argument("--index", help="--history: index pattern, e.g. 'fosstlsoc-logs-squid-*'")
    ap.add_argument("--days", type=int, default=3, help="--history: how far back (default 3)")
    ap.add_argument("--interval", default="1h", help="--history: bucket size 30m/1h/1d (default 1h)")
    args = ap.parse_args()

    if args.history:
        if not args.index:
            ap.error("--history requires --index (e.g. --index 'fosstlsoc-logs-squid-*')")
        run_history(args)
        return

    config = load_config()
    if args.live:
        run_live(args, config)
    else:
        run_synthetic(args, config)


if __name__ == "__main__":
    main()
