#!/usr/bin/env python3
import argparse
import os
import re
import sys
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import ecs_schema
from core.engine import substitute_vars
from core.timeparse import parse_offset

# Named formats known to core/timeparse.py; anything containing '%' is treated
# as an explicit strptime format string.
TS_FORMATS = {"clf", "iso8601", "rfc3164", "epoch", "suricata",
              "nginx_error", "asctime", "roundcube"}

try:
    from kafka import KafkaAdminClient
except Exception:
    KafkaAdminClient = None

ALLOWED_STRATEGIES = {"stateless", "multi_match", "stateful", "json_map", "xml_xpath"}

# --- ReDoS lint (P1-1) -------------------------------------------------------
# shape 1: a quantified group whose body STARTS with an unbounded quantified
# atom — the ambiguous-split shape behind catastrophic backtracking: (a+)+,
# (\S+\s?)*, ... A group that starts with a FIXED separator the inner class
# cannot match (the safe delimited-list idiom, e.g. (?:\.[\w-]+)* ) has a
# unique decomposition and is fine.
_NESTED_QUANT = re.compile(
    r"\((?:\?:)?"                                # group open (incl. (?: )
    r"(?:\\[SwWdDs]|\.|\[[^\]]+\]|[^()\\\[\]^$|?*+])[*+]"  # leading atom is unbounded
    r"(?:[^()\\]|\\.)*"                          # rest of the body
    r"\)\s*[*+]")                                # ... and the group repeats
# shape 2a: unanchored leading unbounded WILDCARD (\S+ / .+ / \w+ ...), even
# behind named-group opens. On a non-matching line Python re retries at every
# position => O(n^2): an 8KB garbage line measured ~490ms/line against the
# old nginx patterns. Anchoring with ^ makes the same line ~0.3ms.
_WILD_LEAD = re.compile(r"^(?:\(\?P<[^>]+>)*(?:\\[SwWD]|\.)[*+]")
# shape 2b: same position but a narrow class (\d+ / [\d.]+): the class fails
# fast on most positions, so it only matters without a prematch gate.
_NARROW_LEAD = re.compile(r"^(?:\(\?P<[^>]+>)*(?:\\d|\[[^\]]+\])[*+]")


def lint_regex(rx, gated):
    """Static ReDoS check for one rule regex.

    Returns a list of (level, message). `gated` = the pattern (or its rule)
    declares a prematch substring, which already keeps garbage lines away
    from the regex."""
    finds = []
    if _NESTED_QUANT.search(rx):
        finds.append(("ERROR",
                      "nested unbounded quantifier (catastrophic-backtracking "
                      "shape like (x+)+ ) - rewrite the group"))
    if not rx.startswith("^"):
        if _WILD_LEAD.match(rx):
            finds.append(("ERROR",
                          "unanchored leading wildcard quantifier (\\S+/.+/\\w+) "
                          "- O(n^2) scan on garbage lines; anchor it with ^"))
        elif not gated and _NARROW_LEAD.match(rx):
            finds.append(("WARNING",
                          "unanchored leading quantifier without a prematch "
                          "gate - consider anchoring with ^ or adding prematch:"))
    return finds


def report(status, message):
    print(f"[{status}] {message}")


def resolve_path(base_dir, path_value):
    if not path_value:
        return None
    if os.path.isabs(path_value):
        return path_value
    return os.path.join(base_dir, path_value)


def load_config(config_path):
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        report("ERROR", f"Config file not found: {config_path}")
        return None
    except Exception as e:
        report("ERROR", f"Failed to load config: {e}")
        return None


