"""Integration tests against a real Postgres (TEST_DATABASE_URL)."""
from datetime import date, datetime, time, timedelta

import pytest

from firstthings import engine, store
from firstthings.db import migrate
from firstthings.planner import build_week
from firstthings.render import parse_cb
from firstthings.store import LedgerError, PlanInvalid
from firstthings.timeutil import ROME, hhmm, shift

MON = date(2026, 10, 26)  # first Monday on Italian winter time


def rome(day, hh, mm=0):
    return datetime.combine(day, time(hh, mm), tzinfo=ROME)


def make_week(db, t, monday=MON, not_before=None):
    with db.tx() as conn:
        return store.ensure_week(conn, t, monday, not_before=not_before)


def item(db, day, activity):
    with db.tx() as conn:
        return next(i for i in store.items_for_day(conn, day) if i["activity_key"] == activity)


def run(db, t, start, end, step=5):
    """Tick from start to end; return [(local HH:MM, Outgoing)] for everything sent."""
    sent, now = [], start
    while now <= end:
        for out in engine.prepare_tick(db, t, now):
            engine.after_send(db, out, 1000 + out.checkpoint_id, now)
            sent.append((hhmm(now), out))
        now = shift(now, step)
    return sent


def press(db, t, out, label, now):
    data = next(d for row in out.keyboard for (lbl, d) in row if lbl == label)
    return engine.handle_callback(db, t, data, now)


def press_in_row(db, t, out, row_label, label, now):
    row = next(r for r in out.keyboard if r[0][0] == row_label)
    data = next(d for (lbl, d) in row if lbl == label)
    return engine.handle_callback(db, t, data, now)


def buttons(out_or_reply):
    return [lbl for row in out_or_reply.keyboard for (lbl, _) in row]


# ---------------------------------------------------------------- schema and weeks

def test_migrations_are_idempotent(db):
    with db.tx() as conn:
        assert migrate(conn) == []


def test_week_instantiation_is_idempotent(db, t):
    a = make_week(db, t)
    b = make_week(db, t)
    assert a == b
    with db.tx() as conn:
        n_items = conn.execute("select count(*) n from ft.plan_items").fetchone()["n"]
        n_cp = conn.execute("select count(*) n from ft.checkpoints").fetchone()["n"]
    expected = sum(len(d.items) for d in build_week(t, MON).days)
    assert n_items == expected
    assert n_cp == 5 * 4 + 1 + 1


def test_mid_week_start_skips_the_past(db, t):
    make_week(db, t, not_before=rome(MON + timedelta(days=2), 12))
    with db.tx() as conn:
        days = {r["day"] for r in conn.execute("select day from ft.plan_items").fetchall()}
        first_cp = conn.execute("select min(due_at) d from ft.checkpoints").fetchone()["d"]
    assert min(days) == MON + timedelta(days=2)
    assert first_cp.astimezone(ROME) >= rome(MON + timedelta(days=2), 12)


def test_invalid_template_refuses_to_build(db, make_template):
    bad = make_template(("max_minutes: 90", "max_minutes: 30"))
    with pytest.raises(PlanInvalid) as e:
        make_week(db, bad)
    assert any("below the 6h target" in err for err in e.value.errors)


# ---------------------------------------------------------------- the Phase 1 gate: timing across 25 October

def test_checkins_fire_at_the_right_italian_times_across_dst(db, t):
    sent = run(db, t, rome(date(2026, 10, 23), 0), rome(date(2026, 10, 26), 23, 55))
    by_day = {}
    for when, out in sent:
        with db.tx() as conn:
            cp = store.get_checkpoint(conn, out.checkpoint_id)
        by_day.setdefault(cp["day"], []).append((when, cp["kind"]))
    assert by_day[date(2026, 10, 23)] == [  # Friday, summer time
        ("11:05", "post_work"), ("13:15", "pre_trading"), ("15:30", "transition"), ("18:40", "shutdown")]
    assert by_day[date(2026, 10, 24)] == [("11:35", "saturday")]
    assert by_day[date(2026, 10, 25)] == [("17:30", "weekly_review")]
    assert by_day[MON] == [  # Monday, winter time: everything an hour earlier
        ("10:05", "post_work"), ("12:15", "pre_trading"), ("14:30", "transition"), ("18:40", "shutdown")]


def test_next_week_is_created_on_sunday_at_noon(db, t):
    run(db, t, rome(date(2026, 10, 25), 11, 50), rome(date(2026, 10, 25), 12, 5))
    with db.tx() as conn:
        assert store.get_week(conn, MON) is not None


