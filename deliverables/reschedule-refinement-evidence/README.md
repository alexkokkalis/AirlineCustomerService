# Reschedule Refinement Evidence

This directory is a preserved copy of the two runs behind the recorded reschedule refinement example. It contains sandbox fixture data only.

| Iteration | Transcript | Deterministic evaluation | LLM evaluation | Refinement evidence |
| --- | --- | --- | --- | --- |
| 1 — failure and correction | [transcript](iteration-1.transcript.jsonl) | [evaluation](iteration-1.deterministic-evaluation.json) | [review](iteration-1.llm-evaluation.json) | [plan](iteration-1.refinement-plan.json), [applied audit](iteration-1.refinement-applied.json) |
| 2 — verification pass | [transcript](iteration-2.transcript.jsonl) | [evaluation](iteration-2.deterministic-evaluation.json) | [review](iteration-2.llm-evaluation.json) | [no-change plan](iteration-2.refinement-plan.json) |

Iteration 1 identifies a missing `get_policy` call in the reschedule workflow, classifies it as a `prompt_issue`, and applies one prompt replacement on the dedicated ElevenLabs refinement branch. Iteration 2 repeats the same scenario and passes both evaluation layers with no further change proposed.

The accompanying [pipeline summary](../pipeline-summary-reschedule-example.json) provides the compact first-to-final comparison; the [recorded example guide](../recorded-example-reschedule.md) explains the demo sequence.
