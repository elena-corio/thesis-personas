"""Step 1: Google Forms CSVs -> data/clean/participants.json (canonical codes).

Reads data/raw/responses_it.csv and, if present, data/raw/responses_en.csv (two
separate forms, same questions in the same order); the file sets each participant's
language, and options and grid rows are matched against the labels in that language.

    python src/parse_responses.py --mapping   # print the column mapping and stop
    python src/parse_responses.py             # write participants.json

Google Forms headers are the question texts, without numbers. Columns are mapped
by section order: consecutive columns that share the same question text form one
question (grid questions export one column per row, with the row label in square
brackets at the end). After the meta columns, the questions are Q1..Q22 (Part A),
then B1..B8 repeated once per place (Part B). Grid row labels are checked against
the codebook's Italian labels.

Answers are mapped to option codes through the codebook. Multi-select cells are
separated by ";" (sometimes ","), but some labels contain commas, so known labels
are matched first and whatever text is left is kept as `other`. Answers that match
no option are listed. Free text (Q22, "Other") is kept verbatim, never translated:
it goes to the persona as the person's own words.
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
# one export per Google Form; the file sets the participant's language
RAW_FILES = {
    "it": ROOT / "data" / "raw" / "responses_it.csv",
    "en": ROOT / "data" / "raw" / "responses_en.csv",  # optional until the first English answers
}
CODEBOOK = ROOT / "config" / "pilot1" / "codebook.yaml"
CLEAN_DIR = ROOT / "data" / "clean"
OUT_JSON = CLEAN_DIR / "participants.json"

N_PLACES = 4
ATTENTION_CHECK = "attention_check"

# columns before Part A that are not questions; identifying ones are never carried over
META = {
    "timestamp": "timestamp",
    "informazioni cronologiche": "timestamp",
    "consenso": "consent",
    "consent": "consent",
    "indirizzo email": "email (dropped)",
    "email": "email (dropped)",
}
GRID_ROW_RE = re.compile(r"\s*\[(?P<row>[^\[\]]+)\]\s*$")


def norm(text):
    text = re.sub(r"[‐-―−]", "-", text)  # en/em dashes, minus -> hyphen
    text = text.replace("’", "'")                  # curly apostrophe
    return re.sub(r"\s+", " ", text).strip().casefold()


def read_headers(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f))


def load_codebook(path=CODEBOOK):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def expected_questions(codebook):
    """Question ids in form order, with their codebook entries."""
    part_a = list(codebook["part_a"].items())
    part_b = list(codebook["part_b"].items())
    out = [(qid, q) for qid, q in part_a]
    for p in range(1, N_PLACES + 1):
        out += [(f"P{p}_{bid}", q) for bid, q in part_b]
    return out


def split_header(header):
    """'Question text [row]' -> ('Question text', 'row'); non-grid -> (text, None)."""
    m = GRID_ROW_RE.search(header)
    if m:
        return header[: m.start()].strip(), m.group("row").strip()
    return header.strip(), None


def row_code(question, row_label, lang):
    """Map a grid row label (in the form's language) to its codebook code, or None."""
    for row in question.get("rows", []):
        if norm(row.get(lang, "")) == norm(row_label):
            return row["code"]
    check = question.get("attention_check")
    if check and check.get(lang) and norm(check[lang]) == norm(row_label):
        return ATTENTION_CHECK
    return None


def map_columns(headers, codebook, lang):
    """Return (mapping, problems). One mapping entry per column."""
    mapping = []
    problems = []

    # meta columns: leading columns whose header is a known non-question
    start = 0
    while start < len(headers) and norm(headers[start]) in META:
        mapping.append({"index": start, "id": None, "row": None, "header": headers[start],
                        "note": META[norm(headers[start])]})
        start += 1

    # group consecutive columns with the same question text
    groups = []
    for i in range(start, len(headers)):
        text, row = split_header(headers[i])
        if groups and norm(groups[-1]["text"]) == norm(text) and row is not None and groups[-1]["rows"]:
            groups[-1]["cols"].append((i, row))
        else:
            groups.append({"text": text, "cols": [(i, row)], "rows": row is not None})

    expected = expected_questions(codebook)
    if len(groups) != len(expected):
        problems.append(f"found {len(groups)} questions in the CSV, codebook expects {len(expected)}")

    for g, (qid, q) in zip(groups, expected):
        is_grid = q["type"].startswith("grid")
        if is_grid != g["rows"]:
            problems.append(f"{qid}: codebook type {q['type']} but CSV columns "
                            f"{'have' if g['rows'] else 'have no'} grid rows")
        for i, row in g["cols"]:
            entry = {"index": i, "id": qid, "row": None, "header": headers[i], "note": ""}
            if row is not None:
                code = row_code(q, row, lang)
                entry["row"] = code or row
                if code is None:
                    entry["note"] = "row not in codebook"
                    problems.append(f"col {i} {qid}: grid row not in codebook: {row!r}")
            mapping.append(entry)
        if is_grid:
            found = {row_code(q, row, lang) for _, row in g["cols"]}
            for r in q.get("rows", []):
                if r["code"] not in found:
                    problems.append(f"{qid}: grid row missing in CSV: {r['code']}")
            if "attention_check" in q and ATTENTION_CHECK not in found:
                problems.append(f"{qid}: attention-check row missing in CSV")

    # leftover columns, if the CSV has more questions than the codebook
    for g in groups[len(expected):]:
        for i, _ in g["cols"]:
            mapping.append({"index": i, "id": None, "row": None, "header": headers[i],
                            "note": "extra column: not mapped"})

    mapping.sort(key=lambda e: e["index"])
    return mapping, problems


def print_mapping(mapping, width=60):
    print(f"{'col':>3}  {'id':<7}  {'grid row':<24}  header")
    print("-" * (3 + 2 + 7 + 2 + 24 + 2 + width))
    for e in mapping:
        header = e["header"].strip()
        if len(header) > width:
            header = header[: width - 1] + "…"
        row = e["row"] or ("" if e["id"] else e["note"])
        if len(row) > 24:
            row = row[:23] + "…"
        print(f"{e['index']:>3}  {e['id'] or '-':<7}  {row:<24}  {header}")


# --- answers -----------------------------------------------------------------

# Consent is a checkbox with two statements; both are required (participation, health data).
CONSENT_REQUIRED = {
    "it": ("accetto di partecipare", "acconsento all'uso"),
    "en": ("agree to take part", "consent to the use"),  # TODO: check against the English form's wording
}


def has_consent(cell, lang):
    return all(phrase in norm(cell) for phrase in CONSENT_REQUIRED[lang])


# Answers that the form allows but the codebook has no option for, mapped by decision.
# (question id, answer as written) -> option code
ANSWER_OVERRIDES = {
    ("Q3", "italia"): "another_european_country",
    ("Q3", "italy"): "another_european_country",
}

# Google Forms timestamp, e.g. "2026/10/13 9:51:11 PM GMT+3"
TIMESTAMP_RE = re.compile(r"^(?P<dt>.+?)\s*GMT(?P<off>[+-]\d{1,2})?(?::?(?P<offmin>\d{2}))?\s*$")


def parse_timestamp(text):
    """-> aware datetime, or None if the format is unknown."""
    m = TIMESTAMP_RE.match(text.strip())
    dt_text = m.group("dt") if m else text.strip()
    for fmt in ("%Y/%m/%d %I:%M:%S %p", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(dt_text, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    hours = int(m.group("off")) if m and m.group("off") else 0
    minutes = int(m.group("offmin")) if m and m.group("offmin") else 0
    offset = timedelta(hours=hours, minutes=minutes if hours >= 0 else -minutes)
    return dt.replace(tzinfo=timezone(offset))


def find_option(question, text, lang):
    for opt in question.get("options", []):
        if norm(opt.get(lang, "")) == norm(text):
            return opt["code"]
    return None


def parse_single(question, cell, lang):
    """-> (code or None, leftover text or None)."""
    if not cell.strip():
        return None, None
    code = find_option(question, cell, lang)
    return (code, None) if code else ("other", cell.strip())


def parse_multi(question, cell, lang):
    """-> (list of codes, leftover text or None). Longest labels are matched first.

    The export separates choices with ";" (sometimes ", "); labels themselves may contain commas.
    """
    if not cell.strip():
        return [], None
    cell = re.sub(r"\s*;\s*", ", ", cell.strip())
    rest = ", " + norm(cell) + ", "
    found = []
    options = sorted(question.get("options", []), key=lambda o: len(o.get(lang, "")), reverse=True)
    for opt in options:
        label = ", " + norm(opt.get(lang, "")) + ", "
        if label in rest:
            found.append(opt["code"])
            rest = rest.replace(label, ", ", 1)
    order = [o["code"] for o in question["options"]]
    codes = sorted(found, key=order.index)
    leftover = rest.strip(" ,")
    if leftover:
        # recover the original casing from the cell
        idx = norm(cell).find(leftover)
        leftover = cell.strip()[idx: idx + len(leftover)] if idx >= 0 else leftover
        codes.append("other")
    return codes, leftover or None


def parse_int(cell):
    cell = cell.strip()
    if not cell:
        return None
    try:
        return int(cell)
    except ValueError:
        return cell  # reported as unmatched by the caller


def parse_row(row, mapping, codebook, lang, unmatched, remapped, pid):
    """Parse one CSV row into {question id: value}, plus free texts and the attention check."""
    answers = {}
    free_text = {}
    check = None
    questions = dict(expected_questions(codebook))

    cols = {}
    for e in mapping:
        if e["id"]:
            cols.setdefault(e["id"], []).append(e)

    for qid, entries in cols.items():
        q = questions[qid]
        qtype = q["type"]
        if qtype == "grid_1_5":
            grid = {}
            for e in entries:
                value = parse_int(row[e["index"]])
                if e["row"] == ATTENTION_CHECK:
                    check = value
                    continue
                if value is not None and not (isinstance(value, int) and 1 <= value <= 5):
                    unmatched.append((pid, f"{qid}[{e['row']}]", str(value), "not an integer 1-5"))
                grid[e["row"]] = value
            answers[qid] = grid
            continue

        cell = row[entries[0]["index"]]
        if qtype == "scale_1_5":
            value = parse_int(cell)
            if value is not None and not (isinstance(value, int) and 1 <= value <= 5):
                unmatched.append((pid, qid, str(value), "not an integer 1-5"))
            answers[qid] = value
        elif qtype == "open_text":
            answers[qid] = cell.strip() or None
        elif qtype in ("single", "multi"):
            override = ANSWER_OVERRIDES.get((qid, norm(cell)))
            if override:
                value, leftover = override, None
                remapped.append((pid, qid, cell.strip(), override))
            elif qtype == "single":
                value, leftover = parse_single(q, cell, lang)
            else:
                value, leftover = parse_multi(q, cell, lang)
            answers[qid] = value
            if leftover:
                has_other = any(o["code"] == "other" for o in q.get("options", []))
                free_text[qid] = leftover
                unmatched.append((pid, qid, leftover,
                                  "'Other' text" if has_other else "no matching option (question has no 'Other')"))
        else:
            raise ValueError(f"{qid}: unknown type {qtype}")
    return answers, free_text, check


def build_participants(sources, codebook):
    """sources: {lang: (path, mapping)}. Participants are numbered across forms in submission order."""
    submissions, excluded, unordered = [], [], []
    for lang, (path, mapping) in sources.items():
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))[1:]
        consent_col = next((e["index"] for e in mapping if e["note"] == "consent"), None)
        time_col = next((e["index"] for e in mapping if e["note"] == "timestamp"), None)
        if consent_col is None:
            sys.exit(f"{path.name}: no consent column found")
        for r, row in enumerate(rows, start=2):
            if not has_consent(row[consent_col], lang):
                excluded.append((path.name, r, row[consent_col]))
                continue
            ts = parse_timestamp(row[time_col]) if time_col is not None else None
            if ts is None:
                unordered.append((path.name, r))
            submissions.append((ts, lang, mapping, row))

    if unordered:
        sys.exit("Timestamps not understood, cannot number participants in submission order: "
                 + ", ".join(f"{name} row {r}" for name, r in unordered))
    submissions.sort(key=lambda s: s[0])

    participants, unmatched, remapped = [], [], []
    for n, (_, lang, mapping, row) in enumerate(submissions, start=1):
        pid = f"P{n:02d}"
        answers, free_text, check = parse_row(row, mapping, codebook, lang, unmatched, remapped, pid)

        part_a = {qid: v for qid, v in answers.items() if qid.startswith("Q")}
        part_b = {}
        for qid, v in answers.items():
            if not qid.startswith("Q"):
                place, bid = qid.split("_")
                part_b.setdefault(place, {})[bid] = v

        participants.append({
            "id": pid,
            "lang": lang,  # language of the form, and of the open answers
            "consent": True,
            "attention_check": {"value": check, "pass": check == 2},
            "part_a": part_a,
            "part_b": part_b,
            "other_text": free_text,  # verbatim, keyed by question id
        })

    return participants, excluded, unmatched, remapped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", action="store_true", help="print the column mapping and stop")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    codebook = load_codebook()

    sources = {}
    failed = False
    for lang, path in RAW_FILES.items():
        if not path.exists():
            print(f"[{lang}] {path.relative_to(ROOT)}: not found, skipped")
            continue
        mapping, problems = map_columns(read_headers(path), codebook, lang)
        if args.mapping:
            print(f"\n[{lang}] {path.relative_to(ROOT)}")
            print_mapping(mapping)
            print()
        if problems:
            failed = True
            print(f"[{lang}] Mapping problems:")
            for p in problems:
                print(f"  - {p}")
        elif args.mapping:
            print(f"[{lang}] All columns mapped: Q1-Q22 and P1-P{N_PLACES} x B1-B8; "
                  "all grid rows match the codebook.")
        sources[lang] = (path, mapping)
    if not sources:
        sys.exit("No export found. Put the Google Forms export(s) in data/raw/ first.")
    if failed:
        sys.exit(1)
    if args.mapping:
        return

    participants, excluded, unmatched, remapped = build_participants(sources, codebook)

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(participants, f, ensure_ascii=False, indent=2)

    by_lang = {lang: sum(p["lang"] == lang for p in participants) for lang in sources}
    print(f"Participants written: {len(participants)} {by_lang} -> {OUT_JSON.relative_to(ROOT)}")
    print(f"Excluded (consent missing or incomplete): {len(excluded)}"
          + "".join(f"\n  - {name} row {r}: {v!r}" for name, r, v in excluded))
    print("Attention check (Q16 'seleziona 2'):")
    for p in participants:
        c = p["attention_check"]
        print(f"  - {p['id']}: value {c['value']} -> {'pass' if c['pass'] else 'FAIL'}")
    print(f"Answers remapped by decision (ANSWER_OVERRIDES): {len(remapped)}")
    for pid, qid, text, code in remapped:
        print(f"  - {pid} {qid}: {text!r} -> {code}")
    print(f"Answers not matching an option (kept as 'other'): {len(unmatched)}")
    for pid, qid, text, why in unmatched:
        print(f"  - {pid} {qid}: {text!r} ({why})")


if __name__ == "__main__":
    main()
