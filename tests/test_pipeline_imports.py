import ast
import unittest
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "src" / "xvt" / "pipeline"

# 声明的依赖白名单，见 AGENTS.md「代码结构」一节的依赖方向说明：
# common ← review ← {transcription.shared, translation, burnin}
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
    # transcription 在 2026-10-20 按 F13 约定拆成子包（本地转写 / 远程获取 /
    # resume 与 rebuild，外加一个零兄弟依赖的 shared 叶子与一层入口分派）。
    # 子包内部方向：shared ← local ← remote ← resume，dispatch ← {local, remote}。
    "transcription": {
        "transcription.dispatch",
        "transcription.local",
        "transcription.remote",
        "transcription.resume",
        "transcription.shared",
    },
    "transcription.dispatch": {"common", "transcription.local", "transcription.remote"},
    "transcription.local": {"common", "transcription.shared"},
    "transcription.remote": {"common", "transcription.local", "transcription.shared"},
    "transcription.resume": {
        "common",
        "transcription.local",
        "transcription.remote",
        "transcription.shared",
    },
    "transcription.shared": {"common", "review"},
    "translation": {"common", "review"},
}


def _intra_package_target(node: ast.AST, module_name: str) -> str | None:
    """从单条 import 语句里解出它依赖的其他 pipeline 模块名（点号形式）。

    返回值是去掉 ``xvt.pipeline.`` 前缀后的模块名，子包成员形如
    ``transcription.local``。解析时按 ``_module_name`` 给出的实际模块集合取最长
    匹配前缀，所以 ``from xvt.pipeline.transcription.local import X`` 解出的是
    ``transcription.local`` 而不是 ``transcription``——否则子包内部的依赖边会被
    压扁成一条「transcription 依赖自己」而静默消失。
    """
    if isinstance(node, ast.ImportFrom):
        candidates = [node.module or ""]
    elif isinstance(node, ast.Import):
        candidates = [alias.name for alias in node.names]
    else:
        return None
    known = _known_modules()
    for name in candidates:
        if not name.startswith("xvt.pipeline."):
            continue
        suffix = name[len("xvt.pipeline.") :]
        target = suffix if suffix in known else suffix.rsplit(".", 1)[0]
        if target in known and target != module_name:
            return target
    return None


def _module_name(path: Path) -> str:
    """把 pipeline 包内的文件路径映射成白名单里用的点号模块名。

    ``transcription/local.py`` → ``transcription.local``；
    ``transcription/__init__.py`` → ``transcription``；
    包根 ``__init__.py`` → ``""``（不参与白名单）。
    """
    parts = list(path.relative_to(PIPELINE_DIR).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _known_modules() -> frozenset[str]:
    return frozenset(_module_name(path) for path in _all_pipeline_files()) - {""}


def _all_pipeline_files() -> list[Path]:
    """包目录下的全部 ``*.py``（含子包与各级 ``__init__.py``）。

    刻意用 ``rglob``：``transcription`` 在 2026-10-20 拆成子包，``glob("*.py")``
    会把整个子包静默漏掉，守卫范围缩小而测试照样全绿。
    """
    return sorted(PIPELINE_DIR.rglob("*.py"))


def _pipeline_modules() -> list[Path]:
    return [path for path in _all_pipeline_files() if _module_name(path) != ""]


def _module_dependencies(path: Path) -> set[str]:
    """解析单个 pipeline 模块文件，提取其模块顶层依赖的其他 xvt.pipeline.X 模块名。

    只看模块顶层语句（tree.body）。包内已不允许函数级 import（由
    ``test_no_function_level_intra_package_imports`` 守卫），所以顶层 import 就是
    完整的包内依赖边集合。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    deps: set[str] = set()
    module_name = _module_name(path)
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
    return {_module_name(path): _module_dependencies(path) for path in _pipeline_modules()}


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
        # 这里刻意扫描包目录下全部 *.py（含各级 __init__.py 与子包），范围比依赖白名单更宽。
        modules = _all_pipeline_files()
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
