"""Step 1: Google Forms CSVs -> data/clean/<study>/participants.json (canonical codes).

    python src/parse_responses.py --mapping   # print the column mapping and stop
    python src/parse_responses.py             # write participants.json
    (--study pilot2 is the default)

Reads data/raw/<study>/responses_it.csv and/or responses_en.csv: two forms with the
same structure; the file sets each participant's language, and titles, options and grid
rows are matched against the codebook texts in that language (title_it/title_en, it/en),
which are the exact form texts.

Columns are mapped by section order and checked by title: consecutive columns that share
the same question text form one question (grid questions export one column per row,
with the row label in [...]); after the meta columns (timestamp, consent) they are Part A
in codebook order, then Part B once per place (P1_B1 ... P6_B7). Every group's text must
equal the codebook title in the form's language.

Answers -> option codes. Multi-select cells are separated by ";" (labels may contain
commas, so whole labels are matched). In a question with an "other" option, any
unmatched text is Google's "Other" box: coded `other` + the text. Unmatched answers
elsewhere are listed. Decided remappings live in the codebook's `remap:` section
({question id: {answer as written: code}}), never in this code. Free text (Q20, B7, every
"Other") is kept verbatim, never translated. Participants are kept only if every consent
statement is ticked; the attention-check row (last row of the Q14 grid) is recorded as
pass/fail and dropped.
"""

import argparse
import csv
import difflib
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from study import LANGS, ROOT, Study, add_study_arg

ATTENTION_CHECK = "attention_check"
TIMESTAMP_HEADERS = {"timestamp", "informazioni cronologiche", "marca temporale"}
GRID_ROW_RE = re.compile(r"\s*\[(?P<row>[^\[\]]+)\]\s*$")


def norm(text):
    text = re.sub(r"[‐-―−]", "-", text)  # en/em dashes, minus -> hyphen
    text = text.replace("’", "'")                  # curly apostrophe
    return re.sub(r"\s+", " ", text).strip().casefold()


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def split_header(header):
    """'Question text [row]' -> ('Question text', 'row'); non-grid -> (text, None)."""
    m = GRID_ROW_RE.search(header)
    if m:
        return header[: m.start()].strip(), m.group("row").strip()
    return header.strip(), None


def expected_questions(codebook):
    """(question id, base id, entry) in form order: Part A, then Part B once per place."""
    out = [(qid, qid, q) for qid, q in codebook["part_a"].items()]
    for p in range(1, codebook["places"] + 1):
        out += [(f"P{p}_{bid}", bid, q) for bid, q in codebook["part_b"].items()]
    return out


def row_code(question, row_label, lang):
    for row in question.get("rows", []):
        if norm(row[lang]) == norm(row_label):
            return row["code"]
    check = question.get("attention_check")
    if check and norm(check.get(lang, "")) == norm(row_label):
        return ATTENTION_CHECK
    return None


# --- columns -----------------------------------------------------------------

