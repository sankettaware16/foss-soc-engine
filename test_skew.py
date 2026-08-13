"""
test_skew.py — regression test for timestamp skew detection & correction.

Covers the verification matrix of the 2026-08-13 enhancement request
(false-DDoS incident: 706k nginx error events indexed +5:30 in the future
because a source host stamps IST wall-clock labeled +00:00):

  T1  correctly-zoned time            -> untouched by the gate
  T2  future time on a clean timezone quantum -> corrected (log_skew_corrected)
  T3  zoneless format assumed UTC, actually local -> same correction path
  T4  future time NOT on a quantum    -> ingest fallback (never "corrected"
      into a plausible lie)
  T5  small future drift within tolerance -> untouched
  T6  mode: backfill                  -> gate disabled entirely
  T7  tag_only                        -> @timestamp unchanged, tagged
  T8  alt: dual-timestamp arbitration -> lying primary loses to plausible
      alternate; disagreement always recorded
  OFF skew_correction: off (default)  -> output identical to the old engine

Standalone: real engine, synthetic rules, timestamps generated relative to
the real clock (no mocking). Run:  python3 test_skew.py   (exit 0 = pass)
"""
import os
import sys
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.engine as eng
from core.engine import UniversalEngine, configure_timestamp_validation
from core.schema import LogInput

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(timezone.utc).replace(microsecond=0)
TRUE = NOW - timedelta(seconds=6)          # the event really happened 6s ago
WALL = TRUE.astimezone(IST)                # its IST wall-clock reading

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def clf(dt_local, offset):
    return (f"{dt_local.day:02d}/{MONTHS[dt_local.month - 1]}/{dt_local.year}:"
            f"{dt_local:%H:%M:%S} {offset}")


def lying_prefix(dt_utc_true):
    """rsyslog prefix as the broken hosts write it: IST wall time labeled UTC."""
    wall = dt_utc_true.astimezone(IST)
    return wall.strftime("%Y-%m-%dT%H:%M:%S+00:00")


# Synthetic single-purpose rules (same shapes as the production nginx rules)
RULE_CLF = {  # trusts the CLF time inside the line (carries its own offset)
    "pattern_name": "t_clf", "strategy": "stateless",
    "regex": r"\[(?P<timestamp>[^\]]+)\]",
    "timestamp": {"group": "timestamp", "format": "clf"},
    "mapping": {},
}
RULE_PREFIX = {  # trusts the (lying) ISO prefix — the incident shape
    "pattern_name": "t_prefix", "strategy": "stateless",
    "regex": r".",
    "timestamp": {"regex": r"^(?P<ts>\S+)", "format": "iso8601"},
    "mapping": {},
}
RULE_ZONELESS = {  # nginx_error-style body time, no tz declared
    "pattern_name": "t_zoneless", "strategy": "stateless",
    "regex": r": (?P<timestamp>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})",
    "timestamp": {"group": "timestamp", "format": "nginx_error"},
    "mapping": {},
}
RULE_ALT_PREFIX_PRIMARY = {  # primary = lying prefix, alt = trustworthy CLF
    "pattern_name": "t_alt_a", "strategy": "stateless",
    "regex": r"\[(?P<clfts>[^\]]+)\]",
    "timestamp": {"regex": r"^(?P<ts>\S+)", "format": "iso8601",
                  "alt": {"group": "clfts", "format": "clf"}},
    "mapping": {},
}
RULE_ALT_CLF_PRIMARY = {  # primary = trustworthy CLF, alt = lying prefix
    "pattern_name": "t_alt_b", "strategy": "stateless",
    "regex": r"\[(?P<clfts>[^\]]+)\]",
    "timestamp": {"group": "clfts", "format": "clf",
                  "alt": {"regex": r"^(?P<ts>\S+)", "format": "iso8601"}},
    "mapping": {},
}


def run(rule, raw):
    engine = UniversalEngine(rule)
    out = engine.process(LogInput(json.dumps(
        {"meta": {"source_program": rule["pattern_name"]}, "raw": raw})))
    assert out is not None, f"line did not parse: {raw!r}"
    return out


def set_mode(**kw):
    cfg = {"skew_correction": "off", "mode": "live",
           "future_tolerance_sec": 300, "quantum_sec": 900, "jitter_sec": 120}
    cfg.update(kw)
    configure_timestamp_validation(cfg)


def check(name, cond, detail=""):
    if not cond:
        print(f"[FAIL] {name}  {detail}")
        sys.exit(1)
    print(f"[PASS] {name}")


