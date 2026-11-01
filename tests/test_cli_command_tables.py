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
    _ARG_HELP,
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

# 同名参数却**必须**给出不同 help 的显式例外，值是理由。
# `--cue` 在 prepare-source-review 是 _AppendAction（可重复给多个 cue），在
# apply-cue-retranslation 是 _StoreAction（单值）——行为不同，一条文案说不准，
# 所以刻意分成两条。这是例外清单而不是待统一的重复，新增同类例外要写明理由。
HELP_DIVERGENCE_ALLOWED = {
    "--cue": "三处可重复（append，要操作哪些 cue）、preview 一处单值（store，从哪条起预览）",
    # 下面两条是本守卫 2026-11-01 首次上线时查出来的既有分歧，不是本次引入：
    # apply 那组变体对这两个旗标多了约束，一条文案覆盖不了。
    "--check": "apply 变体多一条「不能与 --continue 同用」的约束",
    "--fail-on-waiting": "apply 变体的语义是「仅在带 --continue 时生效」",
}


def _arguments() -> list[tuple[str, str, argparse.Action]]:
    """(子命令, 参数键, action)。参数键与 cli._ARG_HELP 的键同口径。"""

    out: list[tuple[str, str, argparse.Action]] = []
    for name, command_parser in _subcommands().items():
        for action in command_parser._actions:
            if action.dest == "help":
                continue
            key = action.option_strings[0] if action.option_strings else action.dest
            out.append((name, key, action))
    return out


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

    def test_every_argument_has_help_text(self) -> None:
        # `xvt --help` 与 README 并列为「用户可见行为」的事实来源（AGENTS 开头那段），
        # 而 2026-11-01 实测 193 个参数里 180 个没有 help——等于这条事实来源是空的。
        # 全集从 build_parser() 内省，不抄第二份名单。
        missing = sorted(
            {f"{command} {key}" for command, key, action in _arguments() if not action.help}
        )
        self.assertEqual(missing, [], f"这些参数没有 help 文案：{missing}")

    def test_same_argument_has_the_same_help_everywhere(self) -> None:
        # 防漂移：55 个唯一参数名复用到 180 个位置，逐处手写必然漂移，同一个
        # `--provider` 在两个命令里说两套话。
        #
        # 它同时是安全网：将来有人加的同名参数语义确实不同时，这条会红，逼他像
        # `--cue` 那样登记进 HELP_DIVERGENCE_ALLOWED 并写明理由，而不是悄悄写一条
        # 对其中一边不准确的文案。
        texts: dict[str, set[str]] = {}
        for _command, key, action in _arguments():
            if action.help:
                texts.setdefault(key, set()).add(action.help)
        diverged = {key: sorted(values) for key, values in texts.items() if len(values) > 1}
        self.assertEqual(
            sorted(diverged),
            sorted(HELP_DIVERGENCE_ALLOWED),
            f"同名参数的 help 不一致：{diverged}",
        )

    def test_help_table_has_no_unused_entries(self) -> None:
        # _ARG_HELP 是唯一事实来源，留着够不着的键只会让人以为某个参数已经有文案了。
        used = {key for _command, key, _action in _arguments()}
        self.assertEqual(
            set(_ARG_HELP) - used,
            set(),
            "_ARG_HELP 里有 parser 够不着的键",
        )

    def test_task_tables_stay_disjoint(self) -> None:
        # 与 cli._assert_disjoint_task_tables() 互补：那条在 import 期跑（保护真实执行），
        # 这条在测试里跑（保证违反时有明确的失败用例而不是 import 崩溃）。
        self.assertEqual(_MANUAL_TASK_GUIDANCE.keys() & _TASK_ROUTES.keys(), set())