def map_columns(headers, codebook, lang):
    """-> (mapping, problems). One entry per column: index, id, base, row, header, note."""
    mapping, problems = [], []
    consent_title = norm(codebook["consent"][f"title_{lang}"])

    start = 0
    while start < len(headers):
        h = norm(headers[start])
        if h in TIMESTAMP_HEADERS:
            note = "timestamp"
        elif h == consent_title:
            note = "consent"
        else:
            break
        mapping.append({"index": start, "id": None, "base": None, "row": None,
                        "header": headers[start], "note": note, "title_ok": True})
        start += 1
    if not any(e["note"] == "consent" for e in mapping):
        problems.append(f"no consent column titled {codebook['consent'][f'title_{lang}']!r} before the questions")

    groups = []
    for i in range(start, len(headers)):
        text, row = split_header(headers[i])
        if groups and row is not None and groups[-1]["rows"] and norm(groups[-1]["text"]) == norm(text):
            groups[-1]["cols"].append((i, row))
        else:
            groups.append({"text": text, "cols": [(i, row)], "rows": row is not None})

    expected = expected_questions(codebook)
    if len(groups) != len(expected):
        problems.append(f"found {len(groups)} questions in the CSV, codebook expects {len(expected)}")

    for g, (qid, base, q) in zip(groups, expected):
        title = q.get(f"title_{lang}", "")
        title_ok = norm(g["text"]) == norm(title)
        if not title_ok:
            problems.append(f"{qid}: header {g['text'][:60]!r} is not the codebook title {title[:60]!r}")
        is_grid = q["type"].startswith("grid")
        if is_grid != g["rows"]:
            problems.append(f"{qid}: codebook type {q['type']} but the CSV columns "
                            f"{'have' if g['rows'] else 'have no'} grid rows")
        for i, row in g["cols"]:
            entry = {"index": i, "id": qid, "base": base, "row": None, "header": headers[i],
                     "note": "", "title_ok": title_ok}
            if row is not None:
                code = row_code(q, row, lang)
                entry["row"] = code or row
                if code is None:
                    entry["note"] = "row not in codebook"
                    problems.append(f"col {i} {qid}: grid row not in codebook: {row!r}")
            mapping.append(entry)
        if is_grid:
            found = {row_code(q, row, lang) for _, row in g["cols"]}
            problems += [f"{qid}: grid row missing in CSV: {r['code']}" for r in q["rows"] if r["code"] not in found]
            if "attention_check" in q and ATTENTION_CHECK not in found:
                problems.append(f"{qid}: attention-check row missing in CSV")

    for g in groups[len(expected):]:
        for i, _ in g["cols"]:
            mapping.append({"index": i, "id": None, "base": None, "row": None, "header": headers[i],
                            "note": "extra column: not mapped", "title_ok": False})
    mapping.sort(key=lambda e: e["index"])
    return mapping, problems


def print_mapping(mapping, width=58):
    print(f"{'col':>3}  {'id':<7}  {'grid row':<22}  {'title':<5}  header")
    print("-" * (3 + 2 + 7 + 2 + 22 + 2 + 5 + 2 + width))
    for e in mapping:
        header = e["header"].strip()
        header = header if len(header) <= width else header[: width - 1] + "…"
        row = e["row"] or ("" if e["id"] else e["note"])
        row = row if len(row) <= 22 else row[:21] + "…"
        ok = "ok" if e["title_ok"] else "NO"
        print(f"{e['index']:>3}  {e['id'] or '-':<7}  {row:<22}  {ok:<5}  {header}")


# --- answers -----------------------------------------------------------------

def find_option(question, text, lang):
    for opt in question.get("options", []):
        if norm(opt[lang]) == norm(text):
            return opt["code"]
    return None


def as_items(text):
    """The export separates choices with ";", and a label may itself contain ";" or ",":
    both cell and labels are compared with every ";" written as ", "."""
    return re.sub(r"\s*;\s*", ", ", text.strip())


def match_labels(question, cell, lang):
    """Multi-select: -> (codes in codebook order, leftover text or None). Longest labels first."""
    if not cell.strip():
        return [], None
    cell = as_items(cell)
    rest = ", " + norm(cell) + ", "
    found = []
    for opt in sorted(question.get("options", []), key=lambda o: len(o[lang]), reverse=True):
        if opt["code"] == "other":
            continue  # "Other" arrives as the typed text, never as the label
        label = ", " + norm(as_items(opt[lang])) + ", "
        if label in rest:
            found.append(opt["code"])
            rest = rest.replace(label, ", ", 1)
    order = [o["code"] for o in question["options"]]
    codes = sorted(found, key=order.index)
    leftover = rest.strip(" ,")
    if leftover:
        idx = norm(cell).find(leftover)
        leftover = cell.strip()[idx: idx + len(leftover)] if idx >= 0 else leftover
    return codes, leftover or None


def has_other(question):
    return any(o["code"] == "other" for o in question.get("options", []))


def near_option(question, text, lang, cutoff=0.8):
    """An option whose label is almost the given text: a form/codebook wording mismatch, not a real 'Other'."""
    labels = {norm(o[lang]): o["code"] for o in question.get("options", []) if o["code"] != "other"}
    close = difflib.get_close_matches(norm(text), labels, n=1, cutoff=cutoff)
    return (labels[close[0]], close[0]) if close else None


def parse_int(cell):
    cell = cell.strip()
    if not cell:
        return None
    try:
        return int(cell)
    except ValueError:
        return cell


