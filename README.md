# CSFloat bot

Массовая обработка Steam-аккаунтов на Playwright/Camoufox (async).

**Модуль 1 (готов):** вход на CSFloat через Steam OpenID с кодом Steam Guard из maFile,
прохождение окна Onboard (согласия + почта), подтверждение письма из Outlook.
**Модуль 2 (заглушка):** получение API-ключа CSFloat — подключается одним классом,
остальной код не меняется.

---

## Установка

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m camoufox fetch          # ~150 МБ, один раз: сборка Firefox с anti-detect
```

Если Camoufox по какой-то причине не нужен — в `config.yaml` поставь
`browser.engine: firefox` (или `chromium`) и выполни `playwright install firefox`.
Остальной код от движка не зависит.

> Если при импорте Camoufox падает с `ImportError: cannot import name 'CLoader' from 'yaml'` —
> системный PyYAML собран без libyaml. Лечится `pip install --ignore-installed pyyaml`.

## Профили и отпечатки

Бот работает как антидетект-браузер: **профиль на аккаунт**, а не общий браузер с
подчищенными cookies.

* `browser.persistent_profile: true` — Camoufox запускается с `user_data_dir =
  profiles/<login>`. Сохраняется весь профиль Firefox: cookies, localStorage,
  IndexedDB, service workers, кеш, история. CSFloat и почта живут во вкладках
  **одного окна**, как у живого человека, а не в изолированных контекстах.
* `browser.pin_fingerprint: true` — отпечаток генерируется один раз и закрепляется за
  аккаунтом в `state/<login>.fp.json`. Без этого Camoufox на каждом запуске выдаёт новые
  seed'ы `canvas`, `audio` и `fonts:spacing`, и аккаунт с живыми cookies всё равно
  выглядит как новое устройство. Гео-свойства (`timezone`, `navigator.language`,
  `webrtc:*`, `geolocation:*`) намеренно **не** закрепляются — их каждый раз
  пересчитывает `geoip` под текущий IP прокси.

Отпечаток переживает сброс аккаунта: `forget()` удаляет cookies и профиль, но оставляет
`*.fp.json` — иначе сброс сессии превращал бы аккаунт в новое устройство.

Профиль занимает порядка 50–150 МБ с кешем; при сотнях аккаунтов это заметно.
Кеш отключается через `browser.enable_cache: false`.

## Входные данные

| Файл | Формат |
|---|---|
| `accounts.txt` | `login:pass:mail:mailpassword` — по строке на аккаунт |
| `proxies.txt` | один прокси на строку; **1 прокси = 1 аккаунт, привязка по порядку строк** |
| `mafiles/` | maFile'ы Steam Desktop Authenticator; сопоставляются по полю `account_name` **внутри** файла |

Форматы прокси распознаются автоматически:
`scheme://user:pass@host:port`, `scheme://host:port`, `user:pass@host:port`,
`host:port:user:pass`, `host:port` (схема по умолчанию — `proxy.default_scheme`).

> SOCKS5 **с авторизацией**: Playwright/Firefox её не поддерживают, поэтому бот поднимает
> локальный SOCKS-релей на 127.0.0.1 и отдаёт браузеру его адрес (`bot/proxy_relay.py`).
> Отключается через `proxy.relay_socks_auth: false`.

Проверить входные файлы, ничего не запуская:

```bash
python main.py --check
```

## Личные настройки

`config.yaml` лежит в репозитории, поэтому правки в нём конфликтуют с `git pull`.
Всё своё клади в `config.local.yaml` — он в `.gitignore`, перекрывает `config.yaml`
по тем ключам, которые в нём указаны, и остальное берёт из основного файла:

```bash
cp config.local.example.yaml config.local.yaml
```

## Запуск

```bash
python main.py                          # прогон по config.yaml
python main.py --threads 5              # параллельность (asyncio.Semaphore)
python main.py --only steamuser1        # один аккаунт
python main.py --only steamuser1 --force --debug   # отладка: паузы на каждом шаге
python main.py --web                    # веб-интерфейс, http://127.0.0.1:8000
```

