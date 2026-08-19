"""Internal IP map - enrichment for the organization's OWN address space.

GeoIP/ASN answer "where in the world is this PUBLIC IP?"; for internal
addresses those databases have nothing - while operators know exactly which
range is which ("10.10.4.0/24 is teaching lab 1", "10.10.2.11-15 is faculty
office 108"). This module turns that knowledge, written as plain YAML, into
per-event enrichment the same way utils/geoip.py turns the MaxMind databases
into source.geo.* - config.yaml `internal_map:` points at one YAML file
(default: database/internal_ips.yaml, living next to the GeoIP .mmdb files;
a directory of files - e.g. one per building - works too) and every source/
destination IP inside a declared range gets that range's fields merged in.

Map file format (all range syntaxes may be mixed freely)::

    defaults:                       # optional: fields added to EVERY entry
      site.organization: Example U  # of this file (entry fields win)
    networks:
      - range: 10.10.4.0/24                # CIDR
        name: Teaching lab 1               # sugar for geo.name (real ECS)
      - range: 10.10.1.1-10                # short range (last octet)
        name: Class room 1
        fields:                            # any extra fields, dotted paths,
          site.building: Engineering Bldg  #   nested under source./destination.
          site.room: "101"
      - ranges: [10.10.2.11-15, 10.10.9.1-5]   # several ranges, one entry
        name: Faculty office 108

Overlaps are legal and useful: declare a /24 with the building's fields and
narrower room ranges inside it - a matching IP gets the building fields PLUS
the room fields, the more specific range overriding on conflicts (equal spans:
the later definition wins). All of that is resolved ONCE at load time into
disjoint (start, end, payload) segments, so a lookup is a single binary search
however deeply entries nest.

Performance contract (this runs on the 1M+ EPS hot path):
  * lookup = LRU dict hit for repeated IPs (SOC traffic repeats constantly);
    a miss is inet_aton + one bisect over precompiled int arrays (~1us).
  * `internal_map:` absent/disabled or no ranges loaded = one boolean check.
  * loading/merging/validation cost lives entirely at (re)load time.

Like the rule registry, a daemon thread watches the map file(s) (mtime/size,
10s) and hot-reloads edits; a broken edit keeps the previous working tables.
Pure stdlib + PyYAML - no new dependencies, nothing optional to install.
"""

import os
import sys
import time
import socket
import logging
import functools
import ipaddress
from bisect import bisect_right
from threading import Thread

import yaml

logger = logging.getLogger("soc-engine")

# Distinct IPs memoized per process (same reasoning as GEOIP_CACHE_SIZE: the
# same internal hosts and scanners recur constantly, so repeats become a dict
# hit). Override with internal_map.cache_size in config.yaml.
INTERNAL_MAP_CACHE_SIZE = 50000

# Where the map lives when internal_map.path is not set: next to the GeoIP
# .mmdb files - all the "describe my world" databases in one folder.
DEFAULT_MAP_PATH = "database/internal_ips.yaml"

_WATCH_INTERVAL_SEC = 10  # same cadence as the rule registry watcher

# Keys an entry may carry; anything else is a typo we refuse loudly instead of
# silently ignoring (a misspelled `fields:` would otherwise just drop data).
_ENTRY_KEYS = {"range", "ranges", "name", "fields"}
_FILE_KEYS = {"defaults", "networks"}
_SCALAR_TYPES = (str, int, float, bool)


# --------------------------------------------------------------------------- #
# Parsing (load time only - clarity over speed here)
# --------------------------------------------------------------------------- #
def _ip_to_int(text):
    ip = ipaddress.ip_address(str(text).strip())
    return int(ip), ip.version


def _parse_one_range(item):
    """One range literal -> (lo_int, hi_int, ip_version). Accepts a CIDR
    (10.0.0.0/24), a full range (10.0.0.1-10.0.0.99), a short IPv4 range where
    the right side is just the final octet (10.0.0.1-99), or a single IP.
    Raises ValueError with a message an operator can act on."""
    s = str(item).strip()
    if not s:
        raise ValueError("empty range")
    if "/" in s:
        net = ipaddress.ip_network(s, strict=False)
        return int(net.network_address), int(net.broadcast_address), net.version
    # a '-' separates a range; IPv6 literals themselves never contain '-'
    if "-" in s:
        left, right = (p.strip() for p in s.split("-", 1))
        lo, ver = _ip_to_int(left)
        if right.isdigit() and "." not in right and ":" not in right:
            if ver != 4:
                raise ValueError(
                    f"'{s}': short ranges (a.b.c.d-N) are IPv4 only - "
                    "write the full end address for IPv6")
            n = int(right)
            if n > 255:
                raise ValueError(f"'{s}': last octet {n} is out of range (0-255)")
            hi = (lo & ~0xFF) | n
        else:
            hi, ver2 = _ip_to_int(right)
            if ver2 != ver:
                raise ValueError(f"'{s}': mixes IPv4 and IPv6")
        if hi < lo:
            raise ValueError(f"'{s}': range end is below its start")
        return lo, hi, ver
    lo, ver = _ip_to_int(s)
    return lo, lo, ver


