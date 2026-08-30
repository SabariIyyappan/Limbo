"""The run controller. Process #11, on :8090.

The panel used to be a spectator: it polled Limbo and four inboxes and showed
you what had already happened, while every run was launched from a terminal
off-screen. This service is what lets the panel *drive* — connect an agent,
pick a vendor, run it, watch it move.

It owns no product logic. It spawns `agent/run.py` exactly as `scripts/demo.sh`
does, parses the log lines that script already prints, and republishes them as
structured steps. Nothing here decides anything about held effects; that is
Limbo's job on :8080 and this process never touches it except to reset between
attempts.

Deliberately a separate process from the proxy. The proxy must stay a thing
that only holds and judges effects — if demo orchestration lived inside it, the
claim "the agent talks to a plain MCP server" would stop being true.
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

PORT = int(os.environ.get("LIMBO_CONTROL_PORT", "8090"))

# The two worlds and the layer between them. Same addresses demo.sh uses.
PROTECTED_MCP = "http://127.0.0.1:8080/mcp"
UNPROTECTED_MCP = "http://127.0.0.1:9001/mcp"
WORLD_P = "http://127.0.0.1:9000"
WORLD_U = "http://127.0.0.1:9001"
LIMBO = "http://127.0.0.1:8080"
MAILPITS = [
    "http://localhost:8025", "http://localhost:8026",   # protected
    "http://localhost:8035", "http://localhost:8036",   # unprotected
]

# The only agent there is. The dropdown lists exactly this and nothing else —
# a greyed-out roadmap in a demo is a promise the code cannot keep.
AGENTS = [
    {
        "id": "vendor-ops",
        "name": "vendor-ops",
        "framework": "LangGraph ReAct",
        "model": os.environ.get("LIMBO_MODEL", "openai/gpt-oss-120b"),
        "tools": ["create_vendor", "send_email", "post_channel", "register_vendor"],
    }
]

# The vendor dropdown. These two tax IDs are not arbitrary: they are exactly
# what runs/*.json were recorded with, so replay covers both without ever
# calling the model. -4417 is the one the registry rejects.
VENDORS = [
    {
        "id": "acme-4417",
        "name": "Acme Industrial Supply",
        "tax_id": "ACME-88-4417",
        "contact": "onboarding@acme.com",
        "flag": "INACTIVE",
        "transcripts": {"unprotected": "unprotected", "protected": "attempt1"},
    },
    {
        "id": "acme-4418",
        "name": "Acme Industrial Supply",
        "tax_id": "ACME-88-4418",
        "contact": "onboarding@acme.com",
        "flag": "",
        "transcripts": {"unprotected": "unprotected", "protected": "attempt2"},
    },
]

# The five steps, in the order the task pins them. Seeded as pending at launch
# so the ledger shows the whole journey ahead of the agent rather than a list
# that grows one row at a time — travelling the steps is the thing being shown.
PLAN = [
    {"tool": "create_vendor", "target": "internal vendor DB"},
    {"tool": "send_email", "target": "onboarding@acme.com"},
    {"tool": "send_email", "target": "finance@company.com"},
    {"tool": "post_channel", "target": "#procurement"},
    {"tool": "register_vendor", "target": "federal registry"},
]


def log(kind: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {kind:<9} {msg}", flush=True)


# ------------------------------------------------------------------- state

STATE: dict = {
    "connected": None,      # agent id, once the operator has connected one
    "protected": False,     # has "protect with limbo" been armed
    "mode": None,           # "unprotected" | "protected" — of the last/current run
    "vendor": VENDORS[0]["id"],
    "live": False,          # replay by default; the toggle turns the model on
    "status": "idle",       # idle | running | done | failed
    "steps": [],
    "lines": [],            # raw agent log, for the output pane
    "summary": None,        # the agent's closing line
    "exit_code": None,
    "started_at": None,
    "finished_at": None,
    "delivered": None,      # what this run actually put into the world
    "rows": None,
    "previous": None,       # the last completed run, kept on screen for contrast
}

_proc: asyncio.subprocess.Process | None = None


def vendor(vid: str) -> dict:
    return next((v for v in VENDORS if v["id"] == vid), VENDORS[0])


def seed_steps() -> list[dict]:
    return [
        {"n": i, "tool": p["tool"], "target": p["target"], "detail": "",
         "status": "pending", "text": ""}
        for i, p in enumerate(PLAN, 1)
    ]


def _snapshot() -> dict:
    """What the finished run amounted to, kept beside the next one.

    `delivered` and `rows` are the damage figures — they are the reason this
    block exists, so the unprotected run's 3-and-1 stays on screen while the
    protected run puts up 0-and-0 next to it.
    """
    return {
        "mode": STATE["mode"],
        "vendor": vendor(STATE["vendor"])["tax_id"],
        "status": STATE["status"],
        "steps": [dict(s) for s in STATE["steps"]],
        "summary": STATE["summary"],
        "delivered": STATE.get("delivered"),
        "rows": STATE.get("rows"),
    }


async def _await_settled(timeout: float = 8.0) -> None:
    """Wait until limbo has stopped changing its mind about this run.

    Terminal means an outcome was recorded, or nothing is held and no verdict
    is pending. Bounded — a wedged proxy must not hang the measurement.
    """
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=3) as c:
        while time.monotonic() < deadline:
            try:
                s = (await c.get(f"{LIMBO}/state")).json()
            except Exception:  # noqa: BLE001 — proxy down: nothing to wait for
                return
            if s["outcome"] is not None:
                return
            if s["verdict"] != "fail":
                # No failure pending, so no discard is coming.
                return
            await asyncio.sleep(0.25)


async def _measure() -> None:
    """Count what the just-finished run actually put into its world.

    On a protected failure Limbo's auto-discard is on a display delay, so the
    agent process exits while the rollback is still pending. Measuring right
    then reports the row that is about to be taken back — the exact number the
    demo exists to disprove. Wait for limbo to reach a terminal state first.
    """
    side = STATE["mode"]
    if side is None:
        return
    if side == "protected":
        await _await_settled()
    boxes = MAILPITS[0:2] if side == "protected" else MAILPITS[2:4]
    world = WORLD_P if side == "protected" else WORLD_U
    total, rows = 0, None
    async with httpx.AsyncClient(timeout=5) as c:
        for m in boxes:
            try:
                total += (await c.get(f"{m}/api/v1/messages")).json()["total"]
            except Exception:  # noqa: BLE001 — a dead inbox must not break the run
                total = None
                break
        try:
            rows = len((await c.get(f"{world}/state")).json()["vendors"])
        except Exception:  # noqa: BLE001
            rows = None
    STATE["delivered"], STATE["rows"] = total, rows


# ------------------------------------------------------------ log parsing
#
# agent/run.py prints "[HH:MM:SS] KIND message" and nothing about it changes
# for us — this is a reader, not a protocol. An unrecognised line is kept for
# the output pane and otherwise ignored; a parser that crashes on a stray line
# would take the whole run display down with it.

LINE = re.compile(r"^\[\d\d:\d\d:\d\d\]\s+(\w+)\s+(.*)$")
CALL = re.compile(r"^(\d+)\.\s+(\w+)\s*(.*)$")


def on_line(raw: str) -> None:
    raw = raw.rstrip()
    if not raw:
        return
    STATE["lines"].append(raw)
    del STATE["lines"][:-200]

    m = LINE.match(raw)
    if not m:
        return
    kind, msg = m.group(1), m.group(2).strip()
    steps = STATE["steps"]

    if kind == "CALL":
        c = CALL.match(msg)
        if not c:
            return
        n, tool, detail = int(c.group(1)), c.group(2), c.group(3).strip()
        # Trust the position, but only if the tool agrees. A live model can in
        # principle deviate from the planned order; when it does, widen the
        # ledger rather than mislabel a row.
        if n <= len(steps) and steps[n - 1]["tool"] == tool:
            s = steps[n - 1]
        else:
            s = {"n": n, "tool": tool, "target": "", "detail": "",
                 "status": "pending", "text": ""}
            if n <= len(steps):
                steps[n - 1] = s
            else:
                steps.append(s)
        s["status"] = "running"
        s["detail"] = detail.lstrip("-> ").strip() or s["detail"]

    elif kind in ("OK", "FAIL"):
        tool = msg.split(":", 1)[0].strip()
        text = msg.split(":", 1)[1].strip() if ":" in msg else ""
        # The running row is the one this result belongs to.
        s = next((x for x in steps if x["status"] == "running"), None)
        if s is None:
            s = next((x for x in reversed(steps) if x["tool"] == tool), None)
        if s is not None:
            s["status"] = "ok" if kind == "OK" else "fail"
            s["text"] = text

    elif kind == "SHORT":
        # The agent stopped before step 5. Say so — never let an unreached
        # step read as anything but unreached.
        for s in steps:
            if s["status"] == "pending":
                s["status"] = "skipped"

    elif kind == "DONE":
        STATE["summary"] = msg


# --------------------------------------------------------------- the run

async def _pump(proc) -> None:
    """Read the agent's stdout to exhaustion, then settle the run."""
    assert proc.stdout is not None
    async for chunk in proc.stdout:
        on_line(chunk.decode("utf-8", "replace"))
    code = await proc.wait()

    STATE["exit_code"] = code
    STATE["finished_at"] = time.time()
    # Exit 1 means a tool returned an error — which on the -4417 path is the
    # demo working, not the harness breaking. "failed" describes the run, and
    # the ledger already shows which step and why.
    STATE["status"] = "failed" if code else "done"
    for s in STATE["steps"]:
        if s["status"] == "running":
            s["status"] = "pending"
    await _measure()
    log("RUN", f"{STATE['mode']} finished (exit {code}) — "
               f"delivered={STATE['delivered']} rows={STATE['rows']}")


