# Prompt for Claude Code: SenCity pilot 2 (survey v2)

The smoke test showed the pipeline works. The survey has since changed: 6 places, shorter lists ordered by sense, no senses grid, renumbered questions. This run adapts the existing pipeline to survey v2 and runs it on a new small pilot. It is still **not** analysis and **not** model selection. Fix only format, instructions and limits; never tune prompts to improve scores.

Ask me before adding any dependency and before any step that costs money beyond the cap.

## 0. Keep the smoke test as it is

- Before changing anything: commit, then tag the current state `smoke-test-v1`. Leave `runs/<smoke run id>/` and `docs/smoke-test-prompt.md` untouched; they document what was run on survey v1.
- Replace `config/codebook.yaml` with the new one I provide (survey v2). Keep the old one as `config/codebook_v1.yaml` so the smoke-test run stays reproducible.
- Save this prompt as `docs/pilot2-prompt.md`.

## Context

Master's thesis (IAAC, urban planning): *Can AI personas perceive a public space the way autistic and sensory-sensitive people do, and does lived experience reduce bias better than demographic labels?*

- Two Google Forms with identical structure, one Italian and one English. **Part A** is the profile; **Part B** shows 6 photos of public places in Vienna (shopping street, park, traffic street, residential area, square, transport hub) with the same questions for each.
- Personas answer **Part B only**, looking at the same photos. Four versions, a 2 × 2 of labels × lived experience:

| | No lived experience | Lived experience (Q13–Q20) |
|---|---|---|
| **No labels** | V0: no persona | V3: lived experience only |
| **Labels (Q1–Q5, Q7–Q12)** | V1: labels only | V2: labels + lived experience |

- Main comparison for the thesis: **V1 vs V3**. Secondary: V2 vs V3 (do labels add stereotyping?) and V2 vs V1.
- Q12 (meltdowns) is a **label** (V1), not lived experience, so V3 contains no clinical labels.
- Q6 (familiarity with Vienna) and B1 (familiarity with each place) are never given to a persona.
- Closed answers are mapped to codes and English labels through the codebook. **Open answers are never translated** (see Step 1).

### What changed from survey v1

| v1 | v2 |
|---|---|
| 4 places | 6 places |
| Q16 senses grid (1–5) | removed; attention check moved into the Q14 grid |
| Q12 meltdowns in V2 | Q12 meltdowns in V1 (label) |
| Q13 "what helps" (neurodivergent branch only) | Q19 "what helps", asked to everyone, shorter |
| Q20 "what do you do" | removed (merged into Q19) |
| long element lists | Q15, Q16, B4, B5: 7 options, one per sense, same order (`sense` tag in the codebook) |
| B4 senses question | removed |
| B5–B8 | B4 bother, B5 feel good, B6 changes (7 senses + space, predictability, recovery), B7 comment |
| "up to 3" | "choose 1 to 3" |

Part A is now Q1–Q20: Q1–Q6 about you, Q7–Q12 health and neurodivergence (Q9–Q12 only if Q8 = yes or not sure), Q13–Q19 senses and public spaces, Q20 open answer.

## Repository

Same structure as the smoke test, with these changes:

```
config/codebook.yaml          # v2, provided by me
config/codebook_v1.yaml       # old, for the smoke test only
config/run.yaml
data/raw/responses_it.csv     # gitignored
data/raw/responses_en.csv     # gitignored
data/photos/place1.jpg … place6.jpg   # in survey order
prompts/persona_v0.txt … persona_v3.txt
prompts/task_part_b.txt
docs/smoke-test-prompt.md     # unchanged
docs/pilot2-prompt.md         # this file
forms/build.py                # one source: writes config/codebook.yaml and forms/Code.gs
forms/Code.gs                 # Google Apps Script that builds both forms
```

`.env`, `data/raw/`, `data/clean/` and raw run outputs stay out of git.

## Model and run settings

`config/run.yaml`:
```yaml
model: "anthropic/claude-haiku-4.5"   # 5.5 rejects a temperature setting; keep 4.5 so temperature is fixed and recorded
temperature: 0.7
runs_per_condition: 2                 # smoke test: B2 identical across runs in 10–11 of 12 places
max_calls: 200
max_cost_usd: 2.00
```
Keep zero-data-retention routing on and record the model id returned by the API in every log line. No free endpoints.

Calls: participants × 4 versions × 6 places × 2 runs (2 participants = 96, 3 = 144). `--dry-run` first: mapping table, one full prompt per version, call count, estimated cost. Show me and wait.