def validate_config_shape(config):
    errors = 0
    warnings = 0

    if not isinstance(config, dict):
        report("ERROR", "Config is not a valid YAML object")
        return 1, 0

    for key in ("kafka", "batch", "paths"):
        if key not in config:
            report("ERROR", f"Missing top-level config section: {key}")
            errors += 1

    kafka = config.get("kafka", {})
    if not isinstance(kafka, dict):
        report("ERROR", "kafka section must be a mapping")
        errors += 1
    else:
        if not kafka.get("bootstrap_servers"):
            report("ERROR", "kafka.bootstrap_servers is missing")
            errors += 1
        if not kafka.get("input_topic"):
            report("ERROR", "kafka.input_topic is missing")
            errors += 1
        if not kafka.get("group_id"):
            report("WARN", "kafka.group_id is missing")
            warnings += 1
        if not kafka.get("auto_offset_reset"):
            report("WARN", "kafka.auto_offset_reset is missing")
            warnings += 1

    batch = config.get("batch", {})
    if not isinstance(batch, dict):
        report("ERROR", "batch section must be a mapping")
        errors += 1
    else:
        if not isinstance(batch.get("size"), int) or batch.get("size", 0) <= 0:
            report("ERROR", "batch.size must be a positive integer")
            errors += 1
        if not isinstance(batch.get("timeout_sec"), (int, float)) or batch.get("timeout_sec", 0) <= 0:
            report("ERROR", "batch.timeout_sec must be a positive number")
            errors += 1

    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        report("ERROR", "paths section must be a mapping")
        errors += 1
    else:
        if not paths.get("output_dir"):
            report("ERROR", "paths.output_dir is missing")
            errors += 1
        if not paths.get("rules_dir"):
            report("ERROR", "paths.rules_dir is missing")
            errors += 1

    program_mapping = config.get("program_mapping")
    if program_mapping is None:
        report("WARN", "program_mapping is missing (optional)")
        warnings += 1
    elif not isinstance(program_mapping, dict):
        report("ERROR", "program_mapping must be a mapping")
        errors += 1

    # optional redis: block (only the stateful strategy uses it)
    rc = config.get("redis")
    if rc is not None:
        if not isinstance(rc, dict):
            report("ERROR", "redis section must be a mapping (host/port/db/password)")
            errors += 1
        else:
            for key in ("port", "db"):
                if key in rc:
                    try:
                        int(rc[key])
                    except (TypeError, ValueError):
                        report("ERROR", f"redis.{key} must be an integer")
                        errors += 1

    return errors, warnings


def validate_paths(base_dir, config):
    errors = 0
    warnings = 0

    rules_dir = resolve_path(base_dir, config.get("paths", {}).get("rules_dir"))
    output_dir = resolve_path(base_dir, config.get("paths", {}).get("output_dir"))

    if rules_dir and not os.path.isdir(rules_dir):
        report("ERROR", f"Rules directory not found: {rules_dir}")
        errors += 1
    elif rules_dir:
        yaml_files = [f for f in os.listdir(rules_dir) if f.endswith(".yaml")]
        if not yaml_files:
            report("ERROR", f"No .yaml rule files found in {rules_dir}")
            errors += 1

    if output_dir:
        if os.path.isdir(output_dir):
            if not os.access(output_dir, os.W_OK):
                report("WARN", f"Output directory is not writable: {output_dir}")
                warnings += 1
        else:
            parent = os.path.dirname(output_dir) or "."
            if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
                report("WARN", f"Output directory does not exist or is not writable: {output_dir}")
                warnings += 1

    geoip = config.get("geoip", {})
    if isinstance(geoip, dict) and geoip.get("enabled"):
        db_path = geoip.get("db_path")
        db_abs = resolve_path(base_dir, db_path)
        if db_abs and not os.path.exists(db_abs):
            report("WARN", f"GeoIP database not found: {db_abs}")
            warnings += 1

        asn_path = geoip.get("asn_db_path")
        asn_abs = resolve_path(base_dir, asn_path)
        if asn_abs and not os.path.exists(asn_abs):
            report("WARN", f"ASN database not found: {asn_abs} (ASN enrichment will be skipped)")
            warnings += 1

    return errors, warnings


