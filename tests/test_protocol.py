import json

from unreal_bridge.remote_exec import (
    JsonStreamDecoder,
    RemoteExecConfig,
    decode_message,
    encode_message,
)


def test_encode_roundtrip():
    raw = encode_message("command", "me", "editor", {"command": "print('é')", "exec_mode": "ExecuteFile"})
    msg = decode_message(json.loads(raw))
    assert msg == {
        "version": 1,
        "magic": "ue_py",
        "type": "command",
        "source": "me",
        "dest": "editor",
        "data": {"command": "print('é')", "exec_mode": "ExecuteFile"},
    }


def test_ping_has_no_dest_or_data():
    msg = json.loads(encode_message("ping", "me"))
    assert "dest" not in msg and "data" not in msg


def test_decode_rejects_foreign_messages():
    assert decode_message({"version": 1, "magic": "other", "type": "ping", "source": "x"}) is None
    assert decode_message({"version": 2, "magic": "ue_py", "type": "ping", "source": "x"}) is None
    assert decode_message([1, 2]) is None


def test_stream_decoder_handles_split_and_concatenated_messages():
    a = encode_message("command_result", "e", "me", {"result": "ünïcode ✓", "success": True})
    b = encode_message("command_result", "e", "me", {"result": "two"})
    stream = a + b"\n" + b
    dec = JsonStreamDecoder()
    got = []
    for i in range(len(stream)):  # one byte at a time, splitting UTF-8 sequences too
        got += dec.feed(stream[i : i + 1])
    assert [m["data"]["result"] for m in got] == ["ünïcode ✓", "two"]
    assert dec.pending == 0


def test_stream_decoder_waits_for_incomplete_json():
    dec = JsonStreamDecoder()
    assert dec.feed(b'{"a": [1, 2') == []
    assert dec.feed(b"]}{") == [{"a": [1, 2]}]
    assert dec.feed(b'"b": 1}') == [{"b": 1}]


def test_stream_decoder_skips_garbage():
    dec = JsonStreamDecoder()
    assert dec.feed(b'\x00junk{"ok": true}') == [{"ok": True}]


def test_config_from_env():
    cfg = RemoteExecConfig.from_env(
        {
            "UNREAL_BRIDGE_MULTICAST_GROUP": "239.0.0.2:7000",
            "UNREAL_BRIDGE_MULTICAST_BIND": "0.0.0.0",
            "UNREAL_BRIDGE_COMMAND_ENDPOINT": "127.0.0.1:6776",
            "UNREAL_BRIDGE_COMMAND_TIMEOUT": "9",
            "UNREAL_BRIDGE_PROJECT": "Backrooms",
        }
    )
    assert cfg.multicast_group == ("239.0.0.2", 7000)
    assert cfg.multicast_bind_address == "0.0.0.0"
    assert (cfg.command_host, cfg.command_port) == ("127.0.0.1", 6776)
    assert cfg.command_timeout == 9.0
    assert cfg.project_filter == "Backrooms"
    assert cfg.is_multicast


def test_defaults_match_ue():
    cfg = RemoteExecConfig()
    assert cfg.multicast_group == ("239.0.0.1", 6766)
    assert cfg.multicast_ttl == 0
    assert cfg.is_multicast
    assert not RemoteExecConfig(multicast_group=("127.0.0.1", 5000)).is_multicast
