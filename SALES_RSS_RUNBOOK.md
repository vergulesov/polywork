# Быстрый запуск сборщика SALES RAW (только RSS)

Этот процесс **отдельный** от `polywork-ops`. Исходный лист RAW и старый `collector_hh.py` не меняются. Никакого HH API.

## Шаг 1. Настроить целевую таблицу

Создать локальный файл `/opt/polywork/sales-rss.env` на сервере (его **не коммитить** в GitHub):

```ini
SPREADSHEET_ID=ID_ТАБЛИЦЫ_ПРОДАЖИ
GOOGLE_APPLICATION_CREDENTIALS=/opt/polywork/google-service-account.json
```

ID взять из URL именно таблицы «ПРОДАЖИ», а **не** из `.env.example`: там ID таблицы PolyWork-подработок. Service account должен иметь редакторский доступ к таблице. Файл с секретами никогда не публиковать.

## Шаг 2. Разовый восстановительный запуск за три дня

```bash
cd /opt/polywork
git pull --ff-only
.venv/bin/python -m py_compile sales_vacancies.py
set -a; . ./sales-rss.env; set +a
.venv/bin/python -u sales_vacancies.py --days 3 --pages 3 --raw-only
.venv/bin/python -u sales_vacancies.py --days 3 --pages 3 --write --raw-only
```

Первый прогон выводит статистику без записи; второй пишет **только** в отдельный `SALES_RAW_V2`. В `SALES_CANDIDATES` и `SALES_APPLY` новых кандидатов не добавляет, пока классификатор не откалиброван. Прогон повторять безопасно: повторные HH ID из листа не дописываются.

Скрипт запрашивает HH RSS с `search_period=3`; не гарантирует полный охват всех вакансий из-за ограниченного количества страниц и поисковых фраз. Найденные вакансии до проверки полной карточки нужно считать предварительно подходящими. Зарплата без подтверждённой нижней границы >=80 тыс. ₽ сейчас не попадает в `SALES_RAW_V2`; это источник возможных пропусков.

## Шаг 3. Включить расписание

```bash
sudo cp deploy/sales-rss.service /etc/systemd/system/
sudo cp deploy/sales-rss.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sales-rss.timer
systemctl list-timers --all | grep sales-rss
sudo journalctl -u sales-rss.service -n 40 --no-pager
```

Интервал: 30 минут после окончания предыдущего выполнения. Первую ручную запись запустить перед включением таймера, чтобы проверить доступ Google Sheets.

## Проверить безопасность

```bash
systemctl is-active sales-rss.timer
systemctl is-active polywork-hh.timer
systemctl is-active polywork-ops.timer
```

Если `polywork-hh.timer` включён, он продолжит писать старый RAW — решение об отключении принимать отдельно после проверки, а не случайно отключать чужой сбор.

Не запускать `sales_vacancies.py --write` без `--raw-only` пока не проверили 70-балльную оценку. Telegram-оповещений пока нет.
