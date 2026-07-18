from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0  # 命令成功完成
    USER_ERROR = 2  # 用户输入或配置错误，需要用户修正后重试
    DEPENDENCY_ERROR = 3  # 外部依赖或子进程故障（doctor 未通过、翻译提供方故障等）
    WORKFLOW_ERROR = 4  # 内部工作流不变量被破坏，属于程序侧问题
    VERIFY_FAILED = 5  # verify 校验未通过，artifact checksum 不一致
    PARTIAL_FAILURE = 6  # 批处理整体完成但存在失败条目
    LOCKED = 7  # 目标 Job 或 batch 已被占用（本 PR 仅定义，P4c 落地并发锁时使用）
    INTERRUPTED = 130  # 被用户中断（SIGINT / Ctrl-C）


class XvtError(Exception):
    """XVT 领域异常基类，携带退出码与结构化分类。

    子类通过覆盖类属性表达对外分类；顶层异常映射优先识别本层次，
    据此决定进程退出码和 `cli_failed` 事件的 category 字段。
    """

    exit_code: ExitCode = ExitCode.WORKFLOW_ERROR
    category: str = "workflow"


class UserInputError(XvtError):
    """用户输入或配置错误，例如缺失的翻译提供方配置。"""

    exit_code = ExitCode.USER_ERROR
    category = "invalid_input"


class DependencyError(XvtError):
    """外部依赖或子进程故障，例如翻译提供方返回不兼容响应。"""

    exit_code = ExitCode.DEPENDENCY_ERROR
    category = "dependency_or_process"


class InternalError(XvtError):
    """内部工作流不变量被破坏，属于程序侧数据完整性问题。"""

    exit_code = ExitCode.WORKFLOW_ERROR
    category = "workflow"
