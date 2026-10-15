"""Step 4b: write runs/<run_id>/report.md and charts from calls.jsonl and scores.json (survey v2).

    python src/report.py runs/<run_id> [runs/<run_id2> ...]

Run src/score.py with the same folders first. The report and charts go to the first
folder. Reasoning samples go to reasoning_samples.md, which stays out of git: persona
reasoning can repeat a participant's health information.
"""

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import yaml  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_personas import latest_records  # noqa: E402
from score import LISTS, mean, study_of  # noqa: E402

N_PARTICIPANTS_FULL, N_RUNS_FULL = 40, 2
FIXED_LAST = {"none", "no_change_needed", "other"}  # never shuffled
VERSIONS_ALL = ("V0", "V1", "V2", "V3")

# chart colours: humans neutral, versions in fixed categorical order (validated palette, light mode)
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
HUMAN = "#8a8984"
VERSION_COLOURS = {"V0": "#2a78d6", "V1": "#eb6834", "V2": "#1baf7a", "V3": "#eda100"}

# the 2 x 2 design: (without, with) pairs for each factor
EFFECTS = {
    "Lived experience": [("V0", "V3", "without labels"), ("V1", "V2", "with labels")],
    "Labels": [("V0", "V1", "without lived experience"), ("V3", "V2", "with lived experience")],
}


def fmt(x, nd=2):
    return "–" if x is None else f"{x:.{nd}f}"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def answered(call):
    return [a for a in call["attempts"] if "raw_response" in a]


def short(text, n=140):
    return text if len(text) <= n else text[: n - 1] + "…"


def load(run_dirs):
    calls, api_errors = [], 0
    for d in run_dirs:
        latest = latest_records(d / "calls.jsonl").values()
        calls += [r for r in latest if r["status"] != "api_error"]
        api_errors += sum(r["status"] == "api_error" for r in latest)
    with open(run_dirs[0] / "scores.json", encoding="utf-8") as f:
        scores = json.load(f)
    configs = [yaml.safe_load((d / "run.yaml").read_text(encoding="utf-8")) for d in run_dirs]
    study = study_of(run_dirs)
    with open(study.participants, encoding="utf-8") as f:
        humans = {p["id"]: p for p in json.load(f)}
    leaks_path = study.clean / "label_leaks.json"
    leaks = json.loads(leaks_path.read_text(encoding="utf-8")) if leaks_path.exists() else {}
    return calls, api_errors, scores, configs, study, humans, leaks


def by_version(cells, key, version, pid=None):
    return mean([c.get(key) for c in cells if c["version"] == version and (pid is None or c["participant"] == pid)])


def baseline_mean(scores, name, key):
    return mean([b.get(name, {}).get(key) for b in scores["baselines"]])


# --- sections ----------------------------------------------------------------

def section_format(calls, api_errors, codebook):
    n = len(calls)
    valid = sum(c["valid"] for c in calls)
    retried = [c for c in calls if len(answered(c)) > 1]
    invalid = [c for c in calls if not c["valid"]]
    too_long = sum(1 for c in retried for e in answered(c)[0]["errors"] if "too long" in e)
    lines = [f"- Valid answers: **{valid} / {n}** answered calls ({100 * valid / n:.0f}%).",
             f"- Retried once for an invalid answer: {len(retried)} ({sum(c['valid'] for c in retried)} valid on retry). "
             f"**More than 3 items** (the smoke test's most common error, before \"Choose 1 to 3\"): {too_long}."]
    if api_errors:
        lines.append(f"- ⚠ Calls still without an answer (API error, not a format problem): {api_errors}.")
    api_attempts = Counter(short(a["error"], 90) for c in calls for a in c["attempts"] if "error" in a)
    if api_attempts:
        lines.append("- API errors before an answer came through (the call was done again):")
        lines += [f"  - {e} ({k}×)" for e, k in api_attempts.most_common()]
    first_errors = Counter(short(e) for c in retried for e in answered(c)[0]["errors"])
    if first_errors:
        lines.append("- First-attempt errors that triggered a retry:")
        lines += [f"  - {e} ({k}×)" for e, k in first_errors.most_common()]
    lines.append(f"- Invalid after retry: {len(invalid)}"
                 + "".join(f"\n  - {c['participant']} {c['version']} {c['place']} run {c['run']}: "
                           f"{'; '.join(c['errors'])}" for c in invalid))
    models = Counter(c["model"] for c in calls)
    providers = Counter(a.get("provider") for c in calls for a in answered(c))
    lines.append(f"- Model ids returned by the API: {dict(models)}; providers: {dict(providers)}.")
    # option order
    keys = {b: codebook["part_b"][b]["json_key"] for b in LISTS}
    pos = defaultdict(list)
    for c in calls:
        if not c["valid"]:
            continue
        for bid, key in keys.items():
            order = [code for code in c["option_order"][bid] if code not in FIXED_LAST]
            pos[bid] += [order.index(code) / (len(order) - 1) for code in c["parsed"][key] if code in order]
    lines.append("- Option order (shuffled each call): mean relative position of the chosen options, "
                 "0.50 = order has no effect, lower = first options favoured: "
                 + ", ".join(f"{b} {fmt(mean(v))}" for b, v in pos.items()) + ".")
    return "\n".join(lines)


