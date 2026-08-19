#!/usr/bin/env python3
"""
Pre-flight checker - run this BEFORE starting the engine.

It validates everything that can stop the engine from working, in one shot:

  1. config.yaml exists and is structurally correct
  2. paths (rules dir, output dir) and the GeoIP database
  3. rules load and their regexes compile
  4. every rule field is valid ECS (uses ecs_helper / ecs_schema)
  5. program_mapping points at rules that exist
  6. the internal IP map parses (ranges valid, fields pass the ECS gate)
  7. the Kafka host:port is actually reachable from THIS server (raw TCP)
  8. the broker really speaks Kafka, and the topics you configured exist
     (with their partition counts)
  9. Redis is reachable (only required if you use any 'stateful' rule)
 10. you have enough Kafka partitions for the number of workers

Usage:
  python3 preflight.py                 # full check using config.yaml
  python3 preflight.py --config /path/to/config.yaml
  python3 preflight.py --skip-live     # static checks only (no network)
  python3 preflight.py --timeout 6     # network timeout per check (seconds)

Exit code 0 = safe to start.  Non-zero = fix the reported errors first.
"""

import os
import re
import sys
import socket
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yaml
import test_config as tc  # reuse the static validators (config/paths/rules/ECS)

try:
    from kafka import KafkaAdminClient, KafkaConsumer
except Exception:
    KafkaAdminClient = KafkaConsumer = None

try:
    import redis as redis_lib
except Exception:
    redis_lib = None


def section(title):
    print(f"\n=== {title} ===")


def resolve_workers(config):
    value = (config.get("runtime") or {}).get("workers", "auto")
    env = os.environ.get("SOC_WORKERS")
    if env:
        value = env
    if isinstance(value, str):
        if value.strip().lower() in ("auto", "", "0"):
            return os.cpu_count() or 1
        try:
            value = int(value)
        except ValueError:
            return os.cpu_count() or 1
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return os.cpu_count() or 1


def parse_brokers(bootstrap):
    """Normalize bootstrap_servers (list or 'h:p,h:p' string) to [(host, port)]."""
    if isinstance(bootstrap, str):
        items = [b.strip() for b in bootstrap.split(",") if b.strip()]
    elif isinstance(bootstrap, (list, tuple)):
        items = list(bootstrap)
    else:
        return []
    out = []
    for item in items:
        host, _, port = str(item).rpartition(":")
        if not host:
            host, port = port, "9092"
        try:
            out.append((host, int(port)))
        except ValueError:
            out.append((host, port))
    return out


# ---------------------------------------------------------------- live checks

def check_network(config, timeout):
    """Raw TCP connect to each broker - 'can this server even reach that IP:port'."""
    errors = 0
    brokers = parse_brokers(config.get("kafka", {}).get("bootstrap_servers"))
    if not brokers:
        tc.report("ERROR", "kafka.bootstrap_servers is empty or invalid")
        return 1
    for host, port in brokers:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                tc.report("OK", f"TCP reachable: {host}:{port}")
        except Exception as e:
            tc.report("ERROR", f"Cannot reach {host}:{port} from this server ({e})")
            errors += 1
    return errors


def check_kafka(config, timeout):
    """Confirm it's really a Kafka broker, list matched topics + partition counts."""
    if KafkaAdminClient is None:
        tc.report("ERROR", "kafka client not installed (pip install kafka-python-ng)")
        return 1, {}

    kafka = config.get("kafka", {})
    bootstrap = kafka.get("bootstrap_servers")
    input_topic = kafka.get("input_topic")
    ms = int(timeout * 1000)

    try:
        admin = KafkaAdminClient(
            bootstrap_servers=bootstrap,
            request_timeout_ms=ms,
            api_version_auto_timeout_ms=ms,
        )
    except Exception as e:
        tc.report("ERROR", f"Broker handshake failed (port open but not Kafka?): {e}")
        return 1, {}

    try:
        all_topics = sorted(admin.list_topics())
    except Exception as e:
        tc.report("ERROR", f"Could not list topics: {e}")
        return 1, {}
    finally:
        try:
            admin.close()
        except Exception:
            pass

    tc.report("OK", f"Kafka broker confirmed, {len(all_topics)} topic(s) on cluster")

    try:
        topic_re = re.compile(input_topic)
    except re.error as e:
        tc.report("ERROR", f"kafka.input_topic is not a valid regex: {e}")
        return 1, {}

    matched = [t for t in all_topics if topic_re.search(t)]
    if not matched:
        tc.report("ERROR", f"No existing topic matches input_topic '{input_topic}'")
        return 1, {}

    # Partition counts for the matched topics.
    part_counts = {}
    try:
        consumer = KafkaConsumer(
            bootstrap_servers=bootstrap, request_timeout_ms=ms,
            api_version_auto_timeout_ms=ms,
        )
        for t in matched:
            parts = consumer.partitions_for_topic(t)
            part_counts[t] = len(parts) if parts else 0
        consumer.close()
    except Exception:
        part_counts = {t: 0 for t in matched}

    shown = ", ".join(
        f"{t}[{part_counts.get(t, '?')}p]" for t in matched[:10]
    )
    tc.report("OK", f"{len(matched)} topic(s) match input_topic: {shown}"
              + (" ..." if len(matched) > 10 else ""))
    if len(matched) > 10:
        tc.report("WARN", f"input_topic matches {len(matched)} topics "
                  "(a '.*' pattern matches everything - is that intended?)")
    return 0, part_counts


