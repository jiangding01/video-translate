import re
import unittest
from pathlib import Path

from xvt import config
from xvt.workflow import WorkflowOptions

_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "xvt"


class ConfigDefaultsTest(unittest.TestCase):
    def test_default_max_retries_is_four(self) -> None:
        self.assertEqual(config.DEFAULT_MAX_RETRIES, 4)

    def test_translation_defaults_are_conservative_for_remote_provider(self) -> None:
        self.assertEqual(config.DEFAULT_BATCH_SIZE, 25)
        self.assertEqual(config.DEFAULT_TIMEOUT_SECONDS, 180)

    def test_workflow_options_defaults_match_config_constants(self) -> None:
        options = WorkflowOptions(input="/tmp/video.mp4", output="/tmp/out")
        self.assertEqual(options.engine, config.DEFAULT_ENGINE)
        self.assertEqual(options.segmentation_preset, config.DEFAULT_SEGMENTATION_PRESET)
        self.assertEqual(options.provider, config.DEFAULT_PROVIDER)
        self.assertEqual(options.batch_size, config.DEFAULT_BATCH_SIZE)
        self.assertEqual(options.max_retries, config.DEFAULT_MAX_RETRIES)
        self.assertEqual(options.timeout_seconds, config.DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(options.api_key_env, config.DEFAULT_API_KEY_ENV)
        self.assertEqual(options.translation_style, config.DEFAULT_TRANSLATION_STYLE)
        self.assertEqual(
            options.minimum_alignment_confidence,
            config.DEFAULT_MIN_ALIGNMENT_CONFIDENCE,
        )
        self.assertEqual(options.hardware_encoding, config.DEFAULT_HARDWARE_ENCODING)

    def test_config_module_stays_dependency_free(self) -> None:
        source = (_SRC_DIR / "config.py").read_text(encoding="utf-8")
        self.assertNotRegex(
            source, r"^\s*(?:import|from)\s+xvt", "config.py must not import xvt modules"
        )

    def test_balanced_literal_absent_from_converged_default_sites(self) -> None:
        # config.py 是默认值的单一来源；segment.py 保留预设定义；pipeline.py 仍在
        # 内部函数签名默认值中使用该字面量（本次收敛的范围只覆盖 config-dict 兜底，
        # 不含函数签名默认值）。这里守护已收敛的入口 cli.py 与 workflow.py 不再回归
        # 手写 "balanced"，并确认 config.py 存在单一来源。
        counts = {
            path.name: len(re.findall(r'"balanced"', path.read_text(encoding="utf-8")))
            for path in _SRC_DIR.rglob("*.py")
        }
        self.assertEqual(counts.get("config.py"), 1)
        self.assertEqual(counts.get("cli.py", 0), 0)
        self.assertEqual(counts.get("workflow.py", 0), 0)


if __name__ == "__main__":
    unittest.main()
