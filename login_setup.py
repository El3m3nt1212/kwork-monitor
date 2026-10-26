"""
Разовая настройка: открывает управляемый Playwright браузер с постоянным
профилем, пользователь логинится в Kwork вручную как обычно, после чего
сессия сохраняется на диск в PROFILE_DIR и переиспользуется scraper.py без
повторного логина.

Запуск:
    python login_setup.py
"""
from playwright.sync_api import sync_playwright

from config import PROFILE_DIR, BASE_URL


def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE_URL)
        print("Залогинься в Kwork в открывшемся окне (как обычно, вручную).")
        print("Когда увидишь свой ник/баланс в шапке — вернись сюда и нажми Enter.")
        input()

        try:
            # Логин мог ещё сам куда-то редиректить страницу — открываем
            # проверку в новой вкладке, чтобы не словить гонку навигации.
            check_page = context.new_page()
            check_page.goto(f"{BASE_URL}/projects", wait_until="networkidle", timeout=15000)
            logged_in = check_page.locator("text=Коннекты").count() > 0
            if logged_in:
                print("Похоже, сессия авторизована — вижу блок 'Коннекты' на бирже заказов.")
            else:
                print("Не нашёл признаков авторизации на /projects. Проверь вручную и перезапусти при необходимости.")
            check_page.close()
        except Exception as e:
            print(f"Проверка авторизации не удалась ({e}), но профиль на диске уже мог сохранить сессию — просто попробуй run_once.py.")

        context.close()
        print(f"Профиль сохранён в {PROFILE_DIR}. Дальше запускай run_once.py — новый логин не нужен.")


if __name__ == "__main__":
    main()
