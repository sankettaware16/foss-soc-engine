import os
import sys
import time
import yaml
import signal
import logging
import traceback
import multiprocessing
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from kafka import KafkaConsumer

from core.schema import LogInput
from core.registry import RuleRegistry
from core.output import OutputWriter, DlqWriter
from utils import fastjson

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
LOG_DIR = os.path.join(BASE_DIR, "logs")
# Runtime heartbeat: lets the Web UI monitor know the engine is alive, which
# PIDs to watch (RAM/CPU), uptime and what it is consuming. Pure observability;
# the parsing pipeline does not depend on it.
RUNTIME_PATH = os.path.join(LOG_DIR, "engine.pid")


def write_runtime(role, workers, worker_pids, config, start_time):
    """Write logs/engine.pid so the Web UI can show live engine status."""
    data = {
        "role": role,
        "pid": os.getpid(),
        "workers": workers,
        "worker_pids": list(worker_pids),
        "start_time": start_time,
        "started_iso": datetime.fromtimestamp(start_time, timezone.utc).isoformat(),
        "kafka": {
            "bootstrap_servers": (config.get("kafka") or {}).get("bootstrap_servers"),
            "input_topic": (config.get("kafka") or {}).get("input_topic"),
            "group_id": (config.get("kafka") or {}).get("group_id"),
        },
        "output_dir": (config.get("paths") or {}).get("output_dir"),
    }
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RUNTIME_PATH, "w") as f:
            f.write(fastjson.dumps(data))
    except Exception:
        pass


def clear_runtime():
    try:
        if os.path.exists(RUNTIME_PATH):
            os.remove(RUNTIME_PATH)
    except Exception:
        pass


def metrics_interval(config):
    """How often each worker writes its stats file (seconds). Lower = more
    real-time monitoring, negligible cost. Default 2s."""
    try:
        v = float((config.get("runtime") or {}).get("metrics_interval_sec", 2))
        return max(1.0, v)
    except (TypeError, ValueError):
        return 2.0


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"Config file not found: {CONFIG_PATH}")
    except Exception as e:
        sys.exit(f"Failed to load config: {e}")


def setup_logging(worker_id, workers):
    """Configure the shared 'soc-engine' logger for this process.

    Handlers are reset on every call so a forked worker never writes through
    a handler it inherited from the supervisor (which would corrupt the file).
    """
    logger = logging.getLogger("soc-engine")
    for h in list(logger.handlers):
        logger.removeHandler(h)

    logger.setLevel(logging.INFO)
    if worker_id is None:
        tag, suffix = "supervisor", ""
    else:
        tag = f"w{worker_id}"
        suffix = "" if workers == 1 else f".w{worker_id}"

    fmt = logging.Formatter(f"%(asctime)s - {tag} - %(levelname)s - %(message)s")
    os.makedirs(LOG_DIR, exist_ok=True)

    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, f"engine{suffix}.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def resolve_workers(config):
    """Decide how many worker processes to run. 'auto' = all CPU cores."""
    value = (config.get("runtime", {}) or {}).get("workers", "auto")
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


def make_dlq(raw_value, program, error):
    if isinstance(raw_value, (bytes, bytearray)):
        raw = raw_value.decode("utf-8", "ignore")
    else:
        raw = str(raw_value)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "program": program,
        "error": str(error),
        "raw": raw,
    }


