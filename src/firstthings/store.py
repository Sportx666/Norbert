"""Ledger persistence and the deterministic operations on it.

Every write to plan state goes through a function here; the LLM (Phase 2) will call these
through tools and never write SQL itself.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from psycopg import Connection
from psycopg.types.json import Jsonb

from .planner import DayAnchors, build_week, resolve_checkins, validate_week
from .template import Template
from .timeutil import local, minutes_between

OPEN = ("planned", "unconfirmed")
RESOLVED = ("done", "partial", "skipped")
TRACKED = ("status", "actual_minutes", "resolved_at", "on_track_at", "skip_reason_category", "skip_reason_text")
REASONS = ("constraint", "capacity", "preference", "reprioritised")


class PlanInvalid(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class LedgerError(ValueError):
    pass


# ---------------------------------------------------------------- catalog

def sync_catalog(conn: Connection, t: Template) -> None:
    for i, (key, name) in enumerate(t.roles.items()):
        conn.execute(
            "insert into ft.roles (key, name, sort) values (%s, %s, %s) "
            "on conflict (key) do update set name = excluded.name, sort = excluded.sort",
            (key, name, i),
        )
    for a in t.activities.values():
        conn.execute(
            """insert into ft.activity_types
               (key, name, role_key, default_minutes, min_minutes, unit, flexibility, quadrant,
                protected, quiet, confirm, is_sleep)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (key) do update set
                 name = excluded.name, role_key = excluded.role_key, default_minutes = excluded.default_minutes,
                 min_minutes = excluded.min_minutes, unit = excluded.unit, flexibility = excluded.flexibility,
                 quadrant = excluded.quadrant, protected = excluded.protected, quiet = excluded.quiet,
                 confirm = excluded.confirm, is_sleep = excluded.is_sleep""",
            (a.key, a.name, a.role, a.minutes, a.min_minutes, a.unit, a.flexibility, a.quadrant,
             a.protected, a.quiet, a.confirm, a.sleep),
        )


# ---------------------------------------------------------------- weeks

def get_week(conn: Connection, monday: date) -> dict | None:
    return conn.execute("select * from ft.weeks where start_date = %s", (monday,)).fetchone()


def ensure_week(conn: Connection, t: Template, monday: date, *, not_before: datetime | None = None,
                baseline: bool = True) -> int:
    """Create the week from the template if it does not exist. Idempotent.

    not_before: on a mid-week first start, items and check-ins before this instant are not created.
    """
    existing = get_week(conn, monday)
    if existing:
        return existing["id"]
    plan = build_week(t, monday)
    v = validate_week(t, plan)
    if not v.ok:
        raise PlanInvalid(v.errors)
    sync_catalog(conn, t)
    week_id = conn.execute(
        "insert into ft.weeks (start_date, baseline, sleep_target_minutes, template_hash) "
        "values (%s, %s, %s, %s) returning id",
        (monday, baseline, t.sleep.target_minutes, t.source_hash),
    ).fetchone()["id"]
    _upsert_targets(conn, t, week_id, baseline)
    for d in plan.days:
        conn.execute(
            "insert into ft.day_logs (day, week_id, anchors) values (%s, %s, %s) on conflict (day) do nothing",
            (d.day, week_id, Jsonb(d.anchors.to_json())),
        )
    _insert_plan(conn, t, week_id, plan, not_before, origin="template")
    return week_id


def _upsert_targets(conn, t: Template, week_id: int, provisional: bool) -> None:
    # priority = position in the template's targets list (first = most important); also the display order.
    for n, tg in enumerate(t.targets.values(), start=1):
        conn.execute(
            "insert into ft.weekly_targets (week_id, activity_key, floor, goal, provisional, priority) "
            "values (%s, %s, %s, %s, %s, %s) on conflict (week_id, activity_key) do update set "
            "floor = excluded.floor, goal = excluded.goal, priority = excluded.priority",
            (week_id, tg.activity, tg.floor, tg.goal, provisional, n),
        )


def _insert_plan(conn, t: Template, week_id: int, plan, not_before: datetime | None, origin: str,
                 change_id: int | None = None, checkpoints: bool = True) -> int:
    created = 0
    for d in plan.days:
        for item in d.items:
            if not_before and item.start <= not_before:
                continue
            act = t.activities[item.activity]
            item.id = conn.execute(
                """insert into ft.plan_items
                   (week_id, day, activity_key, role_key, title, start_at, end_at, planned_minutes, quadrant,
                    origin, change_id)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
                (week_id, d.day, item.activity, act.role, item.title, item.start, item.end, item.minutes,
                 item.quadrant, origin, change_id),
            ).fetchone()["id"]
            created += 1
    if not checkpoints:
        return created
    by_day = {d.day: d for d in plan.days}
    for c in plan.checkpoints:
        if not_before and c.due <= not_before:
            continue
        focus = [i.id for i in by_day[c.day].items if i.activity in c.focus and i.id is not None]
        _insert_checkpoint(conn, week_id, c, focus)
    return created


