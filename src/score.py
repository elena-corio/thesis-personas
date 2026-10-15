"""Step 4a: score persona answers against their own participant.

    python src/score.py runs/<run_id> [runs/<run_id2> ...]

Several run folders can be combined (e.g. a later run with an extra version); the
scores are written to the first folder as scores.json. Every persona answer is
compared only with its own participant:

    B2      absolute error; per participant x version, Spearman correlation of the
            4 places' B2 (does the persona get the order of places right)
    B3      mean absolute error over the 4 situations
    B4-B7   Jaccard overlap between the persona's and the person's selections

Per participant x version x place, the scores are averaged over runs, and the
run-to-run spread is kept (standard deviation over runs; for B2/B3 also the range).

Also computed:
    bias        signed error, persona - person (B2: negative = less comfortable than the
                person; B3: positive = sees more problems than the person)
    B3_vs_Q15   how close the persona's B3 is to the person's general Q15 ratings
                (same 4 situations): near 0 = the persona copies the profile
    baselines   scores of answering from the profile alone, with no model and no photo:
                B3 = own Q15, B4 = top-rated senses in Q16, B2 = always 3
    diversity   how much answers differ between participants, personas vs humans
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
PARTICIPANTS = ROOT / "data" / "clean" / "participants.json"

LISTS = {"B4": "B4_senses", "B5": "B5_elements_bother", "B6": "B6_elements_feel_good", "B7": "B7_changes"}


def load_calls(run_dirs):
    """The latest record of every call that got an answer (API errors are not answers)."""
    from run_personas import latest_records
    calls = []
    for d in run_dirs:
        calls += [r for r in latest_records(d / "calls.jsonl").values() if r["status"] != "api_error"]
    return calls


def jaccard(a, b):
    a, b = set(a), set(b)
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def human_answer(part_b_place):
    """The participant's own Part B answer for one place, in the persona's answer shape."""
    return {
        "B2_comfort": part_b_place["B2"],
        "B3_situations": part_b_place["B3"],
        **{key: part_b_place[bid] for bid, key in LISTS.items()},
    }


def score_answer(persona, human, q15):
    """Accuracy (abs. error, Jaccard), bias (signed error: persona - person; negative = persona
    rates the place worse / less comfortable) and how closely B3 echoes the profile's Q15."""
    s = {"B2_abs_error": abs(persona["B2_comfort"] - human["B2_comfort"]),
         "B2_signed": persona["B2_comfort"] - human["B2_comfort"]}
    rows = [r for r, v in human["B3_situations"].items() if v is not None]
    s["B3_mae"] = statistics.mean(abs(persona["B3_situations"][r] - human["B3_situations"][r]) for r in rows)
    # B3 is "how much a problem": positive = persona sees more problems than the person
    s["B3_signed"] = statistics.mean(persona["B3_situations"][r] - human["B3_situations"][r] for r in rows)
    q15_rows = [r for r in rows if q15.get(r) is not None]
    s["B3_vs_Q15"] = (statistics.mean(abs(persona["B3_situations"][r] - q15[r]) for r in q15_rows)
                      if q15_rows else None)
    for bid, key in LISTS.items():
        s[f"{bid}_jaccard"] = jaccard(persona[key], human[key])
    return s


METRICS = ["B2_abs_error", "B2_signed", "B3_mae", "B3_signed", "B3_vs_Q15"] + [f"{b}_jaccard" for b in LISTS]


def spread(values):
    return statistics.pstdev(values) if len(values) > 1 else 0.0


# --- baselines without any model -----------------------------------------------

def top_senses(q16, k=3):
    """The senses the person rated highest in Q16 (up to k, ties in codebook order)."""
    rated = [(code, v) for code, v in q16.items() if v is not None]
    if not rated:
        return []
    rated.sort(key=lambda cv: -cv[1])  # stable: keeps codebook order within ties
    return [code for code, _ in rated[:k]]


