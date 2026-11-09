"""AI 抽屉：离线规则报告 / 在线流式问答，挂在主窗口右侧 QDockWidget 里。"""
from __future__ import annotations

import threading
import time
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QPlainTextEdit, QTextBrowser, QVBoxLayout, QWidget

from core import ai, util, volumes
from core.config import is_local_url
from . import workers
from .widgets.common import button, confirm, hbox, info, label

WELCOME = (
    "### AI 助手\n\n"
    "- **分析整体**：根据当前扫描数据生成磁盘报告\n"
    "- **分析选中项**：先在任意列表里选中文件 / 文件夹\n"
    "- 也可以直接提问，例如「C 盘为什么快满了」\n\n"
    "> 默认离线规则分析（不联网）；在「设置 → AI」里填写 API Key（或本地 Ollama / LM Studio 地址）后可用大模型。"
)

SELECTION_QUESTION = "请分析我选中的这些项：它们是什么、为什么占空间、能不能删、应该怎么处理？"


class _Input(QPlainTextEdit):
    def __init__(self, on_send):
        super().__init__()
        self._on_send = on_send

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self._on_send()
            return
        super().keyPressEvent(event)


class AiPanel(QWidget):
    def __init__(self, win):
        super().__init__(win)
        self.setObjectName("AiPanel")
        self.win = win
        self.history: list[ai.ChatMessage] = []
        self.transcript = ""
        self.answer = ""
        self.thinking = 0
        self.started = 0.0
        self.online = False
        self.busy = False
        self._cancel = threading.Event()
        self._gen = 0
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(120)
        self._render_timer.timeout.connect(self._render)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.setSpacing(8)
        self.mode_label = label("", "muted", wrap=True)
        lay.addWidget(self.mode_label)
        self.overview_btn = button("分析整体", "primary", "sparkle", on_click=self.ask_overview)
        self.selection_btn = button("分析选中项", None, None, on_click=self.ask_selection)
        self.stop_btn = button("停止", None, "stop", on_click=self.stop)
        self.stop_btn.setEnabled(False)
        lay.addLayout(hbox(self.overview_btn, self.selection_btn, self.stop_btn, None))

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setMarkdown(WELCOME)
        lay.addWidget(self.view, 1)

        self.input = _Input(self.send)
        self.input.setPlaceholderText("输入问题，Enter 发送，Shift+Enter 换行")
        self.input.setFixedHeight(72)
        lay.addWidget(self.input)
        self.send_btn = button("发送", "primary", on_click=self.send)
        lay.addLayout(hbox(button("清空", "ghost", on_click=self.clear),
                           button("导出", "ghost", "export", on_click=self.export), None, self.send_btn))
        self.refresh_mode()

    # ---------------- 模式 ----------------
    def refresh_mode(self) -> None:
        cfg = self.win.config
        if cfg.ai_ready():
            where = "本地模型" if is_local_url(cfg.ai.get("base_url") or "") else "在线模型"
            self.mode_label.setText(f"{where} · {cfg.ai.get('model')}　路径中的用户名默认脱敏，不发送文件内容。")
        else:
            self.mode_label.setText("离线规则分析（不联网）。到「设置 → AI」填写 API Key 或本地模型地址可切换。")

    def focus_input(self) -> None:
        self.input.setFocus()

    # ---------------- 发送 ----------------
    def ask_overview(self) -> None:
        self._start("请分析我的磁盘空间占用情况，指出主要占用来源，并给出可执行的清理建议。", "overview", None)

    def ask_selection(self) -> None:
        records = self.win.current_selection()
        if not records:
            info(self, "分析选中项", "先在「空间分布」「大文件」「最近文件」或「清理推荐」里选中一项。")
            return
        self._start(SELECTION_QUESTION, "selection", records)

    def ask_records(self, records: list[dict]) -> None:
        if records:
            self._start(SELECTION_QUESTION, "selection", records)

    def send(self) -> None:
        question = self.input.toPlainText().strip()
        if not question or self.busy:
            return
        self.input.clear()
        self._start(question, "chat", None)

    def _start(self, question: str, scope: str, selection: list[dict] | None) -> None:
        if self.busy:
            info(self, "AI 助手", "正在生成，等这次结束或点「停止」。")
            return
        cfg = self.win.config
        online = cfg.ai_ready()
        if online and not is_local_url(cfg.ai.get("base_url") or "") and not cfg.privacy.get("privacy_notice_seen"):
            if not confirm(
                self, "发送前的隐私说明",
                "AI 分析会把扫描结果发送给你配置的模型服务。",
                "· 发送：文件夹 / 文件路径、大小、修改时间、清理候选等元数据\n"
                "· 不发送：文件内容\n"
                "· 路径中的用户名默认替换成 <用户>",
            ):
                return
            cfg.privacy["privacy_notice_seen"] = True
            cfg.save()
        self._set_busy(True)
        self._cancel = threading.Event()
        self._gen += 1
        gen = self._gen
        self.transcript += f"\n\n---\n\n**你：** {question}\n\n"
        self.answer = ""
        self.thinking = 0
        self.started = time.monotonic()
        self.online = online
        self._render()
        history = list(self.history)
        self.history.append(ai.ChatMessage("user", question))
        context_args = (self.win.index, list(self.win.cleanup_items), bool(cfg.privacy.get("mask_user", True)))
        settings = ai.ApiSettings.from_config(cfg)
        cancel = self._cancel

        def emit(kind, payload=None):
            workers.call_in_main(self._on_event, gen, kind, payload)

        def worker():
            try:
                context = _context(*context_args, selection)
                if not online:
                    if scope == "chat":
                        text = ai.offline_chat_answer(question, context)
                    else:
                        text = ai.offline_report(context, selection if scope == "selection" else None)
                    emit("delta", text)
                    emit("done")
                    return
                messages = ai.build_messages(context, question, history, scope=scope, mask=context_args[2])
                for kind, text in ai.stream_chat(settings, messages, cancel):
                    emit("delta" if kind == "content" else "reasoning", text)
                emit("stopped" if cancel.is_set() else "done")
            except RuntimeError as exc:
                emit("error", str(exc))
            except Exception:  # noqa: BLE001
                emit("error", "AI 分析失败：" + traceback.format_exc(limit=2))

        threading.Thread(target=worker, daemon=True, name="ai").start()

    def stop(self) -> None:
        if self.busy:
            self._cancel.set()
            self._on_event(self._gen, "stopped", None)

    # ---------------- 事件 ----------------
    def _on_event(self, gen: int, kind: str, payload) -> None:
        if gen != self._gen or not self.busy:
            return
        if kind in ("delta", "reasoning"):
            if kind == "delta":
                self.answer += payload
            else:
                self.thinking += len(payload)
            if not self._render_timer.isActive():
                self._render_timer.start()
            return
        elapsed = time.monotonic() - self.started
        if kind == "done":
            self.transcript += self._buffer()
            if self.online:
                self.transcript += f"\n\n*用时 {elapsed:.0f} 秒*\n"
            if self.answer:
                self.history.append(ai.ChatMessage("assistant", self.answer[-4000:]))
        elif kind == "stopped":
            self.transcript += self._buffer() + "\n\n> 已停止生成。\n"
            self._gen += 1
        elif kind == "error":
            self.transcript += self._buffer() + f"\n\n> ⚠️ {payload}\n"
        self.answer = ""
        self.thinking = 0
        self._set_busy(False)
        self._render()

    def _buffer(self) -> str:
        if not self.busy:
            return ""
        text = "**AI：**\n\n"
        if self.answer:
            return text + self.answer
        if self.thinking:
            return text + f"> 正在思考…（已推理 {self.thinking:,} 字，{time.monotonic() - self.started:.0f} 秒）\n"
        if self.online:
            return text + "> 正在连接模型…\n"
        return text

    def _render(self) -> None:
        self._render_timer.stop()
        self.view.setMarkdown(self.transcript + self._buffer())
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for btn in (self.overview_btn, self.selection_btn, self.send_btn):
            btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        if busy:
            self.win.status("AI 正在分析…")

    def clear(self) -> None:
        if self.busy:
            self.stop()
        self.transcript = ""
        self.answer = ""
        self.history.clear()
        self.view.setMarkdown(WELCOME)

    def export(self) -> None:
        text = (self.transcript + self._buffer()).strip()
        if not text:
            info(self, "导出", "还没有内容可导出。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出 AI 分析", "磁盘分析报告.md", "Markdown 文件 (*.md)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            info(self, "导出失败", str(exc))
            return
        self.win.status(f"已导出到 {path}")


def _context(index, cleanup_items, mask: bool, selection: list[dict] | None) -> dict:
    context = ai.build_context(index, volumes.list_volumes(), cleanup_items, mask=mask)
    if selection:
        context["选中项"] = [
            {
                "路径": util.mask_user(rec.get("path", "")) if mask else rec.get("path", ""),
                "大小": util.human_size(rec.get("size") or 0),
                "修改时间": util.fmt_date(rec.get("mtime") or 0),
                "扩展名": rec.get("ext") or "",
                "类型": "文件夹" if rec.get("is_dir") else "文件",
            }
            for rec in selection[:20]
        ]
    return context
