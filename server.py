# encoding: utf-8
import asyncio
import logging
import os
import time
from asyncio import wait_for
from typing import Optional

import fastapi.logger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi_utils.tasks import repeat_every
from pydantic import BaseModel
import socketio
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from constants import KASPAD_WRPC_URL
from dbsession import async_session, async_session_blocks
from helper.StrictRoute import StrictRoute
from helper.LimitUploadSize import LimitUploadSize
from kaspad.KaspadMultiClient import KaspadMultiClient
from kaspad.KaspadRpcClient import kaspad_rpc_client
from models.Block import Block
from models.BlockTransaction import BlockTransaction

fastapi.logger.logger.setLevel(logging.WARNING)

_logger = logging.getLogger(__name__)

app = FastAPI(
    title="Stokes REST-API server",
    description="REST-API server supporting block, tx and address search, using stokesd and the indexer db.",
    version=os.getenv("VERSION") or "dev",
    contact={"name": "Stokes Network"},
    license_info={"name": "MIT LICENSE"},
    swagger_ui_parameters={"tryItOutEnabled": True},
)
app.router.route_class = StrictRoute

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
sio_app = socketio.ASGIApp(socketio_server=sio, other_asgi_app=app, socketio_path="ws/socket.io")

_socket_subscriptions: dict[str, set[str]] = {}
_socket_last_virtual_parent: str | None = None
_socketio_task: asyncio.Task | None = None


class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.method in ("GET", "HEAD") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "public, max-age=8"
        return response


app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(LimitUploadSize, max_upload_size=200_000)  # ~1MB

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Data-Source", "X-Page-Count", "X-Next-Page-After", "X-Next-Page-Before"],
)

app.add_middleware(CacheControlMiddleware)

socket_app = sio_app


class KaspadStatus(BaseModel):
    is_online: bool = False
    is_wrpc: bool = False
    server_version: Optional[str] = None
    is_utxo_indexed: Optional[bool] = None
    is_synced: Optional[bool] = None


class DatabaseStatus(BaseModel):
    is_online: bool = False


class PingResponse(BaseModel):
    kaspad: KaspadStatus = KaspadStatus()
    database: DatabaseStatus = DatabaseStatus()


@app.get("/ping", include_in_schema=False, response_model=PingResponse)
async def ping_server():
    """
    Ping Pong
    """
    result = PingResponse()

    rpc_client = await kaspad_rpc_client()
    if rpc_client:
        result.kaspad.is_wrpc = True
        try:
            info = await wait_for(rpc_client.get_info(), 10)
            result.kaspad.is_online = True
            result.kaspad.server_version = info["serverVersion"]
            result.kaspad.is_utxo_indexed = info["isUtxoIndexed"]
            result.kaspad.is_synced = info["isSynced"]
        except Exception as err:
            _logger.error(f"Kaspad health check failed {str(err)}")

    elif kaspad_client.kaspads:
        try:
            info = await kaspad_client.kaspads[0].request("getInfoRequest")
            result.kaspad.is_online = True
            result.kaspad.server_version = info["getInfoResponse"]["serverVersion"]
            result.kaspad.is_utxo_indexed = info["getInfoResponse"]["isUtxoIndexed"]
            result.kaspad.is_synced = info["getInfoResponse"]["isSynced"]
        except Exception as err:
            _logger.error("Kaspad health check failed %s", err)

    if os.getenv("SQL_URI") is not None:
        async with async_session() as session:
            try:
                await session.execute("SELECT 1")
                result.database.is_online = True
            except Exception as err:
                _logger.error("Database health check failed %s", err)

    if not result.database.is_online or not result.kaspad.is_synced:
        return JSONResponse(status_code=503, content=result.dict())

    return result


async def _get_mempool_size() -> int:
    rpc_client = await kaspad_rpc_client()
    if rpc_client:
        try:
            info = await wait_for(rpc_client.get_info(), 10)
            return int(info.get("mempoolSize") or 0)
        except Exception:
            return 0

    try:
        info = await kaspad_client.request("getInfoRequest")
        return int(info.get("getInfoResponse", {}).get("mempoolSize") or 0)
    except Exception:
        return 0


