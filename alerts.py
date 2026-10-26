# -*- coding: utf-8 -*-
"""
Оповещения о заказах, куда стоит потратить коннект.

Зачем именно так. Отклик стоит коннект, коннектов 30 (проверено 2026-08-14 по
`exchange_stats`), а в наших нишах на биржу приходит около двух подходящих
заказов в неделю при общем потоке 277 в сутки. То есть задача не «отсортировать
ленту», а «не пропустить редкое». Поэтому здесь нет ни моделей, ни скоринга:
регулярка по пяти нишам плюс три порога, всё считается по данным, которые
монитор и так собирает.

Что отсекаем и почему:
  - блокеры (`classify.blocked_reason`) — лидогенерация, накрутка, чужие
    аккаунты: платят не за работу;
  - заказы, где уже много предложений — коннект там тратится впустую;
  - покупателей, которые ни разу никого не наняли. 66% заказов на бирже
    закрываются без найма, и `hired_percent` — самый дешёвый способ это
    увидеть заранее. Неизвестное значение не отсекаем: у новых покупателей
    статистики просто нет, и это не их вина.
    ⚠ 2026-08-18: обоснование этого фильтра по базе не подтвердилось —
    у бакета «нанимает 0%» доля досрочных закрытий как раз самая высокая
    (50% против фона 35%, n=769). Фильтр пока оставлен как есть, разбор —
    в `data/buyer_track_record.md`, раздел 3.

Чего НЕ отсекаем, а помечаем: покупателя, у которого на наших глазах не
закрылся досрочно ни один заказ (`buyer_history`). Прокси грубый, а коннект
в конце концов тратит человек — пусть решает с цифрой перед глазами.

Дедупликация по order_id: заказ уходит в Telegram один раз, а не каждые
полчаса до его исчезновения.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

import buyer_history
from classify import blocked_reason
from config import DB_PATH, KWORK_DIR

# Ниши = наши кворки. Меняются вместе с ними, а не живут своей жизнью.
NICHES: dict[str, str] = {
    "трейдинг MT4/MT5":
        r"\bmql\b|\bmql4\b|\bmql5\b|\bmt4\b|\bmt5\b|метатрейд|meta ?trader|"
        r"торгов\w+ (робот|бот|стратег|советник|систем)|\bсоветник\w*\b|\bбэктест",
    "TradingView / Pine":
        r"tradingview|trading ?view|pine ?script|\bпайн\b",
    "МойСклад":
        r"мой ?склад|моysklad|my ?sklad",
    "iiko / ресторанные системы":
        r"\biiko\b|\bайко\b|r[_ -]?keeper|\bркипер",
    "выгрузки и таблицы":
        r"google ?(таблиц|sheet)|гугл ?таблиц|apps ?script|"
        r"выгруз\w+ (в|из)\b|интеграц\w+ .{0,25}\bapi\b",
    "дашборды и BI":
        r"дашборд|dashboard|looker|data ?studio|power ?bi|datalens|даталенс",
}
_NICHE_RX = {name: re.compile(pat, re.I) for name, pat in NICHES.items()}

MAX_OFFERS = 10          # выше этого коннект тратить незачем
ENV_PATH = KWORK_DIR / ".env"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts_sent (
    order_id  INTEGER PRIMARY KEY,
    sent_at   REAL,
    niche     TEXT,
    ok        INTEGER,     -- 1 = отправлено, 0 = Telegram не принял, -1 = заказ не открывается
    note      TEXT
);
"""


def init_db() -> None:
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts_sent)")}
        if "note" not in cols:
            conn.execute("ALTER TABLE alerts_sent ADD COLUMN note TEXT")
        conn.commit()


def openable(order_ids: list[int]) -> dict[int, bool]:
    """Открывается ли страница заказа под нашим аккаунтом.

    Зачем. Найдено 2026-08-14: часть заказов висит в ленте, но их страница
    редиректит на /projects — три из шести заказов одной пачки. Возраст ни при
    чём (девять заказов от 0.2 до 22 часов открылись), в разметке карточки
    отличий нет, причина снаружи не определяется. Оповещение с нерабочей
    ссылкой бесполезно, поэтому проверяем перед отправкой, а не гадаем.

    Канонический адрес — /projects/<id>/view; /projects/<id> редиректит туда
    сам, если заказ доступен, и на /projects, если нет.

    Стоит одну загрузку страницы на кандидата, а кандидатов ~2 в неделю.
    Если проверка сама сломалась, считаем заказ доступным: лучше ссылка,
    которая может не открыться, чем пропущенный заказ.
    """
    result = {oid: True for oid in order_ids}
    if not order_ids:
        return result
    try:
        from playwright.sync_api import sync_playwright

        from config import PROFILE_DIR
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for oid in order_ids:
                try:
                    # domcontentloaded, а не networkidle: редирект решается на
                    # сервере и виден сразу, а ожидание тишины в сети даёт
                    # ложные таймауты на первой загрузке в свежем контексте.
                    page.goto(f"https://kwork.ru/projects/{oid}",
                              wait_until="domcontentloaded", timeout=30000)
                    result[oid] = str(oid) in page.url
                except Exception as e:  # noqa: BLE001
                    print(f"  [alerts] проверка {oid} не удалась: {type(e).__name__}")
                time.sleep(2.0)
            ctx.close()
    except Exception as e:  # noqa: BLE001
        print(f"  [alerts] проверку доступности пропускаем: {type(e).__name__}: {str(e)[:120]}")
    return result


