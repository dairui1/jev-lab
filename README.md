# jev-lab

Hands-on experiments with [TypeSafe's Jev](https://docs.typesafe.ai/introduction) — a "System One" model that answers typed choice questions with calibrated probabilities instead of generating text.

## 1. Jev vs LLM on support triage (`agent.py`)

Same triage workflow, two judgment engines: Jev (`--engine jev`) vs Haiku 4.5 via `claude -p` (`--engine llm`). 120 synthetic tickets (Sonnet-generated, Opus-labeled gold), three judgments each: department, urgency, frustration level.

Live replay viewer: **https://jev.dairui1.com**

| | Jev | Haiku 4.5 (no thinking) |
|---|---|---|
| urgent accuracy | 91% | 79% |
| frustration accuracy | 79% | 65% |
| department | tie | tie |
| latency (API only) | ~431 ms | ~1536 ms |
| observed price | ≈ $0.037 / M tokens blended | — |

Jev's `p(urgent)` is well calibrated (bucket hit-rates 0 / 21 / 41 / 75 / 100%), so the workflow can route 0.35–0.65 to `human_review` instead of guessing.

```
make_dataset.py   # generate + label tickets → data/tickets.json
agent.py          # run one engine, write traces/<engine>.jsonl
build_viewer.py   # traces → viewer.html (+ traces/data.js)
run.sh            # both engines in parallel, then build + stage deploy/public
deploy/           # Cloudflare Worker static assets (npx wrangler deploy)
```

Secrets go in `.secrets/typesafe.env` (gitignored). The LLM side runs `claude -p` with tools/MCP disabled — otherwise each call drags ~120k tokens of tool definitions along.

## 2. Jev + browser: two implementations compared (`research/`)

Source-level study of [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) and [cline/plugins → jev-browser](https://github.com/cline/plugins/tree/main/plugins/jev-browser), both landed 2026-09-18.

→ [`research/jev-browser-compare.html`](research/jev-browser-compare.html) (open locally, or the [published page](https://claude.ai/artifact/Qid5du6ztWzYkQeybBW8Jg))

Short version:

- **Shared DNA**: Jev only *chooses* among observed element IDs / fixed operations; a small LLM is called only for `TYPE_TEXT` and must return `{"text": ...}`; structured DOM snapshot (≤6000 chars visible text) instead of screenshots; log the action before observing; stale decisions are re-observed, mutations are never retried; 3 no-progress actions → blocked.
- **The real fork — what one request asks**: ultrafast uses TypeSafe *speculative fan-out* (one `operation` question + a `*_target` head per operation, executor consumes only the selected head). Cline flattens `(operation, target)` into a single `action` choice (`CLICK:3`, `TYPE_TEXT:3`, `SELECT:5:2`, … + `WAIT/DONE/BLOCKED/REVIEW`).
- **Browser**: ultrafast drives the user's existing Chrome over CDP (browser-harness) and waits on rAF / visible `[role=option]` (≤200 ms); Cline runs an isolated Playwright Chromium with origin allowlist, video, fixed 150/350/600 ms post-action delays — the main source of the speed gap.
- **Cline adds** `offscreenControls` + `selectedOptions` to the observation and a `REVIEW` escape hatch that hands consequential actions back to the host agent; ultrafast has the finer accessible-name algorithm and scoped freshness guards, plus measured numbers (7.07 s Google Flights, 17 Jev requests, 178 ms median).

## License

MIT