def scale_distributions(calls, humans, versions, places):
    dist = {"B2": {}, "B3": {}}
    dist["B2"]["Humans"] = Counter(p["part_b"][pl]["B2"] for p in humans.values() for pl in places)
    dist["B3"]["Humans"] = Counter(v for p in humans.values() for pl in places
                                   for v in p["part_b"][pl]["B3"].values() if v is not None)
    for v in versions:
        answers = [c["parsed"] for c in calls if c["valid"] and c["version"] == v]
        dist["B2"][v] = Counter(a["B2_comfort"] for a in answers)
        dist["B3"][v] = Counter(x for a in answers for x in a["B3_situations"].values())
    return dist


def chart_scale(dist, versions, path):
    series = ["Humans"] + versions
    colours = [HUMAN] + [VERSION_COLOURS.get(v, INK_2) for v in versions]
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.4), sharey=True, facecolor=SURFACE)
    width = 0.8 / len(series)
    for ax, q in zip(axes, ("B2", "B3")):
        ax.set_facecolor(SURFACE)
        for k, (name, col) in enumerate(zip(series, colours)):
            counts = dist[q][name]
            total = sum(counts.values()) or 1
            xs = [x + (k - (len(series) - 1) / 2) * width for x in range(1, 6)]
            ax.bar(xs, [counts.get(x, 0) / total for x in range(1, 6)], width=width,
                   color=col, edgecolor=SURFACE, linewidth=1.5, label=name, zorder=2)
        ax.set_title({"B2": "B2 comfort", "B3": "B3 situations (all 4 rows)"}[q], loc="left", fontsize=10, color=INK)
        ax.set_xticks(range(1, 6))
        ax.tick_params(colors=INK_2, labelsize=9, length=0)
        ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_xlabel("answer (1–5)", fontsize=9, color=INK_2)
    axes[0].set_ylabel("share of answers", fontsize=9, color=INK_2)
    axes[0].yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    axes[1].legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def section_scale(dist, versions):
    rows = []
    for q in ("B2", "B3"):
        for name in ["Humans"] + versions:
            c = dist[q][name]
            total = sum(c.values())
            rows.append([q, name, total] + [c.get(x, 0) for x in range(1, 6)]
                        + [f"{100 * c.get(3, 0) / total:.0f}%" if total else "–",
                           f"{100 * (c.get(1, 0) + c.get(5, 0)) / total:.0f}%" if total else "–"])
    return ("![Distribution of B2 and B3 answers](scale_use.png)\n\n"
            + md_table(["Q", "Who", "n", "1", "2", "3", "4", "5", "share of 3", "share of 1 or 5"], rows))


