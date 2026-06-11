# Running an experiment

A short guide to the end-to-end flow: create an experiment, upload questions, pilot it on Prolific, then scale up.

## 1. Create an experiment

From the **Experiments** page, fill out the create form and submit. The most important choice is the number of ratings per question — more ratings give a stronger agreement signal but cost more raters. `3` is a reasonable default.

## 2. Upload questions

Upload a CSV in the **Questions** section. At minimum each row needs a unique ID and the question text. Optional columns let you attach ground-truth answers, multiple-choice options, a question type, JSON metadata, or parent-question links for nested questions. You can upload multiple CSVs into the same experiment.

## 3. Pick an assistance method (optional)

In **Rater Assistance Methods**, decide whether the AI should help raters. Leaving everything off is the right baseline for most pilots — turn assistance on only when you specifically want to test its effect on rating quality. See [Assistance methods](#assistance-methods) below.

## 4. Pilot on Prolific

Always pilot before scaling. In the **Prolific Workflow** section, create a small unpublished study (5 raters is a good default), then **Publish** it. Use the pilot to calibrate your time estimate and reward — guess high on the first round, the platform will give you a tighter recommendation afterwards.

You can click **Preview as Participant** at any time to see exactly what raters will see.

## 5. Launch subsequent rounds

Once the pilot closes, a **Recommendation for next round** panel appears with a suggested size and reward based on the pilot's timing. Create the next round draft from there, review the prefilled values, and publish.

## 6. View results

The **Overview** section shows live progress. From there you can open analytics or export the raw ratings as CSV. The "include preview data" toggle controls whether your own test ratings count.

---

# Assistance methods

## Human-as-a-Tool (iterative subtask delegation)

The AI decomposes each question into a sequence of smaller subtasks, the rater answers them one at a time (with a confidence rating per subtask), and the AI synthesises a final recommendation from those answers. Best for questions that are hard to answer in one shot but break down naturally — e.g. a factuality check that splits into "does the source mention X?", "does it claim Y?", "is Y consistent with the source?".

When you enable it, you pick a **confidence method**:

- **Self-report** — fastest, single AI call. Fine default for piloting.
- **Sampling** — most accurate; multiple samples + clustering, but slower and more expensive.
- **Self-consistency** — multiple samples with majority vote; a middle ground.
