import os
from datetime import datetime

import gspread


def _worksheet():
    credentials = os.getenv("GOOGLE_CREDENTIALS_PATH", "").strip()
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()
    worksheet_name = os.getenv("GOOGLE_WORKSHEET", "FREELANCE RAW").strip()

    if not credentials:
        raise RuntimeError("Set GOOGLE_CREDENTIALS_PATH")
    if not spreadsheet_id:
        raise RuntimeError("Set GOOGLE_SPREADSHEET_ID")

    gc = gspread.service_account(filename=credentials)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_name)

    if ws.col_count < 17:
        ws.resize(cols=17)
    if ws.acell("P1").value != "Описание" or ws.acell("Q1").value != "External ID":
        ws.update("P1:Q1", [["Описание", "External ID"]])

    return ws


def existing_urls(ws=None):
    ws = ws or _worksheet()
    return {v.strip() for v in ws.col_values(5)[1:] if v and v.strip()}


def append_project(project, ws=None):
    ws = ws or _worksheet()
    row = [
        datetime.now().strftime("%d.%m.%Y"),
        project.get("source", ""),
        project.get("title", ""),
        "",
        project.get("url", ""),
        project.get("budget", ""),
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        project.get("description", ""),
        project.get("external_id", ""),
    ]
    ws.append_row(row, value_input_option="RAW")
