import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import aiosqlite

import config


def get_db_path() -> str:
    return config.DB_PATH


async def init_db() -> None:
    """Инициализация таблиц базы данных, индексов и миграция старых данных при необходимости."""
    db_file = get_db_path()
    parent_dir = os.path.dirname(db_file)
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    async with aiosqlite.connect(db_file) as db:
        try:
            await db.execute("PRAGMA journal_mode = WAL;")
        except Exception:
            pass
        await db.execute("PRAGMA foreign_keys = ON;")

        # 1. Чаты рот/кубарей
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                timezone TEXT DEFAULT 'Europe/Moscow',
                current_index INTEGER DEFAULT 0,
                webapp_url TEXT,
                morning_notify INTEGER DEFAULT 1,
                evening_notify INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # 2. Единый список личного состава кубаря (увалы + очередь дежурств)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                telegram_id INTEGER,
                full_name TEXT NOT NULL,
                username TEXT,
                order_num INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                uval_count INTEGER NOT NULL DEFAULT 0,
                max_limit INTEGER NOT NULL DEFAULT 5,
                duty_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # 3. Назначения дежурств по кубарю
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS duties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                slot TEXT NOT NULL,
                assigned_date DATE NOT NULL,
                status TEXT DEFAULT 'active',
                replaced_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(chat_id) REFERENCES chats(chat_id),
                FOREIGN KEY(member_id) REFERENCES members(id)
            );
            """
        )

        # 4. Известные участники беседы (для удобного добавления в WebApp)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS known_users (
                chat_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                username TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(chat_id, telegram_id)
            );
            """
        )

        # 5. Черный список
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                chat_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, telegram_id)
            );
            """
        )

        # 6. Журнал действий (логи)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                action TEXT NOT NULL,
                delta INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                details TEXT,
                created_at TEXT NOT NULL
            );
            """
        )

        # Индексы для быстрой выборки
        await db.execute("CREATE INDEX IF NOT EXISTS idx_members_chat ON members (chat_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_members_tg ON members (chat_id, telegram_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_duties_chat_date ON duties (chat_id, assigned_date);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_logs_chat ON logs (chat_id);")

        await db.commit()

        # Автоматическая миграция со старых баз данных, если они существуют на томе /data
        await _auto_migrate_legacy_data(db)


async def _auto_migrate_legacy_data(db: Any) -> None:
    """Безопасный перенос данных из старых баз uvals.db и duty_service.db, если они есть."""
    try:
        # Проверяем, пуста ли таблица members
        async with db.execute("SELECT COUNT(*) FROM members;") as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0

        if count > 0:
            return  # Данные уже есть, миграция не требуется

        # 1. Попытка переноса из uvals.db
        legacy_uval = "/data/uvals.db" if os.path.exists("/data/uvals.db") else "uvals.db"
        if os.path.exists(legacy_uval) and os.path.abspath(legacy_uval) != os.path.abspath(get_db_path()):
            try:
                async with aiosqlite.connect(legacy_uval) as old_db:
                    old_db.row_factory = aiosqlite.Row
                    async with old_db.execute("SELECT * FROM users") as cur:
                        users = await cur.fetchall()
                        for idx, u in enumerate(users, start=1):
                            tg_id = u["user_id"] if u["user_id"] > 0 else None
                            await db.execute(
                                """
                                INSERT INTO members (chat_id, telegram_id, full_name, username, order_num, is_active, uval_count, max_limit)
                                VALUES (?, ?, ?, ?, ?, 1, ?, ?);
                                """,
                                (u["chat_id"], tg_id, u["full_name"], u["username"], idx, u["uval_count"], u.get("max_limit", 5))
                            )
                    await db.commit()
            except Exception as e:
                print(f"[Migration] uvals.db migration skipped: {e}")

        # 2. Попытка переноса из duty_service.db
        legacy_duty = "/data/duty_service.db" if os.path.exists("/data/duty_service.db") else "duty_service.db"
        if os.path.exists(legacy_duty) and os.path.abspath(legacy_duty) != os.path.abspath(get_db_path()):
            try:
                async with aiosqlite.connect(legacy_duty) as old_db:
                    old_db.row_factory = aiosqlite.Row
                    async with old_db.execute("SELECT * FROM chats") as cur:
                        chats = await cur.fetchall()
                        for c in chats:
                            await db.execute(
                                "INSERT OR IGNORE INTO chats (chat_id, title, timezone, current_index) VALUES (?, ?, ?, ?)",
                                (c["chat_id"], c["title"], c.get("timezone", "Europe/Moscow"), c.get("current_index", 0))
                            )
                    async with old_db.execute("SELECT * FROM known_users") as cur:
                        known = await cur.fetchall()
                        for k in known:
                            await db.execute(
                                "INSERT OR IGNORE INTO known_users (chat_id, telegram_id, full_name) VALUES (?, ?, ?)",
                                (k["chat_id"], k["telegram_id"], k["full_name"])
                            )
                    await db.commit()
            except Exception as e:
                print(f"[Migration] duty_service.db migration skipped: {e}")

    except Exception as e:
        print(f"[Migration] General error: {e}")


