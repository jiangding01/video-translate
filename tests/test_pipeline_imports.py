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
#
# 「包根」不是合法的依赖目标：`xvt/pipeline/__init__.py` import 了全部子模块，包内
# 任何模块再反向依赖它（`from xvt.pipeline import X` / `import xvt.pipeline` / 相对
# import）都是加载期循环的来源，因此解析器把这类边报成 `_PACKAGE_ROOT`，而白名单里
# 没有也不该有这一项。解析覆盖面本身由 `IntraPackageTargetResolutionTest` 钉住。
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


# 「依赖 pipeline 包根」这条边的哨兵。包根 `__init__` import 了全部子模块，包内再
# 反向依赖它就是加载期循环的来源，所以没有任何模块会在白名单里声明它——一出现就红。
# 取一个不可能成为模块名的字符串，免得跟真模块撞名。
_PACKAGE_ROOT = "<package root>"


def _resolve_module_path(dotted: str) -> str | None:
    """把一个 import 出来的点号名映射成白名单用的 pipeline 模块名。

    不在 pipeline 包内 → ``None``；恰好是包根 → ``_PACKAGE_ROOT``；否则取**已知
    模块集合里最长的那个前缀**，所以 ``xvt.pipeline.transcription.local`` 解出的是
    ``transcription.local`` 而不是 ``transcription``——否则子包内部的依赖边会被压扁
    成一条「transcription 依赖自己」而静默消失。``xvt.pipeline.<不存在的东西>``
    同样不放过，按依赖包根处理。
    """
    if dotted == "xvt.pipeline":
        return _PACKAGE_ROOT
    if not dotted.startswith("xvt.pipeline."):
        return None
    parts = dotted[len("xvt.pipeline.") :].split(".")
    known = _known_modules()
    for stop in range(len(parts), 0, -1):
        candidate = ".".join(parts[:stop])
        if candidate in known:
            return candidate
    return _PACKAGE_ROOT


def _intra_package_targets(node: ast.AST, module_name: str) -> set[str]:
    """从单条 import 语句里解出它依赖的**全部**其他 pipeline 模块名（点号形式）。

    返回集合而不是单个值，是 2026-10-20 事后 review 的修正：原实现在第一个匹配上
    就 ``return``，于是 ``import xvt.pipeline.common, xvt.pipeline.transcription.resume``
    里的第二个 alias（反向边）会被第一个合法边掩盖。同时补齐另三类原本整类漏判的
    写法，各有一条测试钉住（见 ``IntraPackageTargetResolutionTest``）：

    - ``from xvt.pipeline import X``：``node.module`` 是 ``xvt.pipeline``，**不带尾点**，
      原判定的 ``startswith("xvt.pipeline.")`` 直接放过。X 是子模块就记那条边，
      是包根 re-export 的符号就记 ``_PACKAGE_ROOT``。
    - ``from xvt.pipeline.transcription import shared``：真实边是子模块
      ``transcription.shared``，不是子包 ``transcription``，所以要把被 import 的
      名字也接到 base 后面再查一次。
    - 相对 import（``from . import common``）：对两道守卫原本都是隐身的。本仓一律用
      绝对 import，这里不去解析它指谁，统一记成 ``_PACKAGE_ROOT`` 让它变红，由人
      改成对应的绝对 import。
    """
    targets: set[str] = set()
    if isinstance(node, ast.ImportFrom):
        if node.level:
            targets.add(_PACKAGE_ROOT)
        elif node.module:
            base = _resolve_module_path(node.module)
            if base is None:
                return set()
            known = _known_modules()
            for alias in node.names:
                deeper = alias.name if base == _PACKAGE_ROOT else f"{base}.{alias.name}"
                targets.add(deeper if deeper in known else base)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            resolved = _resolve_module_path(alias.name)
            if resolved is not None:
                targets.add(resolved)
    return targets - {module_name}


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
        deps |= _intra_package_targets(node, module_name)
    return deps