SCORE_COLS = [("B3_mae", "B3 MAE ↓ (primary)"), ("B2_abs_error", "B2 abs. error ↓"),
              ("B4_jaccard", "B4 Jaccard ↑"), ("B5_jaccard", "B5 Jaccard ↑"), ("B6_jaccard", "B6 Jaccard ↑")]


def section_scores(scores, versions, codebook):
    cells, sp = scores["cells"], scores["spearman_B2"]
    rho = {(s["participant"], s["version"]): s for s in sp}
    rows = []
    for pid in sorted({c["participant"] for c in cells}):
        for v in versions:
            r = rho.get((pid, v), {})
            rows.append([pid, v] + [fmt(by_version(cells, k, v, pid)) for k, _ in SCORE_COLS]
                        + [fmt(r.get("rho")) + (f" ({r['note']})" if r.get("note") else "")])
    for v in versions:
        rows.append(["**all**", f"**{v}**"] + [f"**{fmt(by_version(cells, k, v))}**" for k, _ in SCORE_COLS]
                    + [fmt(mean([s["rho"] for s in sp if s["version"] == v]))])
    base = [("avg_human", "average human (leave-one-out)"), ("chance", "chance (random valid answers)"),
            ("copy", "copy Part A (Q14, Q15, Q16)"), ("const3", "B2 always 3")]
    for name, label in base:
        rows.append([f"*{label}*", ""] + [fmt(baseline_mean(scores, name, k)) for k, _ in SCORE_COLS] + [""])
    main = md_table(["Participant", "Version"] + [h for _, h in SCORE_COLS] + ["B2 Spearman ρ (places) ↑"], rows)

    # precision / recall per question, and per sense / factor
    pr_rows = [[v] + [f"{fmt(by_version(cells, f'{b}_precision', v))} / {fmt(by_version(cells, f'{b}_recall', v))}"
                      for b in LISTS] for v in versions]
    pr_rows.append(["*chance*"] + [f"{fmt(baseline_mean(scores, 'chance', f'{b}_precision'))} / "
                                   f"{fmt(baseline_mean(scores, 'chance', f'{b}_recall'))}" for b in LISTS])
    pr = md_table(["Version", "B4 precision / recall", "B5 precision / recall", "B6 precision / recall"], pr_rows)

    factor_rows = []
    for r in codebook["part_b"]["B3"]["rows"]:
        factor_rows.append([f"{r['factor']} ({r['code']})"] + [fmt(by_version(cells, f"B3_err_{r['code']}", v))
                                                               for v in versions])
    factors = md_table(["B3 row (factor)"] + [f"{v} error ↓" for v in versions], factor_rows)

    code_rows = []
    pc = scores["per_code"]
    for bid in LISTS:
        for opt in codebook["part_b"][bid]["options"]:
            tag = opt.get("sense") or opt.get("factor")
            if not tag:
                continue
            cellv = []
            for v in versions:
                e = next((x for x in pc if x["version"] == v and x["question"] == bid and x["code"] == opt["code"]), None)
                cellv.append("–" if not e else f"{fmt(e['precision'])} / {fmt(e['recall'])}")
            code_rows.append([bid, tag] + cellv)
    codes = md_table(["Q", "sense / factor"] + [f"{v} precision / recall" for v in versions], code_rows)

    return ("Scores per participant and version, averaged over places and runs, with the baselines below "
            "(scored the same way). ↑ higher is better, ↓ lower is better. A version is only useful where it beats "
            "the baselines.\n\n" + main
            + "\n\n**B4–B6 precision / recall.** Precision = share of the persona's picks the person also made; "
              "recall = share of the person's picks the persona found.\n\n" + pr
            + "\n\n**B3 by factor** (error per situation row).\n\n" + factors
            + "\n\n<details><summary>B4–B6 per sense and per factor (precision / recall)</summary>\n\n"
            + codes + "\n\n</details>")


