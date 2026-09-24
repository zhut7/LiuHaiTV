# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（one-folder 模式）。

构建（在项目根执行）：
    <env>/python.exe -m PyInstaller --clean --noconfirm LiuHaiTV.spec

产物：
    dist/LiuHaiTV/LiuHaiTV.exe        ← 双击即可运行
    整个 dist/LiuHaiTV/ 文件夹拷到任何 Windows（64 位）机器都能用

三个必须打进去的东西：
  1. bin/libmpv-2.dll   —— 播放内核。python-mpv 只认 PATH，
     由 liuhaitv/__init__.py 在导入时把 bin/ prepend 进去。
  2. data/liuhaitv.db    —— 作为「种子库」打到 data_seed/。
     程序首次运行时若 exe 旁边没有 data/liuhaitv.db 就复制一份过去，
     这样换台电脑打开就有完整的央视+卫视频道表（见 config.ensure_seed_db）。
  3. mpv 模块           —— python-mpv 是延迟导入的，显式写进 hiddenimports 更稳。

路径策略见 liuhaitv/config.py 顶部注释：用户数据（data/、logs/）落在 **exe 旁边**，
绝不写系统盘、也不写 PyInstaller 的临时解包目录。
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = SPECPATH  # PyInstaller 提供的 spec 所在目录（即项目根）

# 调试版：设 LIUHAITV_CONSOLE=1 再构建，会产出带控制台的 LiuHaiTV_debug，
# 用于看清启动期的真实报错（正式版没有控制台，错误看不见）。
CONSOLE = os.environ.get("LIUHAITV_CONSOLE") == "1"
APP_NAME = "LiuHaiTV"                       # exe 文件名固定
DIST_NAME = os.environ.get("LIUHAITV_DISTNAME") or ("LiuHaiTV_debug" if CONSOLE else "LiuHaiTV")

# ---------------------------------------------------------------------------
# conda 运行时 DLL（必须显式带上，否则打包后必然启动失败）
# ---------------------------------------------------------------------------
# conda 环境里的扩展模块（_sqlite3 / _ctypes / _ssl …）依赖 Library\bin 下的 DLL，
# 而 PyInstaller 默认搜不到那个目录 —— 症状是运行时才炸：
#   ImportError: DLL load failed while importing _sqlite3: 找不到指定的模块。
# （本项目就踩过：sqlalchemy 建引擎时导入 sqlite3 直接失败。）
# 该目录一共只有 22 MB，全部带上即可一次根除这类"缺依赖"，不必逐个猜。
PY_ROOT = os.path.dirname(os.path.abspath(sys.executable))
CONDA_LIB_BIN = os.path.join(PY_ROOT, "Library", "bin")

binaries = []
if os.path.isdir(CONDA_LIB_BIN):
    # 同时塞进 PATH，帮助 PyInstaller 解析这些 DLL 的传递依赖
    os.environ["PATH"] = CONDA_LIB_BIN + os.pathsep + os.environ.get("PATH", "")
    for name in sorted(os.listdir(CONDA_LIB_BIN)):
        if name.lower().endswith(".dll"):
            binaries.append((os.path.join(CONDA_LIB_BIN, name), "."))
    print("[spec] conda Library\\bin 收录 DLL: %d 个" % len(binaries))
else:
    print("[spec] 警告：未找到 %s，跳过 conda DLL 收集" % CONDA_LIB_BIN)

datas = [
    (os.path.join(ROOT, "bin", "libmpv-2.dll"), "bin"),
    (os.path.join(ROOT, "data", "liuhaitv.db"), "data_seed"),
]

hiddenimports = ["mpv"] + collect_submodules("liuhaitv")

# 只排除确定用不到的重家伙（QtWebEngine 一个就能省上百 MB）。
# 注意别排 QtNetwork / QtSvg —— Qt 的图标与网络层可能间接依赖它们。
excludes = [
    "tkinter", "unittest", "pydoc_data", "lib2to3", "test",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtSensors",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech", "PySide6.QtLocation", "PySide6.QtHttpServer",
]

a = Analysis(
    [os.path.join(ROOT, "scripts", "run_gui.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 压过的 exe 更容易被杀软误报，且拖慢启动
    console=CONSOLE,        # 桌面程序不带黑框；调试版才开控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=DIST_NAME,      # 产物文件夹名（设 LIUHAITV_DISTNAME=protable 就直接落到 protable\）
)
