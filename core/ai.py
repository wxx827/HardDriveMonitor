"""AI 分析：离线规则引擎（默认）+ OpenAI 兼容接口流式分析（可选）。"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import util
from .config import Config, is_local_url, normalize_base_url
from .index import Index
from .util import GB, MB

SYSTEM_PROMPT = """你是一个 Windows 磁盘空间顾问，服务对象是普通电脑用户（中文回答）。
规则：
1. 只依据用户提供的扫描数据（路径、大小、修改时间、目录结构）推理，不要编造不存在的文件。
2. 给出具体、可执行的建议：清理什么、大概能释放多少空间、有什么风险、怎么做。
3. 明确区分「可以安全删除」「删除前要确认」「绝对不要删除（系统/项目数据）」。
4. 不确定时直接说「不确定」，并给出确认方法。
5. 不要建议删除 C:\\Windows、Program Files、用户项目数据、正在使用的程序文件。
6. 回答用 Markdown，简洁分点，避免空话。"""


@dataclass
class ChatMessage:
    role: str
    content: str


def _fmt_size(n: int | float) -> str:
    return util.human_size(n)


def build_context(
    index: Index,
    volumes: list,
    cleanup_items: list | None = None,
    mask: bool = True,
    max_files: int = 40,
    max_folders: int = 20,
) -> dict:
    """收集给 AI（或离线引擎）的结构化上下文；默认对用户名脱敏。"""
    username = os.environ.get("USERNAME") or ""

    def mask_path(path: str) -> str:
        return util.mask_user(path, username) if mask else path

    drive_data = []
    for vol in volumes:
        drive_data.append(
            {
                "盘符": vol.letter,
                "卷标": vol.label,
                "总计GB": round(vol.total / GB, 1),
                "已用GB": round(vol.used / GB, 1),
                "剩余GB": round(vol.free / GB, 1),
                "已用百分比": round(vol.used_pct, 1),
            }
        )

    big_rows, _n, _sum = index.big_files(page=0, page_size=max_files, include_system=True)
    top_files = [
        {
            "路径": mask_path(row["path"]),
            "大小MB": round(row["size"] / MB, 1),
            "修改日期": util.fmt_date(row["mtime"]),
            "扩展名": row["ext"] or "无",
            "标记": _flags_text(row["flags"]),
        }
        for row in big_rows
    ]

    folders = index.top_folders_all(limit=max_folders)
    top_folders = [
        {
            "目录": mask_path(row["path"]),
            "大小GB": round(row["size"] / GB, 2),
            "文件数": row["files"],
        }
        for row in folders
    ]

    cleanup = []
    for item in cleanup_items or []:
        cleanup.append(
            {
                "类别": item.category,
                "项目": item.title,
                "路径": mask_path(item.path),
                "大小GB": round(item.size / GB, 2),
                "等级": item.level_text,
                "可删除": item.deletable,
                "说明": item.reason,
            }
        )

    counts = index.counts()
    try:
        recent_rows, recent_n, recent_total = index.recent_files(days=7, page=0, page_size=1)
    except Exception:  # noqa: BLE001
        recent_n, recent_total = 0, 0
    return {
        "磁盘": drive_data,
        "大文件Top": top_files,
        "占用最大的文件夹": top_folders,
        "清理候选": cleanup,
        "索引统计": counts,
        "最近7天": {"文件数": recent_n, "合计GB": round(recent_total / GB, 2)},
        "扫描时间": util.fmt_time(index.last_scan_time() or None),
    }


def _flags_text(flags: int) -> str:
    parts = []
    if flags & util.FLAG_SYSTEM:
        parts.append("系统目录")
    if flags & util.FLAG_PLACEHOLDER:
        parts.append("云端占位")
    if flags & util.FLAG_HARDLINK:
        parts.append("硬链接")
    if flags & util.FLAG_CACHE:
        parts.append("缓存/临时")
    return "、".join(parts) or "普通"


# --------------------------------------------------------------------------
# 离线规则引擎
# --------------------------------------------------------------------------

EXT_KIND = {
    ".mp4": "视频", ".mkv": "视频", ".avi": "视频", ".mov": "视频", ".flv": "视频", ".wmv": "视频",
    ".iso": "光盘镜像", ".img": "磁盘镜像",
    ".zip": "压缩包", ".7z": "压缩包", ".rar": "压缩包", ".tar": "压缩包", ".gz": "压缩包",
    ".exe": "安装程序", ".msi": "安装程序",
    ".dmp": "崩溃转储", ".log": "日志", ".etl": "事件跟踪日志",
    ".pt": "模型权重", ".pth": "模型权重", ".ckpt": "模型权重", ".safetensors": "模型权重",
    ".onnx": "模型文件", ".gguf": "模型文件", ".h5": "模型/数据", ".npz": "数据集", ".parquet": "数据集",
    ".csv": "表格数据", ".xlsx": "表格", ".docx": "文档", ".pptx": "演示文稿", ".pdf": "文档",
    ".psd": "设计源文件", ".ai": "设计源文件", ".blend": "3D 工程", ".db": "数据库", ".sqlite": "数据库",
    ".vmdk": "虚拟机磁盘", ".vdi": "虚拟机磁盘", ".vhd": "虚拟机磁盘", ".vhdx": "虚拟机磁盘",
}

KEEP_EXT = {".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".gguf", ".parquet", ".npz", ".psd", ".ai", ".blend"}


def file_advice(path: str, size: int, mtime: float, ext: str = "") -> dict:
    ext = (ext or os.path.splitext(path)[1]).lower()
    kind = EXT_KIND.get(ext, "普通文件")
    name = os.path.basename(path)
    location = os.path.dirname(path)
    low = path.lower()
    age_days = max(0, int((util.now_ts() - mtime) / 86400)) if mtime else 0

    safe = False
    advice = "建议保留，先确认是否还需要。"
    if ext in KEEP_EXT:
        advice = "这类文件通常是模型权重或工程文件，删了要重新下载/重做，建议保留。"
    elif ext in (".dmp", ".log", ".etl", ".tmp"):
        safe = True
        advice = "日志/转储文件，一般可以删除。"
    elif ext in (".iso", ".img"):
        advice = "系统镜像/磁盘镜像，如果安装盘已经做好，通常可以删除。"
    elif ext in (".zip", ".7z", ".rar", ".tar", ".gz"):
        advice = "压缩包，确认解压后的内容已保留，就可以删除。"
    elif ext in (".exe", ".msi"):
        if age_days > 90:
            advice = "安装包，且已超过 90 天未修改，安装完成后一般不再需要。"
        else:
            advice = "安装包，近期可能还在用，删除前确认软件已经装好。"
    elif "\\temp\\" in low or "\\tmp\\" in low or "\\cache\\" in low:
        safe = True
        advice = "临时/缓存文件，可以删除。"
    elif ext == ".vmdk" or ext == ".vdi" or ext == ".vhd" or ext == ".vhdx":
        advice = "虚拟机磁盘文件，删除会丢失整台虚拟机，请务必确认。"
    elif ext in (".mp4", ".mkv", ".avi", ".mov"):
        advice = "视频文件，占用大但对空间不敏感，建议迁移到其他磁盘或移动硬盘。"

    if "\\node_modules\\" in low:
        advice = "node_modules 是依赖目录，删除后重新 npm install 即可恢复。"
    if "\\windows\\" in low or "\\program files" in low:
        advice = "位于系统/程序目录，不能手动删除。"
        safe = False

    return {
        "名称": name,
        "位置": location,
        "类型": kind,
        "大小": util.human_size(size),
        "修改时间": util.fmt_date(mtime),
        "判断": advice,
        "建议": "可删除" if safe else ("谨慎处理" if ext not in KEEP_EXT else "保留"),
    }


def offline_report(context: dict, selection: list[dict] | None = None) -> str:
    lines: list[str] = []
    lines.append("# 磁盘分析报告（离线规则）")
    lines.append("")
    lines.append("## 一、磁盘现状")
    for d in context.get("磁盘", []):
        pct = float(d.get("已用百分比") or 0)
        if pct >= 92:
            state = "空间告急，需要尽快清理"
        elif pct >= 85:
            state = "空间紧张，建议清理"
        else:
            state = "空间充足"
        lines.append(
            f"- **{d['盘符']}** {d['总计GB']} GB，已用 {d['已用GB']} GB（{pct}%），"
            f"剩余 {d['剩余GB']} GB —— {state}。"
        )

    cleanup = context.get("清理候选", [])
    reclaim = sum(float(c["大小GB"]) for c in cleanup if c.get("可删除"))
    lines.append("")
    lines.append("## 二、可以释放的空间")
    lines.append(f"共找到 **{len(cleanup)}** 项清理候选，其中可删除项合计约 **{reclaim:.1f} GB**。")

    safe = [c for c in cleanup if c.get("等级") == "安全"]
    caution = [c for c in cleanup if c.get("等级") == "谨慎"]
    if safe:
        lines.append("")
        lines.append("### 安全项（可以直接移到回收站）")
        for c in sorted(safe, key=lambda x: -x["大小GB"])[:10]:
            lines.append(f"- {c['项目']}：**{c['大小GB']:.2f} GB** —— {c['说明']}")
    if caution:
        lines.append("")
        lines.append("### 谨慎项（确认后再删）")
        for c in sorted(caution, key=lambda x: -x["大小GB"])[:10]:
            lines.append(f"- {c['项目']}：**{c['大小GB']:.2f} GB** —— {c['说明']}")

    if selection:
        lines.append("")
        lines.append("## 三、选中项分析")
        for item in selection:
            advice = file_advice(
                item.get("path", ""),
                int(item.get("size") or 0),
                float(item.get("mtime") or 0),
                item.get("ext") or "",
            )
            lines.append(f"### {advice['名称']}")
            lines.append(f"- 位置：`{advice['位置']}`")
            lines.append(f"- 大小：{advice['大小']}；类型：{advice['类型']}；修改时间：{advice['修改时间']}")
            lines.append(f"- 判断：{advice['判断']}")
            lines.append(f"- 结论：**{advice['建议']}**")

    lines.append("")
    lines.append("## 四、大文件解读")
    for f in context.get("大文件Top", [])[:12]:
        ext = f.get("扩展名") or ""
        kind = EXT_KIND.get(ext.lower(), "普通文件")
        lines.append(f"- `{f['路径']}` —— {f['大小MB']} MB，{kind}，修改于 {f['修改日期']}。")
    if not context.get("大文件Top"):
        lines.append("- 还没有扫描结果，先点「完整扫描」再分析。")

    blocked = [c for c in cleanup if not c.get("可删除") and c.get("大小GB", 0) > 0]
    if blocked:
        lines.append("")
        lines.append("## 五、不建议删除")
        for c in sorted(blocked, key=lambda x: -x["大小GB"])[:10]:
            lines.append(f"- {c['项目']}（{c['大小GB']:.2f} GB）：{c['说明']}")

    lines.append("")
    lines.append("## 六、下一步怎么做")
    lines.append("1. 到「清理推荐」页，勾选「安全」项，点「移到回收站」。")
    lines.append("2. 对大文件逐个确认：项目数据建议备份或迁移到 D 盘/移动硬盘，而不是删除。")
    lines.append("3. 清理完成后再看一次 C 盘剩余空间；如果仍然紧张，优先迁移视频、模型、虚拟机等大文件。")
    lines.append("")
    lines.append("> 以上为本地规则分析。到「设置」里填写 OpenAI 兼容的 API Key 后，可获得更灵活的 AI 分析。")
    return "\n".join(lines)


def offline_chat_answer(question: str, context: dict) -> str:
    q = (question or "").strip()
    if not q:
        return "请输入你想问的问题，例如「C 盘为什么满了」「这个文件能删吗」。"
    low = q.lower()
    if "盘" in q and ("满" in q or "空" in q or "大" in q):
        return offline_report(context)
    if "删" in q or "清理" in q:
        lines = ["下面是根据当前扫描数据整理的清理建议：", ""]
        for c in sorted(context.get("清理候选", []), key=lambda x: -x["大小GB"])[:10]:
            flag = "✅ 可删" if c.get("可删除") else "⛔ 勿删"
            lines.append(f"- {flag} **{c['项目']}**（{c['大小GB']:.2f} GB）：{c['说明']}")
        if len(lines) == 2:
            lines.append("还没有清理分析结果，先去「清理推荐」页点一次「分析」。")
        return "\n".join(lines)
    if "文件" in q or "这个" in q:
        return (
            "离线模式只能基于扫描到的元数据判断。你可以在「大文件」或「最近文件」页选中一个文件，"
            "右键 →「AI 分析此项」，我会给出这个文件的用途和删除建议。"
        )
    return (
        "离线规则引擎只支持有限的问答。你可以：\n"
        "- 点「分析整体」查看完整报告；\n"
        "  或在「设置」里填写 API Key，启用更灵活的 AI 分析。"
    )


# --------------------------------------------------------------------------
# 在线（OpenAI 兼容）接口
# --------------------------------------------------------------------------


MAX_CONTEXT_CHARS = 16000
RETRY_CODES = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}
DONE = "[DONE]"
_VERSION_RE = re.compile(r"/v\d+[a-z]*$", re.I)
_TRIM_KEYS = ("大文件Top", "清理候选", "占用最大的文件夹", "选中项")


@dataclass
class ApiSettings:
    base_url: str
    model: str
    api_key: str = ""
    temperature: float | None = 0.3
    timeout: int = 120

    @classmethod
    def from_config(cls, config: Config) -> "ApiSettings":
        temp = config.ai.get("temperature")
        return cls(
            base_url=config.ai.get("base_url") or "",
            model=config.ai.get("model") or "",
            api_key=config.api_key,
            temperature=0.3 if temp is None else float(temp),
            timeout=int(config.ai.get("timeout") or 120),
        )


class ApiError(RuntimeError):
    def __init__(self, message: str, code: int = 0, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _base_candidates(base_url: str) -> list[str]:
    base = normalize_base_url(base_url)
    if not base:
        raise ApiError("还没有填写 API Base URL")
    out = [base]
    if not _VERSION_RE.search(base):
        out.append(base + "/v1")
    return out


def _headers(settings: ApiSettings, stream: bool) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "HardDriveMonitor/2",
    }
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    return headers


def _request(settings: ApiSettings, path: str, payload: dict | None = None, stream: bool = False,
             timeout: float | None = None, retries: int = 2, cancel=None):
    """发请求并返回 (响应, 实际生效的 base)。

    404 且 Base URL 没写版本号时自动补 /v1 再试；429 / 5xx / 网络抖动按退避重试。
    只在拿到响应头之前重试，已经开始输出的流不会被重复发送。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    timeout = timeout or settings.timeout or 120
    candidates = _base_candidates(settings.base_url)
    last_exc: ApiError | None = None
    for ci, base in enumerate(candidates):
        for attempt in range(retries + 1):
            if cancel is not None and cancel.is_set():
                raise ApiError("已取消")
            req = urllib.request.Request(base + path, data=data, headers=_headers(settings, stream),
                                         method="POST" if data is not None else "GET")
            try:
                return urllib.request.urlopen(req, timeout=timeout), base
            except urllib.error.HTTPError as exc:
                err = _http_error(exc)
                if exc.code == 404 and ci + 1 < len(candidates):
                    last_exc = err
                    break
                if exc.code in RETRY_CODES and attempt < retries:
                    last_exc = err
                    _backoff(attempt, exc, cancel)
                    continue
                raise err from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                if attempt < retries:
                    _backoff(attempt, None, cancel)
                    continue
                raise _network_error(exc, settings.base_url) from exc
    raise last_exc or ApiError("请求失败")


