# SPDX-License-Identifier: GPL-3.0-or-later
"""
同步直播源弹窗（主界面控制条「🔄 同步源」按钮打开）。

界面：
  ┌ 来源列表（勾选）—— 取自 config.SCRAPE_SOURCES，逐条显示覆盖范围与网络要求
  ├ 加速方式（下拉）—— 取自 config.GITHUB_MIRRORS，默认「自动」
  ├ 选项：☑ 新增地址置顶　☑ 允许新增频道
  └ 开始同步 → 后台线程拉取 + 对账 → 实时进度与结果汇总

关于"加速器"：本机实测 `raw.githubusercontent.com` 直连**完全不通**，
而 jsDelivr / gh-proxy.com / ghproxy.net / ghfast.top 都能拉到同一份文件。
所以凡是以 raw.githubusercontent.com 开头的源，都必须配合加速方式才能同步。
「自动」会依次尝试直连与各加速器，取第一个成功的，并在结果里告诉你用的是哪个。

同步语义（详见 core/sync_sources.py）：
  同来源地址**更换** → 上游没有的**新增** → 上游删掉的**删除**；
  被用户手动改过的源**绝不覆盖**，改为把上游地址另存并**置顶**。

线程约定：
  - 网络抓取与写库都在 QThreadPool 的后台线程（SyncTask）里做，GUI 不会卡死；
  - 进度与结果一律经 Qt 信号回主线程再更新控件（跨线程绝不能直接碰 QWidget）；
  - 同步期间禁用「开始同步」，避免重复提交。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout,
)

import liuhaitv.config as cfg
from liuhaitv.core import mirror as mirror_mod
from liuhaitv.core import sync_sources

log = logging.getLogger(__name__)

# 模块级保活表：同步任务在跑期间，signals 对象不能被 GC，
# 否则窗口先销毁时工作线程 emit 会抛 "Signal source has been deleted"。
_ALIVE: list = []


def _retain(obj) -> None:
    _ALIVE.append(obj)


def _release(obj) -> None:
    try:
        _ALIVE.remove(obj)
    except ValueError:
        pass


class _SyncSignals(QObject):
    progress = Signal(str)
    finished = Signal(object)     # SyncReport


class SyncTask(QRunnable):
    """后台执行一次同步（网络 + 写库），结果经信号回主线程。"""

    def __init__(self, origin_ids, mirror_id, top_new, new_channel_scope,
                 signals: _SyncSignals) -> None:
        super().__init__()
        self.origin_ids = origin_ids
        self.mirror_id = mirror_id
        self.top_new = top_new
        self.new_channel_scope = new_channel_scope
        self.signals = signals
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            report = sync_sources.sync_from_sources(
                self.origin_ids,
                mirror_id=self.mirror_id,
                top_new=self.top_new,
                new_channel_scope=self.new_channel_scope,
                progress=self._emit_progress,
            )
        except Exception as exc:  # noqa: BLE001 - 线程内异常不能外泄
            log.exception("同步任务异常: %s", exc)
            report = sync_sources.SyncReport(
                mirror_id=self.mirror_id,
                mirror_name=mirror_mod.mirror_name(self.mirror_id),
                top_new=self.top_new,
            )
            report.reports.append(sync_sources.OriginReport(
                origin="-", name="同步", ok=False, error="同步异常: %s" % exc))
        self._emit_finished(report)

    def _emit_progress(self, text: str) -> None:
        try:
            self.signals.progress.emit(text)
        except RuntimeError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("进度发送失败: %s", exc)

    def _emit_finished(self, report) -> None:
        try:
            self.signals.finished.emit(report)
        except RuntimeError as exc:
            log.debug("同步结果丢弃（接收方已销毁）: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("同步结果发送失败: %s", exc)


class SyncDialog(QDialog):
    """
    同步直播源弹窗。

    信号：
        synced —— 同步结束且**至少有一个来源成功**时发出（携带 SyncReport），
                  主窗口据此刷新频道列表与颜色。
    """

    synced = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("同步直播源")
        self.resize(720, 560)
        self._running = False
        self._signals = None
        self._build()

    # ---- 构建 --------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)

        root.addWidget(QLabel("选择要从哪些来源同步（每个来源都是一份公开的 GitHub 直播源清单）："))
        self.lst = QListWidget()
        self.lst.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for src in sync_sources.list_sources():
            it = QListWidgetItem("%s　%s" % (src.get("name", src.get("id")),
                                            _mirror_hint(src.get("url", ""))))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if src.get("default", True)
                             else Qt.CheckState.Unchecked)
            tip = [src.get("url", "")] + ([src["note"]] if src.get("note") else [])
            it.setToolTip("\n".join(tip))
            it.setData(Qt.ItemDataRole.UserRole, src.get("id"))
            self.lst.addItem(it)
        root.addWidget(self.lst, 1)

        row = QHBoxLayout()
        self.btn_all = QPushButton("全选")
        self.btn_none = QPushButton("全不选")
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_none)
        row.addStretch(1)
        row.addWidget(QLabel("加速方式"))
        self.cmb_mirror = QComboBox()
        for m in mirror_mod.list_mirrors():
            self.cmb_mirror.addItem(m["name"], m["id"])
            idx = self.cmb_mirror.count() - 1
            if m.get("note"):
                self.cmb_mirror.setItemData(idx, m["note"], Qt.ItemDataRole.ToolTipRole)
        default_idx = self.cmb_mirror.findData(cfg.DEFAULT_MIRROR)
        if default_idx >= 0:
            self.cmb_mirror.setCurrentIndex(default_idx)
        self.cmb_mirror.setMinimumWidth(190)
        row.addWidget(self.cmb_mirror)
        root.addLayout(row)

        opt = QHBoxLayout()
        self.ck_top = QCheckBox("新增的地址置顶（优先试用）")
        self.ck_top.setChecked(True)
        self.ck_top.setToolTip("勾选后，同步新增的地址优先级设为 0，排在现有源之前先被尝试")
        opt.addWidget(self.ck_top)
        opt.addSpacing(12)
        opt.addWidget(QLabel("新增频道"))
        self.cmb_new = QComboBox()
        self.cmb_new.addItem("不新增（只同步地址）", "none")
        self.cmb_new.addItem("只补缺失的央视", "cctv")
        self.cmb_new.addItem("央视 + 卫视都补", "all")
        self.cmb_new.setCurrentIndex(0)
        self.cmb_new.setToolTip(
            "默认「不新增」：同步只动地址，不会往你已经筛过的频道列表里塞新台。\n"
            "「只补缺失的央视」适合补上 CCTV-4 / CCTV-5 这类库里没有的主频道。\n"
            "「央视 + 卫视都补」会把上游有、库里没有的省级卫视也一并建出来。\n"
            "不管选哪个，结果里都会告诉你「另有 N 个上游频道库里没有」。")
        opt.addWidget(self.cmb_new)
        opt.addStretch(1)
        root.addLayout(opt)

        self.hint = QLabel(
            "说明：同一来源的旧地址会被**更换**；上游没有的地址会**新增**；"
            "被你在源管理里手动改过的地址不会被覆盖，而是把上游新地址另存并置顶。"
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#5F5E5A;")
        root.addWidget(self.hint)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(6)
        root.addWidget(self.bar)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setPlaceholderText("点「开始同步」后，这里会显示进度与结果。")
        root.addWidget(self.out, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_run = QPushButton("开始同步")
        self.btn_run.setDefault(True)
        self.btn_run.clicked.connect(self.start_sync)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        btns.addWidget(self.btn_run)
        btns.addWidget(self.btn_close)
        root.addLayout(btns)

        self.lst.itemChanged.connect(self._refresh_state)
        self._refresh_state()

    # ---- 小工具 ------------------------------------------------------------
    def _check_all(self, checked: bool) -> None:
        for i in range(self.lst.count()):
            self.lst.item(i).setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _selected_ids(self):
        out = []
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    @Slot()
    def _refresh_state(self) -> None:
        self.btn_run.setEnabled(bool(self._selected_ids()) and not self._running)
        for w in (self.lst, self.cmb_mirror, self.ck_top, self.cmb_new,
                  self.btn_all, self.btn_none):
            w.setEnabled(not self._running)

    def _log(self, text: str) -> None:
        self.out.appendPlainText(text)

    # ---- 执行 --------------------------------------------------------------
    @Slot()
    def start_sync(self) -> None:
        if self._running:
            return
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请至少勾选一个来源。")
            return

        self._running = True
        self._refresh_state()
        self.out.clear()
        self.bar.setRange(0, 0)                 # 不确定进度：转圈
        self._log("开始同步 %d 个来源，加速方式：%s\n"
                  % (len(ids), self.cmb_mirror.currentText()))

        signals = _SyncSignals()
        signals.progress.connect(self._on_progress)
        signals.finished.connect(self._on_finished)
        _retain(signals)
        self._signals = signals

        QThreadPool.globalInstance().start(SyncTask(
            ids, self.cmb_mirror.currentData(), self.ck_top.isChecked(),
            self.cmb_new.currentData(), signals))
        log.info("已提交流程同步任务：%s（新增频道=%s）", ids, self.cmb_new.currentData())

    @Slot(str)
    def _on_progress(self, text: str) -> None:
        self._log(text)

    @Slot(object)
    def _on_finished(self, report) -> None:
        self._running = False
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        if self._signals is not None:
            _release(self._signals)
            self._signals = None
        self._refresh_state()

        self._log("")
        self._log(report.text())
        if report.ok:
            self._log("")
            self._log("同步完成。关闭本窗口后频道列表会自动刷新。")
            self.synced.emit(report)
        else:
            self._log("")
            self._log("所有来源都没能同步成功 —— 可换个加速方式再试一次。")

    def closeEvent(self, e):
        if self._running:
            QMessageBox.information(self, "正在同步", "同步还在进行中，请等它结束再关闭。")
            e.ignore()
            return
        if self._signals is not None:
            _release(self._signals)
            self._signals = None
        super().closeEvent(e)


def _mirror_hint(url: str) -> str:
    """在来源行尾标注"是否需要加速"，省得用户选了拉不动还不知道为什么。"""
    if url.startswith("https://raw.githubusercontent.com/"):
        return "〔需加速〕"
    return "〔可直连〕"
