# Running an experiment

End-to-end flow: create → upload → optionally configure AI assistance → pilot on Prolific → scale up.

## 1. Create the experiment

From the **Experiments** page, fill out the create form and submit. The main decision is **ratings per question** — more gives a stronger agreement signal but costs more raters. `3` is a reasonable default.

## 2. Upload questions

Upload a CSV or Parquet file in the **Questions** section. You can upload multiple files into the same experiment — rows accumulate. The colab notebook can generate either format from a pandas DataFrame.

| Column | Required | What it does |
| --- | --- | --- |
| `question_id` | Yes | Your unique identifier for the row. Used in exports and analytics. |
| `question_text` | Yes | The text shown to the rater. |
| `gt_answer` | No | Ground-truth answer. Used to compute agreement metrics; not shown to raters. |
| `options` | No | Pipe-separated choices for multiple-choice (e.g. `Yes\|No\|Maybe`). Required if `question_type=MC` and you want preset options. |
| `question_type` | No | `MC` (multiple-choice) or `FT` (free-text). Defaults to `MC`. |
| `metadata` | No | Per-row JSON blob you can attach for your own use. Surfaced in exports. |
| `parent_question_id` | No | The `question_id` of another row in the same experiment. Marks this row as a sub-question of that parent — the parent's text is shown above as context but the parent itself isn't rated. |

### Dataset-level metadata (optional)

Five optional fields frame the rater experience and the AI: a study description, a system prompt for the AI, a one-line prefix and suffix shown around every question, and a label for your Prolific audience. Set them via the **Dataset Metadata** form on the experiment page, or attach them to the upload itself — a `#META:` line at the top of a CSV, or a `dataset_meta` key in the Parquet schema's key-value metadata. Either way, the platform reads the same JSON shape. The [dataset-metadata colab](https://colab.research.google.com/drive/1D4bYm0mvgOWk1v8dHqaZN8yQj-bcoqdB) generates both formats. Each field in the admin form has a hover hint describing exactly where it appears.

#### Example (CSV)

```
#META: {"description":"# Reading-comprehension pilot\n\nAnswer using **only** the passage shown.","human_prompt_prefix":"Based only on the passage above, answer:","prolific_pool":"uk_representative_sample"}
question_id,question_text,gt_answer,options,question_type
q1,Is the sky blue?,Yes,Yes|No,MC
q2,Does the passage mention Paris?,No,Yes|No,MC
q3,Summarise the passage in one sentence.,,,FT
```

The `#META:` line is a single-line JSON object on the very first line. The header row and data rows follow as normal. All five keys are optional — include only what you need.

#### Example (Parquet)

For Parquet, attach the same JSON object as bytes under the `dataset_meta` key in the schema's key-value metadata. The colab notebook does this with:

```python
table = pa.Table.from_pandas(df)
merged = {**(table.schema.metadata or {}), b"dataset_meta": meta_str.encode("utf-8")}
table = table.replace_schema_metadata(merged)
pq.write_table(table, "dataset.parquet")
```

Parquet preserves column types, so `options` can be a typed `list<string>` and `metadata` can be a struct — the platform converts these to the canonical CSV string forms on ingest (pipe-joined and JSON-encoded respectively), so the rest of the platform behaves identically regardless of upload format.

#### Formatting rules

- **`description`** supports markdown — same renderer as the Prolific study description: `# H1`, `## H2`, `**bold**`, `*italic*`, `~~strike~~`, `-`/`1.` lists, blank-line paragraphs.
- The four other fields are plain text. Line breaks are preserved.
- **CSV only:** newlines inside any value must be encoded as `\n` escapes, because the whole JSON object has to fit on one line. `json.dumps(...)` handles this automatically. Parquet stores the metadata as bytes in the schema, so this constraint doesn't apply there.

#### Multiple uploads + conflicts

The **first** upload that declares a value populates the experiment. Later uploads that declare the same key with a *different* value are flagged in the Dataset Metadata section but never overwrite saved values. To change a saved value, edit it directly in the admin form.

### Always preview

Before publishing on Prolific, click **Preview as Participant** in the Prolific Workflow section. It's the only reliable way to check that the splash markdown renders right, the prefix/suffix read naturally on each question, and the assistance method behaves as you expect.

## 3. Pick an assistance method (optional)

In **Rater Assistance Methods**, decide whether the AI should help raters. Leaving everything off is the right baseline for most pilots — turn assistance on only when you specifically want to test its effect on rating quality. See [Assistance methods](#assistance-methods) below.

## 4. Pilot on Prolific

Always pilot before scaling. In the **Prolific Workflow** section, create a small unpublished study (5 raters is a good default), then **Publish** it. Use the pilot to calibrate your time estimate and reward — guess high on the first round; the platform gives a tighter recommendation afterwards.

## 5. Launch subsequent rounds

Once the pilot closes, a **Recommendation for next round** panel appears with a suggested size and reward based on the pilot's timing. Create the next round draft from there, review the prefilled values, and publish.

## 6. View results

The **Overview** section shows live progress. From there you can open analytics or export the raw ratings as CSV. The *include preview data* toggle controls whether your own test ratings count.

---

# Assistance methods

## Human-as-a-Tool (iterative subtask delegation)

The AI decomposes each question into smaller subtasks, the rater answers them one at a time (with a confidence rating per subtask), and the AI synthesises a final recommendation from those answers. Best for questions that are hard to answer in one shot but break down naturally — e.g. a factuality check that splits into "does the source mention X?", "does it claim Y?", "is Y consistent with the source?".

When you enable it, you pick a **confidence method**:

- **Self-report** — fastest, single AI call. Fine default for piloting.
- **Sampling** — most accurate; multiple samples + clustering, but slower and more expensive.
- **Self-consistency** — multiple samples with majority vote; a middle ground.

## Top-N suggestions

The AI ranks the most likely answers and shows the rater a short ordered list before they submit. One-shot, lightweight. Set the number of suggestions to show (capped by the number of MC options when applicable).