def validate_rules(base_dir, config):
    errors = 0
    warnings = 0

    rules_dir = resolve_path(base_dir, config.get("paths", {}).get("rules_dir"))
    if not rules_dir or not os.path.isdir(rules_dir):
        return 1, 0, {}

    rules = {}
    for filename in sorted(os.listdir(rules_dir)):
        if not filename.endswith(".yaml"):
            continue

        path = os.path.join(rules_dir, filename)
        try:
            with open(path, "r") as f:
                rule = yaml.safe_load(f)
        except Exception as e:
            report("ERROR", f"Failed to read rule {filename}: {e}")
            errors += 1
            continue

        if not isinstance(rule, dict):
            report("ERROR", f"Rule {filename} is not a valid mapping")
            errors += 1
            continue

        pattern_name = rule.get("pattern_name") or filename.replace(".yaml", "")
        if pattern_name in rules:
            report("ERROR", f"Duplicate pattern_name: {pattern_name}")
            errors += 1

        # Optional `vars:` block — site-tunable values substituted into the
        # rule's regexes at load (core/engine.py substitute_vars). Validate
        # its shape, then resolve it HERE so every regex check below compiles
        # the same final regex the engine will run.
        vars_block = rule.get("vars")
        if vars_block is not None and not isinstance(vars_block, dict):
            report("ERROR", f"Rule {pattern_name}: vars must be a mapping")
            errors += 1
            continue
        if isinstance(vars_block, dict):
            bad = [str(k) for k, v in vars_block.items()
                   if not (isinstance(v, str) or
                           (isinstance(v, list) and v and
                            all(isinstance(i, str) and i for i in v)))]
            if bad:
                report("ERROR",
                       f"Rule {pattern_name}: vars {', '.join(sorted(bad))} "
                       "must each be a string or a non-empty list of strings")
                errors += 1
                continue
        try:
            rule = substitute_vars(rule)
        except ValueError as e:
            report("ERROR", f"Rule {pattern_name}: invalid vars: {e}")
            errors += 1
            continue

        rules[pattern_name] = rule

        strategy = rule.get("strategy")
        if not strategy:
            report("ERROR", f"Rule {pattern_name} missing strategy")
            errors += 1
            continue
        if strategy not in ALLOWED_STRATEGIES:
            report("ERROR", f"Rule {pattern_name} has invalid strategy: {strategy}")
            errors += 1
            continue

        def run_lint(rx, gated, where):
            e = w = 0
            for level, msg in lint_regex(rx, gated):
                report(level, f"Rule {pattern_name} {where}: {msg}")
                if level == "ERROR":
                    e += 1
                else:
                    w += 1
            return e, w

        rule_gated = bool(rule.get("prematch"))

        if strategy == "stateless":
            regex = rule.get("regex")
            if not regex:
                report("ERROR", f"Rule {pattern_name} missing regex")
                errors += 1
            else:
                try:
                    re.compile(regex)
                except re.error as e:
                    report("ERROR", f"Rule {pattern_name} has invalid regex: {e}")
                    errors += 1
                le, lw = run_lint(regex, rule_gated, "regex")
                errors += le
                warnings += lw

        def check_prematch(spec, where):
            # prematch must be a plain substring (or list of substrings); a
            # regex here would silently gate wrong because it is matched with
            # `in`, not re.search.
            if spec is None:
                return 0
            vals = spec if isinstance(spec, list) else [spec]
            if not vals or not all(isinstance(v, str) and v for v in vals):
                report("ERROR",
                       f"Rule {pattern_name} {where}: prematch must be a "
                       "non-empty string or list of strings")
                return 1
            return 0

        errors += check_prematch(rule.get("prematch"), "rule-level")

        def check_timestamp(spec, where):
            # Validates the `timestamp:` block (event-time extraction).
            if spec is None:
                return 0
            errs = 0
            if not isinstance(spec, dict):
                report("ERROR", f"Rule {pattern_name} {where}: timestamp must be a mapping")
                return 1
            if not any(spec.get(k) for k in ("group", "field", "regex")):
                report("ERROR",
                       f"Rule {pattern_name} {where}: timestamp needs one of "
                       "group / field / regex")
                errs += 1
            fmt = spec.get("format", "iso8601")
            if not isinstance(fmt, str) or (fmt not in TS_FORMATS and "%" not in fmt):
                report("ERROR",
                       f"Rule {pattern_name} {where}: unknown timestamp format "
                       f"'{fmt}' (use one of {sorted(TS_FORMATS)} or a strptime "
                       "string containing %)")
                errs += 1
            if spec.get("regex"):
                try:
                    re.compile(spec["regex"])
                except re.error as e:
                    report("ERROR",
                           f"Rule {pattern_name} {where}: invalid timestamp regex: {e}")
                    errs += 1
            tz = spec.get("tz")
            # "assume_utc" is the explicit acknowledgment that a zoneless
            # format really is UTC (silences the engine's rule-load lint);
            # IANA zone IDs (Asia/Kolkata) are resolved via zoneinfo.
            if (tz is not None and tz != "assume_utc"
                    and parse_offset(str(tz)) is None):
                report("ERROR",
                       f"Rule {pattern_name} {where}: timestamp tz '{tz}' must be "
                       "a numeric offset like '+05:30', an IANA zone like "
                       "'Asia/Kolkata', UTC/GMT/Z, or 'assume_utc' (ambiguous "
                       "abbreviations like IST are refused)")
                errs += 1
            # Optional secondary timestamp for dual-timestamp arbitration:
            # same schema, validated the same way.
            if spec.get("alt") is not None:
                errs += check_timestamp(spec.get("alt"), f"{where} alt")
            return errs

        errors += check_timestamp(rule.get("timestamp"), "rule-level")

        ttl = rule.get("state_ttl_sec")
        if ttl is not None:
            if strategy != "stateful":
                report("WARN", f"Rule {pattern_name}: state_ttl_sec only applies to stateful rules")
                warnings += 1
            try:
                if int(ttl) < 30:
                    report("WARN", f"Rule {pattern_name}: state_ttl_sec < 30 is clamped to 30")
                    warnings += 1
            except (TypeError, ValueError):
                report("ERROR", f"Rule {pattern_name}: state_ttl_sec must be a number")
                errors += 1

        if strategy in ("multi_match", "stateful"):
            patterns = rule.get("patterns")
            if not isinstance(patterns, list) or not patterns:
                report("ERROR", f"Rule {pattern_name} has empty patterns")
                errors += 1
            else:
                for idx, pattern in enumerate(patterns, start=1):
                    if not isinstance(pattern, dict):
                        report("ERROR", f"Rule {pattern_name} pattern #{idx} is not a mapping")
                        errors += 1
                        continue
                    errors += check_prematch(pattern.get("prematch"),
                                             f"pattern #{idx}")
                    errors += check_timestamp(pattern.get("timestamp"),
                                              f"pattern #{idx}")
                    pregex = pattern.get("regex")
                    if not pregex:
                        report("ERROR", f"Rule {pattern_name} pattern #{idx} missing regex")
                        errors += 1
                        continue
                    try:
                        re.compile(pregex)
                    except re.error as e:
                        report("ERROR", f"Rule {pattern_name} pattern #{idx} invalid regex: {e}")
                        errors += 1
                    le, lw = run_lint(pregex,
                                      rule_gated or bool(pattern.get("prematch")),
                                      f"pattern #{idx}")
                    errors += le
                    warnings += lw

        if strategy == "stateful":
            id_regex = rule.get("id_regex")
            end_signal = rule.get("end_signal")
            if not id_regex:
                report("ERROR", f"Rule {pattern_name} missing id_regex")
                errors += 1
            else:
                try:
                    re.compile(id_regex)
                except re.error as e:
                    report("ERROR", f"Rule {pattern_name} invalid id_regex: {e}")
                    errors += 1
                # id_regex runs on EVERY line of the source - never gated
                le, lw = run_lint(id_regex, False, "id_regex")
                errors += le
                warnings += lw
            if not end_signal:
                report("ERROR", f"Rule {pattern_name} missing end_signal")
                errors += 1

        if strategy == "json_map":
            mapping = rule.get("mapping")
            if not isinstance(mapping, dict) or not mapping:
                report("ERROR", f"Rule {pattern_name} missing mapping")
                errors += 1

        if strategy == "xml_xpath":
            mapping = rule.get("mapping")
            if not isinstance(mapping, dict) or not mapping:
                report("ERROR", f"Rule {pattern_name} missing mapping")
                errors += 1
            if not rule.get("items_xpath"):
                report("ERROR", f"Rule {pattern_name} missing items_xpath")
                errors += 1

    return errors, warnings, rules


