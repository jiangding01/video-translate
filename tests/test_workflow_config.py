import tempfile
import unittest
from pathlib import Path

from xvt.job_store import create_job, save_job
from xvt.models import Artifact
from xvt.recovery import file_checksum
from xvt.translate import GlossaryTerm, TranslationPolicy
from xvt.workflow import WorkflowOptions
from xvt.workflow_config import (
    WorkflowConfig,
    clear_burn_fingerprint,
    get_workflow_config,
    require_workflow_config,
    resolve_target_language,
    set_workflow_config,
)


class WorkflowConfigTest(unittest.TestCase):
    def test_round_trip_preserves_fields(self) -> None:
        policy = TranslationPolicy(
            style="natural",
            terms=(GlossaryTerm("API", "接口"),),
        )
        config = WorkflowConfig(
            target_language="zh-CN",
            requested_outputs=("translated_subtitles", "bilingual_subtitles", "burned_video"),
            provider="openai-compatible",
            base_url="https://example.com/v1",
            model="gpt-test",
            batch_size=20,
            max_retries=3,
            timeout_seconds=90.0,
            translation_policy=policy,
            translation_quality="polished",
            segmentation_preset="compact",
            burn=True,
            burn_media="/tmp/video.mp4",
            burn_output="/tmp/out.mp4",
            hardware_encoding=False,
            target_font="Source Han Sans",
            source_font="Arial",
            burn_fingerprint="abc123",
        )
        restored = WorkflowConfig.from_dict(config.to_dict())
        self.assertEqual(restored, config)

    def test_from_dict_applies_defaults_and_preserves_unknown(self) -> None:
        config = WorkflowConfig.from_dict(
            {
                "target_language": "zh-CN",
                "future_field": {"nested": True},
                "requested_outputs": ["translated_subtitles", "future_output"],
            }
        )
        self.assertEqual(config.target_language, "zh-CN")
        self.assertEqual(config.provider, "auto")
        self.assertEqual(config.translation_quality, "standard")
        self.assertFalse(config.burn)
        self.assertIsNone(config.burn_fingerprint)
        self.assertEqual(config.requested_outputs, ("translated_subtitles", "future_output"))
        # 未知顶层字段 round-trip，避免指纹更新抹掉新版本扩展。
        self.assertEqual(config.extras, {"future_field": {"nested": True}})
        self.assertEqual(config.to_dict()["future_field"], {"nested": True})

    def test_to_dict_omits_absent_burn_fingerprint(self) -> None:
        data = WorkflowConfig(target_language="zh-CN").to_dict()
        self.assertNotIn("burn_fingerprint", data)
        data_with = WorkflowConfig(target_language="zh-CN", burn_fingerprint="fp").to_dict()
        self.assertEqual(data_with["burn_fingerprint"], "fp")

    def test_fingerprint_clear_removes_artifact_and_legacy_workflow_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            root = Path(directory)
            fingerprint_path = root / "burn-fingerprint.json"
            fingerprint_path.write_text('{"fingerprint": "old"}\n', encoding="utf-8")
            job.artifacts.append(
                Artifact("burn_fingerprint", str(fingerprint_path), file_checksum(fingerprint_path))
            )
            # 模拟历史 Job：字段不全 + 带未知扩展。
            job.config["workflow"] = {
                "target_language": "zh-CN",
                "burn": True,
                "burn_fingerprint": "old",
                "future_flag": True,
            }
            self.assertTrue(clear_burn_fingerprint(job))
            raw = job.config["workflow"]
            self.assertEqual(
                raw,
                {
                    "target_language": "zh-CN",
                    "burn": True,
                    "future_flag": True,
                },
            )
            self.assertFalse(any(item.kind == "burn_fingerprint" for item in job.artifacts))
            self.assertFalse(clear_burn_fingerprint(job))

    def test_from_dict_rejects_invalid_types(self) -> None:
        with self.assertRaisesRegex(ValueError, "target_language"):
            WorkflowConfig.from_dict({"target_language": 1})
        with self.assertRaisesRegex(ValueError, "batch_size"):
            WorkflowConfig.from_dict({"batch_size": 0})
        with self.assertRaisesRegex(ValueError, "requested_outputs"):
            WorkflowConfig.from_dict({"requested_outputs": [1]})
        with self.assertRaisesRegex(ValueError, "burn"):
            WorkflowConfig.from_dict({"burn": "yes"})
        with self.assertRaisesRegex(ValueError, "translation_quality"):
            WorkflowConfig.from_dict({"translation_quality": "premium"})
        with self.assertRaisesRegex(ValueError, "translation_quality"):
            WorkflowConfig.from_dict({"translation_quality": []})

    def test_workflow_options_persisted_config_is_typed(self) -> None:
        options = WorkflowOptions(
            input="/tmp/video.mp4",
            output="/tmp/out",
            target_language="zh-CN",
            burn=True,
            translation_style="natural",
        )
        config = options.persisted_config(TranslationPolicy(style="natural"))
        self.assertIsInstance(config, WorkflowConfig)
        self.assertEqual(config.target_language, "zh-CN")
        self.assertTrue(config.burn)
        self.assertEqual(
            config.requested_outputs,
            ("translated_subtitles", "bilingual_subtitles", "burned_video"),
        )
        self.assertEqual(config.translation_policy, TranslationPolicy(style="natural"))
        # 序列化边界仍是普通 dict，供 transcription 落盘。
        self.assertEqual(config.to_dict()["target_language"], "zh-CN")

    def test_job_helpers_resolve_language_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            self.assertIsNone(get_workflow_config(job))
            self.assertIsNone(resolve_target_language(job))

            job.config["target_language"] = "ja"
            self.assertEqual(resolve_target_language(job), "ja")

            set_workflow_config(
                job,
                WorkflowConfig(
                    target_language="zh-CN",
                    burn=True,
                    burn_fingerprint="old-fp",
                ),
            )
            save_job(job)

            loaded = require_workflow_config(job)
            self.assertEqual(loaded.target_language, "zh-CN")
            self.assertEqual(resolve_target_language(job), "zh-CN")
            self.assertTrue(clear_burn_fingerprint(job))
            self.assertIsNone(require_workflow_config(job).burn_fingerprint)
            self.assertFalse(clear_burn_fingerprint(job))

    def test_require_workflow_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            with self.assertRaisesRegex(ValueError, "no persisted workflow goal"):
                require_workflow_config(job)

    def test_legacy_job_shape_loads(self) -> None:
        # 历史 Job 可能缺少 burn_fingerprint 字段，且 translation_policy 为 null。
        legacy = {
            "target_language": "zh-CN",
            "requested_outputs": ["translated_subtitles", "bilingual_subtitles"],
            "translation_subtitles": None,
            "provider": "auto",
            "base_url": None,
            "model": None,
            "api_key_env": "XVT_TRANSLATION_API_KEY",
            "batch_size": 40,
            "max_retries": 2,
            "timeout_seconds": 60,
            "translation_policy": None,
            "segmentation_preset": "balanced",
            "burn": False,
            "burn_media": None,
            "burn_output": None,
            "hardware_encoding": True,
            "target_font": None,
            "source_font": None,
        }
        config = WorkflowConfig.from_dict(legacy)
        self.assertEqual(config.target_language, "zh-CN")
        self.assertIsNone(config.translation_policy)
        self.assertEqual(config.translation_quality, "standard")
        self.assertIsNone(config.burn_fingerprint)


if __name__ == "__main__":
    unittest.main()
