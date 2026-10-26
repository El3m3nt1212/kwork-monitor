"""
SQLite-хранилище снапшотов биржи заказов Kwork.

Таблицы:
    orders  — по одной строке на order_id, текущее состояние (открыт/пропал).
    history — по одной строке на каждое наблюдение заказа в конкретном
              прогоне (для отслеживания динамики: число предложений,
              оставшееся время, % найма во времени).

Это даёт материал для вопросов из research-plan.md (п.1, п.9): как быстро
заказы пропадают, что этому предшествует (рост предложений/найма или просто
истечение срока).
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass

from config import DB_PATH
from scraper import ProjectCard, estimate_published_at

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY,
    title TEXT,
    url TEXT,
    description_snippet TEXT,
    buyer_username TEXT,
    keyword_queries TEXT,          -- через запятую: все ключевые слова, по которым встречался
    first_seen_at REAL,
    last_seen_at REAL,
    status TEXT,                   -- 'open' | 'gone'
    gone_at REAL,
    budget_desired INTEGER,
    budget_max INTEGER
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    scraped_at REAL,
    keyword_query TEXT,
    offers_count INTEGER,
    hired_percent INTEGER,
    time_left_text TEXT,
    buyer_projects_count INTEGER,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE INDEX IF NOT EXISTS idx_history_order ON history(order_id);

-- Сводка биржи за месяц из левой колонки страницы проектов: сколько проектов
-- размещено, на какую сумму, сколько из них стало заказами. Снимается раз в
-- прогон, отдельно от карточек.
CREATE TABLE IF NOT EXISTS exchange_stats (
    captured_at REAL PRIMARY KEY,
    projects_month INTEGER,
    amount_month INTEGER,
    orders_month INTEGER,
    connects_left INTEGER,
    connects_total INTEGER
);

-- Счётчики левой колонки ленты: сколько открытых проектов сейчас по рубрикам,
-- по бюджету и по числу уже собранных предложений. Один запрос даёт срез всей
-- биржи; ряд таких срезов — динамику, которую иначе пришлось бы собирать
-- обходом всех карточек.
CREATE TABLE IF NOT EXISTS feed_snapshot (
    captured_at REAL PRIMARY KEY,
    pages_total INTEGER,
    orders_seen INTEGER,
    complete INTEGER,              -- 1, если обойдены все страницы
    counters_json TEXT
);
"""

# Колонки, добавленные после первого боевого запуска. sqlite не умеет
# ADD COLUMN IF NOT EXISTS, поэтому просто пробуем и глотаем «уже есть».
MIGRATIONS = [
    "ALTER TABLE history ADD COLUMN time_left_minutes INTEGER",
    "ALTER TABLE history ADD COLUMN deadline_at REAL",
    "ALTER TABLE history ADD COLUMN feed_page INTEGER",
    "ALTER TABLE orders ADD COLUMN deadline_at REAL",
    "ALTER TABLE orders ADD COLUMN published_at REAL",
    "ALTER TABLE orders ADD COLUMN gone_reason TEXT",
]

# Допуск при ответе на вопрос «заказ дожил до дедлайна или исчез раньше».
# Складывается из округления самой площадки (±30 мин, пока показываются дни и
# часы) и шага расписания монитора (30 мин): пропажу мы замечаем не в момент,
# когда она произошла, а на следующем прогоне.
DEADLINE_TOLERANCE_SEC = 70 * 60

# Признак снятия пачкой — не общее число ранних пропаж в прогоне, а несколько
# заказов ОДНОГО покупателя, исчезнувших одновременно.
#
# Сначала здесь стоял порог «3 любых ранних пропажи за прогон», откалиброванный,
# когда монитор вёл 30 заказов по семи ключевым словам. После перехода на обход
# всей биржи (530 заказов, прогон раз в час) правило начало срабатывать на
# ровном месте: 2026-07-25 в 14:06 «пачкой» записались 6 пропаж шести РАЗНЫХ
# покупателей — то есть шесть независимых наймов, помеченных как «событие
# площадки». Это съедало ровно тот сигнал, ради которого монитор и работает.
# Настоящая пачка выглядит иначе: 2026-07-24 в 23:06 исчезли 6 заказов ДВУХ
# покупателей, по три почти одинаковых у каждого.
SAME_BUYER_BATCH_THRESHOLD = 2


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        conn.commit()


