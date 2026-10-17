"""Inspect global document artifacts and own background processing jobs."""

import asyncio
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, NotRequired, Protocol, TypedDict
from uuid import uuid4

from opentalk.documents.config import DocumentConfig, load_document_config, load_indexing_config
from opentalk.documents.vector_config import VectorStoreConfig, load_rag_config
from opentalk.documents.vector_store import QdrantVectorStore, VectorStore

logger = logging.getLogger(__name__)
_DOCUMENT_ID = re.compile(r"[0-9a-f]{64}")


class DocumentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DocumentStage(TypedDict):
    status: str
    error: NotRequired[str]


class DocumentStages(TypedDict):
    parsing: DocumentStage
    indexing: DocumentStage
    vectorization: DocumentStage


class DocumentRecord(TypedDict):
    document_id: str
    name: str
    size_bytes: int
    parse_count: int
    parse_version: str | None
    status: str
    deletable: bool
    updated_at: str
    stages: DocumentStages


class DocumentManager(Protocol):
    async def enqueue(
        self,
        *,
        document_id: str,
        name: str,
        size_bytes: int,
        staged_path: Path,
    ) -> tuple[bool, str]: ...

    async def documents(self) -> list[DocumentRecord]: ...

    async def delete(self, document_id: str) -> None: ...

    async def close(self) -> None: ...


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    return fallback


def _safe_error(manifest: dict[str, Any] | None) -> str | None:
    error = manifest.get("error") if manifest else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or not message.strip():
        return None
    return message.strip()[:500]


def _stage(status: str, manifest: dict[str, Any] | None = None) -> DocumentStage:
    result: DocumentStage = {"status": status}
    error = _safe_error(manifest)
    if error:
        result["error"] = error
    return result


def _latest_manifest(paths: list[Path]) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for path in paths:
        manifest = _read_json(path)
        if manifest is None:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        candidates.append((_timestamp(manifest.get("started_at"), modified), path, manifest))
    if not candidates:
        return None
    _, path, manifest = max(candidates, key=lambda item: item[0])
    return path, manifest


def _document_record(
    path: Path,
    *,
    index_profile: str,
    embedding_profile: str,
) -> DocumentRecord:
    document_id = path.name
    source = path / "source.pdf"
    parse_manifests = list((path / "parses").glob("*/manifest.json"))
    latest = _latest_manifest(parse_manifests)
    try:
        source_size = source.stat().st_size
        source_modified = source.stat().st_mtime
    except OSError:
        source_size = 0
        source_modified = 0.0
    record: DocumentRecord = {
        "document_id": document_id,
        "name": f"{document_id[:12]}.pdf",
        "size_bytes": source_size,
        "parse_count": len(parse_manifests),
        "parse_version": None,
        "status": "uploaded",
        "deletable": True,
        "updated_at": datetime.fromtimestamp(source_modified, UTC).isoformat(),
        "stages": {
            "parsing": _stage("pending"),
            "indexing": _stage("pending"),
            "vectorization": _stage("pending"),
        },
    }
    if latest is None:
        return record

    parse_manifest_path, parse_manifest = latest
    parse_status = parse_manifest.get("status", "failed")
    record.update(
        name=parse_manifest.get("source_name") or record["name"],
        parse_version=parse_manifest.get("parse_version"),
        updated_at=parse_manifest.get("completed_at")
        or parse_manifest.get("started_at")
        or record["updated_at"],
    )
    record["stages"]["parsing"] = _stage(
        "running" if parse_status == "parsing" else parse_status,
        parse_manifest,
    )
    if parse_status == "failed":
        record["status"] = "failed"
        return record
    if parse_status != "completed":
        record["status"] = "parsing"
        return record

    parse_path = parse_manifest_path.parent
    index_manifest = _read_json(parse_path / "indexing" / index_profile / "index_manifest.json")
    if index_manifest is None:
        record["status"] = "parsed"
        return record
    index_status = index_manifest.get("status", "failed")
    record["stages"]["indexing"] = _stage(
        "running" if index_status == "preparing" else index_status,
        index_manifest,
    )
    record["updated_at"] = (
        index_manifest.get("completed_at")
        or index_manifest.get("started_at")
        or record["updated_at"]
    )
    if index_status == "failed":
        record["status"] = "failed"
        return record
    if index_status != "completed":
        record["status"] = "indexing"
        return record

    vector_manifest = _read_json(
        parse_path
        / "indexing"
        / index_profile
        / "vector_indexes"
        / embedding_profile
        / "manifest.json"
    )
    if vector_manifest is None:
        record["status"] = "indexed"
        return record
    vector_status = vector_manifest.get("status", "failed")
    record["stages"]["vectorization"] = _stage(
        "running" if vector_status == "indexing" else vector_status,
        vector_manifest,
    )
    record["updated_at"] = (
        vector_manifest.get("completed_at")
        or vector_manifest.get("started_at")
        or record["updated_at"]
    )
    if vector_status == "completed":
        record["status"] = "ready"
    elif vector_status == "failed":
        record["status"] = "failed"
    else:
        record["status"] = "vectorizing"
    return record


