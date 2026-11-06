"""
test_sources.py — regression test for source names and topic routing.

Covers:
  1. Rule source names: every rule's `source:` / `aliases:` resolve to that
     rule, `fallback:` chains follow it, unknown names resolve to nothing.
  2. program_mapping keeps working exactly as before and always wins.
  3. Conflicts: two rules claiming one name -> the first rule (file order)
     keeps it; an invalid name is ignored.
  4. Topic routing (main.process_message): on tlsoc.logs.<name> the topic
     names the source for plain lines (no envelope), JSON lines, and
     envelopes with an unknown tag; other topics behave as before.
  5. New sources (main.NewSourceStart): a never-read SMALL topic starts at
     its first message, a never-read big topic does not replay.
  6. Status table (sources.py, also the Web UI's Sources page): each worker's
     stats file lists the partitions it owns; stale workers are ignored; the
     rate can be measured against an earlier sample; lag per topic.

No Kafka/Redis needed. Run:  python3 test_sources.py  (exit 0 = pass)
"""
import os
import sys
import json
import shutil
import logging
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core.registry import RuleRegistry, source_from_topic
import main as engine_main

RULES_DIR = os.path.join(HERE, "rules")
RESULTS = []
logging.getLogger("soc-engine").setLevel(logging.CRITICAL)


def check(label, ok, note=""):
    RESULTS.append((label, bool(ok), note))


def chain(registry, name):
    return [n for n, _ in registry.get_processors(name)]


def test_source_names():
    reg = RuleRegistry(RULES_DIR, {})
    expect = {
        "auth": ["linux_auth"], "sshd": ["linux_auth"], "linux_auth": ["linux_auth"],
        "nginx": ["nginx_access", "apache_access"],
        "nginx_access": ["nginx_access", "apache_access"],
        "vhnginx_access": ["nginx_access", "apache_access"],
        "apache": ["apache_access"], "httpd": ["apache_access"],
        "squid": ["squid_proxy_access"], "dns": ["dnsdist"], "dnsdist": ["dnsdist"],
        "postfix": ["postfix"], "mail": ["postfix"],
        "radius": ["radius"], "radiusd": ["radius"], "sso": ["sso"],
        "policyd-spf": ["policyd_spf"], "suricata": ["suricata"],
    }
    for name, want in expect.items():
        got = chain(reg, name)
        check(f"source '{name}' -> {want}", got == want, f"got {got}")
    check("unknown source resolves to nothing", chain(reg, "moodle") == [])
    check("every rule declares a source", all(
        (e.config or {}).get("source") for e in reg.engines.values()))


def test_program_mapping_wins():
    reg = RuleRegistry(RULES_DIR, {"nginx": "nginx_access", "fmlr": "policyd_spf",
                                   "web01": ["nginx_access", "linux_auth"],
                                   "auth": "postfix"})
    check("mapping is exact (no fallback added)", chain(reg, "nginx") == ["nginx_access"])
    check("site tag via mapping", chain(reg, "fmlr") == ["policyd_spf"])
    check("mapping chain kept", chain(reg, "web01") == ["nginx_access", "linux_auth"])
    check("mapping overrides a rule's own source", chain(reg, "auth") == ["postfix"])
    check("mapping with missing rule -> nothing",
          chain(RuleRegistry(RULES_DIR, {"x": "no_such_rule"}), "x") == [])


def test_conflicts():
    tmp = tempfile.mkdtemp()
    try:
        base = open(os.path.join(RULES_DIR, "sso.yaml")).read()
        open(os.path.join(tmp, "a.yaml"), "w").write(
            base.replace('pattern_name: "sso"', 'pattern_name: "rule_a"')
                .replace('source: "sso"', 'source: "shared"\naliases: ["Bad Name"]'))
        open(os.path.join(tmp, "b.yaml"), "w").write(
            base.replace('pattern_name: "sso"', 'pattern_name: "rule_b"')
                .replace('source: "sso"', 'source: "shared"'))
        reg = RuleRegistry(tmp, {})
        check("conflict: first rule keeps the name", chain(reg, "shared") == ["rule_a"])
        check("invalid alias ignored", "Bad Name" not in reg.sources)
    finally:
        shutil.rmtree(tmp)


def test_topic_helper():
    check("topic -> source", source_from_topic("tlsoc.logs.squid") == "squid")
    check("non-TLSOC topic -> None", source_from_topic("logservercc") is None)
    check("empty suffix -> None", source_from_topic("tlsoc.logs.") is None)
    check("None topic -> None", source_from_topic(None) is None)


class Msg:
    def __init__(self, topic, value):
        self.topic = topic
        self.value = value.encode() if isinstance(value, str) else value


class Sink:
    def __init__(self):
        self.items = []

    def write(self, entry):
        self.items.append(entry)


class Quiet:
    def __getattr__(self, _):
        return lambda *a, **k: None


def run(reg, topic, value):
    batch, dlq = [], Sink()
    engine_main.process_message(Msg(topic, value), reg, batch, dlq, Quiet(), logging.getLogger("t"))
    return batch, dlq.items


