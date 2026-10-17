"""Verify that optional retrieval initialization cannot block LiveKit process startup."""

import threading
from types import SimpleNamespace


def test_worker_prewarm_starts_retrieval_initialization_in_background(monkeypatch):
    from opentalk.voice import worker

    started = threading.Event()
    release = threading.Event()

    class BlockingRetrieval:
        def warmup(self):
            started.set()
            release.wait(timeout=2)

    retrieval = BlockingRetrieval()
    monkeypatch.setattr(worker, "LazyRetrievalService", lambda: retrieval)
    monkeypatch.setattr(worker, "create_vad", lambda configuration: "vad")
    process = SimpleNamespace(userdata={})

    worker.prewarm(process)

    preload = process.userdata["retrieval_preload"]
    assert started.wait(timeout=1)
    assert process.userdata["vad"] == "vad"
    assert process.userdata["retrieval"] is retrieval
    assert preload.is_alive()

    release.set()
    preload.join(timeout=1)
    assert not preload.is_alive()
