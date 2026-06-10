import sys
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, "/tmp/hermes-agent")

from adapter import (  # noqa: E402
    XalgoVoiceAdapter,
    create_event,
    format_outbound_delta,
    format_outbound_message,
    parse_event,
    parse_inbound_message,
)


class FakeConfig:
    token = "token"
    extra = {
        "token": "token",
        "instance_id": "hermes_test",
        "server_url": "wss://example.test/openclaw/connect",
    }


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def test_parse_event_accepts_valid_event():
    raw = '{"event_id":"evt_1","type":"ping","created_at":1718000000000,"payload":{"ts":1}}'
    event = parse_event(raw)
    assert event is not None
    assert event["type"] == "ping"


def test_parse_event_rejects_invalid_json():
    assert parse_event("not json") is None


def test_parse_inbound_message_accepts_xalgo_shape():
    event = create_event(
        "inbound_message",
        {
            "message_id": "msg_1",
            "session_id": "voice_session_1",
            "agent_binding_id": "agent_binding_1",
            "chat_id": "xalgo:user:u123",
            "chat_type": "direct",
            "sender": {"id": "u123", "name": "Leo"},
            "text": "hello hermes",
            "metadata": {"input_type": "voice"},
        },
    )
    message = parse_inbound_message(event)
    assert message is not None
    assert message["id"] == "msg_1"
    assert message["text"] == "hello hermes"
    assert message["conversation_id"] == "xalgo:user:u123"
    assert message["conversation_type"] == "dm"
    assert message["session_id"] == "voice_session_1"
    assert message["agent_binding_id"] == "agent_binding_1"


def test_parse_inbound_message_keeps_utterance_id():
    event = create_event(
        "inbound_message",
        {
            "message_id": "msg_1",
            "utterance_id": "utt_1",
            "chat_id": "xalgo:user:u123",
            "text": "hello hermes",
        },
    )
    message = parse_inbound_message(event)
    assert message is not None
    assert message["utterance_id"] == "utt_1"


def test_parse_inbound_message_accepts_transcript_fallback():
    event = create_event(
        "inbound_message",
        {
            "message_id": "msg_2",
            "chat_id": "xalgo:user:u123",
            "chat_type": "direct",
            "sender": {"id": "u123", "name": "Leo"},
            "metadata": {"input_type": "voice", "transcript": "from metadata"},
        },
    )
    message = parse_inbound_message(event)
    assert message is not None
    assert message["text"] == "from metadata"


def test_parse_inbound_message_rejects_empty_text():
    event = create_event(
        "inbound_message",
        {
            "message_id": "msg_3",
            "chat_id": "xalgo:user:u123",
            "chat_type": "direct",
            "sender": {"id": "u123", "name": "Leo"},
            "text": "",
            "metadata": {"input_type": "voice"},
        },
    )
    assert parse_inbound_message(event) is None


def test_format_outbound_message_voice_first():
    event = format_outbound_message(
        message_id="reply_1",
        session_id="voice_session_1",
        agent_binding_id="agent_binding_1",
        chat_id="xalgo:user:u123",
        reply_to="msg_1",
        text="hi",
        reply_mode="voice_first",
    )
    assert event["type"] == "outbound_message"
    payload = event["payload"]
    assert payload["session_id"] == "voice_session_1"
    assert payload["agent_binding_id"] == "agent_binding_1"
    assert payload["metadata"]["output_type"] == "voice_preferred"
    assert payload["metadata"]["speak"] is True


def test_format_outbound_delta():
    event = format_outbound_delta(
        message_id="reply_1",
        session_id="voice_session_1",
        agent_binding_id="agent_binding_1",
        chat_id="xalgo:user:u123",
        delta_seq=2,
        text_delta="partial",
        span_id="span_1",
        is_final=False,
    )
    assert event["type"] == "outbound_delta"
    payload = event["payload"]
    assert payload["delta_seq"] == 2
    assert payload["text_delta"] == "partial"
    assert payload["span_id"] == "span_1"
    assert payload["is_final"] is False


