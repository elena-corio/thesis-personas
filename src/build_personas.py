"""Step 2: data/clean/participants.json -> data/clean/personas.json (persona texts per version).

    python src/build_personas.py              # write personas.json, print a summary
    python src/build_personas.py --show P01   # also print that participant's texts

Persona texts are short second-person profiles in English, rendered from the
codebook's English labels; open answers (Q22, "Other" texts) are inserted verbatim,
in the language they were written in.

V0 = prompts/persona_v0.txt                              no persona
V1 = prompts/persona_v1.txt                              labels (demographics, health, neurodivergence)
V2 = prompts/persona_v1.txt + prompts/persona_v2.txt     labels + lived experience
V3 = prompts/persona_v0.txt + prompts/persona_v2.txt     lived experience only, no labels

Template notation:
    {Q7}        an answer; {RESIDENCE} combines Q3 and Q4 into one phrase
    [ ... ]     an optional part, dropped when an answer inside it is missing
A line is dropped when an answer outside [ ] is missing, or when nothing but
fixed text would remain. "Missing" means not asked (branching), empty, or
"Prefer not to say". An exclusive "None" answer is written as "none".
Exception: questions in BLANK_FILL (neurodivergence details Q9-Q11, Q13) are
written as "none" / "not applicable" when blank, instead of being dropped.

The task text (Part B) is rendered by render_task() with an option order, so that
run_personas.py can shuffle the options in every run and save the order used.
"""

import argparse
import json
import random
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CODEBOOK = ROOT / "config" / "pilot1" / "codebook.yaml"
PROMPTS = ROOT / "prompts"
PARTICIPANTS = ROOT / "data" / "clean" / "participants.json"
OUT_JSON = ROOT / "data" / "clean" / "personas.json"

VERSIONS = ("V0", "V1", "V2", "V3")
MISSING_CODES = {"prefer_not_to_say"}
# Questions that can be blank because the form skipped them (branching) or the person
# left them empty: written with this text instead of dropping the line. "Prefer not to
# say" is different (the person chose not to share) and is still left out.
BLANK_FILL = {
    "Q9": "none",              # asked only if Q8 = Yes or Not sure
    "Q10": "not applicable",   # follows Q9
    "Q11": "none",
    "Q13": "none",             # "if it happens to you"
}
# labels that keep their capital letter inside a sentence
PROPER_NOUNS = ("Austria", "Tourette's")

# Q3 + Q4 as one phrase after "You live in" (grammar only; labels from the codebook)
COUNTRY = {
    "austria": "Austria",
    "another_european_country": "another European country",
    "outside_europe": "a country outside Europe",
}
PLACE_TYPE = {
    "large_city": "a large city (over 500,000 inhabitants)",
    "medium_sized_city": "a medium-sized city (50,000–500,000)",
    "small_town": "a small town (5,000–50,000)",
    "village_or_countryside": "a village or the countryside (under 5,000)",
}
MOVING = "i_move_around_a"

PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
OPTIONAL_RE = re.compile(r"\[([^\[\]]*)\]")


