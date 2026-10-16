"""Plain-text messages and inline keyboards. Pure functions over rows from store.py.

Phase 1 wording is template-based; Phase 2 hands composition to Claude with these as the fallback.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .timeutil import day_label, fmt_minutes, hhmm, local, minutes_between

Keyboard = list[list[tuple[str, str]]]

REASON_CODES = {"c": "constraint", "k": "capacity", "p": "preference", "r": "reprioritised"}
REASON_LABELS = {"constraint": "can't", "capacity": "too tired", "preference": "not feeling it",
                 "reprioritised": "something better"}

HEADERS = {
    "post_work": "Work's done.",
    "transition": "Time to switch.",
    "shutdown": "Before dinner: let's close the day.",
    "saturday": "Saturday check-in.",
    "weekly_review": "Weekly review time. Here's the week as the ledger has it.",
    "log": "Still open this week.",
}


def cb(cp_id: int, item_id: int, action: str, arg: str = "") -> str:
    data = f"{cp_id}|{item_id}|{action}|{arg}"
    assert len(data.encode()) <= 64, data
    return data


def parse_cb(data: str) -> tuple[int, int, str, str]:
    cp, item, action, arg = data.split("|", 3)
    return int(cp), int(item), action, arg


def short(title: str) -> str:
    return title if len(title) <= 14 else title.split()[0]


def lower(name: str) -> str:
    return name if name.isupper() else name.lower()


def partial_options(planned: int) -> list[int]:
    opts = [c for c in (15, 30, 45, 60, 90, 120, 150, 180) if c < planned]
    if planned >= 120:
        opts = [c for c in opts if c not in (15, 45)]
    return opts[-5:]


def _span(item: dict, tz: ZoneInfo) -> str:
    if item["unit"] == "count" or item["planned_minutes"] <= 15:
        return hhmm(item["start_at"], tz)
    return f"{hhmm(item['start_at'], tz)}–{hhmm(item['end_at'], tz)}"


def is_open(item: dict) -> bool:
    return item["status"] in ("planned", "unconfirmed")


def is_past(item: dict, now: datetime) -> bool:
    return minutes_between(item["end_at"], now) >= 0


def mark(item: dict, now: datetime) -> str:
    s = item["status"]
    if s == "done":
        return "✓"
    if s == "partial":
        return "½"
    if s == "skipped":
        return "✗"
    if s == "moved":
        return "→"
    if s == "cancelled":
        return "–"
    if item.get("on_track_at") and not is_past(item, now):
        return "▶"
    if is_past(item, now):
        return "?"
    return "•"


def item_line(item: dict, now: datetime, tz: ZoneInfo, today: date | None = None) -> str:
    line = f"{mark(item, now)} {item['title']} {_span(item, tz)}"
    if today and item["day"] != today:
        line += f" ({day_label(item['day']).split()[0]})"
    if item["status"] == "partial":
        line += f" · {fmt_minutes(item['actual_minutes'])} of {fmt_minutes(item['planned_minutes'])}"
    if item["status"] == "skipped" and item.get("skip_reason_category"):
        line += f" · {REASON_LABELS[item['skip_reason_category']]}"
    if item["status"] == "planned" and item.get("on_track_at") and not is_past(item, now):
        line += " · on track"
    return line


def item_buttons(cp_id: int, item: dict, now: datetime, expand: str | None = None) -> Keyboard:
    if not is_open(item):
        return []
    iid = item["id"]
    name = short(item["title"])
    if expand == "partial":
        row = [(f"{m}m", cb(cp_id, iid, "m", str(m))) for m in partial_options(item["planned_minutes"])]
        return [row, [("Back", cb(cp_id, iid, "b"))]]
    if expand == "skip":
        return [
            [("Can't", cb(cp_id, iid, "r", "c")), ("Too tired", cb(cp_id, iid, "r", "k"))],
            [("Not feeling it", cb(cp_id, iid, "r", "p")), ("Something better", cb(cp_id, iid, "r", "r"))],
            [("Back", cb(cp_id, iid, "b"))],
        ]
    if is_past(item, now):
        row = [(f"✓ {name}", cb(cp_id, iid, "d"))]
        if item["unit"] in ("minutes", "session") and item["planned_minutes"] >= 30:
            row.append(("Partial", cb(cp_id, iid, "p")))
        row.append(("Skipped", cb(cp_id, iid, "s")))
        return [row]
    if item.get("on_track_at"):
        return []
    return [[(f"▶ {name} on track", cb(cp_id, iid, "o")), (f"Skip {lower(name)}", cb(cp_id, iid, "s"))]]


def render_checkin(*, kind: str, cp_id: int, sweep: list[dict], focus: list[dict], later: list[dict],
                   tomorrow: str | None, summary: list[str] | None, now: datetime, tz: ZoneInfo,
                   today: date, expand: dict[int, str] | None = None) -> tuple[str, Keyboard]:
    expand = expand or {}
    header = HEADERS.get(kind)
    if header is None and kind.startswith("pre_") and focus:
        f = focus[0]
        mins = minutes_between(now, f["start_at"])
        when = f"in {mins} min" if mins > 0 else "now"
        header = f"{f['title']} starts {when}: {_span(f, tz)}, {fmt_minutes(f['planned_minutes'])} planned."
    lines = [header or "Check-in."]
    if summary:
        lines += [""] + summary
    if sweep:
        lines += ["", "Earlier"] + [item_line(i, now, tz, today) for i in sweep]
    if focus and not kind.startswith("pre_"):
        lines += ["", "Next"] + [item_line(i, now, tz, today) for i in focus]
    if later:
        lines += ["", "Later: " + " · ".join(f"{short(i['title'])} {hhmm(i['start_at'], tz)}" for i in later)]
    if tomorrow:
        lines += ["", tomorrow]
    kb: Keyboard = []
    seen = set()
    for i in list(sweep) + list(focus):
        if i["id"] in seen:
            continue
        seen.add(i["id"])
        kb += item_buttons(cp_id, i, now, expand.get(i["id"]))
    return "\n".join(lines), kb


def render_tomorrow(day: date, anchors: dict, items: list[dict], tz: ZoneInfo, tonight_lights_out: datetime) -> str:
    parts = []
    if anchors.get("wake"):
        parts.append(f"wake {hhmm(datetime.fromisoformat(anchors['wake']), tz)}")
    parts += [f"{short(i['title'])} {hhmm(i['start_at'], tz)}" for i in items if i["confirm"] == "ask"]
    return f"Tomorrow ({day_label(day)}): " + " · ".join(parts) + f". Lights out tonight {hhmm(tonight_lights_out, tz)}."


def render_today(day: date, anchors: dict | None, items: list[dict], now: datetime, tz: ZoneInfo,
                 baseline: bool = False) -> str:
    if anchors is None:
        return f"{day_label(day)}: no plan yet."
    head = day_label(day) + (" · baseline week" if baseline else "")
    a = {k: datetime.fromisoformat(v) for k, v in anchors.items() if isinstance(v, str)}
    facts = [f"Wake {hhmm(a['wake'], tz)}"]
    if anchors.get("workday"):
        facts.append(f"work {hhmm(a['work_start'], tz)}–{hhmm(a['work_end'], tz)}")
    facts.append(f"lights out {hhmm(a['lights_out'], tz)}")
    sleep = f"Sleep {fmt_minutes(anchors['night_minutes'])} night"
    if anchors["nap_minutes"]:
        sleep += f" + {fmt_minutes(anchors['nap_minutes'])} nap"
    lines = [head, " · ".join(facts), sleep, ""]
    for i in items:
        lines.append(item_line(i, now, tz))
    if not items:
        lines.append("Nothing planned.")
    return "\n".join(lines)


def _amount(unit: str, n: int) -> str:
    if unit != "minutes":
        return str(n)
    return fmt_minutes(n) if n else "0h"


def progress_lines(progress: list[dict]) -> list[str]:
    lines = []
    for p in progress:
        unit = p["unit"]
        unit_word = " sessions" if unit == "session" else ""
        line = f"{p['name']}: {_amount(unit, p['done'])} of {_amount(unit, p['floor'])}{unit_word} (goal {_amount(unit, p['goal'])})"
        parts = []
        if p["done"] >= p["floor"]:
            parts.append("floor met")
        if p.get("ahead"):
            parts.append(f"{_amount(unit, p['ahead'])} planned ahead")
        if p.get("unanswered"):
            parts.append(f"{p['unanswered']} unanswered")
        if p["done"] + p.get("ahead", 0) + p.get("unanswered_amount", 0) < p["floor"]:
            parts.append("floor out of reach")
        lines.append(line + "".join(f" · {x}" for x in parts))
    return lines


def render_week(week: dict, progress: list[dict], roles: list[dict], counts: dict, reasons: list[dict]) -> str:
    head = f"Week of {day_label(week['start_date'])}"
    if week["baseline"]:
        head += " · baseline, targets provisional"
    lines = [head, ""] + progress_lines(progress)
    worked = [r for r in roles if r["actual_minutes"]]
    if worked:
        lines += ["", "Actual time by role: " + " · ".join(f"{r['name']} {fmt_minutes(r['actual_minutes'])}" for r in worked)]
    tail = [f"Skipped {counts['skipped']}" if counts["skipped"] else "Nothing skipped"]
    if reasons and counts["skipped"]:
        tail[0] += " (" + ", ".join(f"{r['n']} {REASON_LABELS.get(r['reason'], 'no reason')}" for r in reasons) + ")"
    if counts["unresolved"]:
        tail.append(f"{counts['unresolved']} still unanswered: /log")
    lines += ["", " · ".join(tail)]
    return "\n".join(lines)


def localday(now: datetime, tz: ZoneInfo) -> date:
    return local(now, tz).date()
