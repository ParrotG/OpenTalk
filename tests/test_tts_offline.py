"""Test native Soniox TTS with a simulated persistent WebSocket and real PCM framing."""

import asyncio
import base64
import json
import wave
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
import pytest
from livekit.agents import APIError, APIStatusError, tts
from livekit.plugins import soniox

from opentalk.tts import provider, smoke


TEXT = (
    "Your meeting room is available tomorrow morning. "
    "Room A is ready for your team at ten o'clock. "
    "请先确认日期和时间，then we can complete your booking. "
    "The reservation has not yet been confirmed, so please review the details carefully."
)


def test_tts_credentials_and_native_interface(monkeypatch, tmp_path):
    config = replace(provider.load_tts_config(), api_key_env="TEST_TTS_KEY")
    assert config.model == "tts-rt-v2" and config.language == "zh"
    monkeypatch.setattr(provider, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env.local").write_text("TEST_TTS_KEY=file-value\n", encoding="utf-8")
    monkeypatch.setenv("TEST_TTS_KEY", "environment-value")
    model = provider.create_tts(config)
    assert isinstance(model, soniox.TTS) and isinstance(model, tts.TTS)
    assert model.capabilities.streaming
    assert model._opts.api_key == "environment-value"
    assert model._opts.audio_format == "pcm_s16le"
    assert model.sample_rate == 24000 and model.num_channels == 1
    monkeypatch.delenv("TEST_TTS_KEY")
    assert provider.create_tts(config)._opts.api_key == "file-value"
    monkeypatch.delenv("TEST_TTS_KEY")
    (tmp_path / ".env.local").unlink()
    with pytest.raises(ValueError, match="Missing required credential: TEST_TTS_KEY"):
        provider.create_tts(config)


@pytest.mark.parametrize("before,after", [
    ("wss://tts-rt.soniox.com/tts-websocket", "ws://insecure.example/tts"),
    ('voice = "Maya"', 'voice = ""'),
    ("sample_rate = 24000", "sample_rate = true"),
    ("speed = 1.0", "speed = 1.5"),
    ("connection_timeout_seconds = 15.0", "connection_timeout_seconds = nan"),
    ("stream_idle_timeout_seconds = 5.0", "stream_idle_timeout_seconds = 0.0"),
    ("chunk_chars = 12", "chunk_chars = 0"),
    ("chunk_delay_ms = 150", "chunk_delay_ms = -1"),
    ("timeout_seconds = 120.0", "timeout_seconds = inf"),
])
def test_tts_invalid_configuration(tmp_path, before, after):
    text = (provider.PROJECT_ROOT / "config/tts.toml").read_text(encoding="utf-8")
    path = tmp_path / "tts.toml"
    path.write_text(text.replace(before, after), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid TTS configuration"):
        provider.load_tts_config(path)


class FakeWebSocket:
    def __init__(self, mode="success"):
        self.mode = mode
        self.closed = False
        self.close_code = 1000
        self.messages = asyncio.Queue()
        self.requests = []
        self.pcm = []
        self.text_received = asyncio.Event()

    def respond(self, **response):
        self.messages.put_nowait(SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT, data=json.dumps(response), extra="",
        ))

    async def send_str(self, value):
        request = json.loads(value)
        self.requests.append(request)
        stream_id = request.get("stream_id")
        if request.get("cancel"):
            self.respond(stream_id=stream_id, terminated=True)
        if "text" in request:
            self.text_received.set()
            if self.mode == "error":
                self.respond(stream_id=stream_id, error_code=401, error_message="Invalid test credential.")
            elif self.mode == "success":
                pcm = b"\x01\x00" * 4800
                self.pcm.append(pcm)
                self.respond(stream_id=stream_id, audio=base64.b64encode(pcm).decode())
        if request.get("text_end") and self.mode in ("success", "empty", "abort"):
            if self.mode != "abort":
                self.respond(stream_id=stream_id, audio_end=True)
            self.respond(stream_id=stream_id, terminated=True)

    async def receive(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.closed = False
        self.connections = 0

    async def ws_connect(self, url):
        self.connections += 1
        return self.websocket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


def connection(monkeypatch, *, mode="success"):
    websocket = FakeWebSocket(mode)
    session = FakeSession(websocket)
    monkeypatch.setattr(smoke.aiohttp, "ClientSession", lambda: session)
    monkeypatch.setenv("TEST_TTS_KEY", "fake-secret-not-for-reports")
    config = replace(provider.load_tts_config(), api_key_env="TEST_TTS_KEY", chunk_chars=10,
                     chunk_delay_ms=10, timeout_seconds=2.0)
    return config, websocket, session


def test_native_stream_text_audio_overlap_and_wav(monkeypatch, tmp_path):
    config, websocket, session = connection(monkeypatch)
    output = tmp_path / "speech.wav"
    report_file = tmp_path / "report.json"
    report = asyncio.run(smoke.synthesize_text(TEXT, config=config, output=output, report_file=report_file))
    assert report["status"] == "passed"
    assert report["text_chunks"] > 2 and report["audio_frames"] > 2
    assert report["audio_before_input_end"]
    assert report["first_audio_seconds"] < report["input_ended_seconds"]
    assert report["messages"][0]["role"] == "assistant"
    assert report["messages"][0]["submitted_text"] == TEXT
    assert report["messages"][0]["generated_text"] == TEXT
    assert "spoken_text" not in report["messages"][0]
    assert "detected_language" not in report
    assert "".join(request.get("text", "") for request in websocket.requests) == TEXT
    configs = [request for request in websocket.requests if "model" in request]
    assert len(configs) == 1
    assert configs[0]["language"] == "zh" and configs[0]["model"] == "tts-rt-v2"
    assert configs[0]["audio_format"] == "pcm_s16le"
    assert websocket.requests[-1]["text_end"]
    assert websocket.closed and session.closed and session.connections == 1
    with wave.open(str(output)) as audio:
        assert audio.getnchannels() == 1 and audio.getsampwidth() == 2 and audio.getframerate() == 24000
        pcm = audio.readframes(audio.getnframes())
        assert pcm == b"".join(websocket.pcm)
        assert audio.getnframes() / 24000 == pytest.approx(report["audio_duration_seconds"])
    assert "fake-secret" not in report_file.read_text() and "api_key" not in report_file.read_text()
    smoke.save_report(report, report_file)
    smoke.save_report(report, report_file)
    assert len(json.loads(report_file.read_text())["messages"]) == 1
    assert len({event["event_id"] for event in report["events"]}) == len(report["events"])
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("mode,error", [
    ("error", APIStatusError), ("empty", APIError), ("abort", APIStatusError), ("silent", TimeoutError),
])
def test_failure_preserves_existing_audio(monkeypatch, tmp_path, mode, error):
    config, websocket, session = connection(monkeypatch, mode=mode)
    config = replace(config, chunk_delay_ms=0, timeout_seconds=0.2)
    output = tmp_path / "speech.wav"
    output.write_bytes(b"previous-successful-output")
    report_file = tmp_path / "failure.json"
    with pytest.raises(error):
        asyncio.run(smoke.synthesize_text(TEXT, config=config, output=output, report_file=report_file))
    assert output.read_bytes() == b"previous-successful-output"
    report = json.loads(report_file.read_text())
    assert report["status"] == "failed" and report["audio_file"] is None
    assert websocket.closed and session.closed and session.connections == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_cancel_has_valid_partial_wav_and_closes_connection(monkeypatch, tmp_path):
    config, websocket, session = connection(monkeypatch)
    config = replace(config, chunk_delay_ms=50)
    output = tmp_path / "speech.wav"
    output.write_bytes(b"previous-successful-output")
    report_file = tmp_path / "cancelled.json"

    async def scenario():
        task = asyncio.create_task(smoke.synthesize_text(TEXT, config=config, output=output, report_file=report_file))
        await asyncio.wait_for(websocket.text_received.wait(), timeout=2)
        # Allow the native emitter to expose its first audio frames before cancellation.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    report = json.loads(report_file.read_text())
    assert report["status"] == "cancelled"
    assert report["audio_file"] == str(tmp_path / "speech.partial.wav")
    assert output.read_bytes() == b"previous-successful-output"
    with wave.open(report["audio_file"]) as audio:
        assert audio.getnframes() > 0
    assert any(request.get("cancel") for request in websocket.requests)
    assert websocket.closed and session.closed
    assert len(report["messages"][0]["submitted_text"]) < len(TEXT)
    assert not list(tmp_path.glob("*.tmp"))


def test_reject_invalid_input_before_paid_connection(monkeypatch, tmp_path):
    config, websocket, session = connection(monkeypatch)
    with pytest.raises(ValueError, match="must not be empty"):
        asyncio.run(smoke.synthesize_text(" \n", config=config))
    with pytest.raises(ValueError, match=".wav extension"):
        asyncio.run(smoke.synthesize_text(TEXT, config=config, output=tmp_path / "speech.mp3"))
    output = tmp_path / "speech.wav"
    with pytest.raises(ValueError, match="report path must differ"):
        asyncio.run(smoke.synthesize_text(TEXT, config=config, output=output, report_file=output))
    assert session.connections == 0


def test_primary_language_switch_reuses_native_provider_connection(monkeypatch):
    config, websocket, session = connection(monkeypatch)

    async def scenario():
        async with provider.create_tts(config, http_session=session) as model:
            for language, text in (("en", "Your meeting room is ready."),
                                   ("zh", "你的会议室已准备就绪，please check Room A。")):
                model.update_options(language=language)
                async with model.stream(conn_options=config.connection_options) as stream:
                    stream.push_text(text)
                    stream.end_input()
                    audio = [item async for item in stream]
                    assert audio
    asyncio.run(scenario())
    configs = [request for request in websocket.requests if "model" in request]
    assert [config["language"] for config in configs] == ["en", "zh"]
    assert len({config["stream_id"] for config in configs}) == 2
    assert len({config["voice"] for config in configs}) == 1
    assert session.connections == 1 and websocket.closed
