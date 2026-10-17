"""AGENTS 不可破坏约束 6 的守卫：外部命令只能以参数数组执行，不拼接 shell 字符串。

O7b：此前全仓没有任何测试守着这条约束。`process.py` 是唯一允许碰 `subprocess` 的
模块——它负责无 shell 启动、进程组收尾与命令参数脱敏；别处一旦直接 `subprocess.run`，
就绕过了脱敏与取消机制，而且极易顺手写出 `shell=True`。

守卫做三件事：

1. `src/xvt/` 下除 `process.py` 外不得 import `subprocess`。
2. 全仓（含 `process.py`）不得出现 `shell=True` 关键字实参——参数数组是唯一执行形式，
   这条没有例外。
3. 不得调用 `os.system` / `os.popen`：它们只收 shell 字符串，天然违反约束 6，
   只禁 `subprocess` 会把这条后门留着。

实现用 ast 而不是文本扫描，免得续行写法、注释或字符串字面量影响判定。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "xvt"

# 唯一例外：无 shell 外部进程的实现本身。它是 AGENTS 模块表里登记的那一份。
SUBPROCESS_OWNER = "process.py"

# 只收 shell 字符串的 os 入口，与 subprocess 同族。
SHELL_STRING_CALLS = {"system", "popen"}


def _source_files() -> list[Path]:
    return sorted(SOURCE_DIR.rglob("*.py"))


def _relative(path: Path) -> str:
    return str(path.relative_to(SOURCE_DIR))


def _imports_subprocess(tree: ast.AST) -> list[int]:
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "subprocess" or alias.name.startswith("subprocess.")
                for alias in node.names
            ):
                found.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "subprocess" or module.startswith("subprocess."):
                found.append(node.lineno)
    return found


def _shell_true_keywords(tree: ast.AST) -> list[int]:
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                found.append(node.lineno)
    return found


def _shell_string_calls(tree: ast.AST) -> list[int]:
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in SHELL_STRING_CALLS
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        ):
            found.append(node.lineno)
    return found


class SubprocessBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = _source_files()
        # 守卫范围不能被意外缩掉（例如有人把扫描根指错）。
        self.assertGreater(len(self.files), 20, "未扫描到预期数量的 src/xvt 模块")
        self.assertIn(
            SUBPROCESS_OWNER,
            {path.name for path in self.files},
            f"扫描结果里没有 {SUBPROCESS_OWNER}，例外声明已失效",
        )
        self.trees = {
            path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for path in self.files
        }

    def test_only_process_module_imports_subprocess(self) -> None:
        offenders = [
            f"{_relative(path)}:{lineno}"
            for path, tree in self.trees.items()
            if path.name != SUBPROCESS_OWNER
            for lineno in _imports_subprocess(tree)
        ]
        self.assertEqual(
            offenders,
            [],
            "以下模块绕过 process.py 直接 import subprocess，"
            f"会跳过无 shell 执行、进程组收尾与命令脱敏：{offenders}",
        )

    def test_process_module_still_owns_subprocess(self) -> None:
        # 反向钉住：例外不是空头支票。process.py 真的用 subprocess，守卫才有意义。
        owner = next(path for path in self.files if path.name == SUBPROCESS_OWNER)
        self.assertNotEqual(_imports_subprocess(self.trees[owner]), [])

    def test_no_shell_true_anywhere(self) -> None:
        offenders = [
            f"{_relative(path)}:{lineno}"
            for path, tree in self.trees.items()
            for lineno in _shell_true_keywords(tree)
        ]
        self.assertEqual(
            offenders,
            [],
            f"以下调用传了 shell=True，违反 AGENTS 不可破坏约束 6：{offenders}",
        )

    def test_no_os_system_or_os_popen(self) -> None:
        offenders = [
            f"{_relative(path)}:{lineno}"
            for path, tree in self.trees.items()
            for lineno in _shell_string_calls(tree)
        ]
        self.assertEqual(
            offenders,
            [],
            f"以下调用用 os.system / os.popen 执行 shell 字符串：{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
