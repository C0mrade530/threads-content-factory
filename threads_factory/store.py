from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
import json
import sqlite3

from .models import Schedule


def now_iso():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "factory.sqlite3"
        with self.tx() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, slot TEXT NOT NULL, revision INTEGER NOT NULL,
                state TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', reference_id TEXT, error TEXT,
                url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(revision,slot));
              CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, action TEXT NOT NULL, created_at TEXT NOT NULL);
              """)
            c.execute("INSERT OR IGNORE INTO config VALUES(1,?)", (json.dumps({"revision": 1, "profile": None,
                "schedule": Schedule().model_dump(), "account": None}),))
        self.path.chmod(0o600)

    @contextmanager
    def tx(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def config(self):
        with self.tx() as c:
            return json.loads(c.execute("SELECT data FROM config WHERE id=1").fetchone()[0])

    def update(self, key, value):
        with self.tx() as c:
            cfg = json.loads(c.execute("SELECT data FROM config WHERE id=1").fetchone()[0])
            cfg[key] = value
            cfg["revision"] += 1
            c.execute("UPDATE config SET data=? WHERE id=1", (json.dumps(cfg, ensure_ascii=False),))
            c.execute("UPDATE posts SET state='cancelled',error='settings_changed',updated_at=? WHERE state IN ('queued','ready','blocked','generating')", (now_iso(),))
            c.execute("INSERT INTO audit(action,created_at) VALUES(?,?)", ("update_" + key, now_iso()))
            return cfg

    def jobs(self, limit=100):
        with self.tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM posts ORDER BY slot DESC LIMIT ?", (limit,))]

    def ensure(self, slot, revision, text=""):
        identity = str(uuid5(NAMESPACE_URL, f"threads-factory:{revision}:{slot}"))
        with self.tx() as c:
            cfg = json.loads(c.execute("SELECT data FROM config WHERE id=1").fetchone()[0])
            if cfg['revision'] != revision or not cfg['schedule']['enabled']:
                return None
            c.execute("INSERT OR IGNORE INTO posts(id,slot,revision,state,text,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (identity, slot, revision, "ready" if text else "queued", text, now_iso(), now_iso()))
            return dict(c.execute("SELECT * FROM posts WHERE id=?", (identity,)).fetchone())

    def claim(self, identity, expected, next_state):
        with self.tx() as c:
            cfg = json.loads(c.execute("SELECT data FROM config WHERE id=1").fetchone()[0])
            if not cfg['schedule']['enabled']:
                return None
            row = c.execute("SELECT * FROM posts WHERE id=? AND state=? AND revision=?", (identity, expected, cfg['revision'])).fetchone()
            if not row:
                return None
            c.execute("UPDATE posts SET state=?,error=NULL,updated_at=? WHERE id=?", (next_state, now_iso(), identity))
            return dict(row)

    def finish(self, identity, state, *, text=None, reference=None, error=None, url=None):
        with self.tx() as c:
            c.execute("UPDATE posts SET state=?,text=COALESCE(?,text),reference_id=COALESCE(?,reference_id),error=?,url=COALESCE(?,url),updated_at=? WHERE id=? AND state<>'cancelled'",
                      (state, text, reference, error, url, now_iso(), identity))

    def recover(self):
        with self.tx() as c:
            c.execute("UPDATE posts SET state=CASE WHEN state='sending' THEN 'unknown' ELSE 'generation_unknown' END,error='worker_interrupted',updated_at=? WHERE state IN ('sending','generating')", (now_iso(),))

    def enqueue(self, batch, now):
        payload = json.dumps(batch.model_dump(), sort_keys=True, ensure_ascii=False)
        with self.tx() as c:
            previous = c.execute("SELECT * FROM batches WHERE id=?", (batch.request_id,)).fetchone()
            if previous:
                if previous['payload'] != payload:
                    raise ValueError("request_id уже использован для другого пакета")
                return json.loads(previous['result'])
            cfg = json.loads(c.execute("SELECT data FROM config WHERE id=1").fetchone()[0])
            if not batch.approved or not cfg['schedule']['enabled'] or not cfg['profile'] or not cfg['account']:
                raise ValueError("Нужны профиль, подключённый аккаунт, включённое расписание и утверждение текстов")
            schedule = Schedule.model_validate(cfg['schedule'])
            occupied = {r[0] for r in c.execute("SELECT slot FROM posts WHERE revision=?", (cfg['revision'],))}
            free = [s.isoformat() for s in schedule.slots(now, days=60) if s > now and s.isoformat() not in occupied]
            if len(free) < len(batch.posts):
                raise ValueError("Не хватает свободных слотов")
            ids = []
            for index, (text, slot) in enumerate(zip(batch.posts, free)):
                identity = str(uuid5(NAMESPACE_URL, f"threads-factory:batch:{batch.request_id}:{index}"))
                c.execute("INSERT INTO posts(id,slot,revision,state,text,created_at,updated_at) VALUES(?,?,?,'ready',?,?,?)",
                          (identity, slot, cfg['revision'], text.strip(), now_iso(), now_iso()))
                ids.append(identity)
            result = {"queued": len(ids), "post_ids": ids, "slots": free[:len(ids)]}
            c.execute("INSERT INTO batches VALUES(?,?,?)", (batch.request_id, payload, json.dumps(result)))
            c.execute("INSERT INTO audit(action,created_at) VALUES('enqueue_approved_posts',?)", (now_iso(),))
            return result
