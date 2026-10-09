#!/usr/bin/env python3
"""Sales vacancies pipeline using existing HH RSS only. Independent of PolyWork.

Default: read-only dry run. --write explicitly updates dedicated SALES_* tabs.
Required: requests, gspread, service-account JSON and SPREADSHEET_ID.
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
DB = Path(os.getenv("SALES_DB_PATH", HERE / "data" / "sales_v2.sqlite3"))
CONFIG = Path(os.getenv("SALES_QUERIES_PATH", HERE / "sales_queries.json"))
RSS = "https://hh.ru/search/vacancy/rss"
HEADERS = {"User-Agent": "SalesVacanciesResearch/1.0 (personal job search)"}
STOP_TITLE = re.compile(r"охранник|курьер|комплектовщик|кладовщик|грузчик|водитель|кассир|продавец-консультант|оператор колл|риелтор|недвижимост|маркетплейс|wildberries|ozon|edtech", re.I)
TARGET_TITLE = re.compile(r"продаж|клиент|аккаунт|account|проект|оборудован|комплектац|инженер|снабжен", re.I)
HARD_STOP = {
    "Только холодный поиск": r"исключительно холодн|только холодн|100% холодн|поиск клиент(?:ов|а) с нуля|постоянный холодный обзвон",
    "Частые командировки": r"командировк.{0,30}(?:60%|70%|80%|еженедельно|постоянн)|разъездной характер работы|ежедневные выезд",
    "Территориальные продажи": r"развитие территории.{0,70}(?:холодн|активн)|полевые продаж",
}
POS = {
    "Работа": [(r"сопровождени.{0,50}(?:сдел|клиент)|проектн.{0,30}продаж", 8), (r"подготовк.{0,30}коммерческ.{0,15}предлож|спецификац|расчет.{0,20}кп", 8)],
    "Клиенты": [(r"действующ.{0,25}клиент|готов.{0,15}баз", 13), (r"входящ.{0,20}(?:заяв|обращ|лид)|без холодн", 12)],
    "Продукт": [(r"промышленн.{0,30}оборудован|инженерн.{0,30}(?:решен|оборудован)|техническ.{0,30}подбор", 8), (r"чертеж|техническ.{0,10}задан|смет|комплектаци", 7)],
}


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def clean_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", s or "")).strip()


def salary_fit(s):
    """Conservative: require explicit >=80k lower bound in RUB; no assumptions about bonus."""
    if not s or s.get("currency") != "RUR":
        return "UNKNOWN"
    low, high = s.get("from"), s.get("to")
    if low is not None:
        return "PASS" if low >= 80000 else "FAIL"
    if high is not None and high < 80000:
        return "FAIL"
    return "UNKNOWN"


def location_fit(v, requested_mode):
    """Only affirm remote when HH explicitly flags remote; city-only for local."""
    work = v.get("work_format") or []
    ids = {x.get("id") for x in work if isinstance(x, dict)}
    schedule = (v.get("schedule") or {}).get("id")
    remote = "REMOTE" in ids or schedule == "remote"
    area = v.get("area") or {}
    if requested_mode == "remote":
        return "REMOTE" if remote else "UNKNOWN"
    return "CHELYABINSK" if str(area.get("id")) == "104" else "FAIL"


def score(v, text, fmt, sal):
    """Conservative heuristic, not an AI judgment. Lack of evidence earns no positive points."""
    title = (v.get("name") or "")
    hay = title + " " + text
    stop = [name for name, pattern in HARD_STOP.items() if re.search(pattern, hay, re.I)]
    if STOP_TITLE.search(title) or not TARGET_TITLE.search(title):
        stop.append("Непрофильная должность")
    parts = {
        "Работа": 9 if TARGET_TITLE.search(title) else 0,
        "Клиенты": 0,
        "Формат": 20 if fmt in ("REMOTE", "CHELYABINSK") else 0,
        "Деньги": 15 if sal == "PASS" else 0,
        "Продукт": 0,
    }
    for category, patterns in POS.items():
        cap = {"Работа": 25, "Клиенты": 25, "Продукт": 15}[category]
        parts[category] = min(cap, parts[category] + sum(weight for pat, weight in patterns if re.search(pat, hay, re.I)))
    # Unknown client source must not be rewarded as a warm base.
    total = sum(parts.values())
    return total, parts, stop


def fetch_vacancies(session, queries, days, pages):
    """HH RSS only. No api.hh.ru calls. RSS can be truncated; report request errors."""
    out, errors, coverage = {}, [], []
    for q in queries:
        for mode in ("remote", "chelyabinsk"):
            params = {"text": q, "search_period": days,
                      "order_by": "publication_time",
                      "area": 113 if mode == "remote" else 104}
            if mode == "remote":
                params["work_format"] = "REMOTE"
            pages_seen = 0
            last_page_count = 0
            page_signatures = set()
            repeated_page = False
            for page in range(pages):
                try:
                    r = session.get(RSS, params={**params, "page": page}, timeout=20)
                    r.raise_for_status()
                    root = ET.fromstring(r.content)
                    items = root.findall(".//item")
                    signature = tuple((i.findtext("link") or i.findtext("guid") or "") for i in items)
                    if signature in page_signatures and signature:
                        repeated_page = True
                        coverage.append(f"{q} / {mode}: RSS page {page} duplicates previous page; pagination ineffective")
                        break
                    page_signatures.add(signature)
                    pages_seen += 1
                    last_page_count = len(items)
                    for item in items:
                        link = clean_html(item.findtext("link") or item.findtext("guid"))
                        match = re.search(r"/vacancy/(\d+)", link)
                        if not match:
                            continue
                        vid = match.group(1)
                        desc = clean_html(item.findtext("description"))
                        entry = {
                            "id": vid, "name": clean_html(item.findtext("title")),
                            "description": desc, "published_at": clean_html(item.findtext("pubDate")),
                            "alternate_url": f"https://hh.ru/vacancy/{vid}",
                            "mode": mode, "query": q
                        }
                        if vid not in out or mode == "remote":
                            out[vid] = entry
                    if not items or len(items) < 20:
                        break
                except Exception as exc:
                    errors.append(f"{q} / {mode} / page {page}: {exc}")
                    break
            if not repeated_page and pages_seen == pages and last_page_count >= 20:
                coverage.append(f"{q} / {mode}: page cap {pages} reached; more results may exist")
    return out, errors, coverage


def rss_salary(desc):
    m = re.search(r"Предполагаемый уровень месячного дохода:\s*(.*?)(?=\s+(?:Регион:|Создана:|Вакансия компании:)|$)", desc, re.I)
    if not m:
        return None
    txt = m.group(1)
    amounts = [int(x.replace(" ", "").replace("\xa0", ""))
               for x in re.findall(r"\d[\d \xa0]*", txt)
               if int(x.replace(" ", "").replace("\xa0", "")) >= 1000]
    if not amounts or ("руб" not in txt.lower() and "₽" not in txt):
        return None
    if re.search(r"\bдо\b", txt, re.I) and not re.search(r"\bот\b", txt, re.I):
        return {"from": None, "to": max(amounts), "currency": "RUR"}
    return {"from": min(amounts), "to": max(amounts) if len(amounts) > 1 else None, "currency": "RUR"}


def rss_company(desc):
    m = re.search(r"Вакансия компании:\s*(.*?)(?=\s+(?:Создана:|Регион:|Предполагаемый уровень)|$)", desc)
    return m.group(1).strip() if m else ""


def rss_region(desc):
    m = re.search(r"Регион:\s*(.*?)(?=\s+(?:Предполагаемый уровень|Вакансия компании:|Создана:)|$)", desc)
    return m.group(1).strip() if m else ""


def database():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
        hh_id TEXT PRIMARY KEY, decision TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS entries (
        hh_id TEXT PRIMARY KEY, content TEXT NOT NULL, first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, analysis_version TEXT NOT NULL)""")
    conn.commit()
    return conn


