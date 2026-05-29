#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║             Telegram Feedback Bot                    ║
║  Пользователь (личка) ↔ Топик в супергруппе          ║
╚══════════════════════════════════════════════════════╝

Схема:
  1. Клиент пишет /start → приветствие по имени.
  2. Клиент пишет вопрос → бот создаёт топик в группе
     (один раз на пользователя) и пересылает туда сообщение.
  3. Менеджер отвечает в топике → бот пересылает ответ клиенту в личку.
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
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
#  users.json хранит: user_id ↔ topic_id
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
    """Возвращает topic_id для пользователя или None."""
    return _load()["user_to_topic"].get(str(user_id))


def get_user(topic_id: int) -> int | None:
    """Возвращает user_id по topic_id или None."""
    return _load()["topic_to_user"].get(str(topic_id))


def link(user_id: int, topic_id: int) -> None:
    """Сохраняет связку user ↔ topic."""
    data = _load()
    data["user_to_topic"][str(user_id)] = topic_id
    data["topic_to_user"][str(topic_id)] = user_id
    _save(data)


def reset_user(user_id: int) -> None:
    """Удаляет запись о пользователе (например, если топик удалён)."""
    data = _load()
    tid = data["user_to_topic"].pop(str(user_id), None)
    if tid:
        data["topic_to_user"].pop(str(tid), None)
    _save(data)


# ══════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════
async def _ensure_topic(user, ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    """
    Возвращает существующий topic_id или создаёт новый.
    Возвращает None при ошибке.
    """
    uid = user.id
    topic_id = get_topic(uid)

    if topic_id is not None:
        return topic_id

    # Создаём новый топик
    topic_name = f"{user.full_name} · {uid}"[:128]
    try:
        ft = await ctx.bot.create_forum_topic(
            chat_id=GROUP_CHAT_ID,
            name=topic_name,
        )
    except TelegramError as e:
        log.error("create_forum_topic: %s", e)
        return None

    topic_id = ft.message_thread_id
    link(uid, topic_id)

    # Карточка пользователя в шапке топика
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
        log.warning("Не удалось отправить/закрепить карточку: %s", e)

    return topic_id


class _TopicGone(Exception):
    """Топик удалён или закрыт — нужно создать новый."""


# Ошибки Telegram, означающие что топик недоступен
_TOPIC_GONE_MARKERS = (
    "message thread not found",
    "thread not found",
    "message_thread_id_invalid",
    "topic_closed",
    "topic closed",
    "topic is closed",
    "topic_deleted",
    "not found",
)


async def _forward_to_topic(msg, topic_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Пересылает сообщение в топик группы.
    Сначала пробует forward (сохраняет имя отправителя),
    при ошибке приватности — делает copy.
    Поднимает _TopicGone если топик удалён/закрыт.
    Возвращает True при успехе.
    """
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
        except TelegramError as e:
            err = str(e).lower()
            if any(marker in err for marker in _TOPIC_GONE_MARKERS):
                raise _TopicGone()
            last_err = e

    log.error("Не удалось переслать сообщение в топик: %s", last_err)
    return False


# ══════════════════════════════════════════════
#  Обработчики
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — приветствие."""
    user = update.effective_user
    await update.message.reply_text(
        f"Здравствуйте, {user.full_name}!\n\n"
        "Напишите Ваш вопрос, и мы ответим Вам в ближайшее время."
    )


async def from_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Личка пользователя → топик группы поддержки."""
    msg  = update.message
    user = update.effective_user
    uid  = user.id
    
    ok = False
    is_new = get_topic(uid) is None

    # Цикл из двух попыток: отправка в существующий топик / пересоздание при неудаче
    for attempt in range(2):
        # Получаем старый топик или создаём новый (если в базе пусто)
        topic_id = await _ensure_topic(user, ctx)
        if topic_id is None:
            await msg.reply_text("⚠️ Произошла ошибка на стороне сервера. Пожалуйста, попробуйте позже.")
            return

        try:
            ok = await _forward_to_topic(msg, topic_id, ctx)
            if ok:
                break  # Успешно доставили, выходим из цикла
        except _TopicGone:
            # Сюда мы падаем, если Telegram вернул ошибку, что топик удален
            log.warning("Топик %s удалён или закрыт в Telegram. Пересоздаю для юзера %s (попытка %d/2)", topic_id, uid, attempt + 1)
            reset_user(uid)  # Чистим JSON, чтобы на следующем круге _ensure_topic создал новый топик
            is_new = True    # Включаем флаг первого сообщения, чтобы отправить полный текст-подтверждение

    if not ok:
        await msg.reply_text("⚠️ Не удалось отправить сообщение. Попробуйте ещё раз.")
        return

    # Подтверждение отправки для пользователя
    if is_new:
        await msg.reply_text(
            "✅ Сообщение получено!\n"
            "Наши специалисты ответят вам в ближайшее время."
        )
    else:
        await msg.reply_text("✅ Отправлено")


async def from_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ менеджера в топике → личка пользователя."""
    msg = update.message

    # Только наша группа поддержки
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    # Только сообщения внутри топиков (не General)
    if not msg.message_thread_id:
        return
    # Пропускаем ботов
    if msg.from_user and msg.from_user.is_bot:
        return
    # Пропускаем служебные события топика
    if (
        msg.forum_topic_created
        or msg.forum_topic_closed
        or msg.forum_topic_reopened
        or msg.forum_topic_edited
    ):
        return
    # Пропускаем закреплённые сообщения (карточка пользователя)
    if msg.pinned_message:
        return

    uid = get_user(msg.message_thread_id)
    if uid is None:
        return  # Незнакомый топик

    try:
        await ctx.bot.copy_message(
            chat_id=uid,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
    except TelegramError as e:
        log.error("Не удалось отправить ответ пользователю %s: %s", uid, e)


# ══════════════════════════════════════════════
#  Запуск
# ══════════════════════════════════════════════
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в .env")
    if not GROUP_CHAT_ID:
        raise RuntimeError("Укажите GROUP_CHAT_ID в .env")

    app = Application.builder().token(BOT_TOKEN).build()

    # Личка → группа
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            from_user,
        )
    )

    # Группа → личка
    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_CHAT_ID),
            from_group,
        )
    )

    log.info("Бот запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
