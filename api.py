"""
Clore.ai Search & Rent — HTTP API + UI.

Wraps the existing clore_search.py functions in a FastAPI service and serves a
minimalist single-page UI from /static.

Run:
    CLORE_API_KEY=xxxxx uvicorn api:app --host 0.0.0.0 --port 8000

Open http://localhost:8000/ in a browser.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import clore_search as cs

API_KEY_ENV = "CLORE_API_KEY"
STATIC_DIR = Path(__file__).parent / "static"
SCHEDULE_FILE = Path(os.environ.get(
    "CLORE_SCHEDULE_FILE",
    str(Path(__file__).parent / "schedules.json"),
))


# ─── Auto-cancel scheduler ──────────────────────────────────────────────────
# In-memory:  order_id (int) -> {"cancel_at": ISO str, "task": asyncio.Task | None}
# Persisted to SCHEDULE_FILE (without the task object) so the API can restart
# without losing in-flight schedules.
_scheduled: dict[int, dict] = {}


def _load_schedules() -> dict:
    if not SCHEDULE_FILE.exists():
        return {}
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except Exception:
        return {}


def _save_schedules() -> None:
    data = {str(oid): {"cancel_at": v["cancel_at"]} for oid, v in _scheduled.items()}
    try:
        SCHEDULE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[scheduler] Failed to persist schedules: {e}")


async def _run_cancel(order_id: int, delay_seconds: float) -> None:
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        key = os.environ.get(API_KEY_ENV, "").strip()
        if not key:
            print(f"[scheduler] No API key; skipping auto-cancel for order {order_id}")
            return
        client = cs.CloreClient(key)
        # The Clore SDK is sync; run in a thread so we don't block the loop.
        await asyncio.to_thread(client.cancel_order, order_id, "auto-cancel after duration")
        print(f"[scheduler] Auto-cancelled order {order_id}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[scheduler] Auto-cancel failed for order {order_id}: {e}")
    finally:
        _scheduled.pop(order_id, None)
        _save_schedules()


def _schedule_cancel(order_id: int, delay_seconds: float) -> str:
    """Register an auto-cancel. Replaces any existing schedule for the same order."""
    cancel_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    existing = _scheduled.get(order_id)
    if existing and existing.get("task"):
        existing["task"].cancel()
    task = asyncio.create_task(_run_cancel(order_id, delay_seconds))
    iso = cancel_at.isoformat()
    _scheduled[order_id] = {"cancel_at": iso, "task": task}
    _save_schedules()
    return iso


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Restore persisted schedules. If their cancel_at is in the past, they fire
    # immediately (with delay=0).
    persisted = _load_schedules()
    now = datetime.now(timezone.utc)
    for oid_str, v in persisted.items():
        try:
            oid = int(oid_str)
            cancel_at = datetime.fromisoformat(v["cancel_at"])
            delay = max(0.0, (cancel_at - now).total_seconds())
            task = asyncio.create_task(_run_cancel(oid, delay))
            _scheduled[oid] = {"cancel_at": v["cancel_at"], "task": task}
            print(f"[scheduler] Restored auto-cancel for order {oid} in {delay:.0f}s")
        except Exception as e:
            print(f"[scheduler] Failed to restore schedule {oid_str}: {e}")
    yield
    # On shutdown: don't cancel tasks — they'll be persisted and restored next run.


# ─── App ────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Clore Search & Rent",
    description="HTTP wrapper over the Clore.ai marketplace search/rent CLI.",
    version="1.1.0",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _client() -> cs.CloreClient:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise HTTPException(
            status_code=401,
            detail=f"Missing API key. Set {API_KEY_ENV} env var and restart the server.",
        )
    return cs.CloreClient(key)


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


# ─── Schemas ─────────────────────────────────────────────────────────────────

class RentRequest(BaseModel):
    server_id: int = Field(..., description="Clore server ID to rent")
    order_type: Optional[str] = Field(
        None, description="'on-demand' | 'spot' | None (auto = cheapest)"
    )
    currency: Optional[str] = Field(
        None,
        description="'USD-Blockchain' | 'bitcoin' | 'CLORE-Blockchain' | None (auto)",
    )
    spot_price: Optional[float] = Field(
        None, description="Required only if order_type='spot' (native currency / day)"
    )
    image: Optional[str] = None
    ports: Optional[dict] = None
    ssh_password: Optional[str] = None
    autossh_entrypoint: Optional[str] = None
    command: Optional[str] = Field(None, description="Startup script (shell). None = use default.")
    env: Optional[dict] = None
    auto_cancel_hours: Optional[float] = Field(
        None,
        description=(
            "If set (>0), the API schedules a cancel after this many hours. "
            "Persisted to disk so it survives API restarts. None or 0 = no auto-cancel."
        ),
    )


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/defaults")
def get_defaults() -> dict:
    """Return server-side defaults so the UI can prefill its forms."""
    return _ok({
        "image": cs.DEFAULT_IMAGE,
        "ports": cs.DEFAULT_PORTS,
        "ssh_password": cs.DEFAULT_SSH_PASSWORD,
        "autossh_entrypoint": cs.DEFAULT_ENTRYPOINT,
        "currency": cs.DEFAULT_CURRENCY,
        "min_bandwidth": cs.DEFAULT_MIN_BANDWIDTH,
        "min_cuda": cs.DEFAULT_MIN_CUDA,
        "min_vram": 20,
        "min_reliability": 0,
        "command": cs.DEFAULT_STARTUP_SCRIPT,
        "currencies": ["USD-Blockchain", "bitcoin", "CLORE-Blockchain"],
        "order_types": ["on-demand", "spot"],
        "api_key_set": bool(os.environ.get(API_KEY_ENV, "").strip()),
    })


@app.get("/api/prices")
def get_prices() -> dict:
    """Current BTC/USD and CLORE/USD exchange rates (with fallbacks)."""
    return _ok({
        "btc_usd": cs.get_btc_usd_price(),
        "clore_usd": cs.get_clore_usd_price(),
    })


@app.get("/api/marketplace")
def marketplace(
    min_vram: int = Query(20, ge=0),
    min_bandwidth: int = Query(cs.DEFAULT_MIN_BANDWIDTH, ge=0),
    min_cuda: float = Query(cs.DEFAULT_MIN_CUDA, ge=0),
    min_reliability: float = Query(0.0, ge=0.0, le=100.0,
                                   description="0-100 (percent). Servers below are excluded."),
    currency: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Case-insensitive substring match on GPU name"),
) -> dict:
    """Fetch and filter the Clore marketplace."""
    if currency is not None and currency not in ("bitcoin", "CLORE-Blockchain", "USD-Blockchain"):
        raise HTTPException(400, f"Invalid currency '{currency}'")

    client = _client()
    raw = client.get_marketplace()
    filtered = cs.filter_gpu_servers(
        raw,
        min_vram_gb=min_vram,
        currency=currency,
        min_bandwidth_mbps=min_bandwidth,
        min_cuda=min_cuda,
    )

    rel_threshold = min_reliability / 100.0
    if rel_threshold > 0:
        filtered = [s for s in filtered if (s.get("reliability") or 0) >= rel_threshold]

    if search:
        needle = search.lower()
        filtered = [s for s in filtered if needle in (s.get("gpu", "") or "").lower()]

    return _ok({
        "count": len(filtered),
        "servers": filtered,
        "btc_usd": cs.get_btc_usd_price(),
        "clore_usd": cs.get_clore_usd_price(),
    })


@app.post("/api/rent")
def rent(req: RentRequest) -> dict:
    """Create a rental order. Any field left null uses the default / auto-pick.

    If `auto_cancel_hours` is set, schedules a cancel after that duration.
    """
    client = _client()

    order_type = req.order_type
    currency = req.currency
    spot_price = req.spot_price

    # Auto-pick cheapest combo for anything not explicitly given.
    if order_type is None or currency is None:
        servers = client.get_marketplace()
        best = cs.find_best_price_for_server(
            servers,
            req.server_id,
            order_type=order_type,
            currency=currency,
        )
        if best is None:
            raise HTTPException(
                404,
                f"Server #{req.server_id} not found or has no valid pricing.",
            )
        auto_type, auto_cur, _auto_usd, auto_native = best
        if order_type is None:
            order_type = auto_type
        if currency is None:
            currency = auto_cur
        if order_type == "spot" and spot_price is None:
            spot_price = auto_native

    if order_type not in ("on-demand", "spot"):
        raise HTTPException(400, f"Invalid order_type '{order_type}'")
    if currency not in ("bitcoin", "CLORE-Blockchain", "USD-Blockchain"):
        raise HTTPException(400, f"Invalid currency '{currency}'")

    result = client.create_order(
        server_id=req.server_id,
        image=req.image or cs.DEFAULT_IMAGE,
        ports=req.ports or cs.DEFAULT_PORTS,
        ssh_password=req.ssh_password or cs.DEFAULT_SSH_PASSWORD,
        currency=currency,
        order_type=order_type,
        spot_price=spot_price,
        env=req.env,
        autossh_entrypoint=req.autossh_entrypoint or cs.DEFAULT_ENTRYPOINT,
        command=req.command if req.command is not None else cs.DEFAULT_STARTUP_SCRIPT,
    )

    code = result.get("code", -1)
    if code != 0:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": result.get("error", f"Clore API error (code {code})"),
                "code": code,
                "clore_response": result,
            },
        )

    order = result.get("order", {}) or {}
    order_id = order.get("id")
    scheduled_cancel_at: Optional[str] = None

    if req.auto_cancel_hours and req.auto_cancel_hours > 0 and order_id is not None:
        scheduled_cancel_at = _schedule_cancel(int(order_id), req.auto_cancel_hours * 3600)

    return _ok({
        "order": order,
        "order_type": order_type,
        "currency": currency,
        "spot_price": spot_price,
        "scheduled_cancel_at": scheduled_cancel_at,
    })


@app.get("/api/orders")
def orders(include_completed: bool = Query(True)) -> dict:
    client = _client()
    order_list = client.get_orders(return_completed=include_completed)
    # Annotate each order with its scheduled-cancel info, if any.
    for o in order_list:
        oid = o.get("id")
        if oid is not None and int(oid) in _scheduled:
            o["scheduled_cancel_at"] = _scheduled[int(oid)]["cancel_at"]
    return _ok(order_list)


@app.delete("/api/orders/{order_id}")
def cancel_order(order_id: int, issue: Optional[str] = None) -> dict:
    client = _client()
    result = client.cancel_order(order_id, issue=issue)
    # Drop any pending auto-cancel for this order — it's done.
    entry = _scheduled.pop(order_id, None)
    if entry and entry.get("task"):
        entry["task"].cancel()
    _save_schedules()
    if result.get("code", -1) != 0:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": result.get("error", "Cancel failed"),
                     "clore_response": result},
        )
    return _ok(result)


@app.get("/api/scheduled-cancels")
def list_scheduled_cancels() -> dict:
    """List all pending auto-cancels (in-memory + persisted)."""
    out = []
    now = datetime.now(timezone.utc)
    for oid, v in _scheduled.items():
        try:
            cancel_at = datetime.fromisoformat(v["cancel_at"])
            seconds_left = (cancel_at - now).total_seconds()
        except Exception:
            seconds_left = None
        out.append({
            "order_id": oid,
            "cancel_at": v["cancel_at"],
            "seconds_left": seconds_left,
        })
    out.sort(key=lambda x: x["cancel_at"])
    return _ok(out)


@app.delete("/api/scheduled-cancels/{order_id}")
def remove_scheduled_cancel(order_id: int) -> dict:
    """Remove a pending auto-cancel WITHOUT cancelling the order itself."""
    entry = _scheduled.pop(order_id, None)
    if not entry:
        raise HTTPException(404, f"No scheduled auto-cancel for order {order_id}")
    if entry.get("task"):
        entry["task"].cancel()
    _save_schedules()
    return _ok({"removed_schedule_for_order": order_id})


@app.get("/api/wallets")
def wallets() -> dict:
    client = _client()
    return _ok(client.get_wallets())


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "api_key_set": bool(os.environ.get(API_KEY_ENV, "").strip()),
        "pending_auto_cancels": len(_scheduled),
    }


# ─── UI ──────────────────────────────────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(500, "UI not found (static/index.html missing)")
    return FileResponse(str(index_path))