def parse_range_spec(spec):
    """A `range:`/`ranges:` value -> list of (lo, hi, version). The value may
    be one literal, a comma-separated string, or a YAML list of either."""
    if isinstance(spec, str):
        items = [p.strip() for p in spec.split(",") if p.strip()]
    elif isinstance(spec, (list, tuple)):
        items = []
        for v in spec:
            if isinstance(v, str) and "," in v:
                items.extend(p.strip() for p in v.split(",") if p.strip())
            else:
                items.append(v)
    else:
        items = [spec]
    if not items:
        raise ValueError("empty range list")
    return [_parse_one_range(i) for i in items]


def _valid_field_value(v):
    if isinstance(v, _SCALAR_TYPES):
        return True
    return (isinstance(v, list) and v
            and all(isinstance(i, _SCALAR_TYPES) for i in v))


def _read_fields_block(block, where, errors):
    """Validate a {dotted.field: value} mapping -> flat dict (bad pairs are
    reported and skipped; the rest of the entry still loads)."""
    flat = {}
    if not isinstance(block, dict):
        errors.append(f"{where}: must be a mapping of field: value")
        return flat
    for k, v in block.items():
        key = str(k).strip()
        if not key or key.startswith(".") or key.endswith(".") or " " in key:
            errors.append(f"{where}: bad field name '{k}'")
            continue
        if not _valid_field_value(v):
            errors.append(f"{where}: field '{key}' must be a scalar "
                          "(or list of scalars)")
            continue
        flat[key] = v
    return flat


def load_map_text(text, label="<inline>"):
    """Parse ONE map file's text. Returns (entries, errors, warnings) where
    each entry is {'ranges': [(lo, hi, ver), ...], 'fields': {flat dotted},
    'where': human location}. Never raises on content problems - they are all
    collected so validators/the UI can show every issue at once."""
    entries, errors, warnings = [], [], []
    try:
        data = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001 - any YAML error is a user error
        errors.append(f"{label}: YAML syntax error: {e}")
        return entries, errors, warnings
    if data is None:
        warnings.append(f"{label}: file is empty")
        return entries, errors, warnings
    if not isinstance(data, dict):
        errors.append(f"{label}: must be a YAML mapping with a networks: list")
        return entries, errors, warnings

    for k in data:
        if k not in _FILE_KEYS:
            errors.append(f"{label}: unknown top-level key '{k}' "
                          "(expected: defaults, networks)")

    defaults = {}
    if data.get("defaults") is not None:
        defaults = _read_fields_block(data["defaults"], f"{label} defaults", errors)

    networks = data.get("networks")
    if networks is None:
        warnings.append(f"{label}: no networks: list - nothing to map")
        return entries, errors, warnings
    if not isinstance(networks, list):
        errors.append(f"{label}: networks must be a list of entries")
        return entries, errors, warnings

    for idx, entry in enumerate(networks, start=1):
        where = f"{label} entry #{idx}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        unknown = [str(k) for k in entry if k not in _ENTRY_KEYS]
        if unknown:
            errors.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))} "
                          "(allowed: range, ranges, name, fields)")
            continue
        spec = entry.get("range", entry.get("ranges"))
        if "range" in entry and "ranges" in entry:
            errors.append(f"{where}: use either range: or ranges:, not both")
            continue
        if spec is None:
            errors.append(f"{where}: missing range: (or ranges:)")
            continue
        try:
            ranges = parse_range_spec(spec)
        except ValueError as e:
            errors.append(f"{where}: {e}")
            continue

        fields = dict(defaults)
        name = entry.get("name")
        if name is not None:
            if isinstance(name, _SCALAR_TYPES):
                fields["geo.name"] = name  # ECS's own slot for named locations
            else:
                errors.append(f"{where}: name must be plain text")
        if entry.get("fields") is not None:
            fields.update(_read_fields_block(entry["fields"],
                                             f"{where} fields", errors))
        if not fields:
            warnings.append(f"{where}: has no name/fields (and no defaults) - "
                            "matches would add nothing")
            continue
        entries.append({"ranges": ranges, "fields": fields, "where": where})

    return entries, errors, warnings