class HealthMonitor:
    def __init__(self, suffix="", interval=2.0, worker_id=None):
        self.suffix = suffix
        self.interval = max(1.0, float(interval))
        self.worker_id = worker_id
        self.pid = os.getpid()
        self.start_time = time.time()
        self.window_start = time.time()
        self.last_log = time.time()
        self.events_in_window = 0
        self.errors_in_window = 0
        self.total_events = 0
        self.total_errors = 0
        self.rule_stats = {}

    def record_event(self):
        self.events_in_window += 1
        self.total_events += 1

    def record_error(self):
        self.errors_in_window += 1
        self.total_errors += 1

    def _get_rule_stats(self, rule_name):
        if rule_name not in self.rule_stats:
            self.rule_stats[rule_name] = {
                "parsed_messages": 0,
                "parsed_events": 0,
                "no_match": 0,
                "buffered": 0,
                "errors": 0,
                "redis_errors": 0,
            }
        return self.rule_stats[rule_name]

    def record_parsed(self, rule_name, event_count=1):
        stats = self._get_rule_stats(rule_name)
        stats["parsed_messages"] += 1
        stats["parsed_events"] += event_count

    def record_no_match(self, rule_name):
        self._get_rule_stats(rule_name)["no_match"] += 1

    def record_buffered(self, rule_name):
        self._get_rule_stats(rule_name)["buffered"] += 1

    def record_rule_error(self, rule_name):
        self._get_rule_stats(rule_name)["errors"] += 1

    def record_redis_error(self, rule_name):
        self._get_rule_stats(rule_name)["redis_errors"] += 1

    def flush_if_needed(self, logger):
        now = time.time()
        if now - self.window_start < self.interval:
            return

        elapsed = now - self.window_start
        eps = self.events_in_window / elapsed if elapsed else 0

        stats = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": self.pid,
            "worker_id": self.worker_id,
            "uptime_sec": int(now - self.start_time),
            "eps": round(eps, 2),
            "total_processed": self.total_events,
            "total_errors": self.total_errors,
            "errors_last_min": self.errors_in_window,  # errors in the last window
            "window_sec": round(elapsed, 1),
            "parser_stats": self.rule_stats,
        }

        try:
            with open(os.path.join(LOG_DIR, f"stats{self.suffix}.json"), "w") as f:
                f.write(fastjson.dumps(stats))
            # Keep the human log line to once a minute even when the file is
            # written every couple of seconds, so engine.log stays readable.
            if now - self.last_log >= 60:
                logger.info(
                    f"Stats: {stats['eps']} EPS, {stats['total_processed']} processed, "
                    f"{self.total_errors} total errors"
                )
                self.last_log = now
        except Exception:
            pass

        self.window_start = now
        self.events_in_window = 0
        self.errors_in_window = 0


def process_message(message, registry, batch, dlq, monitor, logger):
    raw_value = message.value  # raw bytes off Kafka
    try:
        log_input = LogInput(raw_value)
        if not log_input.valid:
            dlq.write(make_dlq(raw_value, "unknown", "invalid_envelope"))
            monitor.record_error()
            return

        rule_name = registry.program_map.get(log_input.program) or log_input.program
        processor = registry.get_processor(log_input.program)
        if not processor:
            dlq.write(make_dlq(raw_value, log_input.program, "no_matching_rule"))
            monitor.record_error()
            return

        try:
            result = processor.process(log_input)
            redis_error = getattr(processor, "last_redis_error", None)
            if redis_error:
                monitor.record_redis_error(rule_name)
                logger.error(f"Redis error ({log_input.program}): {redis_error}")
                if result is None:
                    dlq.write(make_dlq(raw_value, log_input.program, redis_error))
                    monitor.record_error()
                    monitor.record_rule_error(rule_name)
                    return

            if result:
                if isinstance(result, list):  # XML can return multiple events
                    batch.extend(result)
                    for _ in result:
                        monitor.record_event()
                    monitor.record_parsed(rule_name, len(result))
                else:
                    batch.append(result)
                    monitor.record_event()
                    monitor.record_parsed(rule_name, 1)
            else:
                if processor.strategy == "stateful":
                    if processor.id_regex and processor.id_regex.search(log_input.raw):
                        monitor.record_buffered(rule_name)
                    else:
                        dlq.write(make_dlq(raw_value, log_input.program, "no_match"))
                        monitor.record_error()
                        monitor.record_rule_error(rule_name)
                        monitor.record_no_match(rule_name)
                else:
                    dlq.write(make_dlq(raw_value, log_input.program, "no_match"))
                    monitor.record_error()
                    monitor.record_rule_error(rule_name)
                    monitor.record_no_match(rule_name)
        except Exception as e:
            logger.error(f"Parsing error ({log_input.program}): {e}")
            dlq.write(make_dlq(raw_value, log_input.program, e))
            monitor.record_error()
            monitor.record_rule_error(rule_name)

    except Exception as e:
        logger.error(f"Envelope error: {e}")
        dlq.write(make_dlq(raw_value, "unknown", e))
        monitor.record_error()