def section_copy(scores, versions):
    cells = scores["cells"]
    rows = [[v, fmt(by_version(cells, "B3_mae", v)), fmt(by_version(cells, "B3_vs_Q14", v)),
             fmt(by_version(cells, "B4_jaccard", v)), fmt(by_version(cells, "B5_jaccard", v))] for v in versions]
    rows.append(["**copy Part A, no model**", f"**{fmt(baseline_mean(scores, 'copy', 'B3_mae'))}**", "0.00",
                 f"**{fmt(baseline_mean(scores, 'copy', 'B4_jaccard'))}**",
                 f"**{fmt(baseline_mean(scores, 'copy', 'B5_jaccard'))}**"])
    return ("B3 asks the same 4 situations as Q14, B4 the same sense codes as Q15, B5 as Q16. The last row is what "
            "you get by copying the person's Part A answers onto every place, with no model and no photo. A version "
            "shows perception of the place only where it beats that row. *B3 distance to own Q14*: near 0 = the "
            "persona copies the profile instead of judging the place.\n\n"
            + md_table(["Version", "B3 error vs person ↓", "B3 distance to own Q14", "B4 Jaccard ↑", "B5 Jaccard ↑"],
                       rows))


ND_GROUPS = {  # by Q8: does the profile carry a neurodivergence label?
    "neurodivergent or not sure": {"yes", "not_sure"},
    "not neurodivergent": {"no"},
    "prefer not to say": {"prefer_not_to_say"},
}


def section_bias(scores, versions, humans):
    cells = scores["cells"]
    pids = sorted({c["participant"] for c in cells})
    rows = [[v, fmt(by_version(cells, "B2_signed", v)), fmt(by_version(cells, "B3_signed", v))]
            + [f"{fmt(by_version(cells, 'B2_signed', v, p))} / {fmt(by_version(cells, 'B3_signed', v, p))}"
               for p in pids] for v in versions]

    # stereotyping check: labels (V1) should shift answers only where the labels name a neurodivergence
    group_rows = []
    for group, answers in ND_GROUPS.items():
        members = [p for p in pids if humans[p]["part_a"].get("Q8") in answers]
        if not members:
            continue
        for v in versions:
            g = [c for c in cells if c["version"] == v and c["participant"] in members]
            group_rows.append([group, len(members), v, fmt(mean([c.get("B2_signed") for c in g])),
                               fmt(mean([c.get("B3_signed") for c in g]))])
    groups = ("**By neurodivergence (Q8).** If labels trigger a stereotype, V1's shift (B2 negative, B3 positive) "
              "should appear in the first group and not in the second, and V2/V3 should reduce it.\n\n"
              + md_table(["Group (Q8)", "participants", "Version", "B2 signed", "B3 signed"], group_rows))
    return ("Signed error, persona − person. **B2:** negative = the persona feels *less comfortable* than the "
            "person. **B3:** positive = the persona sees *more problems*. Near 0 = no systematic shift.\n\n"
            + md_table(["Version", "B2 signed (all)", "B3 signed (all)"] + [f"{p}: B2 / B3" for p in pids], rows)
            + "\n\n" + groups)


def section_diversity(scores, versions):
    div = scores["diversity"]
    if not div:
        return "Needs at least 2 participants."
    rows = []
    for who in ["Humans"] + versions:
        d = div.get(who)
        if d:
            rows.append([f"**{who}**" if who == "Humans" else who, fmt(d["B2_sd_between"]), fmt(d["B3_sd_between"])]
                        + [fmt(d[f"{b}_jaccard_between"]) for b in LISTS])
    return ("How much answers differ *between participants* for the same place: SD for B2/B3 (higher = more "
            "different), Jaccard for B4–B6 (lower = more different). Close to the Humans row = the version "
            "differentiates people as much as they differ. Diversity alone is not accuracy.\n\n"
            + md_table(["Who", "B2 SD between", "B3 SD between", "B4 Jaccard between", "B5 Jaccard between",
                        "B6 Jaccard between"], rows))


