import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("XUDU_DATA", ROOT / "data" / "reader-v1"))
SAMPLE = ROOT / "samples" / "journey-to-the-west.pdf"
SHOWCASE = ROOT / "samples" / "pride-and-prejudice.pdf"


@contextmanager
def db():
    connection = sqlite3.connect(DATA / "reader.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def setup():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS documents(
            digest TEXT PRIMARY KEY, pages INTEGER, chapters TEXT, last INTEGER);
        CREATE TABLE IF NOT EXISTS sentences(
            doc TEXT, pos INTEGER, page INTEGER, text TEXT, boxes TEXT,
            PRIMARY KEY(doc,pos));
        CREATE INDEX IF NOT EXISTS page_text ON sentences(doc,page,pos);
        CREATE TABLE IF NOT EXISTS shelves(
            id TEXT PRIMARY KEY, owner TEXT, doc TEXT, title TEXT, progress INTEGER DEFAULT 0,
            previous INTEGER DEFAULT 0, rev INTEGER DEFAULT 0, UNIQUE(owner,doc));
        CREATE TABLE IF NOT EXISTS cast_answers(
            doc TEXT, pos INTEGER, revision INTEGER, answer TEXT NOT NULL,
            PRIMARY KEY(doc,pos,revision));
        CREATE TABLE IF NOT EXISTS cast_blocks(
            doc TEXT, lo INTEGER, hi INTEGER, version INTEGER, entries TEXT NOT NULL,
            PRIMARY KEY(doc,lo,hi,version));
        CREATE TABLE IF NOT EXISTS visitors(id TEXT PRIMARY KEY, active TEXT);
        CREATE TABLE IF NOT EXISTS accounts(
            id TEXT PRIMARY KEY, username TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL,
            salt BLOB NOT NULL, password BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS account_sessions(
            token TEXT PRIMARY KEY, account TEXT NOT NULL, active TEXT,
            expires INTEGER NOT NULL, epoch INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS sessions_by_account ON account_sessions(account,active);
        CREATE TABLE IF NOT EXISTS model_limits(
            bucket TEXT, day INTEGER, calls INTEGER NOT NULL, PRIMARY KEY(bucket,day));
        CREATE TABLE IF NOT EXISTS login_limits(
            key TEXT PRIMARY KEY, until INTEGER NOT NULL, attempts INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, book TEXT, touched INTEGER);
        CREATE TABLE IF NOT EXISTS turns(
            id TEXT PRIMARY KEY, thread TEXT, question TEXT, pos INTEGER, rev INTEGER,
            selected INTEGER, state TEXT, answer TEXT, spoiler TEXT, created INTEGER);
        CREATE INDEX IF NOT EXISTS turns_thread_created ON turns(thread,created);
        CREATE TABLE IF NOT EXISTS tracked_questions(
            turn TEXT PRIMARY KEY, book TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS tracked_book ON tracked_questions(book);
        CREATE TABLE IF NOT EXISTS card_links(
            turn TEXT PRIMARY KEY, root TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0,
            context TEXT);
        CREATE INDEX IF NOT EXISTS card_root ON card_links(root);
        CREATE TABLE IF NOT EXISTS card_checks(
            turn TEXT NOT NULL, pos INTEGER NOT NULL, parent INTEGER, additions TEXT NOT NULL,
            PRIMARY KEY(turn,pos));
        CREATE TABLE IF NOT EXISTS card_failures(
            turn TEXT NOT NULL, target INTEGER NOT NULL, PRIMARY KEY(turn,target));
        UPDATE turns SET state='pending' WHERE state='checking';
        """)


def texts(c, doc, low, high):
    return [
        dict(r)
        for r in c.execute(
            "SELECT pos,page,text FROM sentences WHERE doc=? AND pos BETWEEN ? AND ? ORDER BY pos",
            (doc, low, high),
        )
    ]


def decode(row, field):
    return json.loads(row[field]) if row[field] else None
