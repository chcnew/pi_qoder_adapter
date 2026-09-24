"""Loopback OpenAI-compatible HTTP API."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pi_qoder_adapter.bridge import BridgeError, SessionManager
from pi_qoder_adapter.catalog import (
    default_pi_models,
    openai_model_list,
    pi_model_from_info,
    resolve_model_id,
)
from pi_qoder_adapter.openai_sse import OpenAIEncoder, sse, sse_done
from pi_qoder_adapter.qoder_runtime import public_error

logger = logging.getLogger(__name__)


class ModelCatalog:
    def __init__(self, *, allow_refresh: bool = False) -> None:
        self.models = default_pi_models()
        self.allow_refresh = allow_refresh
        self.loaded_from_service = False
        self._until = 0.0
        self._lock = asyncio.Lock()

    def as_openai(self) -> dict[str, Any]:
        return openai_model_list(self.models)

    async def refresh(self, token: str) -> None:
        if not self.allow_refresh or time.time() < self._until:
            return
        async with self._lock:
            if time.time() < self._until:
                return
            try:
                infos = await asyncio.wait_for(_fetch_models(token), timeout=20)
            except Exception as exc:
                logger.info("model catalog refresh failed: %s", public_error(exc, token))
                return
            converted = [model for info in infos if (model := pi_model_from_info(info))]
            if converted:
                self.models = converted
                self.loaded_from_service = True
                self._until = time.time() + 600
                logger.info("model catalog refreshed count=%s", len(converted))

    async def resolve(self, token: str, requested: str) -> str:
        found = resolve_model_id(requested, self.models)
        if found is None and self.allow_refresh and not self.loaded_from_service:
            await self.refresh(token)
            found = resolve_model_id(requested, self.models)
        if found is not None:
            if found != requested:
                logger.info("model alias %s -> %s", requested, found)
            return found
        if self.loaded_from_service:
            choices = ", ".join(
                f"{model['id']}（{model['name']}）" for model in self.models if model.get("id")
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": f"未知模型 {requested}。请使用 Qoder 的模型 id：{choices}",
                        "type": "invalid_request_error",
                    }
                },
            )
        return requested


async def _fetch_models(token: str) -> list[dict[str, Any]]:
    from qoder_agent_sdk import QoderAgentOptions, QoderSDKClient, access_token

    options = QoderAgentOptions(
        auth=access_token(token),
        tools=[],
        skills=[],
        setting_sources=[],
        permission_mode="dontAsk",
    )
    client = QoderSDKClient(options)
    await client.connect()
    try:
        models = await client.get_available_models()
    finally:
        await client.disconnect()
    return [dict(model) for model in models]


def _bearer(request: Request, *, required: bool) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        if required:
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": "Missing bearer token", "type": "authentication_error"}},
            )
        return None
    token = header[7:].strip()
    if not token:
        if required:
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": "Missing bearer token", "type": "authentication_error"}},
            )
        return None
    return token


def create_app(
    manager: SessionManager | None = None,
    catalog: ModelCatalog | None = None,
    *,
    refresh_on_startup: bool = False,
) -> FastAPI:
    session_manager = manager or SessionManager()
    model_catalog = catalog or ModelCatalog(allow_refresh=refresh_on_startup)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        token = os.environ.get("QODER_PERSONAL_ACCESS_TOKEN", "").strip()
        refresh_task: asyncio.Task[None] | None = None
        if refresh_on_startup and token:
            refresh_task = asyncio.create_task(model_catalog.refresh(token))

        async def idle_loop() -> None:
            while True:
                await asyncio.sleep(60)
                await session_manager.close_idle()

        idle_task = asyncio.create_task(idle_loop())
        try:
            yield
        finally:
            idle_task.cancel()
            if refresh_task is not None:
                refresh_task.cancel()
            await session_manager.aclose_all()

    app = FastAPI(lifespan=lifespan)
    app.state.manager = session_manager
    app.state.catalog = model_catalog

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(exc.detail, status_code=exc.status_code)
        return JSONResponse(
            {"error": {"message": str(exc.detail), "type": "invalid_request_error"}},
            status_code=exc.status_code,
        )

    @app.exception_handler(BridgeError)
    async def bridge_error(_request: Request, exc: BridgeError) -> JSONResponse:
        error_type = "authentication_error" if exc.status == 401 else "invalid_request_error"
        if exc.status >= 500:
            error_type = "server_error"
        return JSONResponse(
            {"error": {"message": exc.message, "type": error_type}},
            status_code=exc.status,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request) -> dict[str, Any]:
        token = _bearer(request, required=False)
        if token:
            await model_catalog.refresh(token)
        return model_catalog.as_openai()

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> JSONResponse | StreamingResponse:
        token = _bearer(request, required=True)
        assert token is not None
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": "JSON object required", "type": "invalid_request_error"}},
            )
        requested = str(body.get("model") or "")
        model = await model_catalog.resolve(token, requested)
        body = {**body, "model": model}
        logger.info("chat completion model=%s stream=%s", model, bool(body.get("stream")))
        if body.get("stream"):
            queue: asyncio.Queue[bytes | None] = asyncio.Queue()
            producer = asyncio.create_task(
                _produce(queue, session_manager, token, body, model)
            )
            producer.add_done_callback(_retrieve_task_error)
            return StreamingResponse(
                _iter_encoded(queue, producer),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        encoder = OpenAIEncoder(completion_id=f"chatcmpl-{uuid.uuid4().hex}", model=model)
        async for event in session_manager.iter_semantics(token, body):
            encoder.chunks(event)
        return JSONResponse(encoder.completion())

    return app


def _retrieve_task_error(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    task.exception()


async def _produce(
    queue: asyncio.Queue[bytes | None],
    manager: SessionManager,
    token: str,
    body: dict[str, Any],
    model: str,
) -> None:
    """Run the model turn outside the HTTP streaming task.

    Qoder's CLI client uses AnyIO cancel scopes. Starlette runs the response
    body in a separate task group, and closing those scopes from that task
    raises RuntimeError after the turn has already finished.
    """
    encoder = OpenAIEncoder(completion_id=f"chatcmpl-{uuid.uuid4().hex}", model=model)
    try:
        async for event in manager.iter_semantics(token, body):
            for chunk in encoder.chunks(event):
                await queue.put(sse(chunk))
        await queue.put(sse_done())
    except BridgeError as exc:
        await queue.put(sse({"error": {"message": exc.message, "type": "server_error"}}))
        await queue.put(sse_done())
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await queue.put(sse({"error": {"message": public_error(exc, token), "type": "server_error"}}))
        await queue.put(sse_done())
    finally:
        await queue.put(None)


async def _iter_encoded(
    queue: asyncio.Queue[bytes | None],
    producer: asyncio.Task[None],
) -> AsyncIterator[bytes]:
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        if not producer.done():
            producer.cancel()