def check_redis(config, rules, timeout):
    """Redis is only needed if any rule uses the 'stateful' strategy."""
    stateful = [n for n, r in rules.items()
                if isinstance(r, dict) and r.get("strategy") == "stateful"]
    if not stateful:
        tc.report("OK", "no stateful rules - Redis not required")
        return 0
    if redis_lib is None:
        tc.report("ERROR", "stateful rules present but redis client not installed")
        return 1
    # Same resolution as the engine (config.yaml `redis:` block, defaults
    # localhost:6379 - see core/engine.py configure_redis).
    rc = config.get("redis") or {}
    host = str(rc.get("host", "localhost"))
    port = int(rc.get("port", 6379))
    db = int(rc.get("db", 0))
    try:
        client = redis_lib.Redis(host=host, port=port, db=db,
                                 password=rc.get("password") or None,
                                 socket_connect_timeout=timeout)
        client.ping()
        tc.report("OK", f"Redis reachable at {host}:{port}/{db} "
                  f"(needed by: {', '.join(stateful)})")
        return 0
    except Exception as e:
        tc.report("ERROR", f"stateful rules need Redis but {host}:{port} is "
                  f"unreachable ({e})")
        return 1


def check_workers_vs_partitions(config, part_counts):
    workers = resolve_workers(config)
    if not part_counts:
        return 0
    min_parts = min(part_counts.values()) if part_counts else 0
    if min_parts and workers > min_parts:
        tc.report("WARN", f"{workers} workers but smallest topic has only "
                  f"{min_parts} partition(s); {workers - min_parts} worker(s) "
                  "will sit idle. Add partitions or lower runtime.workers.")
        return 0
    tc.report("OK", f"{workers} worker(s) <= partitions on every matched topic")
    return 0


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Full pre-flight check before starting the engine.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--timeout", type=float, default=4.0, help="network timeout (s)")
    ap.add_argument("--skip-live", action="store_true", help="static checks only")
    args = ap.parse_args()

    base_dir = os.path.dirname(os.path.abspath(args.config))
    config = tc.load_config(args.config)
    if config is None:
        sys.exit(2)

    errors = 0
    warnings = 0

    def ok_if_silent(e, w):
        if e == 0 and w == 0:
            tc.report("OK", "passed")

    section("1. Config structure")
    e, w = tc.validate_config_shape(config); errors += e; warnings += w
    ok_if_silent(e, w)

    section("2. Paths & GeoIP")
    e, w = tc.validate_paths(base_dir, config); errors += e; warnings += w
    ok_if_silent(e, w)

    section("3. Rules (load + regex)")
    e, w, rules = tc.validate_rules(base_dir, config); errors += e; warnings += w
    ok_if_silent(e, w)

    section("4. ECS field compliance")
    e, w = tc.validate_ecs_fields(rules); errors += e; warnings += w
    ok_if_silent(e, w)

    section("5. Program mapping")
    e, w = tc.validate_program_mapping(config, rules); errors += e; warnings += w
    ok_if_silent(e, w)

    section("6. Internal IP map")
    e, w = tc.validate_internal_map(base_dir, config); errors += e; warnings += w
    ok_if_silent(e, w)

    part_counts = {}
    if args.skip_live:
        section("7-10. Live checks")
        tc.report("INFO", "skipped (--skip-live)")
    else:
        section("7. Network reachability (TCP)")
        errors += check_network(config, args.timeout)

        section("8. Kafka broker & topics")
        e, part_counts = check_kafka(config, args.timeout); errors += e

        section("9. Redis (for stateful rules)")
        errors += check_redis(config, rules, args.timeout)

        section("10. Workers vs partitions")
        check_workers_vs_partitions(config, part_counts)

    print("\n" + "=" * 60)
    if errors:
        print(f"  RESULT: FAIL  -  {errors} error(s), {warnings} warning(s)")
        print("  Fix the errors above before starting the engine.")
        print("=" * 60)
        sys.exit(1)
    print(f"  RESULT: PASS  -  0 errors, {warnings} warning(s)")
    print("  Safe to start:  sudo systemctl start foss-soc   (or  python3 main.py)")
    print("=" * 60)
    sys.exit(0)


if __name__ == "__main__":
    main()
