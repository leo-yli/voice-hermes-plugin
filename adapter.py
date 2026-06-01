from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import httpx
except Exception:  # pragma: no cover - surfaced by check_requirements
    httpx = None

try:
    import websockets
except Exception:  # pragma: no cover - surfaced by check_requirements
    websockets = None

try:
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.session import build_session_key
except Exception:  # pragma: no cover - local unit-test fallback outside Hermes
    from enum import Enum

    @dataclass
    class PlatformConfig:
        enabled: bool = False
        token: Optional[str] = None
        api_key: Optional[str] = None
        home_channel: Any = None
        reply_to_mode: str = "first"
        gateway_restart_notification: bool = True
        extra: dict[str, Any] | None = None

    class Platform(str, Enum):
        XALGO_VOICE = "xalgo_voice"

        @classmethod
        def _missing_(cls, value):
            if value == "xalgo_voice":
                return cls.XALGO_VOICE
            return None

    class MessageType(str, Enum):
        TEXT = "text"

    @dataclass
    class SendResult:
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        raw_response: Any = None
        retryable: bool = False

    @dataclass
    class MessageEvent:
        text: str
        message_type: MessageType = MessageType.TEXT
        source: Any = None
        raw_message: Any = None
        message_id: Optional[str] = None
        timestamp: datetime = datetime.now()

    class BasePlatformAdapter:
        def __init__(self, config: PlatformConfig, platform: Platform):
            self.config = config
            self.platform = platform

        def build_source(self, **kwargs):
            return type("SessionSource", (), kwargs)()

        async def handle_message(self, event: MessageEvent) -> None:
            return None

        async def cancel_background_tasks(self) -> None:
            return None

        async def interrupt_session_activity(self, session_key: str, chat_id: str) -> None:
            return None

        def _mark_connected(self) -> None:
            return None

        def _mark_disconnected(self) -> None:
            return None

        def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
            self._fatal_error_code = code
            self._fatal_error_message = message
            self._fatal_error_retryable = retryable

    def build_session_key(source: Any) -> str:
        return f"{getattr(source, 'platform', PLATFORM_NAME)}:{getattr(source, 'chat_id', '')}"

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "2026.6.1"
PLATFORM_NAME = "xalgo_voice"
PLUGIN_NAME = "xalgo-voice-platform"
DEFAULT_REPLY_MODE = "voice_first"
DEFAULT_RECONNECT_MIN_MS = 1000
DEFAULT_RECONNECT_MAX_MS = 30000
BACKOFF_STEPS_MS = (1000, 2000, 5000, 15000, 30000)
CODE_LENGTH = 8


def _load_endpoints() -> dict[str, str]:
    path = Path(__file__).with_name("endpoints.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "apiBaseUrl": str(data.get("apiBaseUrl") or "https://asr-test.jlpay.com"),
            "serverUrl": str(data.get("serverUrl") or "wss://asr-test.jlpay.com/openclaw/connect"),
        }
    except Exception:
        return {
            "apiBaseUrl": "https://asr-test.jlpay.com",
            "serverUrl": "wss://asr-test.jlpay.com/openclaw/connect",
        }


DEFAULT_ENDPOINTS = _load_endpoints()


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _env_or_extra(extra: dict[str, Any], env_name: str, *extra_names: str, default: str = "") -> str:
    env = os.getenv(env_name, "").strip()
    if env:
        return env
    for name in extra_names:
        value = _clean_str(extra.get(name))
        if value:
            return value
    return default


def create_event(event_type: str, payload: dict[str, Any], event_id: str | None = None) -> dict[str, Any]:
    eid = event_id or f"evt_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    return {
        "event_id": eid,
        "type": event_type,
        "created_at": int(time.time() * 1000),
        "idempotency_key": f"idem_{eid}",
        "payload": payload,
    }


def parse_event(raw: str | bytes) -> dict[str, Any] | None:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("event_id"), str):
        return None
    if not isinstance(data.get("type"), str):
        return None
    if not isinstance(data.get("created_at"), (int, float)):
        return None
    if not isinstance(data.get("payload"), dict):
        return None
    return data