def test_voice_cancel_request_cancels_session_and_confirms():
    async def run():
        adapter = XalgoVoiceAdapter(FakeConfig())
        adapter._ws = FakeWebSocket()
        adapter.cancel_session_processing = AsyncMock()
        adapter._latest_reply_route_by_session["voice_session_1"] = {
            "reply_to": "msg_1",
            "chat_id": "xalgo:user:u123",
            "session_id": "voice_session_1",
            "agent_binding_id": "agent_binding_1",
            "utterance_id": "utt_1",
        }

        await adapter._handle_event(create_event(
            "voice.cancel_request",
            {
                "session_id": "voice_session_1",
                "agent_binding_id": "agent_binding_1",
                "utterance_id": "utt_1",
                "reason": "user_voice_cancel",
                "text": "Never mind, cancel that task.",
            },
        ))

        adapter.cancel_session_processing.assert_awaited_once()
        args, kwargs = adapter.cancel_session_processing.await_args
        assert args[0] == "xalgo_voice:xalgo:user:u123"
        assert kwargs == {"release_guard": True, "discard_pending": True}
        assert "msg_1" in adapter._cancelled_reply_to
        assert adapter._ws.sent[-1]["type"] == "outbound_message"
        assert adapter._ws.sent[-1]["payload"]["text"] == "已取消"
        assert adapter._ws.sent[-1]["payload"]["session_id"] == "voice_session_1"

    asyncio.run(run())


def test_voice_user_turn_routes_reply_with_session_fields():
    async def run():
        adapter = XalgoVoiceAdapter(FakeConfig())
        adapter._ws = FakeWebSocket()
        adapter.handle_message = AsyncMock()

        await adapter._handle_event(create_event(
            "voice.user_turn",
            {
                "message_id": "msg_1",
                "utterance_id": "utt_1",
                "session_id": "voice_session_1",
                "agent_binding_id": "agent_binding_1",
                "text": "hello hermes",
                "metadata": {"input_type": "voice"},
            },
        ))

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "hello hermes"
        assert event.source.chat_id == "voice_session_1"

        result = await adapter.send(event.source.chat_id, "hello back", reply_to=event.message_id)

        assert result.success is True
        assert adapter._ws.sent[-2]["type"] == "outbound_delta"
        assert adapter._ws.sent[-2]["payload"]["session_id"] == "voice_session_1"
        assert adapter._ws.sent[-2]["payload"]["agent_binding_id"] == "agent_binding_1"
        assert adapter._ws.sent[-1]["payload"]["is_final"] is True
        assert adapter._ws.sent[-1]["payload"]["session_id"] == "voice_session_1"

    asyncio.run(run())


def test_send_suppresses_cancelled_reply():
    async def run():
        adapter = XalgoVoiceAdapter(FakeConfig())
        adapter._ws = FakeWebSocket()
        adapter._remember_route({
            "id": "msg_1",
            "conversation_id": "xalgo:user:u123",
            "session_id": "voice_session_1",
            "agent_binding_id": "agent_binding_1",
            "utterance_id": "utt_1",
        })
        adapter._cancelled_reply_to.add("msg_1")

        result = await adapter.send("xalgo:user:u123", "late answer", reply_to="msg_1")

        assert result.success is True
        assert adapter._ws.sent == []

    asyncio.run(run())


def test_voice_interrupt_dot_event_uses_session_route_and_dispatches_text():
    async def run():
        adapter = XalgoVoiceAdapter(FakeConfig())
        adapter._ws = FakeWebSocket()
        adapter.interrupt_session_activity = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._latest_reply_route_by_session["voice_session_1"] = {
            "reply_to": "msg_1",
            "chat_id": "xalgo:user:u123",
            "session_id": "voice_session_1",
            "agent_binding_id": "agent_binding_1",
            "utterance_id": "utt_1",
        }

        await adapter._handle_event(create_event(
            "voice.interrupt",
            {
                "session_id": "voice_session_1",
                "agent_binding_id": "agent_binding_1",
                "utterance_id": "utt_1",
                "user_text": "Actually answer this instead.",
            },
        ))

        adapter.interrupt_session_activity.assert_awaited_once_with(
            "xalgo_voice:xalgo:user:u123",
            "xalgo:user:u123",
        )
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "Actually answer this instead."
        assert event.raw_message["session_id"] == "voice_session_1"
        result = await adapter.send("xalgo:user:u123", "new answer", reply_to=event.message_id)
        assert result.success is True
        assert adapter._ws.sent[-1]["type"] == "outbound_delta"

    asyncio.run(run())
