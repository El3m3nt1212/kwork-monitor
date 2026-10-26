"""Отбор заказов, на которые имеет смысл потратить коннект.

Отдельный шаг от мониторинга: обход ленты собирает всё подряд, а здесь из
собранного выбирается то, куда стоит писать. Пишет `data/shortlist.md`.

Три вещи, которых нет в данных обхода и ради которых скрипт открывает страницы
заказов (только для верхушки списка, не для всей биржи):

1. **Плашка «Низкая оплата».** Kwork сам помечает заказы с неадекватной ценой,
   но на карточке в ленте её нет — только на странице заказа. Прямо
   соответствует гейту D (≥300 ₽/час), поэтому забираем.
2. **Полное описание.** В карточке оно обрезано на «Показать полностью», а
   решают именно детали: требование прислать кейсы, требование доступа или
   связей, оговорка «без нейросетей» — всё это убивает заказ и всё это видно
   только в полном тексте.
3. **Свежий счётчик предложений.** В базе он с последнего обхода, а у молодых
   заказов растёт на глазах (03.08: 0 → 9 за сорок минут).

Отдельно про возраст: у заказа моложе SETTLED_AGE_HOURS число предложений
ничего не значит — оно ещё не устоялось (по нашим же замерам счётчик набирает
почти всё за 6–8 часов). Такие заказы идут отдельным списком без ранжирования.

    python shortlist.py
"""
from __future__ import annotations

import io
import sqlite3
import time

from playwright.sync_api import sync_playwright

from classify import blocked_reason
from config import DB_PATH, PROFILE_DIR

MIN_BUDGET = 5000
MAX_OFFERS = 5
SETTLED_AGE_HOURS = 6
FETCH_TOP = 14                 # сколько страниц открыть для вычитки
DEFAULT_HIRE_RATE = 0.30       # средняя по бирже, если у покупателя нет статистики
OUT_PATH = DB_PATH.parent / "shortlist.md"

LOW_PAY_BADGE = "Низкая оплата"


def candidates() -> tuple[list[dict], list[dict]]:
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("""
        SELECT o.order_id, o.title, o.description_snippet, o.budget_desired, o.budget_max,
               o.buyer_username, o.published_at, o.deadline_at,
               (SELECT offers_count FROM history h WHERE h.order_id = o.order_id
                 AND offers_count IS NOT NULL ORDER BY scraped_at DESC LIMIT 1),
               (SELECT hired_percent FROM history h WHERE h.order_id = o.order_id
                 ORDER BY scraped_at DESC LIMIT 1),
               (SELECT buyer_projects_count FROM history h WHERE h.order_id = o.order_id
                 ORDER BY scraped_at DESC LIMIT 1)
        FROM orders o WHERE o.status = 'open'""").fetchall()
    conn.close()

    now = time.time()
    settled, fresh = [], []
    for (oid, title, desc, bd, bm, buyer, pub, dl, offers, hired, bp) in rows:
        if offers is None or (bd or 0) < MIN_BUDGET or offers > MAX_OFFERS:
            continue
        if dl and dl < now:
            continue
        if blocked_reason(title, desc):
            continue
        hire = (hired if hired is not None else DEFAULT_HIRE_RATE * 100) / 100
        item = dict(order_id=oid, title=title, budget=bd, budget_max=bm, buyer=buyer,
                    offers=offers, hired=hired, buyer_projects=bp,
                    hours_left=(dl - now) / 3600 if dl else None,
                    age_hours=(now - pub) / 3600 if pub else None,
                    score=(bd or 0) * hire / (offers + 1))
        age = item["age_hours"]
        (fresh if age is not None and age < SETTLED_AGE_HOURS else settled).append(item)

    settled.sort(key=lambda x: -x["score"])
    fresh.sort(key=lambda x: -(x["budget"] or 0))
    return settled, fresh