def validate_program_mapping(config, rules):
    errors = 0
    warnings = 0

    program_mapping = config.get("program_mapping", {})
    if not isinstance(program_mapping, dict):
        return errors, warnings

    for source_program, mapped in program_mapping.items():
        # value = one rule name, or a LIST of rule names (chain: first rule
        # that handles a line wins)
        names = mapped if isinstance(mapped, list) else [mapped]
        if not names or not all(isinstance(n, str) and n for n in names):
            report("ERROR", f"program_mapping {source_program}: value must be a "
                            "rule name or a list of rule names")
            errors += 1
            continue
        for rule_name in names:
            if rule_name not in rules:
                report("ERROR", f"program_mapping {source_program} -> {rule_name} has no matching rule")
                errors += 1

    if program_mapping and not rules:
        report("ERROR", "program_mapping defined but no rules were loaded")
        errors += 1

    return errors, warnings


def _rule_targets(rule):
    """Yield (ecs_target, location) for every mapping value and static key."""
    out = []

    def block(mapping, static, where):
        if isinstance(mapping, dict):
            for src, tgt in mapping.items():
                # a list value fans one capture out to several fields
                for t in (tgt if isinstance(tgt, list) else [tgt]):
                    out.append((t, f"{where}[{src}]"))
        if isinstance(static, dict):
            for key in static.keys():
                out.append((key, f"{where} static"))

    block(rule.get("mapping"), rule.get("static"), "mapping")
    for i, p in enumerate(rule.get("patterns") or [], 1):
        if isinstance(p, dict):
            block(p.get("mapping"), p.get("static"), p.get("name") or f"pattern#{i}")
    return out


