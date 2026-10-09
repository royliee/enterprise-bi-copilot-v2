# Enterprise BI Copilot

An AI-assisted business intelligence workspace that answers questions across structured business data and uploaded policy documents. The Next.js interface streams progress and a final report from a FastAPI service. The backend uses a LangGraph workflow to retrieve relevant policy context, generate and run a read-only SQL query, and synthesize the results.

> **Deployment status:** This repository is a development-oriented application, not a production-secure multi-tenant service. Review the security and operational limitations below before exposing it to users or sensitive data.

## Architecture

```text
Browser (Next.js)
  | POST /api/chat (streamed SSE), /api/upload/*
  v
FastAPI (backend/app/main.py)
  |-- router: new audit or follow-up
  |-- new audit: planner -> policy RAG -> SQL executor -> synthesizer
  |-- SQL: PostgreSQL when DATABASE_URL is PostgreSQL; otherwise local SQLite
  |-- RAG: tenant-filtered BM25 plus optional Chroma dense retrieval
  v
LLM provider (Groq by default, Gemini supported)
```

- **Frontend:** Next.js App Router, React, TypeScript, Tailwind CSS, and `lucide-react`. The current page is the Northstar chat workspace, with CSV and PDF upload controls.
- **Backend:** FastAPI exposes health, chat, and upload routes. Chat requests are processed by a compiled LangGraph state graph and returned as server-sent events (SSE).
- **Structured data:** SQL is generated for the active table (`business_data` by default). Queries use PostgreSQL when configured; otherwise the backend reads `backend/app/database/enterprise.db` using SQLite.
- **Policy documents:** PDFs are text-extracted and split into overlapping chunks. Text files in `data/sample_contracts/` are loaded for the default tenant at backend startup. Retrieval uses BM25 and, when enabled, a persistent Chroma collection under `data/chroma_store/`.
- **Tenant identifier:** Requests must include `X-Tenant-ID`. The web client currently sends the fixed value `user_123`.

## Prerequisites

- Python 3.11 (the backend Docker image uses Python 3.11).
- Node.js 20 or later and npm (the frontend Docker image uses Node 20).
- A Groq API key, or a Gemini API key, for LLM requests.
- Optional: PostgreSQL for CSV/XLS/XLSX ingestion. The Compose file does not start a database server; provide an externally reachable PostgreSQL instance if using this feature.
- Optional: Docker Compose for the containerized launch.

## Configuration

Create a root `.env` file. It is ignored by Git; do not commit credentials.

```dotenv
GROQ_API_KEY=replace-with-your-groq-key
LLM_PROVIDER=groq
MODEL_NAME=openai/gpt-oss-120b

# Optional PostgreSQL connection. Omit this to use the local SQLite file.
# DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE

# Optional overrides
# CSV_TABLE_NAME=business_data
# DEFAULT_TENANT_ID=user_123
# ENABLE_DENSE_RAG=true
```

For Gemini, set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=...`. The default provider is Groq; the default Groq model is `openai/gpt-oss-120b`. The application raises an error when neither usable provider key is configured.

| Variable              | Default                                 | Purpose                                                                                                                  |
| --------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `GROQ_API_KEY`        | unset                                   | Groq credentials; required when using the default Groq provider.                                                         |
| `GEMINI_API_KEY`      | unset                                   | Gemini credentials.                                                                                                      |
| `LLM_PROVIDER`        | `groq`                                  | Selects the preferred provider (`groq` or `gemini`).                                                                     |
| `MODEL_NAME`          | `openai/gpt-oss-120b`                   | Groq model name.                                                                                                         |
| `DATABASE_URL`        | unset                                   | PostgreSQL connection URL. If unset, SQL reads the local SQLite database. CSV/XLS/XLSX upload requires a PostgreSQL URL. |
| `CSV_TABLE_NAME`      | `business_data`                         | PostgreSQL table name used by tabular upload and the SQL agent.                                                          |
| `DEFAULT_TENANT_ID`   | `user_123`                              | Tenant assigned to startup-loaded text sample documents.                                                                 |
| `ENABLE_DENSE_RAG`    | `true`                                  | Enables Chroma dense retrieval in a local backend run. Compose overrides this to `false`.                                |
| `NEXT_PUBLIC_API_URL` | `http://127.0.0.1:8000` in the frontend | Backend base URL embedded in the frontend. Compose defaults it to `http://localhost:8000`.                               |

