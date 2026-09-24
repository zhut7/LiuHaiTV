# SPDX-License-Identifier: GPL-3.0-or-later
"""
LiuHaiTV Desktop 主包。

包导入时执行最基础的初始化：
  1. 定位 libmpv-2.dll 的目录并 prepend 到 os.environ["PATH"]
     —— 这是 python-mpv 在 Windows 上加载动态库的唯一可靠机制
        （python-mpv 只搜索 PATH 环境变量，不搜索 exe 目录）。
  2. 暴露版本号。

注意：这里不做任何重初始化（logging/配置在 config.py 中按需触发），
      保持导入零副作用，方便被脚本、测试、UI 三方复用。
"""
import os
import sys

__version__ = "0.1.0"
__app_name__ = "LiuHaiTV Desktop"

# ---------------------------------------------------------------------------
# MPV 动态库路径处理
# ---------------------------------------------------------------------------
# python-mpv 在 Windows 上查找 libmpv-1.dll / libmpv-2.dll / mpv-2.dll 时，
# 只依赖 os.environ["PATH"]（见 mpv.py 源码）。因此必须在任何 "import mpv"
# 发生之前，把存放 dll 的目录插到 PATH 最前面，否则报
#   OSError: Cannot find mpv-1.dll ...
# 本项目把 dll 放在 <项目根>/bin/ 目录下随源码一起分发（打包时也随 exe
# 携带），因此这里把 bin/ 目录 prepend 进 PATH。
def _locate_bin_dir() -> str:
    """返回存放 libmpv-2.dll 的目录（项目 bin/ / 打包解包目录 / exe 旁 / 环境根 兜底）。"""
    candidates = []

    # 1) 首选：包根上一级的 bin/
    #    - 源码运行 = <项目根>/bin
    #    - PyInstaller 打包 = <解包目录>/bin（spec 里把 dll 打到这里）
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), "bin"))

    # 2) 打包形态：exe 自己所在的目录（方便用户把 dll 直接丢在 exe 旁边）
    if getattr(sys, "frozen", False):
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))

    # 3) 兜底：conda 环境根 / 其 DLLs 子目录
    candidates.append(os.path.dirname(os.sys.executable))
    candidates.append(os.path.join(os.path.dirname(os.sys.executable), "DLLs"))

    for c in candidates:
        for name in ("libmpv-2.dll", "mpv-2.dll", "libmpv-1.dll"):
            if os.path.exists(os.path.join(c, name)):
                return c
    # 全部未命中：为了不抛异常（让上层能自行处理），返回首选目录，由调用方决定报错
    return candidates[0]


def _ensure_mpv_on_path() -> None:
    """把可能的 dll 目录 prepend 到 PATH（幂等）。"""
    dll_dir = _locate_bin_dir()
    path = os.environ.get("PATH", "")
    # 幂等：已存在则跳过
    if dll_dir and dll_dir not in path.split(os.pathsep):
        os.environ["PATH"] = dll_dir + os.pathsep + path


_ensure_mpv_on_path()

# 便捷暴露：dll 目录，供外部需要显式加载 dll 时使用
MPV_DLL_DIR = _locate_bin_dir()
