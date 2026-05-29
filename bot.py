#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║             Telegram Feedback Bot                    ║
║  Пользователь (личка) ↔ Топик в супергруппе          ║
╚══════════════════════════════════════════════════════╝
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
# Импортируем BadRequest — это ключевое исправление
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ══════════════════════════════════════════════
#  Настройки — задайте в файле .env
# ══════════════════════════════════════════════
BOT_TOKEN: str   = os.getenv("BOT_TOKEN", "")
GROUP_CHAT_ID: int = int(os.getenv("GROUP_CHAT_ID", "0"))
DATA_FILE: str   = os.getenv("DATA_FILE", "users.json")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
#  Хранилище (JSON)
# ══════════════════════════════════════════════
def _load() -> dict:
    p = Path(DATA_FILE)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"user_to_topic": {}, "topic_to_user": {}}


def _save(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_topic(user_id: int) -> int | None:
    return _load()["user_to_topic"].get(str(user_id))


def get_user(topic_id: int) -> int | None:
    return _load()["topic_to_user"].get(str(topic_id))


def link(user_id: int, topic_id: int) -> None:
    data = _load()
    data["user_to_topic"][str(user_id)] = topic_id
    data["topic_to_user"][str(topic_id)] = user_id
    _save(data)


def reset_user(user_id: int) -> None:
    data = _load()
    tid = data["user_to_topic"].pop(str(user_id), None)
    if tid:
        data["topic_to_user"].pop(str(tid), None)
    _save(data)


# ══════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════
async def _ensure_topic(user, ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    uid = user.id
    topic_id = get_topic(uid)

    if topic_id is not None:
        return topic_id

    topic_name = f"{user.full_name} · {uid}"[:128]
    try:
        ft = await ctx.bot.create_forum_topic(
            chat_id=GROUP_CHAT_ID,
            name=topic_name,
        )
    except TelegramError as e:
        log.error("Ошибка создания топика: %s", e)
        return None

    topic_id = ft.message_thread_id
    link(uid, topic_id)

    uname = f"@{user.username}" if user.username else "—"
    card = (
        f"👤 <b>{user.full_name}</b>\n"
        f"🔗 {uname}\n"
        f"🆔 <code>{uid}</code>\n\n"
        f"ℹ️ Чтобы ответить клиенту — просто пишите в этот топик."
    )
    try:
        header = await ctx.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            message_thread_id=topic_id,
            text=card,
            parse_mode=ParseMode.HTML,
        )
        await header.pin(disable_notification=True)
    except TelegramError as e:
        log.warning("Не удалось закрепить карточку: %s", e)

    return topic_id


class _TopicGone(Exception):
    """Исключение: топик удален, закрыт или недоступен."""


async def _forward_to_topic(msg, topic_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Пересылает сообщение. Если топик мёртв — возбуждает _TopicGone."""
    last_err = None
    for method in ("forward", "copy"):
        try:
            if method == "forward":
                await ctx.bot.forward_message(
                    chat_id=GROUP_CHAT_ID,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    message_thread_id=topic_id,
                )
            else:
                await ctx.bot.copy_message(
                    chat_id=GROUP_CHAT_ID,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    message_thread_id=topic_id,
                )
            return True
        except BadRequest as e:
            # Если Telegram вернул BadRequest — топик гарантированно недействителен
            log.info("Зафиксирован BadRequest от Telegram: %s. Топик %s считается удалённым.", e, topic_id)
            raise _TopicGone(str(e))
        except TelegramError as e:
            last_err = e

    log.error("Критическая ошибка отправки в топик: %s", last_err)
    return False


# ══════════════════════════════════════════════
#  Обработчики
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"Здравствуйте, {user.full_name}!\n\n"
        "Напишите Ваш вопрос, и мы ответим Вам в ближайшее время."
    )


async def from_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    user = update.effective_user
    uid  = user.id
    
    ok = False
    is_new = get_topic(uid) is None

    # Жесткий цикл на 2 попытки
    for attempt in range(2):
        topic_id = await _ensure_topic(user, ctx)
        if topic_id is None:
            await msg.reply_text("⚠️ Произошла ошибка на стороне сервера. Пожалуйста, попробуйте позже.")
            return

        try:
            ok = await _forward_to_topic(msg, topic_id, ctx)
            if ok:
                break  # Всё ушло успешно, выходим из цикла
        except _TopicGone:
            # Сюда бот гарантированно попадет ОПЕРАТИВНО в рантайме при удалении топика
            log.warning("Топик %s удален. Очищаю базу и создаю новый для юзера %s", topic_id, uid)
            reset_user(uid)  # Стираем старый ID из users.json прямо на лету!
            is_new = True    # Чтобы клиенту ушло красивое приветствие

    if not ok:
        await msg.reply_text("⚠️ Не удалось отправить сообщение. Попробуйте ещё раз.")
        return

    if is_new:
        await msg.reply_text(
            "✅ Сообщение получено!\n"
            "Наши специалисты ответят вам в ближайшее время."
        )
    else:
        await msg.reply_text("✅ Отправлено")


async def from_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message

    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    if not msg.message_thread_id:
        return
    if msg.from_user and msg.from_user.is_bot:
        return
    if (
        msg.forum_topic_created
        or msg.forum_topic_closed
        or msg.forum_topic_reopened
        or msg.forum_topic_edited
    ):
        return
    if msg.pinned_message:
        return

    uid = get_user(msg.message_thread_id)
    if uid is None:
        return

    try:
        await ctx.bot.copy_message(
            chat_id=uid,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
    except TelegramError as e:
        log.error("Не удалось отправить ответ пользователю %s: %s", uid, e)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в .env")
    if not GROUP_CHAT_ID:
        raise RuntimeError("Укажите GROUP_CHAT_ID в .env")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, from_user))
    app.add_handler(MessageHandler(filters.Chat(GROUP_CHAT_ID), from_group))

    log.info("Бот запущен. Ожидание сообщений...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
