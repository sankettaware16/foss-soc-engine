"""
Deterministic event-timestamp parsing for the SOC engine.

Rules declare a `timestamp:` block (see docs/writing-rules.md) naming where the
event time lives and which format it is in. This module turns that raw value
into the exact same UTC ISO-8601 string shape that core.engine._now_iso()
emits (aware-UTC datetime .isoformat()), so `@timestamp` stays byte-compatible
with what Kibana/Logstash already consume.

Design rules (stdlib only, no dateutil-style guessing):
- Named formats are parsed with hand-written regexes: locale-independent and
  deterministic. A format string containing '%' is passed to strptime as an
  explicit escape hatch.
- Timezone abbreviations are NEVER guessed: only Z/UTC/GMT and numeric offsets
  (+0530, +05:30, +05:45, ...) are accepted. An ambiguous abbreviation (IST,
  EST, CET, ...) makes the value unparseable -> the caller falls back to
  ingest time and tags the event, rather than silently picking a zone.
- RFC3164 (syslog) has no year: assume the current year, unless that places
  the event more than 48h in the future, in which case use the previous year
  (this is what handles Dec 31 logs read on Jan 1). Assumption: no log source
  is ever legitimately >48h ahead of the parser's clock.
- Leap second :60 is clamped to :59 (Python datetime cannot represent it).
- Fractional seconds beyond microseconds (nanosecond ISO stamps) are truncated
  to microseconds.
- Non-English month names are deliberately NOT parsed (a multilingual month
  table is guessing: e.g. Spanish "mar" is both March and Tuesday). They fall
  back, visibly tagged.
"""
import re
import time
from datetime import datetime, timezone, timedelta

_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

_UTC_NAMES = {'z', 'utc', 'gmt', 'ut'}

_OFFSET_RE = re.compile(r'([+-])(\d{2}):?(\d{2})$')

_ISO_RE = re.compile(
    r'^\s*(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})'
    r'(?:[.,](\d{1,9}))?\s*(Z|z|[+-]\d{2}:?\d{2})?')

_CLF_RE = re.compile(
    r'^\s*(\d{1,2})/([A-Za-z]{3,})\.?/(\d{4}):(\d{1,2}):(\d{2}):(\d{2})'
    r'(?:\s*([AaPp])\.?[Mm]\.?)?(?:\s+(\S+))?')

_RFC3164_RE = re.compile(
    r'^\s*([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})')

_EPOCH_RE = re.compile(r'^\s*(\d{9,19})(?:\.(\d+))?\s*$')

_SURICATA_RE = re.compile(
    r'^\s*(\d{2})/(\d{2})/(\d{4})-(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?')

_NGINX_ERROR_RE = re.compile(
    r'^\s*(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})')

_ASCTIME_RE = re.compile(
    r'^\s*[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})'
    r'(?:\.(\d{1,9}))?\s+(\d{4})(?:\s+(\S+))?')

_ROUNDCUBE_RE = re.compile(
    r'^\s*\[?(\d{1,2})-([A-Za-z]{3,})\.?-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})'
    r'(?:\s+([^\s\]]+))?')


def parse_offset(tz_str):
    """'Z'/'UTC'/'GMT' -> utc; '+0530'/'+05:30'/'+05:45' -> fixed offset;
    IANA zone IDs ('Asia/Kolkata') -> zoneinfo (DST-correct, for rule-declared
    tz:); ambiguous abbreviations (IST, EST, ...) -> None (never guessed)."""
    if not tz_str:
        return None
    s = tz_str.strip()
    if s.lower() in _UTC_NAMES:
        return timezone.utc
    if '/' in s:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(s)
        except Exception:
            return None
    m = _OFFSET_RE.fullmatch(s)
    if not m:
        return None
    sign = 1 if m.group(1) == '+' else -1
    try:
        return timezone(sign * timedelta(hours=int(m.group(2)),
                                         minutes=int(m.group(3))))
    except ValueError:
        return None


