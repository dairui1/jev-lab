"""Build the eval set: Sonnet writes tickets, Opus labels them independently. Both via `claude -p`."""
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent import DEPARTMENTS, FRUSTRATION_LEVELS, URGENT_INSTRUCTIONS

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

DOMAINS = [
    "a B2B SaaS analytics dashboard product",
    "an e-commerce storefront platform used by small merchants",
    "a fintech app handling payments, cards and payouts",
    "a developer API / infrastructure product (SDKs, webhooks, rate limits)",
]
STYLES = (
    "Mix styles heavily: some sarcastic, some that say 'urgent'/'ASAP' about trivial things, some that say "
    "'not urgent' or 'no rush' about genuinely broken things, negations, two topics in one message where one "
    "department clearly dominates, non-native English, very short (3-8 words), long rambling (60+ words), "
    "ALL CAPS, emoji, extremely polite but actually blocked, angry but about something minor, calm but "
    "describing a total outage, questions that are really general/account questions. Avoid duplicates and "
    "avoid reusing the same phrasing."
)


def claude(model, system, prompt, timeout=600):
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
           "--system-prompt", system]
    out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[-500:])
    res = json.loads(out.stdout)["result"]
    m = re.search(r"\[.*\]", res, re.S)
    return json.loads(m.group(0))


def generate(domain, n, seed):
    system = "You write realistic customer support tickets for evaluation datasets. Output only a JSON array."
    prompt = f"""Write {n} distinct customer support messages sent to {domain}.

Target a roughly even spread across departments ({", ".join(DEPARTMENTS)}), roughly half urgent / half not,
and all four frustration levels ({"; ".join(f"{i}={v}" for i, v in enumerate(FRUSTRATION_LEVELS))}).
{STYLES}

Batch seed: {seed}. Return a JSON array of objects: {{"text": "<message>", "hint": {{"is_urgent": bool, "department": "<key>", "frustration": 0-3}}}}
The hint is your intent when writing; it is not the ground truth."""
    return claude("sonnet", system, prompt)


LABEL_SYSTEM = "You are a careful annotator producing gold labels for a support triage benchmark. Output only a JSON array."


def label(batch):
    depts = "\n".join(f"  - {k}: {v}" for k, v in DEPARTMENTS.items())
    levels = "\n".join(f"  - {i}: {v}" for i, v in enumerate(FRUSTRATION_LEVELS))
    items = "\n".join(f"{t['id']}. {t['text']}" for t in batch)
    prompt = f"""Label each ticket on three dimensions.

is_urgent: {URGENT_INSTRUCTIONS}
department (exactly one key):
{depts}
frustration (integer):
{levels}

Tickets:
{items}

Return a JSON array, one object per ticket in the same order:
{{"id": <id>, "is_urgent": bool, "department": "<key>", "frustration": 0|1|2|3}}"""
    return claude("opus", LABEL_SYSTEM, prompt)


def main():
    raw_path = DATA / "tickets_raw.json"
    if raw_path.exists():
        raw = json.loads(raw_path.read_text())
        print(f"reusing {len(raw)} generated tickets")
    else:
        jobs = [(d, 30, i) for i, d in enumerate(DOMAINS)]
        with ThreadPoolExecutor(len(jobs)) as ex:
            batches = list(ex.map(lambda j: generate(*j), jobs))
        raw, seen = [], set()
        for b in batches:
            for t in b:
                key = t["text"].strip().lower()
                if key in seen:
                    continue
                seen.add(key)
                raw.append({"id": len(raw) + 1, "text": t["text"].strip(), "hint": t.get("hint")})
        raw_path.write_text(json.dumps(raw, indent=1, ensure_ascii=False))
        print(f"generated {len(raw)} tickets")

    chunks = [raw[i:i + 20] for i in range(0, len(raw), 20)]
    with ThreadPoolExecutor(len(chunks)) as ex:
        labels = [l for chunk in ex.map(label, chunks) for l in chunk]
    by_id = {l["id"]: l for l in labels}
    out, agree = [], 0
    for t in raw:
        l = by_id.get(t["id"])
        if not l or l["department"] not in DEPARTMENTS or not 0 <= int(l["frustration"]) <= 3:
            print(f"skip #{t['id']}: bad label {l}", file=sys.stderr)
            continue
        gold = {"is_urgent": bool(l["is_urgent"]), "department": l["department"], "frustration": int(l["frustration"])}
        agree += t["hint"] == gold if t.get("hint") else 0
        out.append({"id": t["id"], "text": t["text"], "gold": gold, "hint": t.get("hint")})
    (DATA / "tickets.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    from collections import Counter
    print(f"labeled {len(out)} tickets; writer-hint == opus-gold on all 3 fields: {agree}/{len(out)}")
    print("urgent:", Counter(t["gold"]["is_urgent"] for t in out))
    print("dept:", Counter(t["gold"]["department"] for t in out))
    print("frustration:", Counter(t["gold"]["frustration"] for t in out))


if __name__ == "__main__":
    main()