def classify_gone(gone_at: float, deadline_at: float | None) -> str:
    """Почему заказ исчез из ленты, насколько это видно по данным.

    'timeout'  — дожил до конца приёма предложений: исчез на дедлайне.
    'early'    — исчез заметно раньше дедлайна. По одной карточке различить
                 найм, отмену покупателем и снятие модерацией нельзя — на
                 карточке не остаётся никакого следа, а страница заказа после
                 исчезновения просто редиректит на список (проверено вручную
                 2026-07-25 на трёх ID). Разделять их приходится по косвенным
                 признакам, см. gone_reason='early_mass'.
    'unknown'  — дедлайн неизвестен (заказ ни разу не показал «Осталось»).
    """
    if deadline_at is None:
        return "unknown"
    return "timeout" if gone_at >= deadline_at - DEADLINE_TOLERANCE_SEC else "early"


def record_feed_snapshot(feed) -> None:
    """Сохраняет счётчики ленты одного обхода (см. scraper.FeedResult)."""
    init_db()
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO feed_snapshot
               (captured_at, pages_total, orders_seen, complete, counters_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                time.time(), feed.pages_total, len(feed.cards), int(feed.complete),
                json.dumps(feed.counters, ensure_ascii=False) if feed.counters else None,
            ),
        )
        conn.commit()


def last_full_sweep_at() -> float | None:
    init_db()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(captured_at) FROM feed_snapshot WHERE complete = 1"
        ).fetchone()
    return row[0] if row and row[0] else None


def record_stats(stats: dict | None) -> None:
    if not stats:
        return
    init_db()
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO exchange_stats
               (captured_at, projects_month, amount_month, orders_month,
                connects_left, connects_total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                stats["captured_at"], stats["projects_month"], stats["amount_month"],
                stats["orders_month"], stats["connects_left"], stats["connects_total"],
            ),
        )
        conn.commit()


@dataclass
class GoneOrder:
    order_id: int
    title: str
    seconds_tracked: float          # сколько заказ был на глазах у монитора
    hours_alive: float | None       # сколько прожил с публикации, если она известна
    reason: str                     # timeout | early | early_mass | unknown


@dataclass
class RunSummary:
    new_orders: list[ProjectCard]
    gone_orders: list[GoneOrder]
    still_open: int