async def _get_last_blocks(limit: int = 20) -> list[dict]:
    try:
        async with async_session_blocks() as s:
            rows = (
                await s.execute(
                    select(
                        Block.hash.label("block_hash"),
                        Block.timestamp.label("timestamp"),
                        Block.blue_score.label("blueScore"),
                        func.count(BlockTransaction.transaction_id).label("txCount"),
                    )
                    .select_from(Block)
                    .join(BlockTransaction, BlockTransaction.block_hash == Block.hash, isouter=True)
                    .group_by(Block.hash)
                    .order_by(Block.blue_score.desc())
                    .limit(limit)
                )
            ).all()

        def _to_epoch_ms(value) -> int:
            if value is None:
                return 0
            try:
                # If the DB stores milliseconds already.
                n = int(value)
                if n > 10_000_000_000:
                    return n
                # Seconds -> milliseconds.
                if n > 0:
                    return n * 1000
            except Exception:
                pass

            try:
                # Datetime-like values.
                return int(value.timestamp() * 1000)
            except Exception:
                return 0

        return [
            {
                "block_hash": r.block_hash,
                "timestamp": str(_to_epoch_ms(r.timestamp)),
                "blueScore": int(r.blueScore or 0),
                "txCount": int(r.txCount or 0),
            }
            for r in rows
        ]
    except Exception:
        rows = []

    # Fallback when DB/indexer is not configured/populated: walk back from the
    # virtual selected parent using RPC so the explorer can show recent blocks.
    try:
        rpc_client = await kaspad_rpc_client()
        blocks: list[dict] = []

        if rpc_client:
            bdi = await wait_for(rpc_client.get_block_dag_info(), 10)
            virtual_parent_hashes = bdi.get("virtualParentHashes") or []
            current = virtual_parent_hashes[0] if virtual_parent_hashes else None

            while current and len(blocks) < limit:
                try:
                    resp = await wait_for(rpc_client.get_block({"hash": current, "includeTransactions": False}), 10)
                    block = resp.get("block") or {}
                    hdr = block.get("header") or {}

                    ts = hdr.get("timestamp")
                    bs = hdr.get("blueScore")

                    blocks.append(
                        {
                            "block_hash": current,
                            "timestamp": str(int(ts)) if ts is not None else str(int(time.time() * 1000)),
                            "blueScore": int(bs or 0),
                            "txCount": 0,
                        }
                    )

                    parents = hdr.get("parents") or []
                    parent_hashes = (parents[0] or {}).get("parentHashes") if parents else None
                    current = parent_hashes[0] if parent_hashes else None
                except Exception:
                    break

            return blocks

        # gRPC fallback client
        bdi_resp = await kaspad_client.request("getBlockDagInfoRequest")
        bdi = bdi_resp.get("getBlockDagInfoResponse") or {}
        virtual_parent_hashes = bdi.get("virtualParentHashes") or []
        current = virtual_parent_hashes[0] if virtual_parent_hashes else None

        while current and len(blocks) < limit:
            try:
                block_resp = await kaspad_client.request(
                    "getBlockRequest", {"hash": current, "includeTransactions": False}
                )
                block = (block_resp.get("getBlockResponse") or {}).get("block") or {}
                hdr = block.get("header") or {}

                ts = hdr.get("timestamp")
                bs = hdr.get("blueScore")

                blocks.append(
                    {
                        "block_hash": current,
                        "timestamp": str(int(ts)) if ts is not None else str(int(time.time() * 1000)),
                        "blueScore": int(bs or 0),
                        "txCount": 0,
                    }
                )

                parents = hdr.get("parents") or []
                parent_hashes = (parents[0] or {}).get("parentHashes") if parents else None
                current = parent_hashes[0] if parent_hashes else None
            except Exception:
                break

        return blocks
    except Exception:
        return []


@sio.event
async def connect(sid, environ, auth):
    _socket_subscriptions[sid] = set()

    global _socketio_task
    if _socketio_task is None or _socketio_task.done():
        _socketio_task = asyncio.create_task(_socketio_loop())


@sio.event
async def disconnect(sid):
    _socket_subscriptions.pop(sid, None)


@sio.on("join-room")
async def join_room(sid, room: str):
    subs = _socket_subscriptions.setdefault(sid, set())
    subs.add(room)


@sio.on("last-blocks")
async def last_blocks(sid, _payload):
    blocks = await _get_last_blocks(20)
    await sio.emit("last-blocks", blocks, to=sid)


async def _emit_to_subscribers(event: str, payload):
    for sid, rooms in list(_socket_subscriptions.items()):
        if event == "new-block" and "blocks" in rooms:
            await sio.emit("new-block", payload, to=sid)
        elif event == "bluescore" and "bluescore" in rooms:
            await sio.emit("bluescore", payload, to=sid)
        elif event == "mempool" and "mempool" in rooms:
            await sio.emit("mempool", payload, to=sid)


