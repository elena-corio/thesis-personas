"""Step 4b: write runs/<run_id>/report.md and charts from calls.jsonl and scores.json.

    python src/report.py runs/<run_id> [runs/<run_id2> ...]

Run src/score.py with the same folders first. The report and charts go to the first
folder. Reasoning samples are written to a separate file, reasoning_samples.md, which
stays out of git: persona reasoning can repeat a participant's health information.
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

ROOT = Path(__file__).resolve().parent.parent
PARTICIPANTS = ROOT / "data" / "clean" / "participants.json"

N_PARTICIPANTS_FULL, N_PLACES, N_RUNS = 40, 4, 3
LISTS = {"B4": "B4_senses", "B5": "B5_elements_bother", "B6": "B6_elements_feel_good", "B7": "B7_changes"}

# chart colours: humans neutral, versions in fixed categorical order (validated palette, light mode)
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
HUMAN = "#8a8984"
VERSION_COLOURS = {"V0": "#2a78d6", "V1": "#eb6834", "V2": "#1baf7a", "V3": "#eda100"}


def load(run_dirs):
    """Latest record of every call that got an answer; API errors are counted separately."""
    calls, api_errors = [], 0
    for d in run_dirs:
        latest = latest_records(d / "calls.jsonl").values()
        calls += [r for r in latest if r["status"] != "api_error"]
        api_errors += sum(r["status"] == "api_error" for r in latest)
    with open(run_dirs[0] / "scores.json", encoding="utf-8") as f:
        scores = json.load(f)
    configs = []
    for d in run_dirs:
        with open(d / "run.yaml", encoding="utf-8") as f:
            configs.append(yaml.safe_load(f))
    with open(PARTICIPANTS, encoding="utf-8") as f:
        humans = {p["id"]: p for p in json.load(f)}
    return calls, api_errors, scores, configs, humans


def fmt(x, nd=2):
    return "–" if x is None else f"{x:.{nd}f}"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# --- sections ----------------------------------------------------------------

def answered(call):
    """Attempts that reached the model (API errors and rate limits excluded)."""
    return [a for a in call["attempts"] if "raw_response" in a]


def short(error, n=140):
    return error if len(error) <= n else error[: n - 1] + "…"


def section_format(calls, api_errors):
    n = len(calls)
    valid = sum(c["valid"] for c in calls)
    retried = [c for c in calls if len(answered(c)) > 1]
    fixed = sum(c["valid"] for c in retried)
    invalid = [c for c in calls if not c["valid"]]
    api_attempts = Counter(short(a["error"], 90) for c in calls for a in c["attempts"] if "error" in a)
    lines = [f"- Valid answers: **{valid} / {n}** answered calls ({100 * valid / n:.0f}%).",
             f"- Calls retried once for an invalid answer: {len(retried)} ({fixed} became valid on retry)."]
    if api_errors:
        lines.append(f"- ⚠ Calls still without an answer (API error, not a format problem): {api_errors}.")
    if api_attempts:
        lines.append("- API errors before an answer came through (not format problems; the call was done again):")
        lines += [f"  - {e} ({k}×)" for e, k in api_attempts.most_common()]
    first_errors = Counter(short(e) for c in retried for e in answered(c)[0]["errors"])
    if first_errors:
        lines.append("- First-attempt errors that triggered a retry:")
        lines += [f"  - {e} ({k}×)" for e, k in first_errors.most_common()]
    if invalid:
        lines.append(f"- Invalid after retry: {len(invalid)}")
        lines += [f"  - {c['participant']} {c['version']} {c['place']} run {c['run']}: {'; '.join(c['errors'])}"
                  for c in invalid]
    else:
        lines.append("- Invalid after retry: none.")
    models = Counter(c["model"] for c in calls)
    providers = Counter(a.get("provider") for c in calls for a in answered(c))
    finish = Counter(a.get("finish_reason") for c in calls for a in answered(c))
    lines.append(f"- Model ids returned by the API: {dict(models)}; providers: {dict(providers)}; "
                 f"finish reasons: {dict(finish)}.")
    return "\n".join(lines)


def scale_distributions(calls, humans, versions):
    dist = {"B2": {}, "B3": {}}
    h_b2 = [p["part_b"][f"P{i}"]["B2"] for p in humans.values() for i in range(1, 5)]
    h_b3 = [v for p in humans.values() for i in range(1, 5) for v in p["part_b"][f"P{i}"]["B3"].values()
            if v is not None]
    dist["B2"]["Humans"], dist["B3"]["Humans"] = Counter(h_b2), Counter(h_b3)
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
        ax.set_title({"B2": "B2 comfort", "B3": "B3 situations (all 4 rows)"}[q],
                     loc="left", fontsize=10, color=INK)
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


def section_scale(dist, versions, chart_name):
    rows = []
    for q in ("B2", "B3"):
        for name in ["Humans"] + versions:
            c = dist[q][name]
            total = sum(c.values())
            mid = c.get(3, 0) / total if total else 0
            ext = (c.get(1, 0) + c.get(5, 0)) / total if total else 0
            rows.append([q, name, total] + [c.get(x, 0) for x in range(1, 6)] + [f"{100 * mid:.0f}%", f"{100 * ext:.0f}%"])
    table = md_table(["Q", "Who", "n", "1", "2", "3", "4", "5", "share of 3", "share of 1 or 5"], rows)
    return f"![Distribution of B2 and B3 answers]({chart_name})\n\n{table}"


def section_scores(scores, versions):
    cells, sp = scores["cells"], scores["spearman_B2"]
    rho = {(s["participant"], s["version"]): s for s in sp}
    rows = []
    groups = defaultdict(list)
    for c in cells:
        groups[(c["participant"], c["version"])].append(c)
    keys = ["B2_abs_error", "B3_mae"] + [f"{b}_jaccard" for b in LISTS]
    for pid in sorted({c["participant"] for c in cells}):
        for v in versions:
            g = groups.get((pid, v), [])
            if not g:
                continue
            r = rho.get((pid, v), {})
            rows.append([pid, v, fmt(r.get("rho")) + (f" ({r['note']})" if r.get("note") else "")]
                        + [fmt(statistics.mean(c[k] for c in g)) for k in keys])
    for v in versions:
        g = [c for c in cells if c["version"] == v]
        if g:
            rhos = [s["rho"] for s in sp if s["version"] == v and s["rho"] is not None]
            rows.append(["**all**", f"**{v}**", fmt(statistics.mean(rhos)) if rhos else "–"]
                        + [f"**{fmt(statistics.mean(c[k] for c in g))}**" for k in keys])
    head = ["Participant", "Version", "B2 Spearman ρ (4 places) ↑", "B2 abs. error ↓", "B3 MAE ↓",
            "B4 Jaccard ↑", "B5 Jaccard ↑", "B6 Jaccard ↑", "B7 Jaccard ↑"]
    return ("Scores per participant and version, averaged over places and runs. ↑ higher is better, "
            "↓ lower is better. Errors are on the 1–5 scale; Jaccard is 0 (no overlap) to 1 (same selection).\n\n"
            + md_table(head, rows))


FIXED_LAST = {"none", "none_in_particular", "no_change_needed", "other"}  # never shuffled


def mean_of(cells, key, version):
    vals = [c[key] for c in cells if c["version"] == version and c.get(key) is not None]
    return statistics.mean(vals) if vals else None


def section_bias_copy_diversity(scores, versions):
    cells, base, div = scores["cells"], scores["baselines"], scores["diversity"]

    def base_mean(key):
        vals = [b[key] for b in base if b[key] is not None]
        return statistics.mean(vals) if vals else None

    # bias
    bias_rows = [[v, fmt(mean_of(cells, "B2_signed", v)), fmt(mean_of(cells, "B3_signed", v))] for v in versions]
    bias = ("**Bias** = signed error, persona − person, averaged over participants, places and runs. "
            "**B2:** negative = the persona feels *less comfortable* than the person. "
            "**B3:** positive = the persona sees *more problems* than the person. "
            "Near 0 = no systematic shift (errors may still be large in both directions; see section 3).\n\n"
            + md_table(["Version", "B2 signed error", "B3 signed error"], bias_rows))

    # copying the profile
    copy_rows = [[v, fmt(mean_of(cells, "B3_mae", v)), fmt(mean_of(cells, "B3_vs_Q15", v)),
                  fmt(mean_of(cells, "B4_jaccard", v))] for v in versions]
    copy_rows.append(["**profile only, no model**", f"**{fmt(base_mean('B3_copy_Q15_mae'))}**", "0.00",
                      f"**{fmt(base_mean('B4_copy_Q16_jaccard'))}**"])
    copy = ("**Copying the profile.** B3 asks the same 4 situations as Q15, and Q15 is in the V2/V3 persona "
            "text; B4 asks the same senses as Q16. The last row is what you get with no model and no photo: "
            "B3 = the person's own Q15 answers for every place, B4 = the 3 senses they rated highest in Q16. "
            "A version only shows perception of the place if it does **better** than this row. "
            "*B3 distance to Q15*: how close the persona's B3 is to the person's Q15 (near 0 = it copies the "
            "profile instead of judging the place).\n\n"
            + md_table(["Version", "B3 error vs person ↓", "B3 distance to own Q15", "B4 Jaccard ↑"], copy_rows)
            + f"\n\nFor B2, always answering 3 gives an error of **{fmt(base_mean('B2_const3_abs_error'))}** "
              "(compare with section 3).")

    # diversity
    div_rows = []
    for who in ["Humans"] + versions:
        d = div.get(who)
        if not d:
            continue
        name = f"**{who}**" if who == "Humans" else who
        div_rows.append([name, fmt(d["B2_sd_between"]), fmt(d["B3_sd_between"])]
                        + [fmt(d[f"{b}_jaccard_between"]) for b in LISTS])
    diversity = ("**Diversity** = how much the answers differ *between participants* for the same place. "
                 "SD across participants for B2/B3 (higher = more different); Jaccard between participants "
                 "for B4–B7 (lower = more different). A version is as diverse as the people when its row is "
                 "close to the Humans row; a version that gives everyone the same answer has SD near 0 and "
                 "Jaccard near 1. Diversity alone is not accuracy: random answers are diverse too.\n\n"
                 + md_table(["Who", "B2 SD between", "B3 SD between", "B4 Jaccard between",
                             "B5 Jaccard between", "B6 Jaccard between", "B7 Jaccard between"], div_rows))
    return f"{bias}\n\n{copy}\n\n{diversity}"


def position_bias(calls):
    """Mean relative position (0 = first shown, 1 = last) of the chosen codes among the shuffled options."""
    out = {}
    for bid, key in LISTS.items():
        pos = []
        for c in calls:
            if not c["valid"]:
                continue
            order = [code for code in c["option_order"][bid] if code not in FIXED_LAST]
            n = len(order)
            for code in c["parsed"][key]:
                if code in order:
                    pos.append(order.index(code) / (n - 1) if n > 1 else 0.5)
        out[bid] = (statistics.mean(pos), len(pos)) if pos else (None, 0)
    return out


def section_stability(scores, calls, versions):
    cells = scores["cells"]
    rows = []
    for v in versions:
        g = [c for c in cells if c["version"] == v]
        if not g:
            continue
        rows.append([v, fmt(statistics.mean(c["B2_sd"] for c in g)),
                     f"{sum(c['B2_range'] == 0 for c in g)}/{len(g)}",
                     fmt(statistics.mean(c["B3_sd"] for c in g))]
                    + [fmt(statistics.mean(c[f"{b}_run_agreement"] for c in g
                                           if c[f"{b}_run_agreement"] is not None)) for b in LISTS])
    table = md_table(["Version", "B2 SD over runs", "places with identical B2 in all runs", "B3 SD over runs",
                      "B4 run agreement", "B5 run agreement", "B6 run agreement", "B7 run agreement"], rows)
    bias = position_bias(calls)
    bias_rows = [[b, fmt(m), n] for b, (m, n) in bias.items()]
    return (f"Run-to-run spread within each participant × version × place (3 runs), averaged. "
            f"Run agreement = mean Jaccard between the runs' selections (1 = identical choices every run).\n\n{table}\n\n"
            "**Option order.** B4–B7 options were shuffled in every call. If the shown order did not matter, the "
            "chosen options would sit on average in the middle of the shuffled list (0.50; 0 = first, 1 = last). "
            "Values well below 0.50 mean the model tends to pick options shown first.\n\n"
            + md_table(["Question", "mean relative position of chosen options", "choices"], bias_rows))


def section_time_cost(calls, versions):
    secs = [c["seconds"] for c in calls]
    costs = [c["cost"] for c in calls]
    med, mx = statistics.median(secs), max(secs)
    mean_cost = statistics.mean(costs)
    lines = [f"- Seconds per call (incl. retries): median **{med:.1f}**, max {mx:.1f}.",
             f"- Cost per call: mean **${mean_cost:.4f}**; total ${sum(costs):.4f} for {len(calls)} calls."]
    for nv in sorted({3, len(versions)}):
        n = N_PARTICIPANTS_FULL * N_PLACES * N_RUNS * nv
        lines.append(f"- Projection for {N_PARTICIPANTS_FULL} participants × {N_PLACES} places × {N_RUNS} runs × "
                     f"{nv} versions = {n} calls: about **${n * mean_cost:.2f}** and "
                     f"**{n * med / 3600:.1f} h** one call at a time.")
    if any(c is None for c in costs) or sum(costs) == 0:
        lines.append("- ⚠ Some calls reported no cost: the cost cap could not be enforced for them.")
    return "\n".join(lines)


def reasoning_samples(calls, versions, path):
    out = ["# Reasoning samples", "",
           "Not committed to git: persona reasoning can repeat a participant's health information.", ""]
    for pid in sorted({c["participant"] for c in calls}):
        out.append(f"## {pid}, place P1, run 1")
        for v in versions:
            c = next((c for c in calls if c["participant"] == pid and c["version"] == v and c["place"] == "P1"
                      and c["run"] == 1 and c["valid"]), None)
            text = c["parsed"]["reasoning"] if c else "(no valid answer)"
            out += [f"**{v}**", "", f"> {text}", ""]
    path.write_text("\n".join(out), encoding="utf-8")


# the 2 x 2 design: (without, with) pairs for each factor
EFFECTS = {
    "Labels": [("V0", "V1", "without sensory info"), ("V3", "V2", "with sensory info")],
    "Sensory info": [("V0", "V3", "without labels"), ("V1", "V2", "with labels")],
}


def section_versions(scores, versions):
    cells = {(c["participant"], c["place"], c["version"]): c for c in scores["cells"]}
    pairs = [(a, b) for i, a in enumerate(versions) for b in versions[i + 1:]]

    def avg(key, v):
        return mean_of(scores["cells"], key, v)

    # 2 x 2 view: what does adding each factor change?
    effect_rows = []
    for factor, comparisons in EFFECTS.items():
        for without, with_, context in comparisons:
            if without not in versions or with_ not in versions:
                continue
            d_b2 = avg("B2_abs_error", with_) - avg("B2_abs_error", without)
            d_b3 = avg("B3_mae", with_) - avg("B3_mae", without)
            d_bias2 = abs(avg("B2_signed", with_)) - abs(avg("B2_signed", without))
            d_bias3 = abs(avg("B3_signed", with_)) - abs(avg("B3_signed", without))
            effect_rows.append([f"+ {factor}", context, f"{without} → {with_}",
                                f"{d_b2:+.2f}", f"{d_b3:+.2f}", f"{d_bias2:+.2f}", f"{d_bias3:+.2f}"])
    effects = ("**2 × 2 view.** Each row adds one factor while the other stays fixed. "
               "In every change column, **negative = better**: closer to the person (error) or less "
               "systematic shift (bias = size of the signed error in section 4b).\n\n"
               "|               | No sensory info | Sensory info |\n|---|---|---|\n"
               "| **No labels** | V0 | V3 |\n| **Labels** | V1 | V2 |\n\n"
               + md_table(["Adding", "", "Versions", "B2 abs. error change", "B3 MAE change",
                           "B2 bias change", "B3 bias change"], effect_rows))
    rows = []
    beyond = Counter()
    total = Counter()
    for pid in sorted({c["participant"] for c in scores["cells"]}):
        for place in [f"P{i}" for i in range(1, 5)]:
            for a, b in pairs:
                ca, cb = cells.get((pid, place, a)), cells.get((pid, place, b))
                if not ca or not cb:
                    continue
                d2 = abs(ca["B2_mean"] - cb["B2_mean"])
                noise2 = max(ca["B2_range"], cb["B2_range"])
                d3 = abs(ca["B3_mean"] - cb["B3_mean"])
                noise3 = max(ca["B3_sd"], cb["B3_sd"]) * 2
                ok2, ok3 = d2 > noise2, d3 > noise3
                beyond[(a, b, "B2")] += ok2
                beyond[(a, b, "B3")] += ok3
                total[(a, b)] += 1
                rows.append([pid, place, f"{a} vs {b}",
                             f"{fmt(d2)} vs {fmt(noise2)}", "**yes**" if ok2 else "no",
                             f"{fmt(d3)} vs {fmt(noise3)}", "**yes**" if ok3 else "no"])
    summary = md_table(["Comparison", "B2: differs beyond noise", "B3: differs beyond noise"],
                       [[f"{a} vs {b}", f"{beyond[(a, b, 'B2')]}/{total[(a, b)]}",
                         f"{beyond[(a, b, 'B3')]}/{total[(a, b)]}"] for a, b in pairs])
    detail = md_table(["Participant", "Place", "Versions",
                       "B2: |difference of means| vs noise", "beyond noise?",
                       "B3: |difference of means| vs noise", "beyond noise?"], rows)
    return (effects + "\n\n**Beyond run-to-run noise?** "
            "For each participant and place, the difference between two versions' mean answers (over 3 runs) "
            "is compared with the run-to-run noise. **B2 noise** = the larger of the two versions' ranges over runs "
            "(max − min). **B3 noise** = twice the larger SD over runs of the mean B3 score. A difference counts "
            "only if it is larger than the noise.\n\n" + summary
            + "\n\n<details><summary>Every comparison</summary>\n\n" + detail + "\n\n</details>")


def summary_bullets(calls, scores, dist, versions):
    n = len(calls)
    valid = sum(c["valid"] for c in calls)
    retried = sum(1 for c in calls if len(answered(c)) > 1)
    b = [f"**Pipeline:** {n} calls answered end to end; {valid}/{n} answers valid ({retried} needed a retry)."]
    mids = {v: dist["B2"][v].get(3, 0) / max(sum(dist["B2"][v].values()), 1) for v in versions}
    used = {v: sorted(k for k, x in dist["B2"][v].items() if x) for v in versions}
    hm = dist["B2"]["Humans"]
    b.append("**Scale use (B2):** humans used " + ", ".join(str(k) for k in sorted(hm) if hm[k])
             + "; personas used " + "; ".join(f"{v}: {', '.join(map(str, used[v]))} ({100 * mids[v]:.0f}% 3s)"
                                              for v in versions) + ".")
    cells = scores["cells"]
    err = {v: statistics.mean(c["B2_abs_error"] for c in cells if c["version"] == v) for v in versions}
    b.append("**B2 error by version** (mean abs. error, 1–5 scale): "
             + ", ".join(f"{v} {err[v]:.2f}" for v in versions)
             + f". With {len({c["participant"] for c in calls})} participants this is a feasibility check, not a result.")
    costs = [c["cost"] for c in calls]
    b.append(f"**Time and cost:** median {statistics.median(c['seconds'] for c in calls):.1f} s and "
             f"${statistics.mean(costs):.4f} per call.")
    return b


# --- main --------------------------------------------------------------------

def main():
    run_dirs = [Path(a) for a in sys.argv[1:]]
    if not run_dirs:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    calls, api_errors, scores, configs, humans = load(run_dirs)
    versions = [v for v in ("V0", "V1", "V2", "V3") if any(c["version"] == v for c in calls)]
    out = run_dirs[0]

    dist = scale_distributions(calls, humans, versions)
    chart_scale(dist, versions, out / "scale_use.png")
    reasoning_samples(calls, versions, out / "reasoning_samples.md")

    cfg = configs[0]
    header = [
        "# Smoke test report",
        "",
        f"`pilot: true` — feasibility check with {len(humans)} participants; no conclusions about the hypotheses.",
        "",
        f"- Runs: {', '.join(f'`{c['run_id']}`' for c in configs)}",
        f"- Model: `{cfg['model']}` · temperature {cfg['temperature']} · {cfg['runs_per_condition']} runs per "
        f"condition · provider routing {cfg.get('provider')} · codebook {cfg.get('codebook_version')}",
        f"- Versions: {', '.join(versions)} · participants: {len(humans)} · places: {N_PLACES}",
        "",
        "## Summary",
        "",
        *[f"- {x}" for x in summary_bullets(calls, scores, dist, versions)],
        "- _To add after reading: what to fix before the real run (format, instructions, limits only)._",
        "",
    ]
    sections = [
        ("1. Format", section_format(calls, api_errors)),
        ("2. Scale use", section_scale(dist, versions, "scale_use.png")),
        ("3. Scores", section_scores(scores, versions)),
        ("4. Stability", section_stability(scores, calls, versions)),
        ("4b. Bias, copying the profile, diversity", section_bias_copy_diversity(scores, versions)),
        ("5. Time and cost", section_time_cost(calls, versions)),
        ("6. Reasoning samples", "For each participant, one reasoning per version for place P1, run 1, verbatim, "
                                 "in [`reasoning_samples.md`](reasoning_samples.md) (kept out of git because "
                                 "it can repeat health information)."),
        ("7. Do the versions really differ?", section_versions(scores, versions)),
    ]
    body = "\n".join(header) + "\n" + "\n\n".join(f"## {t}\n\n{s}" for t, s in sections) + "\n"
    (out / "report.md").write_text(body, encoding="utf-8")
    print(f"Report -> {out / 'report.md'} (+ scale_use.png, reasoning_samples.md)")


if __name__ == "__main__":
    main()
