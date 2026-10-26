"""Один прогон мониторинга биржи проектов.

По умолчанию обходит ленту всей биржи (`/projects?c=all`, ~42 страницы).
Полный обход дорогой, поэтому делается не чаще раза в час; между полными
обходами идёт лёгкий проход по первым страницам — он ловит новые проекты и
обновляет наблюдения, но не помечает никого пропавшим (неполный срез на это
права не имеет).

    python run_once.py              # лента всей биржи, режим выбирается сам
    python run_once.py --full       # принудительно полный обход
    python run_once.py --keywords   # прежний режим: только config.KEYWORDS
"""
import datetime as dt
import sys
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "data" / "monitor.log"


def _setup_output() -> None:
    """Куда писать лог.

    Под pythonw (так монитор запускается из планировщика, чтобы не мигала
    консоль и не воровала фокус) sys.stdout и sys.stderr равны None — любой
    print уронил бы прогон. Пишем в тот же файл, куда раньше перенаправлял
    .bat. При ручном запуске из консоли оставляем консоль.

    Вывод должен переживать любые символы из спарсенных данных (ники,
    описания заказов) — не только рубль, который уже один раз уронил
    cp1251-консоль Windows.
    """
    if sys.stdout is None or not sys.stdout.isatty():
        f = open(LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = f
        sys.stderr = f
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


_setup_output()

import alerts  # noqa: E402
from config import KEYWORDS, FULL_SWEEP_MIN_INTERVAL_SEC, LIGHT_SWEEP_PAGES
from scraper import fetch_all, sweep_feed
from storage import (
    record_run, record_stats, record_feed_snapshot, last_full_sweep_at,
)

REASON_LABEL = {
    "timeout": "дожил до конца приёма предложений",
    "early": "исчез раньше дедлайна (найм/отмена/снятие — не различить)",
    "early_mass": "исчез в пачке одновременных пропаж — событие площадки, не найм",
    "unknown": "дедлайн неизвестен",
}


def print_changes(started: str, summary) -> None:
    print(f"  Новых заказов: {len(summary.new_orders)}")
    for c in summary.new_orders:
        print(f"    + [{c.order_id}] {c.title} (до {c.budget_desired} руб., "
              f"предложений: {c.offers_count})")

    print(f"  Пропало заказов: {len(summary.gone_orders)}")
    for g in summary.gone_orders:
        alive = (f"прожил ~{g.hours_alive:.1f} ч" if g.hours_alive is not None
                 else f"под наблюдением был ~{g.seconds_tracked / 3600:.1f} ч")
        print(f"    - [{g.order_id}] {g.title} ({alive}; {REASON_LABEL[g.reason]})")

    print(f"  Всё ещё открыто (без учёта новых): {summary.still_open}")


def run_keywords(started: str) -> None:
    cards, queried_keywords, stats = fetch_all(KEYWORDS)
    summary = record_run(cards, queried_keywords)
    record_stats(stats)
    skipped = set(KEYWORDS) - queried_keywords
    if skipped:
        print(f"[{started}] Пропущены в этом прогоне (сбой запроса): {sorted(skipped)}")
    print(f"[{started}] Прогон по ключевым словам. Собрано карточек: {len(cards)}")
    print_changes(started, summary)


def run_feed(started: str, force_full: bool) -> None:
    last_full = last_full_sweep_at()
    due = force_full or last_full is None or (time.time() - last_full) >= FULL_SWEEP_MIN_INTERVAL_SEC
    feed, stats = sweep_feed(max_pages=None if due else LIGHT_SWEEP_PAGES)

    record_feed_snapshot(feed)
    summary = record_run(feed.cards, mark_missing_gone=feed.complete)
    record_stats(stats)

    mode = "полный обход" if due else f"лёгкий проход ({LIGHT_SWEEP_PAGES} стр.)"
    print(f"[{started}] {mode}: страниц {feed.pages_fetched}"
          f"{f' из {feed.pages_total}' if feed.pages_total else ''}, "
          f"карточек {len(feed.cards)}")
    if due and not feed.complete:
        print("  ⚠ обход неполный — пропавшие заказы в этом прогоне не отмечались")
    if feed.counters:
        offers = feed.counters.get("offers") or {}
        cats = feed.counters.get("categories") or {}
        if offers:
            print("  По числу предложений: " +
                  ", ".join(f"{k} — {v}" for k, v in offers.items()))
        if cats:
            print("  По рубрикам: " + ", ".join(f"{k} {v}" for k, v in cats.items()))
    if stats:
        print(f"  Биржа за месяц: проектов {stats['projects_month']}, "
              f"заказов {stats['orders_month']}, сумма {stats['amount_month']} руб.; "
              f"коннектов осталось {stats['connects_left']} из {stats['connects_total']}")
    print_changes(started, summary)


def main() -> None:
    started = dt.datetime.now().isoformat(timespec="seconds")
    # Запас в минуту: заказ записывается в базу уже после старта прогона,
    # но часы и порядок операций округлять не хочется.
    since = time.time() - 60
    if "--keywords" in sys.argv:
        run_keywords(started)
    else:
        run_feed(started, force_full="--full" in sys.argv)

    # Оповещения — последним шагом и в try: сбор данных важнее рассылки,
    # и падение Telegram не должно стоить нам точки в ряду наблюдений.
    try:
        sent = alerts.notify_new(since)
        if sent:
            print(f"  Оповещений отправлено: {sent}")
    except Exception as e:  # noqa: BLE001
        print(f"  [alerts] сбой: {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
