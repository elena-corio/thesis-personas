"""Step 3: run the personas on Part B through OpenRouter.

    python src/run_personas.py --dry-run                 # build prompts, count calls, estimate cost; no API call
    python src/run_personas.py                           # new run -> runs/<run_id>/calls.jsonl
    python src/run_personas.py --resume runs/<run_id>    # add the calls still missing in that run
    python src/run_personas.py --resume runs/<run_id> --versions V3   # add a version to an existing run
    (--study pilot2 is the default; config, prompts, photos and data come from the study folders)

One call per participant x version x place x run, each a fresh conversation:
system prompt, then a user message with the persona text, the task text and the
photo. Calls are ordered run by run, so a run stopped early still has every
condition equally covered. B4-B6 options are shuffled in every call ("None…",
"No change needed" and "Other" stay last); the shuffle seed comes from the run id
and the call, so a resumed run shows the same order, and the order is logged.

Answers are requested with structured output (JSON schema) and validated again with
jsonschema plus the exclusivity rule. An invalid answer is retried once with the
validation error appended; if it fails again it is logged as invalid, never fixed.

API errors are not answers: a rate limit (429) is waited out and tried again; an
out-of-credits error (402) stops the run. A call that ends in an API error is logged
with status "api_error" and is done again by --resume. Calls with an answer
(valid or invalid) are never repeated. Only attempts that reached the model count
toward max_calls; max_cost_usd counts the cost reported by OpenRouter.
Every request asks OpenRouter for zero-data-retention endpoints only.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import yaml
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_personas as bp  # noqa: E402

from study import ROOT, Study, add_study_arg  # noqa: E402

STUDY = None  # set in main()
BASE_URL = "https://openrouter.ai/api/v1"

MAX_PHOTO_SIDE = 1024
MAX_TOKENS = 600            # per answer; JSON + 2-4 sentences is ~300. Credit is reserved per in-flight request.
EST_OUTPUT_TOKENS = 350     # for the dry-run estimate
CHARS_PER_TOKEN = 3.5       # rough, errs on the high side for English
PROVIDER = {"zdr": True, "data_collection": "deny"}  # zero data retention, no training
RATE_LIMIT_WAITS = (15, 30, 60, 60, 120)  # seconds to wait after each 429 before trying again



def task_keys(codebook):
    """{question: JSON key} of the multi-select Part B questions, from the codebook."""
    return {bid: codebook["part_b"][bid]["json_key"] for bid in bp.TASK_QUESTIONS}


def n_places(codebook):
    return codebook["places"]


class OutOfCredits(Exception):
    pass


def load_run_config():
    with open(STUDY.run_config, encoding="utf-8") as f:
        return yaml.safe_load(f)


def call_key(c):
    return (c["participant"], c["version"], c["place"], c["run"])


def record_status(rec):
    """valid / invalid / api_error; derived for records logged before "status" existed."""
    if "status" in rec:
        return rec["status"]
    if rec["valid"]:
        return "valid"
    return "api_error" if any(e.startswith("API error") for e in rec["errors"]) else "invalid"


def latest_records(log_path):
    """The last logged record of every call (a resumed call is logged again)."""
    latest = {}
    if log_path.exists():
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    rec["status"] = record_status(rec)
                    latest[call_key(rec)] = rec
    return latest


# --- schema ------------------------------------------------------------------

def answer_schema(codebook, strict=False):
    """JSON schema of one answer. strict=True: the subset sent as structured output
    (no maxItems/uniqueItems, which providers may reject); the full one is checked locally."""
    b3_rows = [r["code"] for r in codebook["part_b"]["B3"]["rows"]]
    score = {"type": "integer", "enum": [1, 2, 3, 4, 5]}
    props = {
        "B2_comfort": score,
        "B3_situations": {
            "type": "object",
            "properties": {code: score for code in b3_rows},
            "required": b3_rows,
            "additionalProperties": False,
        },
    }
    for bid, key in task_keys(codebook).items():
        q = codebook["part_b"][bid]
        codes = [o["code"] for o in q["options"]]
        lst = {"type": "array", "items": {"type": "string", "enum": codes}, "minItems": q.get("min", 1)}
        if not strict:
            lst.update({"maxItems": q.get("max", 3), "uniqueItems": True})
        props[key] = lst
    props["reasoning"] = {"type": "string"}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def validate(answer, codebook, schema):
    """-> list of error messages (empty = valid)."""
    errors = [e.message if not e.path else f"{'/'.join(map(str, e.path))}: {e.message}"
              for e in jsonschema.Draft202012Validator(schema).iter_errors(answer)]
    if errors:
        return errors
    for bid, key in task_keys(codebook).items():
        exclusive = {o["code"] for o in codebook["part_b"][bid]["options"] if o.get("exclusive")}
        chosen = answer[key]
        if len(chosen) > 1 and exclusive & set(chosen):
            errors.append(f"{key}: {sorted(exclusive & set(chosen))[0]!r} must be the only code when chosen")
    return errors


# --- prompt ------------------------------------------------------------------

def photo_data_url(place):
    """Photo as a JPEG data URL, resized to at most MAX_PHOTO_SIDE on the long side."""
    path = STUDY.photo(place)
    im = Image.open(path).convert("RGB")
    if max(im.size) > MAX_PHOTO_SIDE:
        im.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), im.size


def call_seed(run_id, pid, version, place, run):
    key = f"{run_id}|{pid}|{version}|P{place}|{run}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)


def plan_calls(run_id, personas, codebook, config, versions):
    """Every call of the run, in execution order, with its prompt parts."""
    system = bp.read_prompt(STUDY, "system.txt")
    calls = []
    for run in range(1, config["runs_per_condition"] + 1):
        for pid in sorted(personas):
            for version in versions:
                for place in range(1, n_places(codebook) + 1):
                    seed = call_seed(run_id, pid, version, place, run)
                    order = bp.option_order(codebook, random.Random(seed))
                    calls.append({
                        "participant": pid, "version": version, "place": f"P{place}", "run": run,
                        "seed": seed, "option_order": order,
                        "system": system,
                        "user_text": personas[pid][version] + "\n\n" + bp.render_task(codebook, order, version, STUDY),
                        "photo": place,
                    })
    return calls


def messages_for(call, photo_url):
    return [
        {"role": "system", "content": call["system"]},
        {"role": "user", "content": [
            {"type": "text", "text": call["user_text"]},
            {"type": "image_url", "image_url": {"url": photo_url}},
        ]},
    ]


# --- cost --------------------------------------------------------------------

def model_pricing(model):
    """USD per token (prompt, completion) from OpenRouter's public model list; no data is sent."""
    with urllib.request.urlopen(f"{BASE_URL}/models/{model}/endpoints", timeout=30) as r:
        data = json.load(r)["data"]
    prices = [(float(e["pricing"]["prompt"]), float(e["pricing"]["completion"])) for e in data["endpoints"]]
    return max(prices)  # the most expensive endpoint, to stay on the safe side