def rebuild_week_from_template(conn: Connection, t: Template, monday: date, now: datetime) -> dict:
    """Apply an edited template to the rest of a week that already exists.

    Everything already answered, on track, or in the past stays as it is. Future open items are
    cancelled (kept for history, excluded from progress) and recreated from the template.
    """
    week = get_week(conn, monday)
    if not week:
        ensure_week(conn, t, monday, not_before=now)
        return {"cancelled": 0, "created": None}
    plan = build_week(t, monday)
    v = validate_week(t, plan)
    if not v.ok:
        raise PlanInvalid(v.errors)
    sync_catalog(conn, t)
    change_id = conn.execute(
        "insert into ft.plan_changes (week_id, operations, rationale, status, applied_at) "
        "values (%s, %s, 'week template edited', 'applied', %s) returning id",
        (week["id"], Jsonb([{"op": "rebuild_from_template", "template_hash": t.source_hash}]), now),
    ).fetchone()["id"]
    cancelled = conn.execute(
        "update ft.plan_items set status = 'cancelled', change_id = %s "
        "where week_id = %s and status = 'planned' and on_track_at is null and start_at > %s",
        (change_id, week["id"], now),
    ).rowcount
    conn.execute(
        "update ft.checkpoints set status = 'cancelled' "
        "where week_id = %s and status = 'scheduled' and created_by = 'rule' and due_at > %s",
        (week["id"], now),
    )
    kept = conn.execute(
        "select day, activity_key from ft.plan_items where week_id = %s and status <> 'cancelled' and start_at > %s",
        (week["id"], now),
    ).fetchall()
    kept_keys = {(r["day"], r["activity_key"]) for r in kept}
    for d in plan.days:
        d.items = [i for i in d.items if (i.day, i.activity) not in kept_keys]
        conn.execute("update ft.day_logs set anchors = %s where day = %s and closed_at is null",
                     (Jsonb(d.anchors.to_json()), d.day))
    _upsert_targets(conn, t, week["id"], week["baseline"])
    conn.execute("update ft.weeks set template_hash = %s where id = %s", (t.source_hash, week["id"]))
    created = _insert_plan(conn, t, week["id"], plan, now, origin="template", change_id=change_id, checkpoints=False)
    for d in plan.days:
        rebuild_checkpoints(conn, t, d.day, now)
    return {"cancelled": cancelled, "created": created}


