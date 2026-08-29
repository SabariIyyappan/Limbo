"""LIMBO — the afterlife for agent side effects.

A transparent MCP proxy in front of the real tool server. The agent connects
here instead of :9000 and sees the identical tool list; the only difference
between the protected and unprotected run is one env var on the agent side.

Every outbound tool call is classified. Internal calls forward to the real
server and land in the open SQL transaction, where they can be rolled back.
Verification calls forward too — they read the outside world rather than
changing it, and their answer is what decides the run. Everything else never
leaves the building: it is appended to the limbo list and answered with a
plausible receipt, so the agent stays coherent and never learns it was
intercepted.

This process holds the effects, records the verdict, and acts on it: a failing
verification rolls the transaction back and destroys the held effects, and the
only way out into the world is a human approval on a run that has already
passed verification.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.server import create_proxy  # noqa: E402
from fastmcp.server.middleware import Middleware, MiddlewareContext  # noqa: E402
from mcp.types import TextContent  # noqa: E402
from fastmcp.tools.tool import ToolResult  # noqa: E402

UPSTREAM = os.environ.get("LIMBO_UPSTREAM", "http://127.0.0.1:9000/mcp")
PORT = int(os.environ.get("LIMBO_PORT", "8080"))

# The tool server's HTTP control surface — the same transaction the internal
# writes landed in. Rollback and commit are database primitives, not undo code.
WORLD = os.environ.get("LIMBO_WORLD", "http://127.0.0.1:9000")

# How long to wait after a FAIL verdict before purging. This is a *display*
# delay, not a decision delay: the decision is already made and nothing can
# escape in the meantime, but the cards need to stay on screen long enough to
# be read against the 422 that killed them. Set to 0 for instant discard.
DISCARD_DELAY = float(os.environ.get("LIMBO_DISCARD_DELAY", "2.5"))

# The classifier. A static map is the honest implementation for a one-day
# build. Three classes:
#
#   INTERNAL — reaches only our own database, inside the open transaction we
#              can roll back. Forwarded; the write is undoable.
#   VERIFY   — the check that decides whether the run was any good. Forwarded
#              so the real answer comes back: the agent sees the real 422 and
#              the verifier has something true to judge. Verification reads
#              the outside world, it does not change it.
#   external — everything else. Held. This is the irreversible category.
#
# Fail closed — a tool nobody classified falls through to external and is held.
INTERNAL = {"create_vendor"}
VERIFY = {"register_vendor"}


def classify(tool: str) -> str:
    if tool in INTERNAL:
        return "internal"
    if tool in VERIFY:
        return "verify"
    return "external"


# ---------------------------------------------------------------- limbo state

HELD: list[dict] = []       # effects awaiting judgment, in capture order
FORWARDED: list[dict] = []  # internal + verify calls that were let through
VERDICTS: list[dict] = []   # what the verification calls came back with
FLUSHED: list[dict] = []    # effects that actually went out, on commit
ATTEMPT = 1                 # which try at the task this is
OUTCOME: str | None = None  # None | "discarded" | "committed" — terminal state
_seq = 0


def log(kind: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {kind:<9} {msg}", flush=True)


def describe(tool: str, args: dict) -> dict:
    """Human-facing summary of an effect, for the staging panel."""
    if tool == "send_email":
        return {"target": args.get("to", ""), "detail": args.get("subject", "")}
    if tool == "post_channel":
        return {"target": args.get("channel", ""),
                "detail": (args.get("message", "") or "")[:80]}
    if tool == "register_vendor":
        return {"target": "federal registry", "detail": args.get("name", "")}
    return {"target": args.get("name", ""), "detail": ""}


def receipt(tool: str, args: dict) -> str:
    """A plausible success string, shaped like the real tool's return value.

    The agent must not be able to tell a held effect from a delivered one, or
    it will change its behaviour and the two runs stop being comparable.
    """
    if tool == "send_email":
        return f"email delivered to {args.get('to')} (subject: {args.get('subject')!r})"
    if tool == "post_channel":
        return f"posted to {args.get('channel')}"
    if tool == "register_vendor":
        return f"registered {args.get('name')} with the federal registry"
    return f"{tool} completed"


class LimboMiddleware(Middleware):
    """Intercepts every tool call passing through the proxy."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        global _seq
        tool = context.message.name
        args = dict(context.message.arguments or {})

        kind = classify(tool)

        if kind in ("internal", "verify"):
            log("FORWARD", f"{tool} ({kind}) -> upstream")
            result = await call_next(context)
            FORWARDED.append(
                {"tool": tool, "kind": kind, "args": args, "at": time.time()}
            )
            if kind == "verify":
                # The verdict. Tool errors arrive as content with isError,
                # not as exceptions, so read the flag and the text.
                text = " ".join(
                    getattr(b, "text", "") for b in (result.content or [])
                ).strip()
                ok = not (result.is_error or "error" in text.lower())
                VERDICTS.append(
                    {"tool": tool, "ok": ok, "detail": text[:200],
                     "at": time.time()}
                )
                log("VERDICT", f"{tool} {'PASS' if ok else 'FAIL'}: {text[:110]}")
                if not ok:
                    # Nobody has to decide anything. A failed verification
                    # destroys the held effects on its own — scheduled, not
                    # awaited, because the agent is still mid-run and blocking
                    # its tool response here would stall it.
                    asyncio.create_task(_delayed_discard())
            return result

        _seq += 1
        text = receipt(tool, args)
        HELD.append(
            {
                "id": _seq,
                "tool": tool,
                "args": args,
                "receipt": text,
                "at": time.time(),
                **describe(tool, args),
            }
        )
        log("HOLD", f"{tool} {describe(tool, args)['target']}  "
                    f"({len(HELD)} in limbo)")
        # The mirrored tools declare an outputSchema (upstream returns str,
        # which FastMCP wraps as {"result": ...}). A receipt with only
        # unstructured content fails the client's output validation and the
        # agent sees an error — so the held result must match that shape too.
        return ToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content={"result": text},
        )


