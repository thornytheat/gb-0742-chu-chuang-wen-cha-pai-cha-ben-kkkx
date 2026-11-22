import json
import sqlite3
from contextlib import closing, contextmanager

from app.errors import FieldError
from app.manifest import canonical


class ArchiveStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            yield connection

    def setup(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS shift_archives (
                    id TEXT PRIMARY KEY,
                    manifest TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    input TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS primary_conclusions (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    batch TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    doc TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created TEXT NOT NULL,
                    UNIQUE(run_id, batch)
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS primary_conclusions_run
                ON primary_conclusions(run_id, version)
            """)

    def insert(self, manifest, evidence, source):
        with self.connect() as connection:
            cursor = connection.execute("INSERT INTO shift_archives VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                                        (manifest["id"], canonical(manifest), canonical(evidence), canonical(source)))
            record = connection.execute("SELECT manifest FROM shift_archives WHERE id = ?", (manifest["id"],)).fetchone()
            return json.loads(record[0]), cursor.rowcount == 1

    def fetch(self, run_id):
        with self.connect() as connection:
            record = connection.execute("SELECT manifest, evidence, input FROM shift_archives WHERE id = ?", (run_id,)).fetchone()
        if record is None:
            raise FieldError("UNKNOWN_RUN", "该归卷未登记", 404)
        return tuple(json.loads(value) for value in record)

    def history(self):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT manifest FROM shift_archives ORDER BY rowid DESC")]

    def conclusion_version(self, run_id, batch):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT doc FROM primary_conclusions WHERE run_id = ? AND batch = ?",
                (run_id, batch)).fetchone()
        return json.loads(row[0]) if row else None

    def latest_conclusion(self, run_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT doc FROM primary_conclusions WHERE run_id = ? ORDER BY version DESC LIMIT 1",
                (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def conclusion_versions(self, run_id):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT doc FROM primary_conclusions WHERE run_id = ? ORDER BY version",
                (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_conclusion(self, doc, digest, created):
        """同批换装重跑命中唯一键即复用旧版；写入失败整体回滚，旧版保留。"""
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO primary_conclusions (id, run_id, batch, version, doc, digest, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, batch) DO NOTHING",
                (doc["id"], doc["run_id"], doc["batch"], doc["version"],
                 canonical(doc), digest, created))
            fresh = cursor.rowcount == 1
            row = connection.execute(
                "SELECT doc FROM primary_conclusions WHERE run_id = ? AND batch = ?",
                (doc["run_id"], doc["batch"])).fetchone()
        return json.loads(row[0]), fresh
