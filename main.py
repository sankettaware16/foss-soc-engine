import os
import sys
import json
import time
import yaml
import logging
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from kafka import KafkaConsumer
from core.schema import LogInput
from core.registry import RuleRegistry

# Setup paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

# Load configuration
try:
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)
except FileNotFoundError:
    sys.exit(f"Config file not found: {CONFIG_PATH}")

OUTPUT_DIR = config["paths"]["output_dir"]
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# logging setup
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "engine.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logger = logging.getLogger("soc-engine")
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Initialize Registry
try:
    registry = RuleRegistry(
        rules_dir=os.path.join(BASE_DIR, config["paths"]["rules_dir"]),
        program_map=config.get("program_mapping", {})
    )
    logger.info("Rule registry loaded successfully")
except Exception as e:
    logger.critical(f"Failed to initialize rule registry: {e}")
    sys.exit(1)

def write_dlq(raw_log, program, error):
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "program": program,
        "error": str(error),
        "raw": raw_log,
    }
    try:
        with open(os.path.join(LOG_DIR, "dlq.json"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error(f"Unable to write DLQ entry: {e}")

class HealthMonitor:
    def __init__(self):
        self.start_time = time.time()
        self.window_start = time.time()
        self.events_in_window = 0
        self.errors_in_window = 0
        self.total_events = 0

    def record_event(self):
        self.events_in_window += 1
        self.total_events += 1

    def record_error(self):
        self.errors_in_window += 1

    def flush_if_needed(self):
        now = time.time()
        if now - self.window_start < 60:
            return

        elapsed = now - self.window_start
        eps = self.events_in_window / elapsed if elapsed else 0

        stats = {
            "timestamp": datetime.utcnow().isoformat(),
            "uptime_sec": int(now - self.start_time),
            "eps": round(eps, 2),
            "total_processed": self.total_events,
            "errors_last_min": self.errors_in_window,
        }

        try:
            with open(os.path.join(LOG_DIR, "stats.json"), "w") as f:
                json.dump(stats, f)
            logger.info(f"Health check: {stats['eps']} EPS, {stats['total_processed']} total, {stats['errors_last_min']} errors")
        except Exception:
            pass

        self.window_start = now
        self.events_in_window = 0
        self.errors_in_window = 0

monitor = HealthMonitor()

def flush_batch(batch):
    if not batch:
        return

    files = {}
    for event in batch:
        module = event.get("event", {}).get("module", "unknown")
        files.setdefault(module, []).append(json.dumps(event))

    for module, lines in files.items():
        try:
            with open(os.path.join(OUTPUT_DIR, f"{module}.json"), "a") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            logger.error(f"Failed to write output for module '{module}': {e}")

def main():
    try:
        consumer = KafkaConsumer(
            bootstrap_servers=config["kafka"]["bootstrap_servers"],
            group_id=config["kafka"]["group_id"],
            auto_offset_reset=config["kafka"]["auto_offset_reset"],
            enable_auto_commit=True,
            max_poll_records=2000,
            value_deserializer=lambda m: m.decode("utf-8", errors="ignore"),
        )
        consumer.subscribe(pattern=config["kafka"]["input_topic"])
        logger.info("Kafka consumer initialized")
    except Exception as e:
        logger.critical(f"Kafka connection failed: {e}")
        sys.exit(1)

    batch = []
    last_flush = time.time()
    batch_size = config["batch"]["size"]
    batch_timeout = config["batch"]["timeout_sec"]

    try:
        while True:
            records = consumer.poll(timeout_ms=1000)

            for _, messages in records.items():
                for message in messages:
                    try:
                        log_input = LogInput(message.value)
                        if not log_input.valid:
                            continue

                        processor = registry.get_processor(log_input.program)
                        if not processor:
                            continue

                        try:
                            event = processor.process(log_input)
                            if event:
                                batch.append(event)
                                monitor.record_event()
                        except Exception as e:
                            logger.error(f"Processor failure ({log_input.program}): {e}")
                            write_dlq(message.value, log_input.program, e)
                            monitor.record_error()

                    except Exception as e:
                        logger.error(f"Malformed message envelope: {e}")
                        write_dlq(message.value, "unknown", e)
                        monitor.record_error()

            now = time.time()
            if len(batch) >= batch_size or (batch and now - last_flush > batch_timeout):
                flush_batch(batch)
                batch.clear()
                last_flush = now

            monitor.flush_if_needed()

    except KeyboardInterrupt:
        logger.info("Shutdown requested")
        flush_batch(batch)
        consumer.close()
    except Exception as e:
        logger.critical(f"Fatal engine error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
