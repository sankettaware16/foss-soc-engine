import re
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from utils.geoip import GeoIPClient
from utils import fastjson
from core.timeparse import parse_timestamp

# Initialize Redis connection.
# redis is only needed by the 'stateful' strategy. We import it lazily so the
# rest of the engine (and the web UI / local testing tools) can run on machines
# where the redis package is not installed. When absent, stateful correlation
# is disabled and the _process_stateful guards fall back gracefully.
# The default client points at localhost:6379; main.py / preflight / the Web UI
# call configure_redis() with config.yaml's `redis:` block to override it.
try:
    import redis
    r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
except Exception:
    redis = None
    r = None

logger = logging.getLogger("soc-engine")


def configure_redis(cfg):
    """Point the stateful strategy at the Redis described by config.yaml's
    optional `redis:` block (host/port/db/password). Constructing the client
    is lazy (no connection until first use), so this is cheap. No-op when the
    redis package is absent or the block is missing/malformed."""
    global r
    if redis is None or not isinstance(cfg, dict) or not cfg:
        return
    try:
        r = redis.Redis(
            host=str(cfg.get("host", "localhost")),
            port=int(cfg.get("port", 6379)),
            db=int(cfg.get("db", 0)),
            password=cfg.get("password") or None,
            decode_responses=True,
        )
    except Exception as e:
        logger.error(f"Invalid redis: config, keeping previous client: {e}")

# The ECS release core/ecs_schema.py's field set is curated from. Stamped on
# every event as ecs.version so Kibana/consumers know the schema contract.
ECS_VERSION = "8.11.0"

# Ingestion timestamp cache. datetime.now().isoformat() is surprisingly costly
# when called per event; this value is the INGEST time (event.ingested, and the
# provisional @timestamp until _apply_timestamp replaces it with the event's
# real time parsed from the log line), so 1-second resolution is plenty.
# Cached per process.
_ts_cache = {"t": 0.0, "v": ""}


