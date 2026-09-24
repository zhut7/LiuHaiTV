# SPDX-License-Identifier: GPL-3.0-or-later
"""播放器内核包。

Step 3 填充：
  - mpv_wrapper.py —— MPVPlayer（python-mpv 薄封装：播放/暂停/音量/事件轮询/terminate）
                     与 PlayerController（Failover 状态机，频道多备用源自动切源）。
    说明：Windows 下把视频嵌入 Qt 窗口(HWND)的能力在窗口创建时用
          MPVPlayer.set_wid(winId) 注入，实现在 Step 5 的 UI 层调用。
"""
