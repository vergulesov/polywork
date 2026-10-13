#!/usr/bin/env python3
"""Restore legacy SALES workbook RAW from HH RSS. Append-only, never change reviewed rows."""
import argparse
import json
import os
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import gspread
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "sales-rss.env", override=True)
SPREADSHEET_ID = "1W9JGIjfaJFcZoBcqwX1oSiM6lRhel44BCvhgvCY2UZw"
WORKSHEET = "RAW"
RSS = "https://hh.ru/search/vacancy/rss"
ZONE = ZoneInfo("Asia/Yekaterinburg")
EXPECTED_HEADERS = [
    "Группа", "Дата найдено", "HH ID", "Вакансия", "Компания",
    "Зарплата", "Формат", "Поисковый запрос", "Сниппет", "Ссылка",
    "Статус", "Продукт", "Лиды", "Холодный поиск", "Звонки",
    "Переписка", "ТЗ / спецификации", "Расчёты / подбор",
    "КП", "Документы", "Тендеры",
]
EXTRA_QUERIES = [
    "входящие заявки B2B", "без холодных звонков B2B",
    "менеджер по обработке заявок оборудование",
    "менеджер по работе с готовой базой B2B",
    "коммерческие предложения по ТЗ",
    "работа с чертежами комплектация",
    "продажи промышленного оборудования без поиска",
]
ID_PATTERN = re.compile(r"/vacancy/(\d+)")
SALARY_PATTERN = re.compile(r"(?:от|до)\s*[\d\s\u00a0]+\s*(?:₽|руб|RUB)", re.I)

def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(str(s or "")))).strip()

def item_field(item, key):
    return clean(item.findtext(key) or "")

def company(item, desc):
    for element in item.iter():
        if element.tag.rsplit("}", 1)[-1].lower() in ("author", "employer", "company"):
            value = clean(" ".join(element.itertext()))
            if value:
                return value
    match = re.search(r"Вакансия компании:\s*(.*?)(?=\s+Создана:|\s+Регион:|$)", desc, re.I)
    return match.group(1).strip() if match else ""

def salary(item, desc):
    raw = item_field(item, "salary")
    if raw:
        return raw
    match = re.search(r"Предполагаемый уровень месячного дохода:\s*(.*?)(?=\s+(?:Создана|Регион|Вакансия компании):|$)", desc, re.I)
    if match:
        return match.group(1).strip()
    match = SALARY_PATTERN.search(desc)
    return match.group(0) if match else ""

def publication(item):
    raw = item_field(item, "pubDate")
    try:
        return parsedate_to_datetime(raw).astimezone(ZONE).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return raw

def read_queries():
    filename = ROOT / "sales_queries.json"
    if not filename.exists():
        raise RuntimeError("sales_queries.json missing; restore it from feature/food-equipment-rss")
    entries = json.loads(filename.read_text(encoding="utf-8"))
    terms = [x if isinstance(x, str) else x.get("text", "") for x in entries]
    return list(dict.fromkeys(x.strip() for x in terms + EXTRA_QUERIES if x.strip()))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.days <= 30 or not 1 <= args.pages <= 20:
        parser.error("days must be 1..30; pages 1..20")

    creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_CREDENTIALS_PATH")
    if not creds:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_CREDENTIALS_PATH missing")
    worksheet = gspread.service_account(filename=creds).open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET)
    headers = worksheet.row_values(1)
    if headers[:len(EXPECTED_HEADERS)] != EXPECTED_HEADERS:
        raise RuntimeError("RAW header mismatch. Refusing to modify the sheet")
    existing = set(clean(v) for v in worksheet.col_values(3)[1:] if str(v).strip().isdigit())

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; polywork-legacy-raw/2.0)"
    queries = read_queries()
    found, errors, scanned, full_pages = {}, [], 0, 0
    for text in queries:
        for mode, area in (("Можно удалённо — проверить", 113), ("Челябинск — проверить", 104)):
            previous = set()
            for page in range(args.pages):
                url = RSS + "?" + urlencode({
                    "text": text, "area": area, "search_period": args.days,
                    "order_by": "publication_time", "page": page,
                    **({"work_format": "REMOTE"} if area == 113 else {}),
                })
                try:
                    response = session.get(url, timeout=30)
                    response.raise_for_status()
                    items = ET.fromstring(response.content).findall(".//item")
                    fingerprint = tuple(item_field(i, "link") for i in items)
                    if fingerprint and fingerprint in previous:
                        break
                    previous.add(fingerprint)
                    scanned += len(items)
                    if len(items) >= 20 and page == args.pages - 1:
                        full_pages += 1
                    for item in items:
                        link = item_field(item, "link") or item_field(item, "guid")
                        match = ID_PATTERN.search(link)
                        if not match:
                            continue
                        vid = match.group(1)
                        if vid in existing or vid in found:
                            continue
                        desc = item_field(item, "description")
                        title = item_field(item, "title")
                        if not title:
                            continue
                        found[vid] = [
                            "⚪", datetime.now(ZONE).strftime("%Y-%m-%d %H:%M:%S"),
                            vid, title, company(item, desc), salary(item, desc),
                            mode, text, desc, "https://hh.ru/vacancy/" + vid,
                            "NEW",
                        ]
                    if len(items) < 20:
                        break
                except Exception as exc:
                    errors.append(f"{text} / {mode} / page {page}: {type(exc).__name__}: {exc}")
                    break
                finally:
                    time.sleep(0.5)

    rows = list(found.values())
    written = 0
    if not args.dry_run:
        for start in range(0, len(rows), 50):
            batch = rows[start:start + 50]
            worksheet.append_rows(batch, value_input_option="RAW")
            written += len(batch)
    result = {
        "run_at": datetime.now(ZONE).isoformat(), "sheet": WORKSHEET,
        "queries": len(queries), "rss_items_scanned": scanned,
        "new_unique": len(rows), "written": written,
        "dry_run": args.dry_run, "page_limit_hits": full_pages,
        "errors": errors[:50],
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "legacy_raw_status.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
