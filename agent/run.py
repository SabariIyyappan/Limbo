"""The agent. Limbo-unaware.

Reads MCP_URL from the environment. Point it at :9000 for the unprotected
run or :8080 for the protected run — that env var is the only difference
between the two sides of the demo. Nothing in this file knows Limbo exists.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Model output contains non-ASCII punctuation; never let the log die on it.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from langchain_groq import ChatGroq  # noqa: E402
from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:9000/mcp")
MODEL = os.environ.get("LIMBO_MODEL", "openai/gpt-oss-120b")

# Overridable so attempt 2 can retry with a corrected tax ID. Same agent,
# same prompt, same code path — only the input handed to it changes.
VENDOR = os.environ.get("LIMBO_VENDOR", "Acme Industrial Supply")
TAX_ID = os.environ.get("LIMBO_TAX_ID", "ACME-88-4417")
CONTACT = os.environ.get("LIMBO_CONTACT", "onboarding@acme.com")

TASK = f"""Onboard the new vendor "{VENDOR}" (tax ID {TAX_ID}) across the company.

Perform these five steps in exactly this order, one tool call at a time:

1. create_vendor — name "{VENDOR}", tax_id "{TAX_ID}", contact "{CONTACT}"
2. send_email to {CONTACT} — subject "Vendor portal invitation",
   body welcoming them and inviting them to the supplier portal
3. send_email to finance@company.com — subject "New vendor payment terms",
   body stating {VENDOR} is onboarded on Net 30 terms
4. post_channel to "#procurement" — message exactly:
   "{VENDOR} is now an approved vendor."
5. register_vendor — name "{VENDOR}", tax_id "{TAX_ID}"

Rules:
- You must make all five tool calls. The task is not complete until
  register_vendor has been called.
- Never summarise or announce that onboarding is finished before step 5.
  Steps 1-4 succeeding does not mean the vendor is onboarded — the vendor
  is not onboarded until the federal registry has accepted it.
- Only after register_vendor returns may you write a final summary.
- A step has failed only if the tool itself returns an error. If a tool
  returns successfully, that step is done — do not re-examine your own
  wording, second-guess the content you sent, or repeat the call. Move
  straight to the next step.
- If a tool returns an error, report the failure and stop."""


def log(kind: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {kind:<9} {msg}", flush=True)


def summarize(name: str, args: dict) -> str:
    if name == "send_email":
        return f"-> {args.get('to')} | {args.get('subject')}"
    if name == "post_channel":
        return f"-> {args.get('channel')}"
    return f"-> {args.get('name', '')} {args.get('tax_id', '')}".rstrip()


def result_text(msg) -> str:
    c = msg.content
    if isinstance(c, list):
        return " ".join(
            b.get("text", "") for b in c if isinstance(b, dict)
        ).strip()
    return str(c).strip()


# ------------------------------------------------------- record and replay
#
# A recording take should not depend on the Groq API: the day's token budget is
# finite, a 429 kills a take mid-shot, and a SHORT run wastes one. So a live run
# writes down the tool calls the model chose, and later takes replay them.
#
# This is NOT an LLM cache. Caching the model's *turns* is what silently
# truncated the run before register_vendor — a cached step-4 turn came back
# marked terminal and the 422 never happened. A transcript holds tool calls
# only and is replayed positionally, so it cannot end early: the calls are
# either all there or the transcript was never written.

RUNS = ROOT / "runs"

# Set from --record; when None a live run just doesn't write a transcript.
label: str | None = None


def save_transcript(label: str, calls: list[dict], tax_id: str) -> Path:
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{label}.json"
    path.write_text(
        json.dumps(
            {
                "label": label,
                "model": MODEL,
                "vendor": VENDOR,
                "tax_id": tax_id,
                "recorded": time.strftime("%Y-%m-%d %H:%M:%S"),
                "calls": calls,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


async def replay(path: Path) -> int:
    """Re-issue a recorded run's tool calls. No model, no tokens.

    Every effect is still real — real SMTP, real SQLite writes, the real 422
    from the registry, and Limbo's real hold/verify/commit/discard. Only the
    five decisions are pre-recorded.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    doc = json.loads(path.read_text(encoding="utf-8"))
    protected = ":8080" in MCP_URL
    log("REPLAY", f"{path.name}  recorded {doc['recorded']}  "
                  f"tax_id={doc['tax_id']}  model={doc['model']}")
    log("RUN", f"MCP_URL={MCP_URL}  "
               f"({'THROUGH LIMBO' if protected else 'UNPROTECTED'})")

    client = MultiServerMCPClient(
        {"vendor-ops": {"url": MCP_URL, "transport": "streamable_http"}}
    )
    tools = {t.name: t for t in await client.get_tools()}
    log("TOOLS", ", ".join(sorted(tools)))

    failed = False
    for i, call in enumerate(doc["calls"], 1):
        name, args = call["tool"], call["args"]
        log("CALL", f"{i}. {name} {summarize(name, args)}")
        try:
            text = await tools[name].ainvoke(args)
            if isinstance(text, list):
                text = " ".join(
                    b.get("text", "") for b in text if isinstance(b, dict)
                )
            text = str(text).strip()
        except Exception as e:  # noqa: BLE001 — adapters may raise instead
            text = f"error: {e}"
        if "error" in text.lower():
            failed = True
            log("FAIL", f"{name}: {text[:150]}")
        else:
            log("OK", f"{name}: {text[:110]}")
        # Live runs pace at roughly this rate; without it the whole run lands
        # in one frame and the panel never shows cards stacking.
        if i < len(doc["calls"]):
            await asyncio.sleep(float(os.environ.get("LIMBO_REPLAY_PACE", "1.8")))

    log("DONE", f"{len(doc['calls'])} tool calls, "
                f"run {'FAILED' if failed else 'succeeded'} (replayed)")
    return 1 if failed else 0