def image_tokens(size):
    """Anthropic's estimate for an image: width x height / 750."""
    w, h = size
    return int(w * h / 750) + 1


# --- dry run -----------------------------------------------------------------

def dry_run(run_id, calls, config, schema_strict, out_dir, versions, codebook):
    places = n_places(codebook)
    photo_sizes = {p: photo_data_url(p)[1] for p in range(1, places + 1)}
    in_tokens = [
        int((len(c["system"]) + len(c["user_text"]) + len(json.dumps(schema_strict))) / CHARS_PER_TOKEN)
        + image_tokens(photo_sizes[c["photo"]])
        for c in calls
    ]
    prompt_price, completion_price = model_pricing(config["model"])
    cost_one = sum(t * prompt_price + EST_OUTPUT_TOKENS * completion_price for t in in_tokens)
    n = len(calls)

    # one full example per version, written to a file (it contains participant data)
    out_dir.mkdir(parents=True, exist_ok=True)
    example = out_dir / "dry_run_examples.txt"
    first = calls[0]["participant"]
    with open(example, "w", encoding="utf-8") as f:
        for version in versions:
            c = next(c for c in calls if c["participant"] == first and c["version"] == version)
            f.write(f"===== {c['participant']} {version} {c['place']} run {c['run']} =====\n")
            f.write(f"--- system ---\n{c['system']}\n--- user (text; the photo place{c['photo']}.jpg "
                    f"{photo_sizes[c['photo']][0]}x{photo_sizes[c['photo']][1]} follows) ---\n{c['user_text']}\n\n")
        f.write(f"===== structured output schema sent to the API =====\n{json.dumps(schema_strict, indent=2)}\n")

    print(f"Dry run {run_id} - no API call made")
    print(f"  model: {config['model']} | temperature {config['temperature']} | provider {PROVIDER}")
    print(f"  calls: {n} (participants x versions x places x runs = "
          f"{len({c['participant'] for c in calls})} x {len(versions)} x {places} x {config['runs_per_condition']})"
          f" | caps: {config['max_calls']} calls, ${config['max_cost_usd']:.2f}")
    print(f"  photos: {', '.join(f'place{p} {s[0]}x{s[1]}' for p, s in photo_sizes.items())}")
    print(f"  input tokens per call: ~{min(in_tokens)}-{max(in_tokens)} (text estimated at "
          f"{CHARS_PER_TOKEN} chars/token, image at w*h/750); output ~{EST_OUTPUT_TOKENS} assumed")
    print(f"  price: ${prompt_price * 1e6:.2f}/M input, ${completion_price * 1e6:.2f}/M output")
    print(f"  estimated cost: ${cost_one:.3f} without retries; ${2 * cost_one:.3f} if every call were retried once")
    if n > config["max_calls"]:
        print(f"  WARNING: {n} calls exceed max_calls {config['max_calls']}; the run will stop at the cap")
    print(f"  one full prompt per version: {example.relative_to(ROOT)}")


