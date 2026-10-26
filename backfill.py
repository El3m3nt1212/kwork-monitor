"""Разовый пересчёт уже собранных данных под новые поля (идемпотентный).

Монитор сутки писал «Осталось» только строкой и не хранил ни дедлайна, ни
времени публикации, а причину исчезновения заказа не определял вовсе. Всё это
восстанавливается из уже имеющейся истории задним числом — перепроходить биржу
не нужно. Можно запускать повторно: пересчитывает те же значения из тех же
исходных строк.

    python backfill.py
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from config import DB_PATH
from scraper import parse_time_left, estimate_published_at
from storage import (
    init_db, classify_gone, SAME_BUYER_BATCH_THRESHOLD,
)


def main() -> None:
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # 1. История: строка «Осталось» -> минуты и абсолютный дедлайн.
    rows = cur.execute(
        "SELECT id, scraped_at, time_left_text FROM history"
    ).fetchall()
    filled = 0
    for row_id, scraped_at, text in rows:
        minutes = parse_time_left(text)
        if minutes is None:
            continue
        cur.execute(
            "UPDATE history SET time_left_minutes = ?, deadline_at = ? WHERE id = ?",
            (minutes, scraped_at + minutes * 60, row_id),
        )
        filled += 1

    # 2. Заказы: дедлайн берём из последнего наблюдения (оно точнее всех
    #    предыдущих — площадка округляет тем грубее, чем больше осталось),
    #    время публикации восстанавливаем по окну приёма (24/48/72 ч), которое
    #    определяется остатком на первой встрече. Там, где заказ пойман не в
    #    первый час, публикация остаётся NULL — это честнее, чем подставлять
    #    первую фиксацию или общий потолок в 72 часа.
    updated = 0
    unknown = 0
    for order_id, first_seen_at in cur.execute(
            "SELECT order_id, first_seen_at FROM orders").fetchall():
        last = cur.execute(
            """SELECT deadline_at FROM history
               WHERE order_id = ? AND deadline_at IS NOT NULL
               ORDER BY scraped_at DESC LIMIT 1""",
            (order_id,),
        ).fetchone()
        if not last:
            continue
        deadline_at = last[0]
        published_at = estimate_published_at(deadline_at, first_seen_at)
        if published_at is None:
            unknown += 1
        cur.execute(
            "UPDATE orders SET deadline_at = ?, published_at = ? WHERE order_id = ?",
            (deadline_at, published_at, order_id),
        )
        updated += 1

    # 3. Причина исчезновения — та же логика, что теперь работает в record_run,
    #    включая склейку одновременных ранних пропаж в «событие площадки».
    gone = cur.execute(
        """SELECT order_id, gone_at, deadline_at, buyer_username
           FROM orders WHERE status = 'gone'"""
    ).fetchall()
    by_run: dict[float, list[tuple[int, str, str]]] = defaultdict(list)
    for order_id, gone_at, deadline_at, buyer in gone:
        by_run[gone_at].append((order_id, classify_gone(gone_at, deadline_at), buyer))

    counts: dict[str, int] = defaultdict(int)
    for gone_at, items in by_run.items():
        # «Пачка» = несколько заказов одного покупателя в одном прогоне.
        per_buyer: dict[str, int] = defaultdict(int)
        for _, reason, buyer in items:
            if reason == "early" and buyer:
                per_buyer[buyer] += 1
        for order_id, reason, buyer in items:
            if reason == "early" and buyer and per_buyer[buyer] >= SAME_BUYER_BATCH_THRESHOLD:
                reason = "early_mass"
            cur.execute(
                "UPDATE orders SET gone_reason = ? WHERE order_id = ?", (reason, order_id)
            )
            counts[reason] += 1

    conn.commit()
    conn.close()

    print(f"history: заполнено строк с распознанным временем — {filled} из {len(rows)}")
    print(f"orders: дедлайн обновлён у {updated}, "
          f"время публикации восстановлено у {updated - unknown}, "
          f"неизвестно у {unknown} (заказ пойман не в первый час)")
    print("причины исчезновения:", dict(counts))


if __name__ == "__main__":
    main()