def run_worker(worker_id, workers, config):
    """One consumer-group member: the original processing loop, parallelized.

    Each worker joins the same Kafka group_id, so the broker spreads topic
    partitions across all workers. Offsets are committed only AFTER the batch
    is written to disk (at-least-once), and SIGTERM triggers a clean final
    flush + commit so restarts never silently drop in-flight events.
    """
    suffix = "" if workers == 1 else f".w{worker_id}"
    logger = setup_logging(worker_id, workers)

    output_dir = config["paths"]["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    out_cfg = config.get("output", {}) or {}
    writer = OutputWriter(
        output_dir,
        suffix=suffix,
        rotate_mb=out_cfg.get("rotate_mb", 0),
        fsync=out_cfg.get("fsync", False),
    )
    dlq = DlqWriter(LOG_DIR, suffix=suffix)

    try:
        registry = RuleRegistry(
            rules_dir=os.path.join(BASE_DIR, config["paths"]["rules_dir"]),
            program_map=config.get("program_mapping", {}),
        )
        logger.info("Rule registry initialized")
    except Exception as e:
        logger.critical(f"Failed to initialize registry: {e}")
        return

    running = {"v": True}

    def _stop(signum, frame):
        running["v"] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    kafka_cfg = config["kafka"]
    try:
        consumer = KafkaConsumer(
            bootstrap_servers=kafka_cfg["bootstrap_servers"],
            group_id=kafka_cfg["group_id"],
            auto_offset_reset=kafka_cfg.get("auto_offset_reset", "latest"),
            enable_auto_commit=False,  # we commit after a durable flush
            max_poll_records=kafka_cfg.get("max_poll_records", 2000),
            fetch_max_bytes=kafka_cfg.get("fetch_max_bytes", 52 * 1024 * 1024),
            max_partition_fetch_bytes=kafka_cfg.get(
                "max_partition_fetch_bytes", 1024 * 1024
            ),
        )
        consumer.subscribe(pattern=kafka_cfg["input_topic"])
        logger.info(
            f"Worker {worker_id} consuming topics matching: {kafka_cfg['input_topic']}"
        )
    except Exception as e:
        logger.critical(f"Kafka connection error: {e}")
        writer.close()
        dlq.close()
        return

    monitor = HealthMonitor(
        suffix=suffix, interval=metrics_interval(config), worker_id=worker_id
    )
    # In single-process mode this worker IS the engine, so it owns the runtime
    # heartbeat. In multi-worker mode the supervisor writes it instead.
    if workers == 1:
        write_runtime("worker", 1, [os.getpid()], config, monitor.start_time)
    batch = []
    batch_size = config["batch"]["size"]
    batch_timeout = config["batch"]["timeout_sec"]
    commit_interval = max(1.0, float(batch_timeout))
    last_flush = time.time()
    last_commit = time.time()

    def flush_and_commit():
        nonlocal last_flush, last_commit
        if batch:
            writer.write_batch(batch)
            writer.flush()
            dlq.flush()
            batch.clear()
            last_flush = time.time()
        now = time.time()
        if now - last_commit >= commit_interval:
            try:
                consumer.commit()  # durable on disk before we advance offsets
            except Exception as e:
                logger.error(f"Offset commit failed: {e}")
            last_commit = now

    try:
        while running["v"]:
            records = consumer.poll(timeout_ms=1000)
            for _, messages in records.items():
                for message in messages:
                    process_message(message, registry, batch, dlq, monitor, logger)

            now = time.time()
            if len(batch) >= batch_size or (batch and now - last_flush > batch_timeout):
                flush_and_commit()
            elif now - last_commit >= commit_interval:
                flush_and_commit()

            monitor.flush_if_needed(logger)
    except Exception as e:
        logger.critical(f"Worker {worker_id} fatal error: {e}")
        traceback.print_exc()
    finally:
        logger.info(f"Worker {worker_id} draining...")
        try:
            if batch:
                writer.write_batch(batch)
                writer.flush()
                batch.clear()
            consumer.commit()
        except Exception as e:
            logger.error(f"Final flush/commit failed: {e}")
        writer.close()
        dlq.close()
        try:
            consumer.close()
        except Exception:
            pass
        if workers == 1:
            clear_runtime()
        logger.info(f"Worker {worker_id} stopped")


def aggregate_stats(workers):
    """Sum per-worker stats files into a single stats.json (total EPS, etc.)."""
    total_eps = 0.0
    total_processed = 0
    total_errors_window = 0
    total_errors = 0
    parser_stats = {}

    for i in range(workers):
        path = os.path.join(LOG_DIR, f"stats.w{i}.json")
        try:
            with open(path) as f:
                s = fastjson.loads(f.read())
        except Exception:
            continue
        total_eps += s.get("eps", 0) or 0
        total_processed += s.get("total_processed", 0) or 0
        total_errors_window += s.get("errors_last_min", 0) or 0
        total_errors += s.get("total_errors", 0) or 0
        for rule, st in (s.get("parser_stats") or {}).items():
            agg = parser_stats.setdefault(rule, {})
            for k, v in st.items():
                agg[k] = agg.get(k, 0) + (v or 0)

    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "workers": workers,
        "eps": round(total_eps, 2),
        "total_processed": total_processed,
        "total_errors": total_errors,
        "errors_last_min": total_errors_window,
        "parser_stats": parser_stats,
    }
    try:
        with open(os.path.join(LOG_DIR, "stats.json"), "w") as f:
            f.write(fastjson.dumps(out))
    except Exception:
        pass


