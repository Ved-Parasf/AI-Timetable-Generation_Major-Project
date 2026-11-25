"""Local-only, workbook-scoped Timora conversation history.

No API keys, workbook data or passwords are stored. Each conversation is tied
to a hashed timetable snapshot so old chat messages never silently become
context for a changed or different timetable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import uuid

DEFAULT_DB = Path(__file__).resolve().parent / 'data' / 'timora_chat_history.sqlite3'
KEY_PATTERN = re.compile(r'\bgsk_[A-Za-z0-9_-]{12,}\b')


def _db(path=None):
    path = Path(path) if path else DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=8)
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY, workbook TEXT NOT NULL, source TEXT NOT NULL,
        title TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL,
        FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE)''')
    return connection


@contextmanager
def _opened(path=None):
    db = _db(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def list_conversations(workbook: str, *, path=None):
    with _opened(path) as db:
        rows=db.execute('''SELECT id,source,title,created,updated FROM conversations
                           WHERE workbook=? ORDER BY updated DESC LIMIT 100''', (workbook,)).fetchall()
    return [{'id':r[0],'source':r[1],'title':r[2],'created':r[3],'updated':r[4]} for r in rows]


def create_conversation(workbook: str, source: str, *, path=None):
    ident=uuid.uuid4().hex
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    with _opened(path) as db:
        db.execute('''INSERT INTO conversations(id,workbook,source,title,created,updated)
                      VALUES (?,?,?,?,?,?)''',(ident,workbook,source,'New conversation',now,now))
    return ident


def load_messages(workbook: str, ident: str, *, path=None):
    with _opened(path) as db:
        found=db.execute('SELECT 1 FROM conversations WHERE id=? AND workbook=?',
                         (ident,workbook)).fetchone()
        if not found:
            return []
        rows=db.execute('SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id',
                        (ident,)).fetchall()
    return [{'role':role,'content':content} for role,content in rows]


def append_messages(workbook: str, ident: str, source: str, messages, *, path=None):
    """Append only to the matching workbook AND timetable snapshot."""
    valid=[]
    for message in messages:
        if not isinstance(message,dict) or message.get('role') not in ('user','assistant'):
            continue
        content=KEY_PATTERN.sub('[redacted Groq API key]',str(message.get('content') or ''))[:12000]
        if content.strip():
            valid.append((ident,message['role'],content))
    if not valid:
        return
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    with _opened(path) as db:
        row=db.execute('SELECT workbook,source,title FROM conversations WHERE id=?',(ident,)).fetchone()
        if row is None or row[0]!=workbook or row[1]!=source:
            raise ValueError('Conversation belongs to a different timetable. Start a new chat.')
        db.executemany('INSERT INTO messages(conversation_id,role,content) VALUES (?,?,?)',valid)
        title=row[2]
        if title=='New conversation':
            title=next((content[:72] for _ident,role,content in valid if role=='user'),title)
        db.execute('UPDATE conversations SET title=?, updated=? WHERE id=?',(title,now,ident))


def delete_conversation(workbook: str, ident: str, *, path=None):
    with _opened(path) as db:
        result=db.execute('DELETE FROM conversations WHERE id=? AND workbook=?',(ident,workbook))
        return result.rowcount>0