def copy_baselines(humans):
    """Per participant x place: the score of answering from the profile alone, without looking
    at any place. B3 = the person's own Q15 (same 4 situations, asked in general); B4 = the
    senses they rated highest in Q16; B2 = always 3 (the middle of the scale)."""
    out = []
    for pid, p in humans.items():
        q15, q16 = p["part_a"]["Q15"], p["part_a"]["Q16"]
        for i in range(1, 5):
            h = human_answer(p["part_b"][f"P{i}"])
            rows = [r for r, v in h["B3_situations"].items() if v is not None and q15.get(r) is not None]
            out.append({
                "participant": pid, "place": f"P{i}",
                "B2_const3_abs_error": abs(3 - h["B2_comfort"]),
                "B3_copy_Q15_mae": statistics.mean(abs(q15[r] - h["B3_situations"][r]) for r in rows) if rows else None,
                "B4_copy_Q16_jaccard": jaccard(top_senses(q16), h["B4_senses"]),
            })
    return out


# --- diversity between participants ----------------------------------------------

def mean_pairwise_jaccard(selections):
    pairs = [jaccard(selections[i], selections[j]) for i in range(len(selections))
             for j in range(i + 1, len(selections))]
    return statistics.mean(pairs) if pairs else None


def diversity(humans, valid):
    """How much answers differ between participants, per place, averaged over places.
    B2/B3: SD across participants (personas: of each participant's mean over runs).
    B4-B7: mean Jaccard between participants (lower = more different); personas compared run by run.
    Humans are the reference: a version close to the humans' values differentiates people as
    much as they really differ; near 0 SD / near 1 Jaccard = everyone gets the same answer."""
    pids = sorted(humans)
    out = {}

    def summarise(b2_by_place, b3_by_place, lists_by_place):
        res = {"B2_sd_between": statistics.mean(spread(v) for v in b2_by_place.values()),
               "B3_sd_between": statistics.mean(spread(v) for v in b3_by_place.values())}
        for bid in LISTS:
            vals = [mean_pairwise_jaccard(sel) for sel in lists_by_place[bid]]
            vals = [v for v in vals if v is not None]
            res[f"{bid}_jaccard_between"] = statistics.mean(vals) if vals else None
        return res

    places = [f"P{i}" for i in range(1, 5)]
    # humans
    b2 = {pl: [humans[p]["part_b"][pl]["B2"] for p in pids] for pl in places}
    b3 = {(pl, r): [humans[p]["part_b"][pl]["B3"][r] for p in pids if humans[p]["part_b"][pl]["B3"].get(r) is not None]
          for pl in places for r in humans[pids[0]]["part_b"][pl]["B3"]}
    lists = {bid: [[humans[p]["part_b"][pl][bid] for p in pids] for pl in places] for bid in LISTS}
    out["Humans"] = summarise(b2, b3, lists)

    # personas, per version
    for version in sorted({c["version"] for c in valid}):
        vc = [c for c in valid if c["version"] == version]
        by = defaultdict(list)
        for c in vc:
            by[(c["participant"], c["place"])].append(c["parsed"])
        if any((p, pl) not in by for p in pids for pl in places):
            continue  # a participant without answers for this version: diversity not comparable
        b2 = {pl: [statistics.mean(a["B2_comfort"] for a in by[(p, pl)]) for p in pids] for pl in places}
        rows = list(vc[0]["parsed"]["B3_situations"])
        b3 = {(pl, r): [statistics.mean(a["B3_situations"][r] for a in by[(p, pl)]) for p in pids]
              for pl in places for r in rows}
        runs = sorted({c["run"] for c in vc})
        run_of = {(c["participant"], c["place"], c["run"]): c["parsed"] for c in vc}
        lists = {bid: [[run_of[(p, pl, rn)][key] for p in pids]
                       for pl in places for rn in runs if all((p, pl, rn) in run_of for p in pids)]
                 for bid, key in LISTS.items()}
        out[version] = summarise(b2, b3, lists)
    return out


