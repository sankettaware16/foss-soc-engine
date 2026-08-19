"""
test_internal_map.py — regression test for the internal IP map enrichment.

Covers:
  1. Range syntax: CIDR / full range / short (last-octet) range / single IP /
     comma lists / YAML lists — and the error cases (bad IPs, reversed ranges,
     out-of-range octets, IPv6 short ranges, mixed-version ranges).
  2. Overlap semantics: nested ranges MERGE (broad building + narrow room),
     the more specific range wins conflicts, equal spans -> later definition
     wins, boundaries are inclusive, gaps stay unmapped.
  3. The client: config-driven load, LRU behavior, reload on edit, disabled /
     unconfigured modes cost one boolean and change nothing.
  4. Engine integration: source AND destination enrichment, merge with a
     GeoIP result on the same endpoint WITHOUT mutating the geoip cache,
     events untouched when the map is off.

No Kafka/Redis/geoip2 needed. Run:  python3 test_internal_map.py  (exit 0 = pass)
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from utils.internal_map import (InternalIPMap, parse_range_spec, load_map_text,
                                build_tables)
from utils.geoip import GeoIPClient
from core.engine import UniversalEngine
from core.schema import LogInput

RESULTS = []


def check(label, ok, note=""):
    RESULTS.append((label, bool(ok), note))


def reset_map_singleton():
    InternalIPMap._instance = None


def reset_geo_singleton():
    GeoIPClient._instance = None
    GeoIPClient._reader = None
    GeoIPClient._asn_reader = None


RULE = {
    "pattern_name": "t_internal",
    "strategy": "stateless",
    "regex": r"src=(?P<src>\S+) dst=(?P<dst>\S+)",
    "mapping": {"src": "source.ip", "dst": "destination.ip"},
}


def run_line(raw):
    eng = UniversalEngine(RULE)
    env = json.dumps({"meta": {"source_program": "t_internal"}, "raw": raw})
    return eng.process(LogInput(env))


# --------------------------------------------------------------------------
# 1. Range parsing
# --------------------------------------------------------------------------
def test_parsing():
    def one(spec):
        return parse_range_spec(spec)[0]

    lo, hi, ver = one("10.77.152.0/24")
    check("cidr", (lo, hi, ver) == (0x0A4D9800, 0x0A4D98FF, 4))
    lo, hi, ver = one("10.77.156.1-10.77.156.10")
    check("full range", (lo, hi, ver) == (0x0A4D9C01, 0x0A4D9C0A, 4))
    check("short range == full range",
          one("10.77.156.1-10") == one("10.77.156.1-10.77.156.10"))
    lo, hi, ver = one("10.88.3.12")
    check("single ip", lo == hi and ver == 4)
    check("short range to 255", one("10.77.152.1-255")[1] == 0x0A4D98FF)

    r = parse_range_spec("10.77.157.11-15, 10.77.15.1-5")
    check("comma list", len(r) == 2 and r[1][2] == 4)
    r = parse_range_spec(["10.77.1.1-255", "10.77.2.1-255"])
    check("yaml list", len(r) == 2)
    lo, hi, ver = one("fdc0:beef::/64")
    check("v6 cidr", ver == 6 and hi - lo == 2 ** 64 - 1)
    check("v6 full range", one("fdc0::1-fdc0::9")[1] - one("fdc0::1-fdc0::9")[0] == 8)

    for bad in ["10.77.999.1-5", "10.77.156.10-2", "10.0.0.1-999",
                "fdc0::1-9", "10.0.0.1-fdc0::2", "", "banana",
                "10.0.0.0/33"]:
        try:
            parse_range_spec(bad)
            check(f"rejects {bad!r}", False)
        except ValueError:
            check(f"rejects {bad!r}", True)


# --------------------------------------------------------------------------
# 2. Overlap / merge semantics (pure table logic)
# --------------------------------------------------------------------------
MAP_YAML = """
defaults:
  site.organization: "Example University"