def _frac_to_us(frac):
    if not frac:
        return 0
    return int((frac + '000000')[:6])


def _mk_dt(year, month, day, hour, minute, second, us=0, tz=None):
    """Build a datetime, clamping leap-second :60 to :59. Returns None if the
    components are not a real date/time."""
    if second == 60:
        second = 59
        if us == 0:
            us = 999999
    try:
        return datetime(year, month, day, hour, minute, second, us, tzinfo=tz)
    except ValueError:
        return None


def _month_num(name):
    return _MONTHS.get(name[:3].lower())


# Each _parse_* returns (datetime-or-None, tz_was_in_value: bool).
# The returned datetime is naive when the format carries no zone.

def _parse_iso8601(s):
    m = _ISO_RE.match(s)
    if not m:
        return None, False
    y, mo, d, h, mi, sec, frac, tzs = m.groups()
    tz = parse_offset(tzs) if tzs else None
    if tzs and tz is None:
        return None, False
    dt = _mk_dt(int(y), int(mo), int(d), int(h), int(mi), int(sec),
                _frac_to_us(frac), tz)
    return dt, tz is not None


def _parse_clf(s):
    m = _CLF_RE.match(s)
    if not m:
        return None, False
    d, mon, y, h, mi, sec, ampm, tzs = m.groups()
    month = _month_num(mon)
    if month is None:
        return None, False
    hour = int(h)
    if ampm:
        if hour < 1 or hour > 12:
            return None, False
        if ampm.lower() == 'p' and hour != 12:
            hour += 12
        elif ampm.lower() == 'a' and hour == 12:
            hour = 0
    tz = None
    if tzs:
        tz = parse_offset(tzs)
        if tz is None:
            # A zone token was present but is ambiguous/garbage: refuse.
            return None, False
    dt = _mk_dt(int(y), month, int(d), hour, int(mi), int(sec), 0, tz)
    return dt, tz is not None


def _parse_rfc3164(s, default_tz, now_ts):
    m = _RFC3164_RE.match(s)
    if not m:
        return None, False
    mon, d, h, mi, sec = m.groups()
    month = _month_num(mon)
    if month is None:
        return None, False
    tz = default_tz or timezone.utc
    now = datetime.fromtimestamp(now_ts, tz)
    dt = _mk_dt(now.year, month, int(d), int(h), int(mi), int(sec), 0, tz)
    if dt is None:
        # e.g. Feb 29 against a non-leap current year
        dt = _mk_dt(now.year - 1, month, int(d), int(h), int(mi), int(sec),
                    0, tz)
        return dt, False
    # Year inference: syslog carries no year. Current year, unless that puts
    # the event >48h in the future (Dec logs drained in Jan) -> previous year.
    if dt - now > timedelta(hours=48):
        prev = _mk_dt(now.year - 1, month, int(d), int(h), int(mi), int(sec),
                      0, tz)
        if prev is not None:
            dt = prev
    return dt, False


def _parse_epoch(v):
    if isinstance(v, bool):
        return None, False
    if isinstance(v, (int, float)):
        num = float(v)
        digits = len(str(int(abs(num)))) if num else 1
    else:
        m = _EPOCH_RE.match(str(v))
        if not m:
            return None, False
        num = float(m.group(1) + ('.' + m.group(2) if m.group(2) else ''))
        digits = len(m.group(1))
    # Magnitude heuristic: seconds (<=11 digits, valid to year 5138),
    # then milliseconds, then microseconds, then nanoseconds.
    if digits <= 11:
        secs = num
    elif digits <= 14:
        secs = num / 1e3
    elif digits <= 17:
        secs = num / 1e6
    else:
        secs = num / 1e9
    try:
        return datetime.fromtimestamp(secs, timezone.utc), True
    except (ValueError, OSError, OverflowError):
        return None, False