async def main() -> int:
    # No LLM cache. A prompt-keyed cache replays the model's step-4 turn as
    # terminal and silently truncates the run before register_vendor — the
    # 422 is the demo, so determinism here means always making the real call.
    protected = ":8080" in MCP_URL
    log("RUN", f"MCP_URL={MCP_URL}  ({'THROUGH LIMBO' if protected else 'UNPROTECTED'})")

    client = MultiServerMCPClient(
        {"vendor-ops": {"url": MCP_URL, "transport": "streamable_http"}}
    )
    tools = await client.get_tools()
    log("TOOLS", ", ".join(sorted(t.name for t in tools)))

    # temperature=0.7, not 0. Greedy decoding on this model occasionally gets
    # stuck re-emitting the vendor name and ships truncated garbage as the
    # post_channel body ('Acice?', 'AcueAc...'); sampling cleared it in the
    # runs measured. Step order is pinned by the prompt, not the temperature.
    agent = create_react_agent(
        ChatGroq(model=MODEL, temperature=0.7, max_retries=3), tools
    )

    calls, failed = 0, False
    pending: dict[str, str] = {}
    seen: set[int] = set()
    transcript: list[dict] = []   # what the model chose, for later replay

    async for _, state in agent.astream(
        {"messages": [("user", TASK)]},
        {"recursion_limit": 40},
        stream_mode="values",
        subgraphs=True,
    ):
        for m in state.get("messages", []):
            if id(m) in seen:
                continue
            seen.add(id(m))
            for tc in getattr(m, "tool_calls", None) or []:
                calls += 1
                pending[tc["id"]] = tc["name"]
                transcript.append({"tool": tc["name"], "args": dict(tc["args"])})
                log("CALL", f"{calls}. {tc['name']} "
                            f"{summarize(tc['name'], tc['args'])}")
            if getattr(m, "tool_call_id", None):
                name = pending.get(m.tool_call_id, "?")
                text = result_text(m)
                if "error" in text.lower():
                    failed = True
                    log("FAIL", f"{name}: {text[:150]}")
                else:
                    log("OK", f"{name}: {text[:110]}")

    # No determinism guard. Every tool call in this log is one the model chose
    # to make. If the agent stops before register_vendor the run simply ends
    # short and the take is reshot — nothing here fabricates the final step.
    short = "register_vendor" not in pending.values()
    if short:
        log("SHORT", "agent stopped before step 5 — discard this take")

    # A short run must never become a transcript, or every replayed take after
    # it would reproduce the truncation.
    if label and not short:
        path = save_transcript(label, transcript, TAX_ID)
        log("SAVED", f"{len(transcript)} calls -> {path.relative_to(ROOT)}")
    elif label:
        log("SAVED", "nothing written — short runs are not recorded")

    log("DONE", f"{calls} tool calls, run {'FAILED' if failed else 'succeeded'}")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="the agent. Limbo-unaware.")
    ap.add_argument("--replay", metavar="FILE",
                    help="re-issue a recorded run's tool calls; no model, "
                         "no tokens. Every effect is still real.")
    ap.add_argument("--record", metavar="LABEL",
                    help="save this live run's tool calls to runs/LABEL.json")
    a = ap.parse_args()

    if a.replay:
        p = Path(a.replay)
        if not p.exists() and not p.is_absolute():
            p = RUNS / a.replay              # bare label, e.g. --replay attempt1
            if p.suffix != ".json":
                p = p.with_suffix(".json")
        if not p.exists():
            print(f"no such transcript: {a.replay}", file=sys.stderr)
            sys.exit(2)
        sys.exit(asyncio.run(replay(p)))

    label = a.record
    sys.exit(asyncio.run(main()))
