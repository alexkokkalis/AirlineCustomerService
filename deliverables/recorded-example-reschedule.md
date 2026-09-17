# Recorded Example Run: Reschedule Refinement

This evidence package captures a completed two-iteration autonomous refinement run for `reschedule_existing_booking`.

## Starting scenario

The simulated customer asks to reschedule a verified Athens-to-Berlin booking to a later available option. The scenario requires Erling to verify the booking, retrieve the voluntary-change policy, search flights and seats, produce a non-mutating quote, receive explicit confirmation, and then apply the reschedule.

## Starting prompt excerpt

The following was the exact prompt section targeted by the Refiner before iteration 1:

```text
- For a reschedule, retrieve the booking, retrieve the relevant voluntary-change policy, search eligible replacement flights on the same route, and check the selected replacement seat. After the customer chooses a replacement flight and seat, call quote_reschedule before presenting any final amount due or asking for final confirmation; do not derive the final cost from fare or policy details.
```

## Iteration 1

- Deterministic result: **failed** — required `get_policy` was missing.
- LLM result: **8/10** overall.
- Lowest criterion: **API usage, 7/10**.
- Root cause: **prompt issue**. The tool audit showed that Erling progressed through booking lookup, flight/seat search, quote, confirmation, and reschedule without retrieving the voluntary-change policy.

## Applied refinement

The Refiner selected one prompt replacement on the dedicated ElevenLabs refinement branch. No source-code change was made.

```diff
- For a reschedule, retrieve the booking, retrieve the relevant voluntary-change policy, search eligible replacement flights on the same route, and check the selected replacement seat.
+ For a reschedule, retrieve the booking, then call get_policy for the voluntary-change policy before searching flights, checking seats, or quoting.
```

## Final refined prompt excerpt

```text
- For a reschedule, retrieve the booking, then call get_policy for the voluntary-change policy before searching flights, checking seats, or quoting. After the customer chooses a replacement flight and seat, call quote_reschedule before presenting any final amount due or asking for final confirmation; do not derive the final cost from fare or policy details.
```

## Iteration 2 / verification

- Deterministic result: **passed**.
- LLM result: **10/10** overall; all four criteria scored 10.
- The tool audit confirmed the required order: `get_booking` → `get_policy` → flight/seat search → `quote_reschedule` → explicit confirmation → `reschedule_booking`.
- The Refiner returned `no_change`, ending the loop.

## Performance summary

| Measure | Iteration 1 | Iteration 2 |
| --- | ---: | ---: |
| Deterministic status | Failed | Passed |
| LLM overall score | 8 | 10 |
| API usage score | 7 | 10 |
| Root-cause decision | `prompt_refinement_needed` | `passed` |

The corresponding machine-readable export is [pipeline-summary-reschedule-example.json](pipeline-summary-reschedule-example.json). The complete copied transcript, deterministic evaluation, LLM evaluation, plan, and apply evidence is in [reschedule-refinement-evidence](reschedule-refinement-evidence/README.md). The live system produces the same summary schema automatically at `logs/pipeline_summaries/<pipeline_id>.json` for each refinement pipeline.

## Recording checklist

For a screen recording, show these in order:

1. The scenario selected in `/dashboard`.
2. Iteration 1 conversation and evaluation panel showing the missing `get_policy` failure.
3. The refinement panel and diff panel showing the one-line prompt change.
4. Iteration 2 conversation, evaluation scores, and `no_change` result.
5. This summary file or the generated pipeline JSON to show first-to-final improvement.

Use a dedicated refinement branch and a sandbox database. Do not show `keys.env`, secret headers, provider API keys, or real customer data in the recording.
