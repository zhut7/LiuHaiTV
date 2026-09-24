# SPDX-License-Identifier: GPL-3.0-or-later
"""
频道列表模型 + 状态渲染（Step 5 左侧）。

状态语义（本次改动：由"启动全量扫描"改为"点击判定"）：
  - unknown  灰色 —— 本次会话内**尚未点击过**该频道（含刚启动时；也是默认态）
  - checking 橙色 —— 正在探测中（点击后到出结果之间的过渡态，表示"在测"）
  - good     绿色 —— 点击后判定**可播放**（该频道至少一个源探测通过）
  - bad      红色 —— 点击后判定**不可播放**（启用中的源全部探测失败 / 无源）

设计要点：
  - 判定结果保存在**模型内的会话状态表** `_status`（channel_id -> 状态），
    因此重启程序后所有频道回到灰色（未点击=灰），不会拿上次会话的陈旧绿色误导用户；
    DB 的 Channel.last_status 仍然记下最后一次判定，便于排障/后续扩展。
  - `set_status()` 就地更新单行并 dataChanged —— 不重建模型，不打断当前播放/选择。
  - `update_sources()` 供源管理弹窗保存后刷新该频道的源列表（failover 用的就是它）。

分组行（中央/卫视/港澳台/地方/其他）不可选，键盘上下键导航会自动跳过。
"""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate

# 固定分组显示顺序
GROUP_ORDER = ["中央", "卫视", "港澳台", "地方", "其他"]

# 四态 -> 颜色
STATUS_UNKNOWN = "unknown"
STATUS_CHECKING = "checking"
STATUS_GOOD = "good"
STATUS_BAD = "bad"

_STATUS_COLORS = {
    STATUS_UNKNOWN: QColor(170, 172, 178),   # 灰：未点击过
    STATUS_CHECKING: QColor(232, 163, 61),   # 橙：检测中
    STATUS_GOOD: QColor(0, 180, 60),         # 绿：可播放
    STATUS_BAD: QColor(224, 30, 40),         # 红：不可播放
}

_STATUS_TEXT = {
    STATUS_UNKNOWN: "未检测",
    STATUS_CHECKING: "检测中…",
    STATUS_GOOD: "可播放",
    STATUS_BAD: "不可播放",
}


class RowType(Enum):
    GROUP = 0
    CHANNEL = 1


class ChannelListItem:
    """一行数据。GROUP 行用 text + row_type；CHANNEL 行额外带 channel/sources/status。"""

    __slots__ = ("row_type", "text", "channel", "sources", "status")

    def __init__(self, row_type, text, channel=None, sources=None, status=STATUS_UNKNOWN):
        self.row_type = row_type
        self.text = text
        self.channel = channel
        self.sources = sources or []
        self.status = status


