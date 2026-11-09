"""SQLite 索引：扫描结果持久化与查询（排序 / 过滤 / 目录钻取 / 历史记录）。

每个线程使用自己的连接（WAL 模式下读不阻塞写），GUI 线程和后台查询线程互不等待；
扫描与分析在子进程里写库，GUI 进程只读。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from .util import FLAG_PLACEHOLDER, FLAG_SYSTEM

SCHEMA_VERSION = 3

# 目录增长对比只记录较浅、较大的目录，避免历史表膨胀
HISTORY_MAX_DEPTH = 5
HISTORY_MIN_SIZE = 64 * 1024 * 1024
HISTORY_KEEP_SCANS = 10
VOLUME_HISTORY_KEEP_DAYS = 400
WAL_SIZE_LIMIT = 64 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS volumes(
    letter TEXT PRIMARY KEY,
    label TEXT,
    fs TEXT,
    total INTEGER,
    free INTEGER,
    scanned_at INTEGER
);

CREATE TABLE IF NOT EXISTS scans(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started INTEGER,
    finished INTEGER,
    roots TEXT,
    files INTEGER,
    bytes INTEGER,
    dirs INTEGER,
    errors INTEGER,
    method TEXT,
    partial INTEGER
);

CREATE TABLE IF NOT EXISTS dirs(
    path TEXT PRIMARY KEY,
    parent TEXT,
    drive TEXT,
    depth INTEGER,
    size INTEGER,
    files INTEGER,
    mtime INTEGER,
    scan_id INTEGER
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS big_files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT,
    name TEXT,
    ext TEXT,
    drive TEXT,
    dir TEXT,
    size INTEGER,
    mtime INTEGER,
    flags INTEGER
);

CREATE TABLE IF NOT EXISTS recent_files(
    path TEXT PRIMARY KEY,
    name TEXT,
    ext TEXT,
    drive TEXT,
    dir TEXT,
    size INTEGER,
    mtime INTEGER,
    flags INTEGER
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cleanup_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT,
    title TEXT,
    path TEXT,
    size INTEGER,
    level TEXT,
    reason TEXT,
    deletable INTEGER,
    kind TEXT,
    action TEXT,
    measured_at INTEGER
);

CREATE TABLE IF NOT EXISTS volume_history(
    letter TEXT,
    ts INTEGER,
    total INTEGER,
    free INTEGER,
    PRIMARY KEY(letter, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS dir_history(
    scan_id INTEGER,
    path TEXT,
    drive TEXT,
    depth INTEGER,
    size INTEGER,
    files INTEGER,
    PRIMARY KEY(scan_id, path)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    letter TEXT,
    kind TEXT,
    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_big_size ON big_files(size DESC);
CREATE INDEX IF NOT EXISTS idx_big_drive ON big_files(drive, size DESC);
CREATE INDEX IF NOT EXISTS idx_recent_mt ON recent_files(mtime DESC);
CREATE INDEX IF NOT EXISTS idx_big_dir ON big_files(dir);
CREATE INDEX IF NOT EXISTS idx_recent_dir ON recent_files(dir);
CREATE INDEX IF NOT EXISTS idx_dirs_parent ON dirs(parent, size DESC);
CREATE INDEX IF NOT EXISTS idx_dirs_depth ON dirs(depth, size DESC);
CREATE INDEX IF NOT EXISTS idx_clean_cat ON cleanup_items(category);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC)
"""

_DATA_TABLES = ("volumes", "scans", "dirs", "big_files", "recent_files", "cleanup_items",
                "volume_history", "dir_history", "alerts")

SORT_COLUMNS = {
    "size": "size",
    "mtime": "mtime",
    "name": "name COLLATE NOCASE",
    "path": "path COLLATE NOCASE",
    "ext": "ext",
}


