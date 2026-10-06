"""Exercise real Soniox plugin framing and transcription without external API calls."""

import asyncio
import json
import wave
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
import av
import pytest
from livekit.agents import APIStatusError, stt
from livekit.plugins import soniox

from opentalk.asr import provider, replay
from opentalk.asr.audio import frames_from_file


def recording(path, *, sample_rate=16000, channels=1, samples=1920):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\x01\x00" * samples * channels)
    return path


def test_asr_credentials_and_native_interface(monkeypatch, tmp_path):
    config = replace(provider.load_asr_config(), api_key_env="TEST_ASR_KEY")
    assert config.language_hints == ("zh", "en")
    (tmp_path / ".env.local").write_text("TEST_ASR_KEY=file-value\n", encoding="utf-8")
    monkeypatch.setattr(provider, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("TEST_ASR_KEY", "environment-value")
    model = provider.create_stt(config)
    assert isinstance(model, soniox.STT)
    assert isinstance(model, stt.STT)
    assert model._api_key == "environment-value"
    assert model.capabilities.streaming and model.capabilities.interim_results
    assert not model.capabilities.offline_recognize
    assert not model._params.language_hints_strict
    assert not model._params.enable_language_identification
    assert model._params.translation is None
    monkeypatch.delenv("TEST_ASR_KEY")
    assert provider.create_stt(config)._api_key == "file-value"
    monkeypatch.delenv("TEST_ASR_KEY")
    (tmp_path / ".env.local").unlink()
    with pytest.raises(ValueError, match="Missing required credential: TEST_ASR_KEY"):
        provider.create_stt(config)


@pytest.mark.parametrize("before,after", [
    ('wss://stt-rt.soniox.com/transcribe-websocket', 'ws://insecure.example/transcribe'),
    ('sample_rate = 16000', 'sample_rate = true'),
    ('["zh", "en"]', '["zh", "zh"]'),
    ('max_endpoint_delay_ms = 1500', 'max_endpoint_delay_ms = 10'),
    ('frame_ms = 20', 'frame_ms = 0'),
    ('tail_silence_ms = 3000', 'tail_silence_ms = 1000'),
    ('drain_timeout_seconds = 15.0', 'drain_timeout_seconds = nan'),
])
def test_asr_invalid_configuration(tmp_path, before, after):
    text = (provider.PROJECT_ROOT / "config/asr.toml").read_text(encoding="utf-8")
    path = tmp_path / "asr.toml"
    path.write_text(text.replace(before, after), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid ASR configuration"):
        provider.load_asr_config(path)


def test_decode_resample_downmix_and_partial_frame(tmp_path):
    path = recording(tmp_path / "stereo.wav", sample_rate=48000, channels=2, samples=4920)
    frames = list(frames_from_file(path, sample_rate=16000, frame_ms=20))
    assert sum(frame.samples_per_channel for frame in frames) == 1640
    assert frames[-1].samples_per_channel == 40
    assert all(frame.num_channels == 1 and frame.sample_rate == 16000 for frame in frames)
    assert all(len(frame.data.tobytes()) == frame.samples_per_channel * 2 for frame in frames)
    invalid = tmp_path / "invalid.wav"
    invalid.write_bytes(b"not an audio file")
    with pytest.raises(ValueError, match="Unable to decode audio file"):
        list(frames_from_file(invalid, sample_rate=16000, frame_ms=20))


def test_decode_compressed_audio_without_system_ffmpeg(tmp_path):
    path = tmp_path / "test.mp3"
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mp3", rate=16000)
        stream.layout = "mono"
        frame = av.AudioFrame(format="s16p", layout="mono", samples=1600)
        frame.sample_rate = 16000
        frame.planes[0].update(b"\x01\x00" * 1600)
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    frames = list(frames_from_file(path, sample_rate=16000, frame_ms=20))
    assert frames
    assert all(frame.sample_rate == 16000 and frame.num_channels == 1 for frame in frames)
    assert all(0 < frame.samples_per_channel <= 320 for frame in frames)


def test_preflight_remains_provisional_and_repeated_updates_do_not_duplicate_messages(tmp_path):
    recorder = replay.TranscriptRecorder("test", "test", tmp_path / "test.wav")
    data = stt.SpeechData(text="book Room A", language="")
    for _ in range(2):
        recorder.record(stt.SpeechEvent(type=stt.SpeechEventType.PREFLIGHT_TRANSCRIPT, alternatives=[data]))
    assert len(recorder.report["messages"]) == 1
    assert recorder.current_message["revision"] == 1
    assert recorder.report["final_segments"] == 0
    assert not recorder.report["messages"][0]["is_final"]
    recorder.record(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[data]))
    assert len(recorder.report["messages"]) == 1
    assert recorder.report["messages"][0]["revision"] == 2
    assert recorder.report["messages"][0]["is_final"]


class FakeWebSocket:
    def __init__(self, *, mode="success"):
        self.mode = mode
        self.messages = asyncio.Queue()
        self.configuration = None
        self.audio = []
        self.closed = False
        self.audio_received = asyncio.Event()

    async def send_str(self, value):
        content = json.loads(value)
        if "model" in content:
            self.configuration = content
            if self.mode == "error":
                self.respond(tokens=[], error_code=401, error_message="Invalid test credential.")

    def respond(self, **content):
        self.messages.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(content)))

    async def send_bytes(self, value):
        self.audio.append(value)
        self.audio_received.set()
        if self.mode == "no_speech":
            self.respond(tokens=[], total_audio_proc_ms=sum(map(len, self.audio)) / 32)
            return
        if self.mode != "success":
            return
        duration_ms = sum(map(len, self.audio)) / 32
        count = len(self.audio)
        tokens = []
        if count == 1:
            tokens = [{"text": "帮我 book Room A at nine", "is_final": False}]
        elif count == 2:
            tokens = [{"text": "帮我 book Room A at ten", "is_final": False}]
        elif count == 3:
            tokens = [{"text": "帮我 book Room A at ten.", "is_final": True,
                       "start_ms": 0, "end_ms": 60}, {"text": "<end>", "is_final": True}]
        elif count == 4:
            tokens = [{"text": "Actually，先不要确认。", "is_final": True,
                       "start_ms": 60, "end_ms": 80}, {"text": "<end>", "is_final": True}]
        self.respond(tokens=tokens, total_audio_proc_ms=duration_ms)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.messages.get()


class FakeSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.closed = False
        self.connections = 0

    async def ws_connect(self, url, **kwargs):
        self.connections += 1
        return self.websocket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


def mock_connection(monkeypatch, *, mode="success"):
    websocket = FakeWebSocket(mode=mode)
    session = FakeSession(websocket)
    monkeypatch.setenv("TEST_ASR_KEY", "fake-secret-not-for-reports")
    monkeypatch.setattr(replay.aiohttp, "ClientSession", lambda: session)
    config = replace(provider.load_asr_config(), api_key_env="TEST_ASR_KEY", frame_ms=20,
                     max_endpoint_delay_ms=500, tail_silence_ms=1000, drain_timeout_seconds=0.1)
    return config, websocket, session


def test_native_soniox_stream_revisions_finalization_and_cleanup(monkeypatch, tmp_path):
    config, websocket, session = mock_connection(monkeypatch)
    path = recording(tmp_path / "test.wav")
    output = tmp_path / "report.json"
    report = asyncio.run(replay.replay_file(path, config=config, output=output))
    assert report["status"] == "passed"
    assert report["interim_updates"] == 2
    assert report["final_segments"] == 2
    assert report["transcript"] == "帮我 book Room A at ten.Actually，先不要确认。"
    assert len(report["messages"]) == 2
    assert all(message["role"] == "user" and message["is_final"] for message in report["messages"])
    assert report["messages"][0]["revision"] == 3
    assert len({event["event_id"] for event in report["events"]}) == len(report["events"])
    assert all("language" not in message for message in report["messages"])
    assert any(event["file_playing"] for event in report["events"] if "text" in event)
    assert report["audio_duration_seconds"] == pytest.approx(0.12)
    assert report["processed_audio_seconds"] == pytest.approx(1.12)
    assert len(websocket.audio) == 56
    assert all(len(chunk) == 640 for chunk in websocket.audio)
    assert websocket.configuration["audio_format"] == "pcm_s16le"
    assert websocket.configuration["language_hints"] == ["zh", "en"]
    assert websocket.configuration["enable_endpoint_detection"]
    assert "translation" not in websocket.configuration
    assert websocket.closed and session.closed and session.connections == 1
    text = output.read_text(encoding="utf-8")
    assert "fake-secret" not in text and "api_key" not in text
    recorder = replay.TranscriptRecorder("test", "test", path)
    recorder.report = report
    recorder.save(output)
    recorder.save(output)
    assert len(json.loads(output.read_text())["messages"]) == 2
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("mode,error", [
    ("silent", TimeoutError), ("error", APIStatusError), ("no_speech", RuntimeError),
])
def test_stream_failure_is_not_success(monkeypatch, tmp_path, mode, error):
    config, websocket, session = mock_connection(monkeypatch, mode=mode)
    path = recording(tmp_path / "test.wav", samples=320)
    output = tmp_path / "failure.json"
    with pytest.raises(error):
        asyncio.run(replay.replay_file(path, config=config, output=output))
    report = json.loads(output.read_text())
    assert report["status"] == "failed"
    assert "transcript" not in report
    assert websocket.closed and session.closed
    assert session.connections == 1


def test_cancel_replay_closes_resources_and_saves_partial_report(monkeypatch, tmp_path):
    config, websocket, session = mock_connection(monkeypatch, mode="silent")
    path = recording(tmp_path / "test.wav", samples=16000)
    output = tmp_path / "cancelled.json"

    async def scenario():
        task = asyncio.create_task(replay.replay_file(path, config=config, output=output))
        await asyncio.wait_for(websocket.audio_received.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert json.loads(output.read_text())["status"] == "cancelled"
    assert websocket.closed and session.closed


def test_empty_recording_opens_no_paid_connection(monkeypatch, tmp_path):
    config, websocket, session = mock_connection(monkeypatch)
    path = recording(tmp_path / "empty.wav", samples=0)
    with pytest.raises(ValueError, match="no decodable audio samples"):
        asyncio.run(replay.replay_file(path, config=config, output=tmp_path / "empty.json"))
    assert session.connections == 0


def test_report_cannot_replace_input_audio(tmp_path):
    path = recording(tmp_path / "test.wav")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="report path must differ"):
        asyncio.run(replay.replay_file(path, output=path))
    assert path.read_bytes() == original
