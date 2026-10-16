"""Pure-core tests: no database."""
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from firstthings.planner import build_week, compute_anchors, in_quiet_window, validate_week
from firstthings.template import TemplateError, load_template, parse_template
from firstthings.timeutil import ROME, hhmm, minutes_between

TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "week_template.yaml"


@pytest.fixture(scope="module")
def t():
    return load_template(TEMPLATE)


def times(day_plan):
    return {i.activity: (hhmm(i.start), hhmm(i.end)) for i in day_plan.items}


# ----------------------------------------------------------- DST: work follows Perth

@pytest.mark.parametrize("day,start,end,wake", [
    (date(2026, 10, 19), "03:00", "11:00", "02:30"),  # before Italian DST ends
    (date(2026, 10, 23), "03:00", "11:00", "02:30"),  # last workday on summer time
    (date(2026, 10, 26), "02:00", "10:00", "01:30"),  # first workday on winter time
    (date(2027, 3, 26), "02:00", "10:00", "01:30"),   # last workday on winter time
    (date(2027, 3, 29), "03:00", "11:00", "02:30"),   # summer time again
])
def test_work_hours_follow_perth(t, day, start, end, wake):
    a = compute_anchors(t, day)
    assert (hhmm(a.work_start), hhmm(a.work_end), hhmm(a.wake)) == (start, end, wake)


def test_naive_arithmetic_trap_is_avoided(t):
    # Sat 24 Oct 20:30 CEST -> Sun 25 Oct 06:30 CET is 11 real hours, 10 on the wall clock.
    a = compute_anchors(t, date(2026, 10, 25))
    assert a.night_minutes == 11 * 60


# ----------------------------------------------------------- sleep: 6h a day, nap only when short

def test_summer_weekdays_have_six_hour_nights_and_no_nap(t):
    for d in range(19, 24):
        a = compute_anchors(t, date(2026, 10, d))
        assert a.night_minutes == 360 and a.nap_minutes == 0


def test_winter_weekdays_get_a_one_hour_nap_after_hcs(t):
    week = build_week(t, date(2026, 10, 26))
    for day_plan in week.days[:5]:
        a = day_plan.anchors
        assert a.night_minutes == 300 and a.nap_minutes == 60 and a.sleep_minutes == 360
        assert times(day_plan)["nap"] == ("16:00", "17:00")
        assert times(day_plan)["hcs"] == ("14:30", "16:00")
    for day_plan in week.days[5:]:
        assert day_plan.anchors.nap_minutes == 0
        assert "nap" not in times(day_plan)


def test_afternoon_blocks_follow_work_end(t):
    before = build_week(t, date(2026, 10, 19)).days[0]
    after = build_week(t, date(2026, 10, 26)).days[0]
    assert times(before)["gym"] == ("11:00", "12:00")
    assert times(before)["trading"] == ("13:30", "15:30")
    assert times(after)["gym"] == ("10:00", "11:00")
    assert times(after)["lunch_wife"] == ("11:00", "12:30")
    assert times(after)["trading"] == ("12:30", "14:30")
    # Clock-anchored family blocks do not move.
    assert times(before)["dinner"] == times(after)["dinner"] == ("19:00", "19:30")


def test_gym_days_match_template(t):
    week = build_week(t, date(2026, 10, 26))
    gym_days = [d.day.weekday() for d in week.days if any(i.activity == "gym" for i in d.items)]
    assert gym_days == [0, 1, 3, 5]  # Mon, Tue, Thu, Sat


# ----------------------------------------------------------- check-in times

def checkin_times(week, day):
    return {c.kind: hhmm(c.due) for c in week.checkpoints if c.day == day}


def test_checkins_before_and_after_dst(t):
    before = build_week(t, date(2026, 10, 19))
    after = build_week(t, date(2026, 10, 26))
    assert checkin_times(before, date(2026, 10, 19)) == {
        "post_work": "11:05", "pre_trading": "13:15", "transition": "15:30", "shutdown": "18:40"}
    assert checkin_times(after, date(2026, 10, 26)) == {
        "post_work": "10:05", "pre_trading": "12:15", "transition": "14:30", "shutdown": "18:40"}
    assert checkin_times(after, date(2026, 10, 31)) == {"saturday": "11:35"}
    assert checkin_times(after, date(2026, 11, 1)) == {"weekly_review": "17:30"}


def test_no_more_than_four_weekday_checkins(t):
    week = build_week(t, date(2026, 10, 26))
    for d in week.days:
        assert len([c for c in week.checkpoints if c.day == d.day]) <= 4


# ----------------------------------------------------------- the template validates all year

def test_template_validates_for_a_full_year(t):
    monday = date(2026, 10, 19)
    for n in range(53):
        week = build_week(t, monday + timedelta(weeks=n))
        v = validate_week(t, week)
        assert v.ok, (week.start, v.errors)


def test_entrepreneur_role_is_flagged_as_unplanned(t):
    v = validate_week(t, build_week(t, date(2026, 10, 26)))
    assert any("Entrepreneur" in w for w in v.warnings)


def test_validator_catches_overlap_and_sleep_cut(t):
    text = TEMPLATE.read_text().replace('{activity: reading, at: "20:15"}', '{activity: reading, at: "20:20"}', 1)
    v = validate_week(t, build_week(load_template_text(text), date(2026, 10, 26)))
    assert any("runs past lights out" in e for e in v.errors)

    text = TEMPLATE.read_text().replace("offset: 270}", "offset: 200}", 1)  # HCS into trading
    v = validate_week(t, build_week(load_template_text(text), date(2026, 10, 26)))
    assert any("overlaps" in e for e in v.errors)

    text = TEMPLATE.read_text().replace("max_minutes: 90", "max_minutes: 30", 1)
    v = validate_week(t, build_week(load_template_text(text), date(2026, 10, 26)))
    assert any("below the 6h target" in e for e in v.errors)


def load_template_text(text):
    return parse_template(text)


def test_template_errors_are_listed():
    with pytest.raises(TemplateError) as e:
        parse_template("roles: {self: Self}\nactivities: {gym: {role: nobody, unit: hours}}\n")
    assert any("role 'nobody'" in p for p in e.value.problems)
    assert any("unit must be" in p for p in e.value.problems)


def test_quiet_windows(t):
    week = build_week(t, date(2026, 10, 26))
    mon = week.days[0]
    assert in_quiet_window(t, mon.anchors, mon.items, datetime(2026, 10, 26, 16, 30, tzinfo=ROME)) == "Nap"
    assert in_quiet_window(t, mon.anchors, mon.items, datetime(2026, 10, 26, 19, 10, tzinfo=ROME)) == "Family dinner"
    assert in_quiet_window(t, mon.anchors, mon.items, datetime(2026, 10, 26, 1, 0, tzinfo=ROME)) == "sleep"
    assert in_quiet_window(t, mon.anchors, mon.items, datetime(2026, 10, 26, 12, 15, tzinfo=ROME)) is None


def test_minutes_between_is_real_time():
    a = datetime(2026, 10, 25, 1, 0, tzinfo=ROME)
    b = datetime(2026, 10, 25, 4, 0, tzinfo=ROME)
    assert minutes_between(a, b) == 240  # the clock went back an hour