def envelope(program, raw):
    return json.dumps({"meta": {"source_program": program, "server": "t"}, "raw": raw})


def test_topic_routing():
    reg = RuleRegistry(RULES_DIR, {})
    apache_line = '203.0.113.7 - - [03/Nov/2026:10:00:00 +0530] "GET /login HTTP/1.1" 200 512 "-" "curl/8.0"'
    auth_line = "Nov  3 10:00:00 web1 sshd[1123]: Failed password for invalid user bob from 198.51.100.8 port 40222 ssh2"

    batch, dlq = run(reg, "tlsoc.logs.nginx", apache_line)
    check("plain nginx line on tlsoc.logs.nginx parses (fallback chain)",
          len(batch) == 1 and not dlq and batch[0].get("event", {}).get("module") == "nginx",
          f"batch={len(batch)} dlq={dlq[:1]}")

    batch, dlq = run(reg, "tlsoc.logs.auth", auth_line)
    check("plain auth line on tlsoc.logs.auth parses", len(batch) == 1 and not dlq)

    batch, dlq = run(reg, "tlsoc.logs.auth", envelope("weird_tag", auth_line))
    check("unknown tag on tlsoc.logs.auth -> topic name used",
          len(batch) == 1 and batch[0].get("event", {}).get("module") == "auth", f"dlq={dlq[:1]}")

    batch, dlq = run(reg, "tlsoc.logs.auth", envelope("sshd", auth_line))
    check("known tag keeps its own name (event.module = tag)",
          len(batch) == 1 and batch[0].get("event", {}).get("module") == "sshd")

    batch, dlq = run(reg, "tlsoc.logs.moodle", "2026-11-03 10:00:00 user alice logged in")
    check("source without a rule -> DLQ under its own name",
          not batch and dlq and "moodle" in json.dumps(dlq[0]), f"dlq={dlq[:1]}")

    batch, dlq = run(reg, "logservercc", envelope("auth", auth_line))
    check("legacy single topic + envelope still parses", len(batch) == 1 and not dlq)

    batch, dlq = run(reg, "logservercc", apache_line)
    check("legacy topic + plain line -> DLQ invalid_envelope (unchanged)",
          not batch and dlq and "invalid_envelope" in json.dumps(dlq[0]))

    fim = os.path.join(HERE, "tests", "samples", "fim", "input.log")
    line = next(l.strip() for l in open(fim) if l.strip().startswith("{"))
    batch, dlq = run(reg, "tlsoc.logs.fim", line)
    check("plain JSON line (FIM) on its topic parses", len(batch) >= 1 and not dlq,
          f"dlq={dlq[:1]}")


class FakeConsumer:
    """Just enough of KafkaConsumer for NewSourceStart."""
    def __init__(self, sizes, committed=()):
        from kafka import TopicPartition
        self.TP = TopicPartition
        self.sizes = sizes            # {topic: [messages per partition]}
        self.committed_tps = set(committed)
        self.seeks = {}

    def committed(self, tp):
        return 5 if tp in self.committed_tps else None

    def partitions_for_topic(self, topic):
        return set(range(len(self.sizes[topic])))

    def beginning_offsets(self, tps):
        return {tp: 10 for tp in tps}

    def end_offsets(self, tps):
        return {tp: 10 + self.sizes[tp.topic][tp.partition] for tp in tps}

    def seek(self, tp, offset):
        self.seeks[tp] = offset


def test_new_source_start():
    from kafka import TopicPartition as TP
    log = logging.getLogger("t")
    c = FakeConsumer({"tlsoc.logs.vpn": [60, 0, 30], "tlsoc.logs.big": [90000, 90000, 5000],
                      "tlsoc.logs.auth": [10, 10, 10]},
                     committed=[TP("tlsoc.logs.auth", 0), TP("tlsoc.logs.auth", 1), TP("tlsoc.logs.auth", 2)])
    lst = engine_main.NewSourceStart(100000, log)
    lst.consumer = c
    # this worker owns only some partitions: the decision still uses the whole topic
    lst.on_partitions_assigned([TP("tlsoc.logs.vpn", 0), TP("tlsoc.logs.vpn", 2),
                                TP("tlsoc.logs.big", 2), TP("tlsoc.logs.auth", 0)])
    check("new small topic starts at its first message",
          c.seeks.get(TP("tlsoc.logs.vpn", 0)) == 10 and c.seeks.get(TP("tlsoc.logs.vpn", 2)) == 10)
    check("never-read BIG topic is not replayed (whole-topic size counts)",
          TP("tlsoc.logs.big", 2) not in c.seeks, f"seeks={c.seeks}")
    check("committed partitions are left alone", TP("tlsoc.logs.auth", 0) not in c.seeks)
    legacy = engine_main.NewSourceStart(100000, log)
    legacy.consumer = FakeConsumer({"logservercc": [50]})
    legacy.on_partitions_assigned([TP("logservercc", 0)])
    check("old shared topic is never touched", not legacy.consumer.seeks)
    off = engine_main.NewSourceStart(0, log)
    off.consumer = FakeConsumer({"tlsoc.logs.vpn": [5]})
    off.on_partitions_assigned([TP("tlsoc.logs.vpn", 0)])
    check("new_source_backfill: 0 turns it off", not off.consumer.seeks)


