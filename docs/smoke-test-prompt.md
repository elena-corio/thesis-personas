# Prompt for Claude Code: SenCity persona smoke test

Build a small Python pipeline that turns survey answers into AI personas, asks a vision-language model to answer Part B of my survey as each persona, and compares the answers with the real person's. This first run is a **smoke test**: it checks that the pipeline works and that the model's answers are usable. It is **not** model selection and **not** analysis, so never tune prompts to make the results look better; fix only what breaks (format, instructions, limits).

Ask me before adding any dependency not listed here, and before any step that costs money beyond the cap below.

## Context

Master's thesis (IAAC, urban planning): *Can AI personas perceive a public space the way autistic and sensory-sensitive people do, and does lived experience reduce bias better than demographic labels?*

- Participants fill in a survey on Google Forms, in one of two separate forms: Italian or English (same questions, same order). **Part A** is a profile (demographics, health and neurodivergence, senses). **Part B** shows 4 photos of public places in Vienna and asks the same questions for each.
- A persona is built from a participant's Part A, in four versions, and answers **Part B only**, looking at the same photos:
  - **V0** no persona (the model answers as "a person");
  - **V1** labels: Q1–Q5 and Q7–Q11;
  - **V2** grounded: V1 plus Q12–Q22;
  - **V3** lived experience only: the V0 line plus Q12–Q22, with no demographics, health conditions or neurodivergence labels. Added after the first smoke-test run (design change, not tuning); note that Q22 is free text and may itself mention a diagnosis.

  The four versions form a 2 × 2 design: labels (Q1–Q5, Q7–Q11) and sensory info (lived experience in public spaces, Q12–Q22) are each either given or not.

  |               | No sensory info | Sensory info |
  |---------------|-----------------|--------------|
  | **No labels** | V0              | V3           |
  | **Labels**    | V1              | V2           |
- Q6 (familiarity with Vienna) and B1 (familiarity with each place) are **never** given to a persona; they are for analysis only.
- Personas are prompted **in English**. Closed answers (Italian or English) are mapped to option codes, and rendered in English, through `config/codebook.yaml` (I provide it: question ids, option codes, Italian and English labels, persona version, limits).
- Open answers (Q22, "Other" texts) are **never translated**: they go to the persona verbatim, as the person's own words, in the language they were written in. Models read Italian well, and a translation step would add its own errors. The language of each participant (`it` or `en`, from the form they filled in) is recorded for the analysis.

## Repository setup

Create this structure (I will put the data files in place):

```
sencity-personas/
  config/codebook.yaml        # provided by me
  config/run.yaml             # model, temperature, runs, cost cap
  data/raw/responses_it.csv   # Google Forms export, Italian form (gitignored)
  data/raw/responses_en.csv   # Google Forms export, English form (gitignored; optional until the first English answers)
  data/photos/place1.jpg … place4.jpg   # numbered in the order the places appear in the survey (place1 = Part B place 1)
  prompts/system.txt
  prompts/persona_v0.txt, persona_v1.txt, persona_v2.txt
  prompts/task_part_b.txt
  src/parse_responses.py      # CSV → data/clean/participants.json (canonical codes)
  src/build_personas.py       # participants.json → persona texts per version
  src/run_personas.py         # API calls, validation, logging
  src/score.py                # persona vs participant
  src/report.py               # runs/<run_id>/report.md + small PNG charts
  runs/                       # one folder per run (gitignored except report.md)
  .env                        # OPENROUTER_API_KEY (gitignored)
  requirements.txt            # pandas, pyyaml, openai, python-dotenv, jsonschema, scipy, matplotlib, pillow
  README.md
```

`git init`, a Python virtual environment, and a `.gitignore` that excludes `.env`, `data/raw/`, `data/clean/` and raw run outputs. The participants' data includes health information: it never leaves this machine except in the API calls.

## API and model

Use **OpenRouter** (OpenAI-compatible API, `base_url="https://openrouter.ai/api/v1"`) so the model is one line in `config/run.yaml` and I can swap it later for model selection without changing code. For this smoke test use **one** small, fast, commercial multimodal model that supports image input and structured (JSON) output, for example a current "Flash" or "mini" model. Check the exact model id on openrouter.ai/models and **record it in every log line**. Do not use free model endpoints: their providers may keep or train on prompts, and this data includes health information. If OpenRouter offers a zero-data-retention setting, turn it on.

`config/run.yaml`:
```yaml
model: "<openrouter model id>"
temperature: 0.7        # fixed and recorded
runs_per_condition: 3
max_calls: 120          # hard stop
max_cost_usd: 2.00      # hard stop
```