def validate_ecs_fields(rules):
    """Ensure every mapping target is a valid ECS field. Known wrong names and
    typos are errors (with the correct ECS field shown); genuine custom fields
    that ECS has no place for are allowed."""
    errors = 0
    custom = 0
    for name, rule in rules.items():
        if not isinstance(rule, dict):
            continue
        for tgt, loc in _rule_targets(rule):
            if not isinstance(tgt, str):
                continue
            status, suggestion = ecs_schema.classify(tgt)
            if status in ("alias", "typo"):
                report("ERROR", f"Rule {name}: '{tgt}' is not ECS - use '{suggestion}' ({loc})")
                errors += 1
            elif status == "custom":
                custom += 1
    if custom:
        report("INFO", f"{custom} custom (non-ECS) field(s) allowed")
    return errors, 0


def validate_kafka(config, timeout_sec):
    errors = 0
    warnings = 0

    kafka = config.get("kafka", {})
    bootstrap = kafka.get("bootstrap_servers")
    input_topic = kafka.get("input_topic")

    if not bootstrap or not input_topic:
        return 1, 0

    if KafkaAdminClient is None:
        report("ERROR", "Kafka client not available. Install kafka-python-ng")
        return 1, 0

    try:
        admin = KafkaAdminClient(
            bootstrap_servers=bootstrap,
            request_timeout_ms=int(timeout_sec * 1000),
            api_version_auto_timeout_ms=int(timeout_sec * 1000)
        )
    except Exception as e:
        report("ERROR", f"Kafka connection failed: {e}")
        return 1, 0

    try:
        topics = sorted(admin.list_topics())
    except Exception as e:
        report("ERROR", f"Failed to list Kafka topics: {e}")
        return 1, 0
    finally:
        try:
            admin.close()
        except Exception:
            pass

    try:
        topic_re = re.compile(input_topic)
    except re.error as e:
        report("ERROR", f"kafka.input_topic is not a valid regex: {e}")
        return 1, 0

    matches = [t for t in topics if topic_re.search(t)]
    if matches:
        report("OK", f"Kafka reachable, matched topics: {', '.join(matches[:10])}")
        if len(matches) > 10:
            report("WARN", f"More than 10 topics matched ({len(matches)} total)")
            warnings += 1
    else:
        report("WARN", "Kafka reachable but no topics match input_topic")
        warnings += 1

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="Validate config.yaml and rules for deployment readiness.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--skip-kafka", action="store_true", help="Skip Kafka connectivity checks")
    parser.add_argument("--timeout", type=int, default=5, help="Kafka connection timeout in seconds")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(args.config))
    config = load_config(args.config)
    if config is None:
        sys.exit(2)

    total_errors = 0
    total_warnings = 0

    report("INFO", "Validating config structure")
    errors, warnings = validate_config_shape(config)
    total_errors += errors
    total_warnings += warnings

    report("INFO", "Validating paths and GeoIP")
    errors, warnings = validate_paths(base_dir, config)
    total_errors += errors
    total_warnings += warnings

    report("INFO", "Validating rules")
    errors, warnings, rules = validate_rules(base_dir, config)
    total_errors += errors
    total_warnings += warnings

    report("INFO", "Validating program mapping")
    errors, warnings = validate_program_mapping(config, rules)
    total_errors += errors
    total_warnings += warnings

    report("INFO", "Validating ECS field compliance")
    errors, warnings = validate_ecs_fields(rules)
    total_errors += errors
    total_warnings += warnings

    if args.skip_kafka:
        report("INFO", "Skipping Kafka connectivity checks")
    else:
        report("INFO", "Validating Kafka connectivity")
        errors, warnings = validate_kafka(config, args.timeout)
        total_errors += errors
        total_warnings += warnings

    if total_errors:
        report("ERROR", f"Config validation failed: {total_errors} errors, {total_warnings} warnings")
        sys.exit(2)

    report("OK", f"Config validation passed: {total_warnings} warnings")
    sys.exit(0)


if __name__ == "__main__":
    main()