networks:
  - range: 10.77.156.0/24
    name: "Engineering 1st floor"
    fields:
      site.building: "Engineering Building"
      site.floor: "1"
  - range: 10.77.156.1-10
    name: "Class room 1 (101)"
    fields:
      site.room: "101"
  - ranges: [10.77.157.11-15, 10.77.15.1-5]
    name: "Faculty office 108"
    fields:
      site.room: "108"
  - range: 10.77.200.1-10
    name: "first twin"
  - range: 10.77.200.1-10
    name: "second twin"
  - range: fdc0:beef::/64
    name: "v6 lab"
"""


def _lookup(tables, ip):
    import socket
    from bisect import bisect_right
    try:
        v = int.from_bytes(socket.inet_aton(ip), "big")
        starts, ends, pays = tables[4]
    except OSError:
        v = int.from_bytes(socket.inet_pton(socket.AF_INET6, ip), "big")
        starts, ends, pays = tables[6]
    i = bisect_right(starts, v) - 1
    if i >= 0 and v <= ends[i]:
        return pays[i]
    return None


def test_tables():
    entries, errors, warnings = load_map_text(MAP_YAML, label="t")
    check("test map loads clean", not errors and len(entries) == 6,
          f"errors={errors}")
    t = build_tables(entries)

    room = _lookup(t, "10.77.156.5")
    check("nested: room inherits floor fields",
          room and room["site"].get("building") == "Engineering Building"
          and room["site"].get("floor") == "1")
    check("nested: specific range wins geo.name",
          room["geo"]["name"] == "Class room 1 (101)")
    check("nested: room field added", room["site"].get("room") == "101")
    check("defaults applied", room["site"].get("organization") == "Example University")

    floor = _lookup(t, "10.77.156.99")
    check("outside the room: floor entry only",
          floor["geo"]["name"] == "Engineering 1st floor" and "room" not in floor["site"])

    check("boundary: last room IP", _lookup(t, "10.77.156.10")["site"].get("room") == "101")
    check("boundary: first IP past the room",
          _lookup(t, "10.77.156.11")["geo"]["name"] == "Engineering 1st floor")
    check("boundary: .0 of the /24 covered",
          _lookup(t, "10.77.156.0")["geo"]["name"] == "Engineering 1st floor")
    check("boundary: .255 of the /24 covered",
          _lookup(t, "10.77.156.255") is not None)
    check("just outside the /24", _lookup(t, "10.77.155.255") is None)

    a = _lookup(t, "10.77.157.12")
    b = _lookup(t, "10.77.15.3")
    check("multi-range entry: both ranges resolve",
          a and b and a["geo"]["name"] == b["geo"]["name"] == "Faculty office 108")
    check("multi-range shares one payload object", a is b)

    check("equal spans: later definition wins",
          _lookup(t, "10.77.200.5")["geo"]["name"] == "second twin")
    check("gap unmapped", _lookup(t, "10.77.201.1") is None)
    check("public ip unmapped", _lookup(t, "8.8.8.8") is None)
    check("v6 lookup", _lookup(t, "fdc0:beef::1234")["geo"]["name"] == "v6 lab")
    check("v6 outside", _lookup(t, "fdc0:dead::1") is None)


# --------------------------------------------------------------------------
# 3+4. Client + engine integration (temp config -> singleton -> engine)
# --------------------------------------------------------------------------
def test_engine_integration():
    tmp = tempfile.mkdtemp(prefix="socmap_")
    map_path = os.path.join(tmp, "map.yaml")
    cfg_path = os.path.join(tmp, "config.yaml")
    with open(map_path, "w") as f:
        f.write(MAP_YAML)
    with open(cfg_path, "w") as f:
        f.write("internal_map:\n  enabled: true\n  path: %r\n" % map_path)

    reset_map_singleton()
    reset_geo_singleton()
    m = InternalIPMap(config_path=cfg_path)
    check("client active", m._active and m.entries_count == 6)

    check("client enrich hit",
          m.enrich("10.77.156.5")["site"]["room"] == "101")
    check("client enrich miss", m.enrich("192.168.99.1") is None)
    check("client rejects garbage", m.enrich("not-an-ip") is None)
    check("client rejects non-string", m.enrich(["10.77.156.5"]) is None)
    check("client rejects empty", m.enrich("") is None)
    check("lru caches the payload object",
          m.enrich("10.77.156.5") is m.enrich("10.77.156.5"))

    # Engine: BOTH sides enriched, event fields land nested under each side
    event = run_line("src=10.77.156.5 dst=10.77.15.3")
    check("engine: source enriched",
          event["source"]["geo"]["name"] == "Class room 1 (101)"
          and event["source"]["site"]["room"] == "101")
    check("engine: destination enriched",
          event["destination"]["geo"]["name"] == "Faculty office 108")
    event = run_line("src=8.8.8.8 dst=1.1.1.1")
    check("engine: public IPs untouched",
          "site" not in event["source"] and "site" not in event["destination"])

    # Merge with a geoip result on the SAME endpoint: geo.name must join the
    # geoip fields in the event, while the geoip LRU's cached dict stays
    # pristine (shared object, never mutated).
    with open(map_path, "w") as f:
        f.write('networks:\n  - range: 8.8.8.0/24\n    name: "our anycast"\n'
                '    fields:\n      site.type: "external-service"\n')
    m.reload()
    check("reload picks up the edit",
          m.enrich("8.8.8.9")["geo"]["name"] == "our anycast")

    geo_cached = {"country_name": "Wonderland"}

    class _FakeGeo:
        def enrich(self, ip):
            return geo_cached

        def enrich_asn(self, ip):
            return None

    eng = UniversalEngine(RULE)
    eng.geoip = _FakeGeo()
    env = json.dumps({"meta": {"source_program": "t"}, "raw": "src=8.8.8.9 dst=9.9.9.9"})
    event = eng.process(LogInput(env))
    sgeo = event["source"]["geo"]
    check("geo merge: geoip fields kept", sgeo.get("country_name") == "Wonderland")
    check("geo merge: map name added", sgeo.get("name") == "our anycast")
    check("geo merge: geoip cache NOT mutated", "name" not in geo_cached)
    check("geo merge: map payload NOT mutated",
          "country_name" not in m.enrich("8.8.8.9")["geo"])

    # Broken edit: previous tables survive (fail-safe like the rule registry)
    with open(map_path, "w") as f:
        f.write("networks: [")
    m.reload()
    check("broken edit keeps serving", m.enrich("8.8.8.9") is not None)

    # Signature changes drive the watcher
    sig1 = m._signature()
    with open(map_path, "w") as f:
        f.write("networks: []\n")
    check("file signature changes on edit", m._signature() != sig1)
    m.reload()
    check("emptied map goes inactive", not m._active and m.enrich("8.8.8.9") is None)

    # Unconfigured: enrich() is a no-op boolean and events stay untouched
    ncfg = os.path.join(tmp, "noblock.yaml")
    with open(ncfg, "w") as f:
        f.write("kafka: {}\n")
    reset_map_singleton()
    m2 = InternalIPMap(config_path=ncfg)
    check("unconfigured: inactive", not m2._enabled and not m2._active)
    check("unconfigured: enrich None", m2.enrich("10.77.156.5") is None)
    event = run_line("src=10.77.156.5 dst=10.77.15.3")
    check("unconfigured: event untouched",
          "site" not in event["source"] and "geo" not in event["source"])

    # Disabled explicitly
    dcfg = os.path.join(tmp, "disabled.yaml")
    with open(dcfg, "w") as f:
        f.write("internal_map:\n  enabled: false\n  path: %r\n" % map_path)
    reset_map_singleton()
    m3 = InternalIPMap(config_path=dcfg)
    check("disabled: inactive", not m3._enabled and m3.enrich("10.77.156.5") is None)

    reset_map_singleton()  # leave no test state for other suites


def main():
    test_parsing()
    test_tables()
    test_engine_integration()

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