# ----------------------------------------------------------------- the proxy

# create_proxy mirrors the upstream tool list verbatim — the agent sees the same
# four tools with the same schemas and docstrings it would see at :9000.
proxy = create_proxy(Client(UPSTREAM), name="vendor-ops")
proxy.add_middleware(LimboMiddleware())


def verdict() -> str:
    """Has the run earned a commit yet?

    'pending' until a verification call has come back. COMMIT is reachable
    only through 'pass' — there is one transition into success, and nothing
    in the hold path can produce it.
    """
    if not VERDICTS:
        return "pending"
    return "pass" if all(v["ok"] for v in VERDICTS) else "fail"


def _state() -> dict:
    return {
        "held": HELD,
        "forwarded": FORWARDED,
        "verdicts": VERDICTS,
        "flushed": FLUSHED,
        "verdict": verdict(),
        "attempt": ATTEMPT,
        "outcome": OUTCOME,
        "count": len(HELD),
        "status": "holding" if HELD else "empty",
    }


# ------------------------------------------------------- discard and commit

async def _world(action: str) -> None:
    """Drive the tool server's transaction. commit | rollback | reset."""
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{WORLD}/txn/{action}")


async def _discard() -> dict:
    """Undo everything this run did. The failure path.

    Two moves and the run is as if it never happened: ROLLBACK takes back the
    internal writes, and the held effects are destroyed unsent. Nothing here
    can reach the outside world — that is the whole point.
    """
    global OUTCOME
    if OUTCOME is not None:
        log("DISCARD", f"already {OUTCOME} — nothing to do")
        return _state()

    await _world("rollback")
    for h in HELD:
        log("PURGE", f"{h['tool']:<14} -> {h['target']}  DESTROYED UNSENT")
    n = len(HELD)
    HELD.clear()
    OUTCOME = "discarded"
    log("DISCARD", f"rollback + {n} effects purged — nothing left the building")
    return _state()


async def _delayed_discard() -> None:
    """Auto-discard, paced so the 422 is readable before the cards vanish."""
    if DISCARD_DELAY:
        await asyncio.sleep(DISCARD_DELAY)
    await _discard()


