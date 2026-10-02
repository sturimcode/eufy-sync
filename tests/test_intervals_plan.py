"""The per-date desired state, without a database or client."""
from __future__ import annotations

import itertools
from datetime import date, datetime, time, timedelta

from eufy_sync.intervals_plan import Reading, group_weigh_ins, plan_days

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
    plans = plan_days([full, raw])
    day = raw.timestamp.astimezone().date()
    assert list(plans) == [day]
    assert plans[day].winner.payload == {"weight": 80.0, "bodyFat": 19.5}
    assert {r.assigned for r in plans[day].readings} == {day}


def test_dates_known_only_from_what_was_sent_are_left_alone():
    stored = _r("old", 0, 80.0, fetched=False)
    tomorrow = Reading("new", NOON + timedelta(days=1), 79.0, 20.0, False)
    plans = plan_days([stored, tomorrow])
    assert list(plans) == [tomorrow.timestamp.astimezone().date()]


def test_the_newest_weigh_in_of_a_date_wins_including_what_was_sent():
    sent_later = _r("later", 3600, 80.0, fetched=False)
    fetched_earlier = _r("earlier", 0, 81.0)
    (plan,) = plan_days([fetched_earlier, sent_later]).values()
    assert plan.winner.source.measurement_id == "later"


def test_reading_json_round_trip():
    r = _r("p", 0, 80.0, bf=19.5)
    back = Reading.from_json(r.to_json())
    assert back == Reading(r.measurement_id, r.timestamp, r.weight_kg, r.body_fat_pct, r.weight_only, fetched=False)
    dated = Reading.from_json({**r.to_json(), "date": "2026-10-01"})
    assert dated.assigned == date(2026, 10, 1)


def test_a_stored_date_is_kept_whatever_the_local_date_says_now():
    stored = Reading("a", NOON, 80.0, 20.0, False, fetched=False, assigned=date(2000, 1, 1))
    fetched_copy = Reading("a", NOON, 80.0, 20.0, False)
    plans = plan_days([fetched_copy, stored])
    assert list(plans) == [date(2000, 1, 1)]


def test_a_late_partner_keeps_the_existing_date():
    midnight = datetime.combine((NOON + timedelta(days=1)).date(), time()).astimezone()
    processed = Reading("p", midnight + timedelta(seconds=10), 80.0, 19.5, False, fetched=False,
                        assigned=(midnight + timedelta(seconds=10)).date())
    raw = Reading("r", midnight - timedelta(seconds=10), 80.0, None, True)
    plans = plan_days([processed, raw])
    assert list(plans) == [processed.assigned]
    assert {r.assigned for r in plans[processed.assigned].readings} == {processed.assigned}


def test_joining_readings_with_different_dates_touches_both():
    a = Reading("x", NOON, 80.0, None, True, fetched=False, assigned=date(2000, 1, 1))
    b = Reading("x", NOON + timedelta(seconds=20), 80.0, 19.5, False, fetched=False, assigned=date(2000, 1, 2))
    plans = plan_days([a, b])
    assert set(plans) == {date(2000, 1, 1), date(2000, 1, 2)}
    assert plans[date(2000, 1, 2)].winner is None
    assert plans[date(2000, 1, 1)].winner.payload == {"weight": 80.0, "bodyFat": 19.5}