def section_2x2(scores, versions):
    cells = scores["cells"]

    def avg(key, v):
        return by_version(cells, key, v)
    rows = []
    for factor, comparisons in EFFECTS.items():
        for without, with_, context in comparisons:
            if without not in versions or with_ not in versions:
                continue
            rows.append([f"+ {factor}", context, f"{without} → {with_}",
                         f"{avg('B3_mae', with_) - avg('B3_mae', without):+.2f}",
                         f"{avg('B2_abs_error', with_) - avg('B2_abs_error', without):+.2f}",
                         f"{abs(avg('B3_signed', with_)) - abs(avg('B3_signed', without)):+.2f}",
                         f"{abs(avg('B2_signed', with_)) - abs(avg('B2_signed', without)):+.2f}"])
    return ("Each row adds one factor while the other stays fixed. In every column, **negative = better** "
            "(closer to the person, or less systematic shift). Main comparison for the thesis: **V1 vs V3**.\n\n"
            "| | No lived experience | Lived experience (Q13–Q20) |\n|---|---|---|\n"
            "| **No labels** | V0 | V3 |\n| **Labels (Q1–Q5, Q7–Q12)** | V1 | V2 |\n\n"
            + md_table(["Adding", "", "Versions", "B3 MAE change", "B2 abs. error change", "B3 bias change",
                        "B2 bias change"], rows)
            + (f"\n\n**V1 vs V3** (labels only vs lived experience only): B3 MAE {fmt(avg('B3_mae', 'V1'))} vs "
               f"{fmt(avg('B3_mae', 'V3'))}; B3 bias {fmt(avg('B3_signed', 'V1'))} vs {fmt(avg('B3_signed', 'V3'))}."
               if "V1" in versions and "V3" in versions else ""))


def section_pairs(scores, versions):
    cells = {(c["participant"], c["place"], c["version"]): c for c in scores["cells"]}
    pairs = [(a, b) for i, a in enumerate(versions) for b in versions[i + 1:]]
    beyond, total, detail = Counter(), Counter(), []
    for pid in sorted({c["participant"] for c in scores["cells"]}):
        for place in scores["places"]:
            for a, b in pairs:
                ca, cb = cells.get((pid, place, a)), cells.get((pid, place, b))
                if not ca or not cb:
                    continue
                d2, n2 = abs(ca["B2_mean"] - cb["B2_mean"]), max(ca["B2_range"], cb["B2_range"])
                d3, n3 = abs(ca["B3_mean"] - cb["B3_mean"]), max(ca["B3_range"], cb["B3_range"])
                beyond[(a, b, "B2")] += d2 > n2
                beyond[(a, b, "B3")] += d3 > n3
                total[(a, b)] += 1
                detail.append([pid, place, f"{a} vs {b}", f"{fmt(d2)} vs {fmt(n2)}", "**yes**" if d2 > n2 else "no",
                               f"{fmt(d3)} vs {fmt(n3)}", "**yes**" if d3 > n3 else "no"])
    summary = md_table(["Versions", "B2: differs beyond noise", "B3: differs beyond noise"],
                       [[f"{a} vs {b}", f"{beyond[(a, b, 'B2')]}/{total[(a, b)]}",
                         f"{beyond[(a, b, 'B3')]}/{total[(a, b)]}"] for a, b in pairs])
    return ("For each participant and place: the difference between two versions' mean answers vs the run-to-run "
            "noise (with 2 runs: the difference between the two runs, the larger of the two versions'). "
            "A difference counts only if it is larger than the noise.\n\n" + summary
            + "\n\n<details><summary>Every comparison</summary>\n\n"
            + md_table(["Participant", "Place", "Versions", "B2: difference vs noise", "beyond?",
                        "B3 mean: difference vs noise", "beyond?"], detail) + "\n\n</details>")


def section_b2_people(scores, versions, codebook):
    sp = {(s["participant"], s["version"]): s for s in scores["spearman_B2"]}
    names = codebook.get("place_names") or []
    rows = []
    for pid in sorted({s["participant"] for s in scores["spearman_B2"]}):
        any_v = next(sp[(pid, v)] for v in versions if (pid, v) in sp)
        rows.append([pid, "**person**"] + [str(x) for x in any_v["human_B2"]] + [""])
        for v in versions:
            s = sp.get((pid, v))
            if s:
                rows.append(["", v] + [fmt(x, 1) for x in s["persona_B2_mean"]] + [fmt(s["rho"])])
    head = ["Participant", "Who"] + [f"{pl}{' ' + names[i] if i < len(names) else ''}"
                                     for i, pl in enumerate(scores["places"])] + ["ρ"]
    return ("Each person's B2 (comfort) per place, next to each version's mean over runs, to see where the order "
            "of places is right or reversed.\n\n" + md_table(head, rows))


