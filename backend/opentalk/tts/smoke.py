"""Feed text incrementally to LiveKit TTS and save arriving PCM frames as WAV."""

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiohttp

from opentalk.config import PROJECT_ROOT
from opentalk.tts.provider import TTSConfig, create_tts, load_tts_config


def save_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def synthesize_text(
    text: str, *, config: TTSConfig | None = None, output: Path | None = None,
    report_file: Path | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    config = config or load_tts_config()
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Input text must not be empty.")
    session_id = uuid4().hex
    output = (output or PROJECT_ROOT / "recordings" / f"tts-{session_id}.wav").resolve()
    report_file = (report_file or PROJECT_ROOT / "logs" / f"tts-{session_id}.json").resolve()
    partial_output = output.with_name(f"{output.stem}.partial.wav")
    if output.suffix.lower() != ".wav":
        raise ValueError("The audio output must have a .wav extension.")
    if report_file in (output, partial_output):
        raise ValueError("The report path must differ from the audio output paths.")
    started = time.perf_counter()
    message = {
        "message_id": f"{session_id}:assistant:1", "role": "assistant", "source": "tts_test",
        "generated_text": text, "submitted_text": "", "status": "running",
    }
    report = {
        "session_id": session_id, "response_id": session_id, "provider": "Soniox",
        "model": config.model, "voice": config.voice, "language": config.language,
        "sample_rate": config.sample_rate, "status": "running",
        "started_at": datetime.now(UTC).isoformat(), "messages": [message], "events": [],
        "report_file": str(report_file), "requested_audio_file": str(output), "audio_file": None,
        "text_chunks": 0, "audio_frames": 0, "audio_duration_seconds": 0.0,
        "first_audio_seconds": None, "input_ended_seconds": None,
        "audio_before_input_end": False,
    }
    temporary_audio = None
    cancelled = False

    def event(kind: str, **data) -> None:
        entry = {
            "event_id": f"{session_id}:event:{len(report['events']) + 1}", "type": kind,
            "received_at": datetime.now(UTC).isoformat(), "elapsed_seconds": time.perf_counter() - started,
            "response_id": session_id, **data,
        }
        report["events"].append(entry)
        if on_event:
            on_event(entry)

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name, suffix=".tmp", delete=False) as file:
            temporary_audio = Path(file.name)
        with wave.open(str(temporary_audio), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(config.sample_rate)
            async with aiohttp.ClientSession() as http_session:
                async with create_tts(config, http_session=http_session) as provider:
                    async with provider.stream(conn_options=config.connection_options) as stream:
                        async def feed() -> None:
                            for offset in range(0, len(text), config.chunk_chars):
                                if offset:
                                    await asyncio.sleep(config.chunk_delay_ms / 1000)
                                chunk = text[offset:offset + config.chunk_chars]
                                stream.push_text(chunk)
                                message["submitted_text"] += chunk
                                report["text_chunks"] += 1
                                event("text_chunk", message_id=message["message_id"], role="assistant", text=chunk)
                            stream.end_input()
                            report["input_ended_seconds"] = time.perf_counter() - started
                            event("input_ended")

                        async def receive() -> None:
                            async for item in stream:
                                frame = item.frame
                                if frame.sample_rate != config.sample_rate or frame.num_channels != 1:
                                    raise RuntimeError("Received audio does not match the configured PCM format.")
                                if frame.samples_per_channel <= 0:
                                    continue
                                if report["first_audio_seconds"] is None:
                                    report["first_audio_seconds"] = time.perf_counter() - started
                                    report["audio_before_input_end"] = report["input_ended_seconds"] is None
                                    event("first_audio", audio_before_input_end=report["audio_before_input_end"])
                                audio.writeframesraw(frame.data.tobytes())
                                report["audio_frames"] += 1
                                report["audio_duration_seconds"] += frame.samples_per_channel / frame.sample_rate
                                event("audio_frame", request_id=item.request_id, segment_id=item.segment_id,
                                      samples=frame.samples_per_channel, is_final=item.is_final)
                            if report["input_ended_seconds"] is None:
                                raise RuntimeError("The synthesis stream ended before text input was complete.")
                            event("stream_completed")

                        tasks = [asyncio.create_task(feed()), asyncio.create_task(receive())]
                        try:
                            async with asyncio.timeout(config.timeout_seconds):
                                await asyncio.gather(*tasks)
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
        if report["audio_frames"] == 0:
            raise RuntimeError("The synthesis stream produced no audio frames.")
        temporary_audio.replace(output)
        report["audio_file"] = str(output)
        report["status"] = "passed"
        message["status"] = "completed"
        return report
    except asyncio.CancelledError:
        cancelled = True
        report["status"] = message["status"] = "cancelled"
        event("cancelled")
        raise
    except Exception as error:
        report["status"] = message["status"] = "failed"
        report["error_type"] = type(error).__name__
        event("failed", error_type=type(error).__name__)
        raise
    finally:
        # A cancelled response has a separately named, playable partial artifact.
        # Failures preserve any existing successful output rather than replacing it.
        if temporary_audio is not None:
            if cancelled and report["audio_frames"]:
                temporary_audio.replace(partial_output)
                report["audio_file"] = str(partial_output)
            temporary_audio.unlink(missing_ok=True)
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["elapsed_seconds"] = time.perf_counter() - started
        save_report(report, report_file)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream text to Soniox TTS and save audio as WAV.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="Text to synthesize, including mixed-language content.")
    source.add_argument("--text-file", type=Path, help="UTF-8 text file to synthesize.")
    parser.add_argument("--config", type=Path, help="TTS TOML configuration path.")
    parser.add_argument("--language", help="Primary delivery language, such as zh or en.")
    parser.add_argument("--voice", help="Soniox voice name or voice ID.")
    parser.add_argument("--output", type=Path, help="WAV output path.")
    parser.add_argument("--report", type=Path, help="JSON report path.")
    parser.add_argument("--cancel-after", type=float, help="Cancel synthesis after this many seconds.")
    args = parser.parse_args()
    if args.cancel_after is not None and (args.cancel_after <= 0 or not math.isfinite(args.cancel_after)):
        parser.error("--cancel-after must be a finite positive number.")
    try:
        config = load_tts_config(args.config)
        if args.language is not None:
            if not args.language.strip():
                raise ValueError("language must not be empty.")
            config = replace(config, language=args.language)
        if args.voice is not None:
            if not args.voice.strip():
                raise ValueError("voice must not be empty.")
            config = replace(config, voice=args.voice)
        text = args.text if args.text_file is None else args.text_file.read_text(encoding="utf-8")
        if args.text_file is not None:
            input_path = args.text_file.resolve()
            if (args.output is not None and input_path in (
                args.output.resolve(), args.output.resolve().with_name(f"{args.output.stem}.partial.wav"),
            )) or (args.report is not None and input_path == args.report.resolve()):
                raise ValueError("Output paths must not overwrite the input text file.")

        async def run() -> dict:
            def display(entry: dict) -> None:
                if entry["type"] in ("first_audio", "input_ended", "stream_completed"):
                    print(json.dumps(entry, ensure_ascii=False), flush=True)

            task = asyncio.create_task(synthesize_text(
                text, config=config, output=args.output, report_file=args.report,
                on_event=display,
            ))
            timer = None
            if args.cancel_after is not None:
                timer = asyncio.get_running_loop().call_later(args.cancel_after, task.cancel)
            try:
                return await task
            finally:
                if timer:
                    timer.cancel()

        report = asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("TTS synthesis cancelled. Check the report for any partial audio artifact.", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"TTS synthesis failed ({type(error).__name__}). Check the report, credentials, and network.",
              file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({key: value for key, value in report.items() if key not in ("messages", "events")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
