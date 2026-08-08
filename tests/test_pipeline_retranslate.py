"""局部重译（pipeline.retranslate）的流程测试。"""

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, save_job
from xvt.models import Artifact, Cue
from xvt.pipeline import (
    apply_cue_retranslation,
    retranslate_cues,
)
from xvt.translation_provider import ProviderResponseError, TranslationProviderError


class CueRetranslationTest(unittest.TestCase):
    def test_cue_retranslation_updates_only_requested_cues_and_invalidates_burn_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", source_language="en")),
                        asdict(Cue(2, 1000, 2000, "Second", source_language="en")),
                    ]
                ),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", "旧译文一", target_language="zh-CN")),
                        asdict(Cue(2, 1000, 2000, "Second", "旧译文二", target_language="zh-CN")),
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 2000}), encoding="utf-8")
            stale_ass = artifacts / "bilingual.ass"
            stale_ass.write_text("stale", encoding="utf-8")
            stale_preview = artifacts / "preview.mp4"
            stale_preview.write_bytes(b"stale preview")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                    Artifact("bilingual_ass", str(stale_ass)),
                    Artifact("preview_video", str(stale_preview)),
                ]
            )
            job_file = save_job(job)
            manifest_path = retranslate_cues(
                job_file,
                cue_ids=(1,),
                target_language="zh-CN",
                provider="agent",
            ).paths[0]
            fingerprint = json.loads(manifest_path.read_text(encoding="utf-8"))["fingerprint"]
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "items": [{"id": 1, "text": "新译文一，"}],
                    }
                ),
                encoding="utf-8",
            )

            apply_cue_retranslation(
                job_file,
                response_path,
                cue_ids=(1,),
                target_language="zh-CN",
            )

            persisted = json.loads(job_file.read_text(encoding="utf-8"))
            repaired = json.loads(translated_path.read_text(encoding="utf-8"))

        self.assertEqual([cue["target_text"] for cue in repaired], ["新译文一", "旧译文二"])
        self.assertEqual(persisted["stages"]["subtitle_qa"]["status"], "succeeded")
        self.assertNotIn("bilingual_ass", {item["kind"] for item in persisted["artifacts"]})
        self.assertNotIn("preview_video", {item["kind"] for item in persisted["artifacts"]})

    def test_cue_retranslation_rejects_a_different_target_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "First", source_language="en"))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "First", "一", target_language="zh-CN"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 1000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            job_file = save_job(job)
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "one"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "target language must match"):
                apply_cue_retranslation(
                    job_file,
                    response_path,
                    cue_ids=(1,),
                    target_language="en",
                )

    def test_cue_retranslation_agent_writes_selected_cue_with_original_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", source_language="en")),
                        asdict(Cue(2, 1000, 2000, "Second", source_language="en")),
                        asdict(Cue(3, 2000, 3000, "Third", source_language="en")),
                    ]
                ),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", "一", target_language="zh-CN")),
                        asdict(Cue(2, 1000, 2000, "Second", "二", target_language="zh-CN")),
                        asdict(Cue(3, 2000, 3000, "Third", "三", target_language="zh-CN")),
                    ]
                ),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                ]
            )
            job_file = save_job(job)

            result = retranslate_cues(
                job_file,
                cue_ids=(2,),
                target_language="zh-CN",
                provider="agent",
            )

            batch_data = json.loads(result.paths[0].read_text(encoding="utf-8"))
            persisted = json.loads(job_file.read_text(encoding="utf-8"))

            stale_response = root / "stale-response.json"
            stale_response.write_text(
                json.dumps(
                    {
                        "fingerprint": batch_data["fingerprint"],
                        "items": [{"id": 2, "text": "旧上下文生成的译文"}],
                    }
                ),
                encoding="utf-8",
            )
            current = json.loads(translated_path.read_text(encoding="utf-8"))
            current[1]["target_text"] = "等待期间人工改过"
            translated_path.write_text(json.dumps(current), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "manifest no longer matches current inputs"):
                apply_cue_retranslation(
                    job_file,
                    stale_response,
                    cue_ids=(2,),
                    target_language="zh-CN",
                )

            with patch("xvt.pipeline.retranslate.TRANSLATION_PROMPT_VERSION", 4):
                old_manifest_path = retranslate_cues(
                    job_file,
                    cue_ids=(2,),
                    target_language="zh-CN",
                    provider="agent",
                ).paths[0]
            old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
            old_prompt_response = root / "old-prompt-response.json"
            old_prompt_response.write_text(
                json.dumps(
                    {
                        "fingerprint": old_manifest["fingerprint"],
                        "items": [{"id": 2, "text": "旧提示词生成的译文"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "manifest no longer matches current inputs"):
                apply_cue_retranslation(
                    job_file,
                    old_prompt_response,
                    cue_ids=(2,),
                    target_language="zh-CN",
                )

        batch = batch_data["batches"][0]
        self.assertEqual(result.mode, "agent")
        self.assertEqual(batch_data["prompt_version"], 5)
        self.assertEqual(batch_data["task"], "retranslate_cues")
        self.assertIsInstance(batch_data["fingerprint"], str)
        self.assertEqual([item["id"] for item in batch["items"]], [2])
        self.assertEqual([item["id"] for item in batch["context_before"]], [1])
        self.assertEqual([item["id"] for item in batch["context_after"]], [3])
        self.assertEqual(persisted["stages"]["translate"]["status"], "awaiting_input")

    def test_cue_retranslation_uses_independent_retry_budgets_and_events(self) -> None:
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"source_language": "en"})
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello", source_language="en"))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                1000,
                                "Hello",
                                "旧译文",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 1000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            job_file = save_job(job)
            corrections = []

            def flaky_provider(batch, config, *, correction=None, **_kwargs):
                corrections.append(correction)
                if len(corrections) == 1:
                    raise TranslationProviderError("disconnected", retryable=True)
                if len(corrections) == 2:
                    raise ProviderResponseError("invalid response")
                return {1: "新译文"}

            events = []
            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch("xvt.pipeline.translation.translate_batch", side_effect=flaky_provider),
                patch("xvt.pipeline.translation.time.sleep") as sleep,
            ):
                retranslate_cues(
                    job_file,
                    cue_ids=(1,),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=1,
                    event_sink=events.append,
                )

            repaired = json.loads(translated_path.read_text(encoding="utf-8"))

        self.assertEqual(corrections[0:2], [None, None])
        self.assertIn("previous response was invalid", corrections[2] or "")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 0.5])
        self.assertEqual(repaired[0]["target_text"], "新译文")
        batch_events = [
            event
            for event in events
            if event.type
            in {
                "translation_batch_started",
                "translation_batch_retrying",
                "translation_batch_completed",
            }
        ]
        self.assertEqual(
            [event.type for event in batch_events],
            [
                "translation_batch_retrying",
                "translation_batch_retrying",
            ],
        )
        self.assertTrue(all(event.data["batch_total"] == 1 for event in batch_events))


if __name__ == "__main__":
    unittest.main()
