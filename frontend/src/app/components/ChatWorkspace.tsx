"use client";

import { DragEvent, useEffect, useRef, useState } from "react";
import {
  BarChart3,
  Check,
  CircleHelp,
  Database,
  FileSpreadsheet,
  FileText,
  HardDriveUpload,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import Chat, {
  type ChatMessage,
  type PolicyMatch,
  type SqlRecord,
} from "./Chat";

type AuditPayload = {
  final_response?: string;
  follow_up?: boolean;
  sql_query?: string;
  sql_results?: { data?: SqlRecord[] };
  rag_results?: { results?: PolicyMatch[] };
  policy_sources_included?: boolean;
};
type UploadPayload = {
  detail?: string;
  filename?: string;
  row_count?: number;
  chunk_count?: number;
};
type TabularSource = { filename: string; rowCount: number };
type PolicySource = { filename: string; chunkCount: number };
type ConnectionState = "checking" | "connected" | "offline";

const tenantId = "user_123";
const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

export default function ChatWorkspace() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pinnedReport, setPinnedReport] = useState("");
  const [records, setRecords] = useState<SqlRecord[]>([]);
  const [ragResults, setRagResults] = useState<PolicyMatch[]>([]);
  const [policySourcesIncluded, setPolicySourcesIncluded] = useState(false);
  const [sqlQuery, setSqlQuery] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [uploadStatus, setUploadStatus] = useState("");
  const [isDragging, setIsDragging] = useState(false);
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [datasets, setDatasets] = useState<TabularSource[]>([]);
  const [policyDocuments, setPolicyDocuments] = useState<PolicySource[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${apiUrl}/health`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Backend health check failed");
        setConnection("connected");
      })
      .catch((requestError: unknown) => {
        if (
          requestError instanceof Error &&
          requestError.name === "AbortError"
        ) {
          return;
        }
        setConnection("offline");
      });
    return () => controller.abort();
  }, []);

  async function sendMessage(message: string) {
    if (isLoading) return;
    setIsLoading(true);
    setError("");
    const nextMessages: ChatMessage[] = [
      ...messages,
      { role: "user", content: message },
    ];
    setMessages(nextMessages);

    try {
      const response = await fetch(`${apiUrl}/api/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Tenant-ID": tenantId,
        },
        body: JSON.stringify({
          message,
          messages: nextMessages,
          audit_context: pinnedReport
            ? {
                report: pinnedReport,
                sql_query: sqlQuery,
                sql_results: records.length ? { data: records } : null,
                rag_results: ragResults.length ? { results: ragResults } : null,
                policy_sources_included: policySourcesIncluded,
              }
            : null,
        }),
      });
      if (!response.ok || !response.body) {
        throw new Error(`Backend returned ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let eventName = "message";
      let finalPayload: AuditPayload | null = null;
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const rawLine of lines) {
          const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          if (line.startsWith("data:")) {
            const payload = JSON.parse(line.slice(5).trim()) as AuditPayload & {
              error?: string;
            };
            if (eventName === "error") {
              throw new Error(payload.error || "The request failed");
            }
            if (eventName === "final") finalPayload = payload;
            eventName = "message";
          }
        }
        if (done) break;
      }
      if (!finalPayload?.final_response) {
        throw new Error("The assistant did not return a response");
      }

      const responsePayload = finalPayload as AuditPayload & {
        final_response: string;
      };
      setMessages((current) => [
        ...current,
        {
          role: "assistant",
          content: responsePayload.final_response,
          sqlQuery: responsePayload.sql_query || undefined,
          records: responsePayload.sql_results?.data || undefined,
          policyMatches: responsePayload.policy_sources_included
            ? responsePayload.rag_results?.results || []
            : undefined,
          policySourcesIncluded: responsePayload.policy_sources_included,
        },
      ]);
      setRagResults(responsePayload.rag_results?.results || []);
      setPolicySourcesIncluded(
        Boolean(responsePayload.policy_sources_included),
      );
      if (!responsePayload.follow_up) {
        setPinnedReport(responsePayload.final_response);
        setRecords(responsePayload.sql_results?.data || []);
        setSqlQuery(responsePayload.sql_query || "");
      }
    } catch (requestError) {
      setMessages((current) => current.slice(0, -1));
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Unable to reach the assistant",
      );
    } finally {
      setIsLoading(false);
    }
  }

  async function uploadFile(file: File) {
    const extension = file.name.split(".").pop()?.toLowerCase();
    const kind = extension === "pdf" ? "pdf" : "csv";
    const supportedTabular = ["csv", "tsv", "xlsx", "xls"].includes(
      extension || "",
    );
    if (kind === "csv" && !supportedTabular) {
      setUploadStatus("Choose a CSV, TSV, Excel, or PDF file.");
      return;
    }

    const formData = new FormData();
    formData.append("file", file);
    setUploadStatus(`Uploading ${file.name}…`);
    try {
      const response = await fetch(`${apiUrl}/api/upload/${kind}`, {
        method: "POST",
        headers: { "X-Tenant-ID": tenantId },
        body: formData,
      });
      const payload = (await response.json()) as UploadPayload;
      if (!response.ok) throw new Error(payload.detail || "Upload failed");

      if (kind === "pdf") {
        setPolicyDocuments((current) => [
          ...current.filter((document) => document.filename !== file.name),
          {
            filename: payload.filename || file.name,
            chunkCount: payload.chunk_count || 0,
          },
        ]);
        setUploadStatus(`${file.name} added to policy sources.`);
      } else {
        setDatasets((current) => [
          ...current.filter((dataset) => dataset.filename !== file.name),
          {
            filename: payload.filename || file.name,
            rowCount: payload.row_count || 0,
          },
        ]);
        setUploadStatus(`${file.name} connected successfully.`);
      }
    } catch (uploadError) {
      setUploadStatus(
        uploadError instanceof Error ? uploadError.message : "Upload failed",
      );
    }
  }

  async function handleFileSelection(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      await uploadFile(file);
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDragging(false);
    if (event.dataTransfer.files.length) {
      void handleFileSelection(event.dataTransfer.files);
    }
  }

  function clearSession() {
    setMessages([]);
    setPinnedReport("");
    setRecords([]);
    setRagResults([]);
    setPolicySourcesIncluded(false);
    setSqlQuery("");
    setError("");
    setUploadStatus("");
    setDatasets([]);
    setPolicyDocuments([]);
  }

  const connectionLabel = {
    checking: "Checking connection",
    connected: "Connected",
    offline: "Offline",
  }[connection];

  return (
    <main className="chat-app-shell">
      <aside className="bi-sidebar" aria-label="Knowledge base and data sources">
        <div className="bi-sidebar-brand">
          <div className="bi-brand-icon">
            <BarChart3 size={18} aria-hidden="true" />
          </div>
          <div>
            <strong>Enterprise BI</strong>
            <span>Workspace</span>
          </div>
        </div>

        <div className="bi-sidebar-content">
          <section className="bi-sidebar-section">
            <div className="bi-section-heading">
              <div>
                <p className="bi-eyebrow">Knowledge base</p>
                <h2>Data sources</h2>
              </div>
              <Database size={16} aria-hidden="true" />
            </div>
            <div
              className={`bi-dropzone${isDragging ? " is-dragging" : ""}`}
              onDragEnter={(event) => {
                event.preventDefault();
                setIsDragging(true);
              }}
              onDragLeave={(event) => {
                if (
                  !(event.relatedTarget instanceof Node) ||
                  !event.currentTarget.contains(event.relatedTarget)
                ) {
                  setIsDragging(false);
                }
              }}
              onDragOver={(event) => event.preventDefault()}
              onDrop={handleDrop}
            >
              <input
                accept=".csv,.tsv,.xlsx,.xls,.pdf"
                aria-label="Upload CSV, Excel, TSV, or PDF files"
                multiple
                onChange={(event) => {
                  if (event.target.files?.length) {
                    void handleFileSelection(event.target.files);
                  }
                }}
                ref={fileInputRef}
                type="file"
              />
              <div className="bi-upload-icon">
                <HardDriveUpload size={19} aria-hidden="true" />
              </div>
              <strong>Drop files to add data</strong>
              <span>CSV, Excel, TSV, or PDF</span>
              <button
                onClick={() => fileInputRef.current?.click()}
                type="button"
              >
                Browse files
              </button>
            </div>
            {uploadStatus && (
              <p className="bi-upload-status" role="status">
                {uploadStatus}
              </p>
            )}
          </section>

          <section className="bi-sidebar-section">
            <div className="bi-list-heading">
              <h3>Active datasets</h3>
              <span>{datasets.length}</span>
            </div>
            {datasets.length ? (
              <ul className="bi-source-list">
                {datasets.map((dataset) => (
                  <li className="bi-source-item" key={dataset.filename}>
                    <FileSpreadsheet size={16} aria-hidden="true" />
                    <div className="bi-source-details">
                      <strong title={dataset.filename}>{dataset.filename}</strong>
                      <span>
                        {dataset.rowCount.toLocaleString()}{" "}
                        {dataset.rowCount === 1 ? "row" : "rows"}
                      </span>
                    </div>
                    <span className="bi-status-pill">
                      <Check size={11} aria-hidden="true" />
                      Ready
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="bi-list-empty">
                Uploaded tables will appear here.
              </p>
            )}
          </section>

          <section className="bi-sidebar-section">
            <div className="bi-list-heading">
              <h3>Corporate policy / RAG</h3>
              <span>{policyDocuments.length}</span>
            </div>
            {policyDocuments.length ? (
              <ul className="bi-source-list">
                {policyDocuments.map((document) => (
                  <li className="bi-source-item" key={document.filename}>
                    <FileText size={16} aria-hidden="true" />
                    <div className="bi-source-details">
                      <strong title={document.filename}>
                        {document.filename}
                      </strong>
                      <span>
                        {document.chunkCount} searchable{" "}
                        {document.chunkCount === 1 ? "section" : "sections"}
                      </span>
                    </div>
                    <ShieldCheck
                      className="bi-policy-icon"
                      size={15}
                      aria-label="Policy document ready"
                    />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="bi-list-empty">
                Policy documents will appear here.
              </p>
            )}
          </section>
        </div>

        <div className="bi-sidebar-footer">
          <button
            className="bi-clear-button"
            onClick={clearSession}
            disabled={isLoading || uploadStatus.startsWith("Uploading ")}
            title="Clears this conversation and local context; uploaded server data is not deleted."
            type="button"
          >
            <Trash2 size={15} aria-hidden="true" />
            Clear Session &amp; Cache
          </button>
          <p>Clears this chat and its local context. Uploaded files remain on the server.</p>
        </div>
      </aside>

      <section className="bi-main">
        <header className="bi-topbar">
          <div className="bi-mobile-brand">
            <div className="bi-brand-icon">
              <BarChart3 size={17} aria-hidden="true" />
            </div>
            <strong>Enterprise BI Assistant</strong>
          </div>
          <div className="bi-desktop-title">
            <h1>Enterprise BI Assistant</h1>
            <p>Business data and policy, in one conversation</p>
          </div>
          <div className="bi-topbar-meta">
            <span className="bi-tenant-badge">Tenant / {tenantId}</span>
            <span className={`bi-connection bi-connection-${connection}`}>
              <span className="bi-connection-dot" />
              {connectionLabel}
            </span>
          </div>
        </header>

        <Chat
          disabled={isLoading}
          error={error}
          isLoading={isLoading}
          messages={messages}
          onSubmit={sendMessage}
        />
        <div className="bi-mobile-disclaimer">
          <CircleHelp size={13} aria-hidden="true" />
          Uploaded files are processed for this tenant.
        </div>
      </section>
    </main>
  );
}
