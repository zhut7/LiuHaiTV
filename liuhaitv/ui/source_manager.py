# SPDX-License-Identifier: GPL-3.0-or-later
"""
直播源管理弹窗（Step 5 新增）。

一个频道的全部直播源都在这里维护：
  ┌ 源表格：优先级 / 启用 / 地址 / 来源 / 类型 / 延迟 / 最近检测 / 状态
  ├ 增：添加（手填地址）  ·  导入 M3U（远程链接或本地文件，按频道名自动勾选）
  ├ 删：删除选中
  ├ 改：双击行编辑（地址 / 来源 / 启用）
  ├ 优先级：上移 / 下移（自动重排为 0,1,2… 越靠前越优先播放）
  └ 测试：选中行探活，回填延迟与状态

交互约定：
  - 所有操作**立即写库**（无"确定/取消"），点关闭即完成；这是 IPTV 类工具的常见手感，
    也避免用户以为点了取消其实已改内存数据。
  - 启用 = 参与播放与检测；禁用的源在 failover 时会被跳过。
  - 每次写库后 emit sourcesChanged(channel_id)，主窗口据此刷新频道行的源列表。

错误处理：一切 try-except + logging；单行失败不影响整体，失败时弹窗提示原因。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import List, Optional

import httpx

import liuhaitv.config as cfg
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from liuhaitv.core.database import session_scope
from liuhaitv.core.m3u_parser import parse_m3u
from liuhaitv.core.models import Channel, Source
from liuhaitv.ui.check_worker import SourceProber

log = logging.getLogger(__name__)

# 列定义
_COL_PRIO, _COL_ENABLE, _COL_URL, _COL_ORIGIN, _COL_KIND, _COL_LAT, _COL_CHECKED, _COL_STATUS = range(8)
_HEADERS = ["优先级", "启用", "地址", "来源", "类型", "延迟(ms)", "最近检测", "状态"]


def infer_kind(url: str) -> str:
    """按扩展名推断流类型（与 Step 2 入库口径保持一致）。"""
    low = (url or "").split("?")[0].lower()
    if low.endswith(".m3u8"):
        return "hls"
    if low.endswith(".flv"):
        return "flv"
    if low.endswith(".ts"):
        return "ts"
    if low.startswith("rtmp"):
        return "rtmp"
    if low.endswith(".m3u"):
        return "m3u"
    return "http"


def infer_protocol(url: str) -> str:
    """按地址推断协议族：ipv6 / ipv4 / domain。"""
    u = (url or "").lower()
    try:
        host = u.split("://", 1)[1].split("/", 1)[0]
        host = host.split("@")[-1]
        host = host.split(":")[0] if not host.startswith("[") else host.split("]")[0][1:]
    except Exception:  # noqa: BLE001 - 解析失败一律当域
        return "domain"
    if not host:
        return "domain"
    if ":" in host:
        return "ipv6"
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return "ipv4"
    return "domain"


def _norm_name(name: str) -> str:
    """频道名归一化，用于 M3U 条目匹配（去掉大小写/空格/常见后缀干扰）。"""
    s = (name or "").strip().lower()
    for junk in ("高清", "标清", "超清", "hd", "sd", "-", "_", " ", "（", "）", "(", ")"):
        s = s.replace(junk, "")
    return s


# ===========================================================================
# 单个源的添加/编辑对话框
# ===========================================================================
class SourceEditDialog(QDialog):
    """添加或编辑一条直播源。"""

    def __init__(self, url: str = "", origin: str = "user", enabled: bool = True,
                 title: str = "添加直播源", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)

        self.ed_url = QLineEdit(url)
        self.ed_url.setPlaceholderText("https://example.com/live/cctv1.m3u8")
        self.ed_origin = QLineEdit(origin)
        self.ed_origin.setPlaceholderText("user")
        self.ck_enabled = QCheckBox("启用（参与播放与检测）")
        self.ck_enabled.setChecked(bool(enabled))
        self.lbl_kind = QLabel(infer_kind(url))
        self.lbl_probe = QLabel("—")
        self.lbl_probe.setWordWrap(True)

        self.ed_url.textChanged.connect(self._on_url_changed)

        form = QFormLayout()
        form.addRow("地址", self.ed_url)
        form.addRow("来源", self.ed_origin)
        form.addRow("类型", self.lbl_kind)
        form.addRow("", self.ck_enabled)

        btn_test = QPushButton("测试连接")
        btn_test.clicked.connect(self._on_test)
        row = QHBoxLayout()
        row.addWidget(btn_test)
        row.addWidget(self.lbl_probe, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(row)
        lay.addWidget(self.buttons)

        # 后台探测（复用主流程的探活器）
        self._prober = SourceProber(self)
        self._prober.finished.connect(self._on_probe_done)

    def _on_url_changed(self, text: str):
        self.lbl_kind.setText(infer_kind(text))

    def _on_test(self):
        url = self.ed_url.text().strip()
        if not url:
            self.lbl_probe.setText("请先填写地址")
            return
        self.lbl_probe.setText("测试中…")
        self._prober.probe(url)

    @Slot(str, bool, object, str)
    def _on_probe_done(self, url: str, ok: bool, latency, note: str):
        if url != self.ed_url.text().strip():
            return  # 结果属于旧地址，忽略
        if ok:
            self.lbl_probe.setText(f"✓ 可播放（延迟 {latency} ms）")
        else:
            self.lbl_probe.setText(f"✗ 不可用：{note}")

    def _on_accept(self):
        if len(self.ed_url.text().strip()) < 8 or "://" not in self.ed_url.text():
            QMessageBox.warning(self, "地址无效", "请填写完整的流地址（含 http:// 或 https://）。")
            return
        self.accept()

    def values(self):
        url = self.ed_url.text().strip()
        origin = self.ed_origin.text().strip() or "user"
        return url, origin, self.ck_enabled.isChecked()


# ===========================================================================
# M3U 导入对话框
# ===========================================================================
class M3UImportDialog(QDialog):
    """
    从远程 M3U 链接 / 本地 M3U 文件导入源。

    流程：拉取文本 -> 解析 -> 按频道名自动勾选同名条目（也可手动勾选任意条目）。
    """

    def __init__(self, channel_name: str, parent=None):
        super().__init__(parent)
        self.channel_name = channel_name or ""
        self.setWindowTitle(f"导入 M3U —— {self.channel_name}")
        self.resize(720, 520)

        self.ed_src = QLineEdit()
        self.ed_src.setPlaceholderText("M3U 链接（https://.../xxx.m3u）或本地文件绝对路径")
        btn_file = QPushButton("选择文件…")
        btn_file.clicked.connect(self._pick_file)
        btn_load = QPushButton("加载")
        btn_load.clicked.connect(self._load)
        row = QHBoxLayout()
        row.addWidget(QLabel("来源"))
        row.addWidget(self.ed_src, 1)
        row.addWidget(btn_file)
        row.addWidget(btn_load)

        self.ck_only_match = QCheckBox("只显示与本频道名称相近的条目")
        self.ck_only_match.setChecked(True)
        self.ck_only_match.stateChanged.connect(lambda _=0: self._render())

        self.listw = QListWidget()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入选中")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        self.lbl_info = QLabel("填入 M3U 链接或选择本地文件后点「加载」。")
        self.lbl_info.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(self.ck_only_match)
        lay.addWidget(self.listw, 1)
        lay.addWidget(self.lbl_info)
        lay.addWidget(self.buttons)

        self._entries = []          # List[ParsedEntry]
        self._matched_cache = set()  # 命中的 entry 下标

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 M3U 文件", "", "M3U 清单 (*.m3u *.m3u8 *.txt);;所有文件 (*)"
        )
        if path:
            self.ed_src.setText(path)

    def _load(self):
        src = self.ed_src.text().strip()
        if not src:
            QMessageBox.information(self, "提示", "请先填写 M3U 链接或选择本地文件。")
            return
        try:
            if os.path.isfile(src):
                with open(src, "rb") as fh:
                    payload = fh.read()
                origin = "user-file"
            elif src.startswith(("http://", "https://")):
                resp = httpx.get(src, headers=cfg.HTTP_HEADERS,
                                 timeout=cfg.SCRAPE_TIMEOUT, follow_redirects=True)
                resp.raise_for_status()
                payload = resp.content
                origin = "user-url"
            else:
                QMessageBox.warning(self, "无效来源", "既不是本地文件也不是 http(s) 链接。")
                return
            self._entries = parse_m3u(payload, origin=origin)
        except Exception as exc:  # noqa: BLE001 - 网络/文件错误都要给出可读提示
            log.exception("加载 M3U 失败: %s", exc)
            QMessageBox.critical(self, "加载失败", f"无法读取该 M3U：\n{exc}")
            return

        # 名称匹配
        target = _norm_name(self.channel_name)
        self._matched_cache = {
            i for i, e in enumerate(self._entries)
            if target and (target in _norm_name(e.name) or _norm_name(e.name) in target)
        }
        self._render()

    def _render(self):
        self.listw.clear()
        only_match = self.ck_only_match.isChecked()
        shown = 0
        for i, e in enumerate(self._entries):
            hit = i in self._matched_cache
            if only_match and not hit:
                continue
            urls = e.all_urls()
            if not urls:
                continue
            item = QListWidgetItem(f"{e.name}    [{len(urls)} 条地址]    {urls[0][:70]}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if hit else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.listw.addItem(item)
            shown += 1
        self.lbl_info.setText(
            f"解析到 {len(self._entries)} 个条目，当前显示 {shown} 个；"
            f"名称命中 {len(self._matched_cache)} 个已默认勾选。"
        )

    def selected_urls(self) -> List[str]:
        out: List[str] = []
        for row in range(self.listw.count()):
            it = self.listw.item(row)
            if it.checkState() != Qt.CheckState.Checked:
                continue
            idx = it.data(Qt.ItemDataRole.UserRole)
            try:
                entry = self._entries[int(idx)]
            except Exception:  # noqa: BLE001
                continue
            for u in entry.all_urls():
                if u and u not in out:
                    out.append(u)
        return out


# ===========================================================================
# 直播源管理主弹窗
# ===========================================================================
class SourceManagerDialog(QDialog):
    """某频道直播源的增删改 + 优先级管理。所有改动立即落库。"""

    sourcesChanged = Signal(int)   # channel_id：源列表发生变化（主窗口据此刷新模型）

    def __init__(self, channel_id: int, parent=None):
        super().__init__(parent)
        self.channel_id = int(channel_id)
        self._loading = False
        self._rows: List[int] = []     # 表格行 -> Source.id
        self._pending: dict = {}       # url -> Source.id（正在测试的源）
        self.channel_name = ""

        self.setWindowTitle("直播源管理")
        self.resize(940, 520)

        self.lbl_head = QLabel()
        self.tbl = QTableWidget(0, len(_HEADERS))
        self.tbl.setHorizontalHeaderLabels(_HEADERS)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(_COL_URL, QHeaderView.ResizeMode.Stretch)
        for c in (_COL_PRIO, _COL_ENABLE, _COL_KIND, _COL_LAT, _COL_CHECKED, _COL_STATUS):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_ORIGIN, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.doubleClicked.connect(lambda _i: self._edit())
        self.tbl.itemChanged.connect(self._on_item_changed)

        self.btn_add = QPushButton("添加")
        self.btn_import = QPushButton("导入 M3U…")
        self.btn_edit = QPushButton("编辑")
        self.btn_del = QPushButton("删除")
        self.btn_top = QPushButton("⤒ 置顶")
        self.btn_up = QPushButton("▲ 上移")
        self.btn_down = QPushButton("▼ 下移")
        self.btn_test = QPushButton("测试选中")
        self.btn_close = QPushButton("关闭")
        self.btn_top.setToolTip("把选中的源排到第一位（优先级 0），其余顺延 —— 它会被最先尝试")
        self.btn_add.clicked.connect(self._add)
        self.btn_import.clicked.connect(self._import_m3u)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_del.clicked.connect(self._delete)
        self.btn_top.clicked.connect(self._move_top)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        self.btn_test.clicked.connect(self._probe_selected)
        self.btn_close.clicked.connect(self.accept)

        btns = QHBoxLayout()
        for b in (self.btn_add, self.btn_import, self.btn_edit, self.btn_del,
                  self.btn_top, self.btn_up, self.btn_down, self.btn_test):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(self.lbl_head)
        lay.addWidget(self.tbl, 1)
        lay.addWidget(QLabel(
            "提示：优先级数字越小越先播放（可「置顶」一键排到第一位）；"
            "禁用的源不参与播放与检测。双击行可编辑。"))
        lay.addLayout(btns)

        self._prober = SourceProber(self)
        self._prober.finished.connect(self._on_probe_done)

        self._load()

    # ---- 数据加载 / 渲染 ---------------------------------------------------
    def _load(self):
        """从库里读该频道的源（按优先级）并刷新表格。"""
        try:
            with session_scope() as s:
                ch = s.get(Channel, self.channel_id)
                if ch is None:
                    log.warning("频道 %s 不存在", self.channel_id)
                    return
                self.channel_name = ch.name
                rows = [
                    (src.id, src.url, src.origin, src.kind, src.protocol,
                     bool(src.is_enabled), src.latency_ms, src.checked_at, bool(src.is_healthy))
                    for src in sorted(ch.sources, key=lambda x: (x.default_priority, x.id))
                ]
        except Exception as exc:  # noqa: BLE001
            log.exception("读取频道 %s 的源失败: %s", self.channel_id, exc)
            QMessageBox.critical(self, "读取失败", f"无法读取直播源：\n{exc}")
            return

        self.setWindowTitle(f"直播源管理 —— {self.channel_name}")
        self.lbl_head.setText(
            f"频道：<b>{self.channel_name}</b>　共 {len(rows)} 个源"
            f"（启用 {sum(1 for r in rows if r[5])} 个）"
        )
        self._render(rows)

    def _render(self, rows):
        self._loading = True
        try:
            self.tbl.setRowCount(0)
            self._rows = []
            for prio, (sid, url, origin, kind, proto, enabled, lat, checked, healthy) in enumerate(rows):
                r = self.tbl.rowCount()
                self.tbl.insertRow(r)
                self._rows.append(sid)

                it_prio = QTableWidgetItem(str(prio))
                it_prio.setData(Qt.ItemDataRole.UserRole, sid)
                it_prio.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.tbl.setItem(r, _COL_PRIO, it_prio)

                it_en = QTableWidgetItem()
                it_en.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                               | Qt.ItemFlag.ItemIsSelectable)
                it_en.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
                it_en.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.tbl.setItem(r, _COL_ENABLE, it_en)

                it_url = QTableWidgetItem(url)
                it_url.setToolTip(f"{url}\n协议: {proto}")
                self.tbl.setItem(r, _COL_URL, it_url)

                self.tbl.setItem(r, _COL_ORIGIN, QTableWidgetItem(origin or ""))
                self.tbl.setItem(r, _COL_KIND, QTableWidgetItem(f"{kind}/{proto}"))
                self.tbl.setItem(r, _COL_LAT, QTableWidgetItem(
                    "" if lat is None else str(lat)))
                self.tbl.setItem(r, _COL_CHECKED, QTableWidgetItem(
                    checked.strftime("%m-%d %H:%M") if isinstance(checked, datetime) else "—"))
                st = QTableWidgetItem(status_text(checked, healthy))
                st.setForeground(status_color(checked, healthy))
                st.setToolTip(status_tooltip(checked, healthy, lat))
                self.tbl.setItem(r, _COL_STATUS, st)
        finally:
            self._loading = False

    @staticmethod
    def _status_text(checked, healthy) -> str:
        """兼容旧调用：等价于模块级 status_text()。"""
        return status_text(checked, healthy)

    # ---- 表格交互 ----------------------------------------------------------
    @Slot(int)
    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading or item.column() != _COL_ENABLE:
            return
        sid = self._row_source_id(item.row())
        if sid is None:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        try:
            with session_scope() as s:
                src = s.get(Source, sid)
                if src is not None:
                    src.is_enabled = enabled
        except Exception as exc:  # noqa: BLE001
            log.exception("切换源启用状态失败: %s", exc)
            QMessageBox.warning(self, "保存失败", f"无法保存启用状态：\n{exc}")
            return
        log.info("源 #%s 启用状态 -> %s", sid, enabled)
        self._emit_changed()
        # 延迟到事件循环下一拍再重建表格：避免在 itemChanged 信号栈内 setRowCount(0)
        # 把正在发信号的那个 QTableWidgetItem 直接删掉（Qt 下可能崩溃）
        QTimer.singleShot(0, self._load)

    def _row_source_id(self, row: int) -> Optional[int]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _selected_ids(self) -> List[int]:
        ids = []
        for idx in self.tbl.selectionModel().selectedRows() if self.tbl.selectionModel() else []:
            sid = self._row_source_id(idx.row())
            if sid is not None and sid not in ids:
                ids.append(sid)
        return ids

    def _emit_changed(self):
        self.sourcesChanged.emit(self.channel_id)

    # ---- 增 / 改 / 删 ------------------------------------------------------
    def _add(self):
        dlg = SourceEditDialog(title=f"添加直播源 —— {self.channel_name}", parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        url, origin, enabled = dlg.values()
        try:
            with session_scope() as s:
                max_prio = max((x.default_priority for x in
                                s.query(Source).filter(Source.channel_id == self.channel_id)), default=-1)
                s.add(Source(
                    channel_id=self.channel_id, url=url, origin=origin,
                    source_id=f"user:{self.channel_id}:{int(datetime.now().timestamp() * 1000) % 1000000}",
                    kind=infer_kind(url), protocol=infer_protocol(url),
                    default_priority=int(max_prio) + 1, is_enabled=bool(enabled),
                    is_healthy=False,
                ))
        except Exception as exc:  # noqa: BLE001
            log.exception("添加源失败: %s", exc)
            QMessageBox.critical(self, "添加失败", f"无法写入数据库：\n{exc}")
            return
        log.info("已为频道 %s 添加源: %s", self.channel_id, url[:80])
        self._load()
        self._emit_changed()

    def _edit(self):
        ids = self._selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, "提示", "请选中一行后再编辑。")
            return
        sid = ids[0]
        try:
            with session_scope() as s:
                src = s.get(Source, sid)
                if src is None:
                    return
                url, origin, enabled = src.url, src.origin, bool(src.is_enabled)
        except Exception as exc:  # noqa: BLE001
            log.exception("读取源失败: %s", exc)
            return

        dlg = SourceEditDialog(url=url, origin=origin, enabled=enabled,
                               title="编辑直播源", parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        url, origin, enabled = dlg.values()
        try:
            with session_scope() as s:
                src = s.get(Source, sid)
                if src is None:
                    return
                changed_url = (src.url != url)
                src.url = url
                src.origin = origin
                src.is_enabled = bool(enabled)
                src.kind = infer_kind(url)
                src.protocol = infer_protocol(url)
                if changed_url:
                    # 地址变了，旧的健康结论作废
                    src.is_healthy = False
                    src.latency_ms = None
                    src.checked_at = None
                    # 标记为"用户手动改过"：同步直播源时不会覆盖它，
                    # 而是把上游来的新地址另存一条并置顶。
                    src.is_user_edited = True
        except Exception as exc:  # noqa: BLE001
            log.exception("保存源失败: %s", exc)
            QMessageBox.critical(self, "保存失败", f"无法保存：\n{exc}")
            return
        self._load()
        self._emit_changed()

    def _delete(self):
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选中要删除的源。")
            return
        ans = QMessageBox.question(
            self, "确认删除",
            f"确定删除选中的 {len(ids)} 个直播源？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            with session_scope() as s:
                for sid in ids:
                    src = s.get(Source, sid)
                    if src is not None:
                        s.delete(src)
        except Exception as exc:  # noqa: BLE001
            log.exception("删除源失败: %s", exc)
            QMessageBox.critical(self, "删除失败", f"无法删除：\n{exc}")
            return
        log.info("已删除源: %s", ids)
        self._load()          # 先拿到删除后的真实行序
        self._renumber()      # 再按新行序压紧优先级 0,1,2…
        self._load()
        self._emit_changed()

    # ---- 优先级 ------------------------------------------------------------
    def _move(self, delta: int):
        ids = self._selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, "提示", "请选中一行后再上移/下移。")
            return
        sid = ids[0]
        try:
            order = list(self._rows)
            i = order.index(sid)
            j = i + delta
            if j < 0 or j >= len(order):
                return
            order[i], order[j] = order[j], order[i]
            with session_scope() as s:
                for prio, xid in enumerate(order):
                    src = s.get(Source, xid)
                    if src is not None:
                        src.default_priority = prio
        except Exception as exc:  # noqa: BLE001
            log.exception("调整优先级失败: %s", exc)
            QMessageBox.warning(self, "调整失败", f"无法调整优先级：\n{exc}")
            return
        self._load()
        self._select_source(sid)
        self._emit_changed()

    def _move_top(self):
        """把选中的源**置顶**（优先级 0），其余按当前顺序顺延为 1,2,3…"""
        ids = self._selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, "提示", "请选中一行后再置顶。")
            return
        sid = ids[0]
        try:
            order = list(self._rows)
            if sid not in order:
                return
            order.remove(sid)
            order.insert(0, sid)
            with session_scope() as s:
                for prio, xid in enumerate(order):
                    src = s.get(Source, xid)
                    if src is not None:
                        src.default_priority = prio
        except Exception as exc:  # noqa: BLE001
            log.exception("置顶失败: %s", exc)
            QMessageBox.warning(self, "置顶失败", f"无法置顶：\n{exc}")
            return
        log.info("源 #%s 已置顶（优先级 0）", sid)
        self._load()
        self._select_source(sid)
        self._emit_changed()

    def _renumber(self):
        """按当前行序把优先级重排为 0,1,2…（删除后消除空洞）。"""
        try:
            with session_scope() as s:
                for prio, sid in enumerate(self._rows):
                    src = s.get(Source, sid)
                    if src is not None:
                        src.default_priority = prio
        except Exception as exc:  # noqa: BLE001
            log.warning("重排优先级失败: %s", exc)

    def _select_source(self, sid: int):
        try:
            row = self._rows.index(sid)
        except ValueError:
            return
        self.tbl.selectRow(row)

    # ---- 测试 --------------------------------------------------------------
    def _probe_selected(self):
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选中要测试的源（可多选）。")
            return
        self._pending = {}
        for sid in ids:
            try:
                with session_scope() as s:
                    src = s.get(Source, sid)
                    url = src.url if src is not None else None
            except Exception as exc:  # noqa: BLE001
                log.warning("读取源 %s 失败: %s", sid, exc)
                continue
            if url:
                self._pending[url] = sid
                self._mark_row_status(sid, "测试中…", neutral=True)
                self._prober.probe(url)
        if not self._pending:
            return
        self.btn_test.setEnabled(False)
        self.btn_test.setText(f"测试中（{len(self._pending)}）…")

    def _mark_row_status(self, sid: int, text: str, neutral: bool = False):
        """临时改某行状态文字（如"测试中…"），不动库、等回调后 _load() 统一重绘。"""
        try:
            row = self._rows.index(sid)
        except ValueError:
            return
        item = self.tbl.item(row, _COL_STATUS)
        if item is not None:
            item.setText(text)
            if neutral:
                from PySide6.QtGui import QBrush, QColor
                item.setForeground(QBrush(QColor(*_COLOR_UNKNOWN)))

    @Slot(str, bool, object, str)
    def _on_probe_done(self, url: str, ok: bool, latency, note: str):
        """探测回调（主线程）：写回 DB 并刷新该行。"""
        sid = getattr(self, "_pending", {}).pop(url, None)
        try:
            with session_scope() as s:
                src = (s.get(Source, sid) if sid is not None else
                       s.query(Source)
                       .filter(Source.channel_id == self.channel_id, Source.url == url)
                       .first())
                if src is not None:
                    sid = src.id
                    src.is_healthy = bool(ok)
                    src.latency_ms = int(latency) if latency is not None else None
                    src.checked_at = datetime.now()
        except Exception as exc:  # noqa: BLE001
            log.exception("写回测试结果失败: %s", exc)
        log.info("源测试 %s -> %s (延迟=%s) %s", url[:60], "可用" if ok else "不可用",
                 latency, note)
        self._load()
        if sid is not None:
            self._select_source(sid)
        if not getattr(self, "_pending", None):
            self.btn_test.setEnabled(True)
            self.btn_test.setText("测试选中")

    # ---- M3U 导入 ----------------------------------------------------------
    def _import_m3u(self):
        dlg = M3UImportDialog(self.channel_name, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        urls = dlg.selected_urls()
        if not urls:
            QMessageBox.information(self, "提示", "没有勾选任何条目。")
            return
        added = skipped = 0
        try:
            with session_scope() as s:
                exist = {u for (u,) in s.query(Source.url)
                         .filter(Source.channel_id == self.channel_id)}
                max_prio = max((x.default_priority for x in
                                s.query(Source).filter(Source.channel_id == self.channel_id)),
                               default=-1)
                for url in urls:
                    if url in exist:
                        skipped += 1
                        continue
                    max_prio += 1
                    s.add(Source(
                        channel_id=self.channel_id, url=url,
                        origin="user-import",
                        source_id=f"import:{self.channel_id}:{max_prio}",
                        kind=infer_kind(url), protocol=infer_protocol(url),
                        default_priority=int(max_prio), is_enabled=True, is_healthy=False,
                    ))
                    added += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("导入 M3U 失败: %s", exc)
            QMessageBox.critical(self, "导入失败", f"写入数据库出错：\n{exc}")
            return
        log.info("频道 %s 导入 M3U: 新增 %d，跳过重复 %d", self.channel_id, added, skipped)
        QMessageBox.information(
            self, "导入完成",
            f"新增 {added} 个源，跳过重复 {skipped} 个。\n建议点「测试选中」筛掉不可用的源。"
        )
        self._load()
        if added:
            self._emit_changed()


def _status_color(healthy: bool):
    """（旧接口，保留兼容）按健康与否取颜色。新代码请用 status_color()。"""
    from PySide6.QtGui import QBrush, QColor
    return QBrush(QColor(0, 150, 50) if healthy else QColor(210, 40, 40))


# 状态配色：未检测=灰 / 可播放=绿 / 未通过=红
_COLOR_UNKNOWN = (150, 152, 158)
_COLOR_GOOD = (0, 150, 50)
_COLOR_BAD = (210, 40, 40)


def status_text(checked, healthy) -> str:
    """
    源状态文案（三态）：
      - 未检测：`checked_at` 为空（从未测过）
      - 可播放：测过且可用
      - 未通过：测过但不可用

    注意判定"测过没有"必须用 **`checked_at`**，不能用 `latency_ms`：
    探测失败的源 latency 同样是 None，用 latency 判会把失败源误显示成"未检测"
    （这正是旧版本的毛病）。
    """
    if checked is None:
        return "未检测"
    return "可播放" if healthy else "未通过"


def status_color(checked, healthy):
    """返回状态单元格的文字颜色（QBrush）。"""
    from PySide6.QtGui import QBrush, QColor
    if checked is None:
        return QBrush(QColor(*_COLOR_UNKNOWN))
    return QBrush(QColor(*(_COLOR_GOOD if healthy else _COLOR_BAD)))


def status_tooltip(checked, healthy, latency) -> str:
    if checked is None:
        return "尚未检测：点「测试选中」可立即探测"
    when = checked.strftime("%Y-%m-%d %H:%M:%S") if isinstance(checked, datetime) else "—"
    if healthy:
        return "最近检测：%s　延迟：%s ms" % (when, latency if latency is not None else "—")
    return "最近检测：%s　结果：不可用（可换地址或置顶其它源）" % when