## Step 1: parse the responses

Keep the parser rules from the smoke test, updated for v2:

- **Two files.** Parse `responses_it.csv` and `responses_en.csv` with the same codebook (match `it` labels in the Italian file, `en` labels in the English one). Record each participant's form language (`it` / `en`).
- **The forms are built by `forms/Code.gs`** from the same source as the codebook, so every question title (`title_it`, `title_en`) and option label (`it`, `en`) in the codebook is the exact form text.
- **Headers** have no question numbers: map columns by the codebook titles and by section order. Part B repeats once per place: `P1_B1` … `P6_B7`. Grid rows (Q14, B3) are one column each; expand them from the header text. **Print the mapping table and stop so I can confirm it.**
- **Consent:** keep a participant only if both consent statements are ticked.
- **Multi-select** cells are separated by `;`.
- **Attention check** is now the last row of the **Q14** grid ("Controllo: per questa riga seleziona 2" / "Check: for this row, select 2"): record pass/fail, then drop the row.
- **"Other"** is Google's built-in option: the export contains only the typed text, so any unmatched value in a question that has an `other` option is coded `other` + text. **Unmatched answers** elsewhere: list them for me. Remappings I have decided go in a `remap:` section of the codebook, never silently in code.
- **Free text** (Q20, B7 and every "Other"): never translated, kept verbatim, with the form language recorded.
- **IDs:** `P01`, `P02`… numbered across both forms. No names, emails or timestamps stored.

## Step 2: build the personas

Render from the codebook's English labels: use `persona_en` where given, otherwise `en` with the first letter lowercased (except ADHD, PTSD, DCD). Wording below includes the grammar fixes from the smoke test (one "You live in" phrase, short sentences, `;` between list items, lowercase labels).

`prompts/system.txt` (unchanged)
```
You will answer a short survey about public spaces as a specific person. Answer as that person would, from their perspective, not as an assistant and not as an average person. If something is not stated about them, do not invent it. Answer only with JSON that follows the schema.
```

`prompts/persona_v0.txt`
```
You are a person taking part in a survey about public spaces in Vienna.
```

`prompts/persona_v1.txt` (labels)
```
You are a person taking part in a survey about public spaces in Vienna. About you:
- You are {Q1} years old; gender: {Q2}. You live in {Q4}, {Q3}. Education: {Q5}.
- Health conditions that affect how you experience public spaces: {Q7}.
- Neurodivergent: {Q8}. Terms that describe you: {Q9}. This is based on: {Q10}. Support you need in daily life: {Q11}. Meltdowns or shutdowns in public spaces: {Q12}.
```

Lived-experience block (used by V2 and V3):
```
How you experience public spaces, in your own answers:
- How often public spaces overwhelm you (1 = never, 5 = almost always): {Q13}.
- How much each situation makes it hard for you to be in a public space (1 = not at all, 5 = very much): {Q14 rows}.
- What bothers you: {Q15}.
- What makes you feel good: {Q16}.
- Places you avoid or only visit when you must: {Q17}.
- What makes all sensations more intense for you: {Q18}.
- What helps you when a place overwhelms you: {Q19}.
- In your own words (quoted exactly as you wrote it, possibly in Italian): "{Q20}"
```

