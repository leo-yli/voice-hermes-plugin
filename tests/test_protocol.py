import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, "/tmp/hermes-agent")

from adapter import (  # noqa: E402
    create_event,
    format_outbound_delta,
    format_outbound_message,
    parse_event,
    parse_inbound_message,
)


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
