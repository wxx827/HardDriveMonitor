"""磁盘监视：定时采样剩余空间、空间告警、目录增长对比、空闲检测与开机自启。"""
from __future__ import annotations

import ctypes
import os
import sqlite3
import sys
import time
from dataclasses import dataclass

from . import util, volumes
from .index import Index
from .util import GB

ALERT_COOLDOWN = 6 * 3600
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "HardDriveMonitor"


@dataclass
class Alert:
    letter: str
    kind: str        # low / drop
    message: str


def low_space_alert(letter: str, total: int, free: int, low_pct: float, low_gb: float) -> Alert | None:
    """剩余低于百分比阈值或绝对值阈值（任一）时告警。"""
    if total <= 0:
        return None
    pct = free / total * 100.0
    if (low_pct and pct < low_pct) or (low_gb and free < low_gb * GB):
        return Alert(letter, "low", f"{letter} 剩余空间只有 {util.human_size(free)}（{pct:.0f}%），建议尽快清理。")
    return None


def drop_alert(letter: str, history: list[tuple[int, int]], now: float, window_s: int, drop_bytes: int) -> Alert | None:
    """history: [(ts, free)]，窗口内剩余空间从峰值下降超过 drop_bytes 时告警。"""
    recent = [(ts, free) for ts, free in history if ts >= now - window_s]
    if len(recent) < 2 or drop_bytes <= 0:
        return None
    recent.sort()
    peak = max(free for _ts, free in recent)
    current = recent[-1][1]
    if peak - current >= drop_bytes:
        minutes = max(1, int(window_s / 60))
        return Alert(
            letter, "drop",
            f"{letter} 在 {minutes} 分钟内减少了 {util.human_size(peak - current)}，"
            f"当前剩余 {util.human_size(current)}。可以到「监视」页看看是哪个目录在膨胀。",
        )
    return None


def recently_alerted(index: Index, letter: str, kind: str, now: float, cooldown: int = ALERT_COOLDOWN) -> bool:
    row = index.conn.execute(
        "SELECT 1 FROM alerts WHERE letter = ? AND kind = ? AND ts >= ? LIMIT 1",
        (letter, kind, int(now - cooldown)),
    ).fetchone()
    return row is not None


class Sampler:
    def __init__(self, index: Index, config):
        self.index = index
        self.config = config

    def sample(self, vols: list | None = None, now: float | None = None) -> list[Alert]:
        now = float(now or time.time())
        vols = vols if vols is not None else volumes.list_volumes()
        cfg = self.config.monitor
        window = int(float(cfg.get("drop_window_min") or 60) * 60)
        drop = int(float(cfg.get("drop_gb") or 0) * GB)
        out: list[Alert] = []
        for vol in vols:
            try:
                self.index.add_volume_sample(vol.letter, vol.total, vol.free, int(now))
            except sqlite3.OperationalError as exc:
                # 扫描子进程正在写入大事务时数据库被锁住：这次采样跳过，下个周期再记
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    return out
                raise
            history = [
                (row["ts"], row["free"])
                for row in self.index.volume_history(vol.letter, since=int(now - window - 60))
            ]
            candidates = (
                low_space_alert(vol.letter, vol.total, vol.free,
                                float(cfg.get("low_pct") or 0), float(cfg.get("low_gb") or 0)),
                drop_alert(vol.letter, history, now, window, drop),
            )
            for alert in candidates:
                if alert and not recently_alerted(self.index, alert.letter, alert.kind, now):
                    self.index.add_alert(alert.letter, alert.kind, alert.message, int(now))
                    out.append(alert)
        return out


def change_since(index: Index, letter: str, seconds: int, now: float | None = None) -> int | None:
    """返回 seconds 之前到现在剩余空间的变化（正数 = 剩余变多）；数据不足返回 None。"""
    now = float(now or time.time())
    rows = index.volume_history(letter, since=int(now - seconds))
    if len(rows) < 2:
        return None
    return int(rows[-1]["free"]) - int(rows[0]["free"])


def growth(index: Index, limit: int = 50) -> dict:
    """对比最近两次完整扫描，返回增长最多的目录。"""
    scans = index.history_scans()
    result = {"rows": [], "before": None, "after": None}
    if len(scans) < 2:
        return result
    newest = scans[0]
    new_map = index.dir_history_map(newest["id"])
    new_drives = {rec["drive"] for rec in new_map.values()}
    older = None
    old_map: dict = {}
    for scan in scans[1:]:
        candidate = index.dir_history_map(scan["id"])
        if new_drives & {rec["drive"] for rec in candidate.values()}:
            older, old_map = scan, candidate
            break
    if older is None:
        return result
    old_drives = {rec["drive"] for rec in old_map.values()}
    rows = []
    for path, rec in new_map.items():
        if rec["drive"] not in old_drives:
            continue
        before = int(old_map.get(path, {}).get("size", 0))
        delta = int(rec["size"]) - before
        if delta <= 0:
            continue
        rows.append({
            "path": path, "drive": rec["drive"], "depth": rec["depth"],
            "before": before, "after": int(rec["size"]), "delta": delta, "new": path not in old_map,
        })
    # 父目录的增长如果主要来自某个子目录，就只列子目录，避免同一份增长被列很多次
    rows.sort(key=lambda r: -r["depth"])
    kept: list[dict] = []
    for row in rows:
        prefix = row["path"].rstrip("\\").lower() + "\\"
        explained = sum(k["delta"] for k in kept if k["path"].lower().startswith(prefix))
        if explained >= row["delta"] * 0.8:
            continue
        kept.append(row)
    kept.sort(key=lambda r: -r["delta"])
    result.update(rows=kept[:limit], before=older["finished"], after=newest["finished"])
    return result


def scan_due(last_scan: int, hours: float, now: float | None = None) -> bool:
    if hours <= 0:
        return False
    now = float(now or time.time())
    return not last_scan or now - last_scan >= hours * 3600


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """距离上次键盘 / 鼠标输入的秒数（用于在空闲时自动扫描）。"""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except (AttributeError, OSError):
        return 0.0


def autostart_command(app_dir: str) -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.join(app_dir, "app.py")}" --minimized'


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool, command: str) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