When running the frontend locally, configure `NEXT_PUBLIC_API_URL` in `frontend/.env.local` or in the frontend process environment. The root `.env` is loaded by backend code, but Next.js does not automatically read it from the repository root.

## Local Setup

Run these commands from the repository root in PowerShell.

1. Create and activate a Python environment, then install backend dependencies:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install -r backend/requirements.txt
   ```

2. Create/configure the root `.env` as described above. Keep a valid provider API key in the environment for chat requests.

3. Start the API from the repository root:

   ```powershell
   uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
   ```

   Check `http://127.0.0.1:8000/health`. Interactive API documentation is at `http://127.0.0.1:8000/docs`.

4. In a second terminal, install and start the frontend:

   ```powershell
   cd frontend
   npm ci
   $env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8000"
   npm run dev
   ```

   Open `http://localhost:3000`.

The included `backend/app/database/seed.py` can recreate a small SQLite sample database:

```powershell
python -m backend.app.database.seed
```

**This command deletes and recreates `backend/app/database/enterprise.db`.** The seed tables (`customers` and `orders`) are illustrative; they do not match the default active table or the SQL agent's required `tenant_id` filter, so they are not a ready-to-query chat dataset. For the supported upload workflow, configure PostgreSQL and upload a tabular file with the UI or API.

## Docker Compose

With Docker Desktop and a root `.env` present, run from the repository root:

```powershell
docker compose up --build
```

- Frontend: `http://localhost:3000`
- API: `http://localhost:8000`
- API docs: `http://localhost:8000/docs`

Compose builds the backend and frontend services, maps ports 8000 and 3000, and mounts the repository into the API container for development reloads. It sets `ENABLE_DENSE_RAG=false`, so document retrieval in this mode is sparse BM25 rather than Chroma dense retrieval. The `api` service declares `.env` as a required `env_file`; create it before starting Compose. PostgreSQL is not included in Compose.

Stop the services with `Ctrl+C`, or run `docker compose down` in another terminal.

## API Reference

All routes are served by the FastAPI application at the configured backend origin. Except for `/health`, routes require a valid `X-Tenant-ID` header: 1-100 letters, digits, underscores, or hyphens. The value is currently a client-supplied identifier, not an authenticated identity.

### `GET /health`

Returns a basic process health response; it does not verify database or LLM connectivity.

```powershell
curl.exe http://127.0.0.1:8000/health
```

### `POST /api/chat`

Accepts JSON and returns an SSE stream (`text/event-stream`). Required field: `message`. Optional fields: `messages` (conversation history) and `audit_context` (previous report context for follow-up questions).

```powershell
curl.exe -N -X POST http://127.0.0.1:8000/api/chat `
  -H "Content-Type: application/json" `
  -H "X-Tenant-ID: user_123" `
  --data-raw '{"message":"Summarize the business data and relevant policy","messages":[]}'
```

The stream emits `step` events while nodes run, then a `final` event with fields such as `final_response`, `executive_summary`, `detailed_findings`, `sql_query`, `sql_results`, and `rag_results`. A processing failure is sent as an `error` event. Because this is a `POST` response stream, consume it with `fetch`/a streaming client or `curl -N`; the browser's native `EventSource` API does not make this POST request.

For a follow-up, include the prior turns in `messages` and pass the previous report and findings in `audit_context`. The frontend constructs this context automatically. A minimal payload is:

```json
{
  "message": "Explain the policy reference in the report",
  "messages": [
    {
      "role": "user",
      "content": "Summarize the business data and relevant policy"
    },
    { "role": "assistant", "content": "Previous report text" }
  ],
  "audit_context": {
    "report": "Previous report text",
    "sql_query": "SELECT ...",
    "sql_results": { "data": [] },
    "rag_results": { "results": [] }
  }
}
```

### `POST /api/upload/csv`

Despite the route name, accepts `.csv`, `.tsv`, `.xlsx`, and `.xls` files as multipart form data. Requires `X-Tenant-ID` and a PostgreSQL `DATABASE_URL`; otherwise it returns `503`.

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/upload/csv `
  -H "X-Tenant-ID: user_123" `
  -F "file=@C:\data\sales.csv"
```

