# SPDX-License-Identifier: GPL-3.0-or-later
"""
主界面（Step 5 核心）。

布局：
  QMainWindow
    ├─ 左: QDockWidget("频道") —— 承载 QListView + ChannelListModel。
    │       标题栏带关闭 X（手动收起）；控制条 ☰ 按钮可随时展开/收起。
    └─ 中央: 右面板 = 视频容器(VideoView) + 控制条（小窗）。全屏时控制条隐藏，只留画面。

设计（电视直播语义）：
  - 不做暂停（直播一直播）。控制条仅含：
      ☰ 频道 / 频道名 / 📡 源管理 / 🔄 同步源 / ⟳ 刷新源 / 音量 / ⛶ 全屏。
  - 全屏 = 视频占满全屏、无控制栏、列表自动收起；退出全屏用 Esc。
  - 小窗默认显示列表与控制条，可手动收起。
  - 音量默认**最大**，并记住上次的值（存 settings 表）。
  - 「⟳ 刷新源」= 用当前频道现有源重新加载画面（断开重连 + 重新判定），画面卡住时用。

频道状态（本次改动：点击判定，而非启动全量扫描）：
  - 灰 unknown：本次会话没点过（含刚启动）。
  - 橙 checking：刚点下、正在探测（空心圆环）。
  - 绿 good：判定可播放（至少一个启用源探测通过）。
  - 红 bad：判定不可播放（启用源全挂 / 无源 / 播放中所有备用源耗尽）。
  - 判定在后台线程池做（ChannelChecker），结果经信号回主线程着色，UI 不卡。

播放链路（复用 Step 3/4）：
  - MPVPlayer 负责真实播放（vo 默认 gpu，wid 绑到 VideoView 窗口句柄）。
  - PlayerController 负责"频道绑定多备用源"的 failover 自动切源。
  - mpv 的 end-file 事件在独立线程触发 → notify_end → failover；
    所有跨线程回调经 Qt 信号转发到主线程。

频道管理：
  - 右键菜单：直播源管理… / 重新检测该频道。
  - 控制条「📡 源管理」：维护当前频道的源（增删改/置顶/优先级/测速）。
  - 控制条「🔄 同步源」：从多个 GitHub 清单源批量同步地址（支持内置加速器），
    同来源地址更换、上游新增则新增、被手动改过的源受保护并置顶。
  - 控制条「⟳ 刷新源」：用当前频道现有源重新加载画面（断开重连 + 重新判定）。
  - 不再有"隐藏频道"概念：列表始终展示库里的全部频道。
    （原「隐藏此频道 / 取消隐藏」右键项与控制条 👁 开关已按需求移除，
     `Channel.is_visible` 列仅作为老库兼容保留，不再参与任何筛选。）
"""
from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QEvent, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCursor, QAction
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDockWidget, QHBoxLayout, QLabel,
    QListView, QMainWindow, QMenu, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from liuhaitv.core.database import session_scope
from liuhaitv.core.models import Channel
from liuhaitv.core import settings as prefs
from liuhaitv.player.mpv_wrapper import MPVPlayer, PlayerController
from liuhaitv.ui.channel_list_model import (
    STATUS_BAD, STATUS_CHECKING, STATUS_GOOD, STATUS_UNKNOWN, ChannelDelegate,
    ChannelListView, ChannelListModel, RowType,
)
from liuhaitv.ui.check_worker import ChannelChecker
from liuhaitv.ui.source_manager import SourceManagerDialog
from liuhaitv.ui.sync_dialog import SyncDialog
from liuhaitv.ui.video_view import VideoView

log = logging.getLogger(__name__)

_EDGE_PX = 6          # 判定光标贴窗口左缘的距离（触发 dock 滑出）
_EDGE_HIDE_MS = 350   # 光标离开列表后延迟自动收起的毫秒数