def _read_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_inbound_text(payload: dict[str, Any]) -> str:
    for key in ("text", "transcript", "asr_text", "asrText", "content", "query", "message"):
        value = _clean_str(payload.get(key))
        if value:
            return value
    for parent_name in ("metadata", "asr", "result"):
        parent = _read_record(payload.get(parent_name))
        for key in ("text", "transcript", "asr_text", "asrText"):
            value = _clean_str(parent.get(key))
            if value:
                return value
    return ""


def parse_inbound_message(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = _read_record(event.get("payload"))
    text = _read_inbound_text(payload)
    if not text:
        return None
    metadata = _read_record(payload.get("metadata"))
    sender = _read_record(payload.get("sender"))
    session_id = (
        _clean_str(payload.get("session_id"))
        or _clean_str(payload.get("sessionId"))
        or _clean_str(metadata.get("session_id"))
        or _clean_str(metadata.get("sessionId"))
    )
    agent_binding_id = (
        _clean_str(payload.get("agent_binding_id"))
        or _clean_str(payload.get("agentBindingId"))
        or _clean_str(metadata.get("agent_binding_id"))
        or _clean_str(metadata.get("agentBindingId"))
    )
    conversation_id = (
        _clean_str(payload.get("chat_id"))
        or _clean_str(payload.get("conversation_id"))
        or _clean_str(payload.get("conversationId"))
        or session_id
    )
    if not conversation_id:
        return None
    message_id = _clean_str(payload.get("message_id")) or f"xalgo_{uuid.uuid4().hex[:12]}"
    return {
        "id": message_id,
        "session_id": session_id,
        "agent_binding_id": agent_binding_id,
        "text": text,
        "sender_id": _clean_str(sender.get("id")) or conversation_id.rsplit(":", 1)[-1],
        "sender_name": _clean_str(sender.get("name")) or "Xalgo User",
        "conversation_id": conversation_id,
        "conversation_type": "group" if payload.get("chat_type") == "room" else "dm",
        "timestamp_ms": int(event.get("created_at") or time.time() * 1000),
        "raw": payload,
    }


def format_outbound_message(
    *,
    message_id: str,
    chat_id: str,
    reply_to: str,
    text: str,
    reply_mode: str,
    session_id: str = "",
    agent_binding_id: str = "",
) -> dict[str, Any]:
    mode = reply_mode if reply_mode in {"voice_first", "text_first", "both"} else DEFAULT_REPLY_MODE
    output_type = {
        "voice_first": "voice_preferred",
        "text_first": "text_preferred",
        "both": "both",
    }[mode]
    payload: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "reply_to": reply_to,
        "text": text,
        "risk_state": "R0",
        "is_final": True,
        "metadata": {
            "output_type": output_type,
            "priority": "normal",
            "speak": mode != "text_first",
            "phone_push": False,
        },
    }
    if session_id:
        payload["session_id"] = session_id
    if agent_binding_id:
        payload["agent_binding_id"] = agent_binding_id
    return create_event("outbound_message", payload)


def format_outbound_delta(
    *,
    message_id: str,
    chat_id: str,
    delta_seq: int,
    text_delta: str,
    span_id: str,
    is_final: bool,
    session_id: str = "",
    agent_binding_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "delta_seq": delta_seq,
        "text_delta": text_delta,
        "risk_state": "R0",
        "span_id": span_id,
        "is_final": is_final,
    }
    if session_id:
        payload["session_id"] = session_id
    if agent_binding_id:
        payload["agent_binding_id"] = agent_binding_id
    return create_event("outbound_delta", payload)


