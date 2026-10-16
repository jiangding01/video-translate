import ast
import unittest
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "src" / "xvt" / "pipeline"

# 声明的依赖白名单，见 AGENTS.md「代码结构」一节的依赖方向说明：
# common ← review ← {transcription, translation, burnin}
# common ← semantic_state ← {review, revision}
# semantic 与 retranslate 依赖 translation；semantic 还依赖 review / revision / semantic_state
# resegment 只依赖 common；burnin 依赖 common 与 review（共用 QA 放行谓词）
# polish 依赖 translation
#
# pipeline 包内已无函数级 import 环：所有包内依赖都是模块顶层 import，方向全部向下。
# 本守卫测试因此既核对顶层 import 的方向（EXPECTED_DEPENDENCIES），也核对函数体内
# 不再出现任何 `xvt.pipeline.*` 的惰性 import（见
# ``test_no_function_level_intra_package_imports``）。
EXPECTED_DEPENDENCIES: dict[str, set[str]] = {
    "burnin": {"common", "review"},
    "common": set(),
    "polish": {"common", "translation"},
    "resegment": {"common"},
    "retranslate": {"common", "translation"},
    "review": {"common", "semantic_state"},
    "revision": {"common", "semantic_state"},
    "semantic": {"common", "review", "revision", "semantic_state", "translation"},
    "semantic_state": {"common"},
    "transcription": {"common", "review"},
    "translation": {"common", "review"},
}


def _intra_package_target(node: ast.AST, module_name: str) -> str | None:
    """从单条 import 语句里解出它依赖的其他 ``xvt.pipeline.X`` 模块名。"""
    if isinstance(node, ast.ImportFrom):
        if node.module and node.module.startswith("xvt.pipeline."):
            target = node.module.split(".")[2]
            if target != module_name:
                return target
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith("xvt.pipeline."):
                parts = alias.name.split(".")
                if len(parts) >= 3 and parts[2] != module_name:
                    return parts[2]
    return None


def _pipeline_modules() -> list[Path]:
    return [path for path in sorted(PIPELINE_DIR.glob("*.py")) if path.name != "__init__.py"]


def _module_dependencies(path: Path) -> set[str]:
    """解析单个 pipeline 模块文件，提取其模块顶层依赖的其他 xvt.pipeline.X 模块名。

    只看模块顶层语句（tree.body）。包内已不允许函数级 import（由
    ``test_no_function_level_intra_package_imports`` 守卫），所以顶层 import 就是
    完整的包内依赖边集合。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    deps: set[str] = set()
    module_name = path.stem
    for node in tree.body:
        target = _intra_package_target(node, module_name)
        if target is not None:
            deps.add(target)
    return deps


def _nested_intra_package_imports(path: Path) -> list[tuple[str, int]]:
    """列出函数/类体内（即非模块顶层）的 ``xvt.pipeline.*`` import。

    实现方式是 ast 遍历：先记下模块顶层语句的 id，再走 ``ast.walk`` 覆盖任意嵌套
    深度的 import 节点（含嵌套 def、try/except 与 with 内部），凡不在顶层语句集合
    里的就是函数级惰性 import。不扫文本，避免缩进宽度或续行写法把漏网之鱼放过去。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top_level = {id(node) for node in tree.body}
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if id(node) in top_level:
            continue
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        # 包内自引用同样算违规，所以这里不复用 _intra_package_target 的「排除自身」逻辑。
        if any(name == "xvt.pipeline" or name.startswith("xvt.pipeline.") for name in names):
            found.append((path.stem, node.lineno))
    return found


def _actual_dependencies() -> dict[str, set[str]]:
    return {path.stem: _module_dependencies(path) for path in _pipeline_modules()}


class PipelineImportDirectionTest(unittest.TestCase):
    def test_dependency_edges_match_whitelist(self) -> None:
        actual = _actual_dependencies()

        self.assertEqual(
            set(actual.keys()),
            set(EXPECTED_DEPENDENCIES.keys()),
            "pipeline 模块集合与白名单声明的模块集合不一致，"
            "新增或删除模块时需同步更新 EXPECTED_DEPENDENCIES",
        )

        for module in sorted(EXPECTED_DEPENDENCIES):
            expected_deps = EXPECTED_DEPENDENCIES[module]
            actual_deps = actual[module]

            extra = actual_deps - expected_deps
            missing = expected_deps - actual_deps

            if extra:
                self.fail(
                    f"{module} 实际依赖了未在白名单声明的模块: "
                    f"{ {f'{module} -> {d}' for d in sorted(extra)} }"
                )
            if missing:
                self.fail(
                    f"{module} 白名单声明了实际未使用的依赖: "
                    f"{ {f'{module} -> {d}' for d in sorted(missing)} }"
                )

    def test_no_function_level_intra_package_imports(self) -> None:
        """pipeline 包内不得再用函数级惰性 import 规避加载期循环。

        R3.1 Phase 2 把 semantic 的只读状态查询下移到叶子模块 semantic_state，
        review ⇄ semantic 与 revision ⇄ semantic 两个环已经消掉。再出现函数体内的
        `from xvt.pipeline.*` 就意味着有人重新引入了循环而不是修正依赖方向。
        """
        # 这里刻意扫描包目录下全部 *.py（含 __init__.py），范围比依赖白名单更宽。
        modules = sorted(PIPELINE_DIR.glob("*.py"))
        self.assertGreaterEqual(
            len(modules),
            len(EXPECTED_DEPENDENCIES) + 1,
            "未扫描到全部 pipeline 模块（含 __init__.py），守卫范围被意外缩小",
        )

        offenders: list[str] = []
        for path in modules:
            offenders.extend(
                f"{module}.py:{lineno}" for module, lineno in _nested_intra_package_imports(path)
            )

        self.assertEqual(
            offenders,
            [],
            "以下位置在函数体内 import 了 xvt.pipeline 内部模块，"
            f"请改为模块顶层 import 或下移共享逻辑: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