# ---------------------------------------------------------------- a day with buttons

def test_a_day_of_buttons(db, t):
    make_week(db, t)
    post = run(db, t, rome(MON, 10, 5), rome(MON, 10, 5))[0][1]
    assert post.text.startswith("Work's done.")
    assert "Gym 10:00–11:00" in post.text
    assert "Later: Trading 12:30 · HCS 14:30 · Nap 16:00" in post.text
    assert "▶ Gym on track" in buttons(post)

    # Meditation happened at 06:00 but is not in the sweep yet: the day's first check-in asks about it.
    assert "? Meditation 06:00" in post.text
    r = press(db, t, post, "✓ Meditation", rome(MON, 10, 6))
    assert "✓ Meditation 06:00" in r.text and r.toast == "Meditation: done"
    r = engine.handle_callback(db, t, next(d for row in r.keyboard for (l, d) in row if l == "▶ Gym on track"),
                               rome(MON, 10, 6))
    assert "▶ Gym 10:00–11:00 · on track" in r.text
    assert r.keyboard == []

    pre = run(db, t, rome(MON, 12, 15), rome(MON, 12, 15))[0][1]
    assert pre.text.startswith("Trading development starts in 15 min: 12:30–14:30, 2h planned.")

    trans = run(db, t, rome(MON, 14, 30), rome(MON, 14, 30))[0][1]
    assert "Earlier" in trans.text and "? Gym 10:00–11:00" in trans.text and "? Trading development 12:30–14:30" in trans.text
    assert "Next\n• HCS 14:30–16:00" in trans.text

    # Trading was only an hour: Partial -> 60m.
    r = press_in_row(db, t, trans, "✓ Trading", "Partial", rome(MON, 14, 31))
    assert buttons(r)[:4] == ["✓ Gym", "Partial", "Skipped", "30m"]
    assert "60m" in buttons(r) and "90m" in buttons(r)
    cp_id = trans.checkpoint_id
    data = next(d for row in r.keyboard for (l, d) in row if l == "60m")
    r = engine.handle_callback(db, t, data, rome(MON, 14, 31))
    assert "½ Trading development 12:30–14:30 · 1h of 2h" in r.text

    # Gym skipped because "not feeling it": decision 8 asks for the reason.
    r = press_in_row(db, t, trans, "✓ Gym", "Skipped", rome(MON, 14, 32))
    assert "Not feeling it" in buttons(r)
    data = next(d for row in r.keyboard for (l, d) in row if l == "Not feeling it")
    r = engine.handle_callback(db, t, data, rome(MON, 14, 32))
    assert r.followup == "What's the reason for skipping gym? One line is enough."
    assert "✗ Gym 10:00–11:00 · not feeling it" in r.text
    reply = engine.handle_text(db, t, "Legs still sore from Saturday", rome(MON, 14, 33))
    assert reply.text.startswith("Noted.")
    assert item(db, MON, "gym")["skip_reason_text"] == "Legs still sore from Saturday"

    with db.tx() as conn:
        assert store.get_checkpoint(conn, cp_id)["status"] == "answered"

    # Progress as the Sunday review will see it.
    week = engine.cmd_week(db, t, rome(MON, 14, 40)).text
    assert "Gym: 0 of 3 sessions (goal 4) · 3 planned ahead" in week
    assert "Trading development: 1h of 8h (goal 10h) · 8h planned ahead" in week
    assert "Meditation: 1 of 5 (goal 7) · 6 planned ahead" in week
    assert week.index("Gym:") < week.index("Trading development:") < week.index("HCS:")  # template order
    assert "Skipped 1 (1 not feeling it)" in week


def test_undo_walks_back_one_change_at_a_time(db, t):
    make_week(db, t)
    gym = item(db, MON, "gym")
    now = rome(MON, 11, 30)
    with db.tx() as conn:
        store.record_outcome(conn, gym["id"], "skipped", now=now, reason_category="capacity")
        store.record_outcome(conn, gym["id"], "done", now=now)
    assert "Gym" in engine.cmd_undo(db, t, now).text
    assert item(db, MON, "gym")["status"] == "skipped"
    engine.cmd_undo(db, t, now)
    g = item(db, MON, "gym")
    assert g["status"] == "planned" and g["resolved_at"] is None
    assert engine.cmd_undo(db, t, now).text == "Nothing to undo."