def section_language(scores, versions, humans):
    by_lang = defaultdict(list)
    for pid, p in humans.items():
        by_lang[p["lang"]].append(pid)
    langs = ", ".join(f"{pid}: {p['lang']}" for pid, p in sorted(humans.items()))
    if len(by_lang) < 2:
        return (f"Form language per participant: {langs}. Only one language so far, so the it/en comparison "
                "is not possible yet.")
    cells = scores["cells"]
    rows = []
    for lang, pids in sorted(by_lang.items()):
        for v in versions:
            g = [c for c in cells if c["version"] == v and c["participant"] in pids]
            rows.append([lang, len(pids), v, fmt(mean([c.get("B3_mae") for c in g])),
                         fmt(mean([c.get("B2_abs_error") for c in g]))])
    return (f"Form language per participant: {langs}. Open answers stay untranslated; this checks that they "
            "don't hurt.\n\n" + md_table(["Language", "participants", "Version", "B3 MAE ↓", "B2 abs. error ↓"], rows))


def section_leaks(leaks):
    flagged = {pid: h for pid, h in leaks.items() if h}
    if not flagged:
        return ("No V3 prompt contains a diagnosis keyword in the person's own words (Q20, \"Other\" texts): "
                "V3 is free of explicit labels for every participant.")
    return ("V3 prompts whose own-words text (Q20, \"Other\") names a diagnosis or condition. The words stay "
            "verbatim; check these participants separately in the analysis.\n\n"
            + md_table(["Participant", "Question", "Keywords"],
                       [[pid, h["question"], ", ".join(h["keywords"])] for pid, hs in flagged.items() for h in hs]))


def section_time_cost(calls, codebook):
    secs, costs = [c["seconds"] for c in calls], [c["cost"] for c in calls]
    med, mean_cost = statistics.median(secs), statistics.mean(costs)
    n = N_PARTICIPANTS_FULL * codebook["places"] * len(VERSIONS_ALL) * N_RUNS_FULL
    return "\n".join([
        f"- Seconds per call (incl. retries): median **{med:.1f}**, max {max(secs):.1f}.",
        f"- Cost per call: mean **${mean_cost:.4f}**; total ${sum(costs):.4f} for {len(calls)} calls.",
        f"- Projection for {N_PARTICIPANTS_FULL} participants × {codebook['places']} places × {len(VERSIONS_ALL)} "
        f"versions × {N_RUNS_FULL} runs = {n} calls: about **${n * mean_cost:.2f}** and **{n * med / 3600:.1f} h** "
        "one call at a time."])


def reasoning_samples(calls, versions, path):
    out = ["# Reasoning samples", "", "Not committed to git: persona reasoning can repeat health information.", ""]
    for pid in sorted({c["participant"] for c in calls}):
        out.append(f"## {pid}, place P1, run 1")
        for v in versions:
            c = next((c for c in calls if c["participant"] == pid and c["version"] == v and c["place"] == "P1"
                      and c["run"] == 1 and c["valid"]), None)
            out += [f"**{v}**", "", f"> {c['parsed']['reasoning'] if c else '(no valid answer)'}", ""]
    path.write_text("\n".join(out), encoding="utf-8")


