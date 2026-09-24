# SPDX-License-Identifier: GPL-3.0-or-later
"""UI 包。

Step 5 填充：
  - main_window.py            —— 主界面：左频道列表 + 右播放器 + 控制条(暂停/音量/全屏)
  - channel_list_model.py     —— 分组频道列表模型 + 状态渲染(delegate)：绿=健康 / 红=无信号
  - video_view.py             —— 视频渲染容器(把 winId 交给 mpv 做嵌入式渲染)
"""
