"""Command line: firstthings check-template | migrate | run | rebuild-week | today | week."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from .planner import build_week, validate_week
from .settings import load_settings
from .template import TemplateError, load_template
from .timeutil import ROME, day_label, fmt_minutes, hhmm, local, monday_of, utcnow


def _template(path):
    try:
        return load_template(path)
    except TemplateError as e:
        print("Template has problems:")
        for p in e.problems:
            print(f"  - {p}")
        sys.exit(1)
    except FileNotFoundError:
        print(f"No template at {path}. Set FT_TEMPLATE or run from the project folder.")
        sys.exit(1)


def _db(settings):
    from .db import Database, check_connection
    if not settings.database_url:
        print("DATABASE_URL is not set (see .env.example).")
        sys.exit(1)
    problem = check_connection(settings.database_url)
    if problem:
        print(f"Can't connect to the database: {problem}")
        sys.exit(1)
    return Database(settings.database_url)


def cmd_check_template(args, settings) -> int:
    t = _template(settings.template_path)
    monday = date.fromisoformat(args.week) if args.week else monday_of(local(utcnow(), ROME).date())
    monday = monday_of(monday)
    week = build_week(t, monday)
    v = validate_week(t, week)
    print(f"Template {settings.template_path} (hash {t.source_hash}), week of {day_label(monday)}\n")
    for d in week.days:
        a = d.anchors
        head = f"{day_label(d.day)}  wake {hhmm(a.wake)}"
        if a.workday:
            head += f" · work {hhmm(a.work_start)}-{hhmm(a.work_end)}"
        head += f" · lights out {hhmm(a.lights_out)} · sleep {fmt_minutes(a.night_minutes)}"
        if a.nap_minutes:
            head += f" + {fmt_minutes(a.nap_minutes)} nap"
        print(head)
        for i in d.items:
            print(f"    {hhmm(i.start)}-{hhmm(i.end)}  {i.title}")
        cps = [c for c in week.checkpoints if c.day == d.day]
        if cps:
            print("    check-ins: " + " · ".join(f"{hhmm(c.due)} {c.kind}" for c in cps))
    if v.warnings:
        print("\nWarnings:")
        for w in v.warnings:
            print(f"  - {w}")
    if v.errors:
        print("\nErrors:")
        for e in v.errors:
            print(f"  - {e}")
        return 1
    print("\nTemplate validates.")
    return 0


def cmd_migrate(args, settings) -> int:
    from .db import migrate
    db = _db(settings)
    try:
        with db.tx() as conn:
            applied = migrate(conn)
    finally:
        db.close()
    print("Applied: " + ", ".join(applied) if applied else "Database is up to date.")
    return 0


def cmd_run(args, settings) -> int:
    from .bot import run
    if not settings.telegram_token:
        print("TELEGRAM_BOT_TOKEN is not set (see .env.example).")
        return 1
    t = _template(settings.template_path)
    db = _db(settings)
    from .db import migrate
    with db.tx() as conn:
        migrate(conn)
    logging.getLogger("firstthings").info("starting; template hash %s", t.source_hash)
    run(settings, db, t)
    return 0


def cmd_rebuild_week(args, settings) -> int:
    from . import store
    t = _template(settings.template_path)
    db = _db(settings)
    now = utcnow()
    try:
        with db.tx() as conn:
            result = store.rebuild_week_from_template(conn, t, monday_of(local(now, ROME).date()), now)
    except store.PlanInvalid as e:
        db.close()
        print("Template does not validate; nothing changed:")
        for err in e.errors:
            print(f"  - {err}")
        return 1
    db.close()
    print(f"Rest of this week rebuilt from the template: {result['cancelled']} open items replaced, "
          f"{result['created']} created. Answered and past items were not touched.")
    return 0


def cmd_today(args, settings) -> int:
    from . import engine
    t = _template(settings.template_path)
    db = _db(settings)
    try:
        print(engine.cmd_today(db, t, utcnow(), args.offset).text)
    finally:
        db.close()
    return 0


def cmd_week(args, settings) -> int:
    from . import engine
    t = _template(settings.template_path)
    db = _db(settings)
    try:
        print(engine.cmd_week(db, t, utcnow()).text)
    finally:
        db.close()
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="firstthings")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check-template", help="print and validate a week built from the template")
    c.add_argument("--week", help="any date in the week to show (YYYY-MM-DD)")
    sub.add_parser("migrate", help="create or update the database schema")
    sub.add_parser("run", help="run the Telegram bot and scheduler")
    sub.add_parser("rebuild-week", help="apply an edited template to the rest of the current week")
    c = sub.add_parser("today", help="print today's plan from the database")
    c.add_argument("--offset", type=int, default=0)
    sub.add_parser("week", help="print this week's progress from the database")
    args = p.parse_args(argv)
    settings = load_settings()
    handler = {"check-template": cmd_check_template, "migrate": cmd_migrate, "run": cmd_run,
               "today": cmd_today, "week": cmd_week, "rebuild-week": cmd_rebuild_week}[args.cmd]
    return handler(args, settings) or 0


if __name__ == "__main__":
    sys.exit(main())
