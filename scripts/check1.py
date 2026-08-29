"""CHECKPOINT 1 — call all four tools over MCP, verify every real side effect."""
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402
from world import db  # noqa: E402

MCP_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9000/mcp"
TAX_ID = "ACME-88-4417"


def inbox(port: int) -> int:
    with urllib.request.urlopen(f"http://localhost:{port}/api/v1/messages") as r:
        return json.load(r)["total"]


def server_state() -> dict:
    """DB state as the tool server's own connection sees it."""
    with urllib.request.urlopen("http://127.0.0.1:9000/state") as r:
        return json.load(r)


def txn(action: str) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:9000/txn/{action}", method="POST", data=b""
    )
    urllib.request.urlopen(req).read()


def text_of(result) -> str:
    if isinstance(result, list):
        return " ".join(b.get("text", "") for b in result if isinstance(b, dict))
    return str(result)


def clear(port: int) -> None:
    req = urllib.request.Request(
        f"http://localhost:{port}/api/v1/messages", method="DELETE"
    )
    urllib.request.urlopen(req).read()


async def main() -> int:
    for p in (8025, 8026):
        clear(p)
    txn("reset")

    tools = {t.name: t for t in await (
        MultiServerMCPClient({"w": {"url": MCP_URL, "transport": "streamable_http"}})
    ).get_tools()}
    print(f"tools listed: {sorted(tools)}\n")

    ok = True

    print(await tools["create_vendor"].ainvoke(
        {"name": "Acme", "tax_id": TAX_ID, "contact": "onboarding@acme.com"}))
    print(await tools["send_email"].ainvoke(
        {"to": "onboarding@acme.com", "subject": "Portal invite", "body": "Welcome."}))
    print(await tools["send_email"].ainvoke(
        {"to": "finance@company.com", "subject": "Payment terms", "body": "Net 30."}))
    print(await tools["post_channel"].ainvoke(
        {"channel": "#procurement", "message": "Acme onboarded."}))

    try:
        reg = text_of(await tools["register_vendor"].ainvoke(
            {"name": "Acme", "tax_id": TAX_ID}))
    except Exception as e:  # adapters may raise instead of returning
        reg = f"raised: {e}"
    print(f"\nregister_vendor -> {reg[:130]}")
    if "422" not in reg:
        print("  FAIL: expected a 422 in the registry response")
        ok = False

    st = server_state()
    rows, m1, m2 = len(st["vendors"]), inbox(8025), inbox(8026)
    print(f"\ndb rows={rows} (in_transaction={st['in_transaction']})"
          f"  inbox1={m1}  inbox2={m2}")

    for label, got, want in (("db rows", rows, 1), ("inbox1", m1, 2), ("inbox2", m2, 1)):
        if got != want:
            print(f"  FAIL {label}: got {got}, want {want}")
            ok = False

    print("\nCHECKPOINT 1: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