def _insert_checkpoint(conn, week_id, spec, focus_ids, created_by="rule"):
    conn.execute(
        """insert into ft.checkpoints
           (week_id, day, kind, due_at, focus_item_ids, sweep, always_send, preview_tomorrow, week_summary, created_by)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (week_id, spec.day, spec.kind, spec.due, focus_ids, spec.sweep, spec.always,
         spec.preview_tomorrow, spec.week_summary, created_by),
    )


def rebuild_checkpoints(conn: Connection, t: Template, day: date, now: datetime) -> int:
    """Regenerate the rest of a day's rule check-ins from its *current* items (used after replans)."""
    log = get_day_log(conn, day)
    if not log:
        return 0
    conn.execute(
        "delete from ft.checkpoints where day = %s and status = 'scheduled' and created_by = 'rule' and due_at > %s",
        (day, now),
    )
    items = [_ItemView(r) for r in items_for_day(conn, day) if r["status"] not in ("moved", "cancelled")]
    specs = resolve_checkins(t, anchors_from_json(day, log["anchors"]), items)
    n = 0
    for s in specs:
        if s.due <= now:
            continue
        _insert_checkpoint(conn, log["week_id"], s, [i.id for i in items if i.activity in s.focus])
        n += 1
    return n


class _ItemView:
    def __init__(self, row):
        self.id = row["id"]
        self.activity = row["activity_key"]
        self.start = row["start_at"]
        self.end = row["end_at"]


def anchors_from_json(day: date, a: dict) -> DayAnchors:
    def dt(v):
        return datetime.fromisoformat(v) if v else None
    return DayAnchors(
        day=day, workday=a["workday"], work_start=dt(a["work_start"]), work_end=dt(a["work_end"]),
        wake=dt(a["wake"]), lights_out=dt(a["lights_out"]), prev_lights_out=dt(a["prev_lights_out"]),
        night_minutes=a["night_minutes"], nap_minutes=a["nap_minutes"],
    )


def get_day_log(conn: Connection, day: date) -> dict | None:
    return conn.execute("select * from ft.day_logs where day = %s", (day,)).fetchone()


# ---------------------------------------------------------------- items

ITEM_SELECT = """
select i.*, a.unit, a.confirm, a.quiet, a.protected, a.flexibility, a.min_minutes, a.is_sleep
from ft.plan_items i join ft.activity_types a on a.key = i.activity_key
"""


def items_for_day(conn: Connection, day: date) -> list[dict]:
    return conn.execute(ITEM_SELECT + " where i.day = %s order by i.start_at, i.id", (day,)).fetchall()


def get_item(conn: Connection, item_id: int) -> dict:
    row = conn.execute(ITEM_SELECT + " where i.id = %s", (item_id,)).fetchone()
    if not row:
        raise LedgerError(f"no item {item_id}")
    return row


def get_items(conn: Connection, ids) -> list[dict]:
    ids = list(ids or [])
    if not ids:
        return []
    return conn.execute(ITEM_SELECT + " where i.id = any(%s) order by i.start_at, i.id", (ids,)).fetchall()


def unresolved_items(conn: Connection, now: datetime, since_day: date, limit: int = 8) -> list[dict]:
    """Past items that need an answer: open status, confirm=ask, already ended."""
    return conn.execute(
        ITEM_SELECT + """ where i.status in ('planned', 'unconfirmed') and a.confirm = 'ask'
                          and i.end_at <= %s and i.day >= %s
                          order by i.start_at, i.id limit %s""",
        (now, since_day, limit),
    ).fetchall()


def _snapshot(row: dict) -> dict:
    out = {}
    for k in TRACKED:
        v = row.get(k)
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _apply(conn: Connection, item_id: int, changes: dict, source: str, now: datetime) -> dict:
    before = get_item(conn, item_id)
    sets = ", ".join(f"{k} = %s" for k in changes)
    conn.execute(f"update ft.plan_items set {sets} where id = %s", (*changes.values(), item_id))
    after = get_item(conn, item_id)
    conn.execute(
        "insert into ft.item_events (item_id, at, source, before, after) values (%s, %s, %s, %s, %s)",
        (item_id, now, source, Jsonb(_snapshot(before)), Jsonb(_snapshot(after))),
    )
    return after


def record_outcome(conn: Connection, item_id: int, status: str, *, now: datetime,
                   actual_minutes: int | None = None, reason_category: str | None = None,
                   reason_text: str | None = None, source: str = "button") -> dict:
    item = get_item(conn, item_id)
    if item["status"] in ("moved", "cancelled"):
        raise LedgerError(f"item {item_id} is {item['status']}")
    planned = item["planned_minutes"]
    if status == "done":
        actual = planned if actual_minutes is None else actual_minutes
    elif status == "partial":
        if actual_minutes is None or not (0 < actual_minutes < planned):
            raise LedgerError(f"partial needs minutes between 1 and {planned - 1}")
        actual = actual_minutes
    elif status == "skipped":
        if reason_category is not None and reason_category not in REASONS:
            raise LedgerError(f"unknown reason {reason_category!r}")
        actual = 0
    else:
        raise LedgerError(f"cannot record status {status!r}")
    if actual is not None and not (0 <= actual <= planned * 3):
        raise LedgerError("implausible minutes")
    changes = {
        "status": status,
        "actual_minutes": actual,
        "resolved_at": now,
        "skip_reason_category": reason_category if status == "skipped" else None,
        "skip_reason_text": reason_text if status == "skipped" else None,
    }
    return _apply(conn, item_id, changes, source, now)


def mark_on_track(conn: Connection, item_id: int, now: datetime, source: str = "button") -> dict:
    item = get_item(conn, item_id)
    if item["status"] not in OPEN:
        raise LedgerError(f"item {item_id} is already {item['status']}")
    return _apply(conn, item_id, {"on_track_at": now}, source, now)


def set_skip_reason_text(conn: Connection, item_id: int, text: str, now: datetime) -> dict:
    item = get_item(conn, item_id)
    if item["status"] != "skipped":
        raise LedgerError("only skipped items take a reason")
    return _apply(conn, item_id, {"skip_reason_text": text.strip()[:500]}, "conversation", now)


def undo_last(conn: Connection, now: datetime) -> dict | None:
    """Revert the most recent user-made change. Returns the item as restored, or None."""
    ev = conn.execute(
        "select * from ft.item_events where undone_at is null and source <> 'day_close' order by id desc limit 1"
    ).fetchone()
    if not ev:
        return None
    restore = {}
    for k in TRACKED:
        v = ev["before"].get(k)
        if k in ("resolved_at", "on_track_at") and v:
            v = datetime.fromisoformat(v)
        restore[k] = v
    sets = ", ".join(f"{k} = %s" for k in restore)
    conn.execute(f"update ft.plan_items set {sets} where id = %s", (*restore.values(), ev["item_id"]))
    conn.execute("update ft.item_events set undone_at = %s where id = %s", (now, ev["id"]))
    item = get_item(conn, ev["item_id"])
    item["undone_from"] = ev["after"]
    return item


# ---------------------------------------------------------------- check-ins

def due_checkpoints(conn: Connection, now: datetime) -> list[dict]:
    return conn.execute(
        "select * from ft.checkpoints where status = 'scheduled' and due_at <= %s order by due_at, id", (now,)
    ).fetchall()


def get_checkpoint(conn: Connection, cp_id: int) -> dict | None:
    return conn.execute("select * from ft.checkpoints where id = %s", (cp_id,)).fetchone()


def set_checkpoint_status(conn: Connection, cp_id: int, status: str, reason: str | None = None) -> None:
    conn.execute("update ft.checkpoints set status = %s, suppression_reason = %s where id = %s", (status, reason, cp_id))


def mark_sent(conn: Connection, cp_id: int, shown_ids, telegram_message_id: int | None, now: datetime) -> None:
    conn.execute(
        "update ft.checkpoints set status = 'sent', sent_at = %s, shown_item_ids = %s, telegram_message_id = %s where id = %s",
        (now, list(shown_ids), telegram_message_id, cp_id),
    )


def mark_answered(conn: Connection, cp_id: int, now: datetime) -> None:
    conn.execute(
        "update ft.checkpoints set status = 'answered', answered_at = coalesce(answered_at, %s) "
        "where id = %s and status in ('sent', 'expired', 'answered')",
        (now, cp_id),
    )


def create_manual_view(conn: Connection, week_id: int, day: date, shown_ids, now: datetime) -> int:
    """A /log message is stored like a check-in so its buttons re-render the same way."""
    return conn.execute(
        """insert into ft.checkpoints (week_id, day, kind, due_at, shown_item_ids, status, created_by, sent_at)
           values (%s, %s, 'log', %s, %s, 'sent', 'user', %s) returning id""",
        (week_id, day, now, list(shown_ids), now),
    ).fetchone()["id"]


def sent_count(conn: Connection, day: date) -> int:
    return conn.execute(
        "select count(*) as n from ft.checkpoints where day = %s and created_by <> 'user' "
        "and status in ('sent', 'answered', 'expired')",
        (day,),
    ).fetchone()["n"]


def expire_checkpoints(conn: Connection, t: Template, now: datetime) -> int:
    cur = conn.execute(
        "update ft.checkpoints set status = 'expired' where status = 'sent' and created_by <> 'user' "
        "and answered_at is null and sent_at <= %s",
        (now - timedelta(minutes=t.checkins.expire_after),),
    )
    return cur.rowcount


# ---------------------------------------------------------------- day close

def close_due_days(conn: Connection, t: Template, now: datetime) -> list[date]:
    """At day_close (22:00): family 'auto' items become done, unanswered 'ask' items unconfirmed.

    Nothing is ever assumed done or missed except items the template marks confirm: auto.
    """
    today = local(now, t.tz).date()
    closed = []
    rows = conn.execute("select day from ft.day_logs where closed_at is null and day <= %s order by day", (today,)).fetchall()
    for r in rows:
        day = r["day"]
        close_at = datetime.combine(day, t.checkins.day_close, tzinfo=t.tz)
        if minutes_between(close_at, now) < 0:
            continue
        for item in items_for_day(conn, day):
            if item["status"] != "planned":
                continue
            if item["confirm"] == "auto":
                _apply(conn, item["id"], {"status": "done", "actual_minutes": item["planned_minutes"],
                                          "resolved_at": now}, "day_close", now)
            elif item["end_at"] <= now:
                _apply(conn, item["id"], {"status": "unconfirmed"}, "day_close", now)
        conn.execute("update ft.day_logs set closed_at = %s where day = %s", (now, day))
        closed.append(day)
    return closed


# ---------------------------------------------------------------- progress

def week_progress(conn: Connection, week_id: int, now: datetime) -> list[dict]:
    """Target progress, with open items split into still-ahead and already-past-but-unanswered."""
    rows = conn.execute(
        """select p.*, t.priority from ft.week_progress p
           join ft.weekly_targets t on t.week_id = p.week_id and t.activity_key = p.activity_key
           where p.week_id = %s order by t.priority, p.activity_key""",
        (week_id,),
    ).fetchall()
    split = {r["activity_key"]: r for r in conn.execute(
        """select i.activity_key,
                  coalesce(sum(case when i.end_at > %s
                                    then case when a.unit = 'minutes' then i.planned_minutes else 1 end end), 0)::int as ahead,
                  coalesce(sum(case when i.end_at <= %s
                                    then case when a.unit = 'minutes' then i.planned_minutes else 1 end end), 0)::int as unanswered_amount,
                  count(*) filter (where i.end_at <= %s)::int as unanswered
           from ft.plan_items i join ft.activity_types a on a.key = i.activity_key
           where i.week_id = %s and i.status in ('planned', 'unconfirmed')
           group by i.activity_key""",
        (now, now, now, week_id),
    ).fetchall()}
    for r in rows:
        sp = split.get(r["activity_key"], {})
        r["ahead"] = sp.get("ahead", 0)
        r["unanswered"] = sp.get("unanswered", 0)
        r["unanswered_amount"] = sp.get("unanswered_amount", 0)
    return rows


def role_time(conn: Connection, week_id: int) -> list[dict]:
    return conn.execute("select * from ft.role_time where week_id = %s order by sort", (week_id,)).fetchall()


def week_counts(conn: Connection, week_id: int, now: datetime) -> dict:
    return conn.execute(
        """select count(*) filter (where i.status = 'skipped')::int as skipped,
                  count(*) filter (where i.status = 'partial')::int as partial,
                  count(*) filter (where i.status in ('planned', 'unconfirmed') and a.confirm = 'ask'
                                   and i.end_at <= %s)::int as unresolved
           from ft.plan_items i join ft.activity_types a on a.key = i.activity_key
           where i.week_id = %s""",
        (now, week_id),
    ).fetchone()


def skip_reasons(conn: Connection, week_id: int) -> list[dict]:
    return conn.execute(
        "select skip_reason_category as reason, count(*)::int as n from ft.plan_items "
        "where week_id = %s and status = 'skipped' group by 1 order by 2 desc",
        (week_id,),
    ).fetchall()


# ---------------------------------------------------------------- misc state

def log_message(conn: Connection, direction: str, kind: str, text: str | None,
                telegram_message_id: int | None = None, checkpoint_id: int | None = None, now: datetime | None = None):
    conn.execute(
        "insert into ft.messages (at, direction, kind, text, telegram_message_id, checkpoint_id) "
        "values (coalesce(%s, now()), %s, %s, %s, %s, %s)",
        (now, direction, kind, text, telegram_message_id, checkpoint_id),
    )


def get_state(conn: Connection, key: str):
    row = conn.execute("select value from ft.bot_state where key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: Connection, key: str, value) -> None:
    if value is None:
        conn.execute("delete from ft.bot_state where key = %s", (key,))
        return
    conn.execute(
        "insert into ft.bot_state (key, value, updated_at) values (%s, %s, now()) "
        "on conflict (key) do update set value = excluded.value, updated_at = now()",
        (key, Jsonb(value)),
    )