def _backoff(attempt: int, exc, cancel) -> None:
    wait = 1.0 * (2 ** attempt)
    try:
        retry_after = exc.headers.get("Retry-After") if exc is not None else None
        if retry_after:
            wait = float(retry_after)
    except (AttributeError, TypeError, ValueError):
        pass
    deadline = time.monotonic() + min(max(wait, 0.5), 20)
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            return
        time.sleep(0.1)


def _network_error(exc, base_url: str) -> ApiError:
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
        return ApiError("连接超时：服务器长时间没有响应。请检查网络 / 代理，或在设置里调大超时时间。")
    text = str(reason)
    upper = text.upper()
    if is_local_url(base_url) and ("10061" in text or "REFUSED" in upper):
        return ApiError("连不上本地模型服务：请确认 Ollama / LM Studio 已经启动，端口和 Base URL 一致。")
    if "CERTIFICATE" in upper or "SSL" in upper:
        return ApiError(f"SSL 证书校验失败：{text}。如果开着抓包或代理软件，请检查它的证书设置。")
    if "GETADDRINFO" in upper or "11001" in text or "11004" in text:
        return ApiError(f"无法解析域名，请检查 Base URL 是否写错、网络是否可用。（{text}）")
    return ApiError(f"网络连接失败：{text}。请检查 Base URL 和网络。")


