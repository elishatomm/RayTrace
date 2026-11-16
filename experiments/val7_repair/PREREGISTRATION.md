# Pre-registration: does val-7's repair call recover the answers it refuses?

Written 2026-11-13, before any measurement. The owner chose this measurement ("Measure first") and its decision rule, so both are fixed here before results exist.

## Why

Validator `val-7` adds two rules to `val-6` (owner decision Q37):

- (a) refuse a rationale that names a deployment ID the packet does not list as a candidate;
- (b) refuse a named faulting change unless an item cited as supporting mentions it.

Offline, on 858 stored model diagnoses, val-7 newly refuses 43 that val-6 accepts. 41 of those name the correct deployment and 33 are outcome-correct (`docs/agent_reports/val7_grounded_attributions.md`). A refused answer gets one repair call with the validator's reasons. Whether that repair recovers the answer is unmeasured, and it decides whether val-7 costs correct answers.

## What is measured

- **The application.** `v1-followups` at the commit that adds val-7, run as the application runs it:
  - prompt `diag-p5`;
  - validator `val-7`;
  - one repair call on refusal;
  - local model `qwen3:8b` through Ollama on 127.0.0.1, at the digest the README records (`500a1f067a9f…`).
- **Sample S, primary.** The distinct stored packets behind the 43 answers above, each diagnosed twice. Two identical outputs count once, and the report says so.
- **Sample C, secondary.** The 48 frozen M4 packets (30 test, 18 development), each diagnosed once. This shows how often a current first answer triggers val-7 at all on representative data.
- **Per diagnosis, recorded:**
  - the first answer; val-6's verdict; val-7's verdict and reasons;
  - the repaired answer, when there is one; the final verdict;
  - outcome and change correctness against the oracle;
  - the added latency.

## Primary metric and decision rule

- **A trigger** is a first answer that val-6 accepts and val-7 refuses.
- **A trigger is recovered** when its repaired answer passes val-7 and its outcome correctness is not worse than the first answer's.
- **R** is the share of triggers recovered, over S and C together.

**Decision (fixed by the owner, 2026-11-13):**

| Triggers | R | Ship |
|---|---|---|
| 10 or more | at least 0.9 | val-7 as built: both rules refuse, with one repair |
| 10 or more | below 0.9 | rule (a) refuses; rule (b) becomes a visible flag on an accepted answer, not a refusal |
| fewer than 10 | every trigger recovered | val-7 as built |
| fewer than 10 | any trigger not recovered | as in the second row |

**Reported, not decided on:**

- the trigger rate in C;
- the latency the repairs add;
- the final answers' correctness against a val-6 run of the same first answers.

## Conditions

- **Stop and report** if Ollama is unavailable or the model digest differs. Never pull a model.
- **The live run happens on the host,** because the application accepts only a loopback model URL. Databases are throwaway, in scratch folders. Ollama is stopped afterwards.
- **No other network.** No change to M4: it stays a `val-4` result.
- **Outputs** go to this folder: the answers, with packet hashes, and `report.json`.