def test_ledger_rejects_bad_writes(db, t):
    make_week(db, t)
    trading = item(db, MON, "trading")
    now = rome(MON, 15)
    with db.tx() as conn:
        with pytest.raises(LedgerError):
            store.record_outcome(conn, trading["id"], "partial", now=now, actual_minutes=120)
        with pytest.raises(LedgerError):
            store.record_outcome(conn, trading["id"], "skipped", now=now, reason_category="lazy")
        with pytest.raises(LedgerError):
            store.record_outcome(conn, trading["id"], "moved", now=now)


def test_short_partial_gym_does_not_count_as_a_session(db, t):
    make_week(db, t)
    now = rome(MON, 12)
    with db.tx() as conn:
        store.record_outcome(conn, item(db, MON, "gym")["id"], "partial", now=now, actual_minutes=15)
        wk = store.get_week(conn, MON)["id"]
        gym = next(p for p in store.week_progress(conn, wk, now) if p["activity_key"] == "gym")
    assert gym["done"] == 0
    with db.tx() as conn:
        store.undo_last(conn, now)
        store.record_outcome(conn, item(db, MON, "gym")["id"], "partial", now=now, actual_minutes=30)
        gym = next(p for p in store.week_progress(conn, wk, now) if p["activity_key"] == "gym")
    assert gym["done"] == 1


# ---------------------------------------------------------------- suppression, expiry, quiet time

def test_pre_trading_is_suppressed_after_a_recent_on_track(db, t):
    make_week(db, t)
    with db.tx() as conn:
        store.mark_on_track(conn, item(db, MON, "trading")["id"], rome(MON, 11, 0))
    sent = run(db, t, rome(MON, 12, 15), rome(MON, 12, 15))
    assert sent == []
    with db.tx() as conn:
        cp = conn.execute("select * from ft.checkpoints where kind = 'pre_trading' and day = %s", (MON,)).fetchone()
    assert (cp["status"], cp["suppression_reason"]) == ("suppressed", "nothing_to_ask")


def test_transition_is_suppressed_when_nothing_is_open(db, t):
    make_week(db, t)
    now = rome(MON, 14, 0)
    with db.tx() as conn:
        for key in ("meditation", "gym"):
            store.record_outcome(conn, item(db, MON, key)["id"], "done", now=now)
        store.record_outcome(conn, item(db, MON, "trading")["id"], "done", now=rome(MON, 14, 30))
        store.mark_on_track(conn, item(db, MON, "hcs")["id"], rome(MON, 14, 25))
    assert run(db, t, rome(MON, 14, 30), rome(MON, 14, 30)) == []


def test_unanswered_checkins_expire_and_stale_ones_are_dropped(db, t):
    make_week(db, t)
    run(db, t, rome(MON, 10, 5), rome(MON, 10, 5))
    run(db, t, rome(MON, 10, 55), rome(MON, 10, 55))
    with db.tx() as conn:
        statuses = {r["kind"]: r["status"] for r in conn.execute("select kind, status from ft.checkpoints where day = %s", (MON,))}
    assert statuses["post_work"] == "expired"

    # Bot down until 17:00: the 14:30 transition is too old to send, the 12:15 one too.
    run(db, t, rome(MON, 17, 0), rome(MON, 17, 0))
    with db.tx() as conn:
        rows = {r["kind"]: (r["status"], r["suppression_reason"])
                for r in conn.execute("select * from ft.checkpoints where day = %s", (MON,))}
    assert rows["pre_trading"] == ("missed", "stale")
    assert rows["transition"] == ("missed", "stale")


def test_no_checkin_during_the_nap_or_dinner(db, t):
    make_week(db, t)
    with db.tx() as conn:
        wk = store.get_week(conn, MON)["id"]
        for hh, mm in ((16, 30), (19, 10)):
            conn.execute(
                "insert into ft.checkpoints (week_id, day, kind, due_at, sweep, always_send, created_by) "
                "values (%s, %s, 'followup', %s, true, true, 'claude')", (wk, MON, rome(MON, hh, mm)))
    sent = run(db, t, rome(MON, 16, 30), rome(MON, 16, 30)) + run(db, t, rome(MON, 19, 10), rome(MON, 19, 10))
    assert sent == []
    with db.tx() as conn:
        reasons = [r["suppression_reason"] for r in conn.execute("select * from ft.checkpoints where kind = 'followup'")]
    assert reasons == ["quiet", "quiet"]


