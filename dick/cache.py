import json
import sqlite3
import threading
import time

from .models import Package


class Cache:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(directory / "index.sqlite3", timeout=30, check_same_thread=False)
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS packages (
                name TEXT NOT NULL, source TEXT NOT NULL, description TEXT NOT NULL,
                version TEXT NOT NULL, repository TEXT NOT NULL, architecture TEXT NOT NULL,
                PRIMARY KEY (source, repository, name, architecture)
            );
            CREATE INDEX IF NOT EXISTS packages_name ON packages(name COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS snapshots (
                source TEXT NOT NULL, repository TEXT NOT NULL, updated REAL NOT NULL,
                count INTEGER NOT NULL, PRIMARY KEY (source, repository)
            );
            CREATE TABLE IF NOT EXISTS queries (
                source TEXT NOT NULL, query TEXT NOT NULL, updated REAL NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY (source, query)
            );
        """)

    def close(self):
        with self.lock:
            self.connection.close()

    def replace(self, repository, packages):
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM packages WHERE source=? AND repository=?",
                                    (repository.source, repository.name))
            self.connection.executemany("INSERT OR REPLACE INTO packages VALUES (?, ?, ?, ?, ?, ?)",
                                        ((package.name, package.source, package.description, package.version,
                                          package.repository, package.architecture) for package in packages))
            count = self.connection.execute("SELECT COUNT(*) FROM packages WHERE source=? AND repository=?",
                                            (repository.source, repository.name)).fetchone()[0]
            self.connection.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?)",
                                    (repository.source, repository.name, time.time(), count))
        return count

    def search(self, query, sources, exact=False):
        if not sources:
            return []
        placeholders = ",".join("?" for source in sources)
        if exact:
            clause, parameters = "name = ? COLLATE NOCASE", [query]
        else:
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clause, parameters = "(name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')", [pattern, pattern]
        with self.lock:
            rows = self.connection.execute(
                f"SELECT name,source,description,version,repository,architecture FROM packages "
                f"WHERE {clause} AND source IN ({placeholders}) ORDER BY name,source,repository",
                [*parameters, *sources],
            )
            return [Package(*row) for row in rows]

    def snapshot(self, repository):
        with self.lock:
            return self.connection.execute("SELECT updated,count FROM snapshots WHERE source=? AND repository=?",
                                           (repository.source, repository.name)).fetchone()

    def query_get(self, source, query, ttl):
        with self.lock:
            row = self.connection.execute("SELECT updated,payload FROM queries WHERE source=? AND query=?",
                                           (source, query)).fetchone()
        if row is None or time.time() - row[0] >= ttl:
            return None
        return [Package(**value) for value in json.loads(row[1])]

    def query_put(self, source, query, packages):
        payload = json.dumps([package.to_dict() for package in packages], ensure_ascii=False)
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO queries VALUES (?, ?, ?, ?)",
                                    (source, query, time.time(), payload))

    def invalidate_queries(self, sources):
        with self.lock, self.connection:
            for source in sources:
                self.connection.execute("DELETE FROM queries WHERE source=?", (source,))

    def prune(self, repositories, sources):
        active = {(repository.source, repository.name) for repository in repositories}
        with self.lock, self.connection:
            for source, name in self.connection.execute("SELECT source,repository FROM snapshots").fetchall():
                if source in sources and (source, name) not in active:
                    self.connection.execute("DELETE FROM packages WHERE source=? AND repository=?", (source, name))
                    self.connection.execute("DELETE FROM snapshots WHERE source=? AND repository=?", (source, name))
