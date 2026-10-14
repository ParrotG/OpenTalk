import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from opentalk.documents import pipeline
from opentalk.documents.config import DocumentConfig
from opentalk.documents.library import DocumentProcessManager, list_documents
from opentalk.sessions import config as session_settings
from opentalk.sessions.api import create_app
from opentalk.sessions.store import SessionStore


def configuration(tmp_path: Path, *, max_file_size_bytes: int = 1024) -> DocumentConfig:
    return DocumentConfig(
        storage_root=tmp_path / "documents",
        max_file_size_bytes=max_file_size_bytes,
        max_pages=20,
        document_timeout_seconds=30.0,
        images_scale=1.0,
        table_mode="accurate",
        do_ocr=True,
        do_picture_description=True,
        do_picture_classification=True,
        do_formula_enrichment=True,
        do_code_enrichment=True,
        do_chart_extraction=False,
    )


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_document_listing_uses_latest_stage_manifests(tmp_path):
    config = configuration(tmp_path)
    document_id = "a" * 64
    document = config.storage_root / document_id
    document.mkdir(parents=True)
    (document / "source.pdf").write_bytes(b"%PDF-1.7\nfixture")
    parse = document / "parses" / "docling-new"
    write_json(
        parse / "manifest.json",
        {
            "status": "completed",
            "document_id": document_id,
            "parse_version": "docling-new",
            "source_name": "manual.pdf",
            "started_at": "2026-10-14T10:00:00+00:00",
            "completed_at": "2026-10-14T10:01:00+00:00",
        },
    )
    index = parse / "indexing" / "hierarchical-v2"
    write_json(
        index / "index_manifest.json",
        {
            "status": "completed",
            "started_at": "2026-10-14T10:01:00+00:00",
            "completed_at": "2026-10-14T10:02:00+00:00",
        },
    )
    vector_manifest = (
        index / "vector_indexes" / "multilingual-mpnet-base-v2-v1" / "manifest.json"
    )
    write_json(
        vector_manifest,
        {
            "status": "completed",
            "started_at": "2026-10-14T10:02:00+00:00",
            "completed_at": "2026-10-14T10:03:00+00:00",
        },
    )

    records = list_documents(config)

    assert len(records) == 1
    assert records[0]["name"] == "manual.pdf"
    assert records[0]["status"] == "ready"
    assert records[0]["parse_version"] == "docling-new"
    assert records[0]["stages"] == {
        "parsing": {"status": "completed"},
        "indexing": {"status": "completed"},
        "vectorization": {"status": "completed"},
    }

    write_json(
        vector_manifest,
        {"status": "indexing", "started_at": "2026-10-14T10:04:00+00:00"},
    )
    active = list_documents(config)[0]
    assert active["status"] == "vectorizing"
    assert active["stages"]["vectorization"] == {"status": "running"}


def test_document_pipeline_composes_independent_stages_and_closes_store(tmp_path, monkeypatch):
    calls = []
    parse_path = tmp_path / "parse"
    index_path = parse_path / "index"
    vector_manifest = index_path / "vector" / "manifest.json"
    parsed = SimpleNamespace(document_id="a" * 64, parse_version="v1", parse_path=parse_path)
    prepared = SimpleNamespace(output_path=index_path)
    indexed = SimpleNamespace(
        manifest_path=vector_manifest,
        collection="documents",
        point_count=7,
    )
    provider = object()

    class Store:
        def __init__(self, config):
            calls.append(("store", config))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(
        pipeline,
        "parse_pdf",
        lambda path, *, config: calls.append(("parse", path, config)) or parsed,
    )
    monkeypatch.setattr(
        pipeline,
        "prepare_index",
        lambda path, *, config: calls.append(("prepare", path, config)) or prepared,
    )
    monkeypatch.setattr(
        pipeline,
        "create_embedding_provider",
        lambda config: calls.append(("provider", config)) or provider,
    )
    monkeypatch.setattr(pipeline, "QdrantVectorStore", Store)
    monkeypatch.setattr(
        pipeline,
        "index_prepared_nodes",
        lambda path, **options: calls.append(("index", path, options)) or indexed,
    )
    document_config = configuration(tmp_path)
    indexing_config = SimpleNamespace(profile="test")
    rag_config = SimpleNamespace(
        embedding=SimpleNamespace(batch_size=8),
        vector_store=SimpleNamespace(collection="documents"),
    )

    result = pipeline.process_pdf(
        tmp_path / "paper.pdf",
        document_config=document_config,
        indexing_config=indexing_config,
        rag_config=rag_config,
    )

    assert result.document_id == "a" * 64
    assert result.point_count == 7
    assert [call[0] for call in calls] == [
        "parse",
        "prepare",
        "provider",
        "store",
        "index",
        "close",
    ]