# --- real run ----------------------------------------------------------------

def usage_of(resp):
    u = resp.usage.model_dump() if resp.usage else {}
    return u.get("prompt_tokens"), u.get("completion_tokens"), u.get("cost")


def request(client, config, messages, response_format):
    """One request, waiting out rate limits. -> (response, list of rate-limit waits)."""
    import openai

    waits = []
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            resp = client.chat.completions.create(
                model=config["model"], messages=messages, temperature=config["temperature"],
                max_tokens=MAX_TOKENS, response_format=response_format,
                extra_body={"provider": PROVIDER, "usage": {"include": True}},
            )
            return resp, waits
        except openai.APIStatusError as e:
            if e.status_code == 402:
                raise OutOfCredits(str(e)) from e
            if e.status_code == 429 and wait is not None:
                retry_after = e.response.headers.get("retry-after") if e.response is not None else None
                pause = max(wait, int(float(retry_after))) if retry_after else wait
                waits.append(pause)
                print(f"    rate limited, waiting {pause}s", flush=True)
                time.sleep(pause)
                continue
            raise


def real_run(run_id, calls, config, codebook, schema, schema_strict, out_dir, versions):
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv(ROOT / ".env")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY not set in .env")
    client = OpenAI(base_url=BASE_URL, api_key=key)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pilot").write_text("pilot: true\n", encoding="utf-8")
    run_yaml = out_dir / "run.yaml"
    meta = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) if run_yaml.exists() else {
        **config, "run_id": run_id, "study": STUDY.name, "provider": PROVIDER,
        "codebook": str(STUDY.codebook_path.relative_to(ROOT)), "codebook_version": codebook.get("version"), "sessions": []}
    meta.setdefault("sessions", []).append({"started": datetime.now(timezone.utc).isoformat(),
                                            "versions": versions, "max_tokens": MAX_TOKENS})
    run_yaml.write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")

    log_path = out_dir / "calls.jsonl"
    done = latest_records(log_path)
    # what earlier sessions of this run already used, so the caps hold across sessions
    every = []  # every logged record, including ones later redone by a resume
    if log_path.exists():
        with open(log_path, encoding="utf-8") as f:
            every = [json.loads(line) for line in f if line.strip()]
    api_calls = sum(1 for rec in every for a in rec["attempts"] if "raw_response" in a)
    total_cost = sum(rec["cost"] or 0 for rec in every)
    todo = [c for c in calls if done.get(call_key(c), {}).get("status") not in ("valid", "invalid")]
    print(f"Run {run_id}: {len(calls)} calls planned for {versions}, {len(calls) - len(todo)} already answered, "
          f"{len(todo)} to do | so far {api_calls} API calls, ${total_cost:.4f}")

    photos = {p: photo_data_url(p)[0] for p in range(1, n_places(codebook) + 1)}
    response_format = {"type": "json_schema",
                       "json_schema": {"name": "part_b_answer", "strict": True, "schema": schema_strict}}

    stopped = None
    for i, call in enumerate(todo, start=1):
        if api_calls >= config["max_calls"] or total_cost >= config["max_cost_usd"]:
            stopped = f"cap reached: {api_calls} API calls, ${total_cost:.4f}"
            break
        messages = messages_for(call, photos[call["photo"]])
        record = {k: call[k] for k in ("participant", "version", "place", "run", "seed", "option_order")}
        record.update({"run_id": run_id, "requested_model": config["model"],
                       "temperature": config["temperature"], "max_tokens": MAX_TOKENS, "attempts": []})
        answer, errors, status = None, ["no attempt"], "api_error"
        for attempt in range(2):  # first try + one retry for an invalid answer
            if attempt and (api_calls >= config["max_calls"] or total_cost >= config["max_cost_usd"]):
                break
            start = datetime.now(timezone.utc)
            t0 = time.perf_counter()
            att = {"timestamp_start": start.isoformat()}
            try:
                resp, waits = request(client, config, messages, response_format)
                raw = resp.choices[0].message.content or ""
                tin, tout, cost = usage_of(resp)
                att.update({"model": resp.model, "provider": getattr(resp, "provider", None),
                            "finish_reason": resp.choices[0].finish_reason, "rate_limit_waits": waits,
                            "input_tokens": tin, "output_tokens": tout, "cost": cost, "raw_response": raw})
                api_calls += 1
                total_cost += cost or 0.0
                try:
                    answer = json.loads(raw)
                    errors = validate(answer, codebook, schema)
                except json.JSONDecodeError as e:
                    answer, errors = None, [f"not valid JSON: {e}"]
                status = "valid" if not errors else "invalid"
            except OutOfCredits as e:
                answer, errors, status = None, [f"API error: out of credits (402): {e}"], "api_error"
                att["error"] = errors[0]
                stopped = "out of OpenRouter credits (402): add credit, then --resume this run"
            except Exception as e:  # other API or network errors: logged, done again by --resume
                answer, errors, status = None, [f"API error: {type(e).__name__}: {e}"], "api_error"
                att["error"] = errors[0]
            end = datetime.now(timezone.utc)
            att.update({"timestamp_end": end.isoformat(), "seconds": round(time.perf_counter() - t0, 3),
                        "parsed": answer, "errors": errors})
            record["attempts"].append(att)
            if status != "invalid":
                break  # valid, or an API error (not a reason to spend the format retry)
            messages = messages + [
                {"role": "assistant", "content": att.get("raw_response", "")},
                {"role": "user", "content": "Your answer was not valid: " + "; ".join(errors)
                 + ". Answer again with JSON that follows the schema."},
            ]

        answered = [a for a in record["attempts"] if "raw_response" in a]
        last = record["attempts"][-1]
        record.update({
            "model": (answered[-1] if answered else last).get("model"),
            "timestamp_start": record["attempts"][0]["timestamp_start"],
            "timestamp_end": last.get("timestamp_end"),
            "seconds": round(sum(a["seconds"] for a in answered), 3),
            "input_tokens": sum(a.get("input_tokens") or 0 for a in answered),
            "output_tokens": sum(a.get("output_tokens") or 0 for a in answered),
            "cost": round(sum(a.get("cost") or 0 for a in answered), 6),
            "raw_response": last.get("raw_response"),
            "parsed": answer if status == "valid" else None,
            "status": status,
            "valid": status == "valid",
            "errors": errors if status != "valid" else [],
            "retries": max(len(answered) - 1, 0),
        })
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[{i}/{len(todo)}] {call['participant']} {call['version']} {call['place']} run {call['run']}: "
              f"{status.upper() if status != 'valid' else status}, retries {record['retries']}, "
              f"{record['seconds']}s, ${record['cost']:.5f} (total ${total_cost:.4f})", flush=True)
        if stopped:
            break

    final = latest_records(log_path)
    counts = {s: sum(1 for k, r in final.items() if r["status"] == s and k[1] in versions)
              for s in ("valid", "invalid", "api_error")}
    print(f"\nRun {run_id} for {versions}: {counts} of {len(calls)} planned | "
          f"{api_calls} API calls, ${total_cost:.4f} -> {log_path.relative_to(ROOT)}")
    if stopped:
        print(f"STOPPED: {stopped}")
    elif counts["api_error"] or sum(counts.values()) < len(calls):
        print(f"Some calls have no answer yet: python src/run_personas.py --resume {out_dir.relative_to(ROOT)}")


