"""Pure planning core: template -> concrete days, sleep anchors, check-in times, validation.

No database, no network. Everything here is deterministic and unit-tested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .template import ANCHORS, CheckinRule, Template
from .timeutil import at, day_label, fmt_minutes, hhmm, minutes_between, overlaps, shift, within


@dataclass(frozen=True)
class DayAnchors:
    day: date
    workday: bool
    work_start: datetime | None
    work_end: datetime | None
    wake: datetime
    lights_out: datetime          # tonight
    prev_lights_out: datetime     # last night
    night_minutes: int            # sleep in the night ending this morning
    nap_minutes: int

    def get(self, name: str) -> datetime | None:
        return {
            "work_start": self.work_start,
            "work_end": self.work_end,
            "wake": self.wake,
            "lights_out": self.lights_out,
        }[name]

    @property
    def sleep_minutes(self) -> int:
        return self.night_minutes + self.nap_minutes

    def to_json(self) -> dict:
        def iso(d):
            return d.isoformat() if d else None
        return {
            "workday": self.workday,
            "work_start": iso(self.work_start),
            "work_end": iso(self.work_end),
            "wake": iso(self.wake),
            "lights_out": iso(self.lights_out),
            "prev_lights_out": iso(self.prev_lights_out),
            "night_minutes": self.night_minutes,
            "nap_minutes": self.nap_minutes,
        }


@dataclass
class PlannedItem:
    day: date
    activity: str
    role: str
    title: str
    start: datetime
    end: datetime
    minutes: int
    quadrant: int | None = None
    id: int | None = None  # set once stored


@dataclass
class DayPlan:
    anchors: DayAnchors
    items: list[PlannedItem]

    @property
    def day(self) -> date:
        return self.anchors.day


@dataclass
class CheckpointSpec:
    day: date
    kind: str
    due: datetime
    focus: tuple[str, ...]
    sweep: bool
    always: bool
    preview_tomorrow: bool
    week_summary: bool


@dataclass
class WeekPlan:
    start: date
    days: list[DayPlan]
    checkpoints: list[CheckpointSpec] = field(default_factory=list)


# ---------------------------------------------------------------- anchors

def _work_window(t: Template, day: date) -> tuple[datetime, datetime] | None:
    if not t.is_workday(day.weekday()):
        return None
    # The employer-zone shift whose start falls on `day` in local time.
    for delta in (0, 1, -1):
        emp_day = day + timedelta(days=delta)
        start = at(emp_day, t.employment.start, t.employment.tz).astimezone(t.tz)
        if start.date() == day:
            end = at(emp_day, t.employment.end, t.employment.tz)
            if t.employment.end <= t.employment.start:
                end = at(emp_day + timedelta(days=1), t.employment.end, t.employment.tz)
            return start, end.astimezone(t.tz)
    return None


def _wake(t: Template, day: date) -> datetime:
    work = _work_window(t, day)
    if work:
        return shift(work[0], -t.sleep.wake_before_work)
    return at(day, t.sleep.weekend_wake, t.tz)


def compute_anchors(t: Template, day: date) -> DayAnchors:
    work = _work_window(t, day)
    wake = _wake(t, day)
    lights_out = at(day, t.sleep.lights_out, t.tz)
    prev_lights_out = at(day - timedelta(days=1), t.sleep.lights_out, t.tz)
    night = minutes_between(prev_lights_out, wake)
    nap = 0
    if work is not None and t.sleep.nap_after in ("work_end", "work_start"):
        short = max(0, t.sleep.target_minutes - night)
        nap = min(t.sleep.nap_max, (short // 5) * 5)
        if nap < 15:
            nap = 0
    return DayAnchors(
        day=day,
        workday=work is not None,
        work_start=work[0] if work else None,
        work_end=work[1] if work else None,
        wake=wake,
        lights_out=lights_out,
        prev_lights_out=prev_lights_out,
        night_minutes=night,
        nap_minutes=nap,
    )


# ---------------------------------------------------------------- days and weeks

def build_day(t: Template, day: date) -> DayPlan:
    anchors = compute_anchors(t, day)
    items: list[PlannedItem] = []
    for slot in t.week[day.weekday()]:
        act = t.activities[slot.activity]
        minutes = slot.minutes or act.minutes
        if slot.at is not None:
            start = at(day, slot.at, t.tz)
        else:
            base = anchors.get(slot.after)
            if base is None:  # e.g. work_end on a non-workday: skipped, flagged by validate_week
                continue
            start = shift(base, slot.offset)
        items.append(PlannedItem(
            day=day, activity=act.key, role=act.role, title=act.name,
            start=start, end=shift(start, minutes), minutes=minutes, quadrant=act.quadrant,
        ))
    if anchors.nap_minutes:
        base = anchors.get(t.sleep.nap_after)
        start = shift(base, t.sleep.nap_offset)
        nap = t.activities["nap"]
        items.append(PlannedItem(
            day=day, activity="nap", role=nap.role, title=nap.name,
            start=start, end=shift(start, anchors.nap_minutes), minutes=anchors.nap_minutes,
            quadrant=nap.quadrant,
        ))
    items.sort(key=lambda i: i.start)
    return DayPlan(anchors=anchors, items=items)


def build_week(t: Template, monday: date) -> WeekPlan:
    if monday.weekday() != 0:
        raise ValueError(f"{monday} is not a Monday")
    days = [build_day(t, monday + timedelta(days=i)) for i in range(7)]
    checkpoints: list[CheckpointSpec] = []
    for d in days:
        checkpoints.extend(resolve_checkins(t, d.anchors, d.items))
    return WeekPlan(start=monday, days=days, checkpoints=checkpoints)


# ---------------------------------------------------------------- check-ins

def _rule_time(rule: CheckinRule, t: Template, anchors: DayAnchors, items) -> datetime | None:
    def first(key: str):
        for i in items:
            if i.activity == key:
                return i
        return None

    def point(ref: str, edge: str) -> datetime | None:
        if ref in ANCHORS:
            return anchors.get(ref)
        item = first(ref)
        if item is None:
            return None
        return item.start if edge == "start" else item.end

    if rule.at is not None:
        return shift(at(anchors.day, rule.at, t.tz), rule.offset)
    if rule.after:
        base = point(rule.after, "end")
        return shift(base, rule.offset) if base else None
    if rule.before:
        base = point(rule.before, "start")
        return shift(base, -rule.offset) if base else None
    if rule.at_end_of:
        base = point(rule.at_end_of, "end")
        return shift(base, rule.offset) if base else None
    if rule.at_start_of:
        base = point(rule.at_start_of, "start")
        return shift(base, rule.offset) if base else None
    return None


def resolve_checkins(t: Template, anchors: DayAnchors, items) -> list[CheckpointSpec]:
    """Check-in times for one day, from the day's *current* items (works for stored items too)."""
    out = []
    for rule in t.checkin_rules(anchors.day.weekday()):
        due = _rule_time(rule, t, anchors, items)
        if due is None:
            continue
        out.append(CheckpointSpec(
            day=anchors.day, kind=rule.kind, due=due, focus=rule.focus, sweep=rule.sweep,
            always=rule.always, preview_tomorrow=rule.preview_tomorrow, week_summary=rule.week_summary,
        ))
    out.sort(key=lambda c: c.due)
    return out


