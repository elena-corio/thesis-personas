"""Step 2: data/clean/<study>/participants.json -> personas.json (persona texts per version).

    python src/build_personas.py              # write personas.json and label_leaks.json, print a summary
    python src/build_personas.py --show P01   # also print that participant's texts
    python src/build_personas.py --task       # also print one shuffled task text
    (--study pilot2 is the default)

Versions (2 x 2: labels x lived experience):
    V0 = persona_v0.txt                              no persona
    V1 = persona_v1.txt                              labels (Q1-Q5, Q7-Q12)
    V2 = persona_v2.txt = V1 + persona_experience    labels + lived experience
    V3 = persona_v3.txt = V0 + persona_experience    lived experience only (Q13-Q20)

Template notation:
    {Q7}        an answer; {AGE} and {RESIDENCE} combine answers into one phrase
    {V0} {V1} {EXPERIENCE}   another rendered template, included whole
    [ ... ]     an optional part, dropped when an answer inside it is missing
A line is dropped when an answer outside [ ] is missing, or when nothing but fixed text
would remain. Missing = "Prefer not to say", or left empty although it was asked.
Questions the form skipped (asked_if in the codebook, e.g. Q9-Q12 when Q8 = no) are
written "not applicable". Exclusive "None" answers are written "none".

Labels: persona_en where the codebook gives it, otherwise en with the first letter
lowercased (not for ADHD, PTSD, DCD and names). Open answers (Q20, "Other") verbatim.

Label leaks: V3 must carry no labels, but the person's own words (Q20, "Other" texts)
may name a diagnosis. They stay verbatim; the V3 texts that contain a diagnosis keyword
are listed in label_leaks.json for the analysis.
"""

import argparse
import json
import random
import re
import sys

from study import ROOT, Study, add_study_arg

VERSIONS = ("V0", "V1", "V2", "V3")
MISSING_CODES = {"prefer_not_to_say"}
NONE_CODES = {"none"}
KEEP_CAPITAL = ("ADHD", "PTSD", "DCD", "Austria", "Tourette's", "I ")
NOT_APPLICABLE = "not applicable"
LEAK_RE = re.compile(
    r"autis\w*|\badhd\b|neurodiverg\w*|diagnos\w*|tinnitus|hyperacusi\w*|photophobi\w*|"
    r"asthma|allerg\w*|chronic pain|migraine|epilep\w*|panic attack\w*|phobi\w*|\bc?ptsd\b|"
    r"dyspraxi\w*|\bdcd\b|tourette\w*|dyslexi\w*|dyscalculi\w*|"
    r"autistic\w*|disturbo|disprassi\w*|dislessi\w*|emicrania|epilessi\w*|asma|fotofobi\w*|acufene",
    re.IGNORECASE)

PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
OPTIONAL_RE = re.compile(r"\[([^\[\]]*)\]")


def read_prompt(study, name):
    return (study.prompts / name).read_text(encoding="utf-8").strip()


# --- answers -> English text -------------------------------------------------

def inline(label):
    """Lower-case a label's first letter for use inside a sentence (not acronyms or names)."""
    if label.startswith(KEEP_CAPITAL):
        return label
    return label[0].lower() + label[1:]


def option_text(option):
    return option.get("persona_en") or inline(option["en"])


def asked(question, answers):
    """False when the codebook's asked_if condition says the form skipped this question."""
    cond = question.get("asked_if")
    if not cond:
        return True
    m = re.fullmatch(r"(Q\d+) in \[(.*)\]", cond.strip())
    if not m:
        raise ValueError(f"asked_if not understood: {cond!r}")
    allowed = {c.strip().strip("'\"") for c in m.group(2).split(",")}
    return answers.get(m.group(1)) in allowed


