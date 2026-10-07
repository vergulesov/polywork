import argparse

from dotenv import load_dotenv

from collector_fl import collect
from sheets import _worksheet, append_project, existing_urls
from storage import init_db, is_seen, mark_seen

load_dotenv()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="Write currently visible FL.ru projects to Google Sheet even if they were seeded before.",
    )
    args = ap.parse_args()

    init_db()
    projects = collect()
    print("collected", len(projects))

    ws = _worksheet()
    sheet_urls = existing_urls(ws)
    written = 0
    skipped = 0

    for p in projects:
        if args.backfill:
            if p["url"] in sheet_urls:
                skipped += 1
                continue
        else:
            if is_seen(p["source"], p["external_id"]):
                skipped += 1
                continue

        try:
            append_project(p, ws)
            sheet_urls.add(p["url"])
            mark_seen(p, "SHEET")
            written += 1
            print("written", p["external_id"], p["title"][:80])
        except Exception as e:
            print("project error", p["external_id"], type(e).__name__, e)

    print("done:", "written", written, "skipped", skipped)


if __name__ == "__main__":
    main()
