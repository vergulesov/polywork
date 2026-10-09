#!/usr/bin/env python3
"""Chelyabinsk regional sales vacancies: HH RSS only, independent REGIONAL RAW."""
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import gspread
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "regional_sales_seen.sqlite3"
STATUS = DATA / "regional_sales_status.json"
QUERIES = ROOT / "regional_sales_queries.json"
RSS_URL = "https://hh.ru/search/vacancy/rss"
HEADERS = ["Собрано", "Компания (RSS)", "Вакансия", "Ссылка HH", "Зарплата (RSS)",
           "Город / регион (RSS)", "Дата публикации", "Запрос", "Описание (RSS)",
           "HH ID", "Статус", "Оклад от 80 — проверка", "Выезды только ЧО — проверка",
           "Продукт — проверка", "Атмосфера — проверка"]
ID_RE = re.compile(r"/vacancy/(\\d+)")
SALARY_RE = re.compile(r"(?:от|до)\\s*[\\d\\s\\u00a0]+\\s*(?:₽|руб|RUB)", re.I)


def clean(s):
    from html import unescape
    return re.sub(r"\\s+", " ", re.sub(r"<[^>]*>", " ", unescape(str(s or "")))).strip()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def txt(item, name):
    return clean(item.findtext(name) or "")


def published(item):
    raw = txt(item, "pubDate")
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return raw


def location(item):
    # HH RSS may expose location in namespaces or formatted description.
    for el in item.iter():
        if el.tag.rsplit("}", 1)[-1].lower() in {"location", "city", "region"} and el.text:
            return clean(el.text)
    return ""


def employer(item):
    for el in item.iter():
        if el.tag.rsplit("}", 1)[-1].lower() in {"author", "employer", "company"}:
            name = clean(" ".join(el.itertext()))
            if name:
                return name
    desc = txt(item, "description")
    m = re.search(r"Вакансия компании:\\s*(.+?)(?=\\s+Создана:|$)", desc, re.I)
    return m.group(1).strip() if m else ""


def main():
    sid, creds = os.getenv("GOOGLE_SPREADSHEET_ID"), os.getenv("GOOGLE_CREDENTIALS_PATH")
    if not sid or not creds:
        raise RuntimeError("GOOGLE_SPREADSHEET_ID and GOOGLE_CREDENTIALS_PATH required")
    sh = gspread.service_account(filename=creds).open_by_key(sid)
    sheet = os.getenv("REGIONAL_RAW_WORKSHEET", "REGIONAL RAW")
    try:
        ws = sh.worksheet(sheet)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet, rows=2000, cols=len(HEADERS))
    if ws.col_count < len(HEADERS):
        ws.add_cols(len(HEADERS) - ws.col_count)
    first = ws.row_values(1)
    if first and first != HEADERS:
        raise RuntimeError("Regional RAW headers differ; refusing to overwrite")
    if not first:
        ws.update(range_name="A1:O1", values=[HEADERS])
    existing = {str(i).strip() for i in ws.col_values(10)[1:] if str(i).strip().isdigit()}
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; regional-vacancy-rss/1.0)"
    errors, pending, local_seen = [], [], set()
    scanned = 0
    with sqlite3.connect(DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, first_seen TEXT)")
        db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", [(i, now()) for i in existing])
        db.commit()
        for query in queries:
            try:
                params = dict(query["params"])
                params["text"] = query["text"]
                url = RSS_URL + "?" + urlencode(params)
                response = session.get(url, timeout=25)
                response.raise_for_status()
                root = ET.fromstring(response.content)
                for item in root.findall(".//item"):
                    link = txt(item, "link") or txt(item, "guid")
                    match = ID_RE.search(link)
                    if not match:
                        continue
                    vid = match.group(1)
                    scanned += 1
                    if vid in existing or vid in local_seen or db.execute("SELECT 1 FROM seen WHERE id=?", (vid,)).fetchone():
                        continue
                    local_seen.add(vid)
                    desc = txt(item, "description")
                    salary = txt(item, "salary")
                    if not salary:
                        m = SALARY_RE.search(desc)
                        salary = m.group(0) if m else ""
                    row = [now(), employer(item), txt(item, "title"),
                           "https://hh.ru/vacancy/" + vid, salary, location(item),
                           published(item), query["name"], desc, vid, "Не проверена",
                           "Неизвестно", "Неизвестно", "Неизвестно", "Неизвестно"]
                    pending.append((vid, row))
            except Exception as exc:
                errors.append(f'{query["name"]}: {type(exc).__name__}: {exc}')
            time.sleep(0.6)
        written = 0
        for index in range(0, len(pending), 50):
            batch = pending[index:index + 50]
            try:
                ws.append_rows([row for _, row in batch], value_input_option="RAW")
                db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)",
                               [(vid, now()) for vid, _ in batch])
                db.commit()
                written += len(batch)
            except Exception as exc:
                errors.append(f"append: {type(exc).__name__}: {exc}")
                break
    result = {"last_run": now(), "queries": len(queries), "scanned": scanned,
              "candidates": len(pending), "written": written, "errors": errors,
              "mode": "HH RSS only", "sheet": sheet}
    STATUS.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