Прогон **возобновляемый**: `results.csv` хранит строку на пару (логин, модуль),
аккаунты со статусом `done` при перезапуске пропускаются (`--force` — не пропускать).

## Сброс аккаунта

```bash
python main.py --reset --only <login>     # cookies + профиль браузера + статусы
python main.py --reset                    # все аккаунты
```

Закреплённый отпечаток (`state/<login>.fp.json`) сохраняется: он должен пережить сброс,
иначе аккаунт снова станет новым устройством. Сброс профиля заодно чистит кеш Firefox,
включая записи Alt-Svc — они живут в профиле и умеют ломать доступ к конкретному сайту
через прокси.

## Диагностика одной страницы

Когда сайт открывается руками, но не открывается в боте:

```bash
python main.py --probe                    # csfloat.com
python main.py --probe https://outlook.live.com/mail/0/ --only <login> --debug
```

Открывает URL в том же браузере, профиле и прокси, что и боевой прогон, и печатает:
движок и применённые настройки, HTTP-статус с заголовками, размер HTML и видимого текста,
отрисовалось ли вообще что-нибудь — и **таблицу кандидатов в селекторы**. Упавшие запросы
и JS-ошибки идут в лог, скриншот, HTML и полный JSON — в `debug/<login>/`.

Элементы, которых нет в DOM до клика (пункты выпадающих меню), снимаются вторым срезом:

```bash
python main.py --probe --click "img.avatar"
```

## Снятие селекторов (делается один раз)

Точный flow CSFloat и Outlook заранее неизвестен, поэтому селекторы вынесены в
`selectors.yaml` **списками кандидатов** — пробуются по порядку, первый видимый выигрывает.

```bash
python main.py --only steamuser1 --debug --force
```

Бот останавливается до и после каждого шага и ждёт команду:

| Клавиша | Действие |
|---|---|
| `Enter` | следующий шаг |
| `d` | выгрузить кандидатов в `debug/<login>/*.json` + HTML + скриншот |
| `s` | только скриншот |
| `p` | Playwright Inspector (`page.pause()`) |
| `c` | доработать аккаунт без пауз |
| `q` | прервать аккаунт |

По `d` в консоль печатается таблица вида `button  #idSIButton9  Next` — готовые
кандидаты для `selectors.yaml`. В веб-интерфейсе `selectors.yaml` правится прямо в
браузере и перечитывается без перезапуска процесса.

Спец-синтаксис: селектор `url:<regex>` проверяет текущий URL, а не DOM.

## Веб-интерфейс

```bash
python main.py --web            # хост/порт — в config.yaml, секция web
```

![Веб-интерфейс](docs/preview-web.png)

* старт/стоп прогона, потоки, `--only`, видимый браузер;
* живой лог (SSE) и таблица статусов, обновляемая в реальном времени;
* загрузка `accounts.txt` / `proxies.txt` / maFile'ов через браузер;
* просмотр скриншотов и HTML ошибок по каждому аккаунту;
* редактирование `config.yaml` и `selectors.yaml` с бэкапом и валидацией YAML;
* сброс статуса аккаунта и его cookies.

Слушает `127.0.0.1` — наружу выставлять нельзя: внутри пароли.
Если нужен доступ по сети, задай `web.token` и открывай `http://host:port/?token=...`.

## Статусы в results.csv

| Статус | Что значит | Повтор |
|---|---|---|
| `done` | модуль выполнен | пропускается |
| `error` | исчерпаны попытки на сетевой/таймаутной ошибке | да |
| `bad_credentials` | Steam: неверный логин/пароль | нет |
| `steam_locked` | аккаунт заблокирован | нет |
| `steam_rate_limited` | слишком много попыток входа с IP | нет |
| `steam_email_code_required` | Steam просит код с почты (нет мобильного аутентификатора) | нет |
| `steam_mobile_confirm_required` | Steam просит подтверждение в приложении, и перехода на ввод кода на странице нет | нет |
| `no_mafile` | maFile не найден или зашифрован | нет |
| `captcha` | обнаружена капча, решалка не подключена | нет |
| `mail_bad_credentials` | неверный пароль почты | нет |
| `mail_blocked` | Outlook заблокировал аккаунт | нет |
| `mail_verify_required` | Outlook требует верификацию личности, и перехода на ввод пароля нет | нет |
| `mail_not_received` | письмо не пришло за `timeouts.mail_wait_s` | да |
| `proxy_auth_failed` | прокси не принял логин/пароль (407) | нет |
| `browser_missing` | движок браузера не скачан — прогон останавливается сразу | нет |
| `not_implemented` | модуль-заглушка (api_key) | нет |