def _now_iso():
    now = time.time()
    if now - _ts_cache["t"] >= 1.0:
        _ts_cache["v"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        _ts_cache["t"] = now
    return _ts_cache["v"]

def _compile_prematch(spec):
    """Normalize a rule/pattern `prematch:` declaration.

    A prematch is a plain substring (or a list = any-of) that MUST appear in
    the raw line before the pattern's regex is even attempted. `"x" in line`
    costs a fraction of a failed regex, so with many patterns almost all of
    them are skipped by this cheap glance — that's what keeps multi_match
    lightweight when a rule grows to hundreds of patterns.
    Returns a tuple of substrings, or None (= no gate, always try the regex).
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        return (spec,) if spec else None
    if isinstance(spec, (list, tuple)):
        vals = tuple(str(s) for s in spec if s)
        return vals or None
    return None


def _prematch_hit(pm, raw):
    if pm is None:
        return True
    for s in pm:
        if s in raw:
            return True
    return False


# Token syntax for the optional rule-level `vars:` block: %{name}
_VAR_TOKEN = re.compile(r'%\{([A-Za-z_][A-Za-z0-9_]*)\}')


def substitute_vars(rule_config):
    """Resolve the optional top-level `vars:` block of a rule.

    A rule may declare site-tunable values once at the top instead of
    hardcoding them inside regexes::

        vars:
          internal_domains: ["example.com"]   # EDIT: your mail domain(s)
        patterns:
          - regex: 'from=<(?P<sender>[^@]+@(?P<s_domain>%{internal_domains}))>'

    List values are re.escape()d and joined into a safe alternation
    ``(?:a\\.com|b\\.org)`` so operators write plain domains, not regex.
    String values are inserted verbatim (for authors who WANT a regex
    fragment). Rules without a `vars:` block are returned untouched —
    zero behavior change. A ``%{token}`` that has no matching var is an
    error (silent non-substitution would quietly break classification).

    Returns a NEW config dict; the caller's dict is never mutated.
    """
    vars_block = rule_config.get('vars') if isinstance(rule_config, dict) else None
    if not isinstance(vars_block, dict) or not vars_block:
        return rule_config

    resolved = {}
    for name, value in vars_block.items():
        if isinstance(value, (list, tuple)):
            parts = [re.escape(str(v)) for v in value if str(v)]
            if not parts:
                raise ValueError(f"vars.{name}: list is empty")
            resolved[str(name)] = "(?:" + "|".join(parts) + ")"
        else:
            resolved[str(name)] = str(value)

    def _sub_str(s):
        def _repl(m):
            token = m.group(1)
            if token not in resolved:
                raise ValueError(
                    f"undefined vars token %{{{token}}} (declared vars: "
                    f"{', '.join(sorted(resolved))})")
            return resolved[token]
        return _VAR_TOKEN.sub(_repl, s) if "%{" in s else s

    def _walk(node):
        if isinstance(node, str):
            return _sub_str(node)
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    out = {k: (_walk(v) if k != 'vars' else v) for k, v in rule_config.items()}
    return out


class UniversalEngine:
    def __init__(self, rule_config, include_original=True):
        self.geoip = GeoIPClient()
        self.disabled = False
        self.last_redis_error = None
        # True iff the LAST process() call stored stateful transaction state
        # (line consumed into an open transaction). This is the authoritative
        # "buffered" signal — callers must NOT infer it from id_regex, which
        # also matches lines the rule then rejects (audit A1 #5).
        self.last_buffered = False
        # Resolve the optional `vars:` block (site-tunable regex values)
        # before anything compiles. A bad token disables the rule — same
        # fail-safe behavior as an invalid regex, never a crash.
        try:
            rule_config = substitute_vars(rule_config)
        except ValueError as e:
            logger.error(
                f"Invalid vars in rule "
                f"'{rule_config.get('pattern_name', 'unknown')}': {e}")
            self.disabled = True
        self.config = rule_config
        self.strategy = rule_config.get('strategy', 'stateless')
        # config.yaml output.include_original (default true): whether events
        # carry the full raw log line in event.original. Turning it off
        # roughly halves Elasticsearch storage; parsing is unaffected.
        self.include_original = bool(include_original)
        # Rule-level `timestamp:` declaration (where the event's real time
        # lives in the log and how to parse it). Optional; without it the
        # event keeps ingest time and is tagged 'ingest_fallback'.
        self.ts_spec = self._compile_ts_spec(rule_config.get('timestamp'))
        # Rule-level prematch: cheap substring gate for the WHOLE rule.
        self.prematch = _compile_prematch(rule_config.get('prematch'))

        if self.strategy == 'stateless':
            try:
                self.main_regex = re.compile(self.config['regex'])
            except re.error as e:
                logger.error(f"Invalid regex in rule '{self.config.get('pattern_name', 'unknown')}': {e}")
                self.main_regex = None
                self.disabled = True

        elif self.strategy in ['multi_match', 'stateful']:
            self.patterns = [] if self.strategy == 'multi_match' else []
            self.sub_patterns = [] if self.strategy == 'stateful' else []

            source_patterns = self.config.get('patterns', [])
            target_list = self.patterns if self.strategy == 'multi_match' else self.sub_patterns

            for p in source_patterns:
                try:
                    compiled = re.compile(p['regex'])
                except re.error as e:
                    logger.error(f"Invalid regex in rule '{self.config.get('pattern_name', 'unknown')}': {e}")
                    continue

                target_list.append({
                    "name": p.get('name'),
                    "regex": compiled,
                    "mapping": p.get('mapping', {}),
                    "static": p.get('static', {}),
                    # Cheap substring gate tried before the regex (optional)
                    "prematch": _compile_prematch(p.get('prematch')),
                    # Per-pattern timestamp override (falls back to rule-level)
                    "timestamp": self._compile_ts_spec(p.get('timestamp'))
                })

            if self.strategy == 'stateful':
                try:
                    self.id_regex = re.compile(self.config['id_regex'])
                except re.error as e:
                    logger.error(f"Invalid id_regex in rule '{self.config.get('pattern_name', 'unknown')}': {e}")
                    self.id_regex = None
                # How long a transaction may stay open in Redis. Rule-level
                # knob: normal postfix mail completes in seconds; raise this
                # for sources whose transactions legitimately run long.
                try:
                    self.state_ttl = max(30, int(self.config.get('state_ttl_sec', 300)))
                except (TypeError, ValueError):
                    self.state_ttl = 300
                # Keys are namespaced per rule so two stateful rules can never
                # collide on the same transaction id, and so the expiry sweep
                # only touches its own rule's transactions.
                self.state_prefix = f"state:{self.config.get('pattern_name', 'unknown')}:"

    def process(self, log_input):
        self.last_buffered = False
        if self.disabled:
            return None
        if not _prematch_hit(self.prematch, log_input.raw):
            return None
        if self.strategy == 'stateless':
            return self._process_stateless(log_input)
        elif self.strategy == 'multi_match':
            return self._process_multi_match(log_input)
        elif self.strategy == 'stateful':
            return self._process_stateful(log_input)
        elif self.strategy == 'json_map':
            return self._process_json_map(log_input)
        elif self.strategy == 'xml_xpath':
            return self._process_xml(log_input)

    def _process_xml(self, log_input):
        try:
            # Parse the raw XML string
            root = ET.fromstring(log_input.raw)
        except ET.ParseError:
            return None

        events = []
        items_path = self.config.get('items_xpath', '.')
        mapping = self.config.get('mapping', {})
        static = self.config.get('static', {})

        for item in root.findall(items_path):
            event = self._init_event(log_input)
            
            for source_path, target_field in mapping.items():
                val = None
                # Support extracting attributes (e.g., nvt/@oid)
                if '/@' in source_path:
                    tag, attr = source_path.split('/@')
                    child = item.find(tag)
                    if child is not None:
                        val = child.get(attr)
                else:
                    child = item.find(source_path)
                    if child is not None and child.text:
                        val = child.text.strip()

                if val:
                    target, dtype = target_field.split('|') if '|' in target_field else (target_field, 'str')
                    if dtype == 'float':
                        try: val = float(val)
                        except ValueError: pass
                    elif dtype == 'int':
                        try: val = int(float(val))
                        except ValueError: pass

                    self._set_nested(event, target, val)

            self._apply_static(event, static)
            self._apply_timestamp(event, self.ts_spec, item=item,
                                  raw=log_input.raw)
            enriched = self._enrich_event(event)
            if enriched:
                events.append(enriched)

        return events

    def _process_json_map(self, log_input):
        try:
            data = fastjson.loads(log_input.raw)
        except (ValueError, TypeError):
            return None

        event = self._init_event(log_input)
        mapping = self.config.get('mapping', {})

        for source_path, target_field in mapping.items():
            value = self._get_path(data, source_path)
            if value is not None:
                if isinstance(value, list) and len(value) > 0:
                    value = [v for v in value if v is not None]
                    if not value: continue
                # |int / |float coercion, same contract as _map_fields —
                # JSON sources often carry numbers as strings ("2") and a
                # string in an ES numeric/date field is mapping drift.
                target, dtype = (target_field.split('|')
                                 if '|' in target_field
                                 else (target_field, 'str'))
                if dtype in ('int', 'float'):
                    conv = int if dtype == 'int' else float
                    try:
                        if isinstance(value, list):
                            value = [conv(v) for v in value]
                        else:
                            value = conv(value)
                    except (ValueError, TypeError):
                        pass  # non-numeric stays as-is, the event survives
                self._set_nested(event, target, value)

        self._apply_static(event, self.config.get('static', {}))
        self._apply_timestamp(event, self.ts_spec, data=data,
                              raw=log_input.raw)
        return self._enrich_event(event)

    def _get_path(self, data, path):
        keys = path.split('.')
        current = data
        for i, k in enumerate(keys):
            if k == '*':
                if isinstance(current, list):
                    remaining_path = '.'.join(keys[i+1:])
                    return [self._get_path(item, remaining_path) for item in current]
                else:
                    return None
            try:
                if isinstance(current, list):
                    k = int(k)
                current = current[k]
            except (KeyError, IndexError, ValueError, TypeError):
                return None
        return current

    def _enrich_event(self, event):
        if not event: return None
        source_ip = event.get('source', {}).get('ip')
        if source_ip:
            geo = self.geoip.enrich(source_ip)
            if geo:
                if 'source' not in event:
                    event['source'] = {}
                event['source']['geo'] = geo
            # ASN enrichment (who owns the IP): source.as.number /
            # source.as.organization.name. Skipped when the ASN mmdb is absent.
            asn = self.geoip.enrich_asn(source_ip)
            if asn:
                if 'source' not in event:
                    event['source'] = {}
                event['source']['as'] = asn
        return event

    def _process_stateless(self, log_input):
        match = self.main_regex.search(log_input.raw)
        if not match: return None

        event = self._init_event(log_input)
        self._map_fields(event, match.groupdict(), self.config.get('mapping', {}))
        self._apply_static(event, self.config.get('static', {}))
        self._apply_timestamp(event, self.ts_spec,
                              groups=match.groupdict(), raw=log_input.raw)
        return self._enrich_event(event)

    def _process_multi_match(self, log_input):
        raw = log_input.raw
        for p in self.patterns:
            if not _prematch_hit(p['prematch'], raw):
                continue
            match = p['regex'].search(raw)
            if match:
                event = self._init_event(log_input)
                self._map_fields(event, match.groupdict(), p['mapping'])
                self._apply_static(event, p.get('static', {}))
                self._apply_timestamp(event, p.get('timestamp') or self.ts_spec,
                                      groups=match.groupdict(),
                                      raw=log_input.raw)
                return self._enrich_event(event)
        return None

    def _process_stateful(self, log_input):
        self.last_redis_error = None

        # Fallback-only when id_regex is missing or invalid
        if not self.id_regex:
            for p in self.sub_patterns:
                if not _prematch_hit(p['prematch'], log_input.raw):
                    continue
                m = p['regex'].search(log_input.raw)
                if m:
                    event = self._init_event(log_input)
                    self._map_fields(event, m.groupdict(), p['mapping'])
                    self._apply_static(event, p.get('static', {}))
                    self._apply_timestamp(event,
                                          p.get('timestamp') or self.ts_spec,
                                          groups=m.groupdict(),
                                          raw=log_input.raw)
                    return self._enrich_event(event)

            return None

        match = self.id_regex.search(log_input.raw)

        # Stateless fallback for non-ID logs
        if not match:
            for p in self.sub_patterns:
                if not _prematch_hit(p['prematch'], log_input.raw):
                    continue
                m = p['regex'].search(log_input.raw)
                if m:
                    event = self._init_event(log_input)
                    self._map_fields(event, m.groupdict(), p['mapping'])
                    self._apply_static(event, p.get('static', {}))
                    self._apply_timestamp(event,
                                          p.get('timestamp') or self.ts_spec,
                                          groups=m.groupdict(),
                                          raw=log_input.raw)
                    return self._enrich_event(event)

            return None

        trx_id = match.group('id')
        redis_key = self.state_prefix + trx_id
        is_end = self.config['end_signal'] in log_input.raw
        try:
            if is_end:
                # Claim the state ATOMICALLY: if a sweeper (any worker sweeps
                # ALL keys) grabbed it first, we get None and emit only this
                # line — the collected lines were already emitted by the
                # sweep, never twice (audit A1#3).
                state = r.eval(self._GETDEL_LUA, 1, redis_key)
            else:
                state = r.get(redis_key)
        except Exception as e:
            self.last_redis_error = f"redis_get_failed: {e}"
            return None

        event = self._build_state(state, log_input, trx_id)

        if is_end:
            if self.include_original:
                event['event']['original'] = "\n".join(event['raw_buffer'])
            if 'raw_buffer' in event:
                del event['raw_buffer']
            if '_metadata' in event:
                del event['_metadata']
            return self._enrich_event(event)

        try:
            if state:
                # Conditional update: only SET while the key still exists. If
                # the sweeper deleted (and emitted) it between our GET and
                # now, a plain SET would RESURRECT already-emitted lines —
                # instead we restart the transaction from this line alone.
                updated = r.eval(self._SET_IF_EXISTS_LUA, 1, redis_key,
                                 fastjson.dumps(event), self.state_ttl)
                if not updated:
                    event = self._build_state(None, log_input, trx_id)
                    r.set(redis_key, fastjson.dumps(event), ex=self.state_ttl)
            else:
                r.set(redis_key, fastjson.dumps(event), ex=self.state_ttl)
        except Exception as e:
            self.last_redis_error = f"redis_set_failed: {e}"
            return None
        self.last_buffered = True  # state durably stored: line is consumed
        return None

    def _build_state(self, state, log_input, trx_id):
        """(Re)build the accumulated transaction event: prior state (JSON
        string) or a fresh one, plus this line's buffer entry and whatever
        the sub-patterns extract from it."""
        event = fastjson.loads(state) if state else self._init_event(log_input)
        if not state:
            event['event']['id'] = trx_id
            event['raw_buffer'] = []
            event['_metadata'] = log_input.meta
            # Event time = the transaction's FIRST line (transaction start)
            self._apply_timestamp(event, self.ts_spec, raw=log_input.raw)

        event['raw_buffer'].append(log_input.raw)

        # Every ID'd line runs against ALL sub-patterns (they accumulate),
        # so the prematch gate pays off most right here.
        for p in self.sub_patterns:
            if not _prematch_hit(p['prematch'], log_input.raw):
                continue
            m = p['regex'].search(log_input.raw)
            if m:
                self._map_fields(event, m.groupdict(), p['mapping'])
                self._apply_static(event, p.get('static', {}))
        return event

    # Atomic get-then-delete so two workers sweeping the same key can never
    # both emit it (GETDEL itself needs Redis >= 6.2; EVAL works everywhere).
    _GETDEL_LUA = ("local v=redis.call('GET',KEYS[1]) "
                   "if v then redis.call('DEL',KEYS[1]) end return v")

    # Conditional update: refuse to re-create a key that a sweeper deleted
    # between our GET and this SET — a plain SET there resurrects lines that
    # were already emitted as an incomplete event (double-emit, audit A1#3).
    _SET_IF_EXISTS_LUA = ("if redis.call('EXISTS',KEYS[1])==1 then "
                          "redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]) "
                          "return 1 else return 0 end")

    def sweep_expired(self, margin_sec=45):
        """Emit transactions whose Redis TTL is nearly gone instead of letting
        them vanish silently (audit fix C4).

        A transaction that never sees its end_signal (deferred mail, a lost
        log line) used to expire in Redis leaving no event, no DLQ entry and
        no counter. This sweep catches keys in their final TTL window, emits
        whatever was collected so far tagged event.incomplete=true /
        event.reason=transaction_timeout, and deletes the key atomically.
        Call it periodically (main.py does, every ~30s; margin_sec must stay
        larger than the call interval so no key can slip through)."""
        if self.strategy != 'stateful' or r is None:
            return []
        out = []
        try:
            for key in r.scan_iter(match=self.state_prefix + '*', count=500):
                ttl = r.ttl(key)
                if ttl is None or ttl < 0 or ttl > margin_sec:
                    continue
                state = r.eval(self._GETDEL_LUA, 1, key)
                if not state:
                    continue
                event = fastjson.loads(state)
                ev = event.setdefault('event', {})
                if self.include_original:
                    ev['original'] = "\n".join(event.get('raw_buffer') or [])
                event.pop('raw_buffer', None)
                event.pop('_metadata', None)
                ev.setdefault('outcome', 'unknown')
                ev.setdefault('reason', 'transaction_timeout')
                ev['incomplete'] = True
                enriched = self._enrich_event(event)
                if enriched:
                    out.append(enriched)
        except Exception as e:
            self.last_redis_error = f"redis_sweep_failed: {e}"
        return out

    def _init_event(self, log_input):
        # Two-timestamp model:
        #   @timestamp     = the event's REAL time (replaced from the log by
        #                    _apply_timestamp; provisional ingest time until
        #                    then, so it is never missing)
        #   event.ingested = parse-time wall clock, ALWAYS, so ingest lag is
        #                    computable (@timestamp - event.ingested)
        # event.timestamp_source makes every fallback visible: 'log',
        # 'log_assumed_utc', or 'ingest_fallback' (overwritten on success).
        ingest_iso = _now_iso()
        base_event = {
            "@timestamp": ingest_iso,
            "ecs": {"version": ECS_VERSION},
            "event": {
                "module": log_input.program,
                "ingested": ingest_iso,
                "timestamp_source": "ingest_fallback",
            },
            "observer": log_input.meta
        }

        # Only include the raw log string if it's NOT an XML file
        if self.include_original and self.strategy != 'xml_xpath':
            base_event["event"]["original"] = log_input.raw

        return base_event

    def _compile_ts_spec(self, spec):
        """Pre-compile a rule/pattern `timestamp:` declaration."""
        if not isinstance(spec, dict):
            return None
        out = dict(spec)
        out['_regex'] = None
        if spec.get('regex'):
            try:
                out['_regex'] = re.compile(spec['regex'])
            except re.error as e:
                logger.error(
                    f"Invalid timestamp regex in rule "
                    f"'{self.config.get('pattern_name', 'unknown')}': {e}")
        return out

    def _xml_find_text(self, item, path):
        if '/@' in path:
            tag, attr = path.split('/@')
            child = item.find(tag)
            return child.get(attr) if child is not None else None
        child = item.find(path)
        if child is not None and child.text:
            return child.text.strip()
        return None

    def _apply_timestamp(self, event, spec, groups=None, raw=None,
                         data=None, item=None):
        """Extract the declared event time and swap it into @timestamp.
        On any failure the provisional ingest time (already in @timestamp)
        stays, with event.timestamp_source = 'ingest_fallback' — the fallback
        is explicit and countable, never silent."""
        if not spec:
            return
        val = None
        grp = spec.get('group')
        if grp and groups:
            val = groups.get(grp)
        if val is None and spec.get('field') is not None:
            if data is not None:
                val = self._get_path(data, spec['field'])
            elif item is not None:
                val = self._xml_find_text(item, spec['field'])
        if val is None and spec.get('_regex') is not None and raw:
            m = spec['_regex'].search(raw)
            if m:
                gd = m.groupdict()
                val = gd.get('ts') if 'ts' in gd else (
                    m.group(1) if m.groups() else m.group(0))
        if val is None:
            return
        iso, source = parse_timestamp(
            val, spec.get('format', 'iso8601'), spec.get('tz'))
        if iso is not None:
            event['@timestamp'] = iso
            event.setdefault('event', {})['timestamp_source'] = source

    def _map_fields(self, event, data, mapping):
        for k, v in data.items():
            if k in mapping:
                if v is None:
                    # optional regex group that didn't participate in the
                    # match: omit the field entirely — never emit null, never
                    # crash coercing None (audit A1#9 / A2#1)
                    continue
                target, dtype = mapping[k].split('|') if '|' in mapping[k] else (mapping[k], 'str')
                if dtype == 'int':
                    try:
                        v = int(v)
                    except (ValueError, TypeError):
                        pass  # non-numeric capture stays a string, line survives
                elif dtype == 'float':
                    try:
                        v = float(v)
                    except (ValueError, TypeError):
                        pass
                self._set_nested(event, target, v)

    def _apply_static(self, event, static):
        if static:
            for k, v in static.items():
                self._set_nested(event, k, v)

    def _set_nested(self, d, path, value):
        # @timestamp is pre-set by _init_event; a rule writing to it must
        # REPLACE the provisional value, never list-promote it (a list-typed
        # @timestamp breaks the Elasticsearch date field).
        if path == '@timestamp':
            d['@timestamp'] = value
            return
        keys = path.split('.')
        for key in keys[:-1]:
            d = d.setdefault(key, {})
        last = keys[-1]
        if last in d:
            if isinstance(d[last], list):
                if isinstance(value, list):
                    for v in value:
                        if v not in d[last]: d[last].append(v)
                elif value not in d[last]:
                    d[last].append(value)
            else:
                if d[last] != value:
                    d[last] = [d[last]]
                    if isinstance(value, list):
                        d[last].extend(value)
                    else:
                        d[last].append(value)
        else:
            d[last] = value