def supervise(config, workers):
    """Spawn and supervise N worker processes; restart crashed ones; shut down
    the whole fleet cleanly on SIGTERM/SIGINT."""
    logger = setup_logging(None, workers)
    ctx = multiprocessing.get_context("fork")
    procs = {}
    last_start = {}
    fast_fails = {}
    shutdown = {"v": False}

    def _term(signum, frame):
        shutdown["v"] = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    sup_start = time.time()
    agg_interval = max(2.0, metrics_interval(config))

    def spawn(i):
        p = ctx.Process(
            target=run_worker, args=(i, workers, config), name=f"soc-worker-{i}"
        )
        p.start()
        procs[i] = p
        last_start[i] = time.time()

    def refresh_runtime():
        live = [procs[i].pid for i in sorted(procs) if procs[i].is_alive()]
        write_runtime("supervisor", workers, live, config, sup_start)

    logger.info(f"Starting {workers} worker processes")
    for i in range(workers):
        spawn(i)
    refresh_runtime()

    last_agg = time.time()
    try:
        while not shutdown["v"]:
            time.sleep(1)
            now = time.time()

            for i, p in list(procs.items()):
                if not p.is_alive() and not shutdown["v"]:
                    if now - last_start.get(i, 0) < 10:
                        fast_fails[i] = fast_fails.get(i, 0) + 1
                    else:
                        fast_fails[i] = 0
                    delay = min(30, 2 * max(1, fast_fails.get(i, 0)))
                    logger.error(
                        f"Worker {i} exited (code {p.exitcode}); restarting in {delay}s"
                    )
                    time.sleep(delay)
                    if shutdown["v"]:
                        break
                    spawn(i)
                    refresh_runtime()

            if now - last_agg >= agg_interval:
                aggregate_stats(workers)
                refresh_runtime()
                last_agg = now
    finally:
        logger.info("Supervisor shutting down workers...")
        for p in procs.values():
            if p.is_alive():
                p.terminate()  # SIGTERM -> graceful drain in each worker
        deadline = time.time() + 25
        for p in procs.values():
            p.join(timeout=max(0.1, deadline - time.time()))
        for p in procs.values():
            if p.is_alive():
                logger.error(f"Force killing worker {p.name}")
                p.kill()
        clear_runtime()
        logger.info("Supervisor stopped")


def main():
    config = load_config()
    workers = resolve_workers(config)

    if workers <= 1:
        # Single-process mode: easiest to debug, logs straight to console.
        run_worker(0, 1, config)
    else:
        supervise(config, workers)


if __name__ == "__main__":
    main()
