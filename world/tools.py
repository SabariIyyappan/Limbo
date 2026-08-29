"""The tool server: four real tools with real side effects.

Completely Limbo-unaware. It has no idea anything might be intercepting it.
"""
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import httpx
from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from world import db  # noqa: E402

# Ports come from the environment so a second, independent copy of this world
# can run alongside the first. Defaults are the protected world's, so nothing
# changes for an existing setup.
SMTP_MAIN = ("localhost", int(os.environ.get("LIMBO_SMTP_MAIN", "1025")))
SMTP_CHANNEL = ("localhost", int(os.environ.get("LIMBO_SMTP_CHANNEL", "1026")))
PORT = int(os.environ.get("LIMBO_TOOLS_PORT", "9000"))

# The registry is stateless — it only rejects one tax ID — so both worlds
# share it safely.
REGISTRY_URL = os.environ.get(
    "LIMBO_REGISTRY", "http://127.0.0.1:7000/register"
)

mcp = FastMCP("vendor-ops")


def _send(host_port: tuple[str, int], sender: str, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(*host_port) as s:
        s.send_message(msg)


@mcp.tool
def create_vendor(name: str, tax_id: str, contact: str = "") -> str:
    """Create the vendor record in the internal vendor database.

    Use this first, before any other onboarding step.
    """
    db.begin()
    vid = db.insert_vendor(name, tax_id, contact or None)
    return f"vendor_id={vid} created for {name} (tax_id={tax_id})"


@mcp.tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email to an external recipient."""
    _send(SMTP_MAIN, "vendor-ops@company.com", to, subject, body)
    return f"email delivered to {to} (subject: {subject!r})"


@mcp.tool
def post_channel(channel: str, message: str) -> str:
    """Post an announcement to an internal company channel."""
    _send(
        SMTP_CHANNEL,
        "vendor-ops@company.com",
        f"{channel.lstrip('#')}@channels.company.com",
        f"[{channel}] vendor onboarding",
        message,
    )
    return f"posted to {channel}"


@mcp.tool
def register_vendor(name: str, tax_id: str) -> str:
    """Submit the vendor to the external federal registry for verification.

    Do this last, once the vendor is fully onboarded.
    """
    r = httpx.post(REGISTRY_URL, json={"name": name, "tax_id": tax_id}, timeout=10)
    if r.status_code != 200:
        d = r.json()
        raise RuntimeError(f"registry {r.status_code}: {d.get('detail', d)}")
    return f"registered {name} with the federal registry"


@mcp.custom_route("/state", methods=["GET"])
async def state(request):
    """Internal DB state, read through the server's own connection.

    Rows written inside the open transaction are visible here and nowhere
    else — that is the whole point of holding the transaction open.
    """
    from starlette.responses import JSONResponse as StarletteJSON

    return StarletteJSON(
        {"vendors": db.list_vendors(), "in_transaction": db.in_transaction()}
    )


@mcp.custom_route("/txn/{action}", methods=["POST"])
async def txn(request):
    """Transaction control for the verifier: commit, rollback, or reset."""
    from starlette.responses import JSONResponse as StarletteJSON

    action = request.path_params["action"]
    {"commit": db.commit, "rollback": db.rollback, "reset": db.reset}[action]()
    if action == "reset":
        db.commit()
    return StarletteJSON({"action": action, "vendors": db.list_vendors()})


if __name__ == "__main__":
    print(f"tool server :{PORT}  smtp {SMTP_MAIN[1]}/{SMTP_CHANNEL[1]}  "
          f"db {db.DB_PATH}", flush=True)
    mcp.run(transport="http", host="127.0.0.1", port=PORT)