def resolve_map_files(path):
    """internal_map.path (already absolute) -> ordered list of YAML files.
    A directory means every *.yaml/*.yml inside it (sorted, like rules/)."""
    if not path:
        return []
    if os.path.isdir(path):
        try:
            return [os.path.join(path, f) for f in sorted(os.listdir(path))
                    if f.endswith((".yaml", ".yml"))]
        except OSError:
            return []
    return [path] if os.path.isfile(path) else []


def load_map_files(files):
    """Parse every file, concatenating entries in file order. Returns
    (entries, errors, warnings) - same contract as load_map_text."""
    entries, errors, warnings = [], [], []
    for path in files:
        label = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            errors.append(f"{label}: cannot read: {e}")
            continue
        e, er, wa = load_map_text(text, label=label)
        entries.extend(e)
        errors.extend(er)
        warnings.extend(wa)
    return entries, errors, warnings


def parse_map_structure(text):
    """YAML text -> the editable structure behind the Web UI's visual editor:
    {'defaults': {field: value}, 'networks': [{'ranges': [str, ...],
    'name': str|None, 'fields': {field: value}}]} - every range kept as its
    ORIGINAL string so edits round-trip. Returns (structure, None), or
    (None, reason) when the content cannot be represented in the editor
    (syntax error, unknown keys, wrong shapes) - the raw-YAML mode handles
    those. Range VALIDITY is not judged here; the loader/validator do that."""
    try:
        data = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        return None, f"YAML syntax error: {e}"
    if data is None:
        return {"defaults": {}, "networks": []}, None
    if not isinstance(data, dict):
        return None, "file must be a YAML mapping"
    unknown = [str(k) for k in data if k not in _FILE_KEYS]
    if unknown:
        return None, f"unknown top-level key(s): {', '.join(sorted(unknown))}"
    defaults = data.get("defaults")
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        return None, "defaults must be a mapping"
    networks = data.get("networks")
    if networks is None:
        networks = []
    if not isinstance(networks, list):
        return None, "networks must be a list"
    out = []
    for i, e in enumerate(networks, start=1):
        if not isinstance(e, dict):
            return None, f"entry #{i} is not a mapping"
        unknown = [str(k) for k in e if k not in _ENTRY_KEYS]
        if unknown:
            return None, (f"entry #{i} has unknown key(s): "
                          f"{', '.join(sorted(unknown))}")
        spec = e.get("range", e.get("ranges"))
        if isinstance(spec, str):
            ranges = [p.strip() for p in spec.split(",") if p.strip()]
        elif isinstance(spec, (list, tuple)):
            ranges = [str(v).strip() for v in spec if str(v).strip()]
        elif spec is None:
            ranges = []
        else:
            ranges = [str(spec).strip()]
        fields = e.get("fields")
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            return None, f"entry #{i}: fields must be a mapping"
        name = e.get("name")
        out.append({
            "ranges": ranges,
            "name": None if name is None else str(name),
            "fields": {str(k): v for k, v in fields.items()},
        })
    return {"defaults": {str(k): v for k, v in defaults.items()},
            "networks": out}, None


def iter_map_fields(entries):
    """Yield (dotted_field, where) for every field of every entry - the hook
    the validators use to run the same ECS gate as rule fields (each field
    lands under source./destination., so 'geo.name' checks as
    'source.geo.name')."""
    for e in entries:
        for field in e["fields"]:
            yield field, e["where"]


# --------------------------------------------------------------------------- #
# Table build: overlapping entries -> disjoint, pre-merged segments
# --------------------------------------------------------------------------- #
def _nest(flat):
    """{'geo.name': x, 'site.room': y} -> {'geo': {'name': x}, 'site': ...}.
    Built once per distinct overlap combination at load time; the hot path
    only ever hands out these prebuilt dicts."""
    out = {}
    for path, value in flat.items():
        keys = path.split(".")
        d = out
        for k in keys[:-1]:
            nxt = d.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                d[k] = nxt
            d = nxt
        d[keys[-1]] = value
    return out