Before the first real call, run `--dry-run`: build every prompt, print one full example per version, count the calls and estimate the cost from token counts. Show me and wait.

## Step 1: parse the responses

- Read `data/raw/responses_it.csv` and, if present, `data/raw/responses_en.csv`; the file sets the participant's language. Google Forms headers are the question texts, **without numbers**; in Part B the same questions repeat once per place. Map columns to ids (`Q1`…`Q22`, `P1_B1`…`P4_B8`) by section order: skip the meta columns (`Timestamp`, `Consenso`/consent), group consecutive columns with the same question text (grid questions have one column per row, with the row label in `[…]`), and assign the groups in order. Check grid row labels against the codebook in the form's language. **Print the mapping table and stop so I can confirm it.**
- Keep only participants who ticked **both** consent statements (participation, and use of health information).
- Map every answer to its option code through the codebook, using the labels in the form's language. Multi-select cells are separated by `;` in the export (some labels contain commas, so match whole labels). Any answer that doesn't match an option: keep it as `other` with its text, and list it for me. Answers I decide to map to an existing option (e.g. Q3 "Italia" → `another_european_country`) are listed in the parser and printed at every run.
- The Q16 row "Controllo: per questa riga seleziona 2" is an attention check: record pass/fail, then drop it.
- Keep free text (Q22, "Other" texts) verbatim; do not translate it. Record each participant's language (`it`/`en`).
- Output `data/clean/participants.json`, one object per participant with a stable pseudonymous id (`P01`, `P02`, numbered across both forms in submission order), never names, emails or timestamps.

## Step 2: build the personas

Render each version as a short second-person profile in plain English, from the codebook's English labels; open answers are inserted verbatim, in their original language. Draft templates (keep the wording; only fix grammar):

`prompts/system.txt`
```
You will answer a short survey about public spaces as a specific person. Answer as that person would, from their perspective, not as an assistant and not as an average person. If something is not stated about them, do not invent it. Answer only with JSON that follows the schema.
```

`prompts/persona_v0.txt`
```
You are a person taking part in a survey about public spaces in Vienna.
```

`prompts/persona_v1.txt`
```
You are a person taking part in a survey about public spaces in Vienna. About you:
- Age: {Q1}. Gender: {Q2}. You live in {Q3}, in {Q4}. Education: {Q5}.
- Health conditions that affect how you experience public spaces: {Q7}.
- Neurodivergence: {Q8}{; terms that describe you: Q9}{; this is based on: Q10}{; support you need in daily life: Q11}.
```

`prompts/persona_v2.txt` = V1, then:
```
How you experience public spaces, in your own answers:
- Meltdowns or shutdowns in public spaces: {Q12}. What helps you then: {Q13}.
- How often public spaces overwhelm you (1 = never, 5 = almost always): {Q14}.
- How much each situation makes it hard for you to be in a public space (1–5): {Q15 rows}.
- How much each sense influences your experience, positively or negatively (1–5): {Q16 rows}.
- Elements that bother you: {Q17}. Elements that make you feel good: {Q18}.
- What makes all sensations more intense for you: {Q19}.
- What you usually do when a place overwhelms you: {Q20}. Places you avoid: {Q21}.
- In your own words: "{Q22}"
```
Omit a line when its question was not asked (branching) or the answer is "Prefer not to say"; write "none" when the answer is None.

Exception: the neurodivergence details and the "what helps" question can be blank because not every participant is asked them (Q9 and Q10 only if Q8 = Yes or Not sure; Q13 only "if it happens to you") or because they are left empty. When blank, write them out instead of dropping them: Q9 "none", Q10 "not applicable", Q11 "none", Q13 "none". "Prefer not to say" is still omitted: the person chose not to share, which is different from not being asked.

**V3 (lived experience only)** has no template of its own: `persona_v0.txt` followed by `persona_v2.txt`, rendered with the same rules as V2. It must contain nothing from V1 (no age, gender, residence, education, health conditions, neurodivergence or support needs). The line between V1 and V3 is **labels vs experience**, not health vs non-health: Q12–Q22 describe what happens to the person in public spaces, so they stay in V2/V3 even where they touch on health (meltdowns or shutdowns in Q12, what helps in Q13). V3 can therefore still hint at a label, indirectly (the model may infer a diagnosis from the experiences) or directly (the Q13 option "People who understand neurodivergence and know how to help", or a diagnosis named in Q22). Open answers stay verbatim, so these words are not removed; describe V3 in the thesis as "no labels, only experience", not "no health information".

