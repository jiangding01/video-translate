---
name: video-translate
description: Transcribe local video/audio files or YouTube URLs, prepare cue-preserving translations, generate source/translated/bilingual SRT subtitles, and optionally burn bilingual ASS subtitles into MP4. Use for video transcription, subtitle translation, Chinese-English bilingual subtitles, SRT/ASS output, YouTube subtitle workflows, or subtitle burn-in tasks backed by the local xvt CLI.
---

# Video Translate

Use the repository's `xvt` CLI as the execution engine. Keep cue timing owned by the pipeline and use the agent only to translate cue text.

## Locate the project

This skill lives at `skills/video-translate`. Treat the directory two levels above this file as `PROJECT_ROOT` and run commands from there.

Before starting, run `uv run --no-sync xvt doctor`. Use `uv run --no-sync xvt ...` when the environment is already installed. If a required dependency or ASR engine is missing, report the failed check before installing anything.

## Workflow

1. Clarify only missing essentials: input, target language, requested outputs, and output directory. Make the output directory absolute.
2. Inspect the input:

   ```bash
   uv run --no-sync xvt inspect "INPUT"
   ```

3. Prefer the complete workflow when the target language is known:

   ```bash
   uv run --no-sync xvt run "INPUT" --output "/absolute/output" --engine mlx \
     --segmentation-preset balanced \
     --target-language zh-CN --burn --events-jsonl
   ```

   Add `--language LANG` only when the source language is known. Use `--engine faster` only when the user requests it or MLX is unavailable. `balanced` preserves the default ASR segmentation; use `conservative` for longer cues or `compact` for shorter cues. Omit `--target-language` and `--burn` when only source subtitles are requested.

   When the user supplies SRT, VTT, ASS, or SSA subtitles, add `--subtitles "/path/to/file"`. This preserves the supplied timeline and skips ASR. Add `--align-subtitles` only when the user wants Whisper to correct possible subtitle offset or drift. The default confidence gate is `0.55`; inspect `alignment.json` before considering `--allow-low-confidence-alignment`.

   When the user needs subtitles only and has no media file, use the source subtitle as `INPUT` directly. This creates a pure-subtitle Job and exports source, translated, and bilingual SRT files without FFmpeg or ASR:

   ```bash
   uv run --no-sync xvt run "/path/to/source.srt" --output "/absolute/output" \
     --language en --target-language zh-CN --provider auto
   ```

   Pure-subtitle Jobs preserve the source timeline and support Agent translation plus `continue`; they do not support alignment, preview, burn-in, re-segmentation, or ASS output because no media dimensions are available.

   When the user provides a directory of local media or subtitle files, use the batch workflow. It creates one independent Job per supported file and continues after individual failures:

   ```bash
   uv run --no-sync xvt batch "/path/to/input-directory" --output "/absolute/output" \
     --target-language zh-CN --provider auto --events-jsonl
   ```

   Add `--recursive` only when the user wants nested directories. Batch execution is serial. Do not silently add newly discovered files to an existing batch or promise background queue behavior.

4. Find the emitted `job.json`, then choose the translation path.

   When a compatible provider is configured, prefer the CLI translation path:

   ```bash
   uv run --no-sync xvt translate "/path/to/job.json" --target-language zh-CN
   ```

   `--provider auto` uses the API only when Base URL, model, and API Key are complete. Otherwise it creates `translation-batches.json` and enters `awaiting_input`.

5. For Agent translation, prepare batches explicitly:

   ```bash
   uv run --no-sync xvt prepare-translation "/path/to/job.json" --source-language en --target-language zh-CN
   ```

   For a new polished translation, add `--translation-quality polished`. The generated manifest uses `task=translate_and_polish`; translate, polish, and self-check in one pass, then return the manifest fingerprint and every requested ID exactly once. Do not create a second polish response for this path.

6. Read `artifacts/translation-batches.json`, including each batch's `semantic_groups`. Translate every item according to [translation-protocol.md](references/translation-protocol.md): use a complete group to keep the expression natural, but return only the existing `items` IDs in one response JSON. Never merge, split, renumber, or move text across cue IDs.
7. Apply the response:

   ```bash
   uv run --no-sync xvt apply-translation "/path/to/job.json" "/path/to/translation-response.json" --target-language zh-CN
   ```

   If the user already has translated subtitles whose cue count matches the source, skip steps 4-6 and import them directly:

   ```bash
   uv run --no-sync xvt import-translation "/path/to/job.json" "/path/to/translated.srt" --target-language zh-CN
   ```

   This deliberately keeps the source cue timing and uses only the imported translated text. Different cue counts are accepted only when timeline overlap completely covers the source cues. Report mapping failures and the `translation-mapping.json` artifact instead of hand-editing a guessed mapping.

   To polish an existing checksum-valid translation on an end-to-end Job, run:

   ```bash
   uv run --no-sync xvt polish-translation "/path/to/job.json"
   ```

   API mode checkpoints each polish batch. Agent mode requires `apply-polish-translation JOB RESPONSE`, followed by `continue`; the response must copy the current manifest's top-level `fingerprint` and cover every requested ID exactly once. Never alter cue IDs, order, source text, or timestamps in the response. This explicit existing-translation path does not support legacy atomic Jobs without `config.workflow`; use `translate --translation-quality polished` when starting a new atomic translation.

