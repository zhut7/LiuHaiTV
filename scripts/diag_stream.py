# SPDX-License-Identifier: GPL-3.0-or-later
"""诊断工具：测量一条直播源从 play() 到"解出第一帧"的总耗时。

用途：区分"网络/源服务器慢" 与 "机器解码慢"。跑完 curl 的
connect/TTFB 网络分段后，再用本脚本测端到端首帧耗时。

用法：
  <env>/python.exe scripts/diag_stream.py "http://ip:port/live/xxx.m3u8"

说明：
  - 用原始 python-mpv (vo=null，无界面)，poll video_params 非空 = 已解出第一帧。
  - 输出首次出帧耗时与分辨率。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import liuhaitv  # noqa: E402  预置 libmpv dll 到 PATH

import mpv  # noqa: E402


def first_frame_timeout(url: str, timeout: float = 45.0):
    m = mpv.MPV(vo='null', quiet=True, ytdl=False)
    t0 = time.perf_counter()
    m.play(url)
    vp = None
    while time.perf_counter() - t0 < timeout:
        try:
            vp = m.video_params
        except Exception:  # noqa: BLE001
            vp = None
        if vp:
            break
        time.sleep(0.1)
    dt = time.perf_counter() - t0
    try:
        m.terminate()
    except Exception:  # noqa: BLE001
        pass
    w = (vp or {}).get('w') or 0
    h = (vp or {}).get('h') or 0
    if vp:
        return dt, w, h, False
    return dt, w, h, True


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        print("用法: python scripts/diag_stream.py <m3u8-url>")
        return 1
    print(f"=== 端到端首帧耗时: {url}")
    dt, w, h, timed_out = first_frame_timeout(url)
    if timed_out:
        print(f"  超时({dt:.0f}s)仍未解出第一帧，分辨率为空 —— 大概率拉不到可解码的流")
    else:
        print(f"  解出第一帧耗时 = {dt:.1f}s   分辨率 = {w}x{h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