The response includes `filename`, `tenant_id`, and `row_count`. The configured table name defaults to `business_data`; the upload adds a `tenant_id` column. **The current implementation writes with `if_exists="replace"`, replacing the entire configured table on each upload.** Do not use this route for concurrent tenants or valuable shared data without changing the storage behavior.

### `POST /api/upload/pdf`

Accepts one `.pdf` multipart file and requires `X-Tenant-ID`. The backend extracts page text, chunks it, and returns `filename`, `tenant_id`, and `chunk_count`.

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/upload/pdf `
  -H "X-Tenant-ID: user_123" `
  -F "file=@C:\data\partner-agreement.pdf"
```

Uploaded PDF chunks are added to the running retriever. They are not automatically reloaded into its in-memory index after a backend restart; treat uploaded documents as ephemeral unless durable ingestion/reindexing is implemented and verified for the deployment configuration.

### Common errors

| Status | Meaning                                                                                                      |
| ------ | ------------------------------------------------------------------------------------------------------------ |
| `400`  | Missing/invalid `X-Tenant-ID`, or unsupported upload extension.                                              |
| `503`  | Tabular upload requested without a PostgreSQL `DATABASE_URL`.                                                |
| `500`  | Upload parsing, database write, or PDF processing failed; the response includes the underlying error detail. |

## Directory Structure

```text
.
|-- docker-compose.yml             # API and web service definitions
|-- backend/
|   |-- Dockerfile                 # Python API image
|   |-- requirements.txt           # Backend dependencies
|   `-- app/
|       |-- main.py                # FastAPI application and HTTP routes
|       |-- agent/
|       |   |-- graph.py           # LangGraph workflow and routing
|       |   |-- nodes.py           # Planner, SQL, RAG, follow-up, synthesis nodes
|       |   `-- state.py           # Shared workflow state model
|       |-- database/
|       |   |-- seed.py            # Recreates the local SQLite sample database
|       |   `-- enterprise.db      # Local SQLite database
|       `-- mcp/tools/
|           |-- sql_tool.py        # Read-only SQL execution and serialization
|           `-- rag_tool.py        # Document chunking and hybrid retrieval
|-- data/
|   |-- chroma_store/              # Persistent Chroma files when dense RAG is enabled
|   `-- sample_contracts/          # Startup-loaded text policy/sample documents
`-- frontend/
    |-- Dockerfile                 # Node/Next.js image
    |-- package.json               # Scripts and frontend dependencies
    |-- package-lock.json          # Reproducible npm dependency lock
    `-- src/app/
        |-- layout.tsx             # Root layout and metadata
        |-- page.tsx               # Main application page
        |-- globals.css            # Global styles
        `-- components/
            |-- ChatWorkspace.tsx  # Chat, uploads, and API integration
            `-- Chat.tsx           # Conversation UI
```

## Dependencies and Verification

- Backend dependencies are declared in `backend/requirements.txt`: FastAPI/Uvicorn, LangGraph/LangChain provider integrations, ChromaDB and BM25 retrieval, SQLAlchemy/PostgreSQL drivers, Pandas and spreadsheet readers, PDF extraction, and SSE support.
- Frontend dependencies and scripts are declared in `frontend/package.json`. The lockfile is committed; use `npm ci` for repeatable installs. Available scripts are `npm run dev`, `npm run build`, `npm run start`, and `npm run lint`.
- No automated backend test suite is present in the current repository tree. Frontend checks can be run from `frontend/`:

  ```powershell
  npm run lint
  npm run build
  ```

## Production Considerations

Before production use, address at least the following:

- Add authentication and authorize each tenant on the server. `X-Tenant-ID` is currently only syntax-checked and can be chosen by the caller.
- Replace wildcard CORS with an explicit allowlist and deploy behind HTTPS and suitable request/file-size limits.
- Redesign tabular ingestion: the current `replace` write targets one shared table and can erase data from prior uploads/tenants.
- Implement and test durable, tenant-isolated document ingestion and index recovery across restarts. Review `ENABLE_DENSE_RAG` and the Chroma volume/storage strategy for each deployment.
- Use a database role restricted to read-only access for generated SQL. The current SQL checks are application-level guards, not a substitute for database permissions or a SQL parser.
- Define retention, backup, logging, and error-redaction policies. Upload failures currently return exception details to the caller.