@dataclass
class XalgoVoiceSettings:
    token: str
    instance_id: str
    bound_at: str = ""
    bound_user_id: str = ""
    bound_user_name: str = ""
    device_label: str = ""
    server_url: str = DEFAULT_ENDPOINTS["serverUrl"]
    api_base_url: str = DEFAULT_ENDPOINTS["apiBaseUrl"]
    reply_mode: str = DEFAULT_REPLY_MODE
    streaming: bool = True
    reconnect_min_ms: int = DEFAULT_RECONNECT_MIN_MS
    reconnect_max_ms: int = DEFAULT_RECONNECT_MAX_MS
    resume: bool = True

    @classmethod
    def from_config(cls, config: PlatformConfig | Any) -> "XalgoVoiceSettings":
        extra = getattr(config, "extra", {}) or {}
        token = _env_or_extra(extra, "XALGO_VOICE_TOKEN", "token", default=_clean_str(getattr(config, "token", "")))
        instance_id = _env_or_extra(extra, "XALGO_VOICE_INSTANCE_ID", "instance_id", "instanceId")
        return cls(
            token=token,
            instance_id=instance_id,
            bound_at=_env_or_extra(extra, "XALGO_VOICE_BOUND_AT", "bound_at", "boundAt"),
            bound_user_id=_env_or_extra(extra, "XALGO_VOICE_BOUND_USER_ID", "bound_user_id", "boundUserId"),
            bound_user_name=_env_or_extra(extra, "XALGO_VOICE_BOUND_USER_NAME", "bound_user_name", "boundUserName"),
            device_label=_env_or_extra(
                extra,
                "XALGO_VOICE_DEVICE_LABEL",
                "device_label",
                "deviceLabel",
                default=f"Hermes on {socket.gethostname()}",
            ),
            server_url=_env_or_extra(extra, "XALGO_VOICE_SERVER_URL", "server_url", "serverUrl", default=DEFAULT_ENDPOINTS["serverUrl"]),
            api_base_url=_env_or_extra(extra, "XALGO_VOICE_API_BASE_URL", "api_base_url", "apiBaseUrl", default=DEFAULT_ENDPOINTS["apiBaseUrl"]),
            reply_mode=_env_or_extra(extra, "XALGO_VOICE_REPLY_MODE", "reply_mode", "replyMode", default=DEFAULT_REPLY_MODE),
            streaming=_truthy(os.getenv("XALGO_VOICE_STREAMING", extra.get("streaming")), True),
            reconnect_min_ms=int(extra.get("reconnect_min_ms") or extra.get("minDelayMs") or DEFAULT_RECONNECT_MIN_MS),
            reconnect_max_ms=int(extra.get("reconnect_max_ms") or extra.get("maxDelayMs") or DEFAULT_RECONNECT_MAX_MS),
            resume=_truthy(extra.get("resume"), True),
        )

    def is_bound(self) -> bool:
        return bool(self.token and self.instance_id and self.server_url)


class RestClient:
    def __init__(self, api_base_url: str) -> None:
        self.base = api_base_url.rstrip("/")

    async def exchange(self, code: str, instance_id: str, device_label: str) -> dict[str, str]:
        if httpx is None:
            raise RuntimeError("httpx is not installed")
        url = f"{self.base}/v1/openclaw/bindings/exchange"
        headers = {
            "x-plugin-version": PLUGIN_VERSION,
            "x-idempotency-key": f"idem_{int(time.time() * 1000)}_{uuid.uuid4().hex}",
        }
        body = {
            "code": code,
            "instance_id": instance_id,
            "device_label": device_label,
            "plugin_version": PLUGIN_VERSION,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=body, headers=headers)
        if response.status_code != 200:
            snippet = response.text[:200]
            try:
                problem = response.json()
                kind = problem.get("type") or "unknown"
            except Exception:
                kind = "unknown"
            raise RuntimeError(f"binding exchange failed: {kind} HTTP {response.status_code} {snippet}")
        data = response.json()
        return {
            "channel_token": _clean_str(data.get("channel_token")),
            "token_prefix": _clean_str(data.get("token_prefix")),
            "binding_id": _clean_str(data.get("binding_id")),
            "user_id": _clean_str(data.get("user_id")),
            "user_display_name": _clean_str(data.get("user_display_name")),
            "ws_url": _clean_str(data.get("ws_url")),
        }

    async def rotate(self, old_token: str, instance_id: str) -> str:
        if httpx is None:
            raise RuntimeError("httpx is not installed")
        url = f"{self.base}/v1/openclaw/bindings/rotate"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={},
                headers={"authorization": f"Bearer {old_token}", "x-instance-id": instance_id},
            )
        if response.status_code != 200:
            raise RuntimeError(f"token rotate failed: HTTP {response.status_code} {response.text[:200]}")
        return _clean_str(response.json().get("channel_token"))


