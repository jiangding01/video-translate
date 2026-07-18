"""进程级协作式取消：把 SIGINT/SIGTERM 统一为一个全局取消事件。

CLI 入口安装信号处理器；`process.run_command` 默认监听全局取消事件并在触发时
清理外部进程组；长循环（翻译批次、批处理条目）在迭代边界调用检查点。
信号处理器只在主线程安装与触发，这是 CPython 的信号语义保证。
"""

from __future__ import annotations

import signal
import threading
from types import FrameType

_cancel_event = threading.Event()
_received_signal: int | None = None


def install_signal_handlers() -> None:
    """在主线程安装 SIGINT 与 SIGTERM 处理器。

    两种信号都置位全局取消事件并抛出 ``KeyboardInterrupt``，复用 CLI 既有的
    中断持久化路径；退出码由 :func:`received_signal` 区分（SIGINT 130 / SIGTERM 143）。
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _handle(signum: int, _frame: FrameType | None) -> None:
        global _received_signal
        if _received_signal is None:
            _received_signal = signum
        _cancel_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def cancel_event() -> threading.Event:
    return _cancel_event


def cancellation_requested() -> bool:
    return _cancel_event.is_set()


def received_signal() -> int | None:
    return _received_signal


def reset() -> None:
    """仅供测试复位全局状态。"""
    global _received_signal
    _received_signal = None
    _cancel_event.clear()