- `persona_v2.txt` = V1 + the block. `persona_v3.txt` = V0's sentence + the block.
- **Blanks:** Q9–Q12 are skipped when Q8 = no: write "not applicable" in V1/V2 (not "none"). "None" answers are written as "none". Lines whose answer is "Prefer not to say" are omitted. Q20 left empty: omit the line.
- **Label leaks in V3:** Q20 and "Other" texts may mention a diagnosis (e.g. "as an autistic person…"). Keep them verbatim (they are the person's words), but count and list the V3 prompts that contain a diagnosis keyword (autis*, ADHD, neurodiverg*, diagnos*, and the Q7 conditions), so the analysis can check them.

`prompts/task_part_b.txt`
```
Look at the photo and imagine spending 20 minutes here on a weekday afternoon.
[V1–V3 only:] Your general answers are above. Now rate this specific place: it may be easier or harder for you than public spaces in general.
Answer these questions about this place:
B2. How comfortable would you feel spending 20 minutes here? (1 = not at all … 5 = very comfortable)
B3. How much would each of these situations be a problem for you here? (1 = not at all … 5 = very much) {B3 rows}
B4. Which elements in the photo would bother you here? Choose 1 to 3. {options}
B5. Which elements in the photo would make you feel good here? Choose 1 to 3. {options}
B6. Which changes would make this place better for you? Choose 1 to 3. {options}
Then, in "reasoning", explain in 2–4 sentences why, from your perspective.
```
Options as `code: label`, shuffled each run, with "none", "no_change_needed" and "other" always last. Save the order used.

## Step 3: run

One fresh call per participant × version × place × run, as in the smoke test (photo max 1024 px, JPEG). Schema:
```json
{"B2_comfort": 3,
 "B3_situations": {"sensory_overload": 4, "space_narrow_or_wide": 5, "uncertainty": 2, "no_place_to_retreat": 4},
 "B4_elements_bother": ["hearing", "body_in_space"],
 "B5_elements_feel_good": ["touch_temperature"],
 "B6_changes": ["recovery", "hearing"],
 "reasoning": "..."}
```
Rules: integers 1–5; B4–B6 lists of **1 to 3** codes from that question; exclusive codes ("none", "no_change_needed") only alone. Invalid → one retry with the error appended; then log as invalid. API errors as in the smoke test: 429 wait and retry, 402 stop, `--resume` continues a stopped run; only completed calls count toward `max_calls`. Same `calls.jsonl` fields.

## Step 4: score and report

Each persona is compared with **its own participant**. Outcomes, fixed before the data:

- **Primary: B3**, mean absolute error over the 4 situations (also per factor).
- **Secondary: B2**, absolute error and the Spearman correlation of the 6 places within each person.
- **B4–B6:** Jaccard, precision and recall. Also per sense (from the `sense` tags) and, for B6, per factor.

Baselines for every score:
- average human (leave-one-out mean of the other participants);
- chance (random valid answers);
- B2 always 3;
- **copy Part A:** B3 = Q14 rows; B4 = Q15 codes; B5 = Q16 codes (same sense codes, so they compare directly).

`runs/<run_id>/report.md`, with `pilot: true`:
1. **Summary:** 5–8 plain-language bullets at the top.
2. **Format:** valid / total, retries and why (check that "choose 1 to 3" removed the 4-item errors).
3. **Scale use:** B2 and B3 distributions, personas vs humans.
4. **Scores** by version and participant, next to the baselines.
5. **Copy check:** persona vs copy baseline, and how far persona B3 is from the person's own Q14.
6. **Bias:** signed error per version, **and per participant**.
7. **Diversity:** spread between people per version vs the humans'.
8. **2 × 2 view:** effect of adding lived experience and of adding labels.
9. **Version pairs:** every pair of V0–V3, difference vs run-to-run spread (with 2 runs: the difference between the two runs).
10. **B2 per participant:** a table of each person's B2 per place next to each version's, so we can see why some orders are reversed.
11. **Language:** form language per participant and scores by language (it / en), as a check that untranslated open answers don't hurt.
12. **Label leaks:** the V3 prompts flagged in Step 2.
13. **Time and cost**, projected for 40 participants × 6 places × 4 versions × 2 runs.
14. **Reasoning samples:** per participant, one reasoning per version for the same place, verbatim.

No conclusions about the hypotheses.

## Pilot participants in the final analysis

Decided on 2026-10-15, before any external pilot answers came in:

- The researcher's own test responses are **not** data. They may stay in the form during the pilot, as a pipeline test alongside the pilot participants, and are deleted from the form (and any linked sheet) before the form is opened to the main sample. They never enter the final analysis.
- The external pilot uses the v2 form as it will be launched; its first 2–3 respondents are the pilot.
- If the pilot leads to **no change** in the form (questions, options, photos, consent), the pilot participants' human answers are **included** in the final analysis like any other response.
- If the pilot leads to **any change** in the form, the changed form is a new version (v3) and the pilot answers are used for the pilot only, not in the final analysis.
- Fixes to the pipeline only (format, instructions, limits) do not count as a change to the form. Persona answers are regenerated for everyone in the final run, so pilot persona runs are never part of the final analysis.

## Done when

1. `smoke-test-v1` is tagged and the old run is untouched.
2. `--dry-run` shows the mapping, one prompt per version, call count and cost, and I've approved them.
3. All calls ran (or stopped at the cap) and are in `calls.jsonl`.
4. `report.md` and the charts are written.
5. No participant data, API key or raw output is committed.

Start with Step 0, then Step 1, and stop after printing the column mapping.