def enrich(items: list[dict]) -> None:
    """Дочитывает страницы заказов: полное описание, плашка, свежий счётчик."""
    if not items:
        return
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=True, viewport={"width": 1280, "height": 1000})
        try:
            for it in items:
                page = ctx.new_page()
                try:
                    page.goto(f"https://kwork.ru/projects/{it['order_id']}",
                              wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)
                    # Исчезнувший заказ редиректит на общий список — по URL это
                    # видно надёжнее, чем по содержимому страницы.
                    if str(it["order_id"]) not in page.url:
                        it["gone"] = True
                        continue
                    body = page.inner_text("body")
                    it["low_pay"] = LOW_PAY_BADGE in body
                    # Содержательная часть лежит между концом шапки («Установить»
                    # — кнопка баннера мобильного приложения) и строкой бюджета.
                    # Якорь по заголовку не годится: в базе он обрезан.
                    tail = body.split("Установить", 1)[-1]
                    for stop in ("Желаемый бюджет", "Цена до:"):
                        i = tail.find(stop)
                        if i > 0:
                            tail = tail[:i]
                            break
                    it["full_desc"] = " ".join(tail.split())[:1200]
                    live = None
                    for line in body.splitlines():
                        if line.strip().startswith("Предложений:"):
                            digits = "".join(ch for ch in line if ch.isdigit())
                            live = int(digits) if digits else None
                            break
                    it["offers_live"] = live
                except Exception as e:
                    it["error"] = f"{type(e).__name__}: {e}"
                finally:
                    page.close()
                time.sleep(2.5)
        finally:
            ctx.close()


def main() -> None:
    settled, fresh = candidates()
    top = settled[:FETCH_TOP]
    enrich(top)

    out = io.open(OUT_PATH, "w", encoding="utf-8")
    out.write(f"# Отбор заказов Kwork — {time.strftime('%Y-%m-%d %H:%M')}\n\n")
    out.write(f"Фильтр: бюджет ≥ {MIN_BUDGET} ₽, предложений ≤ {MAX_OFFERS}, "
              f"не лидогенерация, не заблокированные классы (см. `classify.blocked_reason`).\n")
    out.write(f"Устоявшихся (старше {SETTLED_AGE_HOURS} ч): {len(settled)}, "
              f"из них вычитано {len(top)}. Свежих: {len(fresh)}.\n\n")

    out.write("## Устоявшиеся — по ожидаемой ценности\n\n")
    for it in top:
        flags = []
        if it.get("low_pay"):
            flags.append("⚠️ ПЛАШКА «Низкая оплата» от Kwork")
        if it.get("offers_live") is not None and it["offers_live"] > it["offers"]:
            flags.append(f"счётчик вырос: было {it['offers']}, сейчас {it['offers_live']}")
        out.write(f"### [{it['order_id']}] {it['title']}\n")
        out.write(f"- бюджет до **{it['budget']} ₽**"
                  + (f" (допустимый до {it['budget_max']})" if it["budget_max"] else "") + "\n")
        out.write(f"- предложений {it.get('offers_live', it['offers'])}, "
                  f"возраст {it['age_hours']:.0f} ч, осталось {it['hours_left']:.0f} ч\n")
        out.write(f"- покупатель {it['buyer']}: проектов {it['buyer_projects']}, "
                  f"найм {it['hired'] if it['hired'] is not None else 'н/д'}%\n")
        if flags:
            out.write("- " + "; ".join(flags) + "\n")
        out.write(f"- https://kwork.ru/projects/{it['order_id']}\n\n")
        out.write(f"> {it.get('full_desc') or it.get('error') or '(описание не снялось)'}\n\n")

    out.write("\n## Свежие — конкуренция ещё не определилась\n\n")
    out.write("Число предложений у них пока ничего не значит: счётчик набирает почти всё "
              "за первые 6–8 часов. Смотреть на них имеет смысл, только если готов писать сразу.\n\n")
    for it in fresh[:15]:
        out.write(f"- {it['budget']} ₽, предложений {it['offers']}, возраст "
                  f"{it['age_hours']:.1f} ч — [{it['order_id']}] {it['title']}\n")
    out.close()
    print(f"готово: {OUT_PATH}")
    print(f"устоявшихся {len(settled)}, вычитано {len(top)}, свежих {len(fresh)}")


if __name__ == "__main__":
    main()