def _sweep(recs):
    """Boundary sweep over [(lo, hi, seq, flat_fields), ...] -> parallel
    (starts, ends, payloads) arrays of DISJOINT segments covering exactly the
    declared ranges. Each segment's payload is the merge of every range
    covering it: broader spans first, then declaration order - so the most
    specific range wins a conflict, ties going to the later definition."""
    if not recs:
        return [], [], []
    delta = {}
    for i, (lo, hi, _seq, _fields) in enumerate(recs):
        delta.setdefault(lo, []).append((0, i))       # 0 = range opens here
        delta.setdefault(hi + 1, []).append((1, i))   # 1 = range closed before
    bounds = sorted(delta)
    active = set()
    combo_cache = {}    # overlap combination -> payload
    content_cache = {}  # merged content -> payload (different combinations
    #                     often merge to the same fields, e.g. the two ranges
    #                     of one entry - share a single dict for all of them)
    starts, ends, payloads = [], [], []
    for bi, b in enumerate(bounds):
        for op, i in delta[b]:
            (active.discard if op else active.add)(i)
        if not active:  # gap (the final bound always lands here)
            continue
        seg_start, seg_end = b, bounds[bi + 1] - 1
        key = tuple(sorted(active))
        payload = combo_cache.get(key)
        if payload is None:
            order = sorted(key, key=lambda i: (-(recs[i][1] - recs[i][0]),
                                               recs[i][2]))
            flat = {}
            for i in order:
                flat.update(recs[i][3])
            ckey = tuple(sorted(
                (k, tuple(v) if isinstance(v, list) else v)
                for k, v in flat.items()))
            payload = content_cache.get(ckey)
            if payload is None:
                payload = _nest(flat)
                content_cache[ckey] = payload
            combo_cache[key] = payload
        if payloads and payloads[-1] is payload and ends[-1] == seg_start - 1:
            ends[-1] = seg_end  # coalesce adjacent identical segments
        else:
            starts.append(seg_start)
            ends.append(seg_end)
            payloads.append(payload)
    return starts, ends, payloads


def build_tables(entries):
    """Entries -> {4: (starts, ends, payloads), 6: (...)} per IP version."""
    recs4, recs6 = [], []
    for seq, e in enumerate(entries):
        for lo, hi, ver in e["ranges"]:
            (recs4 if ver == 4 else recs6).append((lo, hi, seq, e["fields"]))
    return {4: _sweep(recs4), 6: _sweep(recs6)}


