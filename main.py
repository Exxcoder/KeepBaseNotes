import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qsl

import pytz
import uvicorn
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    TelegramObject,
    WebAppInfo,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import database as db

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("KubarUvalBot")

BASE_DIR = pathlib.Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Инициализация бота и планировщика
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None
scheduler = AsyncIOScheduler()

if config.BOT_TOKEN:
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()


# --- Вспомогательные функции ---

async def get_bot_username() -> str:
    if bot:
        try:
            me = await bot.get_me()
            return me.username or config.BOT_USERNAME
        except Exception:
            pass
    return config.BOT_USERNAME


async def build_panel_keyboard(chat_id: int, is_group: bool) -> InlineKeyboardMarkup:
    """
    Создает клавиатуру с кнопкой WebApp.
    В группах Telegram запрещает кнопки типа web_app (BUTTON_TYPE_INVALID),
    поэтому используется deep-link в личку бота и прямая ссылка.
    В личных сообщениях отправляется нативная кнопка web_app.
    """
    bot_user = await get_bot_username()
    base_url = await db.get_effective_webapp_url(chat_id)
    target_webapp_url = f"{base_url}/webapp?chat_id={chat_id}" if base_url else ""

    buttons = []
    if is_group:
        deep_link = f"https://t.me/{bot_user}?start=chat_{chat_id}"
        row = [InlineKeyboardButton(text="📱 Открыть панель в Telegram", url=deep_link)]
        if target_webapp_url and target_webapp_url.startswith("https://"):
            row.append(InlineKeyboardButton(text="🌐 Веб-версия", url=target_webapp_url))
        buttons.append(row)
    else:
        if target_webapp_url and target_webapp_url.startswith("https://"):
            buttons.append([
                InlineKeyboardButton(text="📱 Открыть панель кубаря & увалов", web_app=WebAppInfo(url=target_webapp_url))
            ])
        else:
            deep_link = f"https://t.me/{bot_user}"
            buttons.append([
                InlineKeyboardButton(text="📱 Открыть приложение", url=deep_link)
            ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Middleware ---

class UserTrackingMiddleware(BaseMiddleware):
    """
    Автоматическая регистрация участников группы при отправке сообщений в чат.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in ("group", "supergroup"):
            user = event.from_user
            if user and not user.is_bot:
                try:
                    full_name = (user.full_name or user.first_name or f"Боец #{user.id}").strip()
                    await db.get_or_create_chat(event.chat.id, event.chat.title)
                    await db.save_known_user(event.chat.id, user.id, full_name, user.username)
                except Exception as err:
                    logger.debug(f"User tracking error: {err}")
        return await handler(event, data)


if dp:
    dp.message.outer_middleware(UserTrackingMiddleware())


# --- Aiogram Handlers ---

if dp:
    # 1. Быстрые триггеры увалов: +увал, -увал (при ответе на сообщение бойца)
    UVAL_PATTERN = re.compile(
        r"^([+-])\s*(\d*)\s*(?:увал[а-яё]*|ув)(?:\s+(.*))?$", re.IGNORECASE
    )

    @dp.message(F.reply_to_message, F.text)
    async def handle_uval_reply(message: Message) -> None:
        if message.from_user and message.from_user.is_bot:
            return

        text = message.text.strip()
        match = UVAL_PATTERN.match(text)
        if not match:
            return

        target_msg = message.reply_to_message
        target_user = target_msg.from_user
        if not target_user:
            return

        if target_user.is_bot:
            await message.reply("⚠️ Боты не могут получать или расходовать увольнения.")
            return

        chat_id = message.chat.id
        user_id = target_user.id

        if await db.is_blacklisted(chat_id, user_id):
            await message.reply("⚠️ Боец находится в чёрном списке данного кубаря.")
            return

        sign = match.group(1)
        amount_str = match.group(2)
        reason = match.group(3)
        amount = int(amount_str) if amount_str else 1
        delta = amount if sign == "+" else -amount

        target_name = (target_user.full_name or target_user.first_name or f"Боец #{target_user.id}").strip()

        # Регистрация в known_users
        await db.save_known_user(chat_id, user_id, target_name, target_user.username)

        res = await db.change_uval(
            chat_id=chat_id,
            telegram_id=user_id,
            delta=delta,
            fallback_name=target_name,
            username=target_user.username,
            reason=reason or ("По ответу в чате" + (f" ({reason})" if reason else ""))
        )

        if res is None:
            await message.reply("⚠️ Не удалось применить операцию: боец в чёрном списке.")
            return

        new_count, full_name, _ = res
        action_verb = "начислено" if delta > 0 else "списано"
        delta_str = f"+{delta}" if delta > 0 else f"{delta}"

        reply_text = (
            f"🎖 <b>Учёт увольнений «Duty Pass»</b>\n\n"
            f"👤 Боец: <b>{full_name}</b>\n"
            f"⚡ Действие: <b>{action_verb} {delta_str}</b> ув.\n"
            f"📊 Текущий остаток: <b>{new_count}</b> ув."
        )
        if reason:
            reply_text += f"\n📝 Причина: <i>{reason}</i>"

        is_group = message.chat.type in ("group", "supergroup")
        kb = await build_panel_keyboard(chat_id, is_group=is_group)
        await message.reply(reply_text, reply_markup=kb)

    # 2. Главная команда /c: кто СЕГОДНЯ и ЗАВТРА ответственный за кубарь
    @dp.message(Command("c", "check", "duty", "current"))
    async def cmd_duty_check(message: Message) -> None:
        chat_id = message.chat.id
        is_group = message.chat.type in ("group", "supergroup")

        duty_info = await db.get_duty_today_and_tomorrow(chat_id)
        today = duty_info.get("today")
        tomorrow = duty_info.get("tomorrow")

        today_dt = datetime.now()
        tomorrow_dt = today_dt + pytz.timedelta(days=1)
        months_ru = [
            "", "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря"
        ]
        today_date_str = f"{today_dt.day} {months_ru[today_dt.month]}"
        tomorrow_date_str = f"{tomorrow_dt.day} {months_ru[tomorrow_dt.month]}"

        kb_rows = []

        if not today:
            text = (
                "🧹 <b>Ответственные за кубарь</b>\n\n"
                "⚠️ <i>Список дежурных пока пуст или никто не находится в строю.</i>\n\n"
                "Нажмите кнопку ниже, чтобы открыть веб-панель и настроить очередь бойцов:"
            )
        else:
            today_name = f"<a href='tg://user?id={today['telegram_id']}'>{today['full_name']}</a>" if today.get("telegram_id") else f"<b>{today['full_name']}</b>"
            tomorrow_name = f"<a href='tg://user?id={tomorrow['telegram_id']}'>{tomorrow['full_name']}</a>" if (tomorrow and tomorrow.get("telegram_id")) else f"<b>{tomorrow['full_name']}</b>" if tomorrow else "<i>Не назначен</i>"

            text = (
                f"🧹 <b>График ответственных за кубарь</b>\n\n"
                f"📅 <b>СЕГОДНЯ ({today_date_str}):</b>\n"
                f"👤 {today_name} (№{today.get('order_num', 1)} в очереди)\n"
                f"⚡ Смен на кубаре: <b>{today.get('duty_count', 0)}</b> | Увалов: <b>{today.get('uval_count', 0)}</b>\n\n"
                f"📅 <b>ЗАВТРА ({tomorrow_date_str}):</b>\n"
                f"👤 {tomorrow_name}" + (f" (№{tomorrow.get('order_num', 2)} в очереди)\n" if tomorrow else "\n")
            )
            if tomorrow:
                text += f"⚡ Смен на кубаре: <b>{tomorrow.get('duty_count', 0)}</b>\n\n"

            text += "<i>💡 Нажмите кнопку ниже для управления очередью или запроса замены:</i>"

            kb_rows.append([
                InlineKeyboardButton(text="🔄 Запросить замену", callback_data="btn_replace_duty")
            ])

        # Добавляем кнопку открытия WebApp
        panel_kb = await build_panel_keyboard(chat_id, is_group=is_group)
        for row in panel_kb.inline_keyboard:
            kb_rows.append(row)

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

    # 3. Callback для замены ответственного
    @dp.callback_query(F.data == "btn_replace_duty")
    async def cb_replace_duty(callback: CallbackQuery) -> None:
        chat_id = callback.message.chat.id
        caller_id = callback.from_user.id

        is_admin = False
        try:
            admins = await bot.get_chat_administrators(chat_id)
            is_admin = any(adm.user.id == caller_id for adm in admins)
        except Exception:
            pass

        success, msg, old_m, new_m = await db.replace_duty(chat_id, caller_id, is_admin=is_admin)
        if not success:
            await callback.answer(msg, show_alert=True)
            return

        old_name = old_m.get("full_name") if old_m else "Предыдущий"
        new_name = new_m.get("full_name") if new_m else "Новый"
        new_tg_id = new_m.get("telegram_id") if new_m else None
        mention = f"<a href='tg://user?id={new_tg_id}'>{new_name}</a>" if new_tg_id else f"<b>{new_name}</b>"

        text = (
            f"🔄 <b>Произведена замена ответственного за кубарь!</b>\n\n"
            f"❌ Снят с дежурства: <s>{old_name}</s>\n"
            f"✅ Заступил на кубарь: {mention}\n\n"
            f"<i>Назначений у нового ответственного: {new_m.get('duty_count', 1)}</i>"
        )
        is_group = callback.message.chat.type in ("group", "supergroup")
        kb = await build_panel_keyboard(chat_id, is_group=is_group)

        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)

        await callback.answer("Замена успешно выполнена!")

    # 4. Команда /replace
    @dp.message(Command("replace"))
    async def cmd_replace(message: Message) -> None:
        chat_id = message.chat.id
        caller_id = message.from_user.id
        is_admin = False
        try:
            admins = await bot.get_chat_administrators(chat_id)
            is_admin = any(adm.user.id == caller_id for adm in admins)
        except Exception:
            pass

        success, msg, old_m, new_m = await db.replace_duty(chat_id, caller_id, is_admin=is_admin)
        if not success:
            await message.reply(f"⚠️ {msg}")
            return

        new_name = new_m.get("full_name")
        new_tg_id = new_m.get("telegram_id")
        mention = f"<a href='tg://user?id={new_tg_id}'>{new_name}</a>" if new_tg_id else f"<b>{new_name}</b>"

        text = (
            f"🔄 <b>Произведена замена ответственного за кубарь!</b>\n\n"
            f"❌ Снят: <s>{old_m.get('full_name')}</s>\n"
            f"✅ Новый ответственный: {mention}\n"
            f"Смен у бойца: {new_m.get('duty_count', 1)}"
        )
        await message.reply(text)

    # 5. Команда /rotate или /next (смена сдана / передать дежурство следующему)
    @dp.message(Command("rotate", "next"))
    async def cmd_rotate_duty(message: Message) -> None:
        chat_id = message.chat.id
        new_duty = await db.advance_duty_queue(chat_id)
        if not new_duty:
            await message.reply("⚠️ Не удалось передать смену: очередь дежурных пуста.")
            return

        name = new_duty["full_name"]
        tg_id = new_duty.get("telegram_id")
        mention = f"<a href='tg://user?id={tg_id}'>{name}</a>" if tg_id else f"<b>{name}</b>"

        await message.answer(
            f"✅ <b>Смена сдана! Новый ответственный за кубарь:</b>\n"
            f"👤 {mention} (№{new_duty.get('order_num', 1)} в очереди)\n"
            f"Смен всего: {new_duty.get('duty_count', 1)}"
        )

    # 6. Команда /start
    @dp.message(CommandStart())
    async def cmd_start(message: Message, command: CommandObject) -> None:
        chat = message.chat
        is_group = chat.type in ("group", "supergroup")

        target_chat_id = chat.id
        # Поддержка перехода из группы в ЛС: /start chat_-100...
        if not is_group and command.args:
            arg = command.args.strip()
            if arg.startswith("chat_"):
                try:
                    target_chat_id = int(arg.replace("chat_", ""))
                except ValueError:
                    pass

        kb = await build_panel_keyboard(target_chat_id, is_group=is_group)

        if is_group:
            text = (
                "👋 <b>Бот кубаря и учёта увалов готов к работе!</b>\n\n"
                "📌 <b>Основные возможности:</b>\n"
                "• <code>/c</code> — Кто <b>сегодня</b> и <b>завтра</b> ответственный за кубарь\n"
                "• <b>+увал</b> / <b>-увал</b> в ответ на сообщение — начисление и списание увалов\n"
                "• <code>/list</code> — Очередь дежурных и редактирование состава\n"
                "• <code>/stats</code> — Рейтинг роты по увалам и сменам\n"
                "• <code>/replace</code> — Быстрая замена дежурного\n\n"
                "Нажмите кнопку ниже для открытия веб-панели управления:"
            )
        else:
            text = (
                "👋 <b>Привет! Я единый бот кубаря и учёта увольнений «Duty Pass».</b>\n\n"
                "🎖 <b>Увалы:</b> начисление и списание прямо в чате через <code>+увал</code> и <code>-увал</code> в ответ на сообщения бойцов.\n"
                "🧹 <b>Кубарь:</b> автоматический график дежурств по бланку, быстрый просмотр дежурных на сегодня и завтра через <code>/c</code>.\n"
                "📱 <b>Веб-приложение:</b> удобная drag-and-drop сортировка очереди, переключатели статуса «В строю», аналитика и журнал событий.\n\n"
                "<i>Добавьте меня в группу кубаря и сделайте администратором для автоматической работы.</i>"
            )

        await message.answer(text, reply_markup=kb)

    # 7. Команда /list (настройка очереди в WebApp)
    @dp.message(Command("list", "queue", "kubar"))
    async def cmd_list(message: Message) -> None:
        chat_id = message.chat.id
        is_group = message.chat.type in ("group", "supergroup")
        kb = await build_panel_keyboard(chat_id, is_group=is_group)

        await message.answer(
            "📋 <b>Очередь ответственных за кубарь</b>\n\n"
            "Нажмите кнопку ниже, чтобы открыть панель, настроить порядок бойцов по бланку или добавить новых участников:",
            reply_markup=kb
        )

    # 8. Команда /stats (рейтинг увалов и дежурств)
    @dp.message(Command("stats", "uval", "uvals"))
    async def cmd_stats(message: Message) -> None:
        chat_id = message.chat.id
        summary = await db.get_stats_summary(chat_id)
        members = summary.get("top_uvals", [])

        if not members:
            await message.answer(
                "📊 <b>Статистика кубаря</b>\n\n"
                "Список личного состава пока пуст.\n"
                "Напишите сообщения в чат или добавьте бойцов через команду /list."
            )
            return

        lines = [
            "📊 <b>Сводка личного состава</b>",
            f"👥 Всего бойцов: <b>{summary['total_members']}</b> (в строю: <b>{summary['active_members']}</b>)",
            f"🎖 Всего увалов: <b>{summary['total_uvals']}</b> | 🧹 Дежурств: <b>{summary['total_duties']}</b>\n",
            "🏆 <b>Топ бойцов по увалам:</b>"
        ]

        for idx, m in enumerate(members[:10], start=1):
            badge = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
            name = m["full_name"]
            uname = f" (@{m['username']})" if m.get("username") else ""
            uvals = m["uval_count"]
            limit = m.get("max_limit", 5)
            filled = min(uvals, limit)
            bar = "▮" * filled + "▯" * max(0, limit - filled)
            lines.append(f"{badge} <b>{name}</b>{uname} — <b>{uvals}</b> ув. [{bar}] (смен: {m.get('duty_count', 0)})")

        is_group = message.chat.type in ("group", "supergroup")
        kb = await build_panel_keyboard(chat_id, is_group=is_group)
        await message.answer("\n".join(lines), reply_markup=kb)

    # 9. Команда /url (настройка адреса WebApp на лету)
    @dp.message(Command("url"))
    async def cmd_url(message: Message, command: CommandObject) -> None:
        chat_id = message.chat.id
        if command.args and command.args.strip().startswith("http"):
            new_url = command.args.strip().rstrip("/")
            await db.update_chat_url(chat_id, new_url)
            is_group = message.chat.type in ("group", "supergroup")
            kb = await build_panel_keyboard(chat_id, is_group=is_group)
            await message.reply(
                f"✅ <b>Адрес WebApp для данного чата обновлен:</b>\n<code>{new_url}</code>",
                reply_markup=kb
            )
        else:
            curr = await db.get_effective_webapp_url(chat_id)
            await message.reply(
                f"Текущий адрес WebApp: <code>{curr or 'не задан'}</code>\n\n"
                "Чтобы изменить: <code>/url https://ваш-домен.amvera.io</code>"
            )

    # 10. Команда /help
    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        text = (
            "📖 <b>Справочник команд бота:</b>\n\n"
            "🧹 <b>Дежурство по кубарю:</b>\n"
            "• <code>/c</code> — Кто <b>сегодня</b> и <b>завтра</b> ответственный за кубарь\n"
            "• <code>/list</code> — Открыть WebApp для настройки очереди и порядка бойцов\n"
            "• <code>/replace</code> — Запросить замену сегодняшнего дежурного\n"
            "• <code>/rotate</code> — Сдать смену и передать дежурство следующему\n\n"
            "🎖 <b>Учёт увалов:</b>\n"
            "• <code>+увал</code> или <code>-увал</code> — в ответ (Reply) на сообщение бойца\n"
            "• <code>+2 увала</code>, <code>-1 увал</code> — начисление/списание любого числа\n"
            "• <code>/stats</code> — Сводка и рейтинг бойцов по увалам и сменам\n\n"
            "⚙️ <b>Настройки:</b>\n"
            "• <code>/url https://...</code> — Обновить домен веб-приложения"
        )
        await message.answer(text)


# --- Фоновые авто-оповещения по расписанию (APScheduler) ---

async def send_scheduled_duty_announcement(slot_label: str) -> None:
    """Утренняя и вечерняя рассылка графика дежурных во все подключенные группы."""
    if not bot:
        return

    chats = await db.get_all_chats()
    for c in chats:
        chat_id = c["chat_id"]
        duty_info = await db.get_duty_today_and_tomorrow(chat_id)
        today = duty_info.get("today")
        tomorrow = duty_info.get("tomorrow")

        if not today:
            continue

        today_name = f"<a href='tg://user?id={today['telegram_id']}'>{today['full_name']}</a>" if today.get("telegram_id") else f"<b>{today['full_name']}</b>"
        tomorrow_name = f"<a href='tg://user?id={tomorrow['telegram_id']}'>{tomorrow['full_name']}</a>" if (tomorrow and tomorrow.get("telegram_id")) else (f"<b>{tomorrow['full_name']}</b>" if tomorrow else "<i>Не назначен</i>")

        text = (
            f"📋 <b>Назначение ответственного за кубарь</b>\n"
            f"⏰ Слот: <b>{slot_label}</b>\n\n"
            f"👤 Ответственный сегодня: {today_name} (№{today.get('order_num', 1)})\n"
            f"👤 Ответственный завтра: {tomorrow_name}\n\n"
            f"<i>Если требуется замена — воспользуйтесь кнопкой ниже.</i>"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Запросить замену", callback_data="btn_replace_duty")
        ]])

        try:
            sent_msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=kb,
                disable_notification=False
            )
            with suppress(Exception):
                await bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id)
        except Exception as e:
            logger.warning(f"Ошибка рассылки в чат {chat_id}: {e}")


def init_scheduler():
    tz = pytz.timezone(config.DEFAULT_TIMEZONE)
    scheduler.add_job(
        send_scheduled_duty_announcement,
        "cron",
        hour=config.MORNING_HOUR,
        minute=0,
        timezone=tz,
        args=["06:00 (Утренний осмотр)"]
    )
    scheduler.add_job(
        send_scheduled_duty_announcement,
        "cron",
        hour=config.EVENING_HOUR,
        minute=0,
        timezone=tz,
        args=["21:00 (Вечерняя поверка)"]
    )
    scheduler.start()
    logger.info("APScheduler успешно запущен (06:00 и 21:00).")


# --- FastAPI REST API ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    logger.info(f"SQLite БД инициализирована: {db.get_db_path()}")

    init_scheduler()

    polling_task = None
    if bot and dp:
        # Устанавливаем кнопку меню в Telegram для быстрого вызова WebApp
        try:
            effective_url = config.BASE_WEBAPP_URL or "https://amvera.io"
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="📱 Кубарь & Увалы",
                    web_app=WebAppInfo(url=f"{effective_url}/webapp")
                )
            )
        except Exception as e:
            logger.debug(f"Menu button set warning: {e}")

        polling_task = asyncio.create_task(
            dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                handle_signals=False,
            )
        )
        logger.info("Aiogram 3 Polling запущен.")
    else:
        logger.warning("BOT_TOKEN не задан, бот не запущен.")

    yield

    if polling_task:
        polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await polling_task
        if bot:
            await bot.session.close()
        logger.info("Aiogram Polling остановлен.")

    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Kubar & Uval Combined Bot API",
    version="2.0.0",
    description="Единая система управления ответственным за кубарь и учёта увольнений",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Pydantic модели ---

class MemberItem(BaseModel):
    id: Optional[int] = None
    telegram_id: Optional[int] = None
    full_name: str
    username: Optional[str] = None
    order_num: int
    is_active: int = 1


class SaveMembersRequest(BaseModel):
    chat_id: int
    members: List[MemberItem]


class UvalChangeRequest(BaseModel):
    chat_id: int
    member_id: Optional[int] = None
    user_id: Optional[int] = None
    delta: int
    reason: Optional[str] = None


class AddMemberRequest(BaseModel):
    chat_id: int
    full_name: str
    username: Optional[str] = None
    telegram_id: Optional[int] = None


class ReplaceDutyRequest(BaseModel):
    chat_id: int
    caller_id: Optional[int] = None


class AdvanceDutyRequest(BaseModel):
    chat_id: int


class ClearLogsRequest(BaseModel):
    chat_id: int


# --- API Эндпоинты ---

def parse_telegram_init_data(init_data: str) -> Optional[dict]:
    """Валидация данных Telegram WebApp при наличии токена."""
    if not init_data:
        return None
    try:
        parsed_data = dict(parse_qsl(init_data, strict_parsing=True))
        if "hash" not in parsed_data:
            return None
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed_data.items()) if k != "hash"
        )
        secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calc_hash == parsed_data["hash"]:
            if "user" in parsed_data:
                parsed_data["user"] = json.loads(parsed_data["user"])
            return parsed_data
    except Exception:
        pass
    return None


@app.get("/api/chat-data")
async def get_chat_data_endpoint(chat_id: Optional[int] = Query(0)):
    target_chat = chat_id or 0
    chat = await db.get_or_create_chat(target_chat)
    members = await db.get_members(target_chat)
    known = await db.get_known_users_for_chat(target_chat)
    duty_info = await db.get_duty_today_and_tomorrow(target_chat)
    stats = await db.get_stats_summary(target_chat)

    return {
        "status": "ok",
        "chat": chat,
        "today_duty": duty_info.get("today"),
        "tomorrow_duty": duty_info.get("tomorrow"),
        "forecast_7days": duty_info.get("forecast_7days", []),
        "members": members,
        "known_users": known,
        "stats": stats
    }


@app.post("/api/save-members")
async def save_members_endpoint(payload: SaveMembersRequest):
    data = [m.model_dump() for m in payload.members]
    await db.save_members_list(payload.chat_id, data)
    return {"status": "ok"}


@app.post("/api/uval")
async def change_uval_endpoint(payload: UvalChangeRequest):
    if payload.member_id:
        res = await db.change_uval_by_member_id(
            chat_id=payload.chat_id,
            member_id=payload.member_id,
            delta=payload.delta,
            reason=payload.reason
        )
        if not res:
            raise HTTPException(status_code=404, detail="Боец не найден")
        new_count, full_name = res
    elif payload.user_id:
        res = await db.change_uval(
            chat_id=payload.chat_id,
            telegram_id=payload.user_id,
            delta=payload.delta,
            reason=payload.reason
        )
        if not res:
            raise HTTPException(status_code=403, detail="Боец в чёрном списке")
        new_count, full_name, _ = res
    else:
        raise HTTPException(status_code=400, detail="Не указан member_id или user_id")

    return {
        "status": "ok",
        "new_count": new_count,
        "full_name": full_name
    }


@app.post("/api/members/add")
async def add_member_endpoint(payload: AddMemberRequest):
    new_m = await db.add_member(
        chat_id=payload.chat_id,
        full_name=payload.full_name,
        username=payload.username,
        telegram_id=payload.telegram_id
    )
    return {"status": "ok", "member": new_m}


@app.delete("/api/members/{member_id}")
async def delete_member_endpoint(member_id: int, chat_id: Optional[int] = Query(0)):
    await db.delete_member(chat_id or 0, member_id)
    return {"status": "ok", "member_id": member_id}


@app.post("/api/duty/replace")
async def replace_duty_endpoint(payload: ReplaceDutyRequest, request: Request):
    caller_id = payload.caller_id or 0
    # Проверка Telegram initData, если есть
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_auth = parse_telegram_init_data(init_data)
    if tg_auth and "user" in tg_auth:
        caller_id = tg_auth["user"].get("id", caller_id)

    success, msg, old_m, new_m = await db.replace_duty(payload.chat_id, caller_id, is_admin=True)
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    return {
        "status": "ok",
        "message": msg,
        "old_member": old_m,
        "new_member": new_m
    }


@app.post("/api/duty/advance")
async def advance_duty_endpoint(payload: AdvanceDutyRequest):
    new_duty = await db.advance_duty_queue(payload.chat_id)
    if not new_duty:
        raise HTTPException(status_code=400, detail="Очередь пуста")
    return {"status": "ok", "new_duty": new_duty}


@app.get("/api/logs")
async def get_logs_endpoint(chat_id: Optional[int] = Query(0)):
    logs = await db.get_logs(chat_id or 0, limit=50)
    return {"status": "ok", "logs": logs}


@app.post("/api/logs/clear")
async def clear_logs_endpoint(payload: ClearLogsRequest):
    await db.clear_logs(payload.chat_id)
    return {"status": "ok"}


# --- Раздача статики WebApp ---

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
@app.get("/webapp")
@app.get("/webapp/")
@app.get("/index.html")
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    return FileResponse(str(index_file))

@app.get("/style.css")
@app.get("/webapp/style.css")
async def serve_css():
    css_file = STATIC_DIR / "style.css"
    return FileResponse(str(css_file), media_type="text/css")

@app.get("/app.js")
@app.get("/webapp/app.js")
async def serve_js():
    js_file = STATIC_DIR / "app.js"
    return FileResponse(str(js_file), media_type="application/javascript")


if __name__ == "__main__":
    logger.info(f"Запуск объединенного сервера на http://{config.HOST}:{config.PORT}")
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False)
