"""配置读写：data/config.json，API Key 用 DPAPI 加密保存。"""
from __future__ import annotations

import copy
import json
import os
import threading

from . import secrets
from .util import MB, GB

CONFIG_VERSION = 2

DEFAULT_CONFIG: dict = {
    "version": CONFIG_VERSION,
    "ai": {
        "provider": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_enc": "",
        "api_key_plain": "",
        "temperature": 0.3,
        "timeout": 120,
    },
    "privacy": {
        "mask_user": True,
        "send_snippet": False,
        "show_full_path": True,
        "privacy_notice_seen": False,
    },
    "scan": {
        "drives": ["C:", "D:"],
        "min_big_size": 100 * MB,
        "top_k": 20000,
        "max_depth": 0,
        "recent_days": 180,
        "recent_cap": 200000,
        "threads": 0,             # 0 = 自动（CPU 核数，最多 8 个进程）
        "method": "auto",         # auto：NTFS + 管理员时直读 MFT，否则多进程遍历
        "auto_scan": True,
        "auto_scan_hours": 24,
        "watch": True,            # 监听文件增删，几秒内同步到索引
    },
    "monitor": {
        "sample_minutes": 10,
        "low_pct": 10,
        "low_gb": 20,
        "drop_gb": 5,
        "drop_window_min": 60,
        "notify": True,
    },
    "app": {
        "load_cache": True,
        "window_geometry": "",
        "close_to_tray": True,
        "tray_hint_shown": False,
        "autostart": False,
        "migrate_root": "D:\\已迁移",
    },
    "delete": {
        "permanent_temp": False,
    },
}

PROVIDER_PRESETS = [
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-32k"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("硅基流动", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("OpenRouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat"),
    ("Ollama 本地", "http://127.0.0.1:11434/v1", "qwen2.5:7b"),
    ("LM Studio 本地", "http://127.0.0.1:1234/v1", ""),
    ("自定义", "", ""),
]


def normalize_base_url(url: str) -> str:
    """去掉用户误粘贴的 /chat/completions 等后缀，补全协议头。"""
    url = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    if url and "://" not in url:
        url = ("http://" if is_local_url("http://" + url) else "https://") + url
    return url


def is_local_url(url: str) -> bool:
    """本机或局域网地址（Ollama / LM Studio 等），这类服务一般不需要 API Key。"""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url if "://" in url else "http://" + url).hostname or ""
    except ValueError:
        return False
    if host in ("localhost", ""):
        return bool(host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.data: dict = copy.deepcopy(DEFAULT_CONFIG)
        self.key_warning: str = ""
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if not isinstance(raw, dict):
                    raise ValueError("配置根节点不是对象")
                self.data = _deep_merge(DEFAULT_CONFIG, raw)
                if int(raw.get("version") or 1) < 2:
                    # v1 固定 4 线程；v2 改为多进程，默认自动
                    self.data["scan"]["threads"] = 0
                self.data["version"] = CONFIG_VERSION
            except (OSError, ValueError, json.JSONDecodeError):
                self.data = copy.deepcopy(DEFAULT_CONFIG)
                return
            enc = self.data.get("ai", {}).get("api_key_enc", "")
            if enc:
                try:
                    self.api_key = secrets.unprotect(enc)
                except (OSError, ValueError):
                    self.api_key = ""
                    self.key_warning = "已保存的 API Key 无法解密（可能换了 Windows 账户），请重新填写。"
            elif self.data.get("ai", {}).get("api_key_plain"):
                self.api_key = self.data["ai"]["api_key_plain"]
                self.key_warning = "API Key 以明文保存（DPAPI 不可用），建议重新保存。"

    def save(self) -> None:
        with self._lock:
            ai = self.data.setdefault("ai", {})
            key = (self.api_key or "").strip()
            ai["api_key_enc"] = ""
            ai["api_key_plain"] = ""
            if key:
                try:
                    ai["api_key_enc"] = secrets.protect(key)
                except (OSError, ValueError):
                    ai["api_key_plain"] = key
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ---------- 便捷访问 ----------
    @property
    def api_key(self) -> str:
        return getattr(self, "_api_key", "")

    @api_key.setter
    def api_key(self, value: str) -> None:
        self._api_key = value or ""

    @property
    def ai(self) -> dict:
        return self.data.setdefault("ai", {})

    @property
    def privacy(self) -> dict:
        return self.data.setdefault("privacy", {})

    @property
    def scan(self) -> dict:
        return self.data.setdefault("scan", {})

    @property
    def app(self) -> dict:
        return self.data.setdefault("app", {})

    @property
    def monitor(self) -> dict:
        return self.data.setdefault("monitor", {})

    @property
    def delete(self) -> dict:
        return self.data.setdefault("delete", {})

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def ai_ready(self) -> bool:
        base = self.ai.get("base_url") or ""
        if not (base and self.ai.get("model")):
            return False
        return bool(self.api_key) or is_local_url(base)

    def masked_key(self) -> str:
        key = self.api_key or ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "*" * 8 + key[-4:]
