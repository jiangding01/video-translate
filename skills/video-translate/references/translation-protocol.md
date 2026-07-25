# Translation protocol

The same response contract is used by Agent translation and OpenAI-compatible providers. A provider may wrap the JSON in a Markdown code fence, but the parsed payload must be a JSON object with one `items` array.

Read `artifacts/translation-batches.json`. Its `batches` array contains cue items with stable integer IDs and source text. A batch can also contain `semantic_groups`, an array of cue-ID arrays. These are deterministic, complete semantic groups: use the source text of every item in the same group to make the target language read naturally, but do not change group membership, cue boundaries, IDs, order, or timing.

Return one UTF-8 JSON object in this exact shape:

```json
{
  "items": [
    {"id": 1, "text": "Translated subtitle text"},
    {"id": 2, "text": "Translated subtitle text"}
  ]
}
```

Requirements:

- Include every source cue exactly once across all batches.
- Preserve each integer `id`; do not renumber, omit, duplicate, or add IDs.
- Return only the `items` response contract above. Do not return `semantic_groups`, context items, timestamps, group summaries, or any other batch metadata.
- Translate only `text`. Do not copy timestamps or other batch metadata into the response.
- Preserve meaning, terminology, names, numbers, tone, and intentional formatting.
- Write natural subtitle language rather than literal machine-translated prose.
- `max_target_characters` is a display guideline, not permission to omit meaning, split words, or write unnatural text. Deterministic QA evaluates final length, reading speed, and display width.
- You may coordinate wording and sentence continuation inside one `semantic_groups` entry, but do not move information across groups. `context_before` and `context_after` are read-only understanding aids and must never appear in the response.
- Do not merge or split cues.
- Do not add explanations, Markdown, speaker labels, or punctuation that the source does not justify.
- Use an empty-looking source only if it is genuinely meaningful; the pipeline rejects empty translated text.

Before applying the response, verify that the response ID set exactly matches the source ID set. The CLI performs the same validation and will fail on any mismatch.