class ReconnectState:
    def __init__(self, max_delay_ms: int) -> None:
        self.max_delay_ms = max_delay_ms
        self.attempt = 0
        self.connection_id = ""
        self.last_event_id = ""

    def next_delay(self) -> float:
        step = min(self.attempt, len(BACKOFF_STEPS_MS) - 1)
        delay_ms = min(BACKOFF_STEPS_MS[step], self.max_delay_ms)
        self.attempt += 1
        return delay_ms / 1000.0

    def reset(self) -> None:
        self.attempt = 0

    def can_resume(self, enabled: bool) -> bool:
        return enabled and bool(self.connection_id and self.last_event_id)

    def clear_session(self) -> None:
        self.connection_id = ""
        self.last_event_id = ""


class XalgoVoiceAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 12000

    def __init__(self, config: PlatformConfig):
        platform = Platform(PLATFORM_NAME)
        super().__init__(config=config, platform=platform)
        self.settings = XalgoVoiceSettings.from_config(config)
        self._ws: Any = None
        self._run_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._reconnect = ReconnectState(self.settings.reconnect_max_ms)
        self._status = "disconnected"
        self._reply_routes: dict[str, dict[str, str]] = {}
        self._latest_reply_route_by_chat: dict[str, dict[str, str]] = {}
        self._processed_control_events: list[str] = []
        self._missed_pongs = 0

    @property
    def name(self) -> str:
        return "Xalgo Voice"

    @property
    def enforces_own_access_policy(self) -> bool:
        return True

    async def connect(self) -> bool:
        if not self.settings.is_bound():
            logger.error("Xalgo Voice: missing token, instance_id, or server_url")
            self._set_fatal_error("config_missing", "Xalgo Voice binding is incomplete", retryable=False)
            return False
        if websockets is None:
            logger.error("Xalgo Voice: websockets package is not installed")
            self._set_fatal_error("dependency_missing", "Install websockets", retryable=False)
            return False
        if self._run_task and not self._run_task.done():
            return True
        self._stop_event.clear()
        self._run_task = asyncio.create_task(self._run_loop())
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        self._mark_disconnected()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            try:
                await self._ws.close(code=1000, reason="adapter disconnect")
            except Exception:
                pass
            self._ws = None
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        await self.cancel_background_tasks()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ):
        if not content or not content.strip():
            return SendResult(success=True)
        if self._ws is None:
            return SendResult(success=False, error="Xalgo Voice WebSocket is not connected")

        message_id = f"reply_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        route = self._route_for_reply(chat_id, reply_to, metadata)
        reply_to_id = reply_to or route.get("reply_to") or message_id

        try:
            if self.settings.streaming:
                span_id = f"span_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
                await self._send_event(format_outbound_delta(
                    message_id=message_id,
                    chat_id=chat_id,
                    delta_seq=1,
                    text_delta=content,
                    span_id=span_id,
                    is_final=False,
                    session_id=route.get("session_id", ""),
                    agent_binding_id=route.get("agent_binding_id", ""),
                ))
                await self._send_event(format_outbound_delta(
                    message_id=message_id,
                    chat_id=chat_id,
                    delta_seq=2,
                    text_delta="",
                    span_id=span_id,
                    is_final=True,
                    session_id=route.get("session_id", ""),
                    agent_binding_id=route.get("agent_binding_id", ""),
                ))
            else:
                await self._send_event(format_outbound_message(
                    message_id=message_id,
                    chat_id=chat_id,
                    reply_to=reply_to_id,
                    text=content,
                    reply_mode=self.settings.reply_mode,
                    session_id=route.get("session_id", ""),
                    agent_binding_id=route.get("agent_binding_id", ""),
                ))
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.warning("Xalgo Voice: send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm" if ":user:" in chat_id else "group", "chat_id": chat_id}

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                self._reconnect.reset()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.warning("Xalgo Voice: connection loop failed: %s", exc)
            if not self._stop_event.is_set():
                delay = self._reconnect.next_delay()
                logger.info("Xalgo Voice: reconnecting in %.1fs", delay)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def _connect_once(self) -> None:
        assert websockets is not None
        logger.info("Xalgo Voice: connecting to %s", self._safe_url(self.settings.server_url))
        async with websockets.connect(self.settings.server_url, ping_interval=None) as ws:
            self._ws = ws
            if self._reconnect.can_resume(self.settings.resume):
                await self._send_resume()
            else:
                await self._send_connect()

            async for raw in ws:
                if self._stop_event.is_set():
                    break
                event = parse_event(raw)
                if event is None:
                    logger.warning("Xalgo Voice: received malformed event")
                    continue
                await self._handle_event(event)
        self._ws = None
        self._mark_disconnected()
        self._stop_heartbeat()

    async def _send_connect(self) -> None:
        payload = {
            "protocol_version": 1,
            "client": {
                "kind": "hermes",
                "plugin": PLUGIN_NAME,
                "plugin_version": PLUGIN_VERSION,
                "instance_id": self.settings.instance_id,
                "device_name": self.settings.device_label or f"Hermes on {socket.gethostname()}",
            },
            "channel": PLATFORM_NAME,
            "auth": {"token": self.settings.token},
            "capabilities": [
                "text_message",
                "streaming_reply",
                "confirmation",
                "background_notification",
                "voice_interrupt",
                "delivery_ack",
            ],
        }
        await self._send_event(create_event("connect", payload))

    async def _send_resume(self) -> None:
        payload = {
            "connection_id": self._reconnect.connection_id,
            "last_event_id": self._reconnect.last_event_id,
            "auth": {"token": self.settings.token},
        }
        await self._send_event(create_event("resume", payload))

    async def _send_event(self, event: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("websocket is not connected")
        await self._ws.send(json.dumps(event, ensure_ascii=False))

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_id = _clean_str(event.get("event_id"))
        if event_id:
            self._reconnect.last_event_id = event_id
        event_type = _clean_str(event.get("type"))
        payload = _read_record(event.get("payload"))

        if event_type == "connected":
            self._reconnect.connection_id = _clean_str(payload.get("connection_id"))
            interval = int(payload.get("heartbeat_interval_ms") or 15000)
            self._reconnect.reset()
            self._missed_pongs = 0
            self._mark_connected()
            self._start_heartbeat(interval)
            logger.info("Xalgo Voice: authenticated connection_id=%s", self._reconnect.connection_id)
            return
        if event_type == "resumed":
            self._reconnect.reset()
            self._missed_pongs = 0
            self._mark_connected()
            self._start_heartbeat(15000)
            logger.info("Xalgo Voice: session resumed")
            return
        if event_type == "pong":
            self._missed_pongs = 0
            return
        if event_type == "ping":
            await self._send_event(create_event("pong", {"ts": payload.get("ts") or int(time.time() * 1000)}))
            return
        if event_type == "error":
            await self._handle_error_event(payload)
            return
        if event_type == "inbound_message":
            await self._handle_inbound(event)
            return
        if event_type == "voice_interrupt":
            await self._handle_voice_interrupt(event)
            return
        if event_type in {"binding_revoked", "token_rotated_notify", "binding_metadata_updated", "server_announcement"}:
            await self._handle_control_event(event_type, payload, event_id)
            return
        if event_type == "delivery_ack":
            logger.debug("Xalgo Voice: delivery ack %s", payload)

    async def _handle_inbound(self, event: dict[str, Any]) -> None:
        message = parse_inbound_message(event)
        if message is None:
            logger.warning("Xalgo Voice: inbound message has no usable text")
            return
        self._remember_route(message)
        source = self.build_source(
            chat_id=message["conversation_id"],
            chat_name=message["sender_name"],
            chat_type=message["conversation_type"],
            user_id=message["sender_id"],
            user_name=message["sender_name"],
            message_id=message["id"],
        )
        msg_event = MessageEvent(
            text=message["text"],
            message_type=MessageType.TEXT,
            source=source,
            raw_message=message["raw"],
            message_id=message["id"],
            timestamp=datetime.fromtimestamp(message["timestamp_ms"] / 1000),
        )
        logger.info("Xalgo Voice: inbound accepted id=%s chat=%s", message["id"], message["conversation_id"])
        await self.handle_message(msg_event)

    async def _handle_voice_interrupt(self, event: dict[str, Any]) -> None:
        payload = _read_record(event.get("payload"))
        chat_id = _clean_str(payload.get("chat_id"))
        text = _clean_str(payload.get("text"))
        if chat_id:
            source = self.build_source(chat_id=chat_id, chat_type="dm", user_id=chat_id.rsplit(":", 1)[-1])
            try:
                await self.interrupt_session_activity(build_session_key(source), chat_id)
            except Exception:
                logger.debug("Xalgo Voice: interrupt_session_activity failed", exc_info=True)
        if text:
            synthetic = {
                **event,
                "payload": {
                    "message_id": f"interrupt_{event.get('event_id')}",
                    "chat_id": chat_id,
                    "chat_type": "direct",
                    "sender": {"id": chat_id.rsplit(":", 1)[-1] if chat_id else "unknown", "name": "Xalgo User"},
                    "text": text,
                    "metadata": {"input_type": "voice"},
                },
            }
            await self._handle_inbound(synthetic)

    async def _handle_error_event(self, payload: dict[str, Any]) -> None:
        code = _clean_str(payload.get("code"))
        message = _clean_str(payload.get("message"))
        reason = _clean_str(payload.get("reason"))
        if code == "AUTH_FAILED":
            logger.error("Xalgo Voice: auth failed reason=%s message=%s", reason or "unknown", message)
            if self._reconnect.can_resume(self.settings.resume) and (
                reason in {"protocol_error", "resume_failed"} or "resume" in message.lower()
            ):
                self._reconnect.clear_session()
                if self._ws is not None:
                    await self._ws.close(code=1000, reason="retry fresh connect")
                return
            self._set_fatal_error("auth_failed", message or "Authentication failed", retryable=False)
            self._stop_event.set()
            self._mark_disconnected()

    async def _handle_control_event(self, event_type: str, payload: dict[str, Any], event_id: str) -> None:
        if event_id and event_id in self._processed_control_events:
            return
        if event_id:
            self._processed_control_events.append(event_id)
            self._processed_control_events = self._processed_control_events[-100:]

        if event_type == "binding_revoked":
            logger.warning("Xalgo Voice: binding revoked reason=%s", payload.get("reason"))
            self._set_fatal_error("binding_revoked", _clean_str(payload.get("message")) or "Binding revoked", retryable=False)
            self._stop_event.set()
            if self._ws is not None:
                await self._ws.close(code=4001, reason="binding revoked")
            return
        if event_type == "token_rotated_notify":
            try:
                new_token = await RestClient(self.settings.api_base_url).rotate(self.settings.token, self.settings.instance_id)
                if new_token:
                    self.settings.token = new_token
                    logger.info("Xalgo Voice: token rotated in memory; persist XALGO_VOICE_TOKEN after restart")
            except Exception as exc:
                logger.warning("Xalgo Voice: token rotate failed: %s", exc)
            return
        if event_type == "binding_metadata_updated":
            changes = _read_record(payload.get("changes"))
            if _clean_str(changes.get("device_label")):
                self.settings.device_label = _clean_str(changes.get("device_label"))
            return
        if event_type == "server_announcement":
            logger.info("Xalgo Voice announcement [%s] %s: %s", payload.get("level"), payload.get("title"), payload.get("body"))

    def _start_heartbeat(self, interval_ms: int) -> None:
        self._stop_heartbeat()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(max(1000, interval_ms) / 1000.0))

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None

    async def _heartbeat_loop(self, interval: float) -> None:
        while not self._stop_event.is_set() and self._ws is not None:
            await asyncio.sleep(interval)
            self._missed_pongs += 1
            if self._missed_pongs > 3:
                logger.warning("Xalgo Voice: heartbeat timeout")
                try:
                    await self._ws.close(code=4000, reason="heartbeat timeout")
                except Exception:
                    pass
                return
            await self._send_event(create_event("ping", {"ts": int(time.time() * 1000)}))

    def _remember_route(self, message: dict[str, Any]) -> None:
        route = {
            "reply_to": message["id"],
            "session_id": message.get("session_id") or "",
            "agent_binding_id": message.get("agent_binding_id") or "",
        }
        self._reply_routes[message["id"]] = route
        self._latest_reply_route_by_chat[message["conversation_id"]] = route
        if len(self._reply_routes) > 500:
            for key in list(self._reply_routes)[:100]:
                self._reply_routes.pop(key, None)

    def _route_for_reply(self, chat_id: str, reply_to: str | None, metadata: dict[str, Any] | None) -> dict[str, str]:
        route = dict(self._reply_routes.get(reply_to or "", {}) or self._latest_reply_route_by_chat.get(chat_id, {}) or {})
        if metadata:
            route["session_id"] = _clean_str(metadata.get("session_id")) or route.get("session_id", "")
            route["agent_binding_id"] = _clean_str(metadata.get("agent_binding_id")) or route.get("agent_binding_id", "")
        return route

    @staticmethod
    def _safe_url(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def check_requirements() -> bool:
    return httpx is not None and websockets is not None


def validate_config(config) -> bool:
    return XalgoVoiceSettings.from_config(config).is_bound()


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> dict | None:
    token = os.getenv("XALGO_VOICE_TOKEN", "").strip()
    instance_id = os.getenv("XALGO_VOICE_INSTANCE_ID", "").strip()
    if not (token and instance_id):
        return None
    seed: dict[str, Any] = {
        "token": token,
        "instance_id": instance_id,
        "server_url": os.getenv("XALGO_VOICE_SERVER_URL", DEFAULT_ENDPOINTS["serverUrl"]).strip(),
        "api_base_url": os.getenv("XALGO_VOICE_API_BASE_URL", DEFAULT_ENDPOINTS["apiBaseUrl"]).strip(),
        "device_label": os.getenv("XALGO_VOICE_DEVICE_LABEL", f"Hermes on {socket.gethostname()}").strip(),
        "bound_user_id": os.getenv("XALGO_VOICE_BOUND_USER_ID", "").strip(),
        "bound_user_name": os.getenv("XALGO_VOICE_BOUND_USER_NAME", "").strip(),
        "reply_mode": os.getenv("XALGO_VOICE_REPLY_MODE", DEFAULT_REPLY_MODE).strip(),
        "streaming": _truthy(os.getenv("XALGO_VOICE_STREAMING"), True),
    }
    bound_name = seed.get("bound_user_name") or seed.get("bound_user_id") or "Xalgo Voice"
    seed["home_channel"] = {
        "chat_id": os.getenv("XALGO_VOICE_HOME_CHANNEL", "xalgo:user:default"),
        "name": os.getenv("XALGO_VOICE_HOME_CHANNEL_NAME", bound_name),
    }
    return seed


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    data = yaml_cfg.get(PLATFORM_NAME) if isinstance(yaml_cfg, dict) else None
    if not isinstance(data, dict):
        return None
    extra = platform_cfg.setdefault("extra", {})
    for key in (
        "token",
        "instance_id",
        "instanceId",
        "server_url",
        "serverUrl",
        "api_base_url",
        "apiBaseUrl",
        "device_label",
        "deviceLabel",
        "reply_mode",
        "replyMode",
        "streaming",
    ):
        if key in data:
            extra[key] = data[key]
    return extra


def interactive_setup() -> None:
    from hermes_cli.setup import (
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_yes_no,
        save_env_value,
    )

    print_header("Xalgo Voice")
    existing = get_env_value("XALGO_VOICE_TOKEN")
    if existing:
        print_info("Xalgo Voice is already bound.")
        if not prompt_yes_no("Reconfigure Xalgo Voice?", False):
            return

    api_base_url = prompt(
        "Xalgo REST API base URL",
        default=get_env_value("XALGO_VOICE_API_BASE_URL") or DEFAULT_ENDPOINTS["apiBaseUrl"],
    ).strip() or DEFAULT_ENDPOINTS["apiBaseUrl"]
    code = prompt("8-character binding code from Xalgo App").strip().upper()
    if len(code) != CODE_LENGTH or not code.isalnum():
        print_warning("Binding code must be 8 alphanumeric characters.")
        return

    instance_id = get_env_value("XALGO_VOICE_INSTANCE_ID") or f"hermes_{uuid.uuid4()}"
    device_label = prompt(
        "Device label",
        default=get_env_value("XALGO_VOICE_DEVICE_LABEL") or f"Hermes on {socket.gethostname()}",
    ).strip() or f"Hermes on {socket.gethostname()}"

    async def _run_exchange() -> dict[str, str]:
        return await RestClient(api_base_url).exchange(code, instance_id, device_label)

    print_info("Exchanging binding code...")
    try:
        response = asyncio.run(_run_exchange())
    except Exception as exc:
        print_warning(f"Binding failed: {exc}")
        return

    token = response.get("channel_token", "")
    if not token:
        print_warning("Binding response did not contain channel_token.")
        return

    save_env_value("XALGO_VOICE_TOKEN", token)
    save_env_value("XALGO_VOICE_INSTANCE_ID", instance_id)
    save_env_value("XALGO_VOICE_BOUND_AT", datetime.now().isoformat())
    save_env_value("XALGO_VOICE_BOUND_USER_ID", response.get("user_id", ""))
    save_env_value("XALGO_VOICE_BOUND_USER_NAME", response.get("user_display_name", ""))
    save_env_value("XALGO_VOICE_DEVICE_LABEL", device_label)
    save_env_value("XALGO_VOICE_API_BASE_URL", api_base_url)
    save_env_value("XALGO_VOICE_SERVER_URL", response.get("ws_url") or DEFAULT_ENDPOINTS["serverUrl"])
    save_env_value("XALGO_VOICE_STREAMING", get_env_value("XALGO_VOICE_STREAMING") or "true")

    print_success("Xalgo Voice binding saved to ~/.hermes/.env")
    print_info("Enable plugin xalgo-voice-platform and restart the Hermes gateway.")


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list[str]] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    settings = XalgoVoiceSettings.from_config(pconfig)
    if not settings.is_bound():
        return {"error": "Xalgo Voice is not bound"}
    if websockets is None:
        return {"error": "websockets is not installed"}
    try:
        async with websockets.connect(settings.server_url, ping_interval=None) as ws:
            connect = create_event("connect", {
                "protocol_version": 1,
                "client": {
                    "kind": "hermes",
                    "plugin": PLUGIN_NAME,
                    "plugin_version": PLUGIN_VERSION,
                    "instance_id": settings.instance_id,
                    "device_name": settings.device_label,
                },
                "channel": PLATFORM_NAME,
                "auth": {"token": settings.token},
                "capabilities": ["text_message", "background_notification"],
            })
            await ws.send(json.dumps(connect, ensure_ascii=False))
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            event = parse_event(raw)
            if not event or event.get("type") not in {"connected", "resumed"}:
                return {"error": "Xalgo Voice standalone send authentication failed"}
            message_id = f"cron_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
            outbound = format_outbound_message(
                message_id=message_id,
                chat_id=chat_id,
                reply_to=message_id,
                text=message,
                reply_mode=settings.reply_mode,
            )
            await ws.send(json.dumps(outbound, ensure_ascii=False))
            return {"success": True, "platform": PLATFORM_NAME, "chat_id": chat_id, "message_id": message_id}
    except Exception as exc:
        return {"error": f"Xalgo Voice standalone send failed: {exc}"}


def register(ctx) -> None:
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Xalgo Voice",
        adapter_factory=lambda cfg: XalgoVoiceAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["XALGO_VOICE_TOKEN", "XALGO_VOICE_INSTANCE_ID"],
        install_hint="pip install httpx websockets",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="XALGO_VOICE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="XALGO_VOICE_ALLOWED_USERS",
        allow_all_env="XALGO_VOICE_ALLOW_ALL_USERS",
        max_message_length=XalgoVoiceAdapter.MAX_MESSAGE_LENGTH,
        emoji="🎙️",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are talking through Xalgo smart glasses voice. Keep replies concise, "
            "spoken-friendly, and avoid long code blocks unless explicitly requested."
        ),
    )