# --- Управление чатами ---

async def get_or_create_chat(chat_id: int, title: Optional[str] = None) -> Dict[str, Any]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM chats WHERE chat_id = ?;", (chat_id,)) as cur:
            chat = await cur.fetchone()

        if chat:
            if title and chat["title"] != title:
                await db.execute("UPDATE chats SET title = ? WHERE chat_id = ?;", (title, chat_id))
                await db.commit()
            return dict(chat)

        default_title = title or f"Кубарь {chat_id}"
        await db.execute(
            """
            INSERT INTO chats (chat_id, title, timezone, current_index)
            VALUES (?, ?, 'Europe/Moscow', 0);
            """,
            (chat_id, default_title)
        )
        await db.commit()
        return {
            "chat_id": chat_id,
            "title": default_title,
            "timezone": "Europe/Moscow",
            "current_index": 0,
            "webapp_url": None,
            "morning_notify": 1,
            "evening_notify": 1
        }


async def update_chat_url(chat_id: int, url: str) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO chats (chat_id, title, webapp_url) VALUES (?, 'Кубарь', ?)
            ON CONFLICT (chat_id) DO UPDATE SET webapp_url = excluded.webapp_url;
            """,
            (chat_id, url)
        )
        await db.commit()


async def get_effective_webapp_url(chat_id: int) -> str:
    """Возвращает URL панели для чата: сохранённый адрес или глобальный BASE_WEBAPP_URL."""
    async with aiosqlite.connect(get_db_path()) as db:
        async with db.execute("SELECT webapp_url FROM chats WHERE chat_id = ?;", (chat_id,)) as cur:
            row = await cur.fetchone()
            if row and row[0]:
                return row[0].rstrip("/")
    return config.BASE_WEBAPP_URL


async def get_all_chats() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM chats ORDER BY title ASC;") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# --- Черный список ---

async def is_blacklisted(chat_id: int, telegram_id: int) -> bool:
    if not telegram_id or telegram_id <= 0:
        return False
    async with aiosqlite.connect(get_db_path()) as db:
        async with db.execute(
            "SELECT 1 FROM blacklist WHERE chat_id = ? AND telegram_id = ?;",
            (chat_id, telegram_id)
        ) as cur:
            return (await cur.fetchone()) is not None


async def ban_user(chat_id: int, telegram_id: int) -> bool:
    now_str = datetime.now().strftime("%d.%m %H:%M")
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO blacklist (chat_id, telegram_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, telegram_id) DO NOTHING;
            """,
            (chat_id, telegram_id, now_str)
        )
        await db.execute(
            "DELETE FROM members WHERE chat_id = ? AND telegram_id = ?;",
            (chat_id, telegram_id)
        )
        await db.commit()
        return True


# --- Участники (Members) ---