def _fit_context(payload: dict, limit: int = MAX_CONTEXT_CHARS) -> str:
    """压缩 JSON；超长时按比例裁掉最长的列表，而不是把 JSON 从中间截断。"""
    def dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    text = dump(payload)
    if len(text) <= limit:
        return text
    ctx = dict(payload.get("扫描上下文") or {})
    trimmed = {**payload, "扫描上下文": ctx}
    while len(text) > limit:
        lists = [k for k in _TRIM_KEYS if isinstance(ctx.get(k), list) and len(ctx[k]) > 3]
        if not lists:
            break
        longest = max(lists, key=lambda k: len(dump(ctx[k])))
        ctx[longest] = ctx[longest][: max(3, len(ctx[longest]) * 2 // 3)]
        text = dump(trimmed)
    if len(text) > limit:
        text = text[:limit] + "…（数据已截断）"
    return text


def build_messages(
    context: dict,
    question: str,
    history: list[ChatMessage] | None = None,
    scope: str = "overview",
    mask: bool = True,
) -> list[dict]:
    payload = {
        "范围": scope,
        "扫描上下文": context,
    }
    system = SYSTEM_PROMPT
    if scope == "selection":
        system += "\n本次只分析用户选中的文件/文件夹，不要扩大到其它无关内容。"
    data_text = _fit_context(payload)
    messages = [{"role": "system", "content": system}]
    for msg in history or []:
        messages.append({"role": msg.role, "content": msg.content})
    user = f"以下是扫描数据（JSON）：\n{data_text}\n\n用户问题：{question}"
    messages.append({"role": "user", "content": user})
    return messages


def stream_chat(settings: ApiSettings, messages: list[dict], cancel_event=None):
    """流式请求，逐段 yield (kind, text)，kind 为 content（正文）或 reasoning（推理过程）。

    失败时抛出带中文说明的 ApiError。按行读取 SSE，收到一行就立刻交给界面，不攒块。
    """
    payload = {"model": settings.model or "deepseek-chat", "messages": messages, "stream": True}
    if settings.temperature is not None:
        payload["temperature"] = float(settings.temperature)
    try:
        resp, _base = _request(settings, "/chat/completions", payload, stream=True, cancel=cancel_event)
    except ApiError as exc:
        # o1 / gpt-5 / 部分推理模型不接受 temperature，去掉后重发一次
        if exc.code == 400 and "temperature" in payload and "temperature" in exc.detail.lower():
            payload.pop("temperature")
            resp, _base = _request(settings, "/chat/completions", payload, stream=True, cancel=cancel_event)
        else:
            raise

    splitter = _ThinkSplitter()
    with resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" not in ctype and "json" in ctype:
            # 个别服务忽略 stream=True，直接返回完整 JSON
            obj = json.loads(resp.read().decode("utf-8", "replace"))
            parsed = _parse_chunk(obj)
            if parsed:
                yield from (parsed,) if parsed[0] == "reasoning" else splitter.feed(parsed[1])
            yield from splitter.flush()
            return
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return
            raw = resp.readline()
            if not raw:
                break
            parsed = _parse_sse_line(raw.decode("utf-8", "replace"))
            if parsed is None:
                continue
            if parsed == DONE:
                break
            if parsed[0] == "reasoning":
                yield parsed
            else:
                yield from splitter.feed(parsed[1])
        yield from splitter.flush()


class _ThinkSplitter:
    """把正文开头 <think>…</think> 包裹的推理内容（Ollama 上的 R1 / Qwen3 等）分离出来。"""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.started = False
        self.pending = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        buf = self.pending + text
        self.pending = ""
        while buf:
            if self.inside:
                end = buf.find(self.CLOSE)
                if end < 0:
                    keep = _partial_suffix(buf, self.CLOSE)
                    if len(buf) > keep:
                        out.append(("reasoning", buf[: len(buf) - keep]))
                    self.pending = buf[len(buf) - keep:]
                    return out
                if end:
                    out.append(("reasoning", buf[:end]))
                buf = buf[end + len(self.CLOSE):].lstrip("\n")
                self.inside = False
                continue
            if not self.started:
                head = buf.lstrip()
                if head.startswith(self.OPEN):
                    self.started = self.inside = True
                    buf = head[len(self.OPEN):]
                    continue
                if not head or self.OPEN.startswith(head):
                    self.pending = buf
                    return out
                self.started = True
            out.append(("content", buf))
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        text, self.pending = self.pending, ""
        if not text:
            return []
        return [("reasoning" if self.inside else "content", text)]


def _partial_suffix(text: str, tag: str) -> int:
    """text 末尾有多少个字符可能是 tag 的开头（标签被拆到两个数据块里时先留着）。"""
    for n in range(min(len(tag) - 1, len(text)), 0, -1):
        if tag.startswith(text[-n:]):
            return n
    return 0


def _parse_sse_line(line: str):
    """解析一行 SSE：返回 None（忽略）、DONE，或 (kind, text)。"""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == DONE:
        return DONE
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    return _parse_chunk(obj)


def _parse_chunk(obj) -> tuple[str, str] | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("error"):
        err = obj["error"]
        message = (err.get("message") if isinstance(err, dict) else str(err)) or "接口返回错误"
        raise ApiError(f"接口返回错误：{message}", detail=message)
    choices = obj.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    delta = choices[0].get("delta") or choices[0].get("message") or {}
    content = delta.get("content")
    if isinstance(content, str) and content:
        return "content", content
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        return "reasoning", reasoning
    return None


def _http_error(exc: urllib.error.HTTPError) -> ApiError:
    detail = ""
    try:
        raw = exc.read().decode("utf-8", "replace")
        obj = json.loads(raw)
        if isinstance(obj, dict):
            err = obj.get("error") or obj
            if isinstance(err, dict):
                detail = err.get("message") or ""
            elif isinstance(err, str):
                detail = err
    except Exception:  # noqa: BLE001
        pass
    code = exc.code
    hints = {
        400: "请求被拒绝（400），通常是模型名写错或参数不被支持。",
        401: "API Key 无效或已过期，请到设置里重新填写。",
        402: "账户余额不足，请先充值。",
        403: "没有权限访问该模型，请检查 Key 或模型名。",
        404: "接口地址或模型名不正确（404）。",
        413: "发送的数据太长，超出了模型的上下文长度。",
        429: "请求太频繁或额度用完了，请稍后再试。",
        500: "服务端错误，请稍后再试。",
        502: "服务端网关错误，请稍后再试。",
        503: "服务繁忙或不可用，请稍后再试。",
    }
    hint = hints.get(code, f"请求失败（HTTP {code}）。")
    return ApiError(f"{hint}{(' 详情：' + detail) if detail else ''}", code=code, detail=detail)


def _check_settings(settings: ApiSettings) -> str:
    if not normalize_base_url(settings.base_url):
        return "还没有填写 API Base URL。"
    if not settings.api_key and not is_local_url(settings.base_url):
        return "还没有填写 API Key。"
    if not settings.model:
        return "还没有填写模型名。"
    return ""


def test_connection(settings: ApiSettings) -> tuple[bool, str, str]:
    """发一条极短的对话验证 Key / 地址 / 模型，返回 (是否成功, 说明, 实际可用的 Base URL)。"""
    problem = _check_settings(settings)
    if problem:
        return False, problem, ""
    payload = {
        "model": settings.model,
        "messages": [{"role": "user", "content": "收到请只回复 OK"}],
        "stream": False,
    }
    started = time.monotonic()
    try:
        resp, base = _request(settings, "/chat/completions", payload, timeout=min(settings.timeout or 60, 60),
                              retries=1)
        with resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        parsed = _parse_chunk(obj)
    except ApiError as exc:
        return False, str(exc), ""
    except ValueError:
        return False, "接口返回的内容不是合法 JSON，请确认 Base URL 是 OpenAI 兼容接口。", ""
    reply = ""
    if parsed:
        splitter = _ThinkSplitter()
        parts = splitter.feed(parsed[1]) + splitter.flush() if parsed[0] == "content" else []
        reply = "".join(text for kind, text in parts if kind == "content").strip()
    msg = f"连接成功，用时 {time.monotonic() - started:.1f} 秒。模型回复：{reply[:40] or '(空)'}"
    if base != normalize_base_url(settings.base_url):
        msg += f"\nBase URL 已自动修正为 {base}，保存设置后生效。"
    return True, msg, base


def list_models(settings: ApiSettings) -> tuple[list[str], str]:
    """GET /models 拉取服务商提供的模型列表，返回 (模型 ID 列表, 实际可用的 Base URL)。"""
    if not normalize_base_url(settings.base_url):
        raise ApiError("还没有填写 API Base URL。")
    if not settings.api_key and not is_local_url(settings.base_url):
        raise ApiError("还没有填写 API Key。")
    resp, base = _request(settings, "/models", None, timeout=min(settings.timeout or 30, 30), retries=1)
    with resp:
        try:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        except ValueError as exc:
            raise ApiError("模型列表不是合法 JSON，这个服务可能不支持 /models 接口，请手动填写模型名。") from exc
    data = obj.get("data") if isinstance(obj, dict) else obj
    if not isinstance(data, list) and isinstance(obj, dict):
        data = obj.get("models")
    ids = {str(m.get("id") or m.get("name")) for m in data or [] if isinstance(m, dict) and (m.get("id") or m.get("name"))}
    return sorted(ids, key=str.lower), base
