"""CHECKPOINT 5 — both worlds, side by side, no contamination. Zero Groq tokens.

Extends check4.py across the two-world layout. Drives the same five tool calls
the agent makes, through each side's own endpoint, with a direct fastmcp.Client.
The agent's model is what chooses those calls in a real run; here we make them
by hand so every path can be verified without spending the day's token budget.

    python scripts/check5.py

Requires all ten processes: 7000, 8025, 8026, 8035, 8036, 8080, 8081, 9000,
9001 (and the two SMTP ports behind the Mailpits).
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from fastmcp import Client  # noqa: E402

LIMBO = "http://127.0.0.1:8080"

# The two worlds. The unprotected side is reached directly; the protected side
# is reached through Limbo, which forwards to its own tool server on :9000.
WORLDS = {
    "unprotected": {"world": "http://127.0.0.1:9001",
                    "mcp": "http://127.0.0.1:9001/mcp",
                    "mail": (8035, 8036)},
    "protected":   {"world": "http://127.0.0.1:9000",
                    "mcp": f"{LIMBO}/mcp",
                    "mail": (8025, 8026)},
}

BAD_TAX = "ACME-88-4417"   # the registry rejects this one
GOOD_TAX = "ACME-88-4418"  # and accepts everything else
VENDOR = "Acme Industrial Supply"

failures: list[str] = []


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def post(url: str) -> dict:
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read() or b"{}")}


def inbox(port: int) -> int:
    return get(f"http://localhost:{port}/api/v1/messages")["total"]


def mail_total(side: str) -> int:
    return sum(inbox(p) for p in WORLDS[side]["mail"])


def rows(side: str) -> int:
    return len(get(f"{WORLDS[side]['world']}/state")["vendors"])


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"   {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")


async def onboard(mcp_url: str, tax_id: str) -> None:
    """The five calls, in the order the agent makes them."""
    async with Client(mcp_url) as c:
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
        try:
            await c.call_tool("register_vendor", {
                "name": VENDOR, "tax_id": tax_id})
        except Exception as e:  # noqa: BLE001 — a 422 may surface either way
            print(f"   (register_vendor raised: {str(e)[:70]})")


def reset_all() -> None:
    for side, w in WORLDS.items():
        post(f"{w['world']}/txn/reset")
        for p in w["mail"]:
            req = urllib.request.Request(
                f"http://localhost:{p}/api/v1/messages", method="DELETE")
            urllib.request.urlopen(req).read()
    post(f"{LIMBO}/reset?full=1")


async def case_unprotected() -> None:
    print("\n[1] UNPROTECTED — no staging layer, effects escape")
    reset_all()
    await onboard(WORLDS["unprotected"]["mcp"], BAD_TAX)

    check("unprotected inbox1 — portal + finance", inbox(8035), 2)
    check("unprotected inbox2 — #procurement", inbox(8036), 1)
    check("unprotected db row (still in txn)", rows("unprotected"), 1)
    # The 422 happened, but the three effects were already gone. That is the
    # entire problem the product exists to solve.
    print("   ^ step 5 failed AFTER these three already reached the world")


async def case_no_contamination() -> None:
    print("\n[2] ISOLATION — the protected world is untouched by the above")
    check("protected inbox total", mail_total("protected"), 0)
    check("protected db rows", rows("protected"), 0)
    st = get(f"{LIMBO}/state")
    check("limbo held", st["count"], 0)
    print("   ^ both panes can now be filmed in one frame showing opposite states")


async def case_protected_discard() -> None:
    print("\n[3] PROTECTED — same five calls, held then purged")
    await onboard(WORLDS["protected"]["mcp"], BAD_TAX)

    st = get(f"{LIMBO}/state")
    check("verdict at 422", st["verdict"], "fail")
    check("held before purge", st["count"], 3)
    check("protected inbox — nothing sent", mail_total("protected"), 0)

    print("   waiting out the discard delay...")
    await asyncio.sleep(4.0)
    st = get(f"{LIMBO}/state")
    check("outcome", st["outcome"], "discarded")
    check("held after purge", st["count"], 0)
    check("protected db rolled back", rows("protected"), 0)
    check("protected inbox still empty", mail_total("protected"), 0)
    # And the other side is still holding its evidence, unchanged.
    check("unprotected inbox untouched", mail_total("unprotected"), 3)


async def case_gate() -> None:
    print("\n[4] THE GATE — commit refused without a passing verdict")
    post(f"{LIMBO}/reset?full=1")
    post(f"{WORLDS['protected']['world']}/txn/reset")
    await onboard(WORLDS["protected"]["mcp"], BAD_TAX)

    r = post(f"{LIMBO}/commit")
    check("commit refused", r.get("_status"), 409)
    check("nothing flushed", len(r.get("flushed", [])), 0)
    await asyncio.sleep(4.0)
    check("protected inbox still empty", mail_total("protected"), 0)


async def case_commit() -> None:
    print("\n[5] COMMIT — a passing run, then one approval opens the door")
    post(f"{LIMBO}/reset?full=1")
    post(f"{WORLDS['protected']['world']}/txn/reset")
    await onboard(WORLDS["protected"]["mcp"], GOOD_TAX)

    st = get(f"{LIMBO}/state")
    check("verdict", st["verdict"], "pass")
    check("held awaiting approval", st["count"], 3)
    check("nothing sent yet", mail_total("protected"), 0)

    print("   approving...")
    post(f"{LIMBO}/commit")

    st = get(f"{LIMBO}/state")
    check("outcome", st["outcome"], "committed")
    check("flushed", len(st["flushed"]), 3)
    check("protected inbox1", inbox(8025), 2)
    check("protected inbox2", inbox(8026), 1)
    check("protected db committed", rows("protected"), 1)
    check("transaction closed",
          get(f"{WORLDS['protected']['world']}/state")["in_transaction"], False)


def case_cors() -> None:
    """The panel is on :8081 and reads all four Mailpits cross-origin."""
    print("\n[6] CORS — the panel can actually read every inbox")
    for port in (8025, 8026, 8035, 8036):
        req = urllib.request.Request(
            f"http://localhost:{port}/api/v1/messages?limit=1",
            headers={"Origin": "http://127.0.0.1:8081"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                allow = r.headers.get("Access-Control-Allow-Origin", "")
            ok = bool(allow)
        except Exception:  # noqa: BLE001
            ok = False
            allow = "(request failed)"
        print(f"   {'PASS' if ok else 'FAIL'}  :{port} "
              f"Access-Control-Allow-Origin: {allow or 'MISSING'}")
        if not ok:
            failures.append(
                f":{port} has no CORS — start it with --api-cors \"*\" "
                "or its pane stays silently empty")


async def main() -> int:
    for name, w in WORLDS.items():
        try:
            get(f"{w['world']}/state")
        except Exception as e:  # noqa: BLE001
            print(f"{name} world not reachable at {w['world']}: {e}")
            print("Start all ten processes first (scripts/demo.sh ports).")
            return 2

    case_cors()
    await case_unprotected()
    await case_no_contamination()
    await case_protected_discard()
    await case_gate()
    await case_commit()

    print("\n" + "=" * 62)
    if failures:
        print(f"CHECKPOINT 5: FAIL — {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("CHECKPOINT 5: PASS — both worlds isolated, all paths verified")
    print("Run scripts/demo.sh reset before recording.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
