import ast
import unittest
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "src" / "xvt" / "pipeline"

# 声明的依赖白名单，见 AGENTS.md「代码结构」一节的依赖方向说明：
# common ← review ← {transcription, translation}
# semantic 与 retranslate 依赖 translation
# resegment 与 burnin 只依赖 common
# polish 依赖 translation
# revision 只依赖 common
#
# 只统计模块顶层（加载期即执行）的 import 语句。少数模块内部在函数体里用惰性
# import（例如 review/revision 内部惰性 import semantic，semantic 内部惰性
# import review/revision）来避免加载期循环 import；这些函数级 import 是刻意用来
# 打破循环的转义手段，不代表模块加载期的静态依赖方向，因此不计入本守卫测试的
# 依赖边集合，只需要看顶层 import 是否遵守既定方向。
EXPECTED_DEPENDENCIES: dict[str, set[str]] = {
    "burnin": {"common"},
    "common": set(),
    "polish": {"common", "translation"},
    "resegment": {"common"},
    "retranslate": {"common", "translation"},
    "review": {"common"},
    "revision": {"common"},
    "semantic": {"common", "translation"},
    "transcription": {"common", "review"},
    "translation": {"common", "review"},
}


def _module_dependencies(path: Path) -> set[str]:
    """解析单个 pipeline 模块文件，提取其模块顶层依赖的其他 xvt.pipeline.X 模块名。

    只看模块顶层语句（tree.body），不递归进函数/类体，因为函数体内的惰性 import
    不影响模块加载期的依赖方向（详见 EXPECTED_DEPENDENCIES 上方注释）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    deps: set[str] = set()
    module_name = path.stem
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("xvt.pipeline."):
                target = node.module.split(".")[2]
                if target != module_name:
                    deps.add(target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("xvt.pipeline."):
                    parts = alias.name.split(".")
                    if len(parts) >= 3:
                        target = parts[2]
                        if target != module_name:
                            deps.add(target)
    return deps


def _actual_dependencies() -> dict[str, set[str]]:
    actual: dict[str, set[str]] = {}
    for path in sorted(PIPELINE_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        actual[path.stem] = _module_dependencies(path)
    return actual


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


if __name__ == "__main__":
    unittest.main()
