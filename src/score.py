"""Step 4a: score persona answers against their own participant (survey v2).

    python src/score.py runs/<run_id> [runs/<run_id2> ...]

The study comes from the run's run.yaml. Several run folders of the same study can be
combined; scores.json goes to the first folder. Every persona answer is compared only
with its own participant. Outcomes, fixed before the data:

    primary     B3: mean absolute error over the 4 situations (also per factor / row)
    secondary   B2: absolute error; Spearman correlation of the places within each person
    B4-B6       Jaccard, precision (share of the persona's picks the person also made) and
                recall (share of the person's picks the persona found); per sense / factor
    bias        signed error, persona - person (B2 negative = less comfortable than the
                person; B3 positive = sees more problems than the person)
    copy check  distance of the persona's B3 to the person's own Q14 (near 0 = copies the profile)

Baselines, scored exactly like a persona:
    average human  leave-one-out: the mean (B2, B3) or majority picks (B4-B6) of the
                   *other* participants for the same place (needs >= 2 participants)
    chance         random valid answers (expected value, simulated with a fixed seed)
    always 3       B2 = 3
    copy Part A    B3 = Q14 rows, B4 = Q15 codes, B5 = Q16 codes (same sense codes)
"""

import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_personas import latest_records  # noqa: E402
from study import Study  # noqa: E402

LISTS = ("B4", "B5", "B6")
CHANCE_DRAWS = 2000


def load_calls(run_dirs):
    """The latest record of every call that got an answer (API errors are not answers)."""
    calls = []
    for d in run_dirs:
        calls += [r for r in latest_records(d / "calls.jsonl").values() if r["status"] != "api_error"]
    return calls


def study_of(run_dirs):
    names = {yaml.safe_load((d / "run.yaml").read_text(encoding="utf-8")).get("study") for d in run_dirs}
    if len(names) != 1 or None in names:
        sys.exit(f"Run folders must belong to one study, found {names}")
    return Study(names.pop())


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def spread(values):
    return statistics.pstdev(values) if len(values) > 1 else 0.0


# --- comparing one answer with one person ------------------------------------------

def jaccard(a, b):
    a, b = set(a), set(b)
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def precision(pred, true):
    return len(set(pred) & set(true)) / len(set(pred)) if pred else None


def recall(pred, true):
    return len(set(pred) & set(true)) / len(set(true)) if true else None


def compare(answer, human, keys, q14=None):
    """answer/human: {"B2": int, "B3": {row: int}, "B4": [codes], ...} -> metrics."""
    s = {}
    if answer.get("B2") is not None and human["B2"] is not None:
        s["B2_abs_error"] = abs(answer["B2"] - human["B2"])
        s["B2_signed"] = answer["B2"] - human["B2"]
    rows = [r for r, v in human["B3"].items() if v is not None and answer["B3"].get(r) is not None]
    if rows:
        s["B3_mae"] = statistics.mean(abs(answer["B3"][r] - human["B3"][r]) for r in rows)
        s["B3_signed"] = statistics.mean(answer["B3"][r] - human["B3"][r] for r in rows)
        for r in rows:
            s[f"B3_err_{r}"] = abs(answer["B3"][r] - human["B3"][r])
    if q14 is not None:
        q_rows = [r for r in answer["B3"] if q14.get(r) is not None]
        s["B3_vs_Q14"] = statistics.mean(abs(answer["B3"][r] - q14[r]) for r in q_rows) if q_rows else None
    for bid in keys:
        if answer.get(bid) is None:
            continue
        s[f"{bid}_jaccard"] = jaccard(answer[bid], human[bid])
        s[f"{bid}_precision"] = precision(answer[bid], human[bid])
        s[f"{bid}_recall"] = recall(answer[bid], human[bid])
    return s


def human_answer(part_b_place):
    return {"B2": part_b_place["B2"], "B3": part_b_place["B3"], **{b: part_b_place[b] for b in LISTS}}


def persona_answer(parsed, codebook):
    keys = {b: codebook["part_b"][b]["json_key"] for b in LISTS}
    return {"B2": parsed["B2_comfort"], "B3": parsed["B3_situations"], **{b: parsed[k] for b, k in keys.items()}}


# --- baselines --------------------------------------------------------------------

def random_answer(codebook, rng):
    b3_rows = [r["code"] for r in codebook["part_b"]["B3"]["rows"]]
    ans = {"B2": rng.randint(1, 5), "B3": {r: rng.randint(1, 5) for r in b3_rows}}
    for bid in LISTS:
        opts = [o for o in codebook["part_b"][bid]["options"] if o["code"] != "other"]
        exclusive = [o["code"] for o in opts if o.get("exclusive")]
        regular = [o["code"] for o in opts if not o.get("exclusive")]
        # a valid answer: one exclusive code alone, or 1-3 regular codes, each shape equally likely
        n_shapes = len(exclusive) + 3
        shape = rng.randrange(n_shapes)
        ans[bid] = [exclusive[shape]] if shape < len(exclusive) else rng.sample(regular, shape - len(exclusive) + 1)
    return ans