async def start_run(mode: str, vid: str, live: bool) -> dict:
    global _proc
    v = vendor(vid)
    mcp = PROTECTED_MCP if mode == "protected" else UNPROTECTED_MCP

    env = dict(os.environ)
    env["MCP_URL"] = mcp
    env["LIMBO_VENDOR"] = v["name"]
    env["LIMBO_TAX_ID"] = v["tax_id"]
    env["LIMBO_CONTACT"] = v["contact"]
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    label = v["transcripts"][mode]
    if live:
        # GROQ_API_KEY lives in .env and nothing loads it automatically —
        # demo.sh sources it, and a run spawned from here has to do the same.
        _load_env(env)
        args = ["--record", label]
    else:
        args = ["--replay", label]

    # Snapshot the run that just ended, on the way into the next one. Taken
    # here rather than when a run finishes so that "previous" always means a
    # different run than the one on screen — otherwise the panel shows the
    # current run twice, once in the ledger and once as its own history.
    prev = _snapshot() if STATE["steps"] else STATE["previous"]

    STATE.update(
        mode=mode, vendor=vid, live=live, status="running",
        steps=seed_steps(), lines=[], summary=None, exit_code=None,
        started_at=time.time(), finished_at=None, previous=prev,
        delivered=None, rows=None,
    )

    log("RUN", f"{mode} {'LIVE' if live else 'replay ' + label} "
               f"tax_id={v['tax_id']} -> {mcp}")
    _proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "agent" / "run.py"), *args,
        cwd=str(ROOT), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    asyncio.create_task(_pump(_proc))
    return STATE