def main():
    true_iso = TRUE.isoformat()
    lie_iso = lying_prefix(TRUE)                 # claims +5:30 future
    line_clf_good = f"{lie_iso} host prog: x [{clf(WALL, '+0530')}] ok"
    line_prefix = f"{lie_iso} host prog: whatever"
    line_zoneless = f"{lie_iso} host prog: {WALL:%Y/%m/%d %H:%M:%S} [alert] boom"

    # T1 — correctly-zoned CLF stays byte-identical, gate on
    set_mode(skew_correction="correct")
    ev = run(RULE_CLF, line_clf_good)
    check("T1 correct-zone untouched",
          ev["@timestamp"] == true_iso
          and ev["event"]["timestamp_source"] == "log"
          and "timestamp_skew_seconds" not in ev["event"],
          f'got {ev["@timestamp"]} {ev["event"]["timestamp_source"]}')

    # T2 — lying prefix (+5:30, clean quantum) is corrected
    ev = run(RULE_PREFIX, line_prefix)
    check("T2 quantized future corrected",
          ev["@timestamp"] == true_iso
          and ev["event"]["timestamp_source"] == "log_skew_corrected"
          and ev["event"]["timestamp_skew_seconds"] == 19800
          and ev["event"]["timestamp_raw"].startswith(lie_iso[:19]),
          f'got {ev["@timestamp"]} {ev["event"]}')

    # T3 — zoneless body time assumed UTC (actually IST) takes the same path
    ev = run(RULE_ZONELESS, line_zoneless)
    check("T3 log_assumed_utc corrected",
          ev["@timestamp"] == true_iso
          and ev["event"]["timestamp_source"] == "log_skew_corrected"
          and ev["event"]["timestamp_skew_seconds"] == 19800,
          f'got {ev["@timestamp"]} {ev["event"]["timestamp_source"]}')

    # T4 — future but NOT on a quantum -> ingest fallback, never "corrected"
    garbage = (TRUE + timedelta(hours=7, minutes=7)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")
    ev = run(RULE_PREFIX, f"{garbage} host prog: whatever")
    check("T4 non-quantized future -> ingest fallback",
          ev["event"]["timestamp_source"] == "ingest_fallback"
          and ev["event"]["timestamp_reject_reason"] == "future_nonquantized"
          and ev["@timestamp"] == ev["event"]["ingested"],
          f'got {ev["event"]}')

    # T5 — 2 minutes of clock drift is inside tolerance: untouched
    drift = (NOW + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ev = run(RULE_PREFIX, f"{drift} host prog: whatever")
    check("T5 small drift untouched",
          ev["event"]["timestamp_source"] == "log"
          and "timestamp_skew_seconds" not in ev["event"],
          f'got {ev["event"]}')

    # T6 — backfill mode disables the gate even for wild timestamps
    set_mode(skew_correction="correct", mode="backfill")
    ev = run(RULE_PREFIX, line_prefix)
    check("T6 backfill mode disables gate",
          ev["event"]["timestamp_source"] == "log"
          and "timestamp_skew_seconds" not in ev["event"],
          f'got {ev["event"]}')

    # T7 — tag_only flags but does not touch @timestamp
    set_mode(skew_correction="tag_only")
    ev = run(RULE_PREFIX, line_prefix)
    check("T7 tag_only flags without correcting",
          ev["@timestamp"].startswith(lie_iso[:19])
          and ev["event"]["timestamp_source"] == "log_future_flagged"
          and ev["event"]["timestamp_skew_seconds"] == 19800,
          f'got {ev["@timestamp"]} {ev["event"]}')

    # T8a — arbitration: lying primary loses to the plausible alternate
    # (rule opt-in: works even with the gate off)
    set_mode(skew_correction="off")
    ev = run(RULE_ALT_PREFIX_PRIMARY, line_clf_good)
    check("T8a alt selected over lying primary",
          ev["@timestamp"] == true_iso
          and ev["event"]["timestamp_source"] == "log_alt_selected"
          and ev["event"]["timestamp_skew_seconds"] == 19800
          and ev["event"]["timestamp_raw"].startswith(lie_iso[:19]),
          f'got {ev["@timestamp"]} {ev["event"]}')

    # T8b — plausible primary is kept; the alternate's lie is still recorded
    ev = run(RULE_ALT_CLF_PRIMARY, line_clf_good)
    check("T8b primary kept, disagreement recorded",
          ev["@timestamp"] == true_iso
          and ev["event"]["timestamp_source"] == "log"
          and ev["event"]["timestamp_skew_seconds"] == -19800,
          f'got {ev["@timestamp"]} {ev["event"]}')

    # OFF — default config: byte-identical to the old engine, lie and all
    set_mode(skew_correction="off")
    ev = run(RULE_PREFIX, line_prefix)
    check("OFF default = old behavior",
          ev["@timestamp"].startswith(lie_iso[:19])
          and ev["event"]["timestamp_source"] == "log"
          and "timestamp_skew_seconds" not in ev["event"]
          and "timestamp_raw" not in ev["event"],
          f'got {ev["event"]}')

    print("-" * 60)
    print("all skew tests passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        # never leave module state enabled for whoever imports engine next
        configure_timestamp_validation({"skew_correction": "off"})