def parse_row(row, mapping, codebook, lang, pid, log):
    """One CSV row -> (answers {qid: value}, other texts {qid: text}, attention-check value)."""
    questions = {qid: (base, q) for qid, base, q in expected_questions(codebook)}
    remap = codebook.get("remap") or {}
    answers, other_text, check = {}, {}, None

    cols = {}
    for e in mapping:
        if e["id"]:
            cols.setdefault(e["id"], []).append(e)

    for qid, entries in cols.items():
        base, q = questions[qid]
        qtype = q["type"]
        if qtype == "grid_1_5":
            grid = {}
            for e in entries:
                value = parse_int(row[e["index"]])
                if e["row"] == ATTENTION_CHECK:
                    check = value
                    continue
                if value is not None and not (isinstance(value, int) and 1 <= value <= 5):
                    log["unmatched"].append((pid, f"{qid}[{e['row']}]", str(value), "not an integer 1-5"))
                grid[e["row"]] = value
            answers[qid] = grid
            continue

        cell = row[entries[0]["index"]]
        if qtype == "scale_1_5":
            value = parse_int(cell)
            if value is not None and not (isinstance(value, int) and 1 <= value <= 5):
                log["unmatched"].append((pid, qid, str(value), "not an integer 1-5"))
            answers[qid] = value
        elif qtype == "open_text":
            answers[qid] = cell.strip() or None
        elif qtype in ("single", "multi"):
            remapped = {norm(k): v for k, v in (remap.get(base) or {}).items()}
            if qtype == "single":
                text = cell.strip()
                code = None
                if text:
                    code = find_option(q, text, lang) or remapped.get(norm(text))
                    if code and norm(text) in remapped:
                        log["remapped"].append((pid, qid, text, code))
                leftover = text if text and not code else None
                value = code
            else:
                value, leftover = match_labels(q, cell, lang)
                if leftover and norm(leftover) in remapped:
                    code = remapped[norm(leftover)]
                    log["remapped"].append((pid, qid, leftover, code))
                    value = sorted(set(value) | {code}, key=[o["code"] for o in q["options"]].index)
                    leftover = None
            if leftover:
                other_text[qid] = leftover
                near = near_option(q, leftover, lang)
                if near:
                    log["near"].append((pid, qid, leftover, near[0], near[1]))
                if has_other(q):
                    log["other"].append((pid, qid, leftover))
                else:
                    log["unmatched"].append((pid, qid, leftover, "no matching option (question has no 'Other')"))
                value = "other" if qtype == "single" else value + ["other"]
            answers[qid] = value
        else:
            raise ValueError(f"{qid}: unknown type {qtype}")
    return answers, other_text, check


# --- participants --------------------------------------------------------------

TIMESTAMP_RE = re.compile(r"^(?P<dt>.+?)\s*GMT(?P<off>[+-]\d{1,2})?(?::?(?P<offmin>\d{2}))?\s*$")


def parse_timestamp(text):
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
    return dt.replace(tzinfo=timezone(timedelta(hours=hours, minutes=minutes if hours >= 0 else -minutes)))


def consent_given(cell, codebook, lang):
    codes, _ = match_labels(codebook["consent"], cell, lang)
    required = {o["code"] for o in codebook["consent"]["options"]}
    return required <= set(codes)