def render_answer(question, value, other_text):
    """-> English text, or None when the answer is missing."""
    qtype = question["type"]
    if value is None or value == [] or value == {}:
        return None
    if qtype == "scale_1_5":
        return str(value)
    if qtype == "grid_1_5":
        labels = {r["code"]: inline(r["en"]) for r in question["rows"]}
        parts = [f"{labels[code]}: {v}" for code, v in value.items() if v is not None]
        return "; ".join(parts) or None
    if qtype == "open_text":
        return value
    options = {o["code"]: o for o in question.get("options", [])}

    def one(code):
        if code == "other":
            return f'"{other_text}"' if other_text else None
        if code in NONE_CODES:
            return "none"
        return option_text(options[code])

    if qtype == "single":
        return None if value in MISSING_CODES else one(value)
    if qtype == "multi":
        codes = [c for c in value if c not in MISSING_CODES]
        parts = [p for p in (one(c) for c in codes) if p]
        return "; ".join(parts) or None  # labels may contain commas, so items are separated by ";"
    raise ValueError(f"unknown type {qtype}")


def answer_texts(participant, codebook):
    """{placeholder: English text or None} for every Part A question given to a persona."""
    answers = participant["part_a"]
    other = participant.get("other_text", {})
    texts = {}
    for qid, q in codebook["part_a"].items():
        if q.get("persona") not in ("labels", "sensory"):
            continue  # Q6 and anything analysis-only never reach a persona
        if not asked(q, answers):
            texts[qid] = NOT_APPLICABLE
        else:
            texts[qid] = render_answer(q, answers.get(qid), other.get(qid))
    # one phrase each (grammar only)
    age = texts.get("Q1")
    texts["AGE"] = None if age is None else (age if "older" in age else f"{age} years old")
    place, country = texts.get("Q4"), texts.get("Q3")
    texts["RESIDENCE"] = ", ".join(t for t in (place, country) if t) or None
    return texts


# --- templates ---------------------------------------------------------------

def fill(text, texts):
    """Replace placeholders; None if any placeholder in text is missing."""
    if any(texts.get(k) is None for k in PLACEHOLDER_RE.findall(text)):
        return None
    return PLACEHOLDER_RE.sub(lambda m: texts[m.group(1)], text)


def render_line(line, texts):
    if not PLACEHOLDER_RE.search(line):
        return line
    kept_any = False

    def optional(m):
        nonlocal kept_any
        filled = fill(m.group(1), texts)
        if filled is None:
            return ""
        if PLACEHOLDER_RE.search(m.group(1)):
            kept_any = True
        return filled

    line = OPTIONAL_RE.sub(optional, line)
    if PLACEHOLDER_RE.search(line):
        line = fill(line, texts)
        if line is None:
            return None
        kept_any = True
    if not kept_any:
        return None
    line = re.sub(r" {2,}", " ", line).rstrip()
    line = re.sub(r"^(- )[;,.]\s*", r"\1", line)            # a line that starts with a dropped part
    line = re.sub(r"^(- )([a-z])", lambda m: m.group(1) + m.group(2).upper(), line)
    return line


def render_template(template, texts):
    lines = (render_line(line, texts) for line in template.splitlines())
    return "\n".join(line for line in lines if line is not None)


def build_personas(participant, codebook, study):
    texts = answer_texts(participant, codebook)
    v0 = read_prompt(study, "persona_v0.txt")
    v1 = render_template(read_prompt(study, "persona_v1.txt"), texts)
    experience = render_template(read_prompt(study, "persona_experience.txt"), texts)
    if experience.count("\n") < 1:  # only the heading left
        experience = ""
    blocks = {"V0": v0, "V1": v1, "EXPERIENCE": experience}

    def include(name):
        """A template made of {V0}/{V1}/{EXPERIENCE} lines: each replaced by its block (skipped if empty)."""
        out = []
        for line in read_prompt(study, name).splitlines():
            m = PLACEHOLDER_RE.fullmatch(line.strip())
            if m:
                if blocks[m.group(1)]:
                    out.append(blocks[m.group(1)])
            else:
                out.append(line)
        return "\n".join(out)

    return {"V0": v0, "V1": v1, "V2": include("persona_v2.txt"), "V3": include("persona_v3.txt")}


