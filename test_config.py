#!/usr/bin/env python3
import argparse
import os
import re
import sys
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import ecs_schema

try:
    from kafka import KafkaAdminClient
except Exception:
    KafkaAdminClient = None

ALLOWED_STRATEGIES = {"stateless", "multi_match", "stateful", "json_map", "xml_xpath"}


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

    for source_program, rule_name in program_mapping.items():
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
                out.append((tgt, f"{where}[{src}]"))
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
