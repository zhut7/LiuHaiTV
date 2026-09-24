# SPDX-License-Identifier: GPL-3.0-or-later
"""视频渲染容器。

把原生窗口句柄(winId)交给 mpv 做嵌入式渲染（Step 5 右侧播放区）。
mpv 通过 wid 属性绑定到该窗口，视频直接画在这个 widget 上。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget


class VideoView(QWidget):
    """承载 mpv 渲染画面的空白容器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 保证每个实例拥有独立原生窗口句柄（mpv 渲染目标）
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumSize(360, 220)
        # 纯黑底，mpv 未出画面时不像白板
        self.setStyleSheet("background:#000;")

    def native_id(self) -> int:
        """mpv 渲染目标：把 winId 转成整型句柄。窗口一旦 show 过即有效。"""
        return int(self.winId())
