import os, sqlite3
from pathlib import Path

DB = Path(os.getenv('DATABASE_PATH', 'data/polywork.db'))
DB.parent.mkdir(parents=True, exist_ok=True)

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute('CREATE TABLE IF NOT EXISTS seen (source TEXT, external_id TEXT, url TEXT, title TEXT, status TEXT, PRIMARY KEY(source, external_id))')

def is_seen(source, external_id):
    with sqlite3.connect(DB) as c:
        return c.execute('SELECT 1 FROM seen WHERE source=? AND external_id=?', (source, external_id)).fetchone() is not None

def mark_seen(project, status='SEEN'):
    with sqlite3.connect(DB) as c:
        c.execute('INSERT OR REPLACE INTO seen(source, external_id, url, title, status) VALUES (?,?,?,?,?)', (project['source'], project['external_id'], project['url'], project['title'], status))