# --------------------------------------------------------------------------- #
# The per-process client (same singleton shape as utils/geoip.GeoIPClient)
# --------------------------------------------------------------------------- #
class InternalIPMap:
    _instance = None

    def __new__(cls, config_path="config.yaml"):
        if cls._instance is None:
            inst = super(InternalIPMap, cls).__new__(cls)
            inst._initialize(config_path)
            cls._instance = inst
        return cls._instance

    def _initialize(self, config_file):
        self._enabled = False   # internal_map: configured and enabled
        self._active = False    # ... AND at least one range actually loaded
        self._path = None
        self._cache_size = INTERNAL_MAP_CACHE_SIZE
        self._t4 = ([], [], [])
        self._t6 = ([], [], [])
        self._sig = None
        self.entries_count = 0
        self.segments_count = 0
        self.load_errors = []
        # Fail-safe reload state: last successfully parsed entries per file,
        # so a broken edit keeps the previous working version loaded (same
        # contract as the rule registry).
        self._last_good = {}
        # Always create the memoized lookup so enrich() is safe to call even
        # when the feature is off (fork-safe: one cache per worker process).
        self._lookup = functools.lru_cache(maxsize=self._cache_size)(
            self._lookup_uncached
        )
        try:
            # Same path split as utils/geoip.py: frozen (PyInstaller) builds
            # keep the editable config.yaml next to the executable.
            if getattr(sys, "frozen", False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))
            config_path = (config_file if os.path.isabs(config_file)
                           else os.path.join(base_dir, config_file))
            conf = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    conf = yaml.safe_load(f) or {}
            block = conf.get("internal_map") or {}
            if not isinstance(block, dict) or not block:
                return  # not configured: feature silently off, zero cost
            if not block.get("enabled", True):
                print("Internal IP map disabled in config.yaml "
                      "(internal_map.enabled: false)")
                return
            rel = block.get("path") or DEFAULT_MAP_PATH
            self._path = rel if os.path.isabs(rel) else os.path.join(base_dir, rel)
            try:
                self._cache_size = max(
                    1024, int(block.get("cache_size", INTERNAL_MAP_CACHE_SIZE)))
            except (TypeError, ValueError):
                pass
            self._enabled = True
            self.reload(announce=True)
            # Hot reload, registry-style: edits (from the Web UI or a plain
            # editor) apply within ~10s on every worker, no restart. The
            # watcher also notices a map file APPEARING after startup.
            if not getattr(self, "_watcher_started", False):
                self._watcher_started = True
                Thread(target=self._watch_loop, daemon=True).start()
        except Exception as e:  # noqa: BLE001 - enrichment must never crash startup
            print(f"Internal IP map initialization error: {e}")
            self._enabled = False
            self._active = False

    @classmethod
    def refresh(cls):
        """Re-read config + map files NOW (the Web UI calls this after a save
        so its own test tools reflect the edit immediately; workers pick the
        same edit up via their watcher)."""
        inst = cls._instance
        if inst is None:
            cls()
        elif not inst._enabled:
            inst._initialize("config.yaml")
        else:
            inst.reload()

    # -- loading ------------------------------------------------------------ #
    def reload(self, announce=False):
        """(Re)load the map file(s) and swap the lookup tables. Fail-safe like
        the rule registry: a file whose new content is completely unloadable
        (unreadable, YAML syntax error) keeps its PREVIOUS working entries;
        entry-level problems are reported and skipped, the good entries still
        load. Deliberately emptying a file (networks: []) does apply."""
        if not self._enabled:
            return
        try:
            files = resolve_map_files(self._path)
            entries, errors, warnings = [], [], []
            new_good = {}
            for path in files:
                label = os.path.basename(path)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        text = f.read()
                    fe, fer, fwa = load_map_text(text, label=label)
                except OSError as e:
                    fe, fer, fwa = [], [f"{label}: cannot read: {e}"], []
                if fer and not fe and self._last_good.get(path):
                    fe = self._last_good[path]
                    fer = fer + [f"{label}: keeping the previous working "
                                 "version (fix the file and save again)"]
                new_good[path] = fe
                entries.extend(fe)
                errors.extend(fer)
                warnings.extend(fwa)
            tables = build_tables(entries)
            self._last_good = new_good
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Internal IP map reload failed - keeping previous map: {e}")
            return
        self._t4 = tables[4]
        self._t6 = tables[6]
        self.entries_count = len(entries)
        self.segments_count = len(tables[4][0]) + len(tables[6][0])
        self.load_errors = errors
        self._active = self.segments_count > 0
        # Fresh cache bound to the new tables; in-flight lookups just finish
        # against whichever cache they grabbed.
        self._lookup = functools.lru_cache(maxsize=self._cache_size)(
            self._lookup_uncached
        )
        self._sig = self._signature()
        for msg in errors:
            logger.error(f"Internal IP map: {msg}")
        for msg in warnings:
            logger.warning(f"Internal IP map: {msg}")
        if files:
            summary = (f"Internal IP map loaded: {self.entries_count} "
                       f"entries -> {self.segments_count} range segments "
                       f"({len(files)} file(s), {self._path})")
        else:
            summary = (f"Internal IP map: no map file at {self._path} "
                       "(enrichment idle until one appears)")
        if announce:
            print(summary)
        else:
            logger.info(summary)

    def status(self):
        """Snapshot for the Web UI / diagnostics."""
        files = resolve_map_files(self._path) if self._path else []
        return {
            "configured": self._path is not None,
            "enabled": self._enabled,
            "active": self._active,
            "path": self._path,
            "files": files,
            "entries": self.entries_count,
            "segments": self.segments_count,
            "errors": list(self.load_errors),
        }

    # -- hot reload watcher --------------------------------------------------#
    def _signature(self):
        sig = []
        for f in resolve_map_files(self._path):
            try:
                st = os.stat(f)
                sig.append((f, st.st_mtime, st.st_size))
            except OSError:
                continue
        return tuple(sig)

    def _watch_loop(self):
        while True:
            time.sleep(_WATCH_INTERVAL_SEC)
            try:
                sig = self._signature()
                if sig != self._sig:
                    self._sig = sig
                    self.reload()
            except Exception:
                pass

    # -- the hot path -------------------------------------------------------- #
    def _lookup_uncached(self, ip_str):
        # inet_aton/inet_pton are the cheapest validating parsers Python has;
        # anything unparseable (hostnames, CIDR strings, garbage) -> None.
        try:
            val = int.from_bytes(socket.inet_aton(ip_str), "big")
            starts, ends, payloads = self._t4
        except OSError:
            try:
                val = int.from_bytes(
                    socket.inet_pton(socket.AF_INET6, ip_str), "big")
                starts, ends, payloads = self._t6
            except (OSError, ValueError):
                return None
        i = bisect_right(starts, val) - 1
        if i >= 0 and val <= ends[i]:
            return payloads[i]
        return None

    def enrich(self, ip_str):
        """Fields for this IP (a shared prebuilt dict - treat as read-only,
        exactly like the geoip LRU results) or None. One boolean when the
        feature is off; a dict hit for repeated IPs when it is on."""
        if not self._active or not ip_str or not isinstance(ip_str, str):
            return None
        return self._lookup(ip_str)
