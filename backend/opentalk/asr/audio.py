"""Decode local audio incrementally into short mono PCM16 LiveKit frames."""

from collections.abc import Iterator
from pathlib import Path

import av
from livekit import rtc
from livekit.agents.utils.audio import AudioByteStream


def frames_from_file(path: Path, *, sample_rate: int, frame_ms: int) -> Iterator[rtc.AudioFrame]:
    if not path.is_file():
        raise ValueError(f"Audio file does not exist: {path}")
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ValueError("The input file has no audio track.")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
            chunks = AudioByteStream(
                sample_rate=sample_rate, num_channels=1,
                samples_per_channel=sample_rate * frame_ms // 1000,
            )
            for decoded in container.decode(container.streams.audio[0]):
                for converted in resampler.resample(decoded):
                    # The plane may contain padding, which is not part of the audio samples.
                    yield from chunks.write(bytes(converted.planes[0])[:converted.samples * 2])
            for converted in resampler.resample(None):
                yield from chunks.write(bytes(converted.planes[0])[:converted.samples * 2])
            yield from chunks.flush()
    except (av.FFmpegError, OSError) as error:
        raise ValueError(f"Unable to decode audio file: {path.name}") from error


def silence_frames(*, sample_rate: int, frame_ms: int, duration_ms: int) -> Iterator[rtc.AudioFrame]:
    remaining = sample_rate * duration_ms // 1000
    frame_samples = sample_rate * frame_ms // 1000
    while remaining:
        count = min(remaining, frame_samples)
        yield rtc.AudioFrame(
            data=bytes(count * 2), sample_rate=sample_rate,
            num_channels=1, samples_per_channel=count,
        )
        remaining -= count