def record_run(
    cards: list[ProjectCard],
    queried_keywords: set[str] | None = None,
    mark_missing_gone: bool | None = None,
) -> RunSummary:
    """Пишет результаты одного прогона в БД, возвращает сводку изменений.

    mark_missing_gone задаёт, доверять ли полноте этого среза:
        True  — обойдена вся лента, значит всё, чего в ней нет, действительно
                пропало с биржи;
        False — срез заведомо неполный (лёгкий проход по первым страницам):
                новые заказы записываем, наблюдения обновляем, но никого не
                хороним;
        None  — прежнее поведение по ключевым словам (режим `--keywords`).

    queried_keywords — какие ключевые слова реально успешно опросили в этом
    прогоне. Заказ помечается "пропавшим" только если хотя бы одно из его
    ключевых слов реально проверялось и заказа среди результатов не оказалось —
    иначе просто пропущенный из-за сетевого сбоя запрос по ошибке "хоронил" бы
    живые заказы.
    """
    init_db()
    now = time.time()

    # Дедуп по order_id — один заказ может совпасть с несколькими ключевыми словами
    by_id: dict[int, ProjectCard] = {}
    keywords_by_id: dict[int, set[str]] = {}
    for c in cards:
        keywords_by_id.setdefault(c.order_id, set()).add(c.keyword_query)
        if c.order_id not in by_id:
            by_id[c.order_id] = c

    seen_ids = set(by_id.keys())
    new_orders: list[ProjectCard] = []
    gone_orders: list[GoneOrder] = []

    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute("SELECT order_id, keyword_queries FROM orders WHERE status = 'open'")
        open_rows = cur.fetchall()
        if mark_missing_gone is True:
            previously_open = {order_id for order_id, _ in open_rows}
        elif mark_missing_gone is False:
            previously_open = set()
        elif queried_keywords is None:
            previously_open = {order_id for order_id, _ in open_rows}
        else:
            previously_open = {
                order_id for order_id, kw_str in open_rows
                if set((kw_str or "").split(",")) & queried_keywords
            }

        for order_id, card in by_id.items():
            kw_joined = ",".join(sorted(keywords_by_id[order_id]))
            cur.execute(
                "SELECT order_id, keyword_queries, first_seen_at FROM orders WHERE order_id = ?",
                (order_id,),
            )
            row = cur.fetchone()
            # Дедлайн и время публикации перезаписываем на каждом наблюдении
            # осознанно: чем меньше остаётся, тем точнее площадка показывает
            # «Осталось» (сутки+ округляются до часа, меньше суток — до минуты),
            # поэтому последняя оценка всегда точнее предыдущих.
            deadline_at = card.deadline_at
            # Окно приёма определяется по остатку на ПЕРВОЙ встрече, а не на
            # текущей: только там видно, какое из трёх окон выбрал заказчик.
            first_seen_at = row[2] if row is not None else now
            published_at = estimate_published_at(deadline_at, first_seen_at)

            if row is None:
                cur.execute(
                    """INSERT INTO orders
                       (order_id, title, url, description_snippet, buyer_username,
                        keyword_queries, first_seen_at, last_seen_at, status, gone_at,
                        budget_desired, budget_max, deadline_at, published_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?, ?, ?)""",
                    (
                        order_id, card.title, card.url, card.description_snippet,
                        card.buyer_username, kw_joined, now, now,
                        card.budget_desired, card.budget_max, deadline_at, published_at,
                    ),
                )
                new_orders.append(card)
            else:
                existing_kw = set(row[1].split(",")) if row[1] else set()
                merged_kw = ",".join(sorted(existing_kw | keywords_by_id[order_id]))
                cur.execute(
                    """UPDATE orders SET last_seen_at = ?, status = 'open', keyword_queries = ?,
                              deadline_at = COALESCE(?, deadline_at),
                              published_at = COALESCE(?, published_at)
                       WHERE order_id = ?""",
                    (now, merged_kw, deadline_at, published_at, order_id),
                )

            cur.execute(
                """INSERT INTO history
                   (order_id, scraped_at, keyword_query, offers_count, hired_percent,
                    time_left_text, buyer_projects_count, time_left_minutes, deadline_at,
                    feed_page)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id, now, card.keyword_query, card.offers_count,
                    card.hired_percent, card.time_left_text, card.buyer_projects_count,
                    card.time_left_minutes, deadline_at, card.feed_page,
                ),
            )

        # Внимание: "пропавшим" считается всё, что было open и не встретилось
        # в этом прогоне — корректно только пока список ключевых слов в
        # config.KEYWORDS не меняется между запусками (иначе заказ, видимый
        # только по убранному слову, ошибочно пометится как пропавший).
        gone_ids = previously_open - seen_ids
        buyers: dict[int, str | None] = {}
        for order_id in gone_ids:
            cur.execute(
                """SELECT title, first_seen_at, deadline_at, published_at, buyer_username
                   FROM orders WHERE order_id = ?""",
                (order_id,),
            )
            title, first_seen_at, deadline_at, published_at, buyer = cur.fetchone()
            buyers[order_id] = buyer
            gone_orders.append(GoneOrder(
                order_id=order_id,
                title=title,
                seconds_tracked=now - first_seen_at,
                hours_alive=(now - published_at) / 3600 if published_at else None,
                reason=classify_gone(now, deadline_at),
            ))

        # Несколько заказов одного покупателя, исчезнувших разом, — это одно
        # решение (снятие модерацией, отмена серии), а не столько же наймов.
        # Одиночные ранние пропажи разных покупателей так не склеиваем: на
        # обходе всей биржи их по несколько в каждом прогоне, и это норма.
        early_by_buyer: dict[str, list[GoneOrder]] = defaultdict(list)
        for g in gone_orders:
            if g.reason == "early" and buyers.get(g.order_id):
                early_by_buyer[buyers[g.order_id]].append(g)
        for group in early_by_buyer.values():
            if len(group) >= SAME_BUYER_BATCH_THRESHOLD:
                for g in group:
                    g.reason = "early_mass"

        for g in gone_orders:
            cur.execute(
                "UPDATE orders SET status = 'gone', gone_at = ?, gone_reason = ? WHERE order_id = ?",
                (now, g.reason, g.order_id),
            )

        conn.commit()

    return RunSummary(
        new_orders=new_orders,
        gone_orders=gone_orders,
        still_open=len(seen_ids) - len(new_orders),
    )
