# Assistance preparation

This stack adds preparation in five reviewable stages:

1. Optional method contract and first-step adapter (this change).
2. Durable, fenced execution with bounded in-process workers.
3. Server-owned active question and one reserved successor.
4. A frontend queue owner that activates before display.
5. Measurement, disabled-by-default rollout, and operating guidance.

## Extending a method

`AssistanceMethod` defaults to no preparation. Opt in by implementing:

- `plan_preparation(context)`: pure, deterministic, inexpensive; return a
  `PreparationSpec` with a stable name, version, and JSON input snapshot.
- `prepare(spec)`: compute one private JSON artifact. Work must be bounded,
  safe to abandon, and require no future human answer.
- `consume_preparation(spec, artifact)`: validate the artifact and return an
  interaction step. Partial artifacts may require foreground composition.
  Never silently regenerate a prepared artifact.

Use `InitialStepPreparation` when the entire first step can run early. It
calls the existing `start` implementation, preserving prompts, models,
randomization, scoring, and private interaction state. Top-N and Human-as-a-Tool
use this adapter. Human-as-a-Tool prepares only its initial decomposition and
confidence scoring; `advance` still requires actual human input.

Bump the preparation version whenever artifact or input interpretation changes.
The runtime owns identity, participant isolation, persistence, deadlines, and
concurrency. Methods do not choose shared cache keys. Snapshots deliberately
exclude ground truth and upload metadata. Private artifacts and method state
must never be returned as queue metadata.

No background work is scheduled by the contract alone. Existing foreground
behavior remains unchanged at this stage.
