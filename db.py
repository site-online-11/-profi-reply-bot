"""SQLite storage. All state changes are serialized and committed atomically."""
import contextlib
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def source_of(text):
    parts = (text or '').split(maxsplit=1)
    if not parts or parts[0].split('@')[0] != '/start' or len(parts) < 2:
        return None
    value = parts[1].strip()
    if value.startswith('admin_'):
        return None
    return value if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) else None


def day_key():
    return datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()


class DB:
    def __init__(self, path):
        self.path = path
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.c = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.c.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
          first_source TEXT NOT NULL, last_source TEXT NOT NULL,
          created REAL NOT NULL, seen REAL NOT NULL, blocked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, source TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS updates(
          id INTEGER PRIMARY KEY, user_id INTEGER, payload TEXT,
          status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(
          id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          kind TEXT, status TEXT NOT NULL, classification TEXT, reply TEXT, warning TEXT,
          error_code TEXT, created REAL NOT NULL, delivered REAL);
        CREATE TABLE IF NOT EXISTS api_calls(
          id INTEGER PRIMARY KEY, request_id INTEGER REFERENCES requests(id) ON DELETE CASCADE,
          user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, day TEXT NOT NULL,
          input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'started', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
        CREATE INDEX IF NOT EXISTS requests_user ON requests(user_id,created);
        CREATE INDEX IF NOT EXISTS calls_day ON api_calls(day,user_id);
        CREATE INDEX IF NOT EXISTS updates_state ON updates(status,id);
        CREATE INDEX IF NOT EXISTS events_user ON events(user_id,created);
        ''')
        self.c.commit()

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            try:
                self.c.execute('BEGIN IMMEDIATE')
                yield self.c
                self.c.commit()
            except BaseException:
                self.c.rollback()
                raise

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, params).fetchall()]

    def get(self, key, default=None):
        rows = self.rows('SELECT value FROM settings WHERE key=?', (key,))
        return rows[0]['value'] if rows else default

    def set(self, key, value):
        with self.tx() as c:
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def ingest(self, updates):
        """Save the batch BEFORE acknowledging it via the next Telegram offset."""
        now = time.time()
        with self.tx() as c:
            offset = int(self.get('offset', '0'))
            for u in updates:
                uid = int(u['update_id'])
                if uid < offset or c.execute('SELECT 1 FROM updates WHERE id=?', (uid,)).fetchone():
                    continue
                offset = max(offset, uid + 1)
                cb = u.get('callback_query')
                m = u.get('message') or (cb or {}).get('message') or {}
                who = (cb or {}).get('from') or m.get('from') or {}
                if m.get('chat', {}).get('type') != 'private' or who.get('is_bot') or not who.get('id'):
                    continue
                user_id = int(who['id'])
                source = source_of(m.get('text', '')) if not cb else None
                c.execute('''INSERT INTO users(id,username,first_name,last_name,first_source,last_source,created,seen)
                  VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,
                  first_name=excluded.first_name,last_name=excluded.last_name,seen=excluded.seen''',
                  (user_id, who.get('username'), who.get('first_name'), who.get('last_name'), source or 'direct', source or 'direct', now, now))
                if source:
                    c.execute('UPDATE users SET last_source=? WHERE id=?', (source, user_id))
                text = m.get('text', '')
                kind = 'button' if cb else ('start' if text.split(' ', 1)[0].split('@')[0] == '/start' else ('command' if text.startswith('/') else ('photo' if m.get('photo') or m.get('document') else 'text')))
                c.execute('INSERT INTO events(user_id,kind,source,created) VALUES(?,?,?,?)', (user_id, kind, source, now))
                c.execute('INSERT INTO updates(id,user_id,payload,created) VALUES(?,?,?,?)', (uid, user_id, json.dumps(u, ensure_ascii=False), now))
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('offset', str(offset)))

    def recover(self):
        with self.tx() as c:
            c.execute("UPDATE updates SET status='pending' WHERE status='working'")
            c.execute("UPDATE requests SET status='interrupted',error_code='restart' WHERE status='running'")

    def next_update(self):
        with self.tx() as c:
            row = c.execute("SELECT * FROM updates WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            c.execute("UPDATE updates SET status='working' WHERE id=?", (row['id'],))
            return json.loads(row['payload'])

    def finish(self, update_id):
        with self.tx() as c:
            c.execute("UPDATE updates SET status='done',payload=NULL WHERE id=?", (update_id,))

    def admin(self, user_id):
        return bool(self.rows('SELECT 1 FROM admins WHERE user_id=?', (user_id,)))

    def claim(self, user_id, provided, expected):
        if not expected or not hmac.compare_digest(provided, expected):
            return False
        digest = hashlib.sha256(expected.encode()).hexdigest()
        with self.tx() as c:
            if c.execute("SELECT 1 FROM settings WHERE key='claim_used' AND value=?", (digest,)).fetchone():
                return self.admin(user_id)
            if c.execute('SELECT 1 FROM admins LIMIT 1').fetchone():
                return False
            c.execute('INSERT OR IGNORE INTO admins VALUES(?)', (user_id,))
            c.execute("INSERT INTO settings VALUES('claim_used',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (digest,))
            return True

    def request(self, request_id, user_id, kind):
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO requests(id,user_id,kind,status,created) VALUES(?,?,?,'running',?)", (request_id, user_id, kind, time.time()))
            return cur.rowcount == 1

    def reserve_call(self, rid, user_id, user_limit, global_limit):
        day = day_key()
        with self.tx() as c:
            user_n = c.execute('SELECT COUNT(*) FROM api_calls WHERE user_id=? AND day=?', (user_id, day)).fetchone()[0]
            total = c.execute('SELECT COUNT(*) FROM api_calls WHERE day=?', (day,)).fetchone()[0]
            if user_n >= user_limit or total >= global_limit:
                return None
            return c.execute('INSERT INTO api_calls(request_id,user_id,day,created) VALUES(?,?,?,?)', (rid, user_id, day, time.time())).lastrowid

    def call_done(self, call_id, usage=None, status='ok'):
        usage = usage or {}
        with self.tx() as c:
            c.execute('UPDATE api_calls SET input_tokens=?,output_tokens=?,status=? WHERE id=?', (usage.get('input_tokens', 0), usage.get('output_tokens', 0), status, call_id))

    def result(self, rid, result):
        with self.tx() as c:
            c.execute("UPDATE requests SET status='generated',classification=?,reply=?,warning=? WHERE id=?", (result['kind'], result['reply'], result['warning'], rid))

    def delivered(self, rid):
        with self.tx() as c:
            c.execute("UPDATE requests SET status='success',delivered=? WHERE id=?", (time.time(), rid))

    def fail(self, rid, code):
        with self.tx() as c:
            c.execute("UPDATE requests SET status='error',error_code=? WHERE id=?", (code, rid))

    def saved_reply(self, rid, user_id):
        rows = self.rows('SELECT reply FROM requests WHERE id=? AND user_id=?', (rid, user_id))
        return rows[0]['reply'] if rows else None

    def counts(self, user_id=None):
        where, args = (' WHERE user_id=?', (user_id,)) if user_id is not None else ('', ())
        r = self.rows('''SELECT COUNT(*) requests, COALESCE(SUM(status='success'),0) success,
          COALESCE(SUM(status='error'),0) errors, COALESCE(SUM(classification='vacancy'),0) vacancies
          FROM requests''' + where, args)[0]
        r.update(self.rows('SELECT COUNT(*) api_calls,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens FROM api_calls' + where, args)[0])
        return r

    def delete_user(self, user_id):
        with self.tx() as c:
            c.execute('DELETE FROM users WHERE id=?', (user_id,))
            c.execute('DELETE FROM updates WHERE user_id=?', (user_id,))

    def cleanup(self):
        with self.tx() as c:
            c.execute('UPDATE requests SET reply=NULL,warning=NULL WHERE created<?', (time.time() - 7*86400,))
            c.execute("DELETE FROM updates WHERE status='done' AND created<?", (time.time() - 7*86400,))
            c.execute('DELETE FROM events WHERE created<?', (time.time() - 180*86400,))