def test_status_table():
    import time
    import types
    import kafka
    import sources
    from kafka import TopicPartition as TP
    tmp = tempfile.mkdtemp()
    try:
        # a worker writes the partitions it owns into its stats file
        saved = engine_main.LOG_DIR
        engine_main.LOG_DIR = tmp
        try:
            mon = engine_main.HealthMonitor(suffix=".w1", interval=1, worker_id=1)
            mon.assignment = lambda: {TP("tlsoc.logs.squid", 3), TP("tlsoc.logs.squid", 0),
                                      TP("tlsoc.logs.dns", 1)}
            mon.window_start -= 5
            mon.flush_if_needed(logging.getLogger("t"))
        finally:
            engine_main.LOG_DIR = saved
        st = json.load(open(os.path.join(tmp, "stats.w1.json")))
        check("worker stats list owned partitions",
              st.get("assignment") == {"tlsoc.logs.dns": [1], "tlsoc.logs.squid": [0, 3]},
              f"got {st.get('assignment')}")

        def put(name, data, age=0):
            path = os.path.join(tmp, name)
            with open(path, "w") as f:
                json.dump(data, f)
            if age:
                os.utime(path, (time.time() - age, time.time() - age))
        put("engine.pid", {"role": "supervisor", "workers": 3})
        put("stats.w0.json", {"worker_id": 0, "assignment": {"tlsoc.logs.squid": [1, 2]}})
        put("stats.w2.json", {"worker_id": 2, "assignment": {"tlsoc.logs.squid": [4]}}, age=600)
        n, owners = sources.engine_workers(tmp)
        check("supervisor: workers and owners per topic",
              n == 3 and owners.get("tlsoc.logs.squid") == {"w0": [1, 2], "w1": [0, 3]},
              f"got {n} {owners}")
        check("stale worker stats are ignored", "w2" not in owners.get("tlsoc.logs.squid", {}))
        put("engine.pid", {"role": "worker", "workers": 1})
        put("stats.json", {"worker_id": 0, "assignment": {"tlsoc.logs.auth": [0, 1, 2]}})
        n, owners = sources.engine_workers(tmp)
        check("single worker: stats.json", n == 1 and owners == {"tlsoc.logs.auth": {"w0": [0, 1, 2]}},
              f"got {n} {owners}")
        cfg = {"kafka": {"input_topic": r"^tlsoc\.logs\..+$"}, "paths": {"output_dir": tmp}}
        rows = {r["source"]: r for r in sources.collect(cfg, HERE, use_kafka=False, log_dir=tmp)["sources"]}
        auth = rows.get("auth") or {}
        check("no Kafka: topics and partitions from the workers' stats",
              auth.get("topic") == "tlsoc.logs.auth" and auth.get("partitions") == 3
              and auth.get("workers") == {"w0": [0, 1, 2]} and auth.get("lag") is None, f"got {auth}")
        os.remove(os.path.join(tmp, "engine.pid"))
        check("engine not running: no workers", sources.engine_workers(tmp) == (0, {}))

        class FakeKC:
            def __init__(self, **kw): pass
            def topics(self): return {"tlsoc.logs.squid", "other"}
            def partitions_for_topic(self, t): return {0, 1}
            def end_offsets(self, tps): return {tp: 500 for tp in tps}
            def close(self): pass

        class FakeAdmin:
            def __init__(self, **kw): pass
            def list_consumer_group_offsets(self, g):
                return {TP("tlsoc.logs.squid", 0): types.SimpleNamespace(offset=400)}
            def close(self): pass
        real = kafka.KafkaConsumer, kafka.KafkaAdminClient
        kafka.KafkaConsumer, kafka.KafkaAdminClient = FakeKC, FakeAdmin
        try:
            cfg = {"kafka": {"bootstrap_servers": ["x:9092"], "group_id": "g",
                             "input_topic": r"^tlsoc\.logs\..+$"}}
            v = sources.kafka_view(cfg, 0, prev={"t": time.time() - 10,
                                                 "ends": {"tlsoc.logs.squid": 900}})
        finally:
            kafka.KafkaConsumer, kafka.KafkaAdminClient = real
        sq = v.get("tlsoc.logs.squid") or {}
        check("only topics matching input_topic", list(v) == ["tlsoc.logs.squid"], f"got {list(v)}")
        check("lag over committed partitions", sq.get("lag") == 100, f"got {sq}")
        check("rate against an earlier sample", sq.get("rate") and 9 < sq["rate"] < 11, f"got {sq}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_new_source_start()
    test_status_table()
    test_source_names()
    test_program_mapping_wins()
    test_conflicts()
    test_topic_helper()
    test_topic_routing()

    failed = 0
    for label, ok, note in RESULTS:
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {label}"
        if note and not ok:
            line += f"  ({note})"
        print(line)
        if not ok:
            failed += 1
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
