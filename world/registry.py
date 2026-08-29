"""Vendor registry mock. Rejects the Acme tax ID with a 422.

This is the third-party system that no amount of upfront validation can
predict: the only way to learn the tax ID is bad is to submit it.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

REJECTED_TAX_ID = "ACME-88-4417"

app = FastAPI(title="Vendor Registry (mock)")


class Registration(BaseModel):
    name: str
    tax_id: str


@app.post("/register")
def register(reg: Registration):
    if reg.tax_id == REJECTED_TAX_ID:
        return JSONResponse(
            status_code=422,
            content={
                "error": "tax_id_rejected",
                "detail": (
                    f"Tax ID {reg.tax_id} failed federal verification: "
                    "entity status is INACTIVE as of 2026-07-01."
                ),
            },
        )
    return {"status": "registered", "name": reg.name, "tax_id": reg.tax_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=7000, log_level="warning")