def baselines(humans, codebook, places):
    """Per participant x place, the metrics of each baseline."""
    pids = sorted(humans)
    rng = random.Random(0)
    out = []
    for pid in pids:
        p = humans[pid]
        q14 = p["part_a"]["Q14"]
        for pl in places:
            h = human_answer(p["part_b"][pl])
            row = {"participant": pid, "place": pl}
            # always 3
            row["const3"] = {"B2_abs_error": abs(3 - h["B2"]), "B2_signed": 3 - h["B2"]}
            # copy Part A
            copy = {"B2": None, "B3": dict(q14), "B4": p["part_a"]["Q15"], "B5": p["part_a"]["Q16"]}
            copy["B4"] = [c for c in copy["B4"] if c != "other"]
            copy["B5"] = [c for c in copy["B5"] if c != "other"]
            row["copy"] = compare(copy, h, ("B4", "B5"))
            # average human, leave-one-out
            others = [humans[o]["part_b"][pl] for o in pids if o != pid]
            if others:
                avg = {"B2": statistics.mean(o["B2"] for o in others),
                       "B3": {r: mean([o["B3"].get(r) for o in others]) for r in h["B3"]}}
                for bid in LISTS:
                    counts = Counter(c for o in others for c in o[bid])
                    majority = [c for c, n in counts.items() if n >= len(others) / 2]
                    avg[bid] = majority or [c for c, _ in counts.most_common(1)]
                row["avg_human"] = compare(avg, h, LISTS)
            # chance: expected value over random valid answers
            draws = [compare(random_answer(codebook, rng), h, LISTS) for _ in range(CHANCE_DRAWS)]
            row["chance"] = {k: mean([d.get(k) for d in draws]) for k in draws[0]}
            out.append(row)
    return out


# --- per sense / per code ----------------------------------------------------------

def per_code(valid, humans, codebook):
    """Per version x question x code: how often the persona and the person picked it."""
    tally = defaultdict(lambda: Counter())
    for c in valid:
        ans = persona_answer(c["parsed"], codebook)
        h = humans[c["participant"]]["part_b"][c["place"]]
        for bid in LISTS:
            pred, true = set(ans[bid]), set(h[bid])
            for code in pred | true:
                key = (c["version"], bid, code)
                tally[key]["tp"] += code in pred and code in true
                tally[key]["fp"] += code in pred and code not in true
                tally[key]["fn"] += code in true and code not in pred
    out = []
    for (version, bid, code), t in sorted(tally.items()):
        opt = next((o for o in codebook["part_b"][bid]["options"] if o["code"] == code), {})
        out.append({"version": version, "question": bid, "code": code,
                    "sense": opt.get("sense"), "factor": opt.get("factor"), **t,
                    "precision": t["tp"] / (t["tp"] + t["fp"]) if t["tp"] + t["fp"] else None,
                    "recall": t["tp"] / (t["tp"] + t["fn"]) if t["tp"] + t["fn"] else None})
    return out


# --- diversity between participants ----------------------------------------------

def mean_pairwise_jaccard(selections):
    pairs = [jaccard(selections[i], selections[j]) for i in range(len(selections))
             for j in range(i + 1, len(selections))]
    return statistics.mean(pairs) if pairs else None


def diversity(humans, valid, codebook, places):
    """Spread between participants per place, averaged: SD for B2/B3, Jaccard for B4-B6 (lower = more
    different). Needs >= 2 participants."""
    pids = sorted(humans)
    if len(pids) < 2:
        return {}

    def summarise(b2, b3, lists):
        res = {"B2_sd_between": mean([spread(v) for v in b2.values()]),
               "B3_sd_between": mean([spread(v) for v in b3.values()])}
        for bid in LISTS:
            res[f"{bid}_jaccard_between"] = mean([mean_pairwise_jaccard(sel) for sel in lists[bid]])
        return res

    rows = [r["code"] for r in codebook["part_b"]["B3"]["rows"]]
    out = {"Humans": summarise(
        {pl: [humans[p]["part_b"][pl]["B2"] for p in pids] for pl in places},
        {(pl, r): [v for p in pids if (v := humans[p]["part_b"][pl]["B3"].get(r)) is not None]
         for pl in places for r in rows},
        {bid: [[humans[p]["part_b"][pl][bid] for p in pids] for pl in places] for bid in LISTS})}
    for version in sorted({c["version"] for c in valid}):
        by = defaultdict(list)
        for c in valid:
            if c["version"] == version:
                by[(c["participant"], c["place"], c["run"])] = persona_answer(c["parsed"], codebook)
        runs = sorted({k[2] for k in by})
        if any(not any((p, pl, rn) in by for rn in runs) for p in pids for pl in places):
            continue

        def runs_of(p, pl):
            return [by[(p, pl, rn)] for rn in runs if (p, pl, rn) in by]
        out[version] = summarise(
            {pl: [statistics.mean(a["B2"] for a in runs_of(p, pl)) for p in pids] for pl in places},
            {(pl, r): [statistics.mean(a["B3"][r] for a in runs_of(p, pl)) for p in pids]
             for pl in places for r in rows},
            {bid: [[by[(p, pl, rn)][bid] for p in pids] for pl in places for rn in runs
                   if all((p, pl, rn) in by for p in pids)] for bid in LISTS})
    return out


