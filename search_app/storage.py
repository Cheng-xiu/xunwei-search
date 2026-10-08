"""Small, concurrent-safe local evidence library and finished-search history."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid


_PRIVATE_KEYS = {"apikey", "authorization", "proxyauthorization", "password", "secret", "accesstoken", "refreshtoken", "token", "headers", "config", "settings", "credentials"}
_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b", re.I)
_PARAM_PATTERN = re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret|password)=)[^&#\s]+")


def _safe(value):
    """Never persist AI credentials accidentally included in job snapshots."""
    if isinstance(value, dict):
        return {
            str(key): _safe(item) for key, item in value.items()
            if re.sub(r"[^a-z]", "", str(key).lower()) not in _PRIVATE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return _PARAM_PATTERN.sub(r"\1[已移除密钥]", _KEY_PATTERN.sub("[已移除密钥]", value))
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normal(value: str) -> str:
    return unicodedata.normalize("NFKC", value).lower()


def _terms(query: str) -> list[str]:
    chunks = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", _normal(query))
    tokens: list[str] = []
    for chunk in chunks:
        if not re.search(r"[\u3400-\u9fff]", chunk) or len(chunk) <= 2:
            tokens.append(chunk)
        else:
            # Chinese text needs no whitespace or external tokenizer.
            tokens.extend(chunk[index:index + size] for size in (2, 3) for index in range(len(chunk) - size + 1))
    return list(dict.fromkeys(tokens))


class Storage:
    """Open a short-lived SQLite connection for every operation.

    SQLite WAL and a busy timeout permit parallel search jobs and library edits.
    At most 100 finished jobs are retained. Imported documents stay until deleted.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._memory = self.path == ":memory:"
        self._keeper = None
        if self._memory:
            self.path = f"file:smart-search-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS library (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'local',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS history_created_at ON history(created_at DESC);
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15, uri=self._memory)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add_document(self, title: str, url: str, text: str, platform: str) -> dict:
        document = {
            "id": uuid.uuid4().hex,
            "title": _safe(str(title or "未命名导入内容").strip()),
            "url": _safe(str(url or "").strip()),
            "text": _safe(str(text or "")),
            "platform": _safe(str(platform or "local")),
            "created_at": _now(),
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO library (id,title,url,text,platform,created_at) VALUES (:id,:title,:url,:text,:platform,:created_at)",
                document,
            )
        return document

    def list_documents(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM library ORDER BY created_at DESC, rowid DESC").fetchall()
        return [dict(row) for row in rows]

    def delete_document(self, id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM library WHERE id = ?", (str(id),))
            return cursor.rowcount > 0

    def search_documents(self, query: str, limit: int = 30) -> list[dict]:
        terms = _terms(query)
        if not terms or limit <= 0:
            return []
        normalized_query = _normal(query).strip()
        matches: list[tuple[float, dict]] = []
        for document in self.list_documents():
            title, body = _normal(document["title"]), _normal(document["text"])
            matched = [term for term in terms if term in title or term in body]
            if not matched:
                continue
            # Title matches carry more evidence. Coverage prevents a common
            # two-character fragment from outranking a precise long-tail match.
            coverage = len(matched) / len(terms)
            score = sum((3 if term in title else 1) * (1.2 if len(term) >= 3 else 1) for term in matched) * coverage
            if normalized_query in title:
                score += 10
            if normalized_query in body:
                score += 5
            first = min((body.find(term) for term in matched if term in body), default=0)
            start = max(0, first - 70)
            snippet = document["text"][start:start + 360]
            if start:
                snippet = "…" + snippet
            if start + 360 < len(document["text"]):
                snippet += "…"
            matches.append((score, {
                "id": document["id"],
                "title": document["title"],
                "url": document["url"],
                "snippet": snippet,
                "body": document["text"],
                "platform": document["platform"],
                "source": "local",
                "content_level": "local",
            }))
        matches.sort(key=lambda item: item[0], reverse=True)
        return [document for _, document in matches[:min(int(limit), 500)]]

    def save_job(self, job: dict) -> None:
        state = str(job.get("state", job.get("status", "done"))).lower()
        if state not in {"done", "completed", "succeeded", "success", "finished", "error", "failed", "cancelled", "stopped", "awaiting_user"}:
            return
        if not job.get("id"):
            raise ValueError("搜索记录缺少 id")
        safe_job = _safe(job)
        safe_job.setdefault("created_at", _now())
        results = safe_job.get("results", [])
        result_count = len(results) if isinstance(results, (list, dict)) else 0
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO history (id,query,created_at,count,payload) VALUES (?,?,?,?,?)",
                (str(safe_job["id"]), str(safe_job.get("query", "")), str(safe_job["created_at"]), result_count, json.dumps(safe_job, ensure_ascii=False)),
            )
            connection.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY created_at DESC, rowid DESC LIMIT 100)")

    def get_job(self, id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM history WHERE id = ?", (str(id),)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_history(self, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,query,created_at,count FROM history ORDER BY created_at DESC, rowid DESC LIMIT ?", (max(0, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None
