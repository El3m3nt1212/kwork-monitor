"""
Обход биржи заказов Kwork (/projects?keyword=...) через уже авторизованный
Playwright-профиль (см. login_setup.py) и разбор карточек заказов в
структурированные записи.

Разметка карточки (проверено вручную 2026-07-24, могла измениться):
    .project-list .want-card         — одна карточка заказа
    .wants-card__header-title a      — заголовок + ссылка /projects/<id>
    .wants-card__price               — "Желаемый бюджет: до <N> ₽"
    .wants-card__description-higher-price — "Допустимый: до <N> ₽"
    .want-payer-statistic            — блок покупателя (ник, кол-во проектов, % найма)
    .want-card__informers-row        — "Осталось: ..." + "Предложений: N"

Пагинация на выдаче по одному ключевому слову пока не встречалась (обычно
< 20 карточек на узкий запрос) — если результатов станет больше и это
окажется первой страницей из многих, сюда нужно будет добавить её обработку.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import quote

from playwright.sync_api import sync_playwright, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from config import PROFILE_DIR, PROJECTS_URL, REQUEST_DELAY_SEC

# Признак того, что страница отдана авторизованной сессии: ссылка на управление
# своими кворками есть только у залогиненного пользователя. Нужен, чтобы пустая
# выдача не путалась с отвалившейся сессией — см. _assert_trustworthy_empty.
AUTH_MARKER_SELECTOR = "a[href*='/manage_kworks']"

# Дословный текст настоящей пустой выдачи (снято с живой страницы 2026-07-25
# запросом с заведомо несуществующим ключевым словом).
EMPTY_STATE_TEXT = "Нет ни одного доступного проекта в выбранных категориях"

# Блок с числом предложений, оставшимся временем и процентом найма. Заголовок и
# бюджет приходят с сервера сразу, а этот блок дорисовывается отдельно, поэтому
# ждать надо именно его: дождались — значит карточка приехала целиком.
INFORMERS_SELECTOR = ".want-card__informers-row"
INFORMERS_TIMEOUT_MS = 20_000

# Доля карточек страницы, у которых удалось прочитать «Осталось», ниже которой
# странице верить нельзя. 2026-07-25 с 14:06 до 17:33 монитор записал 1500+
# наблюдений, где были только заголовки: страница отдавалась, фоновый запрос за
# данными — нет. Молча писать такие строки хуже, чем пропустить прогон.
MIN_PARSED_SHARE = 0.5


class UnreliableRender(RuntimeError):
    """Карточки на странице есть, но данных в них нет — страница приехала
    наполовину. Отдельный тип ошибки, чтобы такой прогон пропускался целиком,
    а не оседал в истории пустыми строками."""

# Окно приёма предложений выбирает сам заказчик: сутки, двое или трое.
# Проверено на данных 2026-08-11 — «Осталось» при первой фиксации даёт три
# резких пика (23, 47 и 71 час, вместе 72% выборки); доли окон 41/33/26%.
#
# До 2026-08-11 здесь стояла константа 72 часа, выведенная из максимума по 32
# наблюдениям на 25.07. Максимум был принят за общий срок, и это неверно для
# трёх четвертей заказов — см. урок «Максимум наблюдений принят за константу»
# в notes/lessons.md.
ORDER_WINDOWS_HOURS = (24, 48, 72)

# Потолок, а не типичный срок. Оставлен для мест, где нужен именно максимум.
ORDER_LIFETIME_MINUTES = max(ORDER_WINDOWS_HOURS) * 60

# Насколько поздно после публикации монитор мог впервые увидеть заказ, чтобы
# время публикации всё ещё считалось восстановленным. Обход идёт раз в полчаса,
# так что пойманный вовремя заказ отстаёт от публикации меньше чем на час.
FRESH_CATCH_TOLERANCE_SEC = 2 * 3600


def estimate_published_at(deadline_at, first_seen_at):
    """Время публикации заказа или None, если восстановить его нельзя.

    Публикация — это дедлайн минус окно приёма, а окон три. Какое из них выбрал
    заказчик, показывает остаток на момент первой встречи: берём наименьшее
    окно, при котором заказ не оказывается «увиденным раньше, чем опубликован».

    Если при этом выходит, что к первой встрече заказ прожил больше двух часов,
    значит монитор не поймал его в первый час — запускался позже, был перерыв в
    покрытии, заказ появился до начала наблюдения. Тогда время публикации
    неизвестно, и подставлять вместо него первую фиксацию нельзя: именно так
    возраст и завышался.
    """
    if deadline_at is None or first_seen_at is None:
        return None
    remaining_hours = (deadline_at - first_seen_at) / 3600.0
    for window_hours in ORDER_WINDOWS_HOURS:
        if window_hours >= remaining_hours:
            published_at = deadline_at - window_hours * 3600
            if first_seen_at - published_at <= FRESH_CATCH_TOLERANCE_SEC:
                return published_at
            return None
    return None


class UntrustedEmptyResult(RuntimeError):
    """Страница вернула ноль карточек, и это не подтверждённая пустая выдача.

    Отдельный тип ошибки, потому что последствие тяжёлое: если такой результат
    принять за правду, storage.record_run пометит «пропавшими» разом все заказы
    этого ключевого слова. Ровно это и произошло 2026-07-24 в прогоне 23:06.
    """


@dataclass
class ProjectCard:
    order_id: int
    title: str
    url: str
    description_snippet: str
    budget_desired: Optional[int]
    budget_max: Optional[int]
    buyer_username: Optional[str]
    buyer_projects_count: Optional[int]
    hired_percent: Optional[int]
    time_left_text: Optional[str]
    offers_count: Optional[int]
    keyword_query: str
    scraped_at: float
    time_left_minutes: Optional[int] = None
    deadline_at: Optional[float] = None
    feed_page: Optional[int] = None


def _to_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_time_left(text: Optional[str]) -> Optional[int]:
    """«2 д. 18 ч.» / «1 ч. 4 мин.» / «17 мин.» -> минуты.

    Площадка округляет: пока осталось больше суток, показываются только дни и
    часы, поэтому точность такого значения ±30 мин; когда остаётся меньше суток,
    формат переключается на часы с минутами и точность становится ±1 мин.
    """
    if not text:
        return None
    days = re.search(r"(\d+)\s*д", text)
    hours = re.search(r"(\d+)\s*ч", text)
    minutes = re.search(r"(\d+)\s*мин", text)
    if not (days or hours or minutes):
        return None
    total = 0
    if days:
        total += int(days.group(1)) * 24 * 60
    if hours:
        total += int(hours.group(1)) * 60
    if minutes:
        total += int(minutes.group(1))
    return total


def _parse_card(el, keyword_query: str, scraped_at: float) -> Optional[ProjectCard]:
    title_link = el.query_selector(".wants-card__header-title a")
    if not title_link:
        return None
    href = title_link.get_attribute("href") or ""
    m = re.search(r"/projects/(\d+)", href)
    if not m:
        return None
    order_id = int(m.group(1))
    title = title_link.inner_text().strip()

    full_text = el.inner_text()

    price_el = el.query_selector(".wants-card__price")
    budget_desired = _to_int(price_el.inner_text()) if price_el else None

    max_price_el = el.query_selector(".wants-card__description-higher-price")
    budget_max = _to_int(max_price_el.inner_text()) if max_price_el else None

    desc_el = el.query_selector(".wants-card__description-text")
    description_snippet = desc_el.inner_text().strip()[:300] if desc_el else ""

    buyer_link = el.query_selector(".want-payer-statistic a")
    buyer_username = buyer_link.inner_text().strip() if buyer_link else None

    buyer_count_match = re.search(r"Размещено проектов на бирже:\s*(\d+)", full_text)
    buyer_projects_count = int(buyer_count_match.group(1)) if buyer_count_match else None

    hired_match = re.search(r"Нанято:\s*(\d+)\s*%", full_text)
    hired_percent = int(hired_match.group(1)) if hired_match else None

    time_left_match = re.search(r"Осталось:\s*([^\n]+)", full_text)
    time_left_text = time_left_match.group(1).strip() if time_left_match else None

    offers_match = re.search(r"Предложени[йя]:\s*(\d+)", full_text)
    offers_count = int(offers_match.group(1)) if offers_match else None

    # Дедлайн приёма предложений — единственный абсолютный момент времени,
    # который карточка вообще сообщает. Из него потом восстанавливается время
    # публикации (см. estimate_published_at): самой даты публикации в карточке
    # нет, а без неё «провисел N часов» считается от момента, когда заказ
    # впервые попался монитору, а не от его появления на бирже.
    time_left_minutes = parse_time_left(time_left_text)
    deadline_at = scraped_at + time_left_minutes * 60 if time_left_minutes is not None else None

    return ProjectCard(
        order_id=order_id,
        title=title,
        url=f"https://kwork.ru/projects/{order_id}",
        description_snippet=description_snippet,
        budget_desired=budget_desired,
        budget_max=budget_max,
        buyer_username=buyer_username,
        buyer_projects_count=buyer_projects_count,
        hired_percent=hired_percent,
        time_left_text=time_left_text,
        offers_count=offers_count,
        keyword_query=keyword_query,
        scraped_at=scraped_at,
        time_left_minutes=time_left_minutes,
        deadline_at=deadline_at,
    )


def _load_projects_page(page, url: str) -> list:
    """Грузит страницу ленты и возвращает элементы карточек.

    Ждём не `networkidle`, а появления блока с данными: на странице живут
    долгоиграющие соединения (счётчик онлайна, чат), из-за которых сеть может
    не затихнуть вовсе — 2026-07-25 `networkidle` уже упирался в таймаут на
    полностью исправной странице.
    """
    page.goto(url, wait_until="domcontentloaded")
    cards = page.query_selector_all(".project-list .want-card")
    if not cards:
        return []
    try:
        page.wait_for_selector(INFORMERS_SELECTOR, timeout=INFORMERS_TIMEOUT_MS)
    except PlaywrightTimeout:
        raise UnreliableRender(
            f"карточки есть ({len(cards)}), но блок {INFORMERS_SELECTOR} так и не приехал"
        )
    return page.query_selector_all(".project-list .want-card")


def _assert_fields_parsed(cards: list[ProjectCard], page_label: str) -> None:
    if not cards:
        return
    parsed = sum(1 for c in cards if c.time_left_minutes is not None)
    if parsed / len(cards) < MIN_PARSED_SHARE:
        raise UnreliableRender(
            f"{page_label}: «Осталось» прочиталось только у {parsed} карточек из "
            f"{len(cards)} — страница приехала без данных"
        )


def _assert_trustworthy_empty(page) -> None:
    """Проверяет, что ноль карточек — это правда «ничего не найдено».

    Опасность конкретная, а не гипотетическая: `kwork.ru/projects` без живой
    сессии отдаёт нормально загружающийся каркас страницы вообще без заказов
    (проверено 2026-07-24, research-plan.md п.0.2). Для `page.goto` это успех,
    для парсера — пустой список, а для storage.record_run — «все заказы этого
    ключевого слова разом пропали». Поэтому пустой выдаче верим, только если
    страница одновременно показывает признак авторизации и дословный текст
    пустого состояния; иначе считаем запрос неудавшимся.
    """
    if not page.query_selector(AUTH_MARKER_SELECTOR):
        raise UntrustedEmptyResult(
            "ноль карточек и нет признака авторизованной сессии — вероятно, слетел логин"
        )
    if EMPTY_STATE_TEXT not in page.inner_text("body"):
        raise UntrustedEmptyResult(
            "ноль карточек, но и текста пустой выдачи на странице нет — разметка "
            "изменилась либо страница не догрузилась"
        )


def fetch_keyword(context: BrowserContext, keyword: str) -> list[ProjectCard]:
    page = context.new_page()
    try:
        url = f"{PROJECTS_URL}?keyword={quote(keyword)}"
        cards_el = _load_projects_page(page, url)
        scraped_at = time.time()
        if not cards_el:
            _assert_trustworthy_empty(page)
        results = []
        for el in cards_el:
            card = _parse_card(el, keyword, scraped_at)
            if card:
                results.append(card)
        _assert_fields_parsed(results, f"'{keyword}'")
        return results
    finally:
        page.close()


@dataclass
class FeedResult:
    """Итог обхода ленты проектов.

    complete=True означает, что обойдены все страницы и выдача заслуживает
    доверия как полный срез биржи — только при этом условии можно считать
    ненайденный заказ пропавшим.
    """
    cards: list["ProjectCard"]
    pages_fetched: int
    pages_total: Optional[int]
    complete: bool
    counters: Optional[dict]


def _total_pages(page) -> Optional[int]:
    pag = page.query_selector(".pagination")
    if not pag:
        return None
    numbers = [int(n) for n in re.findall(r"\d+", pag.inner_text())]
    return max(numbers) if numbers else None


def parse_sidebar_counters(page) -> Optional[dict]:
    """Счётчики левой колонки: сколько сейчас открытых проектов по рубрикам,
    по бюджету и по числу уже собранных предложений.

    Это готовая агрегированная картина биржи за один запрос — в частности,
    строка «До 5» прямо отвечает, сколько сейчас малоконкурентных проектов.
    """
    text = page.inner_text("body")
    start = text.find("Рубрики")
    end = text.find("Статистика биржи")
    if start < 0 or end < 0:
        return None
    block = text[start:end]

    def section(header: str, stop_headers: tuple[str, ...]) -> dict[str, int]:
        i = block.find(header)
        if i < 0:
            return {}
        chunk = block[i + len(header):]
        for stop in stop_headers:
            j = chunk.find(stop)
            if j > 0:
                chunk = chunk[:j]
        found = {}
        for line in chunk.splitlines():
            m = re.match(r"\s*(.+?)\s*\((\d+)\)\s*$", line)
            if m:
                found[m.group(1)] = int(m.group(2))
        return found

    return {
        "categories": section("Рубрики", ("Бюджет",)),
        "budget": section("Бюджет", ("Нанято", "Количество предложений")),
        "offers": section("Количество предложений", ()),
    }


def fetch_feed(context: BrowserContext, max_pages: int | None = None) -> FeedResult:
    """Обход ленты всей биржи постранично.

    max_pages ограничивает проход сверху (лёгкий проход по первым страницам
    между полными обходами). Сортировка ленты не строго хронологическая —
    среди свежих ID попадаются старые, поэтому «дойти до знакомых заказов и
    остановиться» нельзя, полный срез требует всех страниц.
    """
    from config import (FEED_URL, FEED_MAX_PAGES, FEED_PAGE_DELAY_SEC,
                        FEED_PAGE_ATTEMPTS, FEED_RETRY_BACKOFF_SEC)

    cards: list[ProjectCard] = []
    seen_ids: set[int] = set()
    counters = None
    pages_total = None
    page_no = 0
    pages_fetched = 0
    degraded = False
    failed_pages: list[int] = []

    while True:
        page_no += 1
        if page_no > FEED_MAX_PAGES or (max_pages and page_no > max_pages):
            break
        url = FEED_URL if page_no == 1 else f"{FEED_URL}&page={page_no}"

        # Страницу пробуем несколько раз: при быстром обходе биржа изредка
        # отдаёт пустую страницу (проверено 2026-07-25 — страница 18 упала на
        # 17-й подряд, а поодиночке открылась четыре раза из четырёх). Обрыв
        # без повторов делал бы срез неполным на ровном месте, а неполный срез
        # означает, что за прогон никого нельзя признать пропавшим.
        page_cards = None
        for attempt in range(1, FEED_PAGE_ATTEMPTS + 1):
            page = context.new_page()
            try:
                elements = _load_projects_page(page, url)
                scraped_at = time.time()
                if page_no == 1 and pages_total is None:
                    pages_total = _total_pages(page)
                    counters = parse_sidebar_counters(page)
                if not elements:
                    _assert_trustworthy_empty(page)
                    page_cards = []
                    break
                parsed = [_parse_card(el, "feed", scraped_at) for el in elements]
                parsed = [c for c in parsed if c]
                _assert_fields_parsed(parsed, f"страница {page_no}")
                page_cards = parsed
                break
            except (UnreliableRender, UntrustedEmptyResult, PlaywrightTimeout) as e:
                print(f"[scraper] страница {page_no}, попытка {attempt}/{FEED_PAGE_ATTEMPTS}: "
                      f"{type(e).__name__}: {e}")
                if attempt < FEED_PAGE_ATTEMPTS:
                    time.sleep(FEED_RETRY_BACKOFF_SEC * attempt)
            finally:
                page.close()

        if page_cards is None:
            # Пропускаем страницу и идём дальше, а не бросаем обход: раньше
            # один сбой на 15-й странице стоил тридцати страниц наблюдений.
            # Срез всё равно помечается неполным, поэтому никого не похоронит.
            print(f"[scraper] страница {page_no} не далась за {FEED_PAGE_ATTEMPTS} попытки — "
                  f"пропускаю, срез будет неполным")
            failed_pages.append(page_no)
            degraded = True
            if pages_total and page_no >= pages_total:
                break
            time.sleep(FEED_PAGE_DELAY_SEC)
            continue
        if not page_cards:
            break

        pages_fetched += 1
        for card in page_cards:
            if card.order_id not in seen_ids:
                # Номер страницы = место в выдаче: пригодится, чтобы проверить,
                # влияет ли позиция в ленте на число откликов.
                card.feed_page = page_no
                seen_ids.add(card.order_id)
                cards.append(card)

        if pages_total and page_no >= pages_total:
            break
        time.sleep(FEED_PAGE_DELAY_SEC)

    complete = (bool(pages_total) and pages_fetched >= pages_total
                and not max_pages and not degraded)
    if failed_pages:
        print(f"[scraper] не дались страницы: {failed_pages}")
    return FeedResult(
        cards=cards,
        pages_fetched=pages_fetched,
        pages_total=pages_total,
        complete=complete,
        counters=counters,
    )


def fetch_exchange_stats(context: BrowserContext) -> Optional[dict]:
    """Сводка биржи из левой колонки: сколько проектов за месяц, на какую сумму,
    сколько из них стало заказами.

    Это прямое измерение спроса, которое площадка публикует сама, — и оно
    отвечает на вопрос «какая доля размещённых проектов вообще доходит до
    найма» точнее, чем наши наблюдения за отдельными карточками.
    """
    page = context.new_page()
    try:
        page.goto(PROJECTS_URL, wait_until="domcontentloaded")
        page.wait_for_selector(".project-list", timeout=INFORMERS_TIMEOUT_MS)
        text = page.inner_text("body")
        block = re.search(
            r"Статистика биржи.*?Проектов\s*([\d\s ]+).*?сумму,\s*₽\s*([\d\s ]+).*?Заказов\s*([\d\s ]+)",
            text,
            re.S,
        )
        if not block:
            return None
        projects, amount, orders = (_to_int(g) for g in block.groups())
        connects = re.search(r"Осталось\s+(\d+)\s+из\s+(\d+)", text)
        return {
            "captured_at": time.time(),
            "projects_month": projects,
            "amount_month": amount,
            "orders_month": orders,
            "connects_left": int(connects.group(1)) if connects else None,
            "connects_total": int(connects.group(2)) if connects else None,
        }
    finally:
        page.close()


def fetch_all(keywords: list[str]) -> tuple[list[ProjectCard], set[str], Optional[dict]]:
    """Возвращает (карточки, ключевые слова, реально опрошенные в этом прогоне, сводка биржи).

    Второе нужно storage.record_run, чтобы не помечать заказ "пропавшим" из-за
    того, что запрос по его ключевому слову в этом прогоне просто не удался.
    Ключевое слово считается опрошенным, только если выдача либо дала карточки,
    либо доказала, что она честно пуста (см. _assert_trustworthy_empty).
    """
    all_cards: list[ProjectCard] = []
    successful_keywords: set[str] = set()
    stats: Optional[dict] = None
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            viewport={"width": 1280, "height": 900},
        )
        try:
            for i, kw in enumerate(keywords):
                # Один упавший запрос (сетевой сбой/таймаут) не должен ронять
                # весь прогон и терять данные по остальным ключевым словам —
                # пробуем ключевое слово до 3 раз, иначе пропускаем его в этом
                # прогоне и едем дальше.
                for attempt in range(1, 4):
                    try:
                        cards = fetch_keyword(context, kw)
                        all_cards.extend(cards)
                        successful_keywords.add(kw)
                        break
                    except Exception as e:
                        print(f"[scraper] '{kw}' попытка {attempt}/3 не удалась: {e}")
                        if attempt < 3:
                            time.sleep(5 * attempt)
                        else:
                            print(f"[scraper] '{kw}' пропущено в этом прогоне после 3 попыток")
                if i < len(keywords) - 1:
                    time.sleep(REQUEST_DELAY_SEC)
            try:
                stats = fetch_exchange_stats(context)
            except Exception as e:
                # Сводка биржи — приятное дополнение, а не цель прогона:
                # её потеря не должна стоить собранных карточек.
                print(f"[scraper] сводка биржи не снялась: {e}")
        finally:
            context.close()
    return all_cards, successful_keywords, stats


def sweep_feed(max_pages: int | None = None) -> tuple[FeedResult, Optional[dict]]:
    """Полный (или укороченный) обход ленты + сводка биржи за один сеанс браузера."""
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            viewport={"width": 1280, "height": 900},
        )
        try:
            feed = fetch_feed(context, max_pages=max_pages)
            try:
                stats = fetch_exchange_stats(context)
            except Exception as e:
                print(f"[scraper] сводка биржи не снялась: {e}")
                stats = None
            return feed, stats
        finally:
            context.close()


if __name__ == "__main__":
    from config import KEYWORDS

    cards, queried_keywords, stats = fetch_all(KEYWORDS)
    print(f"Собрано карточек: {len(cards)}, ключевых слов опрошено: {len(queried_keywords)}")
    print(f"Сводка биржи: {stats}")
    for c in cards[:5]:
        print(asdict(c))
