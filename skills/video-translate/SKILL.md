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
     --target-language zh-CN --burn --events-jsonl
   ```

   Add `--language LANG` only when the source language is known. Use `--engine faster` only when the user requests it or MLX is unavailable. Omit `--target-language` and `--burn` when only source subtitles are requested.

   When the user supplies SRT, VTT, ASS, or SSA subtitles, add `--subtitles "/path/to/file"`. This preserves the supplied timeline and skips ASR. Add `--align-subtitles` only when the user wants Whisper to correct possible subtitle offset or drift. The default confidence gate is `0.55`; inspect `alignment.json` before considering `--allow-low-confidence-alignment`.

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

6. Read `artifacts/translation-batches.json`, translate every item according to [translation-protocol.md](references/translation-protocol.md), and create one response JSON containing all cue IDs.
7. Apply the response:

   ```bash
   uv run --no-sync xvt apply-translation "/path/to/job.json" "/path/to/translation-response.json" --target-language zh-CN
   ```

   If the user already has translated subtitles whose cue count matches the source, skip steps 4-6 and import them directly:

   ```bash
   uv run --no-sync xvt import-translation "/path/to/job.json" "/path/to/translated.srt" --target-language zh-CN
   ```

   This deliberately keeps the source cue timing and uses only the imported translated text. Different cue counts are accepted only when timeline overlap completely covers the source cues. Report mapping failures and the `translation-mapping.json` artifact instead of hand-editing a guessed mapping.

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

Run `uv run --no-sync xvt verify "/path/to/job.json" --json` before recovery. Use `xvt continue "/path/to/job.json" --events-jsonl` for a persisted end-to-end goal; it can retry remote inspection or acquisition in the same Job, resume API batches, and retry missing translation or burn outputs. Pass `--cookies-from-browser` or `--proxy` again when remote access requires them. Translation waiting states still require `apply-translation` or `import-translation` before `continue`. Keep `xvt resume` for transcription checkpoints only. Never bypass a missing-file or checksum-mismatch result.

## Safety and boundaries

- Never alter cue IDs or timestamps during translation.
- Never hand-edit generated SRT/ASS files when a pipeline command can regenerate them.
- Never pass browser cookies or a proxy by default. If YouTube access fails, explain the failure and obtain explicit user direction before using `--cookies-from-browser` or `--proxy`.
- Do not expose cookie contents, signed URLs, or proxy credentials in logs or responses.
- Treat a failed subtitle or media QA stage as a failed job; report the issue rather than claiming completion.
