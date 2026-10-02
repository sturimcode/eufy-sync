"""The per-date desired state, without a database or client."""
from __future__ import annotations

import itertools
from datetime import datetime, time, timedelta

from eufy_sync.intervals_plan import Reading, desired_by_date, group_weigh_ins

NOON = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=2)


def _r(mid, seconds, kg, *, raw=False, bf=20.0, fetched=True):
    return Reading(mid, NOON + timedelta(seconds=seconds), kg, None if raw else bf, raw, fetched)


def test_grouping_does_not_depend_on_input_order():
    readings = [_r("p", 0, 80.0), _r("r", 30, 80.05, raw=True), _r("other", 3600, 79.0)]
    expected = None
    for order in itertools.permutations(readings):
        groups = sorted(sorted(r.measurement_id for r in w.readings) for w in group_weigh_ins(list(order)))
        expected = expected or groups
        assert groups == expected == [["other"], ["p", "r"]]


def test_processed_values_win_inside_a_weigh_in_even_when_the_raw_is_later():
    (w,) = group_weigh_ins([_r("r", 30, 80.05, raw=True), _r("p", 0, 80.0, bf=19.5)])
    assert w.source.measurement_id == "p"
    assert w.payload == {"weight": 80.0, "bodyFat": 19.5}
    assert w.started == NOON


def test_two_processed_readings_close_together_stay_separate_weigh_ins():
    assert len(group_weigh_ins([_r("a", 0, 80.0), _r("b", 30, 80.0)])) == 2


def test_raw_outside_the_bounds_is_its_own_weigh_in():
    assert len(group_weigh_ins([_r("p", 0, 80.0), _r("r", 121, 80.0, raw=True)])) == 2
    assert len(group_weigh_ins([_r("p", 0, 80.0), _r("r", 30, 80.2, raw=True)])) == 2


def test_a_pair_across_midnight_belongs_to_the_earlier_date():
    midnight = datetime.combine((NOON + timedelta(days=1)).date(), time()).astimezone()
    raw = Reading("x", midnight - timedelta(seconds=10), 80.0, None, True)
    full = Reading("x", midnight + timedelta(seconds=10), 80.0, 19.5, False)
    desired = desired_by_date([full, raw])
    assert list(desired) == [raw.timestamp.astimezone().date()]
    assert desired[raw.timestamp.astimezone().date()].payload == {"weight": 80.0, "bodyFat": 19.5}


def test_dates_known_only_from_what_was_sent_are_left_alone():
    stored = _r("old", 0, 80.0, fetched=False)
    tomorrow = Reading("new", NOON + timedelta(days=1), 79.0, 20.0, False)
    desired = desired_by_date([stored, tomorrow])
    assert list(desired) == [tomorrow.timestamp.astimezone().date()]


def test_the_newest_weigh_in_of_a_date_wins_including_what_was_sent():
    sent_later = _r("later", 3600, 80.0, fetched=False)
    fetched_earlier = _r("earlier", 0, 81.0)
    (weigh_in,) = desired_by_date([fetched_earlier, sent_later]).values()
    assert weigh_in.source.measurement_id == "later"


def test_reading_json_round_trip():
    r = _r("p", 0, 80.0, bf=19.5)
    back = Reading.from_json(r.to_json())
    assert back == Reading(r.measurement_id, r.timestamp, r.weight_kg, r.body_fat_pct, r.weight_only, fetched=False)