def load_codebook():
    with open(CODEBOOK, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_prompt(name):
    return (PROMPTS / name).read_text(encoding="utf-8").strip()


# --- answers -> English text -------------------------------------------------

def inline(label):
    """Lower-case a label's first letter for use inside a sentence (not acronyms or names)."""
    first = label.split()[0]
    if label.startswith(PROPER_NOUNS) or first == "I" or (len(first) > 1 and first[:2].isupper()):
        return label
    return label[0].lower() + label[1:]


NONE_CODES = {"none", "none_of_these"}  # written as "none"


def option_label(question, code, other_text):
    if code == "other":
        return f'"{other_text}"' if other_text else None
    if code in NONE_CODES:
        return "none"
    for opt in question.get("options", []):
        if opt["code"] == code:
            return inline(opt["en"])
    raise KeyError(f"code {code!r} not in codebook options")


def render_answer(qid, question, value, other_text):
    """-> English text, or None when the answer is missing."""
    qtype = question["type"]
    if value is None or value == [] or value == {}:
        return None
    if qtype in ("scale_1_5",):
        return str(value)
    if qtype == "grid_1_5":
        labels = {r["code"]: inline(r["en"]) for r in question["rows"]}
        parts = [f"{labels[code]}: {v}" for code, v in value.items() if v is not None]
        return "; ".join(parts) or None
    if qtype == "open_text":
        return value
    if qtype == "single":
        if value in MISSING_CODES:
            return None
        return option_label(question, value, other_text)
    if qtype == "multi":
        codes = [c for c in value if c not in MISSING_CODES]
        if not codes:
            return None
        parts = [option_label(question, c, other_text) for c in codes]
        # labels may contain commas, so items are separated by semicolons
        return "; ".join(p for p in parts if p) or None
    raise ValueError(f"{qid}: unknown type {qtype}")


def residence(answers):
    country = COUNTRY.get(answers.get("Q3"))
    place = answers.get("Q4")
    if place == MOVING:
        return f"{country} and move around a lot, with no fixed place" if country else None
    place_text = PLACE_TYPE.get(place)
    if country and place_text:
        return f"{country}, in {place_text}"
    return country or place_text


def answer_texts(participant, codebook):
    """{placeholder: English text or None} for every Part A question given to a persona."""
    answers = participant["part_a"]
    other = participant.get("other_text", {})
    texts = {}
    for qid, q in codebook["part_a"].items():
        if q.get("persona") not in ("V1", "V2"):
            continue  # Q6 and anything analysis-only never reach a persona
        value = answers.get(qid)
        if qid in BLANK_FILL and value in (None, [], {}):
            texts[qid] = BLANK_FILL[qid]
        else:
            texts[qid] = render_answer(qid, q, value, other.get(qid))
    texts["RESIDENCE"] = residence(answers)
    return texts


# --- templates ---------------------------------------------------------------

def fill(text, texts):
    """Replace placeholders; None if any placeholder in text is missing."""
    missing = [k for k in PLACEHOLDER_RE.findall(text) if texts.get(k) is None]
    if missing:
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
    return re.sub(r" {2,}", " ", line).rstrip()


def render_template(template, texts):
    lines = (render_line(line, texts) for line in template.splitlines())
    return "\n".join(line for line in lines if line is not None)


def build_personas(participant, codebook):
    texts = answer_texts(participant, codebook)
    v0 = read_prompt("persona_v0.txt")
    v1 = render_template(read_prompt("persona_v1.txt"), texts)
    experience = render_template(read_prompt("persona_v2.txt"), texts)
    has_experience = experience.count("\n") >= 1  # more than the block's heading
    return {
        "V0": v0,
        "V1": v1,
        "V2": v1 + "\n" + experience if has_experience else v1,
        "V3": v0 + "\n" + experience if has_experience else v0,
    }


# --- task (Part B) -----------------------------------------------------------

TASK_QUESTIONS = ("B4", "B5", "B6", "B7")


def is_last(opt):
    """Options that stay at the end: exclusive ones ("None…", "No change needed") and "Other"."""
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


def render_task(codebook, order):
    b3 = codebook["part_b"]["B3"]["rows"]
    blocks = {"B3": "\n".join(f"- {r['code']}: {r['en']}" for r in b3)}
    for bid in TASK_QUESTIONS:
        labels = {o["code"]: o["en"] for o in codebook["part_b"][bid]["options"]}
        blocks[bid] = "\n".join(f"- {code}: {labels[code]}" for code in order[bid])
    return PLACEHOLDER_RE.sub(lambda m: blocks[m.group(1)], read_prompt("task_part_b.txt"))


# --- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", metavar="PID", help="print this participant's persona texts")
    ap.add_argument("--task", action="store_true", help="print the task text with one shuffled order")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    if not PARTICIPANTS.exists():
        sys.exit(f"Not found: {PARTICIPANTS.relative_to(ROOT)}. Run src/parse_responses.py first.")
    codebook = load_codebook()
    with open(PARTICIPANTS, encoding="utf-8") as f:
        participants = json.load(f)

    personas = {p["id"]: build_personas(p, codebook) for p in participants}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(personas, f, ensure_ascii=False, indent=2)

    print(f"Personas written: {len(personas)} participants x {len(VERSIONS)} versions "
          f"-> {OUT_JSON.relative_to(ROOT)}")
    for pid, versions in personas.items():
        lines = {v: t.count("\n") + 1 for v, t in versions.items()}
        print(f"  - {pid}: lines per version {lines}")

    if args.show:
        if args.show not in personas:
            sys.exit(f"No participant {args.show}")
        print(f"\n=== system.txt ===\n{read_prompt('system.txt')}")
        for v in VERSIONS:
            print(f"\n=== {args.show} {v} ===\n{personas[args.show][v]}")
    if args.task:
        print(f"\n=== task (example shuffle, seed 0) ===\n{render_task(codebook, option_order(codebook, random.Random(0)))}")


if __name__ == "__main__":
    main()
