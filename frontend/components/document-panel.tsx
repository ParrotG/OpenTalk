'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { FileText, LoaderCircle, RefreshCw, Upload, X } from 'lucide-react';
import { SavedDocument, control, errorMessage, uploadDocument } from '@/lib/control';

function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function updated(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? 'Unknown date'
    : parsed.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
}

const stageLabels = {
  parsing: 'Parse',
  indexing: 'Structure',
  vectorization: 'Vectors',
} as const;

export function DocumentPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [documents, setDocuments] = useState<SavedDocument[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [maxUploadBytes, setMaxUploadBytes] = useState<number | null>(null);
  const [loadError, setLoadError] = useState('');
  const [uploadError, setUploadError] = useState('');
  const polling = useRef(false);
  const input = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async (showLoading = false) => {
    if (polling.current) return;
    polling.current = true;
    if (showLoading) setLoading(true);
    try {
      const result = await control<{ items: SavedDocument[]; max_file_size_bytes: number }>(
        'documents',
      );
      setDocuments(result.items);
      setMaxUploadBytes(result.max_file_size_bytes);
      setLoadError('');
    } catch (error) {
      setLoadError(errorMessage(error));
    } finally {
      polling.current = false;
      if (showLoading) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void refresh(true);
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [open, refresh]);

  async function upload(file: File) {
    if (uploading) return;
    setUploadError('');
    if (maxUploadBytes !== null && file.size > maxUploadBytes) {
      setUploadError(`The PDF exceeds the ${bytes(maxUploadBytes)} upload limit.`);
      return;
    }
    setUploading(true);
    try {
      const result = await uploadDocument(file);
      setDocuments((current) => [
        result.document,
        ...current.filter((item) => item.document_id !== result.document.document_id),
      ]);
      await refresh();
    } catch (error) {
      setUploadError(errorMessage(error));
    } finally {
      setUploading(false);
      if (input.current) input.current.value = '';
    }
  }

  return (
    <aside
      className={`document-panel ${open ? 'open' : ''}`}
      aria-label="Documents"
      aria-hidden={!open}
    >
      <div className="document-panel-header">
        <div>
          <h2>Documents</h2>
          <p>Global PDF knowledge</p>
        </div>
        <button
          className="icon-button document-close"
          aria-label="Close documents"
          onClick={onClose}
        >
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      <div className="document-actions">
        <input
          className="sr-only"
          ref={input}
          type="file"
          accept="application/pdf,.pdf"
          disabled={uploading}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void upload(file);
          }}
        />
        <button
          className="upload-button"
          disabled={uploading}
          onClick={() => input.current?.click()}
        >
          {uploading ? (
            <LoaderCircle className="spin" size={17} aria-hidden="true" />
          ) : (
            <Upload size={17} aria-hidden="true" />
          )}
          {uploading ? 'Uploading…' : 'Upload PDF'}
        </button>
        <button
          className="icon-button"
          disabled={loading}
          aria-label="Refresh documents"
          title="Refresh documents"
          onClick={() => void refresh(true)}
        >
          <RefreshCw className={loading ? 'spin' : ''} size={16} aria-hidden="true" />
        </button>
      </div>
      {(uploadError || loadError) && (
        <p className="document-panel-error" role="alert">
          {uploadError || loadError}
        </p>
      )}
      <div className="document-list" aria-live="polite">
        {loading && !documents.length ? (
          <p className="caption">Loading documents…</p>
        ) : documents.length ? (
          documents.map((document) => (
            <article className="document-card" key={document.document_id}>
              <div className="document-summary">
                <span className="document-icon">
                  <FileText size={17} aria-hidden="true" />
                </span>
                <div>
                  <h3 title={document.name}>{document.name}</h3>
                  <p>
                    {bytes(document.size_bytes)} · {updated(document.updated_at)}
                  </p>
                </div>
                <span className={`document-status ${document.status}`}>{document.status}</span>
              </div>
              <div className="document-stages">
                {(Object.keys(stageLabels) as (keyof typeof stageLabels)[]).map((name) => {
                  const stage = document.stages[name];
                  return (
                    <span
                      className={`document-stage ${stage.status}`}
                      key={name}
                      title={stage.error}
                    >
                      <span className="dot" aria-hidden="true" />
                      {stageLabels[name]}
                      <span className="sr-only">: {stage.status}</span>
                    </span>
                  );
                })}
              </div>
              <p className="document-meta">
                <code>{document.document_id.slice(0, 12)}</code>
                <span>
                  {document.parse_count} {document.parse_count === 1 ? 'parse' : 'parses'}
                </span>
              </p>
              {Object.values(document.stages).find((stage) => stage.error)?.error && (
                <p className="document-error">
                  {Object.values(document.stages).find((stage) => stage.error)?.error}
                </p>
              )}
            </article>
          ))
        ) : (
          <div className="document-empty">
            <FileText size={24} aria-hidden="true" />
            <p>No documents yet.</p>
            <span>Upload a PDF to parse and index it for conversations.</span>
          </div>
        )}
      </div>
    </aside>
  );
}
