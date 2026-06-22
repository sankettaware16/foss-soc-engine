#!/usr/bin/env python3
"""
replicate.py - dry-run the WHOLE pipeline locally, with NO Kafka and NO real
servers, before you point a production source at the engine.

It reads your rsyslog imfile config (the file that forwards logs to Kafka) to
learn which log file becomes which `source_program` (Tag). For each source it
takes a small sample of real lines, wraps them in the EXACT Kafka envelope your
rsyslog template produces ({"meta":{...,"source_program":TAG},"raw":LINE}), and
runs them through the real parsing engine - then tells you precisely where the
pipeline would break:

  * rsyslog wouldn't even forward this tag to Kafka
  * the engine isn't subscribed to the Kafka topic rsyslog sends to
  * rsyslog and the engine point at different Kafka brokers
  * there's no program_mapping for this source_program (-> DLQ)
  * the mapped rule's regex doesn't actually match these lines

When a mapping is missing or a rule doesn't match, it auto-detects which rule
DOES match the sample and tells you the exact program_mapping line to add.

Usage:
  python3 replicate.py --rsyslog /etc/rsyslog.d/90-mailserver-kafka.conf
  python3 replicate.py --rsyslog conf.conf --limit 50
  python3 replicate.py --rsyslog conf.conf --logs-dir ./samples   # override file paths
  python3 replicate.py --file /var/log/postfix.log --program postfix   # one source
"""

import os
import re
import sys
import json
import socket
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yaml
from core.registry import RuleRegistry
from core.schema import LogInput

G, Y, R, D, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    G = Y = R = D = B = X = ""

OKW, WARNW, ERRW = f"{G}OK{X}", f"{Y}WARN{X}", f"{R}ERROR{X}"


# ----------------------------------------------------------- rsyslog parsing

def parse_rsyslog(path):
    text = open(path).read()

    inputs = []
    for m in re.finditer(r"input\s*\(([^)]*)\)", text, re.S):
        body = m.group(1)
        f = re.search(r'File\s*=\s*"([^"]+)"', body)
        t = re.search(r'Tag\s*=\s*"([^"]+)"', body)
        if f and t:
            inputs.append((f.group(1), t.group(1).rstrip(":")))

    topic = broker = None
    am = re.search(r"action\s*\(([^)]*omkafka[^)]*)\)", text, re.S)
    if am:
        body = am.group(1)
        mt = re.search(r'topic\s*=\s*"([^"]+)"', body)
        mb = re.search(r"broker\s*=\s*\[([^\]]*)\]", body)
        topic = mt.group(1) if mt else None
        if mb:
            broker = [b.strip().strip('"').strip("'") for b in mb.group(1).split(",") if b.strip()]

    forward_tags = set(re.findall(r"\$programname\s*==\s*'([^']+)'", text))

    meta_static = {}
    tm = re.search(r"template\s*\([^)]*KafkaMailEnvelope.*?\)\s*\{(.*?)\}", text, re.S)
    if tm:
        for k, v in re.findall(r'\\"(\w+)\\":\\"([^"\\]+)\\"', tm.group(1)):
            meta_static[k] = v

    return {
        "inputs": inputs,
        "topic": topic,
        "broker": broker or [],
        "forward_tags": forward_tags,
        "meta_static": meta_static,
    }


# --------------------------------------------------------------- engine side

def envelope(raw, tag, meta_static):
    meta = dict(meta_static or {})
    meta["source_program"] = tag
    meta.setdefault("source_host", socket.gethostname())
    return json.dumps({"meta": meta, "raw": raw})


def recognizes(proc, raw):
    """Does this rule's pattern recognize the line? (no Redis/Kafka needed)"""
    try:
        s = proc.strategy
        if s == "stateless":
            return bool(proc.main_regex and proc.main_regex.search(raw))
        if s == "multi_match":
            return any(p["regex"].search(raw) for p in proc.patterns)
        if s == "stateful":
            if proc.id_regex and proc.id_regex.search(raw):
                return True
            return any(p["regex"].search(raw) for p in proc.sub_patterns)
        if s == "json_map":
            json.loads(raw)
            return True
        if s == "xml_xpath":
            return raw.lstrip().startswith("<")
    except Exception:
        return False
    return False


