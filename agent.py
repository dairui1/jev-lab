"""Support triage benchmark. Same workflow, two judgment engines: --engine jev | llm. Async, concurrent."""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
DEPARTMENTS = {
    "billing": "payments, invoices, charges, refunds, renewals",
    "technical": "bugs, outages, errors, broken features, integrations, login problems",
    "sales": "plans, upgrades, pricing, demos, feature availability",
    "general": "feedback, simple account questions, anything else",
}
FRUSTRATION_LEVELS = [
    "calm, neutral request",
    "mildly annoyed or impatient",
    "clearly frustrated, sarcastic or complaining",
    "angry, threatening to leave or escalate",
]
URGENT_INSTRUCTIONS = (
    "Does the customer need a response within hours because something is broken, "
    "blocking them, or costing them money right now? Judge the situation, not the "
    "wording: sarcastic or rhetorical use of 'urgent'/'asap' does not count, and "
    "'not urgent' can still describe an urgent situation."
)


def decide_action(pred, p_urgent=None):
    if p_urgent is not None and 0.35 <= p_urgent <= 0.65:
        return "human_review"
    if pred["is_urgent"] and pred["frustration"] >= 2:
        return f"escalate_to_lead:{pred['department']}"
    if pred["is_urgent"]:
        return f"priority_queue:{pred['department']}"
    return f"queue:{pred['department']}"


# ---------------------------------------------------------------- jev engine
def make_jev():
    from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy, Score

    client = AsyncTypeSafeClient(retry=RetryPolicy(max_retries=4))
    questions = {
        "urgent": Noul(instructions=URGENT_INSTRUCTIONS),
        "department": Choice(instructions="Which team should handle `ticket`?", criteria=DEPARTMENTS),
        "frustration": Score(instructions="How frustrated is the customer in `ticket`?", criteria=FRUSTRATION_LEVELS),
    }

    async def judge(text):
        r = await client.system_one(state={"ticket": text}, questions=questions)
        a = r.answers
        p_urgent = a["urgent"].noul
        pred = {
            "is_urgent": p_urgent > 0.5,
            "department": a["department"].choice,
            "frustration": int(round(a["frustration"].score)),
        }
        detail = {
            "model": r.model,
            "p_urgent": p_urgent,
            "department_confidence": a["department"].confidence,
            "department_probs": a["department"].probabilities,
            "frustration_score": a["frustration"].score,
            "frustration_confidence": a["frustration"].confidence,
            "frustration_probs": a["frustration"].probabilities,
            "parse_ok": True,
        }
        usage = {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}
        return pred, detail, usage, None, decide_action(pred, p_urgent)

    return judge, client


# ---------------------------------------------------------------- llm engine
LLM_SYSTEM = "You are a support triage classifier. Reply with a single JSON object and nothing else."
LLM_PROMPT = """Classify this support ticket.

Ticket: {text!r}

is_urgent: {urgent}
department (pick one): {depts}
frustration (0-3): {levels}

Return exactly: {{"is_urgent": true|false, "department": "<one of the keys>", "frustration": 0|1|2|3}}"""


def make_llm(model):
    """`claude -p` as the LLM: tools/MCP/settings stripped, thinking off. api_ms is the model-side latency."""
    import os

    env = {**os.environ, "MAX_THINKING_TOKENS": "0"}
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
           "--system-prompt", LLM_SYSTEM]
    depts = "; ".join(f"{k} = {v}" for k, v in DEPARTMENTS.items())
    levels = "; ".join(f"{i} = {v}" for i, v in enumerate(FRUSTRATION_LEVELS))

    async def judge(text):
        prompt = LLM_PROMPT.format(text=text, urgent=URGENT_INSTRUCTIONS, depts=depts, levels=levels)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(err.decode()[-300:])
        res = json.loads(out)
        raw = res.get("result", "")
        u = res["usage"]
        usage = {"input_tokens": u["input_tokens"] + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                 "output_tokens": u["output_tokens"]}
        m = re.search(r"\{.*\}", raw, re.S)
        pred, parse_ok = None, False
        if m:
            try:
                d = json.loads(m.group(0))
                pred = {
                    "is_urgent": bool(d["is_urgent"]),
                    "department": str(d["department"]).lower(),
                    "frustration": int(d["frustration"]),
                }
                parse_ok = pred["department"] in DEPARTMENTS and 0 <= pred["frustration"] <= 3
            except (ValueError, KeyError, TypeError):
                pass
        if not parse_ok:
            pred = {"is_urgent": False, "department": "general", "frustration": 0}
        detail = {"model": next(iter(res.get("modelUsage", {})), model), "raw": raw, "parse_ok": parse_ok,
                  "api_ms": res.get("duration_api_ms"), "thinking_tokens": u.get("output_tokens_details", {}).get("thinking_tokens")}
        cost = res.get("total_cost_usd")
        action = decide_action(pred) if parse_ok else "human_review"
        return pred, detail, usage, cost, action

    return judge, None


# ---------------------------------------------------------------- main loop
async def run(args):
    tickets = json.loads((ROOT / args.tickets).read_text())
    out_path = Path(args.out or ROOT / "traces" / f"{args.engine}.jsonl")
    out_path.parent.mkdir(exist_ok=True)
    judge, client = make_jev() if args.engine == "jev" else make_llm(args.llm_model)
    sem = asyncio.Semaphore(args.concurrency)
    f = out_path.open("w")
    t0 = time.time()
    f.write(json.dumps({"event": "start", "engine": args.engine, "t": t0, "n": len(tickets),
                        "concurrency": args.concurrency}) + "\n")

    async def one(tk):
        async with sem:
            ts = time.time()
            try:
                pred, detail, usage, cost, action = await judge(tk["text"])
                err = None
            except Exception as e:  # keep the session alive; record the failure
                pred, detail, usage, cost, action, err = None, {"parse_ok": False}, None, None, "error", repr(e)
            te = time.time()
        rec = {
            "event": "ticket", "engine": args.engine, "id": tk["id"], "text": tk["text"],
            "t_start": round(ts - t0, 3), "t_end": round(te - t0, 3),
            "latency_ms": round((te - ts) * 1000, 1),
            "gold": tk["gold"], "pred": pred, "action": action,
            "usage": usage, "cost_usd": cost, "detail": detail, "error": err,
        }
        f.write(json.dumps(rec) + "\n")
        f.flush()
        ok = "" if pred is None else " ".join(
            ("+" if pred[k] == tk["gold"][k] else "x") for k in ("is_urgent", "department", "frustration"))
        print(f"[{args.engine}] #{tk['id']:>3} {rec['latency_ms']:>7.0f}ms  {ok}  {action}  {err or ''}", flush=True)

    await asyncio.gather(*(one(tk) for tk in tickets))
    f.write(json.dumps({"event": "end", "engine": args.engine, "t": time.time(),
                        "elapsed_s": round(time.time() - t0, 3)}) + "\n")
    f.close()
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close:
        r = close()
        if asyncio.iscoroutine(r):
            await r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["jev", "llm"], required=True)
    ap.add_argument("--llm-model", default="haiku")
    ap.add_argument("--tickets", default="data/tickets.json")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default=None)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