def test_daily_cap(db, make_template):
    capped = make_template(("daily_cap: 6", "daily_cap: 2"))
    make_week(db, capped)
    sent = run(db, capped, rome(MON, 10, 0), rome(MON, 19, 0))
    assert [w for w, _ in sent] == ["10:05", "12:15"]


# ---------------------------------------------------------------- day close and the sweep

def test_day_close_and_next_day_sweep(db, t):
    make_week(db, t)
    now = rome(MON, 11)
    with db.tx() as conn:
        store.record_outcome(conn, item(db, MON, "meditation")["id"], "done", now=now)
        store.record_outcome(conn, item(db, MON, "gym")["id"], "done", now=now)
    run(db, t, rome(MON, 22, 0), rome(MON, 22, 0))
    statuses = {}
    with db.tx() as conn:
        for i in store.items_for_day(conn, MON):
            statuses[i["activity_key"]] = i["status"]
        assert store.get_day_log(conn, MON)["closed_at"] is not None
    assert statuses["lunch_wife"] == statuses["dinner"] == statuses["family_evening"] == "done"  # confirm: auto
    assert statuses["trading"] == statuses["hcs"] == statuses["nap"] == statuses["reading"] == "unconfirmed"
    assert statuses["gym"] == "done"

    tue = MON + timedelta(days=1)
    post = run(db, t, rome(tue, 10, 5), rome(tue, 10, 5))[0][1]
    assert "? Read 10 pages 20:15 (Mon)" in post.text
    assert "? Trading development 12:30–14:30 (Mon)" in post.text

    with db.tx() as conn:
        roles = {r["role_key"]: r["actual_minutes"] for r in store.role_time(conn, store.get_week(conn, MON)["id"])}
    assert roles["husband"] == 90 and roles["father"] == 75 and roles["self"] == 70


def test_log_command_lists_open_items_with_buttons(db, t):
    make_week(db, t)
    r = engine.cmd_log(db, t, rome(MON, 15, 0))
    assert r.text.startswith("Still open this week.")
    assert "✓ Gym" in buttons(r) and "✓ Trading" in buttons(r)
    data = next(d for row in r.keyboard for (l, d) in row if l == "✓ Gym")
    r2 = engine.handle_callback(db, t, data, rome(MON, 15, 1))
    assert "✓ Gym 10:00–11:00" in r2.text


def test_today_shows_sleep_and_the_nap(db, t):
    make_week(db, t)
    text = engine.cmd_today(db, t, rome(MON, 9)).text
    assert "Wake 01:30 · work 02:00–10:00 · lights out 20:30" in text
    assert "Sleep 5h night + 1h nap" in text
    assert "• Nap 16:00–17:00" in text


# ---------------------------------------------------------------- ready for Phase 2 replans

def test_checkpoints_follow_a_moved_block(db, t):
    make_week(db, t)
    trading = item(db, MON, "trading")
    with db.tx() as conn:
        conn.execute("update ft.plan_items set start_at = start_at + interval '1 hour', "
                     "end_at = end_at + interval '1 hour' where id = %s", (trading["id"],))
        store.rebuild_checkpoints(conn, t, MON, rome(MON, 11))
        due = {r["kind"]: r["due_at"] for r in conn.execute(
            "select * from ft.checkpoints where day = %s and status = 'scheduled'", (MON,))}
    assert hhmm(due["pre_trading"]) == "13:15" and hhmm(due["transition"]) == "15:30"


def test_callback_data_fits_telegram_limit():
    from firstthings.render import cb
    data = cb(9_999_999_999, 9_999_999_999, "m", "180")
    assert len(data.encode()) <= 64 and parse_cb(data) == (9_999_999_999, 9_999_999_999, "m", "180")


def test_template_edit_applies_to_the_rest_of_the_week(db, t, make_template):
    make_week(db, t)
    noon = rome(MON, 12, 0)
    with db.tx() as conn:
        store.record_outcome(conn, item(db, MON, "gym")["id"], "done", now=noon)
        store.mark_on_track(conn, item(db, MON, "trading")["id"], noon)
    later_hcs = make_template(("{name: HCS, role: hcs, minutes: 90", "{name: HCS, role: hcs, minutes: 75"))
    with db.tx() as conn:
        result = store.rebuild_week_from_template(conn, later_hcs, MON, noon)
        mon = {i["activity_key"]: i for i in store.items_for_day(conn, MON) if i["status"] != "cancelled"}
        tue = {i["activity_key"]: i for i in store.items_for_day(conn, MON + timedelta(days=1)) if i["status"] != "cancelled"}
        due = {r["kind"]: r["due_at"] for r in conn.execute(
            "select * from ft.checkpoints where day = %s and status = 'scheduled'", (MON,))}
        trans_focus = conn.execute(
            "select focus_item_ids from ft.checkpoints where day = %s and kind = 'transition' and status = 'scheduled'",
            (MON,)).fetchone()["focus_item_ids"]
    assert result["cancelled"] > 0
    assert mon["gym"]["status"] == "done"                      # answered: untouched
    assert mon["trading"]["on_track_at"] is not None           # on track: kept
    assert hhmm(mon["hcs"]["end_at"]) == "15:45"               # future open: rebuilt
    assert hhmm(tue["hcs"]["end_at"]) == "15:45"
    assert hhmm(due["transition"]) == "14:30" and trans_focus == [mon["hcs"]["id"]]