async def _socketio_loop():
    global _socket_last_virtual_parent

    while True:
        if not _socket_subscriptions:
            await asyncio.sleep(0.5)
            continue

        rpc_client = await kaspad_rpc_client()

        try:
            if rpc_client:
                bdi = await wait_for(rpc_client.get_block_dag_info(), 10)
                virtual_parent_hashes = bdi.get("virtualParentHashes") or []
                current_parent = virtual_parent_hashes[0] if virtual_parent_hashes else None

                blue_score = int(bdi.get("blueScore") or 0)

                await _emit_to_subscribers("bluescore", {"blueScore": blue_score})
                await _emit_to_subscribers("mempool", await _get_mempool_size())

                if current_parent and current_parent != _socket_last_virtual_parent:
                    _socket_last_virtual_parent = current_parent
                    payload = {
                        "block_hash": current_parent,
                        "timestamp": str(int(time.time() * 1000)),
                        "blueScore": blue_score,
                        "txCount": 0,
                    }
                    try:
                        block = await wait_for(
                            rpc_client.get_block({"hash": current_parent, "includeTransactions": False}), 10
                        )
                        b = block.get("block") or {}
                        hdr = b.get("header") or {}
                        ts = hdr.get("timestamp")
                        if ts is not None:
                            try:
                                payload["timestamp"] = str(int(ts))
                            except Exception:
                                pass
                    except Exception:
                        pass

                    await _emit_to_subscribers("new-block", payload)

                await asyncio.sleep(2)
                continue

            bdi_resp = await kaspad_client.request("getBlockDagInfoRequest")
            bdi = bdi_resp.get("getBlockDagInfoResponse") or {}
            virtual_parent_hashes = bdi.get("virtualParentHashes") or []
            current_parent = virtual_parent_hashes[0] if virtual_parent_hashes else None

            try:
                vsp_resp = await kaspad_client.request("getSinkBlueScoreRequest")
                vsp = vsp_resp.get("getSinkBlueScoreResponse") or {}
                blue_score = int(vsp.get("blueScore") or 0)
            except Exception:
                blue_score = int(bdi.get("blueScore") or 0)

            await _emit_to_subscribers("bluescore", {"blueScore": blue_score})
            await _emit_to_subscribers("mempool", await _get_mempool_size())

            if current_parent and current_parent != _socket_last_virtual_parent:
                _socket_last_virtual_parent = current_parent
                payload = {
                    "block_hash": current_parent,
                    "timestamp": str(int(time.time() * 1000)),
                    "blueScore": blue_score,
                    "txCount": 0,
                }
                try:
                    block_resp = await kaspad_client.request(
                        "getBlockRequest", {"hash": current_parent, "includeTransactions": False}
                    )
                    block = (block_resp.get("getBlockResponse") or {}).get("block") or {}
                    hdr = (block.get("header") or {}) if isinstance(block, dict) else {}
                    ts = hdr.get("timestamp")
                    if ts is not None:
                        try:
                            payload["timestamp"] = str(int(ts))
                        except Exception:
                            pass
                except Exception:
                    pass

                await _emit_to_subscribers("new-block", payload)

        except Exception:
            pass

        await asyncio.sleep(2)


kaspad_hosts = []

for i in range(100):
    try:
        kaspad_hosts.append(os.environ[f"KASPAD_HOST_{i + 1}"].strip())
    except KeyError:
        try:
            if i == 0:
                try:
                    kaspad_hosts.append(os.environ["STOKESD_HOST"].strip())
                except KeyError:
                    kaspad_hosts.append(os.environ["STOKESD_HOST_1"].strip())
            else:
                kaspad_hosts.append(os.environ[f"STOKESD_HOST_{i + 1}"].strip())
        except KeyError:
            break

if not kaspad_hosts and not KASPAD_WRPC_URL:
    raise Exception("Please set KASPAD_WRPC_URL or KASPAD_HOST_1 (or STOKESD_HOST) environment variable.")

kaspad_client = KaspadMultiClient(kaspad_hosts)


@app.exception_handler(Exception)
async def unicorn_exception_handler(request: Request, exc: Exception):
    await kaspad_client.initialize_all()
    return JSONResponse(
        status_code=500,
        content={
            "message": "Internal server error"
            # "traceback": f"{traceback.format_exception(exc)}"
        },
    )


@app.on_event("startup")
@repeat_every(seconds=60)
async def periodical_blockdag():
    await kaspad_client.initialize_all()
