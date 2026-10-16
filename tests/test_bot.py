"""Telegram glue, exercised with fakes (no network)."""
import asyncio
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace

from firstthings import bot as botmod
from firstthings.bot import FirstThingsBot, markup
from firstthings.settings import Settings
from firstthings.timeutil import ROME

from test_ledger import MON, make_week

CHAT = 4242


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answered = None
        self.edited = None

    async def answer(self, text=""):
        self.answered = text

    async def edit_message_text(self, text, reply_markup=None):
        self.edited = (text, reply_markup)


def settings(chat=CHAT):
    return Settings(telegram_token="123:TEST", chat_id=chat, database_url=None, template_path=Path("x"))


def test_application_wires_handlers_and_tick(db, t):
    app = FirstThingsBot(settings(), db, t).build()
    assert app.job_queue.get_jobs_by_name("tick")
    assert sum(len(h) for h in app.handlers.values()) >= 8


def test_without_chat_id_only_start_answers(db, t):
    app = FirstThingsBot(settings(chat=None), db, t).build()
    assert sum(len(h) for h in app.handlers.values()) == 1


def test_tick_sends_and_buttons_edit_in_place(db, t, monkeypatch):
    make_week(db, t)
    b = FirstThingsBot(settings(), db, t)
    fake = FakeBot()
    ctx = SimpleNamespace(bot=fake)

    monkeypatch.setattr(botmod, "utcnow", lambda: datetime.combine(MON, time(10, 5), tzinfo=ROME))
    asyncio.run(b.tick(ctx))
    assert len(fake.sent) == 1
    chat, text, kb = fake.sent[0]
    assert chat == CHAT and text.startswith("Work's done.")

    data = next(btn.callback_data for row in kb.inline_keyboard for btn in row if btn.text == "✓ Meditation")
    q = FakeQuery(data)
    update = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=CHAT))
    asyncio.run(b.on_button(update, ctx))
    assert q.answered == "Meditation: done"
    assert "✓ Meditation 06:00" in q.edited[0]


def test_strangers_are_ignored(db, t):
    b = FirstThingsBot(settings(), db, t)
    q = FakeQuery("1|1|d|")
    update = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=999))
    asyncio.run(b.on_button(update, SimpleNamespace(bot=FakeBot())))
    assert q.answered == "" and q.edited is None


def test_markup_none_when_empty():
    assert markup([]) is None