def worksheets():
    import gspread
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(HERE / "google-service-account.json"))
    book = gspread.service_account(filename=credentials).open_by_key(spreadsheet_id)
    titles = ("SALES_RAW_V2", "SALES_CANDIDATES", "SALES_APPLY")
    headers = {
        titles[0]: ["HH ID", "Найдена UTC", "Опубликована", "Вакансия", "Компания",
                    "Зарплата", "Формат", "Оценка", "Причина", "Стоп", "Ссылка", "Запрос", "Статус", "RSS описание", "Статус зарплаты"],
        titles[1]: ["HH ID", "Найдена UTC", "Вакансия", "Компания", "Зарплата", "Формат",
                    "Оценка", "Аргументы", "Ссылка", "Решение ✅/❌"],
        titles[2]: ["HH ID", "Вакансия", "Компания", "Зарплата", "Оценка",
                    "Ссылка", "Статус отклика"],
    }
    result = {}
    for title in titles:
        try:
            ws = book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=title, rows=1000, cols=16)
        existing_header = ws.row_values(1)
        if not existing_header:
            ws.update(range_name="A1", values=[headers[title]])
        elif title == "SALES_RAW_V2" and len(existing_header) < len(headers[title]):
            ws.update(range_name="N1:O1", values=[headers[title][13:15]])
        result[title] = ws
    return result


