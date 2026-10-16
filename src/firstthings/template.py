"""Load and structurally check the week template (config/week_template.yaml)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .timeutil import DAY_KEYS, parse_hhmm

UNITS = {"session", "minutes", "count"}
FLEX = {"fixed", "movable", "droppable"}
CONFIRM = {"ask", "auto"}
ANCHORS = {"work_start", "work_end", "wake", "lights_out"}


class TemplateError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Employment:
    tz: ZoneInfo
    start: time
    end: time
    days: frozenset[int]


@dataclass(frozen=True)
class SleepRule:
    target_minutes: int
    lights_out: time
    wake_before_work: int
    weekend_wake: time
    nap_after: str
    nap_offset: int
    nap_max: int


@dataclass(frozen=True)
class Activity:
    key: str
    name: str
    role: str
    minutes: int
    min_minutes: int | None
    unit: str
    flexibility: str
    quadrant: int | None
    protected: bool = False
    quiet: bool = False
    confirm: str = "ask"
    during_work: bool = False
    sleep: bool = False


@dataclass(frozen=True)
class Slot:
    activity: str
    at: time | None = None
    after: str | None = None
    offset: int = 0
    minutes: int | None = None


@dataclass(frozen=True)
class Target:
    activity: str
    floor: int
    goal: int


@dataclass(frozen=True)
class CheckinRule:
    kind: str
    at: time | None = None
    after: str | None = None
    before: str | None = None
    at_end_of: str | None = None
    at_start_of: str | None = None
    offset: int = 0
    sweep: bool = False
    focus: tuple[str, ...] = ()
    always: bool = False
    preview_tomorrow: bool = False
    week_summary: bool = False


@dataclass(frozen=True)
class CheckinConfig:
    workday: tuple[CheckinRule, ...]
    by_day: dict[int, tuple[CheckinRule, ...]]
    expire_after: int
    stale_after: int
    daily_cap: int
    sweep_days: int
    on_track_window: int
    day_close: time


@dataclass(frozen=True)
class Template:
    tz: ZoneInfo
    employment: Employment
    sleep: SleepRule
    roles: dict[str, str]
    activities: dict[str, Activity]
    week: dict[int, tuple[Slot, ...]]
    targets: dict[str, Target]
    checkins: CheckinConfig
    source_hash: str = field(default="")

    def is_workday(self, weekday: int) -> bool:
        return weekday in self.employment.days

    def checkin_rules(self, weekday: int) -> tuple[CheckinRule, ...]:
        if weekday in self.checkins.by_day:
            return self.checkins.by_day[weekday]
        return self.checkins.workday if self.is_workday(weekday) else ()


def _day_index(key: str, problems: list[str]) -> int:
    key = str(key).lower()
    if key not in DAY_KEYS:
        problems.append(f"unknown day '{key}'")
        return -1
    return DAY_KEYS.index(key)


def _time(raw, where: str, problems: list[str]) -> time | None:
    try:
        return parse_hhmm(raw)
    except Exception:
        problems.append(f"{where}: '{raw}' is not HH:MM")
        return None


def load_template(path: str | Path) -> Template:
    text = Path(path).read_text(encoding="utf-8")
    return parse_template(text)


def parse_template(text: str) -> Template:
    raw = yaml.safe_load(text) or {}
    problems: list[str] = []

    try:
        tz = ZoneInfo(raw.get("timezone", "Europe/Rome"))
    except Exception:
        problems.append(f"unknown timezone {raw.get('timezone')!r}")
        tz = ZoneInfo("Europe/Rome")

    emp = raw.get("employment") or {}
    try:
        emp_tz = ZoneInfo(emp.get("timezone", "Australia/Perth"))
    except Exception:
        problems.append(f"unknown employment timezone {emp.get('timezone')!r}")
        emp_tz = ZoneInfo("Australia/Perth")
    employment = Employment(
        tz=emp_tz,
        start=_time(emp.get("start", "09:00"), "employment.start", problems) or time(9),
        end=_time(emp.get("end", "17:00"), "employment.end", problems) or time(17),
        days=frozenset(_day_index(d, problems) for d in emp.get("days", DAY_KEYS[:5])),
    )

    sl = raw.get("sleep") or {}
    nap = sl.get("nap") or {}
    sleep = SleepRule(
        target_minutes=int(sl.get("target_minutes", 360)),
        lights_out=_time(sl.get("lights_out", "20:30"), "sleep.lights_out", problems) or time(20, 30),
        wake_before_work=int(sl.get("wake_before_work", 30)),
        weekend_wake=_time(sl.get("weekend_wake", "06:30"), "sleep.weekend_wake", problems) or time(6, 30),
        nap_after=str(nap.get("after", "work_end")),
        nap_offset=int(nap.get("offset", 360)),
        nap_max=int(nap.get("max_minutes", 90)),
    )
    if sleep.nap_after not in ANCHORS:
        problems.append(f"sleep.nap.after must be one of {sorted(ANCHORS)}")

    roles = {str(k): str(v) for k, v in (raw.get("roles") or {}).items()}
    if not roles:
        problems.append("no roles defined")

    activities: dict[str, Activity] = {}
    for key, a in (raw.get("activities") or {}).items():
        where = f"activities.{key}"
        a = a or {}
        if a.get("role") not in roles:
            problems.append(f"{where}: role {a.get('role')!r} is not in roles")
        unit = a.get("unit", "minutes")
        if unit not in UNITS:
            problems.append(f"{where}: unit must be one of {sorted(UNITS)}")
        flex = a.get("flexibility", "movable")
        if flex not in FLEX:
            problems.append(f"{where}: flexibility must be one of {sorted(FLEX)}")
        confirm = a.get("confirm", "ask")
        if confirm not in CONFIRM:
            problems.append(f"{where}: confirm must be ask or auto")
        activities[key] = Activity(
            key=key,
            name=str(a.get("name", key)),
            role=str(a.get("role")),
            minutes=int(a.get("minutes", 30)),
            min_minutes=a.get("min_minutes"),
            unit=unit,
            flexibility=flex,
            quadrant=a.get("quadrant"),
            protected=bool(a.get("protected", False)),
            quiet=bool(a.get("quiet", False)),
            confirm=confirm,
            during_work=bool(a.get("during_work", False)),
            sleep=bool(a.get("sleep", False)),
        )
    if "nap" not in activities:
        problems.append("activities.nap is required (the sleep rule places naps)")

    groups: dict[str, tuple[Slot, ...]] = {}
    for gname, slots in (raw.get("groups") or {}).items():
        parsed = []
        for i, s in enumerate(slots or []):
            where = f"groups.{gname}[{i}]"
            act = s.get("activity")
            if act not in activities:
                problems.append(f"{where}: unknown activity {act!r}")
            if ("at" in s) == ("after" in s):
                problems.append(f"{where}: give exactly one of 'at' or 'after'")
            if "after" in s and s["after"] not in ANCHORS:
                problems.append(f"{where}: after must be one of {sorted(ANCHORS)}")
            parsed.append(Slot(
                activity=act,
                at=_time(s["at"], where, problems) if "at" in s else None,
                after=s.get("after"),
                offset=int(s.get("offset", 0)),
                minutes=int(s["minutes"]) if "minutes" in s else None,
            ))
        groups[gname] = tuple(parsed)

    week: dict[int, tuple[Slot, ...]] = {i: () for i in range(7)}
    for dkey, gnames in (raw.get("week") or {}).items():
        idx = _day_index(dkey, problems)
        slots: list[Slot] = []
        for g in gnames or []:
            if g not in groups:
                problems.append(f"week.{dkey}: unknown group {g!r}")
                continue
            slots.extend(groups[g])
        if idx >= 0:
            week[idx] = tuple(slots)

    targets: dict[str, Target] = {}
    for key, tg in (raw.get("targets") or {}).items():
        if key not in activities:
            problems.append(f"targets.{key}: unknown activity")
            continue
        floor, goal = int(tg.get("floor", 0)), int(tg.get("goal", tg.get("floor", 0)))
        if goal < floor:
            problems.append(f"targets.{key}: goal below floor")
        targets[key] = Target(activity=key, floor=floor, goal=goal)

    ck = raw.get("checkins") or {}

    def rules(items, where) -> tuple[CheckinRule, ...]:
        out = []
        for i, r in enumerate(items or []):
            w = f"checkins.{where}[{i}]"
            timing = [k for k in ("at", "after", "before", "at_end_of", "at_start_of") if k in r]
            if len(timing) != 1:
                problems.append(f"{w}: give exactly one timing key")
            for f in r.get("focus", []) or []:
                if f not in activities:
                    problems.append(f"{w}: unknown focus activity {f!r}")
            for k in ("before", "at_end_of", "at_start_of", "after"):
                ref = r.get(k)
                if ref and ref not in ANCHORS and ref not in activities:
                    problems.append(f"{w}: {k} refers to unknown {ref!r}")
            out.append(CheckinRule(
                kind=str(r.get("kind", f"{where}_{i}")),
                at=_time(r["at"], w, problems) if "at" in r else None,
                after=r.get("after"),
                before=r.get("before"),
                at_end_of=r.get("at_end_of"),
                at_start_of=r.get("at_start_of"),
                offset=int(r.get("offset", 0)),
                sweep=bool(r.get("sweep", False)),
                focus=tuple(r.get("focus", []) or []),
                always=bool(r.get("always", False)),
                preview_tomorrow=bool(r.get("preview_tomorrow", False)),
                week_summary=bool(r.get("week_summary", False)),
            ))
        return tuple(out)

    by_day = {}
    for dkey in DAY_KEYS:
        if dkey in ck:
            by_day[DAY_KEYS.index(dkey)] = rules(ck[dkey], dkey)
    checkins = CheckinConfig(
        workday=rules(ck.get("workday"), "workday"),
        by_day=by_day,
        expire_after=int(ck.get("expire_after_minutes", 45)),
        stale_after=int(ck.get("stale_after_minutes", 120)),
        daily_cap=int(ck.get("daily_cap", 6)),
        sweep_days=int(ck.get("sweep_days", 2)),
        on_track_window=int(ck.get("on_track_window_minutes", 90)),
        day_close=_time(ck.get("day_close", "22:00"), "checkins.day_close", problems) or time(22),
    )

    if problems:
        raise TemplateError(problems)

    return Template(
        tz=tz,
        employment=employment,
        sleep=sleep,
        roles=roles,
        activities=activities,
        week=week,
        targets=targets,
        checkins=checkins,
        source_hash=hashlib.sha256(text.encode()).hexdigest()[:12],
    )
