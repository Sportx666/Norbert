# firstthings — Phase 1

A Telegram bot that knows your week, checks in at the day's transitions, and keeps a ledger of
what actually happened. Phase 1 has no AI: it proves the plan, the timing and the ledger.
Conversation and replanning arrive in Phase 2.

## What Phase 1 does

- **Builds each week from `config/week_template.yaml`.** Work is stored in Perth time, so the Italian
  clock shifts by itself: 03:00–11:00 until 25 Oct 2026, 02:00–10:00 from 26 Oct, back on 29 Mar 2027.
  Afternoon blocks follow work end. Sleep is 6 hours a day: lights out 20:30, and a nap after HCS
  appears only when the night is short (from 26 Oct: 5h night + 1h nap at 16:00).
- **Checks in four times on weekdays**, once on Saturday and once on Sunday (the review):
  post-work, 15 minutes before trading, trading end, and 18:40 before dinner.
- **Buttons on every check-in:** ✓ done, Partial (pick minutes), Skipped (pick a reason), ▶ on track.
  "Not feeling it" always asks you for the reason in one line.
- **Never nags:** an unanswered check-in expires quietly after 45 minutes; nothing fires during sleep,
  the nap, dinner or the kids' evening; at most 6 messages a day.
- **Never guesses:** at 22:00 each day closes. Family blocks marked `confirm: auto` count as done;
  everything else you didn't answer becomes *unconfirmed* (not done, not missed) and the next
  check-in asks again.
- **Commands:** `/today`, `/tomorrow`, `/week`, `/log` (unanswered items with buttons, 10 at a time),
  `/undo` (reverts your last answer, step by step).

## Set up on your VPS (about 20 minutes)

You need Python 3.11+, a Telegram bot token and a Supabase project.

1. **Telegram:** message @BotFather, send `/newbot`, keep the token.
2. **Supabase:** create a project, then *Connect* → *Session pooler* and copy the URI.
   The session pooler works over IPv4; the direct connection string may need IPv6.
   The bot's tables live in their own schema `ft`, which Supabase's public API does not expose.
3. **On the VPS**, copy this folder to `~/firstthings`, then:

```bash
cd ~/firstthings
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env && nano .env        # token + DATABASE_URL; leave TELEGRAM_CHAT_ID empty
.venv/bin/firstthings check-template     # prints the week and validates it
.venv/bin/firstthings migrate            # creates the tables
.venv/bin/firstthings run                # send /start to your bot: it replies with your chat id
```

4. Put `TELEGRAM_CHAT_ID=<that number>` in `.env`, stop the bot (Ctrl-C), and run it as a service:

```bash
sudo cp deploy/firstthings.service /etc/systemd/system/   # edit YOUR_USER first
sudo systemctl daemon-reload && sudo systemctl enable --now firstthings
journalctl -u firstthings -f
```

The VPS's own time zone doesn't matter; every time is stored with its zone.

If you start mid-week, nothing before the start time is created, so you won't be asked about the past.

## Changing the plan

Edit `config/week_template.yaml`, then:

```bash
.venv/bin/firstthings check-template --week 2026-10-26   # any date in the week you want to see
.venv/bin/firstthings rebuild-week                       # apply it to the rest of this week
sudo systemctl restart firstthings
```

`rebuild-week` only replaces future items you haven't answered or marked on track; history is kept.
A template that breaks the sleep rule, overlaps blocks or puts a check-in in quiet time is refused
with the exact reason. Next week is built from the template every Sunday at 12:00.

The template's `targets` are provisional for the baseline week (decision 7); the first Sunday
review sets the real floors and goals.

## Phase 1 gate

Three real weekdays where every item ends the day with a status, and check-ins that fire at the
right Italian times across 25 October. The second half is covered by the tests; the first half is yours.

Check where you stand at any time:

```bash
.venv/bin/firstthings gate                     # this week, day by day
.venv/bin/firstthings gate --from 2026-10-26   # from a given day
```

A weekday counts once every item has an answer and every check-in went out within 5 minutes of its time.
The evening reading is asked at the next morning's check-in, so a weekday is complete the morning after.

## Tests

Tests need a throwaway Postgres: they drop and recreate the `ft` schema, and refuse to run against
a Supabase URL.

```bash
.venv/bin/pip install -e '.[dev]'
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/ft_test .venv/bin/pytest -q
```

Without `TEST_DATABASE_URL` only the pure planning tests run.

## Layout

| Path | What it is |
| --- | --- |
| `config/week_template.yaml` | Your default week, targets and check-in rules |
| `src/firstthings/planner.py` | Pure core: anchors, DST, sleep and nap, check-in times, validation |
| `src/firstthings/store.py` | The ledger: every write goes through here (Phase 2's tools will too) |
| `src/firstthings/engine.py` | What happens on each tick, button, command and message |
| `src/firstthings/render.py` | Message text and buttons (Claude composes these from Phase 2) |
| `src/firstthings/bot.py` | Telegram transport and the 60-second scheduler |
| `src/firstthings/migrations/` | SQL schema and progress views |