def build_participants(sources, codebook):
    """sources: {lang: (path, rows, mapping)}. Numbered across forms in submission order."""
    submissions, excluded, unordered = [], [], []
    for lang, (path, rows, mapping) in sources.items():
        consent_col = next(e["index"] for e in mapping if e["note"] == "consent")
        time_col = next((e["index"] for e in mapping if e["note"] == "timestamp"), None)
        for r, row in enumerate(rows[1:], start=2):
            if not consent_given(row[consent_col], codebook, lang):
                excluded.append((path.name, r))
                continue
            ts = parse_timestamp(row[time_col]) if time_col is not None else None
            if ts is None:
                unordered.append((path.name, r))
            submissions.append((ts, lang, mapping, row))
    if unordered:
        sys.exit("Timestamps not understood, cannot number participants in submission order: "
                 + ", ".join(f"{n} row {r}" for n, r in unordered))
    submissions.sort(key=lambda s: s[0])

    expected_check = codebook["part_a"]["Q14"]["attention_check"]["expected"]
    log = {"unmatched": [], "other": [], "remapped": [], "near": []}
    participants = []
    for n, (_, lang, mapping, row) in enumerate(submissions, start=1):
        pid = f"P{n:02d}"
        answers, other_text, check = parse_row(row, mapping, codebook, lang, pid, log)
        part_a = {q: v for q, v in answers.items() if q.startswith("Q")}
        part_b = {}
        for qid, v in answers.items():
            if not qid.startswith("Q"):
                place, bid = qid.split("_")
                part_b.setdefault(place, {})[bid] = v
        participants.append({
            "id": pid,
            "lang": lang,  # form language, and the language of every open answer
            "consent": True,
            "attention_check": {"value": check, "expected": expected_check, "pass": check == expected_check},
            "part_a": part_a,
            "part_b": part_b,
            "other_text": other_text,  # verbatim, keyed by question id (Part B: P1_B4 ...)
        })
    return participants, excluded, log


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_study_arg(ap)
    ap.add_argument("--mapping", action="store_true", help="print the column mapping and stop")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    study = Study(args.study)
    codebook = study.codebook()
    print(f"Study {study.name} | codebook {study.codebook_path.relative_to(ROOT)} (version {codebook.get('version')})")

    sources, failed = {}, False
    for lang in LANGS:
        path = study.raw_file(lang)
        if not path.exists():
            print(f"[{lang}] {path.relative_to(ROOT)}: not found, skipped")
            continue
        rows = read_csv(path)
        mapping, problems = map_columns(rows[0], codebook, lang)
        if args.mapping:
            print(f"\n[{lang}] {path.relative_to(ROOT)}: {len(rows) - 1} responses, {len(rows[0])} columns")
            print_mapping(mapping)
            print()
        if problems:
            failed = True
            print(f"[{lang}] Mapping problems:")
            for p in problems:
                print(f"  - {p}")
        elif args.mapping:
            print(f"[{lang}] All columns mapped and every title matches the codebook: Part A Q1-Q20, "
                  f"Part B P1-P{codebook['places']} x B1-B7; grid rows (incl. attention check) found.")
        sources[lang] = (path, rows, mapping)
    if not sources:
        sys.exit(f"No export found in {study.raw.relative_to(ROOT)}.")
    if failed:
        sys.exit(1)
    if args.mapping:
        return

    participants, excluded, log = build_participants(sources, codebook)
    study.clean.mkdir(parents=True, exist_ok=True)
    with open(study.participants, "w", encoding="utf-8") as f:
        json.dump(participants, f, ensure_ascii=False, indent=2)

    by_lang = {lang: sum(p["lang"] == lang for p in participants) for lang in sources}
    print(f"Participants written: {len(participants)} {by_lang} -> {study.participants.relative_to(ROOT)}")
    print(f"Excluded (consent missing or incomplete): {len(excluded)}"
          + "".join(f"\n  - {name} row {r}" for name, r in excluded))
    print("Attention check (last row of Q14):")
    for p in participants:
        c = p["attention_check"]
        print(f"  - {p['id']} ({p['lang']}): value {c['value']} -> {'pass' if c['pass'] else 'FAIL'}")
    print(f"Remapped by the codebook's remap section: {len(log['remapped'])}"
          + "".join(f"\n  - {pid} {q}: {t!r} -> {c}" for pid, q, t, c in log["remapped"]))
    print(f"'Other' texts (coded other + text, kept verbatim): {len(log['other'])}"
          + "".join(f"\n  - {pid} {q}: {t!r}" for pid, q, t in log["other"]))
    print(f"Answers not matching an option: {len(log['unmatched'])}"
          + "".join(f"\n  - {pid} {q}: {t!r} ({why})" for pid, q, t, why in log["unmatched"]))
    if log["near"]:
        print(f"⚠ Probably a form/codebook wording mismatch, not a real 'Other' ({len(log['near'])}): "
              "fix the codebook label to the exact form text, or add a remap")
        for pid, q, t, code, label in log["near"]:
            print(f"  - {pid} {q}: {t!r} is close to option {code} {label!r}")


if __name__ == "__main__":
    main()