def _parse_suricata(s):
    m = _SURICATA_RE.match(s)
    if not m:
        return None, False
    mo, d, y, h, mi, sec, frac = m.groups()
    dt = _mk_dt(int(y), int(mo), int(d), int(h), int(mi), int(sec),
                _frac_to_us(frac))
    return dt, False


def _parse_nginx_error(s):
    m = _NGINX_ERROR_RE.match(s)
    if not m:
        return None, False
    y, mo, d, h, mi, sec = m.groups()
    dt = _mk_dt(int(y), int(mo), int(d), int(h), int(mi), int(sec))
    return dt, False


def _parse_asctime(s):
    m = _ASCTIME_RE.match(s)
    if not m:
        return None, False
    mon, d, h, mi, sec, frac, y, tzs = m.groups()
    month = _month_num(mon)
    if month is None:
        return None, False
    tz = None
    if tzs:
        tz = parse_offset(tzs)
        if tz is None:
            return None, False
    dt = _mk_dt(int(y), month, int(d), int(h), int(mi), int(sec),
                _frac_to_us(frac), tz)
    return dt, tz is not None


def _parse_roundcube(s):
    m = _ROUNDCUBE_RE.match(s)
    if not m:
        return None, False
    d, mon, y, h, mi, sec, tzs = m.groups()
    month = _month_num(mon)
    if month is None:
        return None, False
    tz = None
    if tzs:
        tz = parse_offset(tzs)
        if tz is None:
            return None, False
    dt = _mk_dt(int(y), month, int(d), int(h), int(mi), int(sec), 0, tz)
    return dt, tz is not None


def parse_timestamp(value, fmt='iso8601', default_tz=None, now_ts=None):
    """Parse a raw timestamp value into (iso_utc_string, source).

    source: 'log'             - parsed; zone came from the value itself, the
                                rule's declared tz, or the format (epoch=UTC)
            'log_assumed_utc' - parsed, but no zone in the value AND none
                                declared: UTC was assumed (visible on purpose)
    Returns (None, None) when the value cannot be parsed confidently; the
    engine then keeps ingest time and tags 'ingest_fallback'.
    """
    if value is None:
        return None, None
    if now_ts is None:
        now_ts = time.time()

    tz_obj = parse_offset(default_tz) if default_tz else None

    if fmt == 'epoch':
        dt, tz_in_value = _parse_epoch(value)
    else:
        s = str(value)
        if '%' in fmt:
            try:
                dt = datetime.strptime(s.strip(), fmt)
                tz_in_value = dt.tzinfo is not None
            except ValueError:
                return None, None
        elif fmt == 'iso8601':
            dt, tz_in_value = _parse_iso8601(s)
        elif fmt == 'clf':
            dt, tz_in_value = _parse_clf(s)
        elif fmt == 'rfc3164':
            dt, tz_in_value = _parse_rfc3164(s, tz_obj, now_ts)
        elif fmt == 'suricata':
            dt, tz_in_value = _parse_suricata(s)
        elif fmt == 'nginx_error':
            dt, tz_in_value = _parse_nginx_error(s)
        elif fmt == 'asctime':
            dt, tz_in_value = _parse_asctime(s)
        elif fmt == 'roundcube':
            dt, tz_in_value = _parse_roundcube(s)
        else:
            return None, None

    if dt is None:
        return None, None
    if dt.year < 1970 or dt.year > 9999:
        return None, None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_obj if tz_obj is not None else timezone.utc)
    # Source reflects where the zone truly came from: the value itself or the
    # rule's declared tz -> 'log'; otherwise UTC was assumed -> tagged.
    # (rfc3164 attaches its zone during parsing for year inference, so the
    # tzinfo check alone is not authoritative — tz_in_value is.)
    if tz_in_value or tz_obj is not None:
        source = 'log'
    else:
        source = 'log_assumed_utc'

    # Same string shape as core.engine._now_iso(): aware-UTC .isoformat()
    return dt.astimezone(timezone.utc).isoformat(), source
