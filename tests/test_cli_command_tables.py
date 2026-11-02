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
    _JOB_FALLBACK_SENTENCE,
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
    "--cue": (
        "四处三种文案：preview 单值（store，从哪条起预览）；retranslate-cues 与 "
        "apply-cue-retranslation 的顺序各有不同含义（前者按传入顺序定批次编号并进 "
        "fingerprint，后者必须照原顺序才能重算出同一份 manifest）；prepare-source-review "
        "两头都 sorted，顺序不影响结果，所以刻意不提顺序"
    ),
    # 下面两条是本守卫 2026-11-01 首次上线时查出来的既有分歧，不是本次引入：
    # apply 那组变体对这两个旗标多了约束，一条文案覆盖不了。
    "--check": "apply 变体多一条「不能与 --continue 同用」的约束",
    "--fail-on-waiting": "apply 变体的语义是「仅在带 --continue 时生效」",
}

# 值会固化进 Job、省略时读 Job 那一份的翻译设置参数。这是一份**语义声明**清单：它决定
# 「无 default 就必须带回退句」那条守卫的覆盖面。新增同类参数要加进来，否则它的漏拼没人
# 管（漏拼的后果是用户以为不传就等于用某个内置默认，而实际读的是 Job 里存着的那份）。
TRANSLATION_SETTING_ARGS = frozenset(
    {
        "--provider",
        "--batch-size",
        "--max-retries",
        "--timeout-seconds",
        "--translation-quality",
        "--translation-style",
    }
)

# 上面这些参数当前有多少个注册点是「无 default、因此必须带回退句」的。写成硬数字是为了
# 让覆盖面不能静默缩小：有人删掉某处的 default 或新增一条同类命令时这里会红，而那恰恰
# 是需要人确认文案对不对的时刻。
JOB_FALLBACK_REGISTRATION_COUNT = 10


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


def _help_base(action: argparse.Action) -> str:
    """剥掉受控后缀，取同名参数之间**必须**一致的那部分基础文案。

    「省略时回退 Job 固化值」是唯一允许按命令出现或不出现的后缀，因为它的真假由该注册点
    有没有 default 决定（见 `cli._JOB_FALLBACK_SENTENCE` 的注释）。剥掉它之后基础文案仍
    必须完全一致，所以这不是给逐处手写文案开的口子。

    两条分支都 `rstrip(" .")`，口径必须对称：只在带后缀那条剥尾句点，会让「同一句基础
    文案、一边带后缀一边不带」在基础文案以句点结尾时被报成「文案不一致」——一个以错误
    理由变红的守卫只会训练人去绕过它。代价是末尾句点的有无不再被这条守卫判为漂移。
    """

    text = action.help or ""
    if text.endswith(_JOB_FALLBACK_SENTENCE):
        return text[: -len(_JOB_FALLBACK_SENTENCE)].rstrip(" .")
    return text.rstrip(" .")


def _action_for(command: str, key: str) -> argparse.Action:
    return next(action for name, item, action in _arguments() if name == command and item == key)


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
        #
        # 比较的是**基础**文案（`_help_base` 剥掉受控的回退后缀）。2026-11-02 之前这里比的
        # 是整句，于是「省略时回退 Job 固化值」只能写进共享文案，而它对带硬默认的 18 个
        # 注册点是假的——守卫把一条说谎的文案锁成了「一致」。
        texts: dict[str, set[str]] = {}
        for _command, key, action in _arguments():
            if action.help:
                texts.setdefault(key, set()).add(_help_base(action))
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

    def test_job_fallback_claim_only_where_there_is_no_hard_default(self) -> None:
        # 带硬默认值的注册点省略时拿到的是那个默认值，永远走不到「回退 Job 固化值」那条
        # 分支。2026-11-02 之前这句写在共享文案里，28 个注册点有 18 个在说谎，其中
        # `run` / `batch` 的 6 个翻译参数最离谱——它们创建新 Job，连「已存设置」都不存在。
        offenders = sorted(
            f"{command} {key} (default={action.default!r})"
            for command, key, action in _arguments()
            if action.help and _JOB_FALLBACK_SENTENCE in action.help and action.default is not None
        )
        self.assertEqual(offenders, [], f"这些参数声称会回退，但带着硬默认值：{offenders}")

    def test_job_fallback_claim_survives_where_it_holds(self) -> None:
        # 反向保护：上一条只罚「多说」，把后缀全删掉它照样绿。覆盖面按 parser 内省取，
        # 不只钉某一个命令——10 个该带后缀的注册点散在 polish-translation、
        # prepare-translation 与 translate 三处，只验其中一处等于放掉另外六个。
        checked = 0
        for command, key, action in _arguments():
            if key not in TRANSLATION_SETTING_ARGS or action.default is not None:
                continue
            checked += 1
            with self.subTest(command=command, argument=key):
                self.assertIn(_JOB_FALLBACK_SENTENCE, action.help or "")
        self.assertEqual(
            checked,
            JOB_FALLBACK_REGISTRATION_COUNT,
            "该带回退句的注册点数量变了：确认新增或删除是有意的，再改这个数字",
        )

    def test_translation_setting_args_are_real_argument_keys(self) -> None:
        # 清单写错一个名字（拼写、改名），上面那条就会静默少验一个参数。
        self.assertLessEqual(TRANSLATION_SETTING_ARGS, set(_ARG_HELP))

    def test_cue_order_claims_match_each_command(self) -> None:
        # `--cue` 的顺序语义三处不同，文案不能共用一条（原先共用的那条说「order matters」，
        # 对 prepare-source-review 是假的：fingerprint 与模板两头都走 sorted(cue_ids)）。
        ordered = _action_for("retranslate-cues", "--cue").help or ""
        must_match = _action_for("apply-cue-retranslation", "--cue").help or ""
        unordered = _action_for("prepare-source-review", "--cue").help or ""
        self.assertIn("Order matters", ordered)
        self.assertIn("Order must match", must_match)
        self.assertNotIn("Order", unordered)

    def test_task_tables_stay_disjoint(self) -> None:
        # 与 cli._assert_disjoint_task_tables() 互补：那条在 import 期跑（保护真实执行），
        # 这条在测试里跑（保证违反时有明确的失败用例而不是 import 崩溃）。
        self.assertEqual(_MANUAL_TASK_GUIDANCE.keys() & _TASK_ROUTES.keys(), set())
