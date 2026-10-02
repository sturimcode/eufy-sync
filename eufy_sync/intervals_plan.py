"""What each Intervals.icu wellness date should hold.

Intervals.icu keeps one wellness record per date, and a PUT replaces the
fields it sends. So rather than sending measurements one by one, which made
the result depend on the order they were fetched in, a run works out the
desired values for every date it touches and sends a date only when those
differ from what was last sent there.

The desired values for a date come from every valid reading known for it:
the ones fetched this run plus every reading stored for that date by earlier
runs, winners or not, so splitting readings across fetches gives the same
answer as fetching them together. A raw Wi-Fi reading and its processed
record (the same id, or within the issue #48 bounds of 120 s and 0.1 kg) are
one weigh-in, and the processed values win because only they carry body fat.
The newest weigh-in of a date wins, ordered by when it started (its earliest
reading).

Dates. A reading's date is decided once, the first time it is seen, and
stored with it; a timezone change later cannot move it. A new weigh-in takes
the machine's local date of its earliest reading, so a raw/processed pair on
either side of midnight belongs to the earlier date, when the person stepped
on the scale. When a late partner joins a weigh-in that already has a date,
the existing date is kept. If two readings with different stored dates turn
out to be one weigh-in, the earliest reading's date wins and the other date
is reported as touched so it is worked out again.

Pure functions only: no I/O, no clients, so every case is cheap to test.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime

from eufy_sync.intervals_client import wellness_payload

# Same bounds as sync.UPGRADE_MAX_SECONDS / UPGRADE_MAX_WEIGHT_KG; repeated
# here so this module does not import sync.
SAME_WEIGH_IN_SECONDS = 120
SAME_WEIGH_IN_KG = 0.1


@dataclass(frozen=True)
class Reading:
    """One Eufy record as far as Intervals.icu cares: weight, body fat when
    the scale processed it, whether it is a raw weight-only reading, and the
    date it was assigned when first seen (None until then)."""

    measurement_id: str
    timestamp: datetime
    weight_kg: float
    body_fat_pct: float | None
    weight_only: bool
    fetched: bool = True
    assigned: date | None = None

    @property
    def key(self) -> tuple[str, str, bool]:
        return (self.measurement_id, self.timestamp.isoformat(), self.weight_only)

    def to_json(self) -> dict:
        return {
            "id": self.measurement_id, "ts": self.timestamp.isoformat(),
            "kg": self.weight_kg, "bf": self.body_fat_pct, "raw": self.weight_only,
            "date": self.assigned.isoformat() if self.assigned else None,
        }

    @classmethod
    def from_json(cls, value: dict) -> "Reading":
        return cls(
            measurement_id=value["id"], timestamp=datetime.fromisoformat(value["ts"]),
            weight_kg=value["kg"], body_fat_pct=value.get("bf"), weight_only=bool(value.get("raw")),
            fetched=False,
            assigned=date.fromisoformat(value["date"]) if value.get("date") else None,
        )


@dataclass
class WeighIn:
    readings: list[Reading] = field(default_factory=list)
    day: date | None = None

    @property
    def started(self) -> datetime:
        return min(r.timestamp for r in self.readings)

    @property
    def source(self) -> Reading:
        """The reading whose values are sent: the newest processed one, or
        the newest raw one when nothing processed has arrived."""
        processed = [r for r in self.readings if not r.weight_only]
        return max(processed or self.readings, key=lambda r: r.timestamp)

    @property
    def payload(self) -> dict[str, float]:
        source = self.source
        return wellness_payload(source.weight_kg, source.body_fat_pct)

    @property
    def fetched(self) -> bool:
        return any(r.fetched for r in self.readings)

    @property
    def processed(self) -> bool:
        return any(not r.weight_only for r in self.readings)


@dataclass
class DayPlan:
    """Every weigh-in now assigned to a date, and the one it should hold
    (None when every reading moved to another date)."""

    day: date
    weigh_ins: list[WeighIn]

    @property
    def winner(self) -> WeighIn | None:
        return max(self.weigh_ins, key=lambda w: w.started) if self.weigh_ins else None

    @property
    def readings(self) -> list[Reading]:
        return [r for w in self.weigh_ins for r in w.readings]


def _pairs(a: Reading, b: Reading) -> bool:
    if a.measurement_id == b.measurement_id:
        return True
    return (
        a.weight_only != b.weight_only
        and abs((a.timestamp - b.timestamp).total_seconds()) <= SAME_WEIGH_IN_SECONDS
        and abs(a.weight_kg - b.weight_kg) <= SAME_WEIGH_IN_KG
    )


def _dedupe(readings: list[Reading]) -> list[Reading]:
    """One copy per reading. The fetched copy wins for its values, but keeps
    the date a stored copy was assigned."""
    unique: dict[tuple[str, str, bool], Reading] = {}
    for r in readings:
        seen = unique.get(r.key)
        if seen is None:
            unique[r.key] = r
            continue
        fresh, other = (r, seen) if r.fetched else (seen, r)
        unique[r.key] = dataclasses.replace(fresh, assigned=fresh.assigned or other.assigned)
    return sorted(unique.values(), key=lambda r: (r.timestamp, r.measurement_id))


def group_weigh_ins(readings: list[Reading]) -> list[WeighIn]:
    """Collapse raw/processed pairs (and repeats of one id) into weigh-ins,
    each with its date set. Union-find keeps chains together regardless of
    the order the readings came in. Dates are not assigned to the readings
    here; see plan_days."""
    items = _dedupe(readings)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_id: dict[str, int] = {}
    for i, r in enumerate(items):
        if r.measurement_id in by_id:
            parent[find(i)] = find(by_id[r.measurement_id])
        else:
            by_id[r.measurement_id] = i
        # Sorted by time, so only the readings just before can be in range.
        j = i - 1
        while j >= 0 and (r.timestamp - items[j].timestamp).total_seconds() <= SAME_WEIGH_IN_SECONDS:
            if _pairs(items[j], r):
                parent[find(i)] = find(j)
            j -= 1

    groups: dict[int, WeighIn] = {}
    for i, r in enumerate(items):
        groups.setdefault(find(i), WeighIn()).readings.append(r)
    weigh_ins = sorted(groups.values(), key=lambda w: w.started)
    for w in weigh_ins:
        dated = [r for r in w.readings if r.assigned is not None]
        w.day = min(dated, key=lambda r: r.timestamp).assigned if dated else w.started.astimezone().date()
    return weigh_ins


def plan_days(readings: list[Reading]) -> dict[date, DayPlan]:
    """Plans for the dates this run touched: dates holding a fetched reading,
    and dates a reading was moved away from. Every reading in a plan carries
    its (possibly new) assigned date, ready to be stored.

    Dates known only from what was stored are left out: nothing new was
    learned about them."""
    weigh_ins = group_weigh_ins(readings)
    touched: set[date] = set()
    for w in weigh_ins:
        moved_from = {r.assigned for r in w.readings if r.assigned is not None and r.assigned != w.day}
        touched |= moved_from
        if w.fetched or moved_from:
            touched.add(w.day)
        w.readings = [dataclasses.replace(r, assigned=w.day) for r in w.readings]
    plans = {day: DayPlan(day, []) for day in touched}
    for w in weigh_ins:
        if w.day in plans:
            plans[w.day].weigh_ins.append(w)
    return plans


def belongs_to(queued_ts: datetime, queued_kg: float, queued_id: str, weigh_in: WeighIn) -> bool:
    """Whether a queued retry (which records only id, time and weight) is one
    of the readings in weigh_in."""
    return any(
        r.measurement_id == queued_id
        or (
            abs((r.timestamp - queued_ts).total_seconds()) <= SAME_WEIGH_IN_SECONDS
            and abs(r.weight_kg - queued_kg) <= SAME_WEIGH_IN_KG
        )
        for r in weigh_in.readings
    )
