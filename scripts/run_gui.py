# SPDX-License-Identifier: GPL-3.0-or-later
"""
手动运行 LiuHaiTV 桌面主界面（GUI 入口，也是打包 exe 的入口脚本）。

职责：
  - 初始化数据库（建表 + 补齐缺列 + 首次运行播种模板库）。
  - 读取**全部**频道灌入频道列表模型；启动时不启动任何全量扫描，
    因此所有频道初始为**灰色（未判定）**。
    （不再按 is_visible 过滤 —— 隐藏频道功能已移除。）
  - 打开主窗口并进入 Qt 事件循环。

健康判定策略：
  - **点击才测**：点击某个频道时，主窗口起播该频道，同时在后台线程池探测它的源：
    有可用源 -> 该频道显示绿色；全部不可用 -> 红色；始终没点过的 -> 灰色。
  - 不启动 HealthWorker 定时全量扫描。
    （仍保留 liuhaitv.core.health.check_all 供脚本/"全量重测"按钮使用。）

**打包相关的两条约定（别改回去）**：
  1. 重量级导入（sqlalchemy / PySide6 / liuhaitv.ui.*）一律**放在函数内**。
     打包成无控制台的 exe 后，模块级导入一旦失败，异常就发生在下面那个顶层
     try/except **之外**，只会弹一个 PyInstaller 的默认错误框（细节没人看得到）；
     放进函数里才能被兜住，写进 logs/crash.log 并弹出可读的原生提示。
  2. 用户数据目录由 liuhaitv/config.py 统一决定（打包后 = exe 旁边），
     本文件不要自己拼路径。

运行：
  <env>/python.exe scripts/run_gui.py
  （排查卡死可设环境变量 LIUHAITV_FAULTHANDLER=1：20 秒后打印各线程堆栈）
"""
from __future__ import annotations

import logging
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import liuhaitv  # noqa: E402  确保 libmpv 等资源路径就绪（与其他脚本保持一致）
import liuhaitv.logger as pylog  # noqa: E402

log = logging.getLogger("run_gui")


def _enable_faulthandler() -> None:
    """
    可选排障开关：LIUHAITV_FAULTHANDLER=1 时，启动 20 秒后把所有线程堆栈打到
    stderr —— 卡死时能直接看出卡在哪一行。
    """
    if os.environ.get("LIUHAITV_FAULTHANDLER") != "1":
        return
    try:
        import faulthandler
        faulthandler.enable()
        faulthandler.dump_traceback_later(20, repeat=False)
        log.warning("已启用 faulthandler：20 秒后将打印线程堆栈")
    except Exception as exc:  # noqa: BLE001
        print("faulthandler 启用失败: %s" % exc)


def load_channels():
    """读取全部频道（含其源）。隐藏功能已移除，不再按 is_visible 过滤。"""
    from sqlalchemy.orm import selectinload

    from liuhaitv.core.database import session_scope
    from liuhaitv.core.models import Channel

    with session_scope() as s:
        return (s.query(Channel)
                .options(selectinload(Channel.sources))
                .order_by(Channel.group_name, Channel.sort_order).all())


def main() -> int:
    # 重量级导入放在函数内：这样顶层 try/except 才能兜住"导入期"的失败
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    from liuhaitv.core.database import init_db
    from liuhaitv.ui.channel_list_model import ChannelListModel
    from liuhaitv.ui.main_window import MainWindow

    pylog.setup_logging()
    _enable_faulthandler()

    app = QApplication(sys.argv)
    init_db()

    channels = load_channels()
    model = ChannelListModel()
    model.reload(channels)
    log.info("频道列表加载: %d 个可见频道（初始全部为灰色未判定）", len(channels))

    window = MainWindow(model)
    window.show()
    log.info("主窗口已显示，进入事件循环")
    ret = app.exec()

    # 退出时丢弃排队中的检测任务（正在跑的少数几个会自然结束），避免关窗卡顿
    try:
        QThreadPool.globalInstance().clear()
    except Exception as exc:  # noqa: BLE001
        log.debug("清理检测线程池已忽略: %s", exc)
    return ret


def _report_crash() -> None:
    """
    启动/运行期未捕获异常的兜底报告。

    为什么需要：打包成**无控制台**的 exe 后，异常默认是静默的 ——
    用户只看到"双击没反应"。这里把堆栈写到 exe 旁边的 logs/crash.log，
    并在打包形态弹一个原生 Windows 对话框（Qt 起不来时也能看见）。
    """
    import traceback

    text = traceback.format_exc()
    base = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False) else _PROJECT_ROOT)
    try:
        d = os.path.join(base, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "crash.log"), "a", encoding="utf-8") as fh:
            fh.write(text + "\n" + "-" * 60 + "\n")
    except Exception:  # noqa: BLE001 - 报告失败也不能再抛
        pass
    print(text)
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                ("LiuHaiTV 启动失败，详细信息已写入：\n"
                 "%s\n\n%s" % (os.path.join(base, "logs", "crash.log"), text[-1200:])),
                "LiuHaiTV 启动失败", 0x10)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - 顶层兜底：把静默崩溃变成可诊断
        _report_crash()
        sys.exit(1)