async def _commit() -> dict:
    """Let the run into the world. The only door, and it needs a passing verdict.

    COMMIT is unreachable except through a verifier that passed: the gate below
    is the product claim. Then the transaction becomes durable and the held
    effects are replayed to the real tool server in capture order.
    """
    global OUTCOME
    v = verdict()
    if v != "pass" or OUTCOME is not None:
        log("REFUSED", f"commit denied (verdict={v}, outcome={OUTCOME})")
        return {"error": "commit requires a passing verdict",
                "verdict": v, "outcome": OUTCOME, **_state()}

    await _world("commit")
    log("COMMIT", "transaction committed — internal state is durable")

    # A fresh client: the proxy's own is session-scoped to the request that
    # is passing through it, and there is no request here.
    failed = []
    async with Client(UPSTREAM) as c:
        for h in list(HELD):
            try:
                r = await c.call_tool(h["tool"], h["args"])
                text = " ".join(
                    getattr(b, "text", "") for b in (r.content or [])
                ).strip()
                FLUSHED.append({**h, "delivered": text[:200]})
                log("FLUSH", f"{h['tool']:<14} -> {h['target']}")
                HELD.remove(h)
            except Exception as e:  # noqa: BLE001 — report, never swallow
                # Flush is not atomic. If it breaks partway we are in exactly
                # the state Limbo exists to prevent, so say so loudly and
                # leave the survivors held rather than pretending they went.
                failed.append(h["tool"])
                log("FLUSH-FAIL", f"{h['tool']} -> {h['target']}: {e}")

    OUTCOME = "committed"
    log("COMMIT", f"{len(FLUSHED)} effects delivered"
                  + (f", {len(failed)} FAILED and still held" if failed else ""))
    return _state()


@proxy.custom_route("/state", methods=["GET"])
async def state(request):
    """What is currently awaiting judgment. The staging panel polls this."""
    from starlette.responses import JSONResponse

    return JSONResponse(_state())


@proxy.custom_route("/discard", methods=["POST"])
async def discard(request):
    """Manual override. The failing path normally fires this by itself."""
    from starlette.responses import JSONResponse

    return JSONResponse(await _discard())


@proxy.custom_route("/commit", methods=["POST"])
async def commit(request):
    """The door. One human approval, and only on a run that passed."""
    from starlette.responses import JSONResponse

    result = await _commit()
    return JSONResponse(result, status_code=409 if "error" in result else 200)


@proxy.custom_route("/reset", methods=["POST"])
async def reset(request):
    """Clear limbo. Not part of the commit/discard story.

    Plain reset is a *retry* — the attempt counter goes up, which is what the
    panel shows during beat 2. `?full=1` resets the counter too, for a fresh
    take.
    """
    from starlette.responses import JSONResponse

    global _seq, ATTEMPT, OUTCOME
    full = request.query_params.get("full") in ("1", "true", "yes")
    HELD.clear()
    FORWARDED.clear()
    VERDICTS.clear()
    FLUSHED.clear()
    OUTCOME = None
    _seq = 0
    ATTEMPT = 1 if full else ATTEMPT + 1
    log("RESET", f"limbo cleared — attempt {ATTEMPT}")
    return JSONResponse(_state())


if __name__ == "__main__":
    import uvicorn
    from starlette.middleware import Middleware as ASGIMiddleware
    from starlette.middleware.cors import CORSMiddleware

    log("LIMBO", f"proxying {UPSTREAM} on :{PORT}")
    log("LIMBO", f"internal={sorted(INTERNAL)}  verify={sorted(VERIFY)}  "
                 f"everything else held")
    log("LIMBO", f"discard delay {DISCARD_DELAY}s")

    # The panel lives on :8081 and polls this server, which is a cross origin.
    # Without CORS (and without relaxing FastMCP's origin protection) every
    # poll fails silently and the panel just sits there empty.
    app = proxy.http_app(
        middleware=[
            ASGIMiddleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            )
        ],
        allowed_origins=["*"],
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