def label_leaks(participant, personas):
    """The person's own words (Q20, 'Other' texts) in V3 that name a diagnosis or condition."""
    words = {"Q20": participant["part_a"].get("Q20") or ""}
    words.update({q: t for q, t in participant.get("other_text", {}).items() if q.startswith("Q")})
    hits = []
    for qid, text in words.items():
        found = sorted({m.group(0).lower() for m in LEAK_RE.finditer(text)})
        if found and text in personas["V3"]:
            hits.append({"question": qid, "keywords": found})
    return hits


# --- task (Part B) -----------------------------------------------------------

TASK_QUESTIONS = ("B4", "B5", "B6")


def is_last(opt):
    """Options that stay at the end: exclusive ones ("none", "no_change_needed") and "other"."""
    return opt.get("exclusive") or opt["code"] == "other"


def option_order(codebook, rng=None):
    """{question: [codes]}; shuffled with rng (None = codebook order), 'last' options kept last."""
    order = {}
    for bid in TASK_QUESTIONS:
        opts = codebook["part_b"][bid]["options"]
        head = [o["code"] for o in opts if not is_last(o)]
        tail = [o["code"] for o in opts if is_last(o)]
        if rng is not None:
            rng.shuffle(head)
        order[bid] = head + tail
    return order


def render_task(codebook, order, version, study):
    blocks = {"B3": "\n".join(f"- {r['code']}: {r['en']}" for r in codebook["part_b"]["B3"]["rows"])}
    for bid in TASK_QUESTIONS:
        labels = {o["code"]: o["en"] for o in codebook["part_b"][bid]["options"]}
        blocks[bid] = "\n".join(f"- {code}: {labels[code]}" for code in order[bid])
    blocks["PLACE_NOTE"] = read_prompt(study, "task_place_note.txt") if version != "V0" else ""
    lines = []
    for line in read_prompt(study, "task_part_b.txt").splitlines():
        line = PLACEHOLDER_RE.sub(lambda m: blocks[m.group(1)], line)
        if line.strip() or not PLACEHOLDER_RE.search(line):
            lines.append(line)
    return "\n".join(line for line in lines if line != "")


# --- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_study_arg(ap)
    ap.add_argument("--show", metavar="PID", help="print this participant's persona texts")
    ap.add_argument("--task", action="store_true", help="print the task text (V1, one shuffled order)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    study = Study(args.study)
    if not study.participants.exists():
        sys.exit(f"Not found: {study.participants.relative_to(ROOT)}. Run src/parse_responses.py first.")
    codebook = study.codebook()
    with open(study.participants, encoding="utf-8") as f:
        participants = json.load(f)

    personas = {p["id"]: build_personas(p, codebook, study) for p in participants}
    leaks = {p["id"]: label_leaks(p, personas[p["id"]]) for p in participants}
    with open(study.personas, "w", encoding="utf-8") as f:
        json.dump(personas, f, ensure_ascii=False, indent=2)
    with open(study.clean / "label_leaks.json", "w", encoding="utf-8") as f:
        json.dump(leaks, f, ensure_ascii=False, indent=2)

    print(f"Personas written: {len(personas)} participants x {len(VERSIONS)} versions "
          f"-> {study.personas.relative_to(ROOT)}")
    for pid, versions in personas.items():
        lines = {v: t.count("\n") + 1 for v, t in versions.items()}
        print(f"  - {pid}: lines per version {lines}")
    flagged = {pid: h for pid, h in leaks.items() if h}
    print(f"V3 texts with a possible label leak (diagnosis keyword in the person's own words): {len(flagged)}"
          + "".join(f"\n  - {pid}: " + "; ".join(f"{h['question']} {h['keywords']}" for h in hs)
                    for pid, hs in flagged.items()))

    if args.show:
        if args.show not in personas:
            sys.exit(f"No participant {args.show}")
        print(f"\n=== system.txt ===\n{read_prompt(study, 'system.txt')}")
        for v in VERSIONS:
            print(f"\n=== {args.show} {v} ===\n{personas[args.show][v]}")
    if args.task:
        print(f"\n=== task, V1 (example shuffle, seed 0) ===\n"
              f"{render_task(codebook, option_order(codebook, random.Random(0)), 'V1', study)}")


if __name__ == "__main__":
    main()