def _env() -> dict[str, str]:
    """Токен и chat_id из .env (файл в .gitignore, в репозиторий не попадает)."""
    out: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    for k in ("KWORK_TG_TOKEN", "KWORK_TG_CHAT"):
        out.setdefault(k, os.environ.get(k, ""))
    return out


def send(text: str) -> bool:
    env = _env()
    token, chat = env.get("KWORK_TG_TOKEN"), env.get("KWORK_TG_CHAT")
    if not token or not chat:
        print("  [alerts] нет KWORK_TG_TOKEN/KWORK_TG_CHAT в .env — не отправляю")
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as e:  # noqa: BLE001
        # Оповещение не должно ронять прогон монитора: сбор данных важнее.
        print(f"  [alerts] Telegram недоступен: {type(e).__name__}: {str(e)[:120]}")
        return False


def match_niche(title: str, descr: str) -> str | None:
    text = f"{title} {descr}"
    for name, rx in _NICHE_RX.items():
        if rx.search(text):
            return name
    return None


def candidates(since: float) -> list[dict]:
    """Заказы, впервые увиденные после `since`, прошедшие фильтры."""
    with sqlite3.connect(str(DB_PATH)) as conn:
        rows = conn.execute("""
            WITH last AS (
                SELECT order_id, offers_count, buyer_projects_count, hired_percent,
                       ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scraped_at DESC) rn
                FROM history
            )
            SELECT o.order_id, o.title, IFNULL(o.description_snippet, ''), o.url,
                   o.budget_desired, o.budget_max,
                   l.offers_count, l.buyer_projects_count, l.hired_percent,
                   o.buyer_username
            FROM orders o
            JOIN last l ON l.order_id = o.order_id AND l.rn = 1
            LEFT JOIN alerts_sent a ON a.order_id = o.order_id
            WHERE o.first_seen_at >= ? AND o.status = 'open' AND a.order_id IS NULL
        """, (since,)).fetchall()
        # Досье считается на том же соединении, одним запросом на кандидата.
        notes = {r[0]: buyer_history.note(conn, r[9]) for r in rows}

    out = []
    for (oid, title, descr, url, bud, bud_max, offers, bprojects, hired, buyer) in rows:
        niche = match_niche(title, descr)
        if not niche:
            continue
        if blocked_reason(title, descr):
            continue
        if offers is not None and offers > MAX_OFFERS:
            continue
        if hired is not None and hired == 0:
            continue          # покупатель ни разу никого не нанял
        out.append({
            "order_id": oid, "title": title, "url": url, "niche": niche,
            "budget": bud or bud_max, "offers": offers,
            "buyer_projects": bprojects, "hired": hired,
            "buyer": buyer, "buyer_note": notes.get(oid),
        })
    return out


def format_alert(c: dict) -> str:
    money = f"{c['budget']} ₽" if c["budget"] else "бюджет не указан"
    offers = "откликов нет" if c["offers"] in (0, None) else f"откликов {c['offers']}"
    buyer = []
    if c["buyer_projects"]:
        buyer.append(f"{c['buyer_projects']} заказов")
    if c["hired"] is not None:
        buyer.append(f"нанимает {c['hired']}%")
    lines = [
        f"{c['niche']} · {money} · {offers}",
        c["title"],
    ]
    if buyer:
        lines.append("Покупатель: " + ", ".join(buyer))
    if c.get("buyer_note"):
        lines.append(c["buyer_note"])
    # Канонический адрес заказа — с /view на конце.
    lines.append(f"https://kwork.ru/projects/{c['order_id']}/view")
    return "\n".join(lines)


def notify_new(since: float) -> int:
    """Разослать оповещения по заказам, появившимся после `since`."""
    init_db()
    found = candidates(since)
    if not found:
        return 0

    live = openable([c["order_id"] for c in found])
    now, sent_count = time.time(), 0
    with sqlite3.connect(str(DB_PATH)) as conn:
        for c in found:
            oid = c["order_id"]
            if not live.get(oid, True):
                # Заказ не открывается под нашим аккаунтом — писать о нём нечего.
                # Запоминаем, чтобы не проверять его заново каждые полчаса.
                conn.execute(
                    "INSERT OR REPLACE INTO alerts_sent (order_id, sent_at, niche, ok, note)"
                    " VALUES (?, ?, ?, -1, ?)",
                    (oid, now, c["niche"], "страница заказа редиректит на /projects"))
                print(f"  [alerts] пропущен (не открывается): [{oid}] {c['title'][:55]}")
                continue
            ok = send(format_alert(c))
            conn.execute(
                "INSERT OR REPLACE INTO alerts_sent (order_id, sent_at, niche, ok, note)"
                " VALUES (?, ?, ?, ?, NULL)", (oid, now, c["niche"], 1 if ok else 0))
            print(f"  [alerts] {'отправлено' if ok else 'НЕ отправлено'}: "
                  f"[{oid}] {c['title'][:60]}")
            sent_count += 1
        conn.commit()
    return sent_count


if __name__ == "__main__":
    import sys
    # Ручная проверка: python alerts.py [часов_назад]
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
    init_db()
    found = candidates(time.time() - hours * 3600)
    print(f"Подходящих заказов за последние {hours:.0f} ч: {len(found)}")
    for c in found:
        print("-" * 60)
        print(format_alert(c))