При любой ошибке в `errors/<login>/` кладутся скриншот, HTML и текст ошибки.
Пароли вырезаются из логов и дампов фильтром `bot/logging_setup.py`.

## Структура

```
config.yaml          настройки            selectors.yaml   селекторы (кандидатами)
main.py              CLI                  web/             веб-интерфейс (FastAPI + SSE)
bot/
  loader.py          accounts/proxies/mafiles + связывание 1:1
  steam_guard.py     TOTP Steam + синхронизация времени с сервером Steam
  browser.py         1 аккаунт = 1 браузер (Camoufox) = 1 прокси = 1 профиль = 1 отпечаток
  proxy_relay.py     локальный SOCKS5-релей для прокси с авторизацией
  runner.py          очередь, Semaphore, ретраи, предохранитель
  storage.py         results.csv, state/<login>.json, state/<login>.fp.json,
                     profiles/<login>/, errors/<login>/
  context.py         AccountContext + ctx.step() (лог, пауза в debug, дамп при падении)
  captcha.py         детект + интерфейс решалки (NullSolver)
  pages/             base (движок селекторов), steam_login, csfloat, mail/outlook_web
  modules/           registration (модуль 1), api_key (модуль 2, заглушка)
```

## Как добавится модуль 2

```python
# bot/modules/api_key.py
@register("api_key")
class ApiKeyModule:
    async def run(self, ctx):
        cs = CsFloatPage(await ctx.session.page("csfloat"), ctx)
        async with ctx.step("open"):        await cs.open_home()
        async with ctx.step("generate"):    ctx.data["api_key"] = await cs.generate_api_key()
```

и в `config.yaml`: `run.modules: [registration, api_key]`.
Браузер к этому моменту уже поднят, cookies CSFloat восстановлены из `state/<login>.json`,
статус пишется отдельной строкой `module=api_key`. Runner, loader и storage не трогаются.

## Ограничения

* Капча не решается — аккаунт получает статус `captcha`. Точка подключения решалки:
  `bot/captcha.py:build_solver`.
* Steam Guard берётся только из maFile. Экран «подтвердите вход в приложении» — штатный
  для аккаунта с maFile: бот сам переходит по ссылке «ввести код вместо этого» и вводит
  код; фатальный статус остаётся только если такой ссылки на странице нет. Вкладка с
  QR-кодом тоже распознаётся — бот переключается на форму логина. Экран «код на почту»
  детектируется, но не автоматизируется.
* Селекторы CSFloat/Outlook в `selectors.yaml` — черновые, снимаются в `--debug`.
* CSFloat часто не подхватывает сессию с первого редиректа со Steam. Бот повторяет вход
  внутри той же сессии (`csfloat.login_attempts`, по умолчанию 3): Steam на втором заходе
  уже авторизован, поэтому повтор идёт без пароля и кода Steam Guard.
* Вход по коду вместо пароля (Microsoft всё чаще делает его вариантом по умолчанию) бот
  обходит сам: жмёт «Use your password», при необходимости через «Other ways to sign in».
  Этот экран внешне совпадает с настоящей верификацией личности и отличается только
  наличием перехода на пароль — если перехода нет, аккаунт честно помечается
  `mail_verify_required`.
* Профиль и закреплённый отпечаток убирают связывание аккаунтов **по отпечатку браузера**.
  Они не влияют на связывание по IP, по поведению и по скорости регистраций — от банов
  за массовую регистрацию это не спасает, на это работают паузы, `run.start_jitter`
  и предохранитель.
