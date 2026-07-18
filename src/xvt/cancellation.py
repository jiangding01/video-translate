"""进程级协作式取消：把 SIGINT/SIGTERM 统一为一个全局取消事件。

CLI 入口通过 :func:`signal_scope` 进入命令级信号作用域；`process.run_command`
默认监听全局取消事件并在触发时清理外部进程组；长循环（翻译批次、批处理条目）
在迭代边界调用检查点。信号处理器只在主线程安装与触发，这是 CPython 的信号
语义保证。
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

_cancel_event = threading.Event()
_received_signal: int | None = None


@contextmanager
def signal_scope() -> Iterator[None]:
    """命令级信号作用域：进入时复位取消状态并安装处理器，退出时恢复原处理器。

    - 第一次 SIGINT/SIGTERM：置位全局取消事件并抛出 ``KeyboardInterrupt``，
      复用 CLI 既有的中断持久化路径；退出码由 :func:`received_signal` 区分
      （SIGINT 130 / SIGTERM 143）。
    - 后续信号：只保持取消状态，不再抛出——中断记账与恢复计划的清理临界区
      不会被二次 Ctrl-C 打断成裸 traceback。
    - 退出时恢复调用前的处理器并复位状态，保证同进程多次调用 ``main()``
      （测试、未来嵌入）互不污染。
    """
    reset()
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _handle(signum: int, _frame: FrameType | None) -> None:
        global _received_signal
        first_signal = not _cancel_event.is_set()
        if _received_signal is None:
            _received_signal = signum
        _cancel_event.set()
        if first_signal:
            raise KeyboardInterrupt

    original_handlers = {
        handled: signal.signal(handled, _handle) for handled in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for handled, original in original_handlers.items():
            signal.signal(handled, original)
        reset()


def cancel_event() -> threading.Event:
    return _cancel_event


def cancellation_requested() -> bool:
    return _cancel_event.is_set()


def received_signal() -> int | None:
    return _received_signal


def reset() -> None:
    """复位全局取消状态（signal_scope 出入口与测试使用）。"""
    global _received_signal
    _received_signal = None
    _cancel_event.clear()
