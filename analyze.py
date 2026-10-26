"""Отчёт по собранным данным мониторинга биржи.

Пишет `data/analysis_snapshot.txt`. Главное отличие от первой версии: время
жизни заказа считается от восстановленной даты публикации (дедлайн минус срок
приёма предложений), а не от момента, когда заказ впервые попался монитору, и
пропавшие заказы разделены по причине исчезновения — иначе «провисел N часов»
смешивает в одну кучу истёкшие по сроку, разобранные и снятые площадкой.
"""
import sqlite3
import time
from collections import defaultdict
from datetime import datetime

from classify import classify as is_lead_gen
from config import DB_PATH

REASON_LABEL = {
    "timeout": "дожили до конца срока (никого не наняли за 3 суток)",
    "early": "исчезли раньше срока поодиночке (вероятный найм или отмена)",
    "early_mass": "исчезли пачкой в один прогон (событие площадки, не найм)",
    "unknown": "дедлайн неизвестен",
}

conn = sqlite3.connect(str(DB_PATH))
cur = conn.cursor()
now = time.time()
ts = lambda t: datetime.fromtimestamp(t).strftime("%d.%m %H:%M") if t else "—"

out = []
out.append("=== Общая картина ===")
cur.execute("SELECT COUNT(*), SUM(status='open'), SUM(status='gone') FROM orders")
total, open_cnt, gone_cnt = cur.fetchone()
out.append(f"Заказов под наблюдением: {total} (открыто {open_cnt}, пропало {gone_cnt})")
cur.execute("SELECT COUNT(DISTINCT scraped_at), MIN(scraped_at), MAX(scraped_at) FROM history")
runs, first_run, last_run = cur.fetchone()
out.append(f"Прогонов: {runs}, окно наблюдения: {ts(first_run)} — {ts(last_run)}")

cur.execute("""SELECT captured_at, projects_month, amount_month, orders_month,
                      connects_left, connects_total
               FROM exchange_stats ORDER BY captured_at DESC LIMIT 1""")
row = cur.fetchone()
if row:
    cap, projects, amount, orders, c_left, c_total = row
    out.append(f"\n=== Сводка биржи (снято {ts(cap)}, публикует сама площадка) ===")
    out.append(f"Проектов за месяц: {projects}")
    out.append(f"Из них стало заказами: {orders}" +
               (f" — {orders / projects * 100:.0f}% размещённых проектов доходят до найма"
                if projects else ""))
    out.append(f"Сумма проектов за месяц: {amount} руб." +
               (f", средний проект {amount / projects:.0f} руб." if projects else ""))
    out.append(f"Коннектов (откликов) осталось: {c_left} из {c_total} — жёсткий потолок "
               f"числа откликов в месяц на бесплатном тарифе")

out.append("\n=== Почему заказы исчезают ===")
cur.execute("""SELECT gone_reason, COUNT(*) FROM orders
               WHERE status='gone' GROUP BY gone_reason ORDER BY COUNT(*) DESC""")
reasons = cur.fetchall()
if not reasons:
    out.append("Пока ни один заказ не пометился как пропавший.")
for reason, cnt in reasons:
    out.append(f"  {cnt} — {REASON_LABEL.get(reason, reason)}")

out.append("\n=== Пропавшие заказы подробно ===")
cur.execute("""SELECT order_id, title, buyer_username, published_at, deadline_at, gone_at,
                      gone_reason, budget_desired
               FROM orders WHERE status='gone' ORDER BY gone_at, order_id""")
for oid, title, buyer, pub, dl, gone, reason, budget in cur.fetchall():
    lived = f"{(gone - pub) / 3600:.1f} ч" if pub else "?"
    cur.execute("""SELECT offers_count FROM history WHERE order_id=?
                   ORDER BY scraped_at DESC LIMIT 1""", (oid,))
    offers = cur.fetchone()[0]
    out.append(f"  [{oid}] {title}")
    out.append(f"      покупатель {buyer}, бюджет до {budget} руб., предложений на момент пропажи: {offers}")
    out.append(f"      опубликован {ts(pub)}, дедлайн {ts(dl)}, исчез {ts(gone)} — прожил {lived}, "
               f"причина: {reason}")