`prompts/task_part_b.txt`
```
Look at the photo and imagine spending 20 minutes here on a weekday afternoon.
Answer these questions about this place:
B2. How comfortable would you feel spending 20 minutes here? (1 = not at all … 5 = very comfortable)
B3. How much would each of these situations be a problem for you here? (1 = not at all … 5 = very much) {B3 rows}
B4. Which of these senses would most influence your experience here, positively or negatively? (up to 3) {options}
B5. Which elements in the photo would bother you here? (up to 3) {options}
B6. Which elements in the photo would make you feel good here? (up to 3) {options}
B7. Which changes would make this place better for you? (up to 3) {options}
Then, in "reasoning", explain in 2–4 sentences why, from your perspective.
```
Option lists come from the codebook in English, as `code: label`; the model answers with codes. Shuffle the option order in every run, except that "None…", "No change needed" and "Other" always stay last. Save the order used.

## Step 3: run

One call per **participant × version × place × run** (2 × 4 × 4 × 3 = 96 calls for this test: 72 for V0–V2, plus 24 for V3 added to the same run with `--resume`), each a fresh conversation: system prompt, persona text, task text and the photo (resized to max 1024 px on the long side, JPEG). Request JSON with this schema (enforce it with the API's structured output if the model supports it, and validate with `jsonschema` anyway):

```json
{"B2_comfort": 3,
 "B3_situations": {"sensory_overload": 4, "crowded_chaotic_narrow": 5, "uncertainty": 2, "no_place_to_retreat": 4},
 "B4_senses": ["hearing", "sight"],
 "B5_elements_bother": ["many_people", "signs_and_ads"],
 "B6_elements_feel_good": ["trees_and_greenery"],
 "B7_changes": ["quiet_corners_or_rest", "less_noise"],
 "reasoning": "..."}
```

Rules: integers 1–5; lists of 1–3 codes from that question's options; an exclusive code ("none…", "no_change_needed") only alone. An invalid answer is retried once with the validation error appended; if it fails again, log it as invalid and move on (don't silently fix it).

Log every call to `runs/<run_id>/calls.jsonl`: run id, timestamp start and end, seconds, model id as returned by the API, temperature, participant, version, place, run number, option order, input and output tokens, cost, raw response, parsed answer, validity, retries. Stop when `max_calls` or `max_cost_usd` is reached.

API errors are not answers and do not use the format retry: a rate limit (429) is waited out and tried again; out of credits (402) stops the run. Calls left with an API error are done again with `--resume runs/<run_id>`, which keeps the run id, the shuffle seeds and every call already answered. Only requests that reach the model count toward `max_calls`.

## Step 4: score and report

Compare each persona answer with **its own participant** (never with other people):
- B2: absolute error; plus, per participant and version, the **Spearman correlation of the 4 places' B2** (does the persona get the order of places right, regardless of the person's personal use of the scale).
- B3: mean absolute error over the 4 situations.
- B4–B7: Jaccard overlap between the persona's and the person's selections.
- Average the 3 runs for the scores, and also report how much the 3 runs differ (run-to-run spread).

`runs/<run_id>/report.md`, short, with:
1. **Format:** valid answers out of total, retries, invalid ones and why.
2. **Scale use:** distribution of B2 and B3 values for personas vs. humans (a small histogram PNG): do personas use the whole 1–5 range or stay in the middle?
3. **Scores** by version (V0, V1, V2) and participant, in one table.
4. **Stability:** run-to-run spread per version, and whether shuffled option order changes the choices.
5. **Time and cost:** seconds per call (median and max), cost per call, and the projected time and cost for 40 participants × 4 places × 3 runs × 3 versions.
6. **Reasoning samples:** for each participant, one reasoning per version for the same place, verbatim, so I can read whether the persona refers to the photo and to the person's profile or answers generically.

7. **Do the versions really differ?** For each participant and place, compare the difference **between** versions (every pair of V0–V3) with the spread **within** a version across its 3 runs. A difference between versions only counts if it is larger than the run-to-run spread; say for each comparison whether it is.
8. **Summary:** at the top of the report, 5–8 plain-language bullets: does the pipeline work, how reliable the format is, how the personas use the scale, whether the versions differ beyond run-to-run noise, time and cost, and anything to fix before the real run (format, instructions, limits only).

No conclusions about the hypotheses: with 2 people this is a feasibility check only. Mark the run as `pilot: true` in its folder.

## Done when

1. `--dry-run` shows the mapping, one prompt per version, the call count and the estimated cost, and I've approved them.
2. The 72 calls ran (or stopped at the cap), every call is in `calls.jsonl`.
3. `report.md` and the charts are written.
4. No participant data, API key or raw output is committed to git.

Start with the repository setup and Step 1, and stop after printing the column mapping.
