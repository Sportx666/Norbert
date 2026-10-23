"""Use cases: what happens on each scheduler tick, button press, command and text message.

Transport-free: returns texts and keyboards; bot.py moves them to and from Telegram.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import store
from .db import Database
from .planner import in_quiet_window
from .render import (
    REASON_CODES,
    Keyboard,
    is_open,
    lower,
    parse_cb,
    progress_lines,
    render_checkin,
    render_today,
    render_tomorrow,
    render_week,
)
from .store import LedgerError, _ItemView, anchors_from_json
from .template import Template
from .timeutil import UTC, day_label, hhmm, local, minutes_between, monday_of

PHASE1_TEXT = ("I can't hold a conversation yet; that arrives in Phase 2. "
               "For now: the buttons on each check-in, or /today, /tomorrow, /week, /log, /undo.")


NO_WEEK = "There is no plan for this week: the template does not validate. Run `firstthings check-template`."


@dataclass
class Outgoing:
    checkpoint_id: int
    text: str
    keyboard: Keyboard
    shown_ids: list[int]


@dataclass
class Reply:
    text: str
    keyboard: Keyboard = field(default_factory=list)
    toast: str | None = None
    followup: str | None = None


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


# ---------------------------------------------------------------- tick

TEMPLATE_BROKEN = ("I can't build the week: the template does not validate.\n- {errors}\n\n"
                   "Fix config/week_template.yaml, run `firstthings check-template`, then restart me.")


def ensure_weeks(conn, t: Template, now: datetime) -> list[str]:
    """Create this week (and next week from Sunday noon). Returns template errors instead of raising."""
    lt = local(now, t.tz)
    monday = monday_of(lt.date())
    try:
        if not store.get_week(conn, monday):
            store.ensure_week(conn, t, monday, not_before=now)
        if lt.weekday() == 6 and lt.hour >= 12:
            store.ensure_week(conn, t, monday + timedelta(days=7))
    except store.PlanInvalid as e:  # raised before anything is written
        return e.errors
    return []


def prepare_tick(db: Database, t: Template, now: datetime) -> list[Outgoing]:
    """Housekeeping plus the check-ins that are due. The caller sends them, then calls after_send."""
    now = _utc(now)
    out: list[Outgoing] = []
    with db.tx() as conn:
        problems = ensure_weeks(conn, t, now)
        if problems and store.get_state(conn, "template_warned") != t.source_hash:
            store.set_state(conn, "template_warned", t.source_hash)
            out.append(Outgoing(0, TEMPLATE_BROKEN.format(errors="\n- ".join(problems[:8])), [], []))
        store.close_due_days(conn, t, now)
        store.expire_checkpoints(conn, t, now)
        sent_today: dict = {}
        for cp in store.due_checkpoints(conn, now):
            day = cp["day"]
            if day not in sent_today:
                sent_today[day] = store.sent_count(conn, day)
            result = compose(conn, t, cp, now, for_send=True, already_sent=sent_today[day])
            if isinstance(result, str):
                store.set_checkpoint_status(conn, cp["id"], "missed" if result == "stale" else "suppressed", result)
            else:
                sent_today[day] += 1
                out.append(result)
    return out


def after_send(db: Database, out: Outgoing, telegram_message_id: int | None, now: datetime) -> None:
    now = _utc(now)
    with db.tx() as conn:
        if out.checkpoint_id:
            store.mark_sent(conn, out.checkpoint_id, out.shown_ids, telegram_message_id, now)
        store.log_message(conn, "out", "checkin" if out.checkpoint_id else "reply", out.text, telegram_message_id,
                          out.checkpoint_id or None, now)


def _recently_on_track(item: dict, now: datetime, window: int) -> bool:
    return bool(item.get("on_track_at")) and minutes_between(item["on_track_at"], now) <= window


def compose(conn, t: Template, cp: dict, now: datetime, *, for_send: bool, already_sent: int = 0,
            expand: dict | None = None) -> Outgoing | str:
    """Build a check-in message. When for_send, may return a suppression reason instead."""
    day = cp["day"]
    if for_send:
        if minutes_between(cp["due_at"], now) > t.checkins.stale_after:
            return "stale"
        if already_sent >= t.checkins.daily_cap:
            return "cap"
        log = store.get_day_log(conn, day)
        if log:
            anchors = anchors_from_json(day, log["anchors"])
            views = [_ItemView(r) for r in store.items_for_day(conn, day) if r["status"] not in ("moved", "cancelled")]
            if in_quiet_window(t, anchors, views, now):
                return "quiet"
        sweep = store.unresolved_items(conn, now, day - timedelta(days=t.checkins.sweep_days)) if cp["sweep"] else []
        focus_all = [i for i in store.get_items(conn, cp["focus_item_ids"]) if i["status"] not in ("moved", "cancelled")]
        sweep_ids = {i["id"] for i in sweep}
        focus = [i for i in focus_all if i["id"] not in sweep_ids]
        actionable = [i for i in focus if is_open(i) and minutes_between(now, i["end_at"]) > 0
                      and not _recently_on_track(i, now, t.checkins.on_track_window)]
        if not cp["always_send"] and not sweep and not actionable:
            return "nothing_to_ask"
        if cp["kind"].startswith("pre_"):
            focus = actionable
    else:
        shown = store.get_items(conn, cp["shown_item_ids"])
        focus_ids = set(cp["focus_item_ids"] or [])
        sweep = [i for i in shown if i["id"] not in focus_ids]
        focus = [i for i in shown if i["id"] in focus_ids]

    shown_ids = [i["id"] for i in sweep] + [i["id"] for i in focus]
    later = []
    if not cp["preview_tomorrow"] and not cp["week_summary"] and not cp["kind"].startswith("pre_") and cp["kind"] != "log":
        after = max([i["start_at"] for i in focus] + [now], key=lambda d: d.astimezone(UTC))
        later = [i for i in store.items_for_day(conn, day)
                 if i["confirm"] == "ask" and i["id"] not in shown_ids and is_open(i)
                 and minutes_between(after, i["start_at"]) > 0]
    tomorrow = None
    if cp["preview_tomorrow"]:
        nxt = day + timedelta(days=1)
        nlog = store.get_day_log(conn, nxt)
        tlog = store.get_day_log(conn, day)
        if nlog and tlog:
            tomorrow = render_tomorrow(nxt, nlog["anchors"], store.items_for_day(conn, nxt), t.tz,
                                       datetime.fromisoformat(tlog["anchors"]["lights_out"]))
    summary = None
    if cp["week_summary"]:
        summary = progress_lines(store.week_progress(conn, cp["week_id"], now))
        open_n = store.week_counts(conn, cp["week_id"], now)["unresolved"]
        if open_n:
            summary += ["", f"{open_n} items are still unanswered: /log clears them before the review."]
    text, kb = render_checkin(kind=cp["kind"], cp_id=cp["id"], sweep=sweep, focus=focus, later=later,
                              tomorrow=tomorrow, summary=summary, now=now, tz=t.tz, today=day, expand=expand)
    return Outgoing(cp["id"], text, kb, shown_ids)


# ---------------------------------------------------------------- buttons and text

def handle_callback(db: Database, t: Template, data: str, now: datetime) -> Reply:
    now = _utc(now)
    cp_id, item_id, action, arg = parse_cb(data)
    with db.tx() as conn:
        store.log_message(conn, "in", "button", data, checkpoint_id=cp_id or None, now=now)
        cp = store.get_checkpoint(conn, cp_id)
        item = store.get_item(conn, item_id)
        name = item["title"]
        expand: dict[int, str] = {}
        toast = followup = None
        try:
            if action == "d":
                store.record_outcome(conn, item_id, "done", now=now)
                toast = f"{name}: done"
            elif action == "p":
                expand[item_id] = "partial"
            elif action == "m":
                store.record_outcome(conn, item_id, "partial", now=now, actual_minutes=int(arg))
                toast = f"{name}: {arg} min logged"
            elif action == "s":
                expand[item_id] = "skip"
            elif action == "r":
                category = REASON_CODES[arg]
                store.record_outcome(conn, item_id, "skipped", now=now, reason_category=category)
                toast = f"{name}: skipped"
                if category == "preference":  # decision 8: always ask why
                    store.set_state(conn, "awaiting_reason", {"item_id": item_id, "at": now.isoformat()})
                    followup = f"What's the reason for skipping {lower(name)}? One line is enough."
            elif action == "o":
                store.mark_on_track(conn, item_id, now)
                toast = f"{name}: on track"
            elif action == "b":
                pass
        except LedgerError as e:
            toast = str(e)
        if cp is None:
            return Reply(text="", toast=toast or "That message is out of date; try /log", followup=followup)
        if cp["created_by"] != "user":
            store.mark_answered(conn, cp_id, now)
        out = compose(conn, t, store.get_checkpoint(conn, cp_id), now, for_send=False, expand=expand)
        return Reply(text=out.text, keyboard=out.keyboard, toast=toast, followup=followup)


def handle_text(db: Database, t: Template, text: str, now: datetime) -> Reply:
    now = _utc(now)
    with db.tx() as conn:
        store.log_message(conn, "in", "text", text, now=now)
        waiting = store.get_state(conn, "awaiting_reason")
        if waiting and minutes_between(datetime.fromisoformat(waiting["at"]), now) <= 180:
            store.set_state(conn, "awaiting_reason", None)
            try:
                store.set_skip_reason_text(conn, waiting["item_id"], text, now)
            except LedgerError:
                return Reply(text=PHASE1_TEXT)
            return Reply(text="Noted. It goes in the ledger and comes up in Sunday's review.")
        return Reply(text=PHASE1_TEXT)


# ---------------------------------------------------------------- commands

def cmd_today(db: Database, t: Template, now: datetime, offset_days: int = 0) -> Reply:
    now = _utc(now)
    day = local(now, t.tz).date() + timedelta(days=offset_days)
    with db.tx() as conn:
        ensure_weeks(conn, t, now)
        log = store.get_day_log(conn, day)
        week = store.get_week(conn, monday_of(day))
        items = [i for i in store.items_for_day(conn, day) if i["status"] not in ("moved", "cancelled")]
        return Reply(text=render_today(day, log["anchors"] if log else None, items, now, t.tz,
                                       baseline=bool(week and week["baseline"])))


def cmd_week(db: Database, t: Template, now: datetime) -> Reply:
    now = _utc(now)
    with db.tx() as conn:
        ensure_weeks(conn, t, now)
        week = store.get_week(conn, monday_of(local(now, t.tz).date()))
        if week is None:
            return Reply(text=NO_WEEK)
        return Reply(text=render_week(week, store.week_progress(conn, week["id"], now), store.role_time(conn, week["id"]),
                                      store.week_counts(conn, week["id"], now), store.skip_reasons(conn, week["id"])))


def cmd_log(db: Database, t: Template, now: datetime) -> Reply:
    now = _utc(now)
    with db.tx() as conn:
        ensure_weeks(conn, t, now)
        today = local(now, t.tz).date()
        week = store.get_week(conn, monday_of(today))
        if week is None:
            return Reply(text=NO_WEEK)
        items = store.unresolved_items(conn, now, monday_of(today), limit=10)
        if not items:
            return Reply(text="Nothing open. The ledger is up to date.")
        cp_id = store.create_manual_view(conn, week["id"], today, [i["id"] for i in items], now)
        out = compose(conn, t, store.get_checkpoint(conn, cp_id), now, for_send=False)
        return Reply(text=out.text, keyboard=out.keyboard)


def cmd_undo(db: Database, t: Template, now: datetime) -> Reply:
    now = _utc(now)
    with db.tx() as conn:
        item = store.undo_last(conn, now)
        if not item:
            return Reply(text="Nothing to undo.")
        state = "open again" if item["status"] in ("planned", "unconfirmed") else item["status"]
        return Reply(text=f"Undone. {item['title']} ({item['day']:%a %d %b}) is {state}.")


# ---------------------------------------------------------------- Phase 1 gate

GATE_WEEKDAYS = 3
LATE_AFTER = 5  # minutes from due to sent before a check-in counts as late


def _gate_checkin(cp: dict, now: datetime, t: Template) -> tuple[str, bool]:
    """One check-in as text, and whether it fails the gate."""
    head = f"{hhmm(cp['due_at'], t.tz)} {cp['kind']}"
    if cp["status"] in ("sent", "answered", "expired"):
        late = minutes_between(cp["due_at"], cp["sent_at"])
        if late > LATE_AFTER:
            return f"{head} sent {hhmm(cp['sent_at'], t.tz)}, {late} min late", True
        return f"{head} sent", False
    if cp["status"] == "suppressed":
        return f"{head} not needed ({cp['suppression_reason']})", False
    if cp["status"] == "missed":
        return f"{head} MISSED", True
    if minutes_between(cp["due_at"], now) > LATE_AFTER:  # still scheduled: the bot was not running
        return f"{head} NOT SENT", True
    return f"{head} due", False


def gate_report(db: Database, t: Template, now: datetime, start, days: int) -> tuple[str, bool]:
    """Day by day: is every item answered, and did every check-in go out on time?"""
    now = _utc(now)
    lines = [f"Phase 1 gate, {day_label(start)} to {day_label(start + timedelta(days=days - 1))}", ""]
    complete = 0
    with db.tx() as conn:
        for n in range(days):
            day = start + timedelta(days=n)
            log = store.get_day_log(conn, day)
            items = [i for i in store.items_for_day(conn, day) if i["status"] not in ("moved", "cancelled")]
            if log is None or not items:
                lines.append(f"{day_label(day)}  nothing tracked")
                continue
            checkins = [_gate_checkin(cp, now, t) for cp in store.checkpoints_for_day(conn, day)]
            late = any(bad for _, bad in checkins)
            unanswered = [i for i in items if i["status"] in store.OPEN]
            if log["closed_at"] is None:
                head = "in progress"
            elif unanswered:
                head = f"{len(unanswered)} of {len(items)} unanswered: " + ", ".join(
                    f"{i['title']} {hhmm(i['start_at'], t.tz)}" for i in unanswered)
            else:
                head = f"all {len(items)} items answered"
            if t.is_workday(day.weekday()) and log["closed_at"] and not unanswered and not late:
                complete += 1
                head = "✓ " + head
            lines.append(f"{day_label(day)}  {head}")
            if checkins:
                lines.append("    " + " · ".join(text for text, _ in checkins))
    passed = complete >= GATE_WEEKDAYS
    lines += ["", f"{min(complete, GATE_WEEKDAYS)} of {GATE_WEEKDAYS} weekdays complete"
              + (": gate passed." if passed else "."),
              "A weekday counts once every item has an answer (the evening reading is asked the next morning) "
              f"and every check-in went out within {LATE_AFTER} minutes."]
    return "\n".join(lines), passed