class MainWindow(QMainWindow):
    # 跨线程信号：mpv 事件线程 -> 主线程（failover 回调用）
    playRequested = Signal(str)
    exhausted = Signal(int)   # 携带 channel_id（无则为 0），用于把该频道标红

    def __init__(self, model: ChannelListModel, vo: str = "gpu", parent=None):
        super().__init__(parent)
        self.model = model
        self._wid_bound = False
        self._fullscreen = False
        self._current_channel_id = None

        self.setWindowTitle("LiuHaiTV Desktop")
        self.resize(1120, 700)
        self.setMouseTracking(True)  # 边缘 hover 依赖鼠标移动事件

        # ---- 播放器链路 ----
        self.player = MPVPlayer(vo=vo, on_end_file=self._on_end_file)
        self.controller = PlayerController(
            on_play=lambda url, idx: self.playRequested.emit(url),
            on_exhausted=self._emit_exhausted,  # 跨线程信号 -> 主线程槽
        )
        self.playRequested.connect(self._do_play)
        self.exhausted.connect(self._on_exhausted)

        # ---- 点击判定（后台线程池）----
        self.checker = ChannelChecker(self)
        self.checker.finished.connect(self._on_check_finished)

        # ---- 中央：右面板(视频+控制条) ----
        self.video = VideoView()
        self.video.setMouseTracking(True)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.video, 1)
        rv.addWidget(self._build_control_bar())
        self.setCentralWidget(right)

        # ---- 左：频道列表 dock（可手动收起 / 全屏自动隐藏+边缘滑出）----
        self.dock = QDockWidget("频道", self)
        self.dock.setObjectName("channelsDock")
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.dock.setMinimumWidth(240)
        self.dock.setMaximumWidth(440)

        self.list = ChannelListView()
        self.list.setModel(model)
        self.list.setItemDelegate(ChannelDelegate(self.list))
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        # currentChanged 与 QAbstractItemView 的 protected 虚方法同名，
        # 在 PySide6 中直接 self.list.currentChanged 拿不到信号；走 selectionModel 的真信号
        self.list.selectionModel().currentChanged.connect(self._on_current_changed)
        self.dock.setWidget(self.list)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock)

        # 初始音量：默认**最大**，之后记住上次的值（存 settings 表）
        self._vol_timer = QTimer(self)
        self._vol_timer.setSingleShot(True)
        self._vol_timer.setInterval(400)          # 拖动时不写库，停手 400ms 再落盘
        self._vol_timer.timeout.connect(self._persist_volume)
        vol = prefs.get_volume()
        self.volume_slider.setValue(vol)          # 触发 _on_volume_changed -> 应用到播放器
        self.player.set_volume(vol)               # 兜底（滑块值未变时不发信号）

        # ---- 边缘自动隐藏（全屏） ----
        self._edge_timer = QTimer(self)
        self._edge_timer.setSingleShot(True)
        self._edge_timer.setInterval(_EDGE_HIDE_MS)
        self._edge_timer.timeout.connect(self._maybe_hide_edge_dock)
        QApplication.instance().installEventFilter(self)

    # ---- 控制条（小窗显示；全屏时隐藏） -----------------------------------
    def _build_control_bar(self) -> QWidget:
        self.control_bar = QWidget()
        bar = QHBoxLayout(self.control_bar)
        bar.setContentsMargins(8, 6, 8, 6)

        self.btn_panel = QPushButton("☰ 频道")
        self.btn_panel.clicked.connect(self._toggle_dock)

        self.lbl_channel = QLabel("请选择左侧频道")
        self.lbl_channel.setMinimumWidth(200)

        self.btn_sources = QPushButton("📡 源管理")
        self.btn_sources.setToolTip("管理当前频道的直播源（增删改 / 优先级 / 测速）")
        self.btn_sources.clicked.connect(self._open_source_manager)

        self.btn_sync = QPushButton("🔄 同步源")
        self.btn_sync.setToolTip(
            "从多个 GitHub 直播源清单同步地址：\n"
            "同来源的旧地址会被更换，上游新增的会补进来，\n"
            "被手动改过的地址受保护并置顶。支持内置 GitHub 加速。")
        self.btn_sync.clicked.connect(self._open_sync_dialog)

        self.btn_reload = QPushButton("⟳ 刷新源")
        self.btn_reload.setToolTip(
            "从当前频道的直播源重新加载画面：\n"
            "断开当前流并按优先级重新拉取（不换地址），随后重新判定可用性。\n"
            "画面卡住／黑屏时点它最省事。")
        self.btn_reload.clicked.connect(self._reload_current)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setMinimumWidth(120)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)

        self.btn_full = QPushButton("⛶ 全屏")
        self.btn_full.clicked.connect(self._toggle_fullscreen)

        bar.addWidget(self.btn_panel)
        bar.addWidget(self.lbl_channel)
        bar.addWidget(self.btn_sources)
        bar.addWidget(self.btn_sync)
        bar.addWidget(self.btn_reload)
        bar.addStretch(1)
        bar.addWidget(QLabel("音量"))
        bar.addWidget(self.volume_slider)
        bar.addSpacing(12)
        bar.addWidget(self.btn_full)
        return self.control_bar

    # ---- 换台（点击 = 换台 + 触发健康判定）--------------------------------
    @Slot("QModelIndex", "QModelIndex")
    def _on_current_changed(self, current, previous):
        if not current.isValid():
            return
        item = self.model.item_at(current.row())
        if item is None or item.row_type != RowType.CHANNEL:
            return

        self._current_channel_id = item.channel.id
        self.lbl_channel.setText(item.channel.name)

        if not item.sources:
            log.info("频道 %s 无源，切换失败", item.channel.name)
            self.lbl_channel.setText(item.channel.name + "（无源，请点📡添加）")
            self._set_status(item.channel.id, STATUS_BAD)
            return

        # 1) 先起播（不等检测，点击即出画面，手感优先）
        self.controller.play_channel(item.sources)
        self._record_watch(item.channel)
        # 2) 后台判定该频道当前能否播放 -> 绿/红
        self.check_channel(item.channel.id)

    def check_channel(self, channel_id: int):
        """点击判定入口：立刻置"检测中"，后台探测后置绿/红。"""
        if channel_id is None:
            return
        self._set_status(channel_id, STATUS_CHECKING)
        self.checker.check(channel_id)

    @Slot(int, bool, str)
    def _on_check_finished(self, channel_id: int, ok: bool, note: str):
        """检测线程回调（主线程）：着色 + 落库 last_status。"""
        self._set_status(channel_id, STATUS_GOOD if ok else STATUS_BAD)
        try:
            with session_scope() as s:
                c = s.get(Channel, channel_id)
                if c is not None:
                    c.last_status = "good" if ok else "bad"
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响界面
            log.warning("写入最后判定状态失败: %s", exc)
        if not ok:
            log.info("频道 %s 判定不可播放: %s", channel_id, note)
            if channel_id == self._current_channel_id:
                self.lbl_channel.setText(self.lbl_channel.text().split("（")[0]
                                         + "（不可播放）")

    def _set_status(self, channel_id: int, status: str):
        """更新列表颜色（含"检测中"过渡态）。"""
        try:
            self.model.set_status(channel_id, status)
        except Exception as exc:  # noqa: BLE001
            log.warning("更新频道状态失败: %s", exc)

    # ---- 源管理 -----------------------------------------------------------
    @Slot()
    def _open_source_manager(self):
        cid = self._selected_channel_id()
        if cid is None:
            log.info("未选中频道，无法打开源管理")
            self.lbl_channel.setText("请先在左侧选择一个频道，再点📡源管理")
            return
        self.open_source_manager(cid)

    def open_source_manager(self, channel_id: int):
        dlg = SourceManagerDialog(channel_id, parent=self)
        dlg.sourcesChanged.connect(self._on_sources_changed)
        dlg.exec()
        # 关闭后同步一次（源数量可能变了，列表 tooltip/播放源都要最新）
        self._reload_sources_into_model(channel_id)

    @Slot(int)
    def _on_sources_changed(self, channel_id: int):
        self._reload_sources_into_model(channel_id)
        # 源变了，旧的绿/红结论作废，回到"未判定"灰色
        self._set_status(channel_id, STATUS_UNKNOWN)

    def _reload_sources_into_model(self, channel_id: int):
        try:
            with session_scope() as s:
                ch = s.get(Channel, channel_id)
                if ch is None:
                    return
                sources = list(ch.sources)
                ch_id, ch_name = ch.id, ch.name
        except Exception as exc:  # noqa: BLE001
            log.warning("刷新频道源失败: %s", exc)
            return
        # 注意：模型里的 channel 对象要保持简单，这里用一个轻量壳传源列表
        try:
            self.model.update_sources(ch_id, sources)
        except Exception as exc:  # noqa: BLE001
            log.warning("更新模型源列表失败: %s", exc)

    # ---- 同步直播源（批量）------------------------------------------------
    @Slot()
    def _open_sync_dialog(self):
        """打开「同步直播源」弹窗：勾来源、选加速方式、后台同步。"""
        dlg = SyncDialog(parent=self)
        dlg.synced.connect(self._on_synced)
        dlg.exec()

    @Slot(object)
    def _on_synced(self, report) -> None:
        """
        同步改动了库里的源 → 列表与颜色都要重置。
        旧的红/绿结论是针对旧地址做的，地址换了就不再可信，统一回到"未判定"灰。
        """
        try:
            self.model.reset_statuses()
        except Exception as exc:  # noqa: BLE001
            log.warning("重置频道状态失败: %s", exc)
        self.reload_channels()
        try:
            t = report.totals()
            log.info("同步完成：更换 %d、新增 %d、删除 %d、保护手改 %d，涉及 %d 个频道",
                     t["replaced"], t["added"], t["removed"], t["kept_manual"],
                     t["channels_touched"])
        except Exception as exc:  # noqa: BLE001
            log.debug("同步统计输出失败: %s", exc)
        self.lbl_channel.setText("同步完成，正在播放的频道请重新点击以重新判定")

    # ---- 音量（默认最大 + 记住上次）----------------------------------------
    @Slot(int)
    def _on_volume_changed(self, value: int):
        """滑块变动：立刻应用到播放器；停顿 400ms 后再落盘（拖动时别高频写库）。"""
        try:
            self.player.set_volume(value)
        except Exception as exc:  # noqa: BLE001
            log.debug("设置音量失败: %s", exc)
        self._vol_timer.start()

    @Slot()
    def _persist_volume(self):
        """把当前音量记进 settings 表，下次启动沿用。"""
        try:
            prefs.set_volume(int(self.volume_slider.value()))
        except Exception as exc:  # noqa: BLE001
            log.debug("保存音量失败: %s", exc)

    # ---- 从当前直播源重新加载画面 ------------------------------------------
    def _load_channel_sources(self, channel_id):
        """从库里取某频道当前的源（按优先级）与频道名。"""
        with session_scope() as s:
            ch = s.get(Channel, channel_id)
            if ch is None:
                return None, []
            return ch.name, list(ch.sources)

    @Slot()
    def _reload_current(self):
        """
        「⟳ 刷新源」：用**当前频道现有的源**重新加载画面。

        不换地址、不重新搜刮 —— 只是断开旧流、按优先级重新拉取（failover 会自然接管），
        随后重新判定一次可用性，把列表的颜色刷新掉。画面卡住/黑屏时点它最省事。
        """
        cid = self._current_channel_id or self._selected_channel_id()
        if cid is None:
            self.lbl_channel.setText("请先在左侧选择一个频道，再点 ⟳ 刷新源")
            return
        try:
            name, sources = self._load_channel_sources(cid)
        except Exception as exc:  # noqa: BLE001
            log.exception("读取频道 %s 的源失败: %s", cid, exc)
            return
        if not sources:
            self._set_status(cid, STATUS_BAD)
            self.lbl_channel.setText(f"{name or '该频道'}（无源，请点📡添加）")
            return

        self._current_channel_id = cid
        try:
            self.controller.stop()          # 先断开旧流，避免 mpv 还挂着上一个地址
        except Exception as exc:  # noqa: BLE001
            log.debug("停止旧流已忽略: %s", exc)
        self.controller.play_channel(sources)   # 重新起播（内部按优先级 + 健康度排序）
        self.check_channel(cid)                 # 重新判定 -> 检测中 -> 绿/红
        self.lbl_channel.setText(f"{name}（已重新加载）")
        log.info("刷新源：频道 %s（%d 个源）重新加载", cid, len(sources))

    def _selected_channel_id(self):
        idx = self.list.currentIndex()
        if not idx.isValid():
            return None
        item = self.model.item_at(idx.row())
        if item is None or item.row_type != RowType.CHANNEL:
            return None
        return item.channel.id

    # ---- 右键菜单：源管理 / 重新检测 ---------------------------------------
    @Slot("QPoint")
    def _on_context_menu(self, pos):
        idx = self.list.indexAt(pos)
        if not idx.isValid():
            return
        item = self.model.item_at(idx.row())
        if item is None or item.row_type != RowType.CHANNEL:
            return
        cid = item.channel.id
        menu = QMenu(self)
        act_src = QAction("📡 直播源管理…", menu)
        act_test = QAction("↻ 重新检测该频道", menu)
        act_src.triggered.connect(lambda: self.open_source_manager(cid))
        act_test.triggered.connect(lambda: self.check_channel(cid))
        menu.addAction(act_src)
        menu.addAction(act_test)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def reload_channels(self):
        """重新加载全部频道（保留会话内已判定的颜色与当前选中）。"""
        from sqlalchemy.orm import selectinload
        try:
            with session_scope() as s:
                channels = (s.query(Channel).options(selectinload(Channel.sources))
                            .order_by(Channel.group_name, Channel.sort_order).all())
        except Exception as exc:  # noqa: BLE001
            log.exception("重新加载频道失败: %s", exc)
            return
        self.model.reload(channels)
        # 恢复选中（若当前频道仍在列表中）
        if self._current_channel_id is not None:
            row = self.model.row_of_channel(self._current_channel_id)
            if row >= 0:
                self.list.setCurrentIndex(self.model.index(row, 0))

    # ---- 跨线程转发 / 播放回调（主线程）------------------------------------
    @Slot(str)
    def _do_play(self, url: str):
        self.player.play(url)

    def _emit_exhausted(self):
        """在 mpv 事件线程调用：把"备用源全挂"转成主线程信号，携带当前频道。"""
        self.exhausted.emit(int(self._current_channel_id or 0))

    @Slot(int)
    def _on_exhausted(self, channel_id: int):
        """所有备用源都失败 -> 该频道标红（真实播放结果比探测更权威）。"""
        if channel_id:
            self._set_status(channel_id, STATUS_BAD)
            try:
                with session_scope() as s:
                    c = s.get(Channel, channel_id)
                    if c is not None:
                        c.last_status = "bad"
            except Exception as exc:  # noqa: BLE001
                log.warning("写入失败状态出错: %s", exc)
            if channel_id == self._current_channel_id:
                self.lbl_channel.setText(
                    self.lbl_channel.text().split("（")[0] + "（备用源全失败）"
                )

    # ---- 交互 -------------------------------------------------------------
    @Slot()
    def _toggle_dock(self):
        """手动展开/收起频道列表（小窗与全屏都可用）。"""
        self.dock.setVisible(not self.dock.isVisible())

    @Slot()
    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self.showFullScreen()
            self.control_bar.hide()   # 全屏只留画面，去除控制栏
            self.btn_full.setText("⛶ 退出全屏")
            self.dock.hide()          # 全屏自动收起列表
        else:
            self.showNormal()
            self.control_bar.show()   # 退出全屏恢复控制栏
            self.btn_full.setText("⛶ 全屏")
            self.dock.show()          # 退出全屏恢复

    # ---- 全屏边缘 hover：滑出 / 自动收回 -----------------------------------
    def eventFilter(self, obj, event):
        if self._fullscreen and event.type() == QEvent.Type.MouseMove:
            self._handle_edge_hover()
        return super().eventFilter(obj, event)

    def _handle_edge_hover(self):
        geo = self.frameGeometry()
        cur = QCursor.pos()
        inside_win = (geo.left() <= cur.x() <= geo.right()
                      and geo.top() <= cur.y() <= geo.bottom())
        if not inside_win:
            return
        if cur.x() - geo.left() <= _EDGE_PX:
            # 光标贴窗口左缘 -> 滑出列表
            if not self.dock.isVisible():
                self.dock.show()
                self.dock.raise_()
        elif self.dock.isVisible() and cur.x() > self.dock.geometry().right() + 8:
            # 光标移出列表右界 -> 安排延迟自动收回
            if not self._edge_timer.isActive():
                self._edge_timer.start()

    @Slot()
    def _maybe_hide_edge_dock(self):
        cur = QCursor.pos()
        if self._fullscreen and self.dock.isVisible() \
                and cur.x() > self.dock.geometry().right() + 8:
            self.dock.hide()

    # ---- 播放/生命周期 -----------------------------------------------------
    def _on_end_file(self, reason: str):
        # 在 mpv 事件线程调用；failover 为纯逻辑，其回调经 Qt 信号转发回主线程
        self.controller.notify_end(reason)

    def _record_watch(self, channel):
        try:
            with session_scope() as s:
                c = s.get(Channel, channel.id)
                if c is not None:
                    c.last_watched_at = datetime.now()
                    c.played_count = (c.played_count or 0) + 1
        except Exception as exc:  # noqa: BLE001 - 记忆失败不影响播放
            log.warning("记录观看失败: %s", exc)

    def showEvent(self, e):
        super().showEvent(e)
        if not self._wid_bound:
            self._wid_bound = True
            self.player.set_wid(self.video.native_id())

    def closeEvent(self, e):
        try:
            self.controller.stop()
            self.player.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("关闭播放器已忽略: %s", exc)
        super().closeEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and self._fullscreen:
            self._toggle_fullscreen()   # 全屏无控制栏，Esc 退出全屏
            e.accept()
            return
        super().keyPressEvent(e)