def money(s):
    if not s:
        return "Не указана"
    a = s.get("from")
    b = s.get("to")
    return f"{a or '—'}–{b or '—'} {s.get('currency', '')}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true", help="write dedicated SALES_* tabs and state DB")
    p.add_argument("--raw-only", action="store_true", help="populate only SALES_RAW_V2; postpone ranking decisions")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--pages", type=int, default=8, help="max RSS pages per query and mode, 1..20")
    p.add_argument("--limit", type=int, default=0, help="limit full-card retrieval (0 = unlimited)")
    args = p.parse_args()
    if args.limit < 0:
        p.error("limit must be >= 0")
    if not 1 <= args.days <= 30 or not 1 <= args.pages <= 20:
        p.error("days must be 1..30 and pages 1..20")
    queries = json.loads(CONFIG.read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers.update(HEADERS)
    found, errors, coverage = fetch_vacancies(session, queries, args.days, args.pages)
    output = []
    drop = {"title": 0, "low_salary": 0, "region": 0, "hard_stop": 0}
    unknown_pay = 0
    for vid, item in found.items():
        title = item["name"]
        if STOP_TITLE.search(title) or not TARGET_TITLE.search(title):
            drop["title"] += 1
            continue
        if args.limit and len(output) >= args.limit:
            break
        desc = item["description"]
        sal_data = rss_salary(desc)
        sal = salary_fit(sal_data)
        if sal == "FAIL":
            drop["low_salary"] += 1
            continue
        if sal == "UNKNOWN":
            unknown_pay += 1
        region = rss_region(desc)
        # Remote is inferred from HH's work_format=REMOTE search result,
        # NOT independently confirmed from a detailed vacancy card.
        if item["mode"] == "remote":
            fmt = "REMOTE_SEARCH"
        else:
            if "челябинск" not in region.lower():
                drop["region"] += 1
                continue
            fmt = "CHELYABINSK"
        # Re-use scoring with confirmed query location represented as temporary category.
        points, parts, stops = score({"name": title}, desc,
                                     "REMOTE" if fmt == "REMOTE_SEARCH" else fmt, sal)
        if stops:
            drop["hard_stop"] += 1
            continue
        output.append({
            "hh_id": vid, "found_at": stamp(), "published": item["published_at"],
            "title": title, "company": rss_company(desc),
            "salary": money(sal_data), "format": fmt, "score": points,
            "breakdown": parts, "stops": stops, "url": item["alternate_url"],
            "query": item["query"], "description": desc, "salary_status": sal,
        })
    output.sort(key=lambda x: (-x["score"], x["published"]))
    print(json.dumps({"found_unique": len(found), "qualified_raw": len(output),
                      "candidates_70": sum(x["score"] >= 70 for x in output),
                      "unknown_salary": unknown_pay, "discarded": drop,
                      "pagination_warnings": coverage[:40], "errors": errors[:30], "sample": output[:5] if args.write else output[:20]}, ensure_ascii=False, indent=2))
    if not args.write:
        print("DRY RUN: no database or spreadsheet changes.")
        return
    if errors:
        print("Partial HH errors; successful results can still be written.")
    con = database()
    tabs = worksheets()
    raw, candidates, apply = (tabs[k] for k in ("SALES_RAW_V2", "SALES_CANDIDATES", "SALES_APPLY"))
    # decisions are read from a stable HH-ID column; do not attach decisions to sorted row numbers.
    existing = {str(r[0]): r for r in raw.get_all_values()[1:] if r and r[0]}
    reviews = {str(r[0]): r for r in candidates.get_all_values()[1:] if r and r[0]}
    applied = {str(r[0]): r for r in apply.get_all_values()[1:] if r and r[0]}
    for vid, row in reviews.items():
        if len(row) >= 10 and row[9].strip() in ("✅", "❌"):
            decision = "APPROVED" if row[9].strip() == "✅" else "REJECTED"
            con.execute("INSERT INTO decisions VALUES (?, ?, ?) ON CONFLICT(hh_id) DO UPDATE SET decision=excluded.decision,updated_at=excluded.updated_at",
                        (vid, decision, stamp()))
    con.commit()
    decisions = dict(con.execute("SELECT hh_id,decision FROM decisions"))
    raw_new, candidate_new, apply_new = [], [], []
    for v in output:
        vid = v["hh_id"]
        if vid not in existing:
            raw_new.append([vid, v["found_at"], v["published"], v["title"], v["company"],
                            v["salary"], v["format"], v["score"], json.dumps(v["breakdown"], ensure_ascii=False),
                            "", v["url"], v["query"], "NEEDS_REVIEW" if v["salary_status"] == "UNKNOWN" else "QUALIFIED", v["description"], v["salary_status"]])
        if not args.raw_only and v["salary_status"] == "PASS" and v["score"] >= 70 and vid not in reviews and vid not in decisions:
            candidate_new.append([vid, v["found_at"], v["title"], v["company"], v["salary"],
                                  v["format"], v["score"], json.dumps(v["breakdown"], ensure_ascii=False),
                                  v["url"], ""])
        if not args.raw_only and decisions.get(vid) == "APPROVED" and vid not in applied:
            apply_new.append([vid, v["title"], v["company"], v["salary"], v["score"], v["url"], "Не откликался"])
        con.execute("""INSERT INTO entries VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(hh_id) DO UPDATE SET content=excluded.content,
                       last_seen=excluded.last_seen,analysis_version=excluded.analysis_version""",
                    (vid, json.dumps(v, ensure_ascii=False), v["found_at"], stamp(), "heuristic-v1"))
    # Previously approved vacancies must remain in APPLY even if absent from today's search.
    for vid, decision in decisions.items():
        if not args.raw_only and decision == "APPROVED" and vid not in applied and all(x[0] != vid for x in apply_new):
            row = reviews.get(vid)
            if row and len(row) >= 9:
                apply_new.append([vid, row[2], row[3], row[4], row[6], row[8], "Не откликался"])
    for ws, rows in ((raw, raw_new), (candidates, candidate_new), (apply, apply_new)):
        if rows:
            ws.append_rows(rows, value_input_option="RAW")
    con.commit()
    print(f"Written: RAW={len(raw_new)}, candidates={len(candidate_new)}, apply={len(apply_new)}")


if __name__ == "__main__":
    main()
