"""设置：扫描、监视与托盘、AI、维护。"""
from __future__ import annotations

import ctypes
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from core import ai, util, volumes
from core.config import PROVIDER_PRESETS, is_local_url, normalize_base_url
from .. import theme, workers
from ..widgets.common import Card, button, confirm, error, hbox, info, label, set_role
from .base import Page

SIZE_CHOICES = [(50, "50 MB"), (100, "100 MB"), (200, "200 MB"), (500, "500 MB"), (1024, "1 GB")]
METHODS = [("auto", "自动（NTFS + 管理员时直读 MFT，否则多进程遍历）"), ("mft", "优先直读 MFT"), ("scandir", "总是多进程遍历")]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(18)
    form.setVerticalSpacing(10)
    form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
    return form


def _spin(lo: int, hi: int, suffix: str = "", special: str | None = None) -> QSpinBox:
    box = QSpinBox()
    box.setRange(lo, hi)
    box.setSuffix(suffix)
    box.setMinimumWidth(110)
    if special:
        box.setSpecialValueText(special)
    return box


def _combo(options) -> QComboBox:
    box = QComboBox()
    for value, text in options:
        box.addItem(text, value)
    return box


def _set_combo(box: QComboBox, value) -> None:
    pos = box.findData(value)
    box.setCurrentIndex(pos if pos >= 0 else 0)