def _safe_text(value):
    """SQLite 不接受孤立的 UTF-16 代理字符（少量非 Unicode 程序生成的文件名），替换成 U+FFFD。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8", "replace")


def _drive_letter(value: str) -> str:
    value = (value or "").strip().rstrip("\\").upper()
    if len(value) == 1:
        value += ":"
    return value[:2]


class Index:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._closed = False
        with self.lock:
            self._ensure_schema()

    # ---------------- 连接 ----------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT}")
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        conn = self.conn
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is not None and str(row["value"]) != str(SCHEMA_VERSION):
            for table in _DATA_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("DELETE FROM meta")
        for stmt in _SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    def close(self) -> None:
        self._closed = True
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.commit()
            except sqlite3.Error:
                pass
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    # ---------------- 写入 ----------------
    def replace_scan(
        self,
        volumes,
        dirs,
        big_files,
        recent_files,
        partial: bool = False,
        drives: list[str] | None = None,
        stats=None,
        method: str = "scandir",
    ) -> int:
        """写入一次扫描结果，返回 scan_id。

        drives 给定时只替换这些盘的数据（只扫 C: 不会清掉 D: 的索引）；为 None 时替换全部。
        """
        scanned_at = int(time.time())
        drive_list = sorted({_drive_letter(d) for d in drives}) if drives else None
        with self.lock:
            conn = self.conn
            cur = conn.cursor()
            cur.execute("BEGIN")
            try:
                if drive_list is None:
                    for table in ("volumes", "dirs", "big_files", "recent_files"):
                        cur.execute(f"DELETE FROM {table}")
                else:
                    marks = ",".join("?" * len(drive_list))
                    cur.execute(f"DELETE FROM volumes WHERE letter IN ({marks})", drive_list)
                    for table in ("dirs", "big_files", "recent_files"):
                        cur.execute(f"DELETE FROM {table} WHERE drive IN ({marks})", drive_list)
                cur.execute(
                    "INSERT INTO scans(started, finished, roots, files, bytes, dirs, errors, method, partial)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        int(getattr(stats, "started_at", 0) or scanned_at),
                        scanned_at,
                        ",".join(drive_list) if drive_list else "*",
                        int(getattr(stats, "files", 0) or 0),
                        int(getattr(stats, "bytes", 0) or 0),
                        int(getattr(stats, "dirs", 0) or len(dirs)),
                        int(getattr(stats, "errors", 0) or 0),
                        method,
                        1 if partial else 0,
                    ),
                )
                scan_id = int(cur.lastrowid)
                cur.executemany(
                    "INSERT OR REPLACE INTO volumes(letter, label, fs, total, free, scanned_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        (_safe_text(v.letter), _safe_text(v.label), _safe_text(v.fs),
                         int(v.total), int(v.free), scanned_at)
                        for v in volumes
                    ),
                )
                cur.executemany(
                    "INSERT OR REPLACE INTO volume_history(letter, ts, total, free) VALUES(?,?,?,?)",
                    ((v.letter, scanned_at, int(v.total), int(v.free)) for v in volumes),
                )
                cur.executemany(
                    "INSERT OR REPLACE INTO dirs(path, parent, drive, depth, size, files, mtime, scan_id)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        (_safe_text(d.path), _safe_text(d.parent), _safe_text(d.drive),
                         d.depth, int(d.size), int(d.files), int(d.mtime), scan_id)
                        for d in dirs
                    ),
                )
                if not partial:
                    cur.executemany(
                        "INSERT OR REPLACE INTO dir_history(scan_id, path, drive, depth, size, files)"
                        " VALUES(?,?,?,?,?,?)",
                        (
                            (scan_id, _safe_text(d.path), _safe_text(d.drive), d.depth, int(d.size), int(d.files))
                            for d in dirs
                            if d.depth <= HISTORY_MAX_DEPTH and d.size >= HISTORY_MIN_SIZE
                        ),
                    )
                cur.executemany(
                    "INSERT INTO big_files(path, name, ext, drive, dir, size, mtime, flags)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        (_safe_text(f.path), _safe_text(f.name), _safe_text(f.ext),
                         _safe_text(f.drive), _safe_text(f.directory),
                         int(f.size), int(f.mtime), int(f.flags))
                        for f in big_files
                    ),
                )
                cur.executemany(
                    "INSERT OR REPLACE INTO recent_files(path, name, ext, drive, dir, size, mtime, flags)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        (_safe_text(f.path), _safe_text(f.name), _safe_text(f.ext),
                         _safe_text(f.drive), _safe_text(f.directory),
                         int(f.size), int(f.mtime), int(f.flags))
                        for f in recent_files
                    ),
                )
                cur.execute(
                    "DELETE FROM dir_history WHERE scan_id NOT IN"
                    " (SELECT id FROM scans WHERE partial = 0 ORDER BY id DESC LIMIT ?)",
                    (HISTORY_KEEP_SCANS,),
                )
                cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_scan', ?)", (str(scanned_at),))
                cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('partial', ?)", ("1" if partial else "0",))
                cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('scan_method', ?)", (method,))
                cur.execute("COMMIT")
            except sqlite3.Error:
                cur.execute("ROLLBACK")
                raise
        return scan_id

    def replace_cleanup(self, items) -> None:
        """items: CleanItem 列表（category/title/path/size/level/reason/deletable/kind/action）。"""
        measured = int(time.time())
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.execute("DELETE FROM cleanup_items")
                cur.executemany(
                    "INSERT INTO cleanup_items(category, title, path, size, level, reason,"
                    " deletable, kind, action, measured_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [
                        (_safe_text(it.category), _safe_text(it.title), _safe_text(it.path),
                         int(it.size), it.level, _safe_text(it.reason),
                         1 if it.deletable else 0, it.kind, it.action, measured)
                        for it in items
                    ],
                )
                cur.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('last_cleanup', ?)",
                    (str(measured),),
                )
                cur.execute("COMMIT")
            except sqlite3.Error:
                cur.execute("ROLLBACK")
                raise

    def checkpoint(self) -> None:
        """把 WAL 合并回主库并截断（大批量写入后调用，避免 -wal 文件长期占用几百 MB）。"""
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def replace_subtree(self, root: str, dirs, big_files, recent_files) -> dict:
        """只重扫一个文件夹后，替换它在索引里的子树，并把大小变化累加到所有上级目录。

        返回 {"before": 旧大小, "after": 新大小}。
        """
        root = _dir_key(root)
        lo, hi = _subtree_range(root)
        new_root = next((d for d in dirs if d.path == root), None)
        with self.lock:
            conn = self.conn
            cur = conn.cursor()
            cur.execute("BEGIN")
            try:
                old = cur.execute("SELECT size, files, scan_id FROM dirs WHERE path = ?", (root,)).fetchone()
                scan_id = int(old["scan_id"]) if old and old["scan_id"] is not None else 0
                cur.execute("DELETE FROM dirs WHERE path = ? OR (path >= ? AND path < ?)", (root, lo, hi))
                for table in ("big_files", "recent_files"):
                    cur.execute(f"DELETE FROM {table} WHERE path >= ? AND path < ?", (lo, hi))
                cur.executemany(
                    "INSERT OR REPLACE INTO dirs(path, parent, drive, depth, size, files, mtime, scan_id)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        (_safe_text(d.path), _safe_text(d.parent), _safe_text(d.drive),
                         d.depth, int(d.size), int(d.files), int(d.mtime), scan_id)
                        for d in dirs
                    ),
                )
                before = (int(old["size"] or 0), int(old["files"] or 0)) if old else (0, 0)
                after = (int(new_root.size), int(new_root.files)) if new_root else (0, 0)
                self._shift_ancestors(cur, _parent_dir(root), after[0] - before[0], after[1] - before[1])
                if new_root is not None:
                    self._bump_mtime(cur, _parent_dir(root), new_root.mtime)
                cur.executemany(
                    "INSERT INTO big_files(path, name, ext, drive, dir, size, mtime, flags) VALUES(?,?,?,?,?,?,?,?)",
                    ((_safe_text(f.path), _safe_text(f.name), _safe_text(f.ext), _safe_text(f.drive),
                      _safe_text(f.directory), int(f.size), int(f.mtime), int(f.flags)) for f in big_files),
                )
                cur.executemany(
                    "INSERT OR REPLACE INTO recent_files(path, name, ext, drive, dir, size, mtime, flags)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    ((_safe_text(f.path), _safe_text(f.name), _safe_text(f.ext), _safe_text(f.drive),
                      _safe_text(f.directory), int(f.size), int(f.mtime), int(f.flags)) for f in recent_files),
                )
                cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_refresh', ?)",
                            (str(int(time.time())),))
                cur.execute("COMMIT")
            except sqlite3.Error:
                cur.execute("ROLLBACK")
                raise
        return {"before": before[0], "after": after[0]}

    def apply_deletions(self, deleted: list[tuple[str, int]]) -> int:
        """删除 / 迁移后同步索引：去掉对应条目，并从各级上级目录里减掉它们的大小。

        deleted: [(路径, 文件大小)]，目录的大小以索引里记录的为准。返回从索引里减掉的总字节数。
        """
        if not deleted:
            return 0
        freed = 0
        with self.lock:
            conn = self.conn
            cur = conn.cursor()
            cur.execute("BEGIN")
            try:
                for path, size in deleted:
                    key = _dir_key(path)
                    row = cur.execute("SELECT size, files FROM dirs WHERE path = ?", (key,)).fetchone()
                    if row is not None:
                        lo, hi = _subtree_range(key)
                        cur.execute("DELETE FROM dirs WHERE path = ? OR (path >= ? AND path < ?)", (key, lo, hi))
                        for table in ("big_files", "recent_files"):
                            cur.execute(f"DELETE FROM {table} WHERE path >= ? AND path < ?", (lo, hi))
                        gone, files = int(row["size"] or 0), int(row["files"] or 0)
                    else:
                        cur.execute("DELETE FROM big_files WHERE path = ?", (path,))
                        cur.execute("DELETE FROM recent_files WHERE path = ?", (path,))
                        gone, files = int(size or 0), 1
                    self._shift_ancestors(cur, _parent_dir(key), -gone, -files)
                    freed += gone
                cur.execute("COMMIT")
            except sqlite3.Error:
                cur.execute("ROLLBACK")
                raise
        return freed

    def remove_paths(self, paths: list[str]) -> None:
        self.apply_deletions([(p, 0) for p in paths])

    def sync_dir_files(self, path: str, own_bytes: int, own_files: int, mtime: float,
                       big_files, recent_files) -> int:
        """用新读到的「本层文件」替换一个目录的数据：本层大小差值累加到它和所有上级，
        并替换它名下的大文件 / 最近文件条目。目录不在索引里时不做任何事。返回大小差值。"""
        key = _dir_key(path)
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN")
            try:
                row = cur.execute("SELECT size, files FROM dirs WHERE path = ?", (key,)).fetchone()
                if row is None:
                    cur.execute("ROLLBACK")
                    return 0
                kids = cur.execute(
                    "SELECT COALESCE(SUM(size),0) AS s, COALESCE(SUM(files),0) AS f FROM dirs"
                    " WHERE parent = ? AND path != parent", (key,),
                ).fetchone()
                old_bytes = max(0, int(row["size"] or 0) - int(kids["s"]))
                old_files = max(0, int(row["files"] or 0) - int(kids["f"]))
                delta = int(own_bytes) - old_bytes
                self._shift_ancestors(cur, key, delta, int(own_files) - old_files)
                self._bump_mtime(cur, key, mtime)
                cur.execute("DELETE FROM big_files WHERE dir = ?", (key,))
                cur.execute("DELETE FROM recent_files WHERE dir = ?", (key,))
                cur.executemany(
                    "INSERT INTO big_files(path, name, ext, drive, dir, size, mtime, flags) VALUES(?,?,?,?,?,?,?,?)",
                    ((_safe_text(f.path), _safe_text(f.name), _safe_text(f.ext), _safe_text(f.drive),
                      _safe_text(f.directory), int(f.size), int(f.mtime), int(f.flags)) for f in big_files),
                )
                cur.executemany(
                    "INSERT OR REPLACE INTO recent_files(path, name, ext, drive, dir, size, mtime, flags)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    ((_safe_text(f.path), _safe_text(f.name), _safe_text(f.ext), _safe_text(f.drive),
                      _safe_text(f.directory), int(f.size), int(f.mtime), int(f.flags)) for f in recent_files),
                )
                cur.execute("COMMIT")
            except sqlite3.Error:
                cur.execute("ROLLBACK")
                raise
        return delta

    @staticmethod
    def _shift_ancestors(cur, start: str, size_delta: int, files_delta: int) -> None:
        if not start or (not size_delta and not files_delta):
            return
        cur.executemany(
            "UPDATE dirs SET size = MAX(0, size + ?), files = MAX(0, files + ?) WHERE path = ?",
            ((size_delta, files_delta, p) for p in _dir_chain(start)),
        )

    @staticmethod
    def _bump_mtime(cur, start: str, mtime: float) -> None:
        """目录的 mtime 表示整棵子树里最近的修改时间：沿上级链取较大值。"""
        if not start or not mtime:
            return
        cur.executemany(
            "UPDATE dirs SET mtime = MAX(COALESCE(mtime, 0), ?) WHERE path = ?",
            ((int(mtime), p) for p in _dir_chain(start)),
        )

    def add_volume_sample(self, letter: str, total: int, free: int, ts: int | None = None) -> None:
        ts = int(ts or time.time())
        with self.lock:
            conn = self.conn
            conn.execute(
                "INSERT OR REPLACE INTO volume_history(letter, ts, total, free) VALUES(?,?,?,?)",
                (letter, ts, int(total), int(free)),
            )
            conn.execute("DELETE FROM volume_history WHERE ts < ?", (ts - VOLUME_HISTORY_KEEP_DAYS * 86400,))
            conn.commit()

    def add_alert(self, letter: str, kind: str, message: str, ts: int | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO alerts(ts, letter, kind, message) VALUES(?,?,?,?)",
                (int(ts or time.time()), letter, kind, message),
            )
            self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, str(value)))
            self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    # ---------------- 查询 ----------------
    def has_index(self) -> bool:
        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM big_files) AS b, EXISTS(SELECT 1 FROM dirs) AS d"
        ).fetchone()
        return bool(row["b"] or row["d"])

    def clear(self) -> None:
        with self.lock:
            cur = self.conn.cursor()
            for table in ("volumes", "dirs", "big_files", "recent_files", "cleanup_items", "dir_history", "scans"):
                cur.execute(f"DELETE FROM {table}")
            cur.execute("DELETE FROM meta WHERE key IN ('last_scan','partial','last_cleanup','scan_method')")
            self.conn.commit()

    def counts(self) -> dict:
        row = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM big_files) AS big,"
            " (SELECT COUNT(*) FROM recent_files) AS recent,"
            " (SELECT COUNT(*) FROM dirs) AS dirs"
        ).fetchone()
        return {"big": row["big"], "recent": row["recent"], "dirs": row["dirs"]}

    def last_scan_time(self) -> int:
        try:
            return int(self.get_meta("last_scan", "0") or 0)
        except ValueError:
            return 0

    def cached_volumes(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM volumes ORDER BY letter").fetchall()
        return [dict(r) for r in rows]

    def scans(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_scan(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def big_files(
        self,
        drive: str | None = None,
        min_size: int = 0,
        ext: str | None = None,
        exts: list[str] | None = None,
        keyword: str | None = None,
        include_system: bool = True,
        sort: str = "size",
        desc: bool = True,
        page: int = 0,
        page_size: int = 500,
    ) -> tuple[list[dict], int, int]:
        where, params = self._big_where(drive, min_size, ext, exts, keyword, include_system)
        order = SORT_COLUMNS.get(sort, "size")
        direction = "DESC" if desc else "ASC"
        offset = max(0, page) * page_size
        sql = f"SELECT * FROM big_files {where} ORDER BY {order} {direction} LIMIT ? OFFSET ?"
        count_sql = f"SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS total FROM big_files {where}"
        conn = self.conn
        rows = conn.execute(sql, (*params, page_size, offset)).fetchall()
        total_row = conn.execute(count_sql, params).fetchone()
        return [dict(r) for r in rows], int(total_row["n"]), int(total_row["total"])

    @staticmethod
    def _big_where(drive, min_size, ext, exts, keyword, include_system):
        clauses = ["size >= ?"]
        params: list = [int(min_size or 0)]
        if drive:
            clauses.append("drive = ?")
            params.append(drive)
        if ext:
            clauses.append("ext = ?")
            params.append(ext.lower())
        if exts:
            clauses.append("ext IN (" + ",".join("?" * len(exts)) + ")")
            params.extend(exts)
        if keyword:
            clauses.append("(name LIKE ? OR path LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        if not include_system:
            clauses.append("(flags & ?) = 0")
            params.append(FLAG_SYSTEM)
        return "WHERE " + " AND ".join(clauses), params

    def recent_files(
        self,
        days: int = 7,
        drive: str | None = None,
        exclude_cache: bool = True,
        keyword: str | None = None,
        page: int = 0,
        page_size: int = 500,
    ) -> tuple[list[dict], int, int]:
        since = int(time.time() - max(1, days) * 86400)
        clauses = ["mtime >= ?"]
        params: list = [since]
        if drive:
            clauses.append("drive = ?")
            params.append(drive)
        if exclude_cache:
            clauses.append("(flags & 9) = 0")  # FLAG_SYSTEM | FLAG_CACHE
        if keyword:
            clauses.append("(name LIKE ? OR path LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        where = "WHERE " + " AND ".join(clauses)
        offset = max(0, page) * page_size
        sql = f"SELECT * FROM recent_files {where} ORDER BY mtime DESC LIMIT ? OFFSET ?"
        count_sql = f"SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS total FROM recent_files {where}"
        conn = self.conn
        rows = conn.execute(sql, (*params, page_size, offset)).fetchall()
        total_row = conn.execute(count_sql, params).fetchone()
        return [dict(r) for r in rows], int(total_row["n"]), int(total_row["total"])

    def root_dirs(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM dirs WHERE depth = 1 ORDER BY path").fetchall()
        return [dict(r) for r in rows]

    def dir_info(self, path: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM dirs WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def top_folders(self, drive: str | None = None, limit: int = 10) -> list[dict]:
        if drive:
            rows = self.conn.execute(
                "SELECT * FROM dirs WHERE depth = 2 AND drive = ? ORDER BY size DESC LIMIT ?",
                (drive, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM dirs WHERE depth = 2 ORDER BY size DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def top_folders_all(self, limit: int = 20) -> list[dict]:
        return self.top_folders(None, limit)

    def cache_dirs(self, min_size: int = 200 * 1024 * 1024, limit: int = 200) -> list[dict]:
        names = (
            "node_modules",
            "__pycache__",
            ".venv",
            ".pytest_cache",
            "target",
            ".next",
            ".nuxt",
            "venv",
            ".tox",
            ".mypy_cache",
            ".ruff_cache",
        )
        clauses = " OR ".join("path LIKE ?" for _ in names)
        params = [f"%\\{name}" for name in names]
        rows = self.conn.execute(
            f"SELECT * FROM dirs WHERE size >= ? AND ({clauses}) ORDER BY size DESC LIMIT ?",
            (int(min_size), *params, int(limit)),
        ).fetchall()
        out = []
        for row in rows:
            rec = dict(row)
            # 只保留最外层的缓存目录（node_modules 里嵌套的 node_modules 不重复列出）
            if any(rec["path"].lower().startswith(o["path"].lower() + "\\") for o in out):
                continue
            out.append(rec)
        return out

    def folder_children(self, parent: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM dirs WHERE parent = ? AND path != parent ORDER BY size DESC LIMIT ?",
            (parent, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def child_dirs(self, parent: str) -> dict[str, int]:
        """直接子目录 {路径: 大小}（不限数量，用于同步）。"""
        rows = self.conn.execute(
            "SELECT path, size FROM dirs WHERE parent = ? AND path != parent", (_dir_key(parent),)
        ).fetchall()
        return {r["path"]: int(r["size"] or 0) for r in rows}

    def has_dir(self, path: str) -> bool:
        return self.conn.execute("SELECT 1 FROM dirs WHERE path = ?", (_dir_key(path),)).fetchone() is not None

    def check_candidates(self, min_dir_size: int, recent_limit: int) -> tuple[list[str], list[tuple[str, str]]]:
        """用于发现「程序没运行时被删掉」的东西：较大的目录，以及大文件 / 最新文件 (路径, 所在目录)。"""
        conn = self.conn
        dirs = [r["path"] for r in conn.execute(
            "SELECT path FROM dirs WHERE size >= ? AND path != parent", (int(min_dir_size),))]
        files = [(r["path"], r["dir"]) for r in conn.execute("SELECT path, dir FROM big_files")]
        files += [(r["path"], r["dir"]) for r in conn.execute(
            "SELECT path, dir FROM recent_files ORDER BY mtime DESC LIMIT ?", (int(recent_limit),))]
        return dirs, files

    def children_totals(self, parents: list[str]) -> dict[str, int]:
        """每个父目录下所有直接子目录的大小之和（用来算「本层文件」占了多少）。"""
        out: dict[str, int] = {}
        for i in range(0, len(parents), 400):
            chunk = parents[i:i + 400]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT parent, COALESCE(SUM(size),0) AS total FROM dirs"
                f" WHERE parent IN ({marks}) AND path != parent GROUP BY parent",
                chunk,
            ).fetchall()
            out.update({r["parent"]: int(r["total"]) for r in rows})
        return out

    def parents_with_children(self, paths: list[str]) -> set[str]:
        out: set[str] = set()
        for i in range(0, len(paths), 400):
            chunk = paths[i:i + 400]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT DISTINCT parent FROM dirs WHERE parent IN ({marks}) AND path != parent", chunk
            ).fetchall()
            out.update(r["parent"] for r in rows)
        return out

    def dirs_under(self, prefix: str, min_size: int, min_depth: int, max_depth: int, limit: int = 500) -> list[dict]:
        prefix = prefix.rstrip("\\") + "\\"
        rows = self.conn.execute(
            "SELECT * FROM dirs WHERE path LIKE ? ESCAPE '|' AND size >= ? AND depth BETWEEN ? AND ?"
            " ORDER BY size DESC LIMIT ?",
            (_like_prefix(prefix), int(min_size), int(min_depth), int(max_depth), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_items(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM cleanup_items ORDER BY size DESC").fetchall()
        return [dict(r) for r in rows]

    def cleanup_objects(self):
        """从数据库重建 CleanItem 列表（启动时恢复上次分析结果）。"""
        from .recommend import CATEGORIES, LEVEL_ORDER, CleanItem

        out = []
        for row in self.cleanup_items():
            out.append(
                CleanItem(
                    category=row["category"],
                    title=row["title"] or os.path.basename(str(row["path"])),
                    path=row["path"],
                    size=int(row["size"] or 0),
                    level=row["level"] or "caution",
                    reason=row["reason"] or "",
                    deletable=bool(row["deletable"]),
                    kind=row["kind"] or "file",
                    action=row["action"] or "",
                )
            )
        cat_index = {name: i for i, name in enumerate(CATEGORIES)}
        out.sort(key=lambda it: (cat_index.get(it.category, 99), LEVEL_ORDER.get(it.level, 9), -it.size))
        return out

    def cleanup_totals(self) -> dict:
        rows = self.conn.execute(
            "SELECT level, COUNT(*) AS n, COALESCE(SUM(size),0) AS total"
            " FROM cleanup_items WHERE deletable = 1 GROUP BY level"
        ).fetchall()
        return {r["level"]: {"count": r["n"], "total": r["total"]} for r in rows}

    # ---------------- 历史 / 监视 ----------------
    def volume_history(self, letter: str | None = None, since: int = 0) -> list[dict]:
        if letter:
            rows = self.conn.execute(
                "SELECT * FROM volume_history WHERE letter = ? AND ts >= ? ORDER BY ts", (letter, since)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM volume_history WHERE ts >= ? ORDER BY letter, ts", (since,)
            ).fetchall()
        return [dict(r) for r in rows]

    def alerts(self, limit: int = 100, since: int = 0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM alerts WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def history_scans(self, limit: int = HISTORY_KEEP_SCANS) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.* FROM scans s WHERE s.partial = 0 AND EXISTS"
            " (SELECT 1 FROM dir_history h WHERE h.scan_id = s.id) ORDER BY s.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def dir_history_map(self, scan_id: int) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT path, drive, depth, size, files FROM dir_history WHERE scan_id = ?", (scan_id,)
        ).fetchall()
        return {r["path"]: dict(r) for r in rows}

    def extend(self, table: str, columns: str, rows: list[tuple]) -> None:
        """通用批量写入（测试与迁移用）。"""
        assert table in {"big_files", "recent_files", "dirs", "volumes", "cleanup_items",
                         "volume_history", "dir_history", "scans", "alerts"}
        with self.lock:
            self.conn.executemany(f"INSERT INTO {table}({columns}) VALUES({','.join('?' * len(rows[0]))})", rows)
            self.conn.commit()


def _like_prefix(prefix: str) -> str:
    return prefix.replace("|", "||").replace("%", "|%").replace("_", "|_") + "%"


def _dir_key(path: str) -> str:
    p = (path or "").rstrip("\\")
    if len(p) == 2 and p[1] == ":":
        return p + "\\"
    return p


def _parent_dir(path: str) -> str:
    """上级目录；盘根没有上级，返回空串。"""
    p = path.rstrip("\\")
    head = p.rpartition("\\")[0]
    return _dir_key(head) if head else ""


def _dir_chain(path: str) -> list[str]:
    """path 自身以及它的所有上级目录，一直到盘根。"""
    out = []
    while path:
        out.append(path)
        path = _parent_dir(path)
    return out


def _subtree_range(root: str) -> tuple[str, str]:
    """root 下所有后代路径的区间 [lo, hi)：'\\' 的下一个字符是 ']'，可以走主键索引做范围查询。"""
    base = root.rstrip("\\") + "\\"
    return base, base[:-1] + "]"


def placeholder_ratio(index: Index) -> float:
    row = index.conn.execute(
        "SELECT COUNT(*) AS p FROM big_files WHERE (flags & ?) != 0", (FLAG_PLACEHOLDER,)
    ).fetchone()
    row2 = index.conn.execute("SELECT COUNT(*) AS n FROM big_files").fetchone()
    return (row["p"] / row2["n"]) if row2["n"] else 0.0