def _load_env(env: dict) -> None:
    """Pull .env into a spawned run's environment. Live runs only."""
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, val = line.partition("=")
        env.setdefault(k.strip(), val.strip().strip('"').strip("'"))


def running() -> bool:
    return _proc is not None and _proc.returncode is None


# ------------------------------------------------------------------ app

app = FastAPI(title="Limbo run controller")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.get("/config")
def config():
    """What the panel offers in its two dropdowns."""
    return {"agents": AGENTS, "vendors": VENDORS}


@app.get("/state")
def state():
    return {**STATE, "running": running(),
            "agent": next((a for a in AGENTS if a["id"] == STATE["connected"]), None)}


@app.post("/connect")
async def connect(request: Request):
    body = await request.json()
    aid = body.get("agent")
    if not any(a["id"] == aid for a in AGENTS):
        return JSONResponse({"error": f"unknown agent {aid!r}"}, status_code=404)
    STATE["connected"] = aid
    log("CONNECT", f"agent {aid} connected")
    return state()


@app.post("/run")
async def run(request: Request):
    body = await request.json()
    if not STATE["connected"]:
        return JSONResponse({"error": "no agent connected"}, status_code=409)
    if running():
        return JSONResponse({"error": "a run is already in flight"}, status_code=409)

    mode = "protected" if STATE["protected"] else "unprotected"
    vid = body.get("vendor", STATE["vendor"])
    live = bool(body.get("live", STATE["live"]))

    if mode == "protected":
        # Each protected run is its own attempt: clear limbo and roll the
        # world back to the state it was in before, so a second run is not
        # judged on the first one's leftovers.
        await _limbo_reset(full=STATE["previous"] is None
                           or STATE["previous"]["mode"] != "protected")
    return await start_run(mode, vid, live)