out.append("\n=== Проверка гипотезы отбора: найм против числа предложений ===")
out.append("Если малоконкурентные заказы пустуют потому, что они плохие, доля дошедших")
out.append("до найма у них будет не выше, чем у людных. Таблица наполняется по мере")
out.append("накопления исчезновений — на малых числах выводов не делать.")
buckets = [(0, 5), (6, 15), (16, 30), (31, 10 ** 6)]
tally = {b: defaultdict(int) for b in buckets}
cur.execute("""SELECT order_id, gone_reason FROM orders WHERE status='gone'""")
for oid, reason in cur.fetchall():
    cur.execute("""SELECT offers_count FROM history WHERE order_id=? AND offers_count IS NOT NULL
                   ORDER BY scraped_at DESC LIMIT 1""", (oid,))
    row = cur.fetchone()
    if not row or row[0] is None:
        continue
    for lo, hi in buckets:
        if lo <= row[0] <= hi:
            tally[(lo, hi)][reason or "unknown"] += 1
for (lo, hi), counts in tally.items():
    label = f"{lo}-{hi} предложений" if hi < 10 ** 6 else f"{lo}+ предложений"
    total = sum(counts.values())
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "нет данных"
    out.append(f"  {label:22s} исчезло {total:3d} | {detail}")

out.append("\n=== Малоконкурентные открытые заказы (≤5 предложений) ===")
out.append("Лидогенерация («приведите клиентов») вынесена отдельно: там пусто по той же")
out.append("причине, что в любой партнёрке, и бюджет означает потолок комиссии с продаж,")
out.append("а не оплату работы. Разметка — monitor/classify.py, эвристика по заголовку.")
out.append("«Нанято» — доля проектов этого покупателя, закончившихся наймом.")
cur.execute("""SELECT o.order_id, o.title, o.budget_desired, o.published_at, o.buyer_username,
                      o.description_snippet,
                      (SELECT offers_count FROM history h WHERE h.order_id=o.order_id
                        ORDER BY scraped_at DESC LIMIT 1) offers,
                      (SELECT hired_percent FROM history h WHERE h.order_id=o.order_id
                        ORDER BY scraped_at DESC LIMIT 1) hired
               FROM orders o WHERE o.status='open'""")
low = [r for r in cur.fetchall() if r[6] is not None and r[6] <= 5]
low.sort(key=lambda r: -(r[2] or 0))
work, leadgen = [], []
for r in low:
    (leadgen if is_lead_gen(r[1], r[5])[0] else work).append(r)
out.append(f"Всего малоконкурентных: {len(low)} — из них лидогенерации {len(leadgen)} "
           f"({len(leadgen) / len(low) * 100:.0f}%), задач на работу {len(work)}")

out.append(f"\n--- Задачи на работу ({len(work)}), топ по бюджету ---")
for oid, title, budget, pub, buyer, _d, offers, hired in work[:35]:
    age = f"{(now - pub) / 3600:.0f} ч" if pub else "?"
    hired_s = f"{hired}%" if hired is not None else "—"
    out.append(f"  до {str(budget):>7} руб. | предл. {offers} | нанято у покупателя {hired_s:>4} | "
               f"возраст {age:>5} | [{oid}] {title[:52]}")

out.append(f"\n--- Отсеяно как лидогенерация ({len(leadgen)}), топ по бюджету ---")
for oid, title, budget, pub, buyer, desc, offers, hired in leadgen[:15]:
    marker = is_lead_gen(title, desc)[1]
    out.append(f"  до {str(budget):>7} руб. | предл. {offers} | [{oid}] {title[:48]}  ← {marker!r}")

out.append("\n=== Скорость набора предложений (первые наблюдения против последних) ===")
cur.execute("""SELECT order_id, COUNT(*) c FROM history GROUP BY order_id HAVING c >= 4""")
speeds = []
for oid, _ in cur.fetchall():
    cur.execute("""SELECT scraped_at, offers_count FROM history
                   WHERE order_id=? AND offers_count IS NOT NULL ORDER BY scraped_at""", (oid,))
    h = cur.fetchall()
    if len(h) < 2:
        continue
    hours = (h[-1][0] - h[0][0]) / 3600
    if hours <= 0:
        continue
    speeds.append(((h[-1][1] - h[0][1]) / hours, oid, h[0][1], h[-1][1], hours))
speeds.sort(reverse=True)
for rate, oid, first, last, hours in speeds[:10]:
    cur.execute("SELECT title FROM orders WHERE order_id=?", (oid,))
    out.append(f"  {rate:5.2f} предл./ч  [{oid}] {first}->{last} за {hours:.1f} ч — {cur.fetchone()[0][:45]}")

with open(r"D:\money\research\kwork\data\analysis_snapshot.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))

print("done")