def summary_bullets(calls, scores, dist, versions, humans):
    cells = scores["cells"]
    n, valid = len(calls), sum(c["valid"] for c in calls)
    b = [f"**Pipeline:** {n} calls answered; {valid}/{n} valid "
         f"({sum(1 for c in calls if len(answered(c)) > 1)} needed a retry)."]
    b.append("**B3 (primary), mean error:** " + ", ".join(f"{v} {fmt(by_version(cells, 'B3_mae', v))}" for v in versions)
             + f"; copy Part A {fmt(baseline_mean(scores, 'copy', 'B3_mae'))}, chance "
               f"{fmt(baseline_mean(scores, 'chance', 'B3_mae'))}.")
    b.append("**Bias (B3 signed):** " + ", ".join(f"{v} {fmt(by_version(cells, 'B3_signed', v))}" for v in versions)
             + " (positive = persona sees more problems than the person).")
    b.append("**Scale use (B2):** humans " + ", ".join(str(k) for k in sorted(dist["B2"]["Humans"]))
             + "; " + "; ".join(f"{v} {', '.join(str(k) for k in sorted(dist['B2'][v]))}" for v in versions) + ".")
    costs = [c["cost"] for c in calls]
    b.append(f"**Time and cost:** median {statistics.median(c['seconds'] for c in calls):.1f} s and "
             f"${statistics.mean(costs):.4f} per call.")
    b.append(f"With {len(humans)} participant{'s' if len(humans) != 1 else ''} this is a feasibility check, "
             "not a result.")
    return b


# --- main --------------------------------------------------------------------

def main():
    run_dirs = [Path(a) for a in sys.argv[1:]]
    if not run_dirs:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    calls, api_errors, scores, configs, study, humans, leaks = load(run_dirs)
    codebook = study.codebook()
    versions = [v for v in VERSIONS_ALL if any(c["version"] == v for c in calls)]
    out = run_dirs[0]

    dist = scale_distributions(calls, humans, versions, scores["places"])
    chart_scale(dist, versions, out / "scale_use.png")
    reasoning_samples(calls, versions, out / "reasoning_samples.md")

    cfg = configs[0]
    header = [
        f"# Pilot report: {study.name}",
        "",
        f"`pilot: true` — {len(humans)} participant{'s' if len(humans) != 1 else ''}; "
        "no conclusions about the hypotheses.",
        "",
        f"- Runs: {', '.join(f'`{c['run_id']}`' for c in configs)}",
        f"- Model: `{cfg['model']}` · temperature {cfg['temperature']} · {cfg['runs_per_condition']} runs per "
        f"condition · provider routing {cfg.get('provider')} · codebook {cfg.get('codebook')} "
        f"(version {cfg.get('codebook_version')})",
        f"- Versions: {', '.join(versions)} · participants: {len(humans)} · places: {codebook['places']}",
        "",
    ]
    sections = [
        ("1. Summary", "\n".join(f"- {x}" for x in summary_bullets(calls, scores, dist, versions, humans))
         + "\n- _To add after reading: what to fix before the real run (format, instructions, limits only)._"),
        ("2. Format", section_format(calls, api_errors, codebook)),
        ("3. Scale use", section_scale(dist, versions)),
        ("4. Scores and baselines", section_scores(scores, versions, codebook)),
        ("5. Copy check", section_copy(scores, versions)),
        ("6. Bias", section_bias(scores, versions, humans)),
        ("7. Diversity", section_diversity(scores, versions)),
        ("8. 2 × 2 view", section_2x2(scores, versions)),
        ("9. Version pairs vs run-to-run noise", section_pairs(scores, versions)),
        ("10. B2 per participant", section_b2_people(scores, versions, codebook)),
        ("11. Language", section_language(scores, versions, humans)),
        ("12. Label leaks", section_leaks(leaks)),
        ("13. Time and cost", section_time_cost(calls, codebook)),
        ("14. Reasoning samples", "One reasoning per version for place P1, run 1, verbatim, in "
                                  "[`reasoning_samples.md`](reasoning_samples.md) (kept out of git: it can repeat "
                                  "health information)."),
    ]
    body = "\n".join(header) + "\n" + "\n\n".join(f"## {t}\n\n{s}" for t, s in sections) + "\n"
    (out / "report.md").write_text(body, encoding="utf-8")
    print(f"Report -> {out / 'report.md'} (+ scale_use.png, reasoning_samples.md)")


if __name__ == "__main__":
    main()
