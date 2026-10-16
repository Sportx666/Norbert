"""Telegram transport: commands, buttons, free text, and the 60-second scheduler tick.

All decisions live in engine.py; this file only moves text between Telegram and the engine.
The bot answers one chat only (TELEGRAM_CHAT_ID) and ignores everyone else.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import engine
from .db import Database
from .render import Keyboard
from .settings import Settings
from .template import Template
from .timeutil import utcnow

log = logging.getLogger("firstthings.bot")

HELP = (
    "I check in at the day's transitions and keep the ledger of what actually happened.\n\n"
    "/today  today's plan as it stands\n"
    "/tomorrow  tomorrow's plan\n"
    "/week  targets and progress\n"
    "/log  everything still unanswered this week\n"
    "/undo  revert your last button press\n\n"
    "Conversation and replanning arrive in Phase 2."
)


def markup(kb: Keyboard) -> InlineKeyboardMarkup | None:
    if not kb:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in kb])


class FirstThingsBot:
    def __init__(self, settings: Settings, db: Database, template: Template):
        self.s = settings
        self.db = db
        self.t = template

    # ------------------------------------------------------------ wiring

    def build(self) -> Application:
        app = Application.builder().token(self.s.telegram_token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        if self.s.chat_id is None:
            log.warning("TELEGRAM_CHAT_ID is not set: only /start answers (it tells you your chat id).")
            return app
        mine = filters.Chat(chat_id=self.s.chat_id)
        app.add_handler(CommandHandler("help", self.cmd_help, filters=mine))
        app.add_handler(CommandHandler("today", self.cmd_today, filters=mine))
        app.add_handler(CommandHandler("tomorrow", self.cmd_tomorrow, filters=mine))
        app.add_handler(CommandHandler("week", self.cmd_week, filters=mine))
        app.add_handler(CommandHandler("log", self.cmd_log, filters=mine))
        app.add_handler(CommandHandler("undo", self.cmd_undo, filters=mine))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & mine, self.on_text))
        app.add_error_handler(self.on_error)
        app.job_queue.run_repeating(self.tick, interval=60, first=5, name="tick")
        return app

    def _mine(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == self.s.chat_id

    async def _reply(self, update: Update, reply: engine.Reply) -> None:
        await update.effective_message.reply_text(reply.text, reply_markup=markup(reply.keyboard))

    # ------------------------------------------------------------ scheduler

    async def tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            outs = await asyncio.to_thread(engine.prepare_tick, self.db, self.t, utcnow())
        except Exception:
            log.exception("tick failed")
            return
        for out in outs:
            try:
                msg = await context.bot.send_message(self.s.chat_id, out.text, reply_markup=markup(out.keyboard))
                await asyncio.to_thread(engine.after_send, self.db, out, msg.message_id, utcnow())
            except Exception:
                log.exception("sending check-in %s failed; it will retry next tick", out.checkpoint_id)

    # ------------------------------------------------------------ handlers

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if self.s.chat_id is None:
            await update.effective_message.reply_text(
                f"Your chat id is {chat_id}. Put TELEGRAM_CHAT_ID={chat_id} in the .env file and restart me.")
            return
        if chat_id != self.s.chat_id:
            return
        await update.effective_message.reply_text(HELP)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, await asyncio.to_thread(engine.cmd_today, self.db, self.t, utcnow()))

    async def cmd_tomorrow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, await asyncio.to_thread(engine.cmd_today, self.db, self.t, utcnow(), 1))

    async def cmd_week(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, await asyncio.to_thread(engine.cmd_week, self.db, self.t, utcnow()))

    async def cmd_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, await asyncio.to_thread(engine.cmd_log, self.db, self.t, utcnow()))

    async def cmd_undo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, await asyncio.to_thread(engine.cmd_undo, self.db, self.t, utcnow()))

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self._mine(update):
            await q.answer()
            return
        reply = await asyncio.to_thread(engine.handle_callback, self.db, self.t, q.data, utcnow())
        await q.answer(reply.toast or "")
        if reply.text:
            try:
                await q.edit_message_text(reply.text, reply_markup=markup(reply.keyboard))
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise
        if reply.followup:
            await context.bot.send_message(self.s.chat_id, reply.followup)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        reply = await asyncio.to_thread(engine.handle_text, self.db, self.t, update.effective_message.text, utcnow())
        await self._reply(update, reply)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("handler error", exc_info=context.error)
        if isinstance(update, Update) and self._mine(update):
            try:
                await context.bot.send_message(self.s.chat_id, "Something went wrong on my side; it's in the log.")
            except Exception:
                pass


def run(settings: Settings, db: Database, template: Template) -> None:
    app = FirstThingsBot(settings, db, template).build()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