class FakeDocumentManager:
    def __init__(self):
        self.items = []
        self.upload = None
        self.closed = False

    async def documents(self):
        return self.items

    async def enqueue(self, **upload):
        self.upload = upload
        self.items = [
            {
                "document_id": upload["document_id"],
                "name": upload["name"],
                "size_bytes": upload["size_bytes"],
                "parse_count": 0,
                "parse_version": None,
                "status": "queued",
                "updated_at": "2026-10-14T10:00:00+00:00",
                "stages": {
                    "parsing": {"status": "pending"},
                    "indexing": {"status": "pending"},
                    "vectorization": {"status": "pending"},
                },
            }
        ]
        return True, "test-job"

    async def close(self):
        self.closed = True


def test_document_api_streams_one_pdf_and_reports_queue_state(tmp_path):
    import asyncio

    async def scenario():
        document_config = configuration(tmp_path)
        manager = FakeDocumentManager()
        session_config = session_settings.load_session_config()
        store = SessionStore(tmp_path / "sessions.sqlite3")
        app = create_app(
            configuration=session_config,
            store=store,
            document_config=document_config,
            document_manager=manager,
        )
        async with TestClient(TestServer(app)) as client:
            form = FormData()
            content = b"%PDF-1.7\napi fixture"
            form.add_field(
                "file",
                BytesIO(content),
                filename="研究 report.pdf",
                content_type="application/pdf",
            )
            response = await client.post("/documents", data=form)
            assert response.status == 202
            body = await response.json()
            assert body["accepted"] is True
            assert body["job_id"] == "test-job"
            assert body["document"]["status"] == "queued"
            assert manager.upload["staged_path"].read_bytes() == content
            assert manager.upload["name"] == "研究 report.pdf"
            listing = await (await client.get("/documents")).json()
            assert listing["items"][0]["document_id"] == manager.upload["document_id"]
            assert listing["max_file_size_bytes"] == 1024
        assert manager.closed is True

    asyncio.run(scenario())


def test_document_api_rejects_invalid_and_oversized_uploads(tmp_path):
    import asyncio

    async def scenario():
        manager = FakeDocumentManager()
        app = create_app(
            configuration=session_settings.load_session_config(),
            store=SessionStore(tmp_path / "sessions.sqlite3"),
            document_config=configuration(tmp_path, max_file_size_bytes=10),
            document_manager=manager,
        )
        async with TestClient(TestServer(app)) as client:
            invalid = FormData()
            invalid.add_field("file", BytesIO(b"not pdf"), filename="notes.txt")
            assert (await client.post("/documents", data=invalid)).status == 400
            large = FormData()
            large.add_field("file", BytesIO(b"%PDF-" + b"x" * 20), filename="large.pdf")
            assert (await client.post("/documents", data=large)).status == 413
            assert manager.upload is None

    asyncio.run(scenario())


def test_document_manager_owns_and_terminates_processing_subprocess(tmp_path, monkeypatch):
    import asyncio

    class BlockingProcess:
        def __init__(self):
            self.returncode = None
            self.started = asyncio.Event()
            self.terminated = False

        async def communicate(self):
            self.started.set()
            await asyncio.Future()

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def scenario():
        process = BlockingProcess()

        async def create_subprocess(*args, **options):
            assert args[1:3] == ("-m", "opentalk.documents.process")
            assert options["stderr"] == asyncio.subprocess.PIPE
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
        config = configuration(tmp_path)
        staged = config.storage_root / ".uploads" / "test" / "paper.pdf"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"%PDF-1.7\nfixture")
        manager = DocumentProcessManager(config)
        accepted, job_id = await manager.enqueue(
            document_id="b" * 64,
            name="paper.pdf",
            size_bytes=staged.stat().st_size,
            staged_path=staged,
        )
        assert accepted is True and job_id
        await process.started.wait()
        documents = await manager.documents()
        assert documents[0]["status"] == "parsing"
        await manager.close()
        assert process.terminated is True
        assert not staged.parent.exists()

    asyncio.run(scenario())