class SettingsPage(Page):
    def __init__(self, win):
        super().__init__(win, scroll=True)
        self.body.addLayout(hbox(label("修改后点「保存设置」生效。", "muted"), None,
                                 button("保存设置", "primary", "check", on_click=self.save)))

        # ---- 扫描 ----
        scan = Card("扫描")
        form = _form()
        self.drive_row = QHBoxLayout()
        self.drive_boxes: dict[str, QCheckBox] = {}
        drive_holder = QWidget()
        drive_holder.setLayout(self.drive_row)
        self.drive_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("扫描的磁盘", drive_holder)
        self.method = _combo(METHODS)
        form.addRow("扫描方式", self.method)
        self.admin_label = label("", "small", wrap=True)
        form.addRow("", self.admin_label)
        self.threads = _spin(0, 16, " 个进程", "自动")
        form.addRow("并行进程", self.threads)
        self.min_size = _combo(SIZE_CHOICES)
        form.addRow("大文件阈值", self.min_size)
        self.recent_days = _spin(7, 3650, " 天")
        form.addRow("记录最近文件", self.recent_days)
        self.auto_scan = QCheckBox("在电脑空闲时自动重新扫描，每")
        self.auto_hours = _spin(1, 720, " 小时")
        form.addRow("自动扫描", hbox(self.auto_scan, self.auto_hours, None))
        self.watch = QCheckBox("文件被删除、新建或改动后，几秒内自动更新索引（不用重新扫描）")
        form.addRow("自动同步", self.watch)
        scan.body.addLayout(form)
        self.body.addWidget(scan)

        # ---- 监视与托盘 ----
        mon = Card("监视与托盘")
        form = _form()
        self.sample_minutes = _spin(1, 240, " 分钟")
        form.addRow("采样间隔", self.sample_minutes)
        self.low_pct = _spin(0, 50, " %", "不检查")
        self.low_gb = _spin(0, 2000, " GB", "不检查")
        form.addRow("空间不足提醒", hbox(label("剩余低于"), self.low_pct, label("或"), self.low_gb, None))
        self.drop_gb = _spin(0, 500, " GB", "不检查")
        self.drop_window = _spin(5, 1440, " 分钟")
        form.addRow("空间骤降提醒", hbox(self.drop_window, label("内减少超过"), self.drop_gb, None))
        self.notify = QCheckBox("通过托盘气泡通知")
        form.addRow("", self.notify)
        self.close_to_tray = QCheckBox("关闭窗口时最小化到托盘，继续在后台监视")
        form.addRow("关闭按钮", self.close_to_tray)
        self.autostart = QCheckBox("登录 Windows 时自动在托盘启动")
        form.addRow("开机自启", self.autostart)
        self.migrate_root = QLineEdit()
        form.addRow("迁移目标根目录", hbox(self.migrate_root, button("浏览…", on_click=self._browse_migrate)))
        self.theme = _combo([("light", "浅色"), ("dark", "深色")])
        form.addRow("界面主题", self.theme)
        mon.body.addLayout(form)
        self.body.addWidget(mon)

        # ---- AI ----
        ai_card = self.ai_card = Card("AI 助手（可选）")
        ai_card.body.addWidget(label("不填写时使用离线规则分析，不联网。支持任何 OpenAI 兼容接口（含本地 Ollama / LM Studio，"
                                     "本地服务不用填 Key）；API Key 用 Windows DPAPI 加密保存。", "muted", wrap=True))
        form = _form()
        self.provider = _combo([(name, name) for name, _u, _m in PROVIDER_PRESETS])
        self.provider.currentIndexChanged.connect(self._on_provider)
        form.addRow("服务商", self.provider)
        self.base_url = QLineEdit()
        self.base_url.setMinimumWidth(360)
        self.base_url.textChanged.connect(self._update_key_hint)
        form.addRow("Base URL", self.base_url)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setMinimumWidth(260)
        self.model.setInsertPolicy(QComboBox.NoInsert)
        self.models_btn = button("获取模型列表", None, "refresh", "从服务商拉取可用模型", self._fetch_models)
        form.addRow("模型", hbox(self.model, self.models_btn))
        self.api_key = QLineEdit()
        self.api_key.setMinimumWidth(300)
        self.api_key.setEchoMode(QLineEdit.Password)
        show = button("显示", "ghost", on_click=self._toggle_key)
        form.addRow("API Key", hbox(self.api_key, show))
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 1.5)
        self.temperature.setSingleStep(0.1)
        self.timeout = _spin(20, 600, " 秒")
        form.addRow("Temperature", hbox(self.temperature, label("超时"), self.timeout, None))
        self.mask_user = QCheckBox("发送前把路径里的用户名替换成 <用户>")
        form.addRow("隐私", self.mask_user)
        self.test_btn = button("测试连接", None, on_click=self._test)
        self.test_label = label("", "muted", wrap=True)
        form.addRow("", hbox(self.test_btn, self.test_label, None))
        ai_card.body.addLayout(form)
        self.key_warning = label("", "warn", wrap=True)
        ai_card.body.addWidget(self.key_warning)
        self.body.addWidget(ai_card)

        # ---- 维护 ----
        maint = Card("维护")
        self.admin_btn = button("以管理员身份重新启动", None, None, "管理员权限下可直读 MFT，扫描快得多，也能看到更多系统目录",
                                self._relaunch_admin)
        maint.body.addLayout(hbox(button("打开数据目录", on_click=self._open_data),
                                  button("清空索引", on_click=self._clear_index), self.admin_btn, None))
        maint.body.addWidget(label("索引、配置、日志和迁移记录都保存在程序目录的 data 文件夹里。不会自动删除任何文件。",
                                   "small", wrap=True))
        self.body.addWidget(maint)
        self.body.addStretch(1)
        self._loading = False

    # ---------------- 读取 ----------------
    def refresh(self) -> None:
        self._loading = True
        cfg = self.win.config
        while self.drive_row.count():
            item = self.drive_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.drive_boxes.clear()
        selected = {d.rstrip(":").upper() + ":" for d in (cfg.scan.get("drives") or [])}
        for vol in volumes.list_volumes():
            box = QCheckBox(f"{vol.letter} {vol.label or ''}（{util.human_size(vol.total)}，{vol.fs}）")
            box.setChecked(vol.letter.upper() in selected)
            self.drive_boxes[vol.letter] = box
            self.drive_row.addWidget(box)
        self.drive_row.addStretch(1)
        _set_combo(self.method, cfg.scan.get("method") or "auto")
        admin = is_admin()
        self.admin_label.setText("当前是管理员权限：NTFS 磁盘会直读 MFT，几十秒即可扫完整盘。" if admin else
                                 "当前不是管理员：将使用多进程遍历。以管理员身份运行可直读 MFT，速度快很多。")
        set_role(self.admin_label, "ok" if admin else "small")
        self.admin_btn.setVisible(not admin)
        self.threads.setValue(int(cfg.scan.get("threads") or 0))
        min_mb = int((cfg.scan.get("min_big_size") or 100 * util.MB) // util.MB)
        _set_combo(self.min_size, min(SIZE_CHOICES, key=lambda c: abs(c[0] - min_mb))[0])
        self.recent_days.setValue(int(cfg.scan.get("recent_days") or 180))
        self.auto_scan.setChecked(bool(cfg.scan.get("auto_scan", True)))
        self.auto_hours.setValue(int(cfg.scan.get("auto_scan_hours") or 24))
        self.watch.setChecked(bool(cfg.scan.get("watch", True)))

        mon = cfg.monitor
        self.sample_minutes.setValue(int(mon.get("sample_minutes") or 10))
        self.low_pct.setValue(int(mon.get("low_pct") or 0))
        self.low_gb.setValue(int(mon.get("low_gb") or 0))
        self.drop_gb.setValue(int(mon.get("drop_gb") or 0))
        self.drop_window.setValue(int(mon.get("drop_window_min") or 60))
        self.notify.setChecked(bool(mon.get("notify", True)))
        self.close_to_tray.setChecked(bool(cfg.app.get("close_to_tray", True)))
        self.autostart.setChecked(bool(cfg.app.get("autostart")))
        self.migrate_root.setText(cfg.app.get("migrate_root") or "D:\\已迁移")
        _set_combo(self.theme, cfg.app.get("theme") or "light")

        _set_combo(self.provider, cfg.ai.get("provider") or "DeepSeek")
        self.base_url.setText(cfg.ai.get("base_url") or "")
        self.model.setEditText(cfg.ai.get("model") or "")
        self.api_key.setText(cfg.api_key)
        temp = cfg.ai.get("temperature")
        self.temperature.setValue(0.3 if temp is None else float(temp))
        self.timeout.setValue(int(cfg.ai.get("timeout") or 120))
        self._update_key_hint()
        self.mask_user.setChecked(bool(cfg.privacy.get("mask_user", True)))
        self.key_warning.setText(cfg.key_warning)
        self.key_warning.setVisible(bool(cfg.key_warning))
        self._loading = False

    def on_show(self) -> None:
        pass

    def focus_ai(self) -> None:
        """从 AI 面板跳过来：滚到 AI 设置，光标放进 API Key 输入框。"""
        def go():
            self.scroll_area.verticalScrollBar().setValue(self.ai_card.y())
            self.api_key.setFocus()

        QTimer.singleShot(0, go)

    # ---------------- 保存 ----------------
    def save(self) -> None:
        cfg = self.win.config
        drives = [letter for letter, box in self.drive_boxes.items() if box.isChecked()]
        if not drives:
            error(self, "设置", "至少选择一个要扫描的磁盘。")
            return
        cfg.scan.update(
            drives=drives,
            method=self.method.currentData(),
            threads=self.threads.value(),
            min_big_size=int(self.min_size.currentData()) * util.MB,
            recent_days=self.recent_days.value(),
            auto_scan=self.auto_scan.isChecked(),
            auto_scan_hours=self.auto_hours.value(),
            watch=self.watch.isChecked(),
        )
        cfg.monitor.update(
            sample_minutes=self.sample_minutes.value(),
            low_pct=self.low_pct.value(),
            low_gb=self.low_gb.value(),
            drop_gb=self.drop_gb.value(),
            drop_window_min=self.drop_window.value(),
            notify=self.notify.isChecked(),
        )
        old_theme = cfg.app.get("theme") or "light"
        new_theme = self.theme.currentData()
        want_autostart = self.autostart.isChecked()
        autostart_changed = want_autostart != bool(cfg.app.get("autostart"))
        cfg.app.update(close_to_tray=self.close_to_tray.isChecked(), autostart=want_autostart,
                       migrate_root=self.migrate_root.text().strip() or "D:\\已迁移", theme=new_theme)
        cfg.ai.update(provider=self.provider.currentData(), base_url=normalize_base_url(self.base_url.text()),
                      model=self.model.currentText().strip(), temperature=round(self.temperature.value(), 2),
                      timeout=self.timeout.value())
        cfg.api_key = self.api_key.text().strip()
        cfg.privacy["mask_user"] = self.mask_user.isChecked()
        try:
            cfg.save()
        except OSError as exc:
            error(self, "保存失败", str(exc))
            return
        cfg.key_warning = ""
        self.key_warning.hide()
        if autostart_changed and not self.win.set_autostart(want_autostart):
            error(self, "开机自启", "写入注册表失败，详情见 data/app.log。")
        self.win.apply_monitor_settings()
        self.win.apply_watch_settings()
        self.win.ai_panel.refresh_mode()
        if new_theme != old_theme:
            theme.apply_theme(QApplication.instance(), new_theme)
            info(self, "界面主题", "主题已切换，部分图标重启程序后完全生效。")
        self.win.status("设置已保存")

    # ---------------- 其它 ----------------
    def _on_provider(self) -> None:
        if self._loading:
            return
        name = self.provider.currentData()
        for preset, url, model in PROVIDER_PRESETS:
            if preset == name and url:
                self.base_url.setText(url)
                self.model.clear()
                self.model.setEditText(model)

    def _update_key_hint(self) -> None:
        local = is_local_url(self.base_url.text())
        self.api_key.setPlaceholderText("本地服务一般不需要，可留空" if local else "sk-…")

    def _probe(self) -> ai.ApiSettings:
        return ai.ApiSettings(base_url=self.base_url.text().strip(), model=self.model.currentText().strip(),
                              api_key=self.api_key.text().strip(), temperature=self.temperature.value(),
                              timeout=self.timeout.value())

    def _fetch_models(self) -> None:
        probe = self._probe()
        current = probe.model
        self.models_btn.setEnabled(False)
        self.test_label.setText("正在获取模型列表…")
        set_role(self.test_label, "muted")

        def done(result):
            models, base = result
            self.models_btn.setEnabled(True)
            if base and base != normalize_base_url(self.base_url.text()):
                self.base_url.setText(base)
            if not models:
                self.test_label.setText("服务没有返回模型列表，请手动填写模型名。")
                return
            self.model.clear()
            self.model.addItems(models)
            self.model.setEditText(current if current else models[0])
            self.test_label.setText(f"获取到 {len(models)} 个模型，可在下拉框里选择。")
            set_role(self.test_label, "ok")

        def failed(exc, _tb):
            self.models_btn.setEnabled(True)
            self.test_label.setText(f"获取失败：{exc}")
            set_role(self.test_label, "danger")

        workers.run_async(lambda: ai.list_models(probe), done, failed)

    def _toggle_key(self) -> None:
        hidden = self.api_key.echoMode() == QLineEdit.Password
        self.api_key.setEchoMode(QLineEdit.Normal if hidden else QLineEdit.Password)

    def _browse_migrate(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择迁移目标根目录", self.migrate_root.text() or "D:\\")
        if folder:
            self.migrate_root.setText(os.path.normpath(folder))

    def _test(self) -> None:
        probe = self._probe()
        self.test_btn.setEnabled(False)
        self.test_label.setText("正在测试…")
        set_role(self.test_label, "muted")

        def done(result):
            ok, msg, base = result
            self.test_btn.setEnabled(True)
            if ok and base and base != normalize_base_url(self.base_url.text()):
                self.base_url.setText(base)
            self.test_label.setText(msg)
            set_role(self.test_label, "ok" if ok else "danger")

        workers.run_async(lambda: ai.test_connection(probe), done,
                          lambda exc, _tb: done((False, f"测试失败：{exc}", "")))

    def _open_data(self) -> None:
        try:
            os.startfile(self.win.data_dir)  # noqa: S606
        except OSError as exc:
            error(self, "打开失败", str(exc))

    def _clear_index(self) -> None:
        if self.win.is_busy():
            info(self, "清空索引", "有任务正在进行，稍后再试。")
            return
        if not confirm(self, "清空索引", "清空已扫描的索引和清理分析结果？", "不会删除任何文件；空间历史记录会保留。清空后需要重新扫描。",
                       danger=True, ok_text="清空"):
            return
        try:
            self.win.index.clear()
        except Exception as exc:  # noqa: BLE001
            error(self, "清空失败", str(exc))
            return
        self.win.cleanup_items = []
        self.win.indexChanged.emit()
        self.win.cleanupChanged.emit()
        self.win.status("索引已清空")

    def _relaunch_admin(self) -> None:
        script = os.path.join(self.win.app_dir, "app.py")
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            pythonw = exe[:-10] + "pythonw.exe"
            if os.path.exists(pythonw):
                exe = pythonw
        params = "--replace" if getattr(sys, "frozen", False) else f'"{script}" --replace'
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, self.win.app_dir, 1)
        if rc <= 32:
            return
        self.win.quit_app()
