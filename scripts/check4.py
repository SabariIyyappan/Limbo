"""CHECKPOINT 4/5 — discard, commit, and the commit gate. Zero Groq tokens.

Drives the same five tool calls the agent makes, through the same :8080 proxy,
using a direct fastmcp.Client. The agent's model is what chooses those calls in
a real run; here we make them by hand so every path can be verified without
spending the day's token budget on it.

    python scripts/check4.py

Requires the five servers up: 8025, 8026, 7000, 9000, 8080.
"""
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from fastmcp import Client  # noqa: E402

LIMBO = "http://127.0.0.1:8080"
WORLD = "http://127.0.0.1:9000"
MCP = f"{LIMBO}/mcp"

BAD_TAX = "ACME-88-4417"   # the registry rejects this one
GOOD_TAX = "ACME-88-4418"  # and accepts everything else
VENDOR = "Acme Industrial Supply"

failures: list[str] = []


def get(url: str) -> dict:
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def post(url: str) -> dict:
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read() or b"{}")}


def inbox(port: int) -> int:
    return get(f"http://localhost:{port}/api/v1/messages")["total"]


def clear_inboxes() -> None:
    for port in (8025, 8026):
        req = urllib.request.Request(
            f"http://localhost:{port}/api/v1/messages", method="DELETE"
        )
        urllib.request.urlopen(req).read()


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"   {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")


async def onboard(tax_id: str) -> None:
    """The five calls, in the order the agent makes them, through Limbo."""
    async with Client(MCP) as c:
        await c.call_tool("create_vendor", {
            "name": VENDOR, "tax_id": tax_id, "contact": "onboarding@acme.com"})
        await c.call_tool("send_email", {
            "to": "onboarding@acme.com", "subject": "Vendor portal invitation",
            "body": "Welcome to the supplier portal."})
        await c.call_tool("send_email", {
            "to": "finance@company.com", "subject": "New vendor payment terms",
            "body": f"{VENDOR} is onboarded on Net 30 terms."})
        await c.call_tool("post_channel", {
            "channel": "#procurement",
            "message": f"{VENDOR} is now an approved vendor."})
        # register_vendor is verify-class: it forwards, so this is a real HTTP
        # call to the registry and the error comes back as content.
        try:
            await c.call_tool("register_vendor", {
                "name": VENDOR, "tax_id": tax_id})
        except Exception as e:  # noqa: BLE001 — a 422 may surface either way
            print(f"   (register_vendor raised: {str(e)[:80]})")


def reset_all() -> None:
    clear_inboxes()
    post(f"{WORLD}/txn/reset")
    post(f"{LIMBO}/reset?full=1")


async def case_discard() -> None:
    print("\n[1] DISCARD — failing verification destroys everything")
    reset_all()
    await onboard(BAD_TAX)

    st = get(f"{LIMBO}/state")
    check("verdict at 422", st["verdict"], "fail")
    print("   waiting out the discard delay...")
    await asyncio.sleep(4.0)

    st, world = get(f"{LIMBO}/state"), get(f"{WORLD}/state")
    check("outcome", st["outcome"], "discarded")
    check("held effects", st["count"], 0)
    check("db rows after rollback", len(world["vendors"]), 0)
    check("inbox1 — nothing sent", inbox(8025), 0)
    check("inbox2 — nothing sent", inbox(8026), 0)


async def case_gate() -> None:
    print("\n[2] THE GATE — commit is refused without a passing verdict")
    reset_all()
    await onboard(BAD_TAX)

    # Race the auto-discard: the gate must refuse on the verdict alone.
    r = post(f"{LIMBO}/commit")
    check("commit refused", r.get("_status"), 409)
    check("nothing flushed", len(r.get("flushed", [])), 0)
    await asyncio.sleep(4.0)
    check("inbox1 still empty", inbox(8025), 0)
    check("inbox2 still empty", inbox(8026), 0)


async def case_commit() -> None:
    print("\n[3] COMMIT — a passing run, then one approval opens the door")
    reset_all()
    await onboard(GOOD_TAX)

    st = get(f"{LIMBO}/state")
    check("verdict", st["verdict"], "pass")
    check("held awaiting approval", st["count"], 3)
    check("nothing sent yet", inbox(8025), 0)

    print("   approving...")
    post(f"{LIMBO}/commit")

    st, world = get(f"{LIMBO}/state"), get(f"{WORLD}/state")
    check("outcome", st["outcome"], "committed")
    check("flushed", len(st["flushed"]), 3)
    check("limbo empty", st["count"], 0)
    check("inbox1 — 2 emails", inbox(8025), 2)
    check("inbox2 — 1 announcement", inbox(8026), 1)
    check("db row committed", len(world["vendors"]), 1)
    check("transaction closed", world["in_transaction"], False)


async def main() -> int:
    try:
        get(f"{LIMBO}/state")
    except Exception as e:  # noqa: BLE001
        print(f"Limbo not reachable at {LIMBO}: {e}")
        print("Start all five servers first (scripts/demo.sh ports).")
        return 2

    await case_discard()
    await case_gate()
    await case_commit()

    print("\n" + "=" * 60)
    if failures:
        print(f"CHECKPOINT 4: FAIL — {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("CHECKPOINT 4: PASS — discard, gate, and commit all verified")
    print("Left on screen: 1 committed row, 2 + 1 messages. "
          "Run scripts/demo.sh reset before recording.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
