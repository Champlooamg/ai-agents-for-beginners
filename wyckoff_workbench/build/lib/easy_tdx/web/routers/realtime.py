"""实时数据 WebSocket 路由。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])


@router.websocket("/ws/realtime/{symbol}")
async def realtime_websocket(websocket: WebSocket, symbol: str) -> None:
    """WebSocket 实时行情订阅。

    连接后自动订阅指定标的的实时事件。
    symbol 格式: SZ000001, SH600000 等。

    客户端可发送 JSON 消息来控制订阅：
    - {"action": "subscribe", "symbol": "SZ000001"}
    - {"action": "unsubscribe", "symbol": "SZ000001"}

    服务端推送消息格式：
    - {"type": "tick", "market": "SZ", "code": "000001", "price": 10.5, ...}
    - {"type": "signal", "direction": "BUY", ...}
    """
    await websocket.accept()
    logger.info("WebSocket client connected for symbol: %s", symbol)

    # Try to get EventBus from app state
    event_bus = getattr(websocket.app.state, "event_bus", None)

    subscribed_symbols: set[str] = {symbol.upper()}

    async def _on_event(event: Any) -> None:
        """EventBus 回调 → 推送 WebSocket 消息。"""
        event_symbol = f"{event.market}{event.code}"
        if event_symbol in subscribed_symbols:
            try:
                msg = {
                    "type": event.event_type.value,
                    "market": event.market,
                    "code": event.code,
                    "price": event.price,
                    "volume": event.volume,
                    "timestamp": event.timestamp,
                    "data": event.data,
                }
                await websocket.send_json(msg)
            except Exception:
                logger.warning("Failed to send WebSocket message")

    # Subscribe to event bus if available
    if event_bus is not None:
        event_bus.subscribe_all(_on_event)

    try:
        while True:
            # Receive client messages (subscribe/unsubscribe control)
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                data = json.loads(raw)
                action = data.get("action", "")

                if action == "subscribe":
                    new_symbol = data.get("symbol", "").upper()
                    if new_symbol:
                        subscribed_symbols.add(new_symbol)
                        await websocket.send_json(
                            {"type": "status", "msg": f"subscribed {new_symbol}"}
                        )

                elif action == "unsubscribe":
                    old_symbol = data.get("symbol", "").upper()
                    subscribed_symbols.discard(old_symbol)
                    await websocket.send_json(
                        {"type": "status", "msg": f"unsubscribed {old_symbol}"}
                    )

            except asyncio.TimeoutError:
                # Send heartbeat ping
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "msg": "invalid JSON"})

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected: %s", symbol)
    except Exception:
        logger.exception("WebSocket error for %s", symbol)
    finally:
        if event_bus is not None:
            event_bus.unsubscribe(symbol.upper(), _on_event)
        logger.info("WebSocket connection closed: %s", symbol)
