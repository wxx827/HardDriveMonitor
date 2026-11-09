"""监视：剩余空间历史曲线、两次扫描间膨胀的目录、告警记录、采样与自动扫描状态。"""
from __future__ import annotations

import time

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QMargins, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout

from core import monitor, util
from .. import theme, workers
from ..actions import show_record_menu
from ..models import Column, RecordTableModel, make_table, selected_rows
from ..widgets.common import Card, Segmented, button, hbox, label, short_name
from .base import Page

RANGES = [("1", "24 小时"), ("7", "7 天"), ("30", "30 天"), ("0", "全部")]


class MonitorPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self._latest = workers.Latest()

        self.status_label = label("", "muted", wrap=True)
        self.sample_btn = button("立即采样", None, "refresh", on_click=win.sample_now)
        self.scan_btn = button("立即扫描", None, "scan", on_click=lambda: win.start_scan())
        self.body.addLayout(hbox(self.status_label, None, self.sample_btn, self.scan_btn))

        chart_card = Card("剩余空间变化")
        self.range = Segmented(RANGES, "7")
        self.range.changed.connect(lambda _v: self.refresh())
        chart_card.header.addWidget(self.range)
        self.chart = QChart()
        self.chart.setMargins(QMargins(4, 4, 4, 4))
        self.chart.legend().setAlignment(Qt.AlignTop)
        self.chart.setAnimationOptions(QChart.NoAnimation)
        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setMinimumHeight(260)
        self.chart_empty = label("", "muted")
        self.chart_empty.setAlignment(Qt.AlignCenter)
        chart_card.body.addWidget(self.chart_view, 1)
        chart_card.body.addWidget(self.chart_empty)
        self.body.addWidget(chart_card, 3)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        growth_card = Card("最近膨胀的目录")
        self.growth_sub = label("", "small")
        growth_card.header.addWidget(self.growth_sub)
        self.growth_model = RecordTableModel([
            Column("path", "目录", 320, fmt=lambda r: r["path"], stretch=True),
            Column("before", "之前", 86, "right", fmt=lambda r: "新出现" if r["new"] else util.human_size(r["before"])),
            Column("after", "现在", 86, "right", fmt=lambda r: util.human_size(r["after"])),
            Column("delta", "增长", 90, "right", fmt=lambda r: "+" + util.human_size(r["delta"]), color=lambda r: "warn"),
        ])
        self.growth_table = make_table(self.growth_model, sort_key="delta")
        self.growth_table.doubleClicked.connect(
            lambda idx: win.goto_explorer(self.growth_model.rows[idx.row()]["path"]))
        self.growth_table.customContextMenuRequested.connect(self._growth_menu)
        self.growth_hint = label("", "muted", wrap=True)
        growth_card.body.addWidget(self.growth_table, 1)
        growth_card.body.addWidget(self.growth_hint)
        bottom.addWidget(growth_card, 3)

        alert_card = Card("告警记录")
        self.alert_model = RecordTableModel([
            Column("ts", "时间", 130, fmt=lambda r: util.fmt_time(r["ts"])),
            Column("letter", "磁盘", 50),
            Column("message", "内容", 300, stretch=True, color=lambda r: "warn"),
        ])
        self.alert_table = make_table(self.alert_model, sort_key="ts")
        alert_card.body.addWidget(self.alert_table, 1)
        bottom.addWidget(alert_card, 2)
        self.body.addLayout(bottom, 2)

        win.volumesChanged.connect(self._on_volumes_changed)

    def _on_volumes_changed(self) -> None:
        if self.win.current_key() == "monitor":
            self.refresh()

    def on_busy(self, busy: bool) -> None:
        self.scan_btn.setEnabled(not busy)

    def selected_records(self) -> list[dict]:
        return [{"path": r["path"], "size": r["after"], "is_dir": True, "name": short_name(r["path"])}
                for r in selected_rows(self.growth_table, self.growth_model)]

    def _growth_menu(self, pos) -> None:
        records = self.selected_records()
        if records:
            show_record_menu(self.win, self, records, self.growth_table.viewport().mapToGlobal(pos))

    # ---------------- 数据 ----------------
    def refresh(self) -> None:
        self._update_status()
        index = self.win.index
        days = int(self.range.value() or 7)
        since = int(time.time() - days * 86400) if days else 0

        def load():
            history = index.volume_history(None, since)
            return history, monitor.growth(index, 200), index.alerts(200)

        self._latest.run(load, self._on_loaded, lambda _e, tb: self.win.log("监视页加载失败", tb))

    def _update_status(self) -> None:
        mon, scan = self.win.config.monitor, self.win.config.scan
        parts = [f"每 {mon.get('sample_minutes', 10)} 分钟记录一次剩余空间",
                 f"剩余低于 {mon.get('low_pct', 10)}% 或 {mon.get('low_gb', 20)} GB 时提醒",
                 f"{mon.get('drop_window_min', 60)} 分钟内减少超过 {mon.get('drop_gb', 5)} GB 时提醒"]
        if scan.get("auto_scan"):
            parts.append(f"每 {scan.get('auto_scan_hours', 24)} 小时在电脑空闲时自动扫描")
        else:
            parts.append("自动扫描已关闭")
        if not mon.get("notify", True):
            parts.append("托盘通知已关闭")
        self.status_label.setText("　·　".join(parts))

    def _on_loaded(self, result) -> None:
        history, growth, alerts = result
        self._fill_chart(history)
        rows = growth.get("rows") or []
        self.growth_model.set_rows(rows)
        if growth.get("before"):
            self.growth_sub.setText(f"{util.fmt_time(growth['before'])} → {util.fmt_time(growth['after'])}")
            self.growth_hint.setText("" if rows else "两次扫描之间没有明显增长的目录（只统计 ≥ 64 MB 的前 5 层目录）。")
        else:
            self.growth_sub.setText("")
            self.growth_hint.setText("需要至少两次完整扫描才能对比增长。")
        self.growth_hint.setVisible(bool(self.growth_hint.text()))
        self.alert_model.set_rows(alerts)

    def _fill_chart(self, history: list[dict]) -> None:
        chart = self.chart
        chart.removeAllSeries()
        for axis in list(chart.axes()):
            chart.removeAxis(axis)
        dark = theme.is_dark()
        chart.setTheme(QChart.ChartThemeDark if dark else QChart.ChartThemeLight)
        chart.setBackgroundBrush(theme.color("panel"))
        chart.setBackgroundRoundness(0)
        chart.legend().setLabelColor(theme.color("text"))
        by_drive: dict[str, list[dict]] = {}
        for row in history:
            by_drive.setdefault(row["letter"], []).append(row)
        points = sum(len(v) for v in by_drive.values())
        if points < 2:
            self.chart_view.setVisible(False)
            self.chart_empty.setText("还没有足够的采样数据。程序在后台运行时会定期记录各磁盘剩余空间。")
            self.chart_empty.setVisible(True)
            return
        self.chart_view.setVisible(True)
        self.chart_empty.setVisible(False)
        x_axis = QDateTimeAxis()
        span = max(r["ts"] for r in history) - min(r["ts"] for r in history)
        x_axis.setFormat("HH:mm" if span <= 2 * 86400 else "MM-dd")
        x_axis.setTickCount(7)
        y_axis = QValueAxis()
        y_axis.setLabelFormat("%.0f GB")
        for axis in (x_axis, y_axis):
            axis.setLabelsColor(theme.color("muted"))
            axis.setGridLineColor(theme.color("border"))
            axis.setLinePenColor(theme.color("border_strong"))
        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignLeft)
        lo, hi = None, 0.0
        for i, (letter, rows) in enumerate(sorted(by_drive.items())):
            series = QLineSeries()
            series.setName(f"{letter} 剩余")
            pen = QPen(QColor(theme.TREEMAP_HUES[i % len(theme.TREEMAP_HUES)]))
            pen.setWidthF(2.0)
            series.setPen(pen)
            for row in rows:
                gb = row["free"] / util.GB
                series.append(QDateTime.fromSecsSinceEpoch(int(row["ts"])).toMSecsSinceEpoch(), gb)
                lo = gb if lo is None else min(lo, gb)
                hi = max(hi, gb)
            chart.addSeries(series)
            series.attachAxis(x_axis)
            series.attachAxis(y_axis)
        pad = max(1.0, (hi - (lo or 0)) * 0.1)
        y_axis.setRange(max(0.0, (lo or 0) - pad), hi + pad)
