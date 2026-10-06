"""Replay a local recording through the native LiveKit STT streaming interface."""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from uuid import uuid4

import aiohttp
from livekit.agents import stt

from opentalk.asr.audio import frames_from_file, silence_frames
from opentalk.asr.provider import ASRConfig, create_stt, load_asr_config
from opentalk.config import PROJECT_ROOT


class TranscriptRecorder:
    """Keep provisional revisions separate from finalized user messages."""

    def __init__(self, session_id: str, model: str, audio_file: Path):
        self.started = time.perf_counter()
        self.current_message: dict | None = None
        self.file_finished = False
        self.input_finished = False
        self.sent_seconds = 0.0
        self.report = {
            "session_id": session_id, "provider": "Soniox", "model": model,
            "started_at": datetime.now(UTC).isoformat(), "audio_file": str(audio_file),
            "status": "running", "messages": [], "events": [],
            "processed_audio_seconds": 0.0, "first_interim_seconds": None,
            "first_final_seconds": None, "interim_updates": 0, "final_segments": 0,
        }

    def record(self, event: stt.SpeechEvent) -> dict:
        elapsed = time.perf_counter() - self.started
        entry = {
            "event_id": f"{self.report['session_id']}:event:{len(self.report['events']) + 1}",
            "type": event.type.value, "received_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": elapsed, "file_playing": not self.file_finished,
        }
        if event.recognition_usage:
            duration = event.recognition_usage.audio_duration
            self.report["processed_audio_seconds"] += duration
            entry["audio_duration_seconds"] = duration
        if event.alternatives and event.type in (
            stt.SpeechEventType.INTERIM_TRANSCRIPT,
            stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
            stt.SpeechEventType.FINAL_TRANSCRIPT,
        ):
            data = event.alternatives[0]
            is_final = event.type == stt.SpeechEventType.FINAL_TRANSCRIPT
            if self.current_message is None:
                message_id = f"{self.report['session_id']}:user:{len(self.report['messages']) + 1}"
                self.current_message = {
                    "message_id": message_id, "role": "user", "source": "asr",
                    "text": "", "revision": 0, "is_final": False,
                }
                self.report["messages"].append(self.current_message)
            message = self.current_message
            if message["text"] != data.text or message["is_final"] != is_final:
                message["revision"] += 1
            message.update(text=data.text, is_final=is_final,
                           audio_start_seconds=data.start_time, audio_end_seconds=data.end_time)
            entry.update(message_id=message["message_id"], role="user", text=data.text,
                         revision=message["revision"], is_final=is_final)
            if is_final:
                self.report["final_segments"] += 1
                if self.report["first_final_seconds"] is None:
                    self.report["first_final_seconds"] = elapsed
                self.current_message = None
            else:
                self.report["interim_updates"] += 1
                if self.report["first_interim_seconds"] is None:
                    self.report["first_interim_seconds"] = elapsed
        self.report["events"].append(entry)
        return entry

    def drained(self) -> bool:
        # Usage confirms that the service processed the tail as well as the recording.
        return (
            self.input_finished and self.current_message is None
            and self.report["processed_audio_seconds"] + 0.001 >= self.sent_seconds
        )

    def save(self, path: Path) -> None:
        self.report["elapsed_seconds"] = time.perf_counter() - self.started
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=path.name,
                suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self.report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


async def replay_file(
    audio_file: Path, *, config: ASRConfig | None = None, output: Path | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    config = config or load_asr_config()
    audio_file = audio_file.resolve()
    session_id = uuid4().hex
    output = output or PROJECT_ROOT / "logs" / f"asr-{session_id}.json"
    if output.resolve() == audio_file:
        raise ValueError("The report path must differ from the input audio path.")
    recorder = TranscriptRecorder(session_id, config.model, audio_file)
    report = recorder.report
    report["report_file"] = str(output.resolve())
    report["sample_rate"] = config.sample_rate
    report["tail_silence_seconds"] = config.tail_silence_ms / 1000
    report["audio_duration_seconds"] = 0.0
    try:
        with closing(frames_from_file(
            audio_file, sample_rate=config.sample_rate, frame_ms=config.frame_ms,
        )) as frames:
            # Reject an empty or undecodable input before opening a paid connection.
            first_frame = next(frames, None)
            if first_frame is None:
                raise ValueError("The input file contains no decodable audio samples.")
            async with aiohttp.ClientSession() as http_session:
                async with create_stt(config, http_session=http_session) as provider:
                    async with provider.stream(conn_options=config.connection_options) as stream:
                        completed = asyncio.Event()

                        async def feed() -> None:
                            playback_started = time.perf_counter()

                            async def push(frame) -> None:
                                stream.push_frame(frame)
                                recorder.sent_seconds += frame.samples_per_channel / frame.sample_rate
                                # Pace against a fixed origin to avoid cumulative sleep drift.
                                delay = playback_started + recorder.sent_seconds - time.perf_counter()
                                await asyncio.sleep(max(0.0, delay))

                            for frame in chain((first_frame,), frames):
                                await push(frame)
                                report["audio_duration_seconds"] = recorder.sent_seconds
                            recorder.file_finished = True
                            report["file_feed_finished_seconds"] = time.perf_counter() - recorder.started
                            for frame in silence_frames(
                                sample_rate=config.sample_rate, frame_ms=config.frame_ms,
                                duration_ms=config.tail_silence_ms,
                            ):
                                await push(frame)
                            stream.end_input()
                            recorder.input_finished = True
                            if recorder.drained():
                                completed.set()
                            async with asyncio.timeout(config.drain_timeout_seconds):
                                await completed.wait()

                        async def receive() -> None:
                            async for event in stream:
                                entry = recorder.record(event)
                                if on_event:
                                    on_event(entry)
                                if recorder.drained():
                                    completed.set()
                                    return
                            raise RuntimeError("The ASR stream ended before all audio was processed.")

                        tasks = [asyncio.create_task(feed()), asyncio.create_task(receive())]
                        try:
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                            # The receiver can already be waiting for its next event when EOF
                            # makes the accumulated usage sufficient. Do not wait on it forever.
                            await tasks[0]
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
        if not report["final_segments"] or not any(
            message["is_final"] and message["text"].strip() for message in report["messages"]
        ):
            raise RuntimeError("No finalized speech was recognized in the recording.")
        report["status"] = "passed"
        report["transcript"] = "".join(
            message["text"] for message in report["messages"] if message["is_final"]
        )
        return report
    except asyncio.CancelledError:
        report["status"] = "cancelled"
        raise
    except Exception as error:
        # Raw provider errors can contain authentication payloads; retain only the type.
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        recorder.save(output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream a local audio file to Soniox through LiveKit STT.")
    parser.add_argument("audio_file", type=Path, help="Local WAV, MP3, M4A, or other PyAV-decodable audio file.")
    parser.add_argument("--config", type=Path, help="ASR TOML configuration path.")
    parser.add_argument("--output", type=Path, help="JSON report path; replaces an existing report atomically.")
    args = parser.parse_args()

    def display(event: dict) -> None:
        if "text" in event:
            print(json.dumps(event, ensure_ascii=False), flush=True)

    try:
        report = asyncio.run(replay_file(
            args.audio_file, config=load_asr_config(args.config), output=args.output, on_event=display,
        ))
    except KeyboardInterrupt:
        print("ASR replay cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"ASR replay failed ({type(error).__name__}). Check the report, credentials, network, and audio input.",
              file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({key: value for key, value in report.items() if key not in ("messages", "events")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
