# Translation protocol

The same response contract is used by Agent translation and OpenAI-compatible providers. A provider may wrap the JSON in a Markdown code fence, but the parsed payload must be a JSON object with one `items` array.

Read `artifacts/translation-batches.json`. Its `batches` array contains cue items with stable integer IDs and source text. A batch can also contain `semantic_groups`, an array of cue-ID arrays. These are deterministic, complete semantic groups: use the source text of every item in the same group to make the target language read naturally, but do not change group membership, cue boundaries, IDs, order, or timing.

For a standard translation manifest, return one UTF-8 JSON object in this exact shape:

```json
{
  "items": [
    {"id": 1, "text": "Translated subtitle text"},
    {"id": 2, "text": "Translated subtitle text"}
  ]
}
```

When the manifest has `task=translate_and_polish`, translate, polish, and self-check in one pass. Copy the manifest's top-level `fingerprint` into the response:

```json
{
  "fingerprint": "manifest fingerprint",
  "items": [
    {"id": 1, "text": "Final polished subtitle text"},
    {"id": 2, "text": "Final polished subtitle text"}
  ]
}
```

Requirements:

- Include every source cue exactly once across all batches.
- Preserve each integer `id`; do not renumber, omit, duplicate, or add IDs.
- Return only the applicable response contract above. Do not return `semantic_groups`, context items, timestamps, group summaries, or any other batch metadata.
- For `task=translate_and_polish`, return the manifest fingerprint unchanged. For a standard manifest, do not add a fingerprint.
- Translate only `text`. Do not copy timestamps or other batch metadata into the response.
- Preserve meaning, terminology, names, numbers, tone, and intentional formatting.
- Write natural subtitle language rather than literal machine-translated prose.
- `max_target_characters` is a display guideline, not permission to omit meaning, split words, or write unnatural text. Deterministic QA evaluates final length, reading speed, and display width.
- You may coordinate wording and sentence continuation inside one `semantic_groups` entry, but do not move information across groups. `context_before` and `context_after` are read-only understanding aids and must never appear in the response.
- Do not merge or split cues.
- Do not add explanations, Markdown, speaker labels, or punctuation that the source does not justify.
- Use an empty-looking source only if it is genuinely meaningful; the pipeline rejects empty translated text.

Before applying the response, verify that the response ID set exactly matches the source ID set. The CLI performs the same validation and will fail on any mismatch.

## Semantic review and one repair

When the manifest has `task=semantic_review_and_repair`, copy its fingerprint and return every review batch exactly once in `initial_results`. Issues may use only `meaning_error`, `omission`, `terminology`, `awkward_join`, or `source_suspect`, always with `severity=review`. Report only genuine issues; never propose timing, ID, source-text, or cue-boundary changes.

For repairable issues, return every repair batch exactly once in `repair_results`, covering every item in the requested complete semantic group, including unchanged items. Never repair `source_suspect`; it remains pending even when unrelated issues are repaired. Review the repaired candidate once and return every final batch in `final_results`. If no repairable issue exists, both arrays must be empty. The CLI validates the entire combined response before replacing the waiting manifest, so a rejected response can be corrected and submitted again.

```json
{
  "fingerprint": "manifest fingerprint",
  "initial_results": [
    {
      "batch": 1,
      "issues": [
        {
          "category": "meaning_error",
          "cue_ids": [1],
          "severity": "review",
          "message": "Meaning changed",
          "suggested_items": [{"id": 1, "text": "Suggested text"}]
        }
      ]
    }
  ],
  "repair_results": [
    {"batch": 1, "items": [{"id": 1, "text": "Repaired text"}]}
  ],
  "final_results": [{"batch": 1, "issues": []}]
}
```

When a semantic manifest has `task=semantic_review_only`, review the human-revised candidate once. Copy its fingerprint and return every batch exactly once in `initial_results`; omit `repair_results` and `final_results` (or return empty arrays). Human revision does not grant another automatic repair pass.