def test_template_edit_that_breaks_sleep_is_refused(db, t, make_template):
    make_week(db, t)
    into_nap = make_template(("offset: 270}", "offset: 275}"))  # HCS would run into the nap
    with db.tx() as conn:
        with pytest.raises(PlanInvalid):
            store.rebuild_week_from_template(conn, into_nap, MON, rome(MON, 12))


def test_broken_template_warns_once_and_keeps_the_current_week_running(db, t, make_template):
    make_week(db, t)
    broken = make_template(("offset: 270}", "offset: 275}"))
    sun = MON + timedelta(days=6)
    sent = run(db, broken, rome(sun, 11, 55), rome(sun, 17, 30))
    texts = [o.text for _, o in sent]
    assert texts[0].startswith("I can't build the week") and "overlaps Nap" in texts[0]
    assert sum(1 for x in texts if x.startswith("I can't build")) == 1
    assert any(x.startswith("Weekly review time.") for x in texts)


def test_weekly_review_message_separates_ahead_from_unanswered(db, t):
    make_week(db, t)
    sun = MON + timedelta(days=6)
    with db.tx() as conn:
        for d in range(3):
            day = MON + timedelta(days=d)
            store.record_outcome(conn, item(db, day, "trading")["id"], "done", now=rome(day, 15))
    review = run(db, t, rome(sun, 17, 30), rome(sun, 17, 30))[0][1]
    assert review.text.startswith("Weekly review time.")
    assert "Trading development: 6h of 8h (goal 10h) · 2 unanswered" in review.text
    assert "Gym: 0 of 3 sessions (goal 4) · 4 unanswered" in review.text
    assert "still unanswered: /log" in review.text
    assert review.keyboard == []


# ---------------------------------------------------------------- the gate report

def answer_everything(db, t, start, end):
    """A diligent user: every ✓ offered by a check-in is pressed when it arrives."""
    now = start
    while now <= end:
        for out in engine.prepare_tick(db, t, now):
            engine.after_send(db, out, 1000 + out.checkpoint_id, now)
            for lbl in [lbl for lbl in buttons(out) if lbl.startswith("✓")]:
                press(db, t, out, lbl, now)
        now = shift(now, 5)


def test_gate_passes_after_three_answered_weekdays(db, t):
    make_week(db, t)
    thu = MON + timedelta(days=3)
    answer_everything(db, t, rome(MON, 0), rome(thu, 10, 5))  # Wednesday's reading is answered Thursday 10:05
    text, passed = engine.gate_report(db, t, rome(thu, 10, 10), MON, 4)
    assert passed, text
    lines = text.splitlines()
    assert sum(line.startswith(("Mon", "Tue", "Wed")) and "  ✓ all" in line for line in lines) == 3
    assert "10:05 post_work sent · 12:15 pre_trading sent · 14:30 transition sent · 18:40 shutdown sent" in text
    assert "Thu 29 Oct  in progress" in text


def test_gate_flags_downtime_and_unanswered_items(db, t):
    make_week(db, t)
    run(db, t, rome(MON, 0), rome(MON, 11))      # post_work goes out, nobody answers
    text, passed = engine.gate_report(db, t, rome(MON, 12, 30), MON, 1)
    assert "12:15 pre_trading NOT SENT" in text  # the bot was not running at 12:15
    run(db, t, rome(MON, 22), rome(MON, 22))     # back at 22:00: the rest is too old to send
    text, passed = engine.gate_report(db, t, rome(MON, 22, 5), MON, 1)
    assert not passed
    assert "14:30 transition MISSED" in text and "18:40 shutdown MISSED" in text
    assert "unanswered: Meditation 06:00, Gym 10:00" in text