def first_parsed_event(proc, lines, tag, meta_static):
    for raw in lines:
        try:
            ev = proc.process(LogInput(envelope(raw, tag, meta_static)))
            if ev:
                return ev[0] if isinstance(ev, list) and ev else ev
        except Exception:
            continue
    return None


def auto_suggest(registry, lines):
    """Which rule best recognizes these lines? -> [(rule_name, hits)]."""
    scores = []
    for name, proc in registry.engines.items():
        hits = sum(1 for raw in lines if recognizes(proc, raw))
        if hits:
            scores.append((name, hits))
    scores.sort(key=lambda x: -x[1])
    return scores


def read_sample(path, limit):
    lines = []
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                s = line.rstrip("\n")
                if s.strip():
                    lines.append(s)
                if len(lines) >= limit:
                    break
    except FileNotFoundError:
        return None
    return lines


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Replicate the rsyslog->Kafka->engine pipeline locally (no Kafka).")
    ap.add_argument("--rsyslog", help="path to the rsyslog imfile/omkafka .conf")
    ap.add_argument("--config", default="config.yaml", help="engine config.yaml")
    ap.add_argument("--limit", type=int, default=50, help="lines sampled per source (default 50)")
    ap.add_argument("--logs-dir", help="override: read each file's basename from this dir")
    ap.add_argument("--file", help="single-source mode: a log file to test")
    ap.add_argument("--program", help="single-source mode: its source_program (Tag)")
    args = ap.parse_args()

    base_dir = os.path.dirname(os.path.abspath(args.config))
    config = yaml.safe_load(open(args.config))
    program_map = config.get("program_mapping", {}) or {}
    registry = RuleRegistry(
        rules_dir=os.path.join(base_dir, config["paths"]["rules_dir"]),
        program_map=program_map,
    )

    engine_topic = config.get("kafka", {}).get("input_topic", "")
    try:
        engine_topic_re = re.compile(engine_topic)
    except re.error:
        engine_topic_re = None
    engine_brokers = config.get("kafka", {}).get("bootstrap_servers", []) or []
    if isinstance(engine_brokers, str):
        engine_brokers = [engine_brokers]

    # Build the source list
    rs = None
    if args.file:
        sources = [(args.file, args.program or "unknown")]
    elif args.rsyslog:
        rs = parse_rsyslog(args.rsyslog)
        sources = rs["inputs"]
    else:
        ap.error("provide --rsyslog <conf> or --file <log> --program <tag>")

    print(f"{B}Pipeline replication{X} - {len(sources)} source(s), sampling up to {args.limit} line(s) each\n")

    # One-time producer<->engine cross-checks (rsyslog mode)
    if rs:
        if rs["topic"] and engine_topic_re:
            if engine_topic_re.search(rs["topic"]):
                via = "  (matched only by a '.*' wildcard)" if ".*" in engine_topic else ""
                print(f"[{OKW}] Kafka topic '{rs['topic']}' is consumed by the engine{via}")
            else:
                print(f"[{ERRW}] rsyslog sends to topic '{rs['topic']}' but engine.input_topic "
                      f"'{engine_topic}' does NOT match it - engine will never see these logs")
        rsb = set(rs["broker"])
        eb = set(str(b) for b in engine_brokers)
        if rsb and eb and rsb.isdisjoint(eb):
            print(f"[{WARNW}] rsyslog broker {sorted(rsb)} != engine broker {sorted(eb)} "
                  "- make sure these are the same Kafka cluster")
        print()

    healthy, issues = [], []

    for path, tag in sources:
        real_path = path
        if args.logs_dir:
            real_path = os.path.join(args.logs_dir, os.path.basename(path))

        print(f"{B}SOURCE{X}  {tag}   {D}({real_path}){X}")
        source_ok = True

        # 1. would rsyslog forward this tag at all?
        if rs is not None:
            if tag in rs["forward_tags"]:
                print(f"   [{OKW}] rsyslog forwards tag '{tag}' to Kafka")
            else:
                print(f"   [{ERRW}] rsyslog reads '{tag}' but its forwarding rule does NOT include it "
                      f"- these logs never reach Kafka  (add '{tag}' to the omkafka 'if $programname' filter)")
                source_ok = False

        # 2. mapping: source_program -> rule
        resolved = program_map.get(tag) or tag
        proc = registry.engines.get(resolved)
        if proc:
            how = f"via program_mapping -> '{resolved}'" if tag in program_map else f"(direct rule name '{resolved}')"
            print(f"   [{OKW}] source_program '{tag}' resolves to rule '{resolved}' {how}")
        else:
            print(f"   [{ERRW}] no rule for source_program '{tag}' "
                  f"- engine would DLQ every line as 'no_matching_rule'")
            source_ok = False

        # 3. sample + parse
        lines = read_sample(real_path, args.limit)
        if lines is None:
            print(f"   [{WARNW}] sample file not found - cannot test parsing "
                  f"(use --logs-dir to point at sample files)")
            (issues if not source_ok else healthy).append(tag)
            print()
            continue
        if not lines:
            print(f"   [{WARNW}] sample file is empty - nothing to parse")
            print()
            (issues if not source_ok else healthy).append(tag)
            continue

        if proc:
            hits = sum(1 for raw in lines if recognizes(proc, raw))
            n = len(lines)
            if hits == n:
                print(f"   [{OKW}] rule '{resolved}' matches all {n} sampled line(s)")
            elif hits == 0:
                print(f"   [{ERRW}] rule '{resolved}' matched 0/{n} lines - the pattern does not fit this log")
                print(f"          e.g.: {D}{lines[0][:110]}{X}")
                source_ok = False
            else:
                print(f"   [{WARNW}] rule '{resolved}' matched {hits}/{n} lines - some lines have a different format")
                bad = next((l for l in lines if not recognizes(proc, l)), "")
                print(f"          unmatched e.g.: {D}{bad[:110]}{X}")

            # Show a real parsed ECS sample so they can eyeball the output
            ev = first_parsed_event(proc, lines, tag, rs["meta_static"] if rs else {})
            if ev:
                flat = ", ".join(sorted(_leaf_keys(ev))[:8])
                print(f"          {D}parsed ECS fields: {flat} ...{X}")

        # Auto-suggest when nothing resolved or the rule doesn't fit
        if not proc or (proc and sum(1 for raw in lines if recognizes(proc, raw)) == 0):
            sugg = auto_suggest(registry, lines)
            if sugg:
                best, hits = sugg[0]
                print(f"   {B}-> suggestion:{X} these lines best match rule '{G}{best}{X}' "
                      f"({hits}/{len(lines)}). Add to config.yaml:")
                print(f"        {D}program_mapping:\n          {tag}: \"{best}\"{X}")
            else:
                print(f"   {B}-> suggestion:{X} no existing rule matches these lines - write a new rule "
                      f"(see WRITING_RULES.md or ecs_helper)")

        (healthy if source_ok else issues).append(tag)
        print()

    # Summary
    print("=" * 64)
    print(f"  {B}RESULT{X}:  {G}{len(healthy)} source(s) OK{X}, {R}{len(issues)} with issues{X}")
    if issues:
        print(f"  Issues in: {', '.join(issues)}")
        print("  Fix the items marked ERROR above before sending real logs.")
    print("=" * 64)
    sys.exit(1 if issues else 0)


def _leaf_keys(d, prefix=""):
    keys = []
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys.extend(_leaf_keys(v, p))
        else:
            keys.append(p)
    return keys


if __name__ == "__main__":
    main()