def quiet_windows(t: Template, anchors: DayAnchors, items) -> list[tuple[datetime, datetime, str]]:
    wins = [
        (anchors.prev_lights_out, anchors.wake, "sleep"),
        (anchors.lights_out, shift(anchors.lights_out, 12 * 60), "sleep"),
    ]
    for i in items:
        act = t.activities.get(i.activity)
        if act and act.quiet:
            wins.append((i.start, i.end, act.name))
    return wins


def in_quiet_window(t: Template, anchors: DayAnchors, items, when: datetime) -> str | None:
    for start, end, label in quiet_windows(t, anchors, items):
        if within(when, start, end):
            return label
    return None


# ---------------------------------------------------------------- validation

@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_week(t: Template, week: WeekPlan) -> Validation:
    v = Validation()
    used_roles: set[str] = set()
    planned: dict[str, int] = {}

    for d in week.days:
        a = d.anchors
        label = day_label(d.day)
        items = sorted(d.items, key=lambda i: i.start)
        for i in items:
            act = t.activities[i.activity]
            used_roles.add(i.role)
            planned[i.activity] = planned.get(i.activity, 0) + (i.minutes if act.unit == "minutes" else 1)
            span = f"{i.title} {hhmm(i.start)}-{hhmm(i.end)}"
            if a.work_start and not act.during_work and overlaps(i.start, i.end, a.work_start, a.work_end):
                v.errors.append(f"{label}: {span} overlaps work {hhmm(a.work_start)}-{hhmm(a.work_end)}")
            if minutes_between(a.wake, i.start) < 0:
                v.errors.append(f"{label}: {span} starts before wake {hhmm(a.wake)}")
            if minutes_between(i.end, a.lights_out) < 0:
                v.errors.append(f"{label}: {span} runs past lights out {hhmm(a.lights_out)}")
        for x, y in zip(items, items[1:]):
            if overlaps(x.start, x.end, y.start, y.end):
                v.errors.append(f"{label}: {x.title} {hhmm(x.start)}-{hhmm(x.end)} overlaps {y.title} {hhmm(y.start)}")
        for slot in t.week[d.day.weekday()]:
            if slot.after and a.get(slot.after) is None:
                v.errors.append(f"{label}: {slot.activity} is placed after {slot.after}, which this day does not have")
        if a.sleep_minutes < t.sleep.target_minutes - 5:
            msg = (f"{label}: sleep {fmt_minutes(a.night_minutes)} night + {fmt_minutes(a.nap_minutes)} nap "
                   f"is below the {fmt_minutes(t.sleep.target_minutes)} target")
            (v.errors if a.workday else v.warnings).append(msg)
        if a.nap_minutes and a.nap_minutes < t.sleep.target_minutes - a.night_minutes:
            v.warnings.append(f"{label}: nap capped at {fmt_minutes(a.nap_minutes)}")

    for c in week.checkpoints:
        d = next(x for x in week.days if x.day == c.day)
        quiet = in_quiet_window(t, d.anchors, d.items, c.due)
        if quiet:
            v.errors.append(f"{day_label(c.day)}: check-in '{c.kind}' at {hhmm(c.due)} falls in quiet time ({quiet})")

    for role, name in t.roles.items():
        if role == "employee":
            continue
        if role not in used_roles:
            v.warnings.append(f"{name} has no planned time this week")
    for key, tg in t.targets.items():
        have = planned.get(key, 0)
        if have < tg.floor:
            v.warnings.append(f"{t.activities[key].name}: only {have} planned against a floor of {tg.floor}")
    return v
