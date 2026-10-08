#!/usr/bin/env python3
"""Independent HH RSS -> PolyWork RAW collector. Does not touch the primary job search."""
import json
import os
import re
import sqlite3
import time
from datetime import datetime
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
DB = DATA / "polywork_ops_seen.sqlite3"
STATUS = DATA / "polywork_ops_status.json"
QUERIES = ROOT / "polywork_ops_queries.json"
SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
CREDS = os.environ.get("GOOGLE_CREDENTIALS_PATH", "")
RSS_URL = "https://hh.ru/search/vacancy/rss"
ID_RE = re.compile(r"/vacancy/(\d+)")
BASE_HEADERS = ["Дата", "Компания", "Вакансия", "Ссылка", "Зарплата", "Формат",
                "График", "Занятость", "Опыт", "Async", "Automation", "Fast hire",
                "Sync load", "Статус", "Комментарий", "Human residue",
                "Как автоматизировать / условие оценки", "Vacancy key",
                "PolyWork status", "Причина / риск", "Maintenance h/week"]
EXTRA_HEADERS = ["Источник", "Дата публикации", "Последняя проверка",
                 "Описание вакансии (HH)", "Доступность — факт", "Тип оплаты"]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


def name(v):
    if isinstance(v, dict):
        return v.get("name") or v.get("id") or ""
    if isinstance(v, list):
        return ", ".join(name(x) for x in v)
    return str(v or "")


def salary(d):
    s = d.get("salary") or {}
    if not s:
        return ""
    out = []
    if s.get("from") is not None:
        out.append("от " + str(s["from"]))
    if s.get("to") is not None:
        out.append("до " + str(s["to"]))
    out.append(str(s.get("currency") or ""))
    if s.get("gross") is not None:
        out.append("до налогов" if s["gross"] else "на руки")
    return " ".join(x for x in out if x)


def is_remote(d):
    schedule = d.get("schedule") or {}
    formats = d.get("work_format") or []
    ids = [str(schedule.get("id") or "").lower()]
    if isinstance(formats, list):
        ids += [str(x.get("id") or "").lower() for x in formats if isinstance(x, dict)]
    # If HH has not supplied enough metadata, keep it as UNKNOWN instead of guessing.
    return "remote" in ids or "REMOTE" in str(formats) or not any(ids)


def make_row(item, vid, qname, d):
    desc = clean(item.findtext("description"))
    full = clean(d.get("description")) or desc
    employer = name(d.get("employer"))
    if isinstance(d.get("employer"), dict):
        employer = d["employer"].get("name") or ""
    if not employer:
        m = re.search(r"Вакансия компании:\s*(.+?)(?:\s+Создана:|$)", desc)
        employer = m.group(1) if m else ""
    row = [""] * 33
    row[0], row[1], row[2] = now(), employer, d.get("name") or clean(item.findtext("title"))
    row[3], row[4] = f"https://hh.ru/vacancy/{vid}", salary(d)
    row[5], row[6], row[7], row[8] = name(d.get("work_format")), name(d.get("schedule")), name(d.get("employment")), name(d.get("experience"))
    row[13], row[14], row[17], row[18] = "Не проверена", "Запрос: " + qname, vid, "RAW"
    row[27], row[28], row[29], row[30] = "HH", d.get("published_at") or clean(item.findtext("pubDate")), now(), full
    row[31], row[32] = "Неизвестно", "Неизвестно"
    return row


def main():
    if not SPREADSHEET_ID or not CREDS:
        raise RuntimeError("GOOGLE_SPREADSHEET_ID and GOOGLE_CREDENTIALS_PATH required")
    ws = gspread.service_account(filename=CREDS).open_by_key(SPREADSHEET_ID).worksheet("RAW")
    if ws.row_values(1)[:21] != BASE_HEADERS:
        raise RuntimeError("Unexpected PolyWork RAW headers; refusing to write")
    if ws.col_count < 33:
        ws.add_cols(33 - ws.col_count)
    extras = ws.get("AB1:AG1")
    current = extras[0] if extras else []
    if any(v and v != EXTRA_HEADERS[i] for i, v in enumerate(current)):
        raise RuntimeError("Existing columns AB:AG differ; refusing to overwrite")
    if current != EXTRA_HEADERS:
        ws.update(range_name="AB1:AG1", values=[EXTRA_HEADERS])

    sheet_ids = {str(x).strip() for x in ws.col_values(18)[1:] if str(x).strip().isdigit()}
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 PolyWorkOps/1.0"})
    pending, errors, in_run = [], [], set()
    scanned = skipped = 0
    with sqlite3.connect(DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS seen (vacancy_id TEXT PRIMARY KEY, first_seen TEXT, source_query TEXT, url TEXT)")
        db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
                       [(i, now(), "seed:RAW", f"https://hh.ru/vacancy/{i}") for i in sheet_ids])
        db.commit()
        for q in queries:
            try:
                params = dict(q.get("params") or {})
                params["text"] = q["text"]
                r = session.get(RSS_URL + "?" + urlencode(params), timeout=25)
                r.raise_for_status()
                root = ET.fromstring(r.content)
                for item in root.findall(".//item"):
                    link = clean(item.findtext("link") or item.findtext("guid"))
                    match = ID_RE.search(link)
                    if not match:
                        continue
                    vid = match.group(1)
                    scanned += 1
                    if vid in sheet_ids or vid in in_run or db.execute("SELECT 1 FROM seen WHERE vacancy_id=?", (vid,)).fetchone():
                        skipped += 1
                        continue
                    in_run.add(vid)
                    d = {}
                    try:
                        detail = session.get(f"https://api.hh.ru/vacancies/{vid}", timeout=15)
                        if detail.ok:
                            d = detail.json()
                    except (requests.RequestException, ValueError):
                        pass
                    if d and not is_remote(d):
                        skipped += 1
                        continue
                    pending.append((vid, q["name"], make_row(item, vid, q["name"], d)))
            except Exception as exc:
                errors.append(f'{q["name"]}: {type(exc).__name__}: {exc}')
            time.sleep(0.6)

        written = 0
        for i in range(0, len(pending), 50):
            batch = pending[i:i+50]
            try:
                ws.append_rows([r for _, _, r in batch], value_input_option="RAW")
                db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
                               [(vid, now(), qname, row[3]) for vid, qname, row in batch])
                db.commit()
                written += len(batch)
            except Exception as exc:
                errors.append(f"append: {type(exc).__name__}: {exc}")
                break

    status = {"last_run": now(), "status": "ok" if not errors else "partial",
              "queries": len(queries), "scanned": scanned, "skipped": skipped,
              "candidates": len(pending), "written": written, "errors": errors}
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
