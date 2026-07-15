# Translation protocol

The same response contract is used by Agent translation and OpenAI-compatible providers. A provider may wrap the JSON in a Markdown code fence, but the parsed payload must be a JSON object with one `items` array.

Read `artifacts/translation-batches.json`. Its `batches` array contains cue items with stable integer IDs and source text.

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
- Translate only `text`. Do not copy timestamps or other batch metadata into the response.
- Preserve meaning, terminology, names, numbers, tone, and intentional formatting.
- Write natural subtitle language rather than literal machine-translated prose.
- Keep each result concise enough for the source cue's display duration. Do not merge or split cues.
- Do not add explanations, Markdown, speaker labels, or punctuation that the source does not justify.
- Use an empty-looking source only if it is genuinely meaningful; the pipeline rejects empty translated text.

Before applying the response, verify that the response ID set exactly matches the source ID set. The CLI performs the same validation and will fail on any mismatch.