8. Before a full burn, generate a short preview when the user wants to inspect subtitle layout or timing:

   ```bash
   uv run --no-sync xvt preview "/path/to/job.json" --cue 18 --duration 15
   ```

   Preview requires checksum-valid translated cues and a passing translated subtitle QA report. It reuses the formal ASS style and encoding path without changing the persisted workflow goal. For an audio-only Job or a remote Job without a valid `source_video`, require a video via `--media`.

9. If burn-in is requested, run:

   ```bash
   uv run --no-sync xvt burn "/path/to/job.json"
   ```

   Add `--software` when hardware encoding is unavailable. For an audio-only job, require a separate video via `--media`.

10. Report the generated artifact paths and QA result. Typical outputs are `source.srt`, `translated.srt`, `bilingual.srt`, `bilingual.ass`, `preview.mp4`, and `burned.mp4`.

## Recover interrupted jobs

Default recovery path for end-to-end Jobs:

```bash
uv run --no-sync xvt status "/path/to/job.json"
# or: uv run --no-sync xvt status "/path/to/job.json" --json
uv run --no-sync xvt continue "/path/to/job.json" --events-jsonl
```

Read `status` first: it prints `recovery` category/reason, optional `steps:` (`advance_steps` aligned with `continue`), and `next` / `before` commands. Do not guess between `resume` and `continue`.

When `status` reports a source-subtitle QA error and recommends rebuilding a media Job, inspect the QA report and run the explicit repair command:

```bash
uv run --no-sync xvt rebuild-source "/path/to/job.json" --strategy asr
```

Pass `--cookies-from-browser` or `--proxy` only when remote access requires it. This command reuses checksum-valid word/audio checkpoints or downloaded media when possible, invalidates the old candidate QA, source timeline, and all derived translation/render/burn artifacts, and preserves a valid imported translation artifact for remapping onto the rebuilt cues. It is not supported for pure-subtitle Jobs, and `continue` never triggers it implicitly. If the repair is interrupted, follow the explicit `rebuild-source` command from `status`; after success, use `continue` for workflow Jobs or the `translate` command printed by the CLI for legacy atomic Jobs. Do not repeat the same rebuild when the rebuilt ASR cues themselves fail QA; that requires manual subtitle repair or a different segmentation decision.

Optionally run `uv run --no-sync xvt verify "/path/to/job.json" --json` when integrity is in doubt. `continue` can retry remote inspection or acquisition in the same Job, resume API translation or polish batches, and retry missing translation or burn outputs (including stale burn fingerprints). Pass `--cookies-from-browser` or `--proxy` again when remote access requires them. Translation waiting states require `apply-translation`, `import-translation`, or `apply-polish-translation` as directed by `status` before `continue`. Keep `xvt resume` for transcription checkpoints only (advanced). Prefer `run`/`continue` over atomic `translate`/`burn` for end-to-end goals. Never bypass a missing-file or checksum-mismatch result.

For a directory batch, use `uv run --no-sync xvt batch-continue "/path/to/batch.json" --events-jsonl`. Read each incomplete item's `recovery_plan` before claiming the batch is recoverable: retryable Jobs continue automatically, Agent waits still require translation application, and manual or unrecoverable items remain blocked. Completed items are skipped only when their Job recovery plan confirms checksum-valid outputs.

## Safety and boundaries

- Never alter cue IDs or timestamps during translation.
- Treat `semantic_groups` as read-only translation context. They describe complete cue groups within a provider batch; they do not authorize changing the response schema or cue boundaries.
- Never hand-edit generated SRT/ASS files when a pipeline command can regenerate them.
- Never pass browser cookies or a proxy by default. If YouTube access fails, explain the failure and obtain explicit user direction before using `--cookies-from-browser` or `--proxy`.
- Do not expose cookie contents, signed URLs, or proxy credentials in logs or responses.
- Treat a failed subtitle or media QA stage as a failed job; report the issue rather than claiming completion.
