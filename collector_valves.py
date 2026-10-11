#!/usr/bin/env python3
"""Independent HH RSS collector for valve/pipeline-equipment sales vacancies.

Targets:
- remote vacancies across Russia;
- vacancies in Chelyabinsk;
- valve / pipeline valve sales only.

Writes only to the "Запорная арматура" sheet in the SALES workbook.
Does not touch other collectors or tabs.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import gspread
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SHEET_ID = os.getenv("VALVES_SPREADSHEET_ID", "1W9JGIjfaJFcZoBcqwX1oSiM6lRhel44BCvhgvCY2UZw")
SHEET_NAME = os.getenv("VALVES_WORKSHEET", "Запорная арматура")
RSS = "https://hh.ru/search/vacancy/rss"
TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
EPOCH = dt.datetime(1899, 12, 30)
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "valves_seen.sqlite3"
STATUS = DATA / "valves_status.json"

HEADERS = [
    "HH ID", "Найдена (Челябинск)", "Опубликована (Челябинск)", "Вакансия",
    "Работодатель", "Зарплата", "Город / регион", "Формат поиска",
    "Ссылка HH", "Поисковый запрос", "Статус", "Холодный поиск — проверить",
    "Командировки / выезды — проверить", "Релевантность", "Описание RSS",
]

PRODUCT_QUERIES = [
    "запорная арматура",
    "трубопроводная арматура",
    "регулирующая арматура",
    "промышленная арматура",
    "шаровые краны",
    "шаровой кран",
    "клапаны",
    "задвижки",
    "дисковые затворы",
    "вентили",
    "трубопроводное оборудование",
    "детали трубопроводов",
    "фланцы",
    "компенсаторы трубопроводов",
]

ROLE_PREFIXES = [
    "менеджер",
    "инженер по продажам",
    "региональный менеджер",
    "менеджер по работе с клиентами",
    "специалист по продажам",
]

SEARCHES = []
for mode, params in [
    ("Удалёнка", {"area": "113", "work_format": "REMOTE"}),
    ("Челябинск", {"area": "104"}),
]:
    # Product-first searches catch vacancies where the product is explicit.
    for product in PRODUCT_QUERIES:
        SEARCHES.append((mode, product, params))

    # Broad sales-role searches are needed because some manufacturers publish
    # generic titles/descriptions and HH RSS may omit the product wording.
    for role in ROLE_PREFIXES:
        SEARCHES.append((mode, role, params))

    # A few targeted employer-name searches recover generic roles at key makers.
    for employer in ["ALSO", "АЛСО", "LD", "АРМАКОМ", "УЗТА", "УралАрм", "СпецАрматура"]:
        SEARCHES.append((mode, employer, params))


TARGET = re.compile(
    r"запорн.{0,12}арматур|трубопроводн.{0,12}арматур|регулирующ.{0,12}арматур|"
    r"промышленн.{0,12}арматур|шаров.{0,8}кран|клапан|задвижк|затвор|вентил|"
    r"трубопроводн.{0,12}оборудован|детал.{0,12}трубопровод|фланц|"
    r"компенсатор.{0,12}трубопровод",
    re.I,
)
SALES = re.compile(
    r"менеджер|продаж|инженер.{0,12}продаж|региональн.{0,12}(?:менеджер|представител)|"
    r"аккаунт|клиент|коммерч|сбыт|торгов.{0,12}представител|работа с клиент",
    re.I,
)

FOCUSED_EMPLOYERS = {
    "also",
    "алсо",
    "уральский завод трубопроводной арматуры",
    "армаком",
    "узта",
    "ураларм",
    "спецарматура",
}

DIVERSIFIED_EMPLOYERS = {
    "группа компаний ld",
}

def employer_name(desc):
    return field(desc, "Вакансия компании").strip()

def normalize_employer(name):
    return re.sub(r"[^a-zа-я0-9]+", " ", name.lower()).strip()
FALSE_POSITIVE = re.compile(
    r"арматурщик|вязк.{0,8}арматур|железобетон|жби|бетонщик|монолит|"
    r"слесарь|токарь|фрезеровщик|сварщик|кладовщик|водитель|грузчик",
    re.I,
)
COLD = re.compile(r"холодн|поиск клиент|привлечени.{0,20}нов|самостоятельн.{0,20}поиск|активн.{0,10}поиск", re.I)
TRAVEL = re.compile(r"командиров|выезд|разъезд|полев", re.I)

def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()

def field(desc, key):
    m = re.search(
        re.escape(key) + r":\s*(.*?)(?=\s+(?:Вакансия компании|Регион|Предполагаемый уровень месячного дохода|Создана):|$)",
        desc,
        re.I,
    )
    return m.group(1).strip() if m else ""

def serial(value):
    if not value:
        return ""
    try:
        x = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            x = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return ""
    if x.tzinfo is None:
        return ""
    return (x.astimezone(TIMEZONE).replace(tzinfo=None) - EPOCH).total_seconds() / 86400

def fetch_items(session, mode, query, base_params, days, pages):
    found = []
    for page in range(pages):
        params = dict(base_params)
        params.update({
            "text": query,
            "search_period": days,
            "order_by": "publication_time",
            "page": page,
        })
        response = session.get(RSS + "?" + urlencode(params), timeout=25)
        response.raise_for_status()
        items = ET.fromstring(response.content).findall(".//item")
        for item in items:
            link = clean(item.findtext("link") or item.findtext("guid"))
            match = re.search(r"/vacancy/(\d+)", link)
            if match:
                found.append((match.group(1), mode, query, item))
        if len(items) < 20:
            break
        time.sleep(0.35)
    return found

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--pages", type=int, default=5)
    args = ap.parse_args()
    if not 1 <= args.days <= 30 or not 1 <= args.pages <= 20:
        ap.error("days: 1..30, pages: 1..20")

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; PolyWorkValvesRSS/1.0)"

    seen = {}
    errors = []
    scanned = 0
    for mode, query, params in SEARCHES:
        try:
            for vid, mode, query, item in fetch_items(session, mode, query, params, args.days, args.pages):
                scanned += 1
                title = clean(item.findtext("title"))
                desc = clean(item.findtext("description"))
                text = title + " " + desc
                if FALSE_POSITIVE.search(text):
                    continue
                employer_text = employer_name(desc)
                employer_key = normalize_employer(employer_text)
                is_sales = bool(SALES.search(title + " " + desc))
                is_product = bool(TARGET.search(text))
                is_focused_employer = employer_key in FOCUSED_EMPLOYERS
                is_diversified_employer = employer_key in DIVERSIFIED_EMPLOYERS

                # Focused valve makers (e.g. ALSO, UZTA) may use generic sales titles,
                # so sales roles from them are accepted even when RSS omits the product.
                #
                # Diversified groups (e.g. LD) have many unrelated product lines;
                # for them we still require valve/pipeline product wording.
                if not is_sales:
                    continue
                if not (is_product or is_focused_employer):
                    continue
                if is_diversified_employer and not is_product:
                    continue
                if vid not in seen:
                    seen[vid] = (mode, query, title, desc, clean(item.findtext("pubDate")))
                elif seen[vid][0] != "Челябинск" and mode == "Челябинск":
                    seen[vid] = (mode, query, title, desc, clean(item.findtext("pubDate")))
        except Exception as exc:
            errors.append(f"{mode}/{query}: {type(exc).__name__}: {exc}")
        time.sleep(0.35)

    now_value = serial(dt.datetime.now(dt.timezone.utc).isoformat())
    rows = []
    for vid, (mode, query, title, desc, published) in seen.items():
        text = title + " " + desc
        relevance = []
        if re.search(r"запорн.{0,12}арматур", text, re.I):
            relevance.append("запорная")
        if re.search(r"трубопроводн.{0,12}арматур", text, re.I):
            relevance.append("трубопроводная")
        if re.search(r"регулирующ.{0,12}арматур", text, re.I):
            relevance.append("регулирующая")
        if re.search(r"шаров.{0,8}кран", text, re.I):
            relevance.append("шаровые краны")
        if re.search(r"клапан|задвижк|затвор|вентил", text, re.I):
            relevance.append("клапаны/задвижки")
        if re.search(r"детал.{0,12}трубопровод|фланц|компенсатор", text, re.I):
            relevance.append("детали трубопроводов")
        rows.append([
            vid,
            now_value,
            serial(published),
            title,
            field(desc, "Вакансия компании"),
            field(desc, "Предполагаемый уровень месячного дохода"),
            field(desc, "Регион"),
            mode,
            f"https://hh.ru/vacancy/{vid}",
            query,
            "Проверить",
            "Есть признаки холодного поиска" if COLD.search(text) else "По RSS неясно",
            "Есть признаки выездов" if TRAVEL.search(text) else "По RSS неясно",
            ", ".join(dict.fromkeys(relevance)) or "проверить",
            desc,
        ])

    result = {
        "last_run": dt.datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "searches": len(SEARCHES),
        "scanned": scanned,
        "matched": len(rows),
        "mode": "HH RSS only; remote Russia + Chelyabinsk",
        "sheet": SHEET_NAME,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False))

    if not args.write:
        print("DRY RUN; no changes")
        return 1 if errors else 0

    credentials = os.getenv("GOOGLE_CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not credentials:
        raise RuntimeError("Set GOOGLE_CREDENTIALS_PATH or GOOGLE_APPLICATION_CREDENTIALS")

    ws = gspread.service_account(filename=credentials).open_by_key(SHEET_ID).worksheet(SHEET_NAME)
    if ws.row_values(1) != HEADERS:
        raise RuntimeError(f"{SHEET_NAME} headers differ; refusing to write")

    existing = {str(x).strip() for x in ws.col_values(1)[1:] if str(x).strip()}
    with sqlite3.connect(DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, first_seen TEXT)")
        db.executemany(
            "INSERT OR IGNORE INTO seen VALUES (?, ?)",
            [(x, dt.datetime.now(TIMEZONE).isoformat()) for x in existing],
        )
        db.commit()
        fresh = [row for row in rows if row[0] not in existing and not db.execute("SELECT 1 FROM seen WHERE id=?", (row[0],)).fetchone()]
        for start in range(0, len(fresh), 100):
            batch = fresh[start:start + 100]
            ws.append_rows(batch, value_input_option="RAW")
            db.executemany(
                "INSERT OR IGNORE INTO seen VALUES (?, ?)",
                [(row[0], dt.datetime.now(TIMEZONE).isoformat()) for row in batch],
            )
            db.commit()

    result["written"] = len(fresh)
    result["duplicates_skipped"] = len(rows) - len(fresh)
    STATUS.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 1 if errors else 0

if __name__ == "__main__":
    raise SystemExit(main())
