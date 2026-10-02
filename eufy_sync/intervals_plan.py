"""What each Intervals.icu wellness date should hold.

Intervals.icu keeps one wellness record per date, and a PUT replaces the
fields it sends. So rather than sending measurements one by one, which made
the result depend on the order they were fetched in, a run works out the
desired values for every date it touches and sends a date only when those
differ from what was last sent there.

The desired values for a date come from every valid measurement known for
it: the ones fetched this run plus the weigh-in last sent to that date. A raw
Wi-Fi reading and its processed record (the same id, or within the issue #48
bounds of 120 s and 0.1 kg) are one weigh-in, and the processed values win
because only they carry body fat. The newest weigh-in of a date wins.

A weigh-in whose raw and processed records fall on either side of midnight
belongs to the date of its earliest record, which is when the person stepped
on the scale. Dates are the machine's local date, as for Garmin.

Pure functions only: no I/O, no clients, so every case is cheap to test.
"""
from __future__ import annotations

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
    the scale processed it, and whether it is a raw weight-only reading."""

    measurement_id: str
    timestamp: datetime
    weight_kg: float
    body_fat_pct: float | None
    weight_only: bool
    fetched: bool = True

    def to_json(self) -> dict:
        return {
            "id": self.measurement_id, "ts": self.timestamp.isoformat(),
            "kg": self.weight_kg, "bf": self.body_fat_pct, "raw": self.weight_only,
        }

    @classmethod
    def from_json(cls, value: dict) -> "Reading":
        return cls(
            measurement_id=value["id"], timestamp=datetime.fromisoformat(value["ts"]),
            weight_kg=value["kg"], body_fat_pct=value.get("bf"), weight_only=bool(value.get("raw")),
            fetched=False,
        )


@dataclass
class WeighIn:
    readings: list[Reading] = field(default_factory=list)

    @property
    def started(self) -> datetime:
        return min(r.timestamp for r in self.readings)

    @property
    def local_date(self) -> date:
        return self.started.astimezone().date()

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


def _pairs(a: Reading, b: Reading) -> bool:
    if a.measurement_id == b.measurement_id:
        return True
    return (
        a.weight_only != b.weight_only
        and abs((a.timestamp - b.timestamp).total_seconds()) <= SAME_WEIGH_IN_SECONDS
        and abs(a.weight_kg - b.weight_kg) <= SAME_WEIGH_IN_KG
    )


def group_weigh_ins(readings: list[Reading]) -> list[WeighIn]:
    """Collapse raw/processed pairs (and repeats of one id) into weigh-ins.

    The same reading can arrive twice (fetched, and stored with the last
    sent date); the fetched copy is kept. Union-find keeps chains together
    regardless of the order the readings came in."""
    unique: dict[tuple[str, str, bool], Reading] = {}
    for r in readings:
        key = (r.measurement_id, r.timestamp.isoformat(), r.weight_only)
        if key not in unique or r.fetched:
            unique[key] = r
    items = sorted(unique.values(), key=lambda r: (r.timestamp, r.measurement_id))

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
    return sorted(groups.values(), key=lambda w: w.started)


def desired_by_date(readings: list[Reading]) -> dict[date, WeighIn]:
    """The weigh-in each date should hold, for dates with a fetched reading.

    Dates whose only knowledge is what was already sent are left out: this
    run learned nothing new about them."""
    weigh_ins = group_weigh_ins(readings)
    touched = {w.local_date for w in weigh_ins if w.fetched}
    desired: dict[date, WeighIn] = {}
    for w in weigh_ins:
        day = w.local_date
        if day in touched and (day not in desired or w.started > desired[day].started):
            desired[day] = w
    return desired


def same_weigh_in(queued_ts: datetime, queued_kg: float, queued_id: str, weigh_in: WeighIn) -> bool:
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