def main():
    global STUDY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_study_arg(ap)
    ap.add_argument("--dry-run", action="store_true", help="build prompts and estimate cost, no API call")
    ap.add_argument("--resume", type=Path, metavar="RUN_DIR", help="continue this run (same run id and seeds)")
    ap.add_argument("--versions", nargs="+", choices=bp.VERSIONS, default=list(bp.VERSIONS),
                    help="versions to run (default: all)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    STUDY = Study(args.study)
    config = load_run_config()
    codebook = STUDY.codebook()
    if not STUDY.personas.exists():
        sys.exit(f"Not found: {STUDY.personas.relative_to(ROOT)}. Run src/build_personas.py first.")
    with open(STUDY.personas, encoding="utf-8") as f:
        personas = json.load(f)
    missing = [v for v in args.versions if any(v not in p for p in personas.values())]
    if missing:
        sys.exit(f"personas.json has no {missing}: run src/build_personas.py again.")

    if args.resume:
        out_dir = args.resume if args.resume.is_absolute() else ROOT / args.resume
        meta = yaml.safe_load((out_dir / "run.yaml").read_text(encoding="utf-8"))
        run_id = meta["run_id"]
        if meta.get("study", STUDY.name) != STUDY.name:
            sys.exit(f"Run {run_id} belongs to study {meta['study']!r}, not {STUDY.name!r}: use --study {meta['study']}.")
        if meta["model"] != config["model"] or meta["temperature"] != config["temperature"]:
            sys.exit(f"{STUDY.run_config.relative_to(ROOT)} (model {config['model']}, temperature "
                     f"{config['temperature']}) differs from run {run_id} ({meta['model']}, {meta['temperature']}): "
                     "not resuming.")
    else:
        model_short = config["model"].split("/")[-1]
        run_id = (datetime.now().strftime("%Y%m%d-%H%M%S") + f"_{model_short}_{STUDY.name}"
                  + ("_dry" if args.dry_run else ""))
        out_dir = STUDY.runs / run_id
    schema = answer_schema(codebook)
    schema_strict = answer_schema(codebook, strict=True)
    calls = plan_calls(run_id, personas, codebook, config, args.versions)

    if args.dry_run:
        dry_run(run_id, calls, config, schema_strict, out_dir, args.versions, codebook)
    else:
        real_run(run_id, calls, config, codebook, schema, schema_strict, out_dir, args.versions)


if __name__ == "__main__":
    main()