def _nested_intra_package_imports(path: Path) -> list[tuple[str, int]]:
    """列出函数/类体内（即非模块顶层）的 ``xvt.pipeline.*`` import。

    返回 ``(包内相对路径, 行号)``。刻意不用 ``path.stem``：转包之后
    ``transcription/shared.py`` 的 stem 是 ``shared``，跟顶层同名文件无法区分；
    也刻意不用点号模块名，那样报错会拼成 ``transcription.local.py:123`` 这种
    既不是路径也不是模块名的东西。

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
            # 相对 import 的 node.module 不带包前缀（``from . import x`` 甚至是 None），
            # 按名字判定看不见它；而包内文件里的相对 import 必然是包内依赖。
            if node.level:
                found.append((str(path.relative_to(PIPELINE_DIR)), node.lineno))
                continue
            names = [node.module or ""]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        # 包内自引用同样算违规，所以这里不复用 _intra_package_targets 的「排除自身」逻辑。
        if any(name == "xvt.pipeline" or name.startswith("xvt.pipeline.") for name in names):
            found.append((str(path.relative_to(PIPELINE_DIR)), node.lineno))
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
                f"{relative}:{lineno}" for relative, lineno in _nested_intra_package_imports(path)
            )

        self.assertEqual(
            offenders,
            [],
            "以下位置在函数体内 import 了 xvt.pipeline 内部模块，"
            f"请改为模块顶层 import 或下移共享逻辑: {offenders}",
        )


class IntraPackageTargetResolutionTest(unittest.TestCase):
    """解析覆盖面：六种 import 写法下守卫都要把包内依赖边**解对**。

    2026-10-20 的事后 review 逐条注入实测过：前五种在当时的实现下**静默漏判**
    （守卫全绿而反向边真实存在）；第六种（`from xvt.pipeline.transcription import
    shared` 这类）当时**会红，但归因错**——边被压扁成子包名 `transcription`，
    而真实边是 `transcription.shared`，照错误消息去查会查错模块。所以这一组测试
    钉的是「解出的边对不对」，不单是「红不红」。

    它们不是假想：`from xvt.pipeline import X` 这种写法在 `batch.py` / `cli.py` /
    `workflow.py` 与大量测试里到处都是，只是目前还没有任何**包内**模块这么写；
    `ruff check` 对多 alias 的 `import a, b` 会报 `E401` / `I001`，所以那一条另有
    一道兜底，但守卫自己不该依赖它（其余五种 ruff 全绿）。
    """

    def _targets(self, source: str, module_name: str) -> set[str]:
        return _intra_package_targets(ast.parse(source).body[0], module_name)

    def test_multi_alias_plain_import_reports_every_alias(self) -> None:
        """`import a, b` 只看第一个 alias 时，第二个会被第一个合法边掩盖。"""
        self.assertEqual(
            self._targets(
                "import xvt.pipeline.common, xvt.pipeline.transcription.resume",
                "transcription.shared",
            ),
            {"common", "transcription.resume"},
        )

    def test_package_root_submodule_import_is_an_edge(self) -> None:
        """`from xvt.pipeline import resegment` 的 node.module 不带尾点。

        原判定只认 ``startswith("xvt.pipeline.")``，所以这一整类直接放过。
        """
        self.assertEqual(
            self._targets("from xvt.pipeline import resegment", "transcription.shared"),
            {"resegment"},
        )

    def test_package_root_symbol_import_is_reported_as_the_root(self) -> None:
        """从包根取 re-export 的符号 = 依赖 ``__init__``，包内不允许。

        与 ``test_no_function_level_intra_package_imports`` 同口径：包根 import 了
        全部子模块，包内再反向依赖它就是加载期循环的来源。没有模块会声明
        ``_PACKAGE_ROOT``，所以这条边一出现就红。
        """
        self.assertEqual(
            self._targets("from xvt.pipeline import error_category", "transcription.shared"),
            {_PACKAGE_ROOT},
        )

    def test_plain_package_root_import_is_reported_as_the_root(self) -> None:
        self.assertEqual(self._targets("import xvt.pipeline", "common"), {_PACKAGE_ROOT})

    def test_relative_import_is_reported_as_the_root(self) -> None:
        """相对 import 对两道守卫原本都是隐身的（``node.module`` 不带包前缀）。

        本仓一律用绝对 import，所以这里不去解析它到底指谁，统一报成包根让它变红，
        由人决定改成哪条绝对 import。
        """
        self.assertEqual(
            self._targets("from . import common", "transcription.shared"), {_PACKAGE_ROOT}
        )
        self.assertEqual(
            self._targets("from .shared import _render_source_srt", "transcription.local"),
            {_PACKAGE_ROOT},
        )

    def test_submodule_imported_as_a_name_resolves_to_the_submodule(self) -> None:
        """`from xvt.pipeline.transcription import shared` 的真实边是子模块，不是子包。"""
        self.assertEqual(
            self._targets("from xvt.pipeline.transcription import shared", "transcription.resume"),
            {"transcription.shared"},
        )

    def test_importing_oneself_through_the_subpackage_is_not_an_edge(self) -> None:
        """`shared.py` 里 `from xvt.pipeline.transcription import shared` 是自引用，不是边。

        未修版把它压扁成 `-> transcription` 报出来，是一条**误报**（那不是依赖边）。
        取最长前缀解出 `transcription.shared`，与所在模块同名，正确地不计入。
        """
        self.assertEqual(
            self._targets("from xvt.pipeline.transcription import shared", "transcription.shared"),
            set(),
        )

    def test_self_import_is_not_an_edge(self) -> None:
        self.assertEqual(
            self._targets("from xvt.pipeline.common import write_json", "common"), set()
        )

    def test_import_from_outside_the_package_is_not_an_edge(self) -> None:
        self.assertEqual(self._targets("from xvt.models import Job", "common"), set())
        self.assertEqual(self._targets("import json", "common"), set())


if __name__ == "__main__":
    unittest.main()
