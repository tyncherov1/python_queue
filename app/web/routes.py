from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import segno
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.queue_service import QueueService

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter()


class TimerAddPayload(BaseModel):
    seconds: int = Field(default=60, ge=-3600, le=3600)


class SettingsUpdatePayload(BaseModel):
    seminar_title: str | None = None
    timer_duration_sec: int | None = Field(default=None, ge=10, le=7200)
    rejoin_cooldown_minutes: int | None = Field(default=None, ge=0, le=1440)
    cancel_cooldown_minutes: int | None = Field(default=None, ge=0, le=120)
    queue_open: bool | None = None
    auto_start_timer: bool | None = None
    board_visible_queue: int | None = Field(default=None, ge=1, le=32)


class AdminInsertPayload(BaseModel):
    student_id: int | None = None
    custom_name: str | None = None
    position: int | None = Field(default=None, ge=1, le=200)


class AdminMovePayload(BaseModel):
    new_position: int = Field(..., ge=1, le=200)


class ClearQueuePayload(BaseModel):
    reset_cooldowns: bool = False


class PinVerifyPayload(BaseModel):
    pin: str = ""


def _get_service(request: Request) -> QueueService:
    return request.app.state.queue_service


def _require_board_pin(
    request: Request,
    x_board_pin: str | None = Header(default=None),
) -> None:
    expected = request.app.state.settings.board_pin
    if not expected:
        return
    if (x_board_pin or "").strip() != expected:
        raise HTTPException(status_code=403, detail="Неверный или отсутствующий PIN-код доски")


# ----------------------------------------------------------------------
# Static Whiteboard & Health
# ----------------------------------------------------------------------
@router.get("/", include_in_schema=False)
async def serve_board() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    svc: QueueService = _get_service(request)
    snap = await svc.get_board_snapshot()
    return {
        "status": "ok",
        "bot_connected": snap["bot_connected"],
        "queue_length": snap["queue_length"],
        "ws_clients": svc.ws_manager.connection_count,
    }


# ----------------------------------------------------------------------
# Public Read Endpoints & WebSocket
# ----------------------------------------------------------------------
@router.get("/api/state")
async def api_get_state(svc: QueueService = Depends(_get_service)) -> dict[str, Any]:
    return await svc.get_board_snapshot()


@router.get("/api/students")
async def api_list_students(
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> list[dict[str, Any]]:
    students = await svc.db.list_roster_students()
    return [s.to_dict() for s in students]


@router.post("/api/pin/verify")
async def api_verify_pin(payload: PinVerifyPayload, request: Request) -> dict[str, bool]:
    expected = request.app.state.settings.board_pin
    if not expected:
        return {"valid": True, "pin_required": False}
    return {"valid": payload.pin.strip() == expected, "pin_required": True}


@router.get("/api/qr.svg")
async def api_bot_qr(svc: QueueService = Depends(_get_service)) -> Response:
    username = svc.bot_username
    if not username:
        raise HTTPException(status_code=404, detail="Bot username is not configured")
    url = f"https://t.me/{username}"
    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=5, border=2, dark="#f8fafc", light=None)
    return Response(
        content=buf.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.websocket("/ws")
async def websocket_board(websocket: WebSocket) -> None:
    svc: QueueService = websocket.app.state.queue_service
    await svc.ws_manager.connect(websocket)
    try:
        # Immediately send full state snapshot on connect
        snapshot = await svc.get_board_snapshot()
        await websocket.send_json({"type": "state", "data": snapshot})

        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket error: %s", exc)
    finally:
        await svc.ws_manager.disconnect(websocket)


# ----------------------------------------------------------------------
# Protected Whiteboard Controls (Timer + Queue Management)
# ----------------------------------------------------------------------
@router.post("/api/timer/start")
async def api_timer_start(
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    try:
        return await svc.timer_start()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/timer/pause")
async def api_timer_pause(
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    return await svc.timer_pause()


@router.post("/api/timer/reset")
async def api_timer_reset(
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    return await svc.timer_reset()


@router.post("/api/timer/add")
async def api_timer_add(
    payload: TimerAddPayload,
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    return await svc.timer_add_seconds(payload.seconds)


@router.post("/api/queue/next")
async def api_queue_next(
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    res = await svc.call_next(mark_current_as="done")
    snap = await svc.get_board_snapshot()
    return {"ok": res.ok, "message": res.message, "state": snap}


@router.post("/api/queue/skip")
async def api_queue_skip(
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    res = await svc.call_next(mark_current_as="skipped")
    snap = await svc.get_board_snapshot()
    return {"ok": res.ok, "message": res.message, "state": snap}


@router.post("/api/queue/finish")
async def api_queue_finish(
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    res = await svc.finish_current()
    snap = await svc.get_board_snapshot()
    return {"ok": res.ok, "message": res.message, "state": snap}


@router.post("/api/queue/insert")
async def api_queue_insert(
    payload: AdminInsertPayload,
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    res = await svc.admin_insert_student(
        student_id=payload.student_id,
        custom_name=payload.custom_name,
        position=payload.position,
    )
    if not res.ok:
        raise HTTPException(status_code=400, detail=res.message)
    return {"ok": True, "message": res.message, "state": await svc.get_board_snapshot()}


@router.post("/api/queue/{entry_id}/move")
async def api_queue_move(
    entry_id: int,
    payload: AdminMovePayload,
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    res = await svc.admin_move_entry(entry_id, payload.new_position)
    if not res.ok:
        raise HTTPException(status_code=400, detail=res.message)
    return {"ok": True, "message": res.message, "state": await svc.get_board_snapshot()}


@router.delete("/api/queue/{entry_id}")
async def api_queue_delete(
    entry_id: int,
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    res = await svc.admin_remove_entry(entry_id)
    if not res.ok:
        raise HTTPException(status_code=400, detail=res.message)
    return {"ok": True, "message": res.message, "state": await svc.get_board_snapshot()}


@router.post("/api/queue/clear")
async def api_queue_clear(
    payload: ClearQueuePayload,
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    res = await svc.clear_queue(reset_cooldowns=payload.reset_cooldowns)
    return {"ok": res.ok, "message": res.message, "state": await svc.get_board_snapshot()}


@router.patch("/api/settings")
async def api_update_settings(
    payload: SettingsUpdatePayload,
    svc: QueueService = Depends(_get_service),
    _: None = Depends(_require_board_pin),
) -> dict[str, Any]:
    return await svc.update_settings(
        seminar_title=payload.seminar_title,
        timer_duration_sec=payload.timer_duration_sec,
        rejoin_cooldown_minutes=payload.rejoin_cooldown_minutes,
        cancel_cooldown_minutes=payload.cancel_cooldown_minutes,
        queue_open=payload.queue_open,
        auto_start_timer=payload.auto_start_timer,
        board_visible_queue=payload.board_visible_queue,
    )


# ----------------------------------------------------------------------
# Telegram Webhook Endpoint (used when BOT_MODE=webhook)
# ----------------------------------------------------------------------
@router.post("/webhook/telegram", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    expected = request.app.state.settings.resolved_webhook_secret
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    bot_runtime = request.app.state.bot_runtime
    await bot_runtime.feed_webhook_update(payload)
    return {"ok": True}
