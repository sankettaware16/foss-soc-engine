#!/usr/bin/env python3
"""Generate the Elasticsearch index template from the shipped rules.

Walks every rules/*.yaml, collects every field the engine can emit (mapping
targets, static keys, engine-added fields, GeoIP/ASN enrichment), assigns
each an Elasticsearch type, and writes `soc-index-template.json`.

Why this exists (audit P1-5): without a template, the first document decides
each field's mapping — a string "2" makes event.severity text, a null makes
url.query useless, and the index breaks for every later document that
disagrees. Loading this template BEFORE the first event makes day-one
mapping conflicts impossible for every known field.

Regenerate after adding or changing rules:
    python elasticsearch/generate_template.py
The script REFUSES to write if two rules disagree about a field's shape
(scalar vs object) — fix the rule, don't hand-edit the JSON.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import yaml

RULES_DIR = os.path.join(ROOT, "rules")
OUT_PATH = os.path.join(HERE, "soc-index-template.json")

# Fields the ENGINE adds (core/engine.py + utils/geoip.py), not the rules.
ENGINE_FIELDS = {
    "@timestamp": "date",
    "ecs.version": "keyword",
    "event.ingested": "date",
    "event.timestamp_source": "keyword",
    "event.original": "match_only_text",
    "event.original_time": "keyword",
    "event.module": "keyword",
    "event.id": "keyword",
    "event.incomplete": "boolean",
    "event.reason": "keyword",
    "event.outcome": "keyword",
    "observer.source_program": "keyword",
    "source.geo.country_name": "keyword",
    "source.geo.country_iso_code": "keyword",
    "source.geo.city_name": "keyword",
    "source.geo.location": "geo_point",
    "source.as.number": "long",
    "source.as.organization.name": "keyword",
    "destination.geo.country_name": "keyword",
    "destination.geo.country_iso_code": "keyword",
    "destination.geo.city_name": "keyword",
    "destination.geo.location": "geo_point",
    "destination.as.number": "long",
    "destination.as.organization.name": "keyword",
    "source.geo.name": "keyword",
    "destination.geo.name": "keyword",
}

# Exact-name type overrides (ECS-typed or semantically known).
TYPE_OVERRIDES = {
    "event.created": "date",       # rules only map ISO-8601 strings here
    "event.severity": "long",
    "event.duration": "long",
    "event.risk_score": "float",
    "host.risk_score": "float",
    "vulnerability.severity": "float",
    "vulnerability.score.base": "float",
    "vulnerability.score.vpr": "float",
    "vulnerability.score.epss": "float",
    "vulnerability.confidence": "long",
    "email.size": "long",
    "email.recipient_count": "long",
    "nessus.scan_start_epoch": "long",
    "nessus.scan_end_epoch": "long",
    "nessus.severity_counts.critical": "long",
    "nessus.severity_counts.high": "long",
    "nessus.severity_counts.medium": "long",
    "nessus.severity_counts.low": "long",
    "nessus.severity_counts.info": "long",
}

# Suffix heuristics, applied when no override / |hint decides.
_SUFFIX_RULES = [
    (re.compile(r"(^|\.)ip$"), "ip"),
    (re.compile(r"(^|\.)port$"), "long"),
    (re.compile(r"(^|\.)bytes$"), "long"),
    (re.compile(r"(^|\.)pid$"), "long"),
    (re.compile(r"thread\.id$"), "long"),
    (re.compile(r"status_code$"), "long"),
    (re.compile(r"(_|\.)count$"), "long"),
]


def field_type(name, hint=None):
    if name in ENGINE_FIELDS:
        return ENGINE_FIELDS[name]
    if name in TYPE_OVERRIDES:
        return TYPE_OVERRIDES[name]
    if hint == "int":
        return "long"
    if hint == "float":
        return "float"
    for rx, typ in _SUFFIX_RULES:
        if rx.search(name):
            return typ
    if hint == "bool":
        return "boolean"
    return "keyword"


def collect_rule_fields():
    fields = {}

    def add(target, hint=None):
        if not isinstance(target, str) or not target:
            return
        name, _, dtype = target.partition("|")
        t = field_type(name, dtype or hint)
        prev = fields.get(name)
        if prev and prev != t:
            # numeric beats keyword (a |int in one rule wins), else keep the
            # first and let the conflict check below catch real trouble
            if "keyword" in (prev, t):
                t = prev if prev != "keyword" else t
        fields[name] = t

    for fname in sorted(os.listdir(RULES_DIR)):
        if not fname.endswith(".yaml"):
            continue
        with open(os.path.join(RULES_DIR, fname), encoding="utf-8") as f:
            rule = yaml.safe_load(f)
        if not isinstance(rule, dict):
            continue
        for tgt in (rule.get("mapping") or {}).values():
            add(tgt)
        for key, val in (rule.get("static") or {}).items():
            hint = ("bool" if isinstance(val, bool)
                    else "int" if isinstance(val, int)
                    else "float" if isinstance(val, float) else None)
            add(key, hint)
        for p in rule.get("patterns") or []:
            if not isinstance(p, dict):
                continue
            for tgt in (p.get("mapping") or {}).values():
                add(tgt)
            for key, val in (p.get("static") or {}).items():
                hint = ("bool" if isinstance(val, bool)
                        else "int" if isinstance(val, int)
                        else "float" if isinstance(val, float) else None)
                add(key, hint)

    fields.update(ENGINE_FIELDS)
    return fields


def check_conflicts(fields):
    """A field mapped as a scalar AND used as an object prefix elsewhere
    cannot exist in one index — refuse to generate."""
    problems = []
    names = set(fields)
    for name in names:
        prefix = name + "."
        clashes = [o for o in names if o.startswith(prefix)]
        if clashes:
            problems.append(f"{name} is a scalar but also an object "
                            f"prefix of: {', '.join(sorted(clashes))}")
    return problems


def to_properties(fields):
    root = {}
    for name in sorted(fields):
        if name == "@timestamp":
            root["@timestamp"] = {"type": "date"}
            continue
        node = root
        parts = name.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {}).setdefault("properties", {})
        leaf = {"type": fields[name]}
        if fields[name] == "keyword":
            leaf["ignore_above"] = 1024
        node[parts[-1]] = leaf
    return root


def main():
    fields = collect_rule_fields()
    problems = check_conflicts(fields)
    if problems:
        print("REFUSING to generate - object/scalar conflicts in the rules:")
        for p in problems:
            print(f"  - {p}")
        return 1

    template = {
        "index_patterns": ["soc-*"],
        "priority": 200,
        "template": {
            "settings": {
                "index": {"mapping": {"ignore_malformed": True}}
            },
            "mappings": {
                "dynamic_templates": [
                    {
                        "strings_as_keyword": {
                            "match_mapping_type": "string",
                            "mapping": {"type": "keyword",
                                        "ignore_above": 1024},
                        }
                    }
                ],
                "properties": to_properties(fields),
            },
        },
        "_meta": {
            "description": "FOSS SOC Engine - generated by "
                           "elasticsearch/generate_template.py; do not "
                           "hand-edit, regenerate after rule changes",
            "field_count": len(fields),
        },
    }
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(template, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {OUT_PATH} ({len(fields)} fields)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
