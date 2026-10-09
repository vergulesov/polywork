#!/usr/bin/env python3
"""HH RSS -> dedicated 'Пищевой RAW' tab. No HH API. Run: python food_rss.py --write"""
import argparse
import datetime as dt
import html
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
import requests

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1W9JGIjfaJFcZoBcqwX1oSiM6lRhel44BCvhgvCY2UZw")
TAB = "Пищевой RAW"
RSS = "https://hh.ru/search/vacancy/rss"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalJobResearch/1.0)"}
QUERIES = {
    "Общепит": [
        "профессиональное кухонное оборудование", "оборудование для общепита",
        "технологическое оборудование общепит", "пароконвектоматы",
        "оснащение ресторанов", "оснащение пищеблоков", "тепловое оборудование horeca",
        "Abat", "АТЕСИ", "Техно-ТТ", "Торговый Дизайн", "КЛЕН",
    ],
    "Пищевые производства": [
        "пищевое оборудование", "оборудование для пищевых производств",
        "хлебопекарное оборудование", "кондитерское оборудование",
        "мясоперерабатывающее оборудование", "молочное оборудование",
        "линии розлива", "фасовочное оборудование", "упаковочное оборудование пищевое",
        "производственные линии пищевые",
    ],
    "Проектные продажи": [
        "проектные продажи пищевое оборудование", "комплектация пищевого производства",
        "подбор технологического оборудования", "оборудование для пекарен",
    ],
}
POS = re.compile(r"продаж|клиент|аккаунт|account|проект|комплектац|оборудован|инженер по продаж|развитие дилер|региональн", re.I)
FOOD = re.compile(r"пищев|общепит|horeca|ресторан|столов|пекар|хлебопек|кондитер|мясоперераб|молочн|тестомес|пароконвект|кухонн|абат|abat|атеси|техно.тт|холодильн|фасовоч|упаковоч", re.I)
BAD = re.compile(r"торговый представитель|мерчендайзер|продавец-консультант|курьер|кладовщик|повар|оператор линии|технолог пищевого производства", re.I)
RISK = re.compile(r"холодн.{0,25}(звон|поиск|продаж)|активн.{0,25}поиск|разъездн|частые командиров|ежедневн.{0,15}выезд", re.I)
COLUMNS = ["HH ID","Компания","Вакансия","Прямая ссылка","Зарплата","Формат (RSS)","Регион","Опубликована","Обнаружена UTC","Сегмент","Поисковый запрос","Релевантность","Риски","Статус","RSS описание"]

def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", " ", s or ""))).strip()

def field(description, label):
    m = re.search(re.escape(label) + r":\s*(.*?)(?=\s+(?:Вакансия компании|Регион|Предполагаемый уровень месячного дохода|Создана):|$)", description, re.I)
    return m.group(1).strip() if m else ""

def fetch(session, days, pages, pause):
    found, errors, warnings = {}, [], []
    for segment, queries in QUERIES.items():
        for term in queries:
            for mode, area in (("REMOTE_SEARCH", 113), ("CHELYABINSK_SEARCH", 104)):
                params = {"text": term, "area": area,
                          "search_period": days, "order_by": "publication_time"}
                if mode == "REMOTE_SEARCH":
                    params["work_format"] = "REMOTE"
                previous = set()
                for page in range(pages):
                    try:
                        response = session.get(RSS, params={**params, "page": page}, timeout=25)
                        response.raise_for_status()
                        root = ET.fromstring(response.content)
                        items = root.findall(".//item")
                        signature = tuple((it.findtext("link") or "") for it in items)
                        if signature in previous and signature:
                            warnings.append(f"{term} / {mode}: repeated RSS page {page}; pagination stopped")
                            break
                        previous.add(signature)
                        for it in items:
                            link = clean(it.findtext("link") or it.findtext("guid"))
                            match = re.search(r"/vacancy/(\d+)", link)
                            if not match:
                                continue
                            vid = match.group(1)
                            description = clean(it.findtext("description"))
                            title = clean(it.findtext("title"))
                            if not POS.search(title) or BAD.search(title):
                                continue
                            evidence = title + " " + description + " " + term
                            score = (3 if FOOD.search(title + " " + description) else 0) + (2 if FOOD.search(term) else 0) + (2 if re.search(r"проект|комплектац|подбор|технич", evidence, re.I) else 0)
                            if vid not in found or score > found[vid]["score"] or (score == found[vid]["score"] and mode == "REMOTE_SEARCH"):
                                found[vid] = {"id": vid, "title": title, "desc": description, "mode": mode,
                                              "segment": segment, "query": term, "score": score,
                                              "published": clean(it.findtext("pubDate"))}
                        if len(items) < 20:
                            break
                        if page == pages - 1:
                            warnings.append(f"{term}: page limit reached")
                        time.sleep(pause)
                    except Exception as exc:
                        errors.append(f"{term} / {mode} page={page}: {exc}")
                        break
    return found, errors, warnings

def row(v):
    d = v["desc"]
    risk = "Проверить холодный поиск / разъезды" if RISK.search(d + " " + v["title"]) else "Не установлено по RSS"
    confidence = "Профильное оборудование" if FOOD.search(v["title"] + " " + d) else "Проверить отрасль компании"
    return [v["id"], field(d,"Вакансия компании"), v["title"],
            f"https://hh.ru/vacancy/{v['id']}", field(d,"Предполагаемый уровень месячного дохода"),
            v["mode"] + " — проверить карточку", field(d,"Регион"), v["published"],
            dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            v["segment"], v["query"], v["score"], risk,
            "NEW" if confidence == "Профильное оборудование" else "CHECK_INDUSTRY", d]

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--pages", type=int, default=4)
    p.add_argument("--pause", type=float, default=0.35)
    p.add_argument("--write", action="store_true", help="Append new IDs to existing Google Sheet")
    args = p.parse_args()
    if not (1 <= args.days <= 30 and 1 <= args.pages <= 20):
        p.error("--days 1..30, --pages 1..20")
    session = requests.Session()
    session.headers.update(HEADERS)
    found, errors, warnings = fetch(session, args.days, args.pages, args.pause)
    rows = [row(v) for v in sorted(found.values(), key=lambda v: (-v["score"], v["id"]))]
    print(f"unique_candidates={len(rows)} errors={len(errors)} warnings={len(warnings)}")
    for r in rows[:30]:
        print(f"{r[11]} | {r[2]} | {r[1]} | {r[3]}")
    print("by_mode=", {mode: sum(r[5].startswith(mode) for r in rows) for mode in ("REMOTE_SEARCH", "CHELYABINSK_SEARCH")})
    for e in errors[:15]:
        print("ERROR:", e)
    for w in warnings[:15]:
        print("WARNING:", w)
    if not args.write:
        print("DRY RUN, no Google Sheets writes. Use --write to append.")
        return
    import gspread
    cred = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "google-service-account.json")
    ws = gspread.service_account(filename=cred).open_by_key(SPREADSHEET_ID).worksheet(TAB)
    existing = ws.col_values(1)
    if existing and existing[0] != COLUMNS[0]:
        raise RuntimeError("Unexpected sheet headers; refusing to write")
    ids = set(existing[1:])
    fresh = [r for r in rows if r[0] not in ids]
    if fresh:
        for i in range(0, len(fresh), 150):
            ws.append_rows(fresh[i:i+150], value_input_option="RAW")
    print(f"written={len(fresh)} duplicates_skipped={len(rows)-len(fresh)}")

if __name__ == "__main__":
    main()
