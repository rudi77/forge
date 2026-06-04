"""Minimaler 5-Feld-Cron-Matcher für `schedule`-Trigger (Conductor Phase B).

Bewusst dependency-frei und rein — der Conductor-Heartbeat wertet damit
``ScheduleTriggerConfig.cron`` aus, ohne eine externe croniter-Lib. Unterstützt
den Standard-Subset:

    ┌── minute (0-59)
    │ ┌── hour (0-23)
    │ │ ┌── day-of-month (1-31)
    │ │ │ ┌── month (1-12)
    │ │ │ │ ┌── day-of-week (0-6, 0 = Sonntag; 7 wird als Sonntag akzeptiert)
    *  *  *  *  *

Pro Feld erlaubt: ``*``, ``*/n`` (Step), ``a-b`` (Range), ``a-b/n``,
Listen ``a,b,c`` und Einzelwerte. **Nicht** unterstützt: Monats-/Wochentag-
Namen (JAN/MON), ``L``/``#``/``?``-Erweiterungen, 6-/7-Feld-Cron mit
Sekunden/Jahr. Eingaben sind Operator-Config (trusted), kein User-Input.

Alle Zeiten sind UTC — naive oder nicht-UTC-Eingaben werden vom Caller
normalisiert; hier wird auf den vollen Minuten-Tick gerechnet.
"""

from __future__ import annotations

from datetime import datetime, timedelta

_FIELD_BOUNDS = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day-of-month
    (1, 12),  # month
    (0, 7),  # day-of-week (Sonntag = 0; 7 wird ebenfalls akzeptiert)
)


class CronError(ValueError):
    """Ungültiger Cron-Ausdruck."""


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Parst ein einzelnes Cron-Feld in die Menge der erlaubten Werte."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty cron field component in {field!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise CronError(f"invalid step {step_s!r} in {field!r}") from None
            if step <= 0:
                raise CronError(f"step must be > 0 in {field!r}")
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_s, _, end_s = base.partition("-")
            try:
                start, end = int(start_s), int(end_s)
            except ValueError:
                raise CronError(f"invalid range {base!r} in {field!r}") from None
        else:
            try:
                start = end = int(base)
            except ValueError:
                raise CronError(f"invalid value {base!r} in {field!r}") from None

        if start > end:
            raise CronError(f"range start > end in {base!r}")
        for v in range(start, end + 1, step):
            if not lo <= v <= hi:
                raise CronError(f"value {v} out of bounds [{lo},{hi}] in {field!r}")
            values.add(v)
    return values


def parse_cron(expr: str) -> tuple[set[int], ...]:
    """Parst einen 5-Feld-Cron-Ausdruck in fünf Wertemengen.

    Raises :class:`CronError` bei falscher Feldzahl oder ungültigem Feld.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(
            f"cron must have exactly 5 fields, got {len(fields)}: {expr!r}"
        )
    parsed: list[set[int]] = []
    for idx, (field, (lo, hi)) in enumerate(zip(fields, _FIELD_BOUNDS, strict=True)):
        vals = _parse_field(field, lo, hi)
        # day-of-week (Index 4): Sonntag darf 0 ODER 7 sein → auf 0 normalisieren,
        # damit der Matcher mit weekday() (Sonntag=0 nach Umrechnung) vergleicht.
        if idx == 4 and 7 in vals:
            vals.discard(7)
            vals.add(0)
        parsed.append(vals)
    return tuple(parsed)


def cron_matches(expr: str, when: datetime) -> bool:
    """True, wenn der Cron-Ausdruck auf die Minute von ``when`` passt.

    Day-of-month UND day-of-week werden — wie bei Vixie-Cron — mit ODER
    verknüpft, sobald beide eingeschränkt sind. Sind beide ``*``, matcht der
    Tag immer.
    """
    minute, hour, dom, month, dow = parse_cron(expr)
    if when.minute not in minute or when.hour not in hour or when.month not in month:
        return False
    # Python: Montag=0..Sonntag=6 → Cron: Sonntag=0..Samstag=6.
    cron_dow = (when.weekday() + 1) % 7
    dom_restricted = len(dom) < 31
    dow_restricted = len(dow) < 7
    dom_ok = when.day in dom
    dow_ok = cron_dow in dow
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def cron_due(expr: str, *, last: datetime | None, now: datetime) -> bool:
    """True, wenn der Cron seit ``last`` (exklusiv) bis ``now`` (inklusiv)
    mindestens eine fällige Minute hatte.

    ``last=None`` (noch nie gelaufen) → fällig, wenn ``now`` selbst matcht.
    Das Fenster wird minutenweise gescannt und auf 36h (2160 Minuten)
    begrenzt, damit ein lange schlafender Heartbeat nicht endlos iteriert —
    eine fällige Minute innerhalb der letzten 36h reicht zum Feuern.
    """
    now = now.replace(second=0, microsecond=0)
    if last is None:
        return cron_matches(expr, now)
    last = last.replace(second=0, microsecond=0)
    if now <= last:
        return False
    window_start = max(last + timedelta(minutes=1), now - timedelta(minutes=2160))
    cur = window_start
    while cur <= now:
        if cron_matches(expr, cur):
            return True
        cur += timedelta(minutes=1)
    return False
