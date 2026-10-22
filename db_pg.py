"""PostgreSQL backend sharing persistence rules with SQLite."""
import contextlib
import os
import re
import threading
from db import DB as SQLiteDB, SCHEMA

TRANSACTION_LOCK = 8685856140
WORKER_LOCK = 8685856141


def translate(sql):
    sql = sql.strip().rstrip(';')
    ignored = bool(re.match(r'INSERT OR IGNORE', sql, re.I))
    sql = re.sub(r'INSERT OR IGNORE', 'INSERT', sql, flags=re.I)
    if ignored:
        sql += ' ON CONFLICT DO NOTHING'
    return sql.replace('?', '%s')


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, cursor, insert_id=False):
        self.rowcount = cursor.rowcount
        self.lastrowid = None
        self.data = []
        if cursor.description:
            names = [column.name for column in cursor.description]
            self.data = [Row(zip(names, row)) for row in cursor.fetchall()]
        if insert_id and self.data:
            self.lastrowid = self.data[0][0]
        cursor.close()

    def fetchone(self):
        return self.data.pop(0) if self.data else None

    def fetchall(self):
        rows, self.data = self.data, []
        return rows


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        sql = translate(sql)
        insert_id = bool(re.match(r'INSERT INTO api_calls\(', sql, re.I))
        if insert_id:
            sql += ' RETURNING id'
        return Result(self.raw.execute(sql, args), insert_id)

    def commit(self):
        self.execute('COMMIT')

    def rollback(self):
        self.execute('ROLLBACK')

    def close(self):
        self.raw.close()


class DB(SQLiteDB):
    def __init__(self, path=None):
        import psycopg
        self.path = 'postgresql'
        self.lock = threading.RLock()
        self.url = os.environ['DATABASE_URL']
        self.psycopg = psycopg
        self.worker_owned = False
        self.connect()
        with self.tx() as c:
            for sql in SCHEMA.split(';'):
                sql = sql.strip()
                if not sql or sql.startswith('PRAGMA'):
                    continue
                sql = sql.replace(' INTEGER', ' BIGINT').replace(' REAL', ' DOUBLE PRECISION')
                if re.match(r'CREATE TABLE IF NOT EXISTS (events|api_calls)\(', sql):
                    sql = sql.replace('id BIGINT PRIMARY KEY', 'id BIGSERIAL PRIMARY KEY', 1)
                c.execute(sql)

    def connect(self):
        raw = self.psycopg.connect(
            self.url, autocommit=True, connect_timeout=10, prepare_threshold=None,
            sslmode=os.getenv('PGSSLMODE', 'require'),
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
            options='-c statement_timeout=15000 -c lock_timeout=10000')
        self.c = Connection(raw)
        if self.worker_owned:
            acquired = self.c.execute('SELECT pg_try_advisory_lock(?)', (WORKER_LOCK,)).fetchone()[0]
            if not acquired:
                self.c.close()
                raise RuntimeError('Worker ownership lost')

    def ensure(self):
        if self.c.raw.closed or self.c.raw.broken:
            self.connect()

    def acquire_worker(self):
        """Only one deployment processes the queue during a rolling restart."""
        with self.lock:
            self.ensure()
            if self.worker_owned:
                return True
            self.worker_owned = bool(self.c.execute(
                'SELECT pg_try_advisory_lock(?)', (WORKER_LOCK,)).fetchone()[0])
            return self.worker_owned

    def release_worker(self):
        with self.lock:
            if self.worker_owned and not self.c.raw.closed:
                self.c.execute('SELECT pg_advisory_unlock(?)', (WORKER_LOCK,))
            self.worker_owned = False

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.ensure()
            try:
                self.c.execute('BEGIN')
                self.c.execute('SELECT pg_advisory_xact_lock(?)', (TRANSACTION_LOCK,))
                yield self.c
                self.c.commit()
            except BaseException:
                try:
                    self.c.rollback()
                except Exception:
                    pass
                raise

    def rows(self, sql, params=()):
        with self.lock:
            self.ensure()
            return self.c.execute(sql, params).fetchall()