def main():
    run_dirs = [Path(a) for a in sys.argv[1:]]
    if not run_dirs:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    with open(PARTICIPANTS, encoding="utf-8") as f:
        humans = {p["id"]: p for p in json.load(f)}

    calls = load_calls(run_dirs)
    valid = [c for c in calls if c["valid"]]

    # per call
    per_call = []
    for c in valid:
        human = human_answer(humans[c["participant"]]["part_b"][c["place"]])
        q15 = humans[c["participant"]]["part_a"]["Q15"]
        per_call.append({k: c[k] for k in ("run_id", "participant", "version", "place", "run")}
                        | score_answer(c["parsed"], human, q15))

    # per participant x version x place: mean over runs and run-to-run spread
    groups = defaultdict(list)
    for s in per_call:
        groups[(s["participant"], s["version"], s["place"])].append(s)
    metrics = METRICS
    cells = []
    for (pid, version, place), items in sorted(groups.items()):
        call_of = {(c["participant"], c["version"], c["place"], c["run"], c["run_id"]): c for c in valid}
        answers = [call_of[(pid, version, place, s["run"], s["run_id"])]["parsed"] for s in items]
        b2 = [a["B2_comfort"] for a in answers]
        cell = {"participant": pid, "version": version, "place": place, "n_runs": len(items),
                "B2_values": b2, "B2_mean": statistics.mean(b2), "B2_sd": spread(b2),
                "B2_range": max(b2) - min(b2)}
        b3_means = [statistics.mean(a["B3_situations"].values()) for a in answers]
        cell.update({"B3_mean": statistics.mean(b3_means), "B3_sd": spread(b3_means)})
        for m in metrics:
            vals = [s[m] for s in items if s[m] is not None]
            cell[m] = statistics.mean(vals) if vals else None
            cell[f"{m}_sd"] = spread(vals)
        # how much the runs agree on the list choices: mean pairwise Jaccard between runs
        for bid, key in LISTS.items():
            pairs = [jaccard(answers[i][key], answers[j][key])
                     for i in range(len(answers)) for j in range(i + 1, len(answers))]
            cell[f"{bid}_run_agreement"] = statistics.mean(pairs) if pairs else None
        cells.append(cell)

    # Spearman of the 4 places' B2, per participant x version (mean persona B2 over runs)
    spearman = []
    for pid in sorted({c["participant"] for c in cells}):
        human_b2 = [humans[pid]["part_b"][f"P{i}"]["B2"] for i in range(1, 5)]
        for version in sorted({c["version"] for c in cells}):
            by_place = {c["place"]: c["B2_mean"] for c in cells if c["participant"] == pid and c["version"] == version}
            if len(by_place) < 4:
                spearman.append({"participant": pid, "version": version, "rho": None, "note": "fewer than 4 places"})
                continue
            persona_b2 = [by_place[f"P{i}"] for i in range(1, 5)]
            if len(set(human_b2)) == 1 or len(set(persona_b2)) == 1:
                rho, note = None, "constant B2 (no order to compare)"
            else:
                rho, note = float(spearmanr(human_b2, persona_b2).statistic), ""
            spearman.append({"participant": pid, "version": version, "rho": rho, "note": note,
                             "human_B2": human_b2, "persona_B2_mean": persona_b2})

    out = {"run_dirs": [str(d) for d in run_dirs], "n_calls": len(calls), "n_valid": len(valid),
           "per_call": per_call, "cells": cells, "spearman_B2": spearman,
           "baselines": copy_baselines(humans), "diversity": diversity(humans, valid)}
    with open(run_dirs[0] / "scores.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Scored {len(valid)} valid of {len(calls)} calls -> {run_dirs[0] / 'scores.json'}")


if __name__ == "__main__":
    main()