async def save_known_user(chat_id: int, telegram_id: int, full_name: str, username: Optional[str] = None) -> None:
    if not telegram_id or telegram_id <= 0:
        return
    clean_name = full_name.strip()
    clean_username = username.strip().lstrip("@") if username else None

    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO known_users (chat_id, telegram_id, full_name, username, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, telegram_id) DO UPDATE SET
                full_name = excluded.full_name,
                username = COALESCE(excluded.username, known_users.username),
                updated_at = CURRENT_TIMESTAMP;
            """,
            (chat_id, telegram_id, clean_name, clean_username)
        )
        # Также обновляем в members, если боец уже есть
        await db.execute(
            """
            UPDATE members SET full_name = ?, username = COALESCE(?, username)
            WHERE chat_id = ? AND telegram_id = ?;
            """,
            (clean_name, clean_username, chat_id, telegram_id)
        )
        await db.commit()


async def get_members(chat_id: int) -> List[Dict[str, Any]]:
    """Получение всех бойцов кубаря, отсортированных по очередности (order_num)."""
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT m.*
            FROM members m
            WHERE (m.chat_id = ? OR ? = 0)
              AND NOT EXISTS (
                  SELECT 1 FROM blacklist b
                  WHERE b.chat_id = m.chat_id AND b.telegram_id = m.telegram_id
              )
            ORDER BY m.order_num ASC, m.full_name ASC;
            """,
            (chat_id, chat_id)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_active_members(chat_id: int) -> List[Dict[str, Any]]:
    """Получение активных бойцов (в строю) по порядку очереди."""
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT m.*
            FROM members m
            WHERE m.chat_id = ? AND m.is_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM blacklist b
                  WHERE b.chat_id = m.chat_id AND b.telegram_id = m.telegram_id
              )
            ORDER BY m.order_num ASC;
            """,
            (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_member_by_telegram_id(chat_id: int, telegram_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM members WHERE chat_id = ? AND telegram_id = ? LIMIT 1;",
            (chat_id, telegram_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_member_by_id(member_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM members WHERE id = ?;", (member_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def add_member(
    chat_id: int,
    full_name: str,
    username: Optional[str] = None,
    telegram_id: Optional[int] = None
) -> Dict[str, Any]:
    """Добавление нового бойца в конец очереди кубаря."""
    clean_name = full_name.strip()
    clean_username = username.strip().lstrip("@") if username else None

    async with aiosqlite.connect(get_db_path()) as db:
        # Вычисляем максимальный order_num в чате
        async with db.execute("SELECT COALESCE(MAX(order_num), 0) FROM members WHERE chat_id = ?;", (chat_id,)) as cur:
            max_order = (await cur.fetchone())[0]

        new_order = max_order + 1

        cursor = await db.execute(
            """
            INSERT INTO members (chat_id, telegram_id, full_name, username, order_num, is_active, uval_count, max_limit, duty_count)
            VALUES (?, ?, ?, ?, ?, 1, 0, 5, 0);
            """,
            (chat_id, telegram_id, clean_name, clean_username, new_order)
        )
        new_id = cursor.lastrowid

        # Логируем добавление
        now_str = datetime.now().strftime("%d.%m %H:%M")
        await db.execute(
            """
            INSERT INTO logs (chat_id, user_id, full_name, action, delta, new_count, details, created_at)
            VALUES (?, ?, ?, 'member_added', 0, 0, 'Добавлен в список кубаря', ?);
            """,
            (chat_id, telegram_id or 0, clean_name, now_str)
        )

        await db.commit()

        return {
            "id": new_id,
            "chat_id": chat_id,
            "telegram_id": telegram_id,
            "full_name": clean_name,
            "username": clean_username,
            "order_num": new_order,
            "is_active": 1,
            "uval_count": 0,
            "max_limit": 5,
            "duty_count": 0
        }


async def delete_member(chat_id: int, member_id: int) -> bool:
    """Удаление бойца и автоматический пересчет порядковых номеров."""
    async with aiosqlite.connect(get_db_path()) as db:
        # Получаем данные бойца для лога
        async with db.execute("SELECT full_name, telegram_id FROM members WHERE id = ? AND chat_id = ?;", (member_id, chat_id)) as cur:
            row = await cur.fetchone()
            name = row[0] if row else "Боец"
            tg_id = row[1] if row else 0

        await db.execute("DELETE FROM duties WHERE member_id = ?;", (member_id,))
        await db.execute("DELETE FROM members WHERE id = ? AND chat_id = ?;", (member_id, chat_id))

        # Пересчитываем order_num
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM members WHERE chat_id = ? ORDER BY order_num ASC;", (chat_id,)) as cur:
            remaining = await cur.fetchall()

        for idx, r in enumerate(remaining, start=1):
            await db.execute("UPDATE members SET order_num = ? WHERE id = ?;", (idx, r["id"]))

        now_str = datetime.now().strftime("%d.%m %H:%M")
        await db.execute(
            """
            INSERT INTO logs (chat_id, user_id, full_name, action, delta, new_count, details, created_at)
            VALUES (?, ?, ?, 'member_deleted', 0, 0, 'Удален из состава', ?);
            """,
            (chat_id, tg_id or 0, name, now_str)
        )

        await db.commit()
        return True


async def save_members_list(chat_id: int, members_data: List[Dict[str, Any]]) -> None:
    """Полное сохранение переупорядоченного списка бойцов из WebApp."""
    async with aiosqlite.connect(get_db_path()) as db:
        # Считываем текущие счетчики, чтобы не затереть uval_count и duty_count
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, uval_count, max_limit, duty_count FROM members WHERE chat_id = ?;", (chat_id,)) as cur:
            existing = {r["id"]: dict(r) for r in await cur.fetchall()}

        # Обрабатываем каждого переданного бойца
        current_ids = set()
        for idx, m in enumerate(members_data, start=1):
            m_id = m.get("id")
            tg_id = m.get("telegram_id")
            full_name = m.get("full_name", "").strip()
            username = m.get("username", "").strip().lstrip("@") if m.get("username") else None
            is_active = 1 if m.get("is_active", 1) else 0

            if m_id and m_id in existing:
                # Обновление существующего
                current_ids.add(m_id)
                await db.execute(
                    """
                    UPDATE members SET
                        order_num = ?,
                        full_name = ?,
                        username = COALESCE(?, username),
                        telegram_id = COALESCE(?, telegram_id),
                        is_active = ?
                    WHERE id = ?;
                    """,
                    (idx, full_name, username, tg_id, is_active, m_id)
                )
            else:
                # Создание нового
                cur = await db.execute(
                    """
                    INSERT INTO members (chat_id, telegram_id, full_name, username, order_num, is_active, uval_count, max_limit, duty_count)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 5, 0);
                    """,
                    (chat_id, tg_id, full_name, username, idx, is_active)
                )
                current_ids.add(cur.lastrowid)

        # Удаляем бойцов, которых удалили из списка в веб-приложении
        to_delete = [mid for mid in existing if mid not in current_ids]
        for mid in to_delete:
            await db.execute("DELETE FROM duties WHERE member_id = ?;", (mid,))
            await db.execute("DELETE FROM members WHERE id = ?;", (mid,))

        await db.commit()


async def get_known_users_for_chat(chat_id: int) -> List[Dict[str, Any]]:
    """Возвращает участников чата, которых еще нет в списке бойцов."""
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT k.telegram_id, k.full_name, k.username
            FROM known_users k
            WHERE k.chat_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM members m WHERE m.chat_id = k.chat_id AND m.telegram_id = k.telegram_id
              )
            ORDER BY k.full_name ASC;
            """,
            (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# --- Учет увольнений (Увалы) ---

async def change_uval(
    chat_id: int,
    telegram_id: int,
    delta: int,
    fallback_name: Optional[str] = None,
    username: Optional[str] = None,
    reason: Optional[str] = None
) -> Optional[Tuple[int, str, int]]:
    """
    Начисление или списание увала бойцу (+1, -1 и т.д.).
    Возвращает: (новый_остаток, ФИО, member_id).
    """
    if await is_blacklisted(chat_id, telegram_id):
        return None

    now_str = datetime.now().strftime("%d.%m %H:%M")

    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row

        # Поиск по telegram_id в чате
        async with db.execute(
            "SELECT id, full_name, uval_count FROM members WHERE chat_id = ? AND telegram_id = ? LIMIT 1;",
            (chat_id, telegram_id)
        ) as cur:
            row = await cur.fetchone()

        if row:
            member_id = row["id"]
            full_name = row["full_name"]
            curr_count = row["uval_count"]
        else:
            # Если бойца еще нет, регистрируем его в составе
            full_name = fallback_name.strip() if fallback_name else f"Боец #{telegram_id}"
            curr_count = 0

            async with db.execute("SELECT COALESCE(MAX(order_num), 0) FROM members WHERE chat_id = ?;", (chat_id,)) as o_cur:
                max_order = (await o_cur.fetchone())[0]

            cursor = await db.execute(
                """
                INSERT INTO members (chat_id, telegram_id, full_name, username, order_num, is_active, uval_count, max_limit, duty_count)
                VALUES (?, ?, ?, ?, ?, 1, 0, 5, 0);
                """,
                (chat_id, telegram_id, full_name, username, max_order + 1)
            )
            member_id = cursor.lastrowid

        new_count = max(0, curr_count + delta)

        await db.execute(
            "UPDATE members SET uval_count = ? WHERE id = ?;",
            (new_count, member_id)
        )

        details = reason if reason else ("Начисление увала" if delta > 0 else "Списание увала")
        await db.execute(
            """
            INSERT INTO logs (chat_id, user_id, full_name, action, delta, new_count, details, created_at)
            VALUES (?, ?, ?, 'uval_change', ?, ?, ?, ?);
            """,
            (chat_id, telegram_id, full_name, delta, new_count, details, now_str)
        )

        await db.commit()
        return new_count, full_name, member_id


async def change_uval_by_member_id(
    chat_id: int,
    member_id: int,
    delta: int,
    reason: Optional[str] = None
) -> Optional[Tuple[int, str]]:
    """Изменение увала напрямую по member_id (из WebApp)."""
    now_str = datetime.now().strftime("%d.%m %H:%M")

    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT full_name, telegram_id, uval_count FROM members WHERE id = ? AND (chat_id = ? OR ? = 0);",
            (member_id, chat_id, chat_id)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        full_name = row["full_name"]
        telegram_id = row["telegram_id"] or 0
        curr_count = row["uval_count"]
        new_count = max(0, curr_count + delta)

        await db.execute("UPDATE members SET uval_count = ? WHERE id = ?;", (new_count, member_id))

        details = reason if reason else ("Изменение в WebApp" + (f" (+{delta})" if delta > 0 else f" ({delta})"))
        await db.execute(
            """
            INSERT INTO logs (chat_id, user_id, full_name, action, delta, new_count, details, created_at)
            VALUES (?, ?, ?, 'uval_change', ?, ?, ?, ?);
            """,
            (chat_id, telegram_id, full_name, delta, new_count, details, now_str)
        )

        await db.commit()
        return new_count, full_name


# --- Дежурства по кубарю (Duty) ---

async def get_current_duty_record(chat_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает последнюю активную запись дежурства из таблицы duties."""
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT d.id AS duty_id, d.slot, d.assigned_date, d.status,
                   m.id AS member_id, m.full_name, m.telegram_id, m.username, m.duty_count, m.order_num
            FROM duties d
            JOIN members m ON d.member_id = m.id
            WHERE d.chat_id = ? AND d.status = 'active'
            ORDER BY d.id DESC LIMIT 1;
            """,
            (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_duty_today_and_tomorrow(chat_id: int) -> Dict[str, Any]:
    """
    Главная функция для определения ответственных на СЕГОДНЯ и ЗАВТРА.
    Учитывает активную запись дежурства, порядок очереди (order_num) и пропуск неактивных бойцов.
    """
    chat = await get_or_create_chat(chat_id)
    active_members = await get_active_members(chat_id)

    if not active_members:
        return {
            "today": None,
            "tomorrow": None,
            "active_count": 0,
            "total_count": len(await get_members(chat_id)),
            "forecast_7days": []
        }

    today_str = datetime.now().strftime("%Y-%m-%d")
    current_duty = await get_current_duty_record(chat_id)

    today_member = None
    today_index = 0

    # Если на сегодня уже зафиксировано активное дежурство
    if current_duty and current_duty.get("assigned_date") == today_str:
        # Ищем этого бойца среди активных
        match_idx = next((i for i, m in enumerate(active_members) if m["id"] == current_duty["member_id"]), None)
        if match_idx is not None:
            today_index = match_idx
            today_member = active_members[match_idx]
        else:
            today_member = {
                "id": current_duty["member_id"],
                "full_name": current_duty["full_name"],
                "telegram_id": current_duty["telegram_id"],
                "username": current_duty.get("username"),
                "order_num": current_duty.get("order_num", 1),
                "duty_count": current_duty["duty_count"],
                "is_active": 1
            }
            today_index = chat.get("current_index", 0) % len(active_members)
    else:
        # Если дежурство еще не создано на сегодня, берем по current_index
        today_index = chat.get("current_index", 0) % len(active_members)
        today_member = active_members[today_index]

    # Завтра — следующий активный боец по очереди
    tomorrow_index = (today_index + 1) % len(active_members)
    tomorrow_member = active_members[tomorrow_index]

    # Прогноз на 7 дней вперед
    forecast = []
    now = datetime.now()
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for day_offset in range(7):
        target_date = now + timedelta(days=day_offset)
        assigned_m = active_members[(today_index + day_offset) % len(active_members)]
        weekday_name = days_ru[target_date.weekday()]
        label = "Сегодня" if day_offset == 0 else ("Завтра" if day_offset == 1 else weekday_name)
        forecast.append({
            "date": target_date.strftime("%d.%m"),
            "full_date": target_date.strftime("%Y-%m-%d"),
            "day_name": label,
            "member_id": assigned_m["id"],
            "full_name": assigned_m["full_name"],
            "order_num": assigned_m["order_num"],
            "duty_count": assigned_m["duty_count"]
        })

    return {
        "today": today_member,
        "tomorrow": tomorrow_member,
        "active_count": len(active_members),
        "total_count": len(await get_members(chat_id)),
        "forecast_7days": forecast
    }


async def assign_duty(chat_id: int, member_id: int, slot_name: str, assigned_date: Optional[str] = None) -> None:
    """Запись назначения дежурного в базу данных."""
    date_str = assigned_date or datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%d.%m %H:%M")

    async with aiosqlite.connect(get_db_path()) as db:
        # Завершаем предыдущие активные дежурства
        await db.execute(
            "UPDATE duties SET status = 'completed' WHERE chat_id = ? AND status = 'active';",
            (chat_id,)
        )
        # Добавляем новое
        await db.execute(
            """
            INSERT INTO duties (chat_id, member_id, slot, assigned_date, status)
            VALUES (?, ?, ?, ?, 'active');
            """,
            (chat_id, member_id, slot_name, date_str)
        )
        # Увеличиваем счетчик дежурств бойца
        await db.execute("UPDATE members SET duty_count = duty_count + 1 WHERE id = ?;", (member_id,))

        # Получаем данные бойца для лога
        async with db.execute("SELECT full_name, telegram_id FROM members WHERE id = ?;", (member_id,)) as cur:
            row = await cur.fetchone()
            name = row[0] if row else "Боец"
            tg_id = row[1] if row else 0

        await db.execute(
            """
            INSERT INTO logs (chat_id, user_id, full_name, action, delta, new_count, details, created_at)
            VALUES (?, ?, ?, 'duty_assigned', 0, 0, ?, ?);
            """,
            (chat_id, tg_id or 0, name, f"Заступил ответственным за кубарь ({slot_name})", now_str)
        )

        await db.commit()


async def advance_duty_queue(chat_id: int) -> Optional[Dict[str, Any]]:
    """Переход очереди к следующему бойцу (смена сдана / следующий дежурный)."""
    active_members = await get_active_members(chat_id)
    if not active_members:
        return None

    chat = await get_or_create_chat(chat_id)
    curr_idx = chat.get("current_index", 0)
    next_idx = (curr_idx + 1) % len(active_members)
    new_duty = active_members[next_idx]

    today_str = datetime.now().strftime("%Y-%m-%d")
    slot_name = "Смена по графику"

    await assign_duty(chat_id, new_duty["id"], slot_name, today_str)

    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("UPDATE chats SET current_index = ? WHERE chat_id = ?;", (next_idx, chat_id))
        await db.commit()

    return new_duty


async def replace_duty(chat_id: int, caller_tg_id: int, is_admin: bool = False) -> Tuple[bool, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Замена ответственного за кубарь.
    Разрешено: назначенному бойцу или администратору чата.
    Выбирается активный боец с наименьшим количеством смен.
    """
    active_duty = await get_current_duty_record(chat_id)
    if not active_duty:
        # Если дежурство в таблице не зафиксировано, берем текущего по очереди
        today_info = await get_duty_today_and_tomorrow(chat_id)
        if not today_info.get("today"):
            return False, "Список дежурных пуст.", None, None
        curr_m = today_info["today"]
        curr_duty_id = None
        curr_member_id = curr_m["id"]
        slot = "Текущая смена"
        assigned_date = datetime.now().strftime("%Y-%m-%d")
    else:
        curr_m = active_duty
        curr_duty_id = active_duty["duty_id"]
        curr_member_id = active_duty["member_id"]
        slot = active_duty["slot"]
        assigned_date = active_duty["assigned_date"]

    # Проверка прав: сам дежурный или админ
    if curr_m.get("telegram_id") != caller_tg_id and not is_admin:
        return False, "Запросить замену может только назначенный ответственный или админ.", None, None

    # Поиск кандидата на замену (с минимальным duty_count, исключая текущего)
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM members
            WHERE chat_id = ? AND is_active = 1 AND id != ?
            ORDER BY duty_count ASC, order_num ASC
            LIMIT 1;
            """,
            (chat_id, curr_member_id)
        ) as cur:
            candidate = await cur.fetchone()

        if not candidate:
            return False, "Нет доступных участников в строю для замены.", None, None

        cand_dict = dict(candidate)

        # Выполняем замену
        if curr_duty_id:
            await db.execute(
                "UPDATE duties SET status = 'replaced', replaced_by_id = ? WHERE id = ?;",
                (cand_dict["id"], curr_duty_id)
            )

        await db.execute(
            """
            INSERT INTO duties (chat_id, member_id, slot, assigned_date, status)
            VALUES (?, ?, ?, ?, 'active');
            """,
            (chat_id, cand_dict["id"], slot, assigned_date)
        )

        # Корректируем duty_count
        await db.execute("UPDATE members SET duty_count = MAX(0, duty_count - 1) WHERE id = ?;", (curr_member_id,))
        await db.execute("UPDATE members SET duty_count = duty_count + 1 WHERE id = ?;", (cand_dict["id"],))

        now_str = datetime.now().strftime("%d.%m %H:%M")
        await db.execute(
            """
            INSERT INTO logs (chat_id, user_id, full_name, action, delta, new_count, details, created_at)
            VALUES (?, ?, ?, 'duty_replaced', 0, 0, ?, ?);
            """,
            (
                chat_id,
                cand_dict["telegram_id"] or 0,
                cand_dict["full_name"],
                f"Замена: {curr_m['full_name']} ➔ {cand_dict['full_name']}",
                now_str
            )
        )

        await db.commit()

    return True, "Замена успешно произведена!", curr_m, cand_dict


# --- Логи и статистика ---

async def get_logs(chat_id: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM logs
            WHERE (chat_id = ? OR ? = 0)
            ORDER BY id DESC
            LIMIT ?;
            """,
            (chat_id, chat_id, limit)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def clear_logs(chat_id: int) -> bool:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("DELETE FROM logs WHERE (chat_id = ? OR ? = 0);", (chat_id, chat_id))
        await db.commit()
        return True


async def get_stats_summary(chat_id: int = 0) -> Dict[str, Any]:
    """Сводная аналитика по роте/кубарю."""
    members = await get_members(chat_id)
    total_members = len(members)
    active_members = sum(1 for m in members if m["is_active"] == 1)
    total_uvals = sum(m["uval_count"] for m in members)
    total_duties = sum(m["duty_count"] for m in members)

    top_uvals = sorted(members, key=lambda x: x["uval_count"], reverse=True)[:10]
    top_duties = sorted(members, key=lambda x: x["duty_count"], reverse=True)[:10]

    return {
        "total_members": total_members,
        "active_members": active_members,
        "total_uvals": total_uvals,
        "total_duties": total_duties,
        "top_uvals": top_uvals,
        "top_duties": top_duties
    }