def list_documents(config: DocumentConfig | None = None) -> list[DocumentRecord]:
    """Return global documents using only file-side processing manifests."""
    config = config or load_document_config()
    index_profile = load_indexing_config().profile
    embedding_profile = load_rag_config().embedding.profile
    if not config.storage_root.is_dir():
        return []
    records = [
        _document_record(
            path,
            index_profile=index_profile,
            embedding_profile=embedding_profile,
        )
        for path in config.storage_root.iterdir()
        if path.is_dir() and _DOCUMENT_ID.fullmatch(path.name)
    ]
    return sorted(records, key=lambda item: item["updated_at"], reverse=True)


@dataclass
class ProcessingJob:
    job_id: str
    document_id: str
    name: str
    size_bytes: int
    staged_path: Path
    created_at: str
    status: str = "queued"
    error: str | None = None
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None


class DocumentProcessManager:
    """Own bounded subprocesses so blocking model work never enters the API loop."""

    def __init__(
        self,
        config: DocumentConfig | None = None,
        *,
        concurrency: int = 1,
        vector_store_factory: Callable[[VectorStoreConfig], VectorStore] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("Document processing concurrency must be positive.")
        self.config = config or load_document_config()
        self._semaphore = asyncio.Semaphore(concurrency)
        self._mutation_lock = asyncio.Lock()
        self._jobs: dict[str, ProcessingJob] = {}
        self._deleting: set[str] = set()
        self._vector_store_factory = vector_store_factory or QdrantVectorStore
        self._closing = False

    async def enqueue(
        self,
        *,
        document_id: str,
        name: str,
        size_bytes: int,
        staged_path: Path,
    ) -> tuple[bool, str]:
        async with self._mutation_lock:
            if self._closing:
                raise RuntimeError("Document processing is shutting down.")
            if document_id in self._deleting:
                await asyncio.to_thread(shutil.rmtree, staged_path.parent, True)
                raise DocumentError("document_busy", "The document is being deleted.")
            for job in self._jobs.values():
                if job.document_id == document_id and job.status in {"queued", "processing"}:
                    await asyncio.to_thread(shutil.rmtree, staged_path.parent, True)
                    return False, job.job_id
            existing = await asyncio.to_thread(list_documents, self.config)
            ready = next(
                (item for item in existing if item["document_id"] == document_id and item["status"] == "ready"),
                None,
            )
            if ready is not None:
                await asyncio.to_thread(shutil.rmtree, staged_path.parent, True)
                return False, ""
            job = ProcessingJob(
                job_id=uuid4().hex,
                document_id=document_id,
                name=name,
                size_bytes=size_bytes,
                staged_path=staged_path,
                created_at=datetime.now(UTC).isoformat(),
            )
            self._jobs[job.job_id] = job
            job.task = asyncio.create_task(self._run(job), name=f"document-{job.job_id}")
            return True, job.job_id

    def _delete_persisted(self, document_id: str, document_path: Path) -> None:
        rag_config = load_rag_config()
        store = self._vector_store_factory(rag_config.vector_store)
        try:
            store.delete({"document_id": document_id})
        finally:
            store.close()
        shutil.rmtree(document_path)

    async def delete(self, document_id: str) -> None:
        if _DOCUMENT_ID.fullmatch(document_id) is None:
            raise DocumentError("invalid_document_id", "The document ID is invalid.")
        async with self._mutation_lock:
            if self._closing:
                raise DocumentError("document_unavailable", "Document processing is shutting down.")
            if document_id in self._deleting or any(
                job.document_id == document_id and job.status in {"queued", "processing"}
                for job in self._jobs.values()
            ):
                raise DocumentError(
                    "document_busy",
                    "Wait for document processing to finish before deleting it.",
                )
            document_path = self.config.storage_root / document_id
            if not document_path.is_dir():
                raise DocumentError("document_not_found", "The document was not found.")
            self._deleting.add(document_id)
        try:
            deletion = asyncio.create_task(
                asyncio.to_thread(self._delete_persisted, document_id, document_path)
            )
            try:
                await asyncio.shield(deletion)
            except asyncio.CancelledError:
                await asyncio.shield(deletion)
                raise
        except Exception as error:
            logger.exception("Document %s could not be deleted.", document_id)
            raise DocumentError(
                "document_delete_failed",
                "The document could not be deleted. Check the backend and Qdrant services.",
            ) from error
        finally:
            async with self._mutation_lock:
                self._deleting.discard(document_id)
        async with self._mutation_lock:
            self._jobs = {
                job_id: job
                for job_id, job in self._jobs.items()
                if job.document_id != document_id
            }

    async def _run(self, job: ProcessingJob) -> None:
        try:
            async with self._semaphore:
                if self._closing:
                    return
                job.status = "processing"
                job.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "opentalk.documents.process",
                    str(job.staged_path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await job.process.communicate()
                if job.process.returncode == 0:
                    job.status = "completed"
                else:
                    job.status = "failed"
                    job.error = "Document processing failed. Inspect the stage manifest and backend logs."
                    output = stderr.decode("utf-8", errors="replace")[-4000:]
                    logger.error("Document job %s failed:\n%s", job.job_id, output)
        except asyncio.CancelledError:
            if job.process is not None and job.process.returncode is None:
                job.process.terminate()
                try:
                    await asyncio.wait_for(job.process.wait(), timeout=5)
                except TimeoutError:
                    job.process.kill()
                    await job.process.wait()
            raise
        except Exception:
            job.status = "failed"
            job.error = "Document processing could not be started. Inspect the backend logs."
            logger.exception("Document job %s could not be started.", job.job_id)
        finally:
            job.process = None
            await asyncio.to_thread(shutil.rmtree, job.staged_path.parent, True)

    async def documents(self) -> list[DocumentRecord]:
        records = await asyncio.to_thread(list_documents, self.config)
        by_id = {item["document_id"]: item for item in records}
        for job in self._jobs.values():
            if job.status == "completed":
                continue
            record = by_id.get(job.document_id)
            if record is None:
                new_record: DocumentRecord = {
                    "document_id": job.document_id,
                    "name": job.name,
                    "size_bytes": job.size_bytes,
                    "parse_count": 0,
                    "parse_version": None,
                    "status": "uploaded",
                    "deletable": False,
                    "updated_at": job.created_at,
                    "stages": {
                        "parsing": _stage("pending"),
                        "indexing": _stage("pending"),
                        "vectorization": _stage("pending"),
                    },
                }
                record = new_record
                records.append(record)
                by_id[job.document_id] = record
            if job.status in {"queued", "processing"}:
                record["deletable"] = False
                if job.status == "queued":
                    record["status"] = "queued"
                elif record["status"] == "parsed":
                    record["status"] = "indexing"
                    record["stages"]["indexing"] = _stage("running")
                elif record["status"] == "indexed":
                    record["status"] = "vectorizing"
                    record["stages"]["vectorization"] = _stage("running")
                elif record["status"] in {"uploaded", "ready", "failed"}:
                    record["status"] = "parsing"
                    record["stages"] = {
                        "parsing": _stage("running"),
                        "indexing": _stage("pending"),
                        "vectorization": _stage("pending"),
                    }
            elif job.status == "failed" and record["status"] not in {"failed"}:
                record["status"] = "failed"
                failed_stage = "parsing"
                if record["stages"]["parsing"]["status"] == "completed":
                    failed_stage = "indexing"
                if record["stages"]["indexing"]["status"] == "completed":
                    failed_stage = "vectorization"
                record["stages"][failed_stage] = {
                    "status": "failed",
                    "error": job.error or "Document processing failed.",
                }
        return sorted(records, key=lambda item: item["updated_at"], reverse=True)

    async def close(self) -> None:
        self._closing = True
        tasks = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
