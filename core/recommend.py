"""清理推荐：已知缓存目录现测 + 索引规则（旧安装包 / 重复文件 / 项目缓存目录 / 日志转储）。"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import scanner, util, volumes
from .index import Index
from .util import GB, MB

LEVEL_TEXT = {"safe": "安全", "caution": "谨慎", "blocked": "不建议"}
LEVEL_ORDER = {"safe": 0, "caution": 1, "blocked": 2}

CATEGORIES = [
    "临时文件",
    "缓存与开发",
    "浏览器缓存",
    "应用缓存",
    "回收站",
    "系统更新",
    "旧安装包",
    "重复文件",
    "项目缓存目录",
    "日志与转储",
    "迁移建议",
    "系统占用（仅说明）",
    "重要数据（勿删）",
]


@dataclass
class CleanItem:
    category: str
    title: str
    path: str
    size: int
    level: str
    reason: str
    deletable: bool
    kind: str            # dir_contents / dir / file / recyclebin / info / migrate
    action: str = ""

    @property
    def level_text(self) -> str:
        return LEVEL_TEXT.get(self.level, self.level)


@dataclass
class _Candidate:
    category: str
    title: str
    path: str
    level: str
    reason: str
    kind: str
    action: str = ""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").rstrip("\\")


def _known_candidates(app_dir: str) -> list[_Candidate]:
    home = _env("USERPROFILE")
    local = _env("LOCALAPPDATA")
    roaming = _env("APPDATA")
    temp = _env("TEMP")
    cands: list[_Candidate] = []
    seen: set[str] = set()

    def add(category, title, path, level, reason, kind, action=""):
        if not path:
            return
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            return
        if norm.startswith(os.path.normcase(os.path.abspath(app_dir))):
            return
        seen.add(norm)
        cands.append(_Candidate(category, title, path, level, reason, kind, action))

    # 临时文件
    add("临时文件", "用户临时目录", temp, "safe",
        "程序运行产生的临时文件，可安全清空；正在被占用的文件会跳过。", "dir_contents")
    add("临时文件", "Windows 临时目录", r"C:\Windows\Temp", "safe",
        "系统临时文件，部分需要管理员权限才能删除。", "dir_contents")
    for entry in _list_dirs(r"C:\Users"):
        if entry.lower() in ("public", "default", "default user", "all users"):
            continue
        add("临时文件", f"用户临时目录（{os.path.basename(entry)}）",
            os.path.join(entry, "AppData", "Local", "Temp"), "safe",
            "该账户的临时文件，可安全清空。", "dir_contents")

    # 缓存与开发
    add("缓存与开发", "pip 下载缓存", os.path.join(local, "pip"), "safe",
        "pip 安装包缓存，删除后下次安装会重新下载，不影响已安装的库。", "dir")
    add("缓存与开发", "用户缓存 ~/.cache", os.path.join(home, ".cache"), "caution",
        "各类工具共用的缓存（模型、字体、下载等），删除后可能首次运行变慢。", "dir")
    add("缓存与开发", "Gradle 构建缓存", os.path.join(home, ".gradle"), "safe",
        "Java 构建缓存，删除后重新构建会自动生成。", "dir")
    add("缓存与开发", "Maven 本地仓库", os.path.join(home, ".m2", "repository"), "caution",
        "Java 依赖仓库，删除后重新构建需要联网重新下载。", "dir")
    add("缓存与开发", "npm 缓存", os.path.join(local, "npm-cache"), "safe",
        "npm 包缓存，可安全删除。", "dir")
    add("缓存与开发", "pnpm 缓存", os.path.join(local, "pnpm-store"), "safe",
        "pnpm 内容寻址缓存，删除后重新安装依赖会变慢。", "dir")
    add("缓存与开发", "Yarn 缓存", os.path.join(local, "Yarn", "Cache"), "safe",
        "Yarn 包缓存，可安全删除。", "dir")
    add("缓存与开发", "conda 包缓存", os.path.join(home, ".conda", "pkgs"), "caution",
        "conda 解压后的包缓存，可用 conda clean 清理。", "dir")
    add("缓存与开发", "NuGet 包缓存", os.path.join(home, ".nuget", "packages"), "caution",
        ".NET 依赖缓存，删除后重新还原会联网下载。", "dir")
    add("缓存与开发", "HuggingFace 模型缓存", os.path.join(home, ".cache", "huggingface"), "caution",
        "下载的模型权重，删除后需要重新下载（通常很大）。", "dir")
    add("缓存与开发", "Cargo 注册表缓存", os.path.join(home, ".cargo", "registry"), "caution",
        "Rust 依赖缓存，删除后重新编译需要联网。", "dir")

    # 浏览器缓存
    for browser, base in (
        ("Edge", os.path.join(local, "Microsoft", "Edge", "User Data")),
        ("Chrome", os.path.join(local, "Google", "Chrome", "User Data")),
        ("Brave", os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data")),
    ):
        for profile in ("Default", "Profile 1", "Profile 2"):
            root = os.path.join(base, profile)
            if not os.path.isdir(root):
                continue
            for sub, label in (("Cache", "缓存"), ("Code Cache", "代码缓存"), ("GPUCache", "GPU 缓存")):
                add("浏览器缓存", f"{browser} {profile} {label}", os.path.join(root, sub), "caution",
                    "浏览器缓存，删除后首次打开网页会稍慢，登录状态不受影响。", "dir_contents")
    for path in _glob_dirs(os.path.join(local, "Mozilla", "Firefox", "Profiles", "*", "cache2")):
        add("浏览器缓存", f"Firefox 缓存（{os.path.basename(os.path.dirname(path))}）", path, "caution",
            "Firefox 网页缓存，可安全清空。", "dir_contents")

    # 应用缓存
    add("应用缓存", "微信文件缓存", os.path.join(home, "Documents", "WeChat Files"), "caution",
        "微信聊天文件与图片，删除会丢失聊天中的媒体文件，请先在微信里确认。", "dir")
    add("应用缓存", "微信文件缓存（新）", os.path.join(home, "Documents", "xwechat_files"), "caution",
        "新版微信聊天文件，删除会丢失聊天媒体文件。", "dir")
    for path in (r"D:\WeChat", r"D:\xwechat_files", r"D:\Weixin", os.path.join(home, "Documents", "Tencent Files")):
        add("应用缓存", f"聊天软件文件（{os.path.basename(path)}）", path, "caution",
            "聊天软件保存的文件与媒体，删除前请确认是否需要保留。", "dir")
    add("应用缓存", "飞书缓存", os.path.join(local, "Feishu"), "caution",
        "飞书本地缓存，删除后首次打开会重新加载。", "dir")
    add("应用缓存", "钉钉缓存", os.path.join(local, "DingTalk"), "caution",
        "钉钉本地缓存，可清理。", "dir")

    # 回收站
    for path in ("C:\\$Recycle.Bin", "D:\\$RECYCLE.Bin"):
        add("回收站", "回收站", path, "safe",
            "已删除文件的暂存区，清空后无法恢复。", "recyclebin", "清空回收站")

    # 系统更新
    add("系统更新", "Windows 更新下载缓存", r"C:\Windows\SoftwareDistribution\Download", "caution",
        "Windows 更新安装包缓存，建议通过「磁盘清理」清理；直接删除可能失败。", "dir_contents")
    add("系统更新", "传递优化缓存", os.path.join(_env("ProgramData"), "Microsoft", "Windows", "DeliveryOptimization"),
        "caution", "Windows 更新分发缓存，可通过系统设置清理。", "dir_contents")

    # 系统占用说明
    for path, title in (
        ("C:\\pagefile.sys", "虚拟内存页面文件"),
        ("C:\\hiberfil.sys", "休眠文件"),
        ("C:\\swapfile.sys", "交换文件"),
        (r"C:\Windows\WinSxS", "WinSxS 组件存储"),
        (r"C:\Windows\MEMORY.DMP", "内存转储文件"),
        (r"C:\Windows\Minidump", "小型转储文件"),
    ):
        if os.path.exists(path):
            add("系统占用（仅说明）", title, path, "blocked",
                "系统关键文件，请不要手动删除；需要清理请使用系统自带工具。", "info")

    return cands


def _list_dirs(root: str) -> list[str]:
    try:
        with os.scandir(util.long_path(root)) as it:
            return [e.path for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _glob_dirs(pattern: str) -> list[str]:
    import glob

    return [p for p in glob.glob(pattern) if os.path.isdir(p)]


def _measure(cands: list[_Candidate], cancel: threading.Event | None, on_progress) -> dict[str, tuple[int, int]]:
    results: dict[str, tuple[int, int]] = {}
    counter = {"done": 0}
    counter_lock = threading.Lock()

    def job(cand: _Candidate):
        if cancel is not None and cancel.is_set():
            return cand.path, (0, 0)
        size, count = scanner.measure_dir(cand.path, cancel)
        with counter_lock:
            counter["done"] += 1
            if on_progress and counter["done"] % 3 == 0:
                on_progress(f"正在测量：{cand.title}")
        return cand.path, (size, count)

    with ThreadPoolExecutor(max_workers=6) as pool:
        for path, result in pool.map(job, cands):
            results[path] = result
    return results


def _hash_file(path: str, limit: int | None = None) -> str | None:
    h = hashlib.sha256()
    remaining = limit if limit else None
    try:
        with open(util.long_path(path), "rb") as fh:
            while True:
                if remaining is not None and remaining <= 0:
                    break
                chunk_size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _duplicates(index: Index, cancel, on_progress) -> list[CleanItem]:
    rows, _total, _size = index.big_files(min_size=100 * MB, page=0, page_size=5000)
    groups: dict[int, list[dict]] = {}
    for row in rows:
        if row["flags"] & util.FLAG_PLACEHOLDER:
            continue
        groups.setdefault(int(row["size"]), []).append(row)
    candidates = [g for g in groups.values() if len(g) > 1]
    if not candidates:
        return []
    if on_progress:
        on_progress("正在检测重复文件（先比大小，再比内容）…")
    items: list[CleanItem] = []
    hashed = 0
    hashed_bytes = 0
    max_head_files = 3000
    max_hash_bytes = 2 * GB
    for group in candidates:
        if cancel is not None and cancel.is_set():
            break
        head: dict[str, list[dict]] = {}
        for row in group:
            if hashed >= max_head_files:
                break
            digest = _hash_file(row["path"], 64 * 1024)
            hashed += 1
            if not digest:
                continue
            head.setdefault(digest, []).append(row)
        for rows2 in head.values():
            if len(rows2) < 2:
                continue
            full: dict[str, list[dict]] = {}
            for row in rows2:
                if cancel is not None and cancel.is_set():
                    break
                if hashed_bytes >= max_hash_bytes:
                    break
                hashed_bytes += int(row["size"])
                digest = _hash_file(row["path"])
                if not digest:
                    continue
                full.setdefault(digest, []).append(row)
            for rows3 in full.values():
                if len(rows3) < 2:
                    continue
                rows3.sort(key=lambda r: r["mtime"])
                keeper = rows3[0]
                for row in rows3[1:]:
                    items.append(
                        CleanItem(
                            category="重复文件",
                            title=row["name"],
                            path=row["path"],
                            size=int(row["size"]),
                            level="caution",
                            reason=f"与 {keeper['path']} 内容完全相同（保留修改时间最早的一份）",
                            deletable=True,
                            kind="file",
                            action="删除副本",
                        )
                    )
    return items


def analyze(
    index: Index,
    app_dir: str,
    cancel: threading.Event | None = None,
    on_progress=None,
    include_duplicates: bool = True,
) -> list[CleanItem]:
    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    progress("正在测量缓存目录…")
    cands = _known_candidates(app_dir)
    measured = _measure([c for c in cands if c.kind != "info"], cancel, on_progress)
    items: list[CleanItem] = []
    seen: set[str] = set()

    def add_item(item: CleanItem) -> None:
        key = os.path.normcase(os.path.abspath(item.path)) if item.path else item.title
        if key in seen:
            return
        seen.add(key)
        items.append(item)

    for cand in cands:
        size, _count = measured.get(cand.path, (0, 0))
        if cand.kind == "recyclebin":
            size = volumes.recycle_bin_size(util.drive_of(cand.path)) or size
        if cand.kind == "info":
            if size == 0:
                # 用索引里的目录大小补充说明项
                size = _index_size(index, cand.path)
            if size <= 0:
                continue
            add_item(
                CleanItem(cand.category, cand.title, cand.path, size, cand.level,
                          cand.reason, False, cand.kind, cand.action)
            )
            continue
        if size <= 0:
            continue
        add_item(
            CleanItem(cand.category, cand.title, cand.path, size, cand.level,
                      cand.reason, cand.level != "blocked", cand.kind, cand.action)
        )

    progress("正在整理大文件规则…")
    big_rows, _n, _s = index.big_files(page=0, page_size=20000)
    now = time.time()
    installer_exts = {".exe", ".msi", ".zip", ".7z", ".rar", ".iso", ".img", ".bin"}
    log_exts = {".log", ".dmp", ".etl", ".tmp", ".old"}
    for row in big_rows:
        if cancel is not None and cancel.is_set():
            break
        flags = int(row["flags"])
        if flags & util.FLAG_SYSTEM or flags & util.FLAG_PLACEHOLDER:
            continue
        ext = row["ext"] or ""
        size = int(row["size"])
        mtime = int(row["mtime"] or 0)
        if ext in installer_exts and size >= 200 * MB and mtime and now - mtime > 90 * 86400:
            add_item(
                CleanItem("旧安装包", row["name"], row["path"], size, "caution",
                          f"超过 90 天未修改的压缩包 / 安装包 / 镜像（{util.fmt_date(mtime)}）；"
                          "如果里面的数据还有用请保留。",
                          True, "file", "确认不需要后可删除")
            )
        elif ext in log_exts and size >= 100 * MB:
            add_item(
                CleanItem("日志与转储", row["name"], row["path"], size, "safe",
                          "日志或崩溃转储文件，删除后不影响程序运行。", True, "file", "可删除")
            )

    progress("正在查找项目缓存目录…")
    for row in index.cache_dirs(min_size=200 * MB):
        if cancel is not None and cancel.is_set():
            break
        name = os.path.basename(str(row["path"]).rstrip("\\"))
        add_item(
            CleanItem("项目缓存目录", name, row["path"], int(row["size"]), "caution",
                      f"{name} 是项目依赖/构建产物，删除后需要重新安装或构建。", True, "dir", "可删除")
        )

    if include_duplicates:
        for item in _duplicates(index, cancel, on_progress):
            add_item(item)

    progress("正在查找适合迁移到其它盘的大目录…")
    try:
        from . import migrate

        for row in migrate.suggest(index):
            add_item(
                CleanItem("迁移建议", os.path.basename(row["path"]), row["path"], int(row["size"]), "caution",
                          f"C 盘用户目录里的大目录（{row['files']:,} 个文件）。迁移到其它盘后在原位置建目录联接，"
                          "程序照常使用原路径，C 盘可腾出约这么多空间。迁移前请关闭相关程序。",
                          False, "migrate", "迁移到其它盘")
            )
    except Exception:  # noqa: BLE001 - 迁移建议失败不影响其它清理项
        pass

    progress("正在整理重要数据提醒…")
    for item in _important_folders(index):
        add_item(item)

    # 排序：类别顺序 -> 等级 -> 大小
    cat_index = {name: i for i, name in enumerate(CATEGORIES)}
    items.sort(key=lambda it: (cat_index.get(it.category, 99), LEVEL_ORDER.get(it.level, 9), -it.size))
    return items


def _index_size(index: Index, path: str) -> int:
    try:
        with index.lock:
            row = index.conn.execute("SELECT size FROM dirs WHERE path = ?", (path,)).fetchone()
        if row:
            return int(row["size"])
        row = index.conn.execute(
            "SELECT size FROM big_files WHERE path = ?", (path,)
        ).fetchone()
        return int(row["size"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _important_folders(index: Index) -> list[CleanItem]:
    out: list[CleanItem] = []
    cache_names = {
        "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build",
        ".cache", "cache", "temp", "tmp", "appdata", "program files", "windows",
        "users", "programdata", "recovery", "perflogs", "$recycle.bin",
    }
    for row in index.top_folders_all(limit=15):
        name = os.path.basename(str(row["path"]).rstrip("\\")).lower()
        if name in cache_names:
            continue
        if int(row["size"]) < 5 * GB:
            continue
        out.append(
            CleanItem("重要数据（勿删）", os.path.basename(str(row["path"]).rstrip("\\")),
                      row["path"], int(row["size"]), "blocked",
                      "看起来是你的项目或数据目录，建议备份 / 迁移到其它磁盘，而不是删除。",
                      False, "info", "建议备份")
        )
    return out


def summarize(items: list[CleanItem]) -> dict:
    totals = {"safe": 0, "caution": 0, "blocked": 0}
    counts = {"safe": 0, "caution": 0, "blocked": 0}
    for item in items:
        if not item.deletable:
            continue
        totals[item.level] = totals.get(item.level, 0) + item.size
        counts[item.level] = counts.get(item.level, 0) + 1
    return {"totals": totals, "counts": counts, "reclaimable": totals["safe"] + totals["caution"]}
