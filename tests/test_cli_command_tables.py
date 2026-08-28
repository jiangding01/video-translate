"""CLI 命令表之间的硬关系守卫。

`cli.py` 里有四份互相牵连的命令表：`build_parser()` 的子命令、`_COMMAND_DISPATCH`、
`_JOB_MUTATING_COMMANDS`，以及 `_TASK_ROUTES` / `_STEP_ROUTES` 的路由目标。它们之间
的关系是硬不变量（漏一条就是运行期 AttributeError 或静默空实现），但此前只有零散断言。

本文件里的命令**全集永远从 `build_parser()` 内省得到**，不抄第五份手工名单；手工内容
只有下面两份「豁免/例外」表态清单。
"""

from __future__ import annotations

import argparse
import unittest

from xvt.cli import (
    _COMMAND_DISPATCH,
    _JOB_MUTATING_COMMANDS,
    _MANUAL_TASK_GUIDANCE,
    _STEP_ROUTES,
    _TASK_ROUTES,
    build_parser,
)

# 只读的 Job 命令豁免清单：这两条有 job_file 位置参数，但正常路径不写 job.json
# （_cmd_status / _cmd_verify 只 load_job + 读 artifact，不 save_job），因此不取 Job 锁。
# 新增任何带 job_file 的子命令时，必须在这里显式表态（写进豁免清单）或者
# 加进 cli._JOB_MUTATING_COMMANDS，否则本测试失败。这是一份**只有 2 条**的表态清单，
# 不是命令名单的第五份副本：命令全集始终从 build_parser() 内省得到。
READ_ONLY_JOB_COMMANDS = frozenset({"status", "verify"})

# 唯一走 batch 锁的命令。同样是表态清单而非全集副本。
BATCH_LOCK_COMMANDS = frozenset({"batch-continue"})


def _subcommands() -> dict[str, argparse.ArgumentParser]:
    parser = build_parser()
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
    return dict(action.choices)


def _commands_with_dest(dest: str) -> set[str]:
    return {
        name
        for name, command_parser in _subcommands().items()
        if any(action.dest == dest for action in command_parser._actions)
    }


class CliCommandTableTests(unittest.TestCase):
    def test_dispatch_table_matches_parser_subcommands(self) -> None:
        # 硬关系 1：分发表与 parser 子命令集合完全一致，多一个少一个都失败。
        # 少一个 => _run_command 静默返回 0；多一个 => 分发表里挂着够不着的实现。
        self.assertEqual(set(_COMMAND_DISPATCH), set(_subcommands()))

    def test_mutating_commands_are_a_subset_of_dispatch(self) -> None:
        # 硬关系 2：mutating ⊆ dispatch，不允许对不存在的命令登记 Job 锁。
        self.assertLessEqual(_JOB_MUTATING_COMMANDS, set(_COMMAND_DISPATCH))

    def test_mutating_commands_all_take_a_job_file(self) -> None:
        # 硬关系 3：_hold_execution_locks 对 mutating 命令取 parsed.job_file，
        # 没有 job_file 位置参数就会在运行期 AttributeError。
        self.assertLessEqual(_JOB_MUTATING_COMMANDS, _commands_with_dest("job_file"))

    def test_every_job_command_declares_whether_it_mutates(self) -> None:
        # 「会写 Job」由 parser 内省判定（有 job_file 位置参数），而不是另抄一份名单。
        # 有 job_file 但不取锁的，必须在 READ_ONLY_JOB_COMMANDS 里显式豁免。
        # 断言方向是「集合差 == 豁免清单」而不是「⊆ 豁免清单」，所以清单里放了不该放的
        # 名字同样会失败，防止豁免清单自己腐化。
        self.assertEqual(
            _commands_with_dest("job_file") - _JOB_MUTATING_COMMANDS,
            set(READ_ONLY_JOB_COMMANDS),
            "新增带 job_file 的子命令时，要么加进 cli._JOB_MUTATING_COMMANDS，"
            "要么在 READ_ONLY_JOB_COMMANDS 里说明它为什么不写 Job",
        )

    def test_batch_lock_commands_match_parser(self) -> None:
        # batch 锁与 Job 锁互斥：_hold_execution_locks 的 elif 分支只对 batch-continue 生效。
        self.assertEqual(_commands_with_dest("batch_file"), set(BATCH_LOCK_COMMANDS))
        self.assertEqual(_JOB_MUTATING_COMMANDS & BATCH_LOCK_COMMANDS, set())

    def test_route_targets_are_dispatchable_commands(self) -> None:
        # 硬关系 4：路由表的目标必须是真实存在的子命令，否则 _cmd_apply 路由到空实现。
        targets = set(_TASK_ROUTES.values()) | set(_STEP_ROUTES.values())
        self.assertLessEqual(targets, set(_COMMAND_DISPATCH))

    def test_task_tables_stay_disjoint(self) -> None:
        # 与 cli._assert_disjoint_task_tables() 互补：那条在 import 期跑（保护真实执行），
        # 这条在测试里跑（保证违反时有明确的失败用例而不是 import 崩溃）。
        self.assertEqual(_MANUAL_TASK_GUIDANCE.keys() & _TASK_ROUTES.keys(), set())
