#!/usr/bin/env python3
"""HH RSS -> PolyWork / Фасовка. No HH API, no outreach or automatic applications."""
import argparse
import datetime as dt
import os
import re
import sqlite3
import time
import html
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import gspread
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
SHEET_ID = os.getenv("PACKING_SPREADSHEET_ID", "1DLlrob85ahsfBRkhQFGFMqH23L5PnM9ksOF7x4jPY_I")
SHEET_NAME = "Фасовка"
AREA = os.getenv("PACKING_HH_AREA", "104")  # Chelyabinsk city
TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
EPOCH = dt.datetime(1899, 12, 30)
RSS = "https://hh.ru/search/vacancy/rss"
QUERIES = [
    "фасовщик", "упаковщик", "фасовщик упаковщик", "маркировщик",
    "стикеровщик", "сортировщик", "комплектовщик", "упаковщик заказов",
    "сборщик изделий", "сборщик мелких деталей", "контролер качества",
    "контролер ОТК", "оператор упаковки", "укладчик упаковщик",
]
TARGET = re.compile(r"фасов|упаков|маркиров|стикеров|сортиров|комплектов|сборщик|контрол[её]р|укладчик", re.I)
EXCLUDE = re.compile(r"менеджер|руководител|продаж|колл.?центр|оператор поддержки|преподавател|курьер|водитель|продавец|грузчик", re.I)
HEAVY = re.compile(r"тяж[её]л|разгруз|погруз|подъ[её]м.*кг|вес.*(?:15|20|25|30) кг|рохл|паллет|погрузчик", re.I)
NIGHTS = re.compile(r"ночн|сутки|24.?час|дневн.*ночн", re.I)
CONTACT = re.compile(r"клиент|покупател|звонк|консультаци|общени|работа с людьми|продаж", re.I)
SCAM = re.compile(r"на дому|домашняя сборка|предоплат|страхов[оы]й взнос|выкуп материалов|вступительн[ыо]й взнос", re.I)
HEADERS = ["HH ID", "Найдена (Челябинск)", "Опубликована (Челябинск)", "Вакансия",
           "Работодатель", "Зарплата", "Город", "График / формат (RSS)",
           "Тип работы", "Общение", "Физнагрузка / риски", "Ссылка HH",
           "Поисковый запрос", "Статус", "Описание RSS"]

def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()

def field(s, key):
    m = re.search(re.escape(key) + r":\s*(.*?)(?=\s+(?:Вакансия компании|Регион|Предполагаемый уровень месячного дохода|Создана):|$)", s, re.I)
    return m.group(1).strip() if m else ""

def serial(value):
    if not value:
        return ""
    try:
        x = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if x.tzinfo is None:
            return ""
        return (x.astimezone(TIMEZONE).replace(tzinfo=None) - EPOCH).total_seconds() / 86400
    except ValueError:
        return ""

def get_items(session, q, days, pages):
    result = []
    for page in range(pages):
        params = {"text": q, "area": AREA, "search_period": days,
                  "order_by": "publication_time", "page": page}
        response = session.get(RSS + "?" + urlencode(params), timeout=25)
        response.raise_for_status()
        items = ET.fromstring(response.content).findall(".//item")
        for item in items:
            link = clean(item.findtext("link") or item.findtext("guid"))
            m = re.search(r"/vacancy/(\d+)", link)
            if m:
                result.append((m.group(1), item))
        if len(items) < 20:
            break
        time.sleep(0.4)
    return result

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--pages", type=int, default=2)
    args = ap.parse_args()
    if not 1 <= args.days <= 30 or not 1 <= args.pages <= 20:
        ap.error("days: 1..30, pages: 1..20")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; PolyWorkPackingRSS/1.0)"
    seen = {}
    errors = []
    for q in QUERIES:
        try:
            for vid, item in get_items(session, q, args.days, args.pages):
                title = clean(item.findtext("title"))
                if not TARGET.search(title) or EXCLUDE.search(title):
                    continue
                desc = clean(item.findtext("description"))
                if vid not in seen:
                    seen[vid] = (q, title, desc, clean(item.findtext("pubDate")))
        except Exception as exc:
            errors.append(f"{q}: {type(exc).__name__}: {exc}")
        time.sleep(0.4)
    now = serial(dt.datetime.now(dt.timezone.utc).isoformat())
    rows = []
    excluded_hard = 0
    for vid, (q, title, desc, published) in seen.items():
        if HEAVY.search(title + " " + desc) or NIGHTS.search(title + " " + desc) or SCAM.search(title + " " + desc):
            excluded_hard += 1
            continue
        risks = []
        if HEAVY.search(title + " " + desc):
            risks.append("Тяжести / погрузка — проверить")
        if NIGHTS.search(desc):
            risks.append("Ночные смены — проверить")
        if SCAM.search(desc + " " + title):
            risks.append("Предоплата / работа на дому — проверить")
        rows.append([vid, now, serial(published), title, field(desc, "Вакансия компании"),
                     field(desc, "Предполагаемый уровень месячного дохода"),
                     field(desc, "Регион"), "По RSS не подтверждён", q,
                     "Есть контакт с людьми — проверить" if CONTACT.search(title + " " + desc) else "По RSS неясно",
                     "; ".join(risks) if risks else "Неясно по RSS",
                     f"https://hh.ru/vacancy/{vid}", q, "Проверить", desc])
    print(f"unique={len(rows)} excluded_hard={excluded_hard} errors={len(errors)}")
    for err in errors[:20]:
        print("ERROR", err)
    if not args.write:
        print("DRY RUN; no changes")
        return 1 if errors else 0
    credentials = os.getenv("GOOGLE_CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not credentials:
        raise RuntimeError("Set GOOGLE_CREDENTIALS_PATH or GOOGLE_APPLICATION_CREDENTIALS")
    ws = gspread.service_account(filename=credentials).open_by_key(SHEET_ID).worksheet(SHEET_NAME)
    if ws.row_values(1) != HEADERS:
        raise RuntimeError("Фасовка headers differ; refusing to write")
    existing = set(ws.col_values(1)[1:])
    fresh = [r for r in rows if r[0] not in existing]
    for start in range(0, len(fresh), 100):
        ws.append_rows(fresh[start:start+100], value_input_option="RAW")
    print(f"written={len(fresh)} duplicates_skipped={len(rows)-len(fresh)}")
    return 1 if errors else 0

if __name__ == "__main__":
    raise SystemExit(main())
