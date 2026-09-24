# SPDX-License-Identifier: GPL-3.0-or-later
"""
Step 1 环境检查脚本。

用途：验证 liuhaitv 环境已正确构建 —— Python 版本、全部核心依赖可导入、
       libmpv-2.dll 可定位可加载、Qt 平台可用。

运行：
    <env>/python.exe scripts/check_env.py

返回码：
    0  —— 全部通过
    1  —— 存在失败项（详细见输出）
"""

import sys
import os

# 把项目根加入 sys.path，使从任意工作目录运行本脚本都能 import liuhaitv 包
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 必须先初始化 liuhaitv 包（它会把 bin/ 加入 PATH，从而让 import mpv 成功）
import liuhaitv  # noqa: E402  (确保 MPV dll 路径已 prepend)
import liuhaitv.logger as pylog  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1

# 逐个探针：模块名 -> (导入名, 版本属性)，None 版本属性表示仅验证导入
PROBES = [
    ("PySide6",          "PySide6",  "__version__"),
    ("python-mpv",       "mpv",      None),
    ("httpx",            "httpx",    "__version__"),
    ("SQLAlchemy",       "sqlalchemy", "__version__"),
]


def check_python_version(log) -> bool:
    """校验 Python 主版本应为 3.11.x。"""
    ver = sys.version_info
    ok = ver.major == 3 and ver.minor == 11
    log.info("Python 版本: %s.%s.%s -> %s",
             ver.major, ver.minor, ver.micro, "OK" if ok else "MISMATCH(应为3.11)")
    return ok


def check_imports(log) -> bool:
    """逐个导入核心依赖并读取版本，记录成功/失败。"""
    all_ok = True
    for label, mod, attr in PROBES:
        try:
            m = __import__(mod, fromlist=["*"])
            if attr:
                ver = getattr(m, attr, "?")
                log.info("导入 OK  %-14s %s", label, ver)
            else:
                log.info("导入 OK  %-14s v=%s", label, getattr(m, "__version__", "?"))
        except Exception as exc:
            all_ok = False
            log.error("导入失败 %-14s %s", label, exc)
    return all_ok


def check_libmpv(log) -> bool:
    """定位并真实加载 libmpv-2.dll，验证 python-mpv 可用。"""
    try:
        import mpv
        # 尝试建立真实 MPV 实例，触发底层 dll 加载
        player = mpv.MPV(ytdl=False, vo="null", input_default_bindings=False,
                         input_vo_keyboard=False)
    except Exception as exc:
        log.error("libmpv 加载失败(MPV实例): %s", exc)
        return False
    try:
        # python-mpv 通过属性访问读取属性 (__getattr__ 把 _ 转 -)，没有 get_property() 方法。
        # 用 player.mpv_version（初始化时会读取该属性，必定存在；等价于 mpv 的 mpv-version 属性）。
        lv = player.mpv_version
        log.info("libmpv 加载 OK  内核版本: %s", lv)
    except Exception as exc:
        log.error("libmpv 已加载但属性读取失败: %s", exc)
        return False
    finally:
        try:
            player.terminate()
        except Exception:
            pass
    return True


def check_qt_platform(log) -> bool:
    """初始化 QApplication 并读取当前 Qt 平台（确认可创建 GUI）。"""
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        log.info("Qt 平台初始化 OK platform=%s", app.platformName())
        return True
    except Exception as exc:
        log.error("Qt 平台初始化失败: %s", exc)
        return False


def main() -> int:
    log = pylog.get_logger("check_env")
    # 仅用控制台输出便于直接查看；文件日志不影响检测
    pylog.setup_logging()

    log.info("===== LiuHaiTV Desktop 环境检查 (Step 1) =====")
    log.info("项目根目录: %s", liuhaitv.config.PROJECT_ROOT)
    log.info("MPV DLL 目录: %s", liuhaitv.MPV_DLL_DIR)

    results = {
        "python版本": check_python_version(log),
        "依赖导入":    check_imports(log),
        "libmpv加载":  check_libmpv(log),
    }
    all_ok = all(results.values())
    qtok = check_qt_platform(log)
    results["Qt平台"] = qtok
    all_ok = all_ok and qtok

    log.info("----- 结果汇总 -----")
    for k, v in results.items():
        log.info("  %-12s %s", k, "PASS" if v else "FAIL")
    log.info("%s", "全部通过 ✓ 可进入 Step 2" if all_ok else "存在失败项，请根据上方日志修复")
    return EXIT_OK if all_ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