@app.post("/protect")
async def protect(request: Request):
    """Arm (or disarm) the staging layer. The pivot of the demo.

    Arming does not run anything — it resets limbo and the protected world so
    the next run starts clean, and flips the panel into its protected skin.
    """
    # Read the body once. `await request.body()` consumes the stream, so
    # testing it before calling .json() leaves the parse with nothing and
    # every disarm silently reads as an arm.
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    on = bool(body.get("on", True))
    if running():
        return JSONResponse({"error": "a run is in flight"}, status_code=409)
    STATE["protected"] = on
    if on:
        await _limbo_reset(full=True)
    # Arming clears the ledger for the protected run, but the unprotected run
    # it replaces is the entire comparison — keep it as history rather than
    # dropping it on the floor at the exact moment it starts to matter.
    if STATE["steps"]:
        STATE["previous"] = _snapshot()
    STATE["steps"] = []
    STATE["status"] = "idle"
    STATE["lines"] = []
    STATE["summary"] = None
    log("PROTECT", f"limbo {'armed' if on else 'disarmed'}")
    return state()


async def _limbo_reset(full: bool) -> None:
    """Clear limbo and the protected world's open transaction."""
    async with httpx.AsyncClient(timeout=10) as c:
        for url in (f"{WORLD_P}/txn/reset",
                    f"{LIMBO}/reset" + ("?full=1" if full else "")):
            try:
                await c.post(url)
            except Exception as e:  # noqa: BLE001 — a dead process must not 500
                log("WARN", f"{url}: {e}")


@app.post("/reset")
async def reset():
    """Full clean slate — the same sequence as `scripts/demo.sh reset`."""
    if running():
        return JSONResponse({"error": "a run is in flight"}, status_code=409)
    async with httpx.AsyncClient(timeout=10) as c:
        for url in (f"{WORLD_P}/txn/reset", f"{WORLD_U}/txn/reset",
                    f"{LIMBO}/reset?full=1"):
            try:
                await c.post(url)
            except Exception as e:  # noqa: BLE001
                log("WARN", f"{url}: {e}")
        for m in MAILPITS:
            try:
                await c.delete(f"{m}/api/v1/messages")
            except Exception as e:  # noqa: BLE001
                log("WARN", f"{m}: {e}")
    STATE.update(protected=False, mode=None, status="idle", steps=[],
                 lines=[], summary=None, exit_code=None, previous=None,
                 started_at=None, finished_at=None, delivered=None, rows=None)
    log("RESET", "both worlds, limbo, and all four inboxes clear")
    return state()


@app.get("/worlds")
async def worlds():
    """Durable rows on both sides, in one call so the panel needs one fetch.

    Read through each tool server's own connection — rows written inside the
    open transaction are visible there and nowhere else.
    """
    out = {}
    async with httpx.AsyncClient(timeout=5) as c:
        for key, url in (("protected", WORLD_P), ("unprotected", WORLD_U)):
            try:
                out[key] = (await c.get(f"{url}/state")).json()
            except Exception:  # noqa: BLE001 — one dead world must not blank both
                out[key] = None
    return out


if __name__ == "__main__":
    import uvicorn

    log("CONTROL", f"run controller on :{PORT}")
    log("CONTROL", f"protected -> {PROTECTED_MCP}   unprotected -> {UNPROTECTED_MCP}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