# --- main ----------------------------------------------------------------------

def main():
    run_dirs = [Path(a) for a in sys.argv[1:]]
    if not run_dirs:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    study = study_of(run_dirs)
    codebook = study.codebook()
    places = [f"P{i}" for i in range(1, codebook["places"] + 1)]
    with open(study.participants, encoding="utf-8") as f:
        humans = {p["id"]: p for p in json.load(f)}

    calls = load_calls(run_dirs)
    valid = [c for c in calls if c["valid"]]

    per_call = []
    for c in valid:
        h = human_answer(humans[c["participant"]]["part_b"][c["place"]])
        q14 = humans[c["participant"]]["part_a"]["Q14"]
        per_call.append({k: c[k] for k in ("run_id", "participant", "version", "place", "run")}
                        | compare(persona_answer(c["parsed"], codebook), h, LISTS, q14))

    # per participant x version x place: mean over runs, and how much the runs differ
    groups = defaultdict(list)
    for s, c in zip(per_call, valid):
        groups[(s["participant"], s["version"], s["place"])].append((s, persona_answer(c["parsed"], codebook)))
    cells = []
    for (pid, version, place), items in sorted(groups.items()):
        scores = [s for s, _ in items]
        answers = [a for _, a in items]
        b2 = [a["B2"] for a in answers]
        b3_means = [statistics.mean(a["B3"].values()) for a in answers]
        cell = {"participant": pid, "version": version, "place": place, "n_runs": len(items),
                "B2_values": b2, "B2_mean": statistics.mean(b2), "B2_range": max(b2) - min(b2),
                "B3_mean": statistics.mean(b3_means), "B3_range": max(b3_means) - min(b3_means)}
        for m in {k for s in scores for k in s if k not in ("run_id", "participant", "version", "place", "run")}:
            vals = [s.get(m) for s in scores]
            cell[m] = mean(vals)
        for bid in LISTS:
            pairs = [jaccard(answers[i][bid], answers[j][bid])
                     for i in range(len(answers)) for j in range(i + 1, len(answers))]
            cell[f"{bid}_run_agreement"] = mean(pairs)
        cells.append(cell)

    # Spearman of the places' B2 within each person (persona: mean over runs)
    spearman = []
    for pid in sorted(humans):
        human_b2 = [humans[pid]["part_b"][pl]["B2"] for pl in places]
        for version in sorted({c["version"] for c in cells}):
            by_place = {c["place"]: c["B2_mean"] for c in cells if c["participant"] == pid and c["version"] == version}
            entry = {"participant": pid, "version": version, "human_B2": human_b2,
                     "persona_B2_mean": [by_place.get(pl) for pl in places], "rho": None, "note": ""}
            if len(by_place) < len(places):
                entry["note"] = "missing places"
            elif len(set(human_b2)) == 1 or len(set(by_place.values())) == 1:
                entry["note"] = "constant B2 (no order to compare)"
            else:
                entry["rho"] = float(spearmanr(human_b2, [by_place[pl] for pl in places]).statistic)
            spearman.append(entry)

    out = {"study": study.name, "run_dirs": [str(d) for d in run_dirs], "n_calls": len(calls),
           "n_valid": len(valid), "places": places, "per_call": per_call, "cells": cells,
           "spearman_B2": spearman, "baselines": baselines(humans, codebook, places),
           "per_code": per_code(valid, humans, codebook),
           "diversity": diversity(humans, valid, codebook, places)}
    with open(run_dirs[0] / "scores.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Scored {len(valid)} valid of {len(calls)} answered calls ({study.name}) -> {run_dirs[0] / 'scores.json'}")


if __name__ == "__main__":
    main()