class ChannelListModel(QAbstractListModel):
    StatusRole = Qt.UserRole + 1
    ChannelRole = Qt.UserRole + 2
    RowTypeRole = Qt.UserRole + 3
    SourceCountRole = Qt.UserRole + 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list = []
        # 会话状态表：channel_id -> 四态字符串。reload 时保留，保证刷新不丢颜色
        self._status: dict = {}

    # ---- 构建 -------------------------------------------------------------
    def reload(self, channels) -> None:
        """整体重建（首次加载 / 频道增删后）。会重置选择，但保留会话内已有颜色。"""
        self.beginResetModel()
        buckets = {g: [] for g in GROUP_ORDER}
        for c in channels:
            g = c.group_name if c.group_name in buckets else "其他"
            buckets[g].append(c)
        items: list = []
        for g in GROUP_ORDER:
            grp = buckets[g]
            if not grp:
                continue
            grp.sort(key=lambda c: (c.sort_order, c.name))
            items.append(ChannelListItem(RowType.GROUP, g))
            for c in grp:
                items.append(self._make_channel_item(c))
        self._items = items
        self.endResetModel()

    @classmethod
    def _make_channel_item(cls, c) -> ChannelListItem:
        sources = list(getattr(c, "sources", None) or [])
        return ChannelListItem(RowType.CHANNEL, c.name, channel=c, sources=sources)

    # ---- 状态（会话内，点击驱动）-------------------------------------------
    def status_of(self, channel_id) -> str:
        """取某频道当前展示状态；无记录=未点击=灰。"""
        return self._status.get(channel_id, STATUS_UNKNOWN)

    def set_status(self, channel_id, status: str, channel=None) -> None:
        """就地更新某频道的状态（灰/检测中/绿/红）。channel 传入时会顺带刷新其源列表。"""
        if channel_id is None:
            return
        old = self._status.get(channel_id, STATUS_UNKNOWN)
        if channel is not None:
            self._refresh_sources(channel_id, getattr(channel, "sources", None))
        if old == status and channel is None:
            return
        self._status[channel_id] = status
        idx = self._row_index_of(channel_id)
        if idx >= 0:
            item = self._items[idx]
            item.status = status
            if channel is not None:
                item.channel = channel
            mi = self.index(idx, 0)
            self.dataChanged.emit(mi, mi)

    def reset_statuses(self) -> None:
        """清空全部会话状态（全部回到灰色）。"""
        self._status.clear()
        first = last = None
        for i, item in enumerate(self._items):
            if item.row_type != RowType.CHANNEL:
                continue
            if item.status != STATUS_UNKNOWN:
                item.status = STATUS_UNKNOWN
                if first is None:
                    first = i
                last = i
        if first is not None:
            self.dataChanged.emit(self.index(first, 0), self.index(last, 0))

    def update_sources(self, channel_id, sources) -> None:
        """源管理弹窗保存后调用：替换该频道的源列表（failover 依据）。"""
        self._refresh_sources(channel_id, sources)

    def _refresh_sources(self, channel_id, sources) -> None:
        if sources is None:
            return
        idx = self._row_index_of(channel_id)
        if idx < 0:
            return
        self._items[idx].sources = list(sources)

    def _row_index_of(self, channel_id) -> int:
        for i, item in enumerate(self._items):
            if item.row_type == RowType.CHANNEL and item.channel is not None \
                    and item.channel.id == channel_id:
                return i
        return -1

    def row_of_channel(self, channel_id) -> int:
        """公开取行号（-1 表示不在当前列表中）。"""
        return self._row_index_of(channel_id)

    # ---- QAbstractListModel -------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        if role in (Qt.DisplayRole, Qt.EditRole):
            return item.text
        if role == self.StatusRole:
            return item.status
        if role == self.ChannelRole:
            return item.channel
        if role == self.RowTypeRole:
            return item.row_type.value
        if role == self.SourceCountRole:
            return len(item.sources)
        if role == Qt.ToolTipRole:
            if item.row_type == RowType.GROUP:
                return None
            return self._tooltip(item)
        return None

    @staticmethod
    def _tooltip(item) -> str:
        st = _STATUS_TEXT.get(item.status, "")
        srcs = item.sources or []
        enabled = [s for s in srcs if getattr(s, "is_enabled", True)]
        return (f"{item.text}\n状态: {st}\n源数量: {len(srcs)}（启用 {len(enabled)}）")

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        if self._items[index.row()].row_type == RowType.GROUP:
            # 分组头：可见但不可选（键盘上下键导航自动跳过）
            return Qt.ItemIsEnabled
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def item_at(self, row):
        return self._items[row] if 0 <= row < len(self._items) else None


class ChannelDelegate(QStyledItemDelegate):
    """绘制：分组头=灰色小标题；频道行=状态圆点(灰/橙/绿/红)+频道名。"""

    def paint(self, painter, option, index):
        painter.save()
        item = index.model().item_at(index.row())
        if item.row_type == RowType.GROUP:
            painter.fillRect(option.rect, QColor(238, 238, 243))
            painter.setPen(QColor(96, 96, 108))
            f = option.font
            f.setPointSizeF(9)
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(option.rect.adjusted(8, 0, 0, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, item.text)
            painter.restore()
            return

        # 频道行背景
        selected = bool(option.state & QStyle.State_Selected)
        painter.fillRect(option.rect,
                         option.palette.highlight() if selected else option.palette.base())
        # 状态圆点（检查中画成空心环，与"已判定"的实心点区分）
        color = _STATUS_COLORS.get(item.status, _STATUS_COLORS[STATUS_UNKNOWN])
        d = 12
        cx = option.rect.left() + 16
        cy = option.rect.center().y()
        if item.status == STATUS_CHECKING:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(color)
            painter.drawEllipse(int(cx - d / 2.0), int(cy - d / 2.0), d, d)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(cx - d / 2.0, cy - d / 2.0, d, d)
        # 频道名
        if selected:
            painter.setPen(option.palette.highlightedText().color())
        else:
            painter.setPen(option.palette.text().color())
        f = option.font
        f.setPointSizeF(10)
        painter.setFont(f)
        painter.drawText(option.rect.adjusted(30, 0, 4, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, item.text)
        painter.restore()

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        sz.setHeight(max(sz.height(), 32))
        return sz


class ChannelListView(QListView):
    """
    频道列表视图：**右键只弹菜单，不切换频道**。

    为什么需要单独一个子类：`QAbstractItemView` 默认在任何鼠标键按下时都会把
    `currentIndex` 移到光标下的那一项，而本程序把 `currentChanged` 当作
    "播放这个台"的触发点（见 main_window._on_current_changed）——
    结果就是**右键点一下也会换台并开始拉流**。

    这里直接吞掉右键的 press / release（不调 super()），于是：
      - currentIndex 不变 → 不换台、不换选中项、不触发播放；
      - `contextMenuEvent` 是独立事件，仍然照常发出，
        所以 `customContextMenuRequested` 照旧工作，菜单针对**光标下那一行**弹出。
    """

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()          # 吞掉：不让基类改 currentIndex
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)

