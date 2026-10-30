import json
import asyncio
import os
import re
from collections.abc import Hashable
from io import BytesIO
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import create_engine
from pypdf import PdfReader

from backend.app.agent.graph import bi_agent
from backend.app.agent.state import AgentState
from backend.app.mcp.tools.rag_tool import retriever

app = FastAPI(title="Enterprise BI Copilot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str
    messages: list[ChatMessage] = Field(default_factory=list)
    audit_context: dict[str, object] | None = None


def require_tenant_id(x_tenant_id: str | None) -> str:
    tenant_id = x_tenant_id.strip() if x_tenant_id else ""
    if not tenant_id:
        raise HTTPException(status_code=400, detail="X-Tenant-ID header is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tenant_id):
        raise HTTPException(status_code=400, detail="X-Tenant-ID contains invalid characters")
    return tenant_id


SQL_RESERVED_WORDS = {
    "all", "and", "as", "asc", "between", "by", "case", "check", "column",
    "create", "delete", "desc", "distinct", "drop", "else", "end", "exists",
    "from", "group", "having", "in", "insert", "into", "is", "join", "like",
    "limit", "not", "null", "on", "or", "order", "select", "table", "then",
    "union", "unique", "update", "values", "when", "where", "with",
}


def _flatten_column(column: Hashable) -> str:
    if isinstance(column, tuple):
        return "_".join(str(part).strip() for part in column if str(part).strip())
    return str(column).strip()


def _sanitize_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
    names: list[str] = []
    used: dict[str, int] = {}
    for position, raw_column in enumerate(dataframe.columns, start=1):
        original = _flatten_column(raw_column)
        normalized = original.lower().strip()
        normalized = re.sub(r"[\s/\-.()]+", "_", normalized)
        normalized = re.sub(r"[^a-z0-9_]", "", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("_")
        if not normalized or normalized.startswith("unnamed"):
            normalized = f"col_{position}"
        if normalized[0].isdigit() or normalized in SQL_RESERVED_WORDS:
            normalized = f"col_{normalized}"
        count = used.get(normalized, 0) + 1
        used[normalized] = count
        names.append(normalized if count == 1 else f"{normalized}_{count}")
    dataframe.columns = names
    return dataframe


def _trim_empty_boundaries(dataframe: pd.DataFrame) -> pd.DataFrame:
    while len(dataframe) and dataframe.iloc[0].isna().all():
        dataframe = dataframe.iloc[1:]
    while len(dataframe) and dataframe.iloc[-1].isna().all():
        dataframe = dataframe.iloc[:-1]
    while len(dataframe.columns) and dataframe.iloc[:, 0].isna().all():
        dataframe = dataframe.iloc[:, 1:]
    while len(dataframe.columns) and dataframe.iloc[:, -1].isna().all():
        dataframe = dataframe.iloc[:, :-1]
    return dataframe


def _cast_numeric_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
    for column in dataframe.select_dtypes(include=["object", "string"]).columns:
        values = dataframe[column].astype("string").str.strip()
        cleaned = (
            values.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
            .str.replace(r"[$€£,\s]", "", regex=True)
            .str.replace("%", "", regex=False)
        )
        converted = pd.to_numeric(cleaned, errors="coerce")
        parsed_count = converted.notna().sum()
        if len(dataframe) and parsed_count > 0 and (parsed_count / len(dataframe)) >= 0.5:
            dataframe[column] = converted
    return dataframe


def _read_tabular_file(filename: str, content: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        engine = "openpyxl" if suffix == ".xlsx" else "xlrd"
        preview = pd.read_excel(BytesIO(content), engine=engine, header=None, nrows=2)
        has_multi_level_header = len(preview) > 1 and all(
            isinstance(value, str) and value.strip() for value in preview.iloc[0]
        ) and all(isinstance(value, str) and value.strip() for value in preview.iloc[1])
        header = [0, 1] if has_multi_level_header else 0
        dataframe = pd.read_excel(BytesIO(content), engine=engine, header=header)
        if isinstance(dataframe.columns, pd.MultiIndex):
            dataframe.columns = ["_".join(str(part) for part in column if str(part) != "nan") for column in dataframe.columns]
    else:
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                try:
                    dataframe = pd.read_csv(BytesIO(content), sep=None, engine="python", encoding=encoding, low_memory=False)
                except ValueError as error:
                    if "low_memory" not in str(error):
                        raise
                    dataframe = pd.read_csv(BytesIO(content), sep=None, engine="python", encoding=encoding)
                break
            except (UnicodeDecodeError, pd.errors.ParserError) as error:
                last_error = error
        else:
            raise ValueError(f"Unable to parse tabular file: {last_error}")
    dataframe = _trim_empty_boundaries(dataframe)
    dataframe = _sanitize_columns(dataframe)
    return _cast_numeric_columns(dataframe)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "Enterprise BI Copilot Backend"}

@app.post("/api/chat")
async def chat_endpoint(payload: QueryRequest, x_tenant_id: str | None = Header(default=None)):
    tenant_id = require_tenant_id(x_tenant_id)

    async def event_generator():
        messages = [message.model_dump() for message in payload.messages]
        if not messages or messages[-1].get("content") != payload.message:
            messages.append({"role": "user", "content": payload.message})
        audit_context = payload.audit_context or {}
        initial_state = AgentState(
            user_query=payload.message,
            tenant_id=tenant_id,
            messages=messages,
            final_response=str(audit_context.get("report", "")),
            executive_summary=str(audit_context.get("executive_summary", "")),
            detailed_findings=str(audit_context.get("detailed_findings", "")),
            sql_results=audit_context.get("sql_results"),
            rag_results=audit_context.get("rag_results"),
            rag_routed=bool(audit_context.get("policy_sources_included")),
        )
        
        yield {
            "event": "step",
            "data": json.dumps({"step": "start", "message": "Analyzing query and formulating execution plan..."})
        }
        await asyncio.sleep(0.05)

        accumulated_state = {}

        try:
            for chunk in bi_agent.stream(initial_state):
                for node_name, node_output in chunk.items():
                    accumulated_state.update(node_output)
                    step_data = {
                        "node": node_name,
                        "current_step": node_output.get("current_step"),
                        "trace": node_output.get("execution_trace", [])[-1] if node_output.get("execution_trace") else None
                    }
                    yield {
                        "event": "step",
                        "data": json.dumps(step_data)
                    }
                    await asyncio.sleep(0.05)

                    if node_name in {"synthesizer", "sql_failure"} and node_output.get("final_response"):
                        rag_routed = bool(accumulated_state.get("rag_routed"))
                        rag_results = accumulated_state.get("rag_results")
                        rag_results_present = bool(
                            (rag_results or {}).get("results")
                        )
                        final_payload = {
                            "final_response": node_output.get("final_response"),
                            "executive_summary": accumulated_state.get("executive_summary", ""),
                            "detailed_findings": accumulated_state.get("detailed_findings", ""),
                            "sql_query": accumulated_state.get("sql_query"),
                            "sql_results": accumulated_state.get("sql_results"),
                            "policy_sources_included": rag_routed or rag_results_present,
                        }
                        if rag_routed:
                            final_payload["rag_results"] = rag_results or {"results": []}
                        yield {
                            "event": "final",
                            "data": json.dumps(final_payload)
                        }
                    elif node_name == "follow_up_chat":
                        assistant_messages = [
                            message for message in node_output.get("messages", [])
                            if message.get("role") == "assistant"
                        ]
                        if assistant_messages:
                            rag_routed = bool(accumulated_state.get("rag_routed"))
                            rag_results = accumulated_state.get("rag_results")
                            policy_sources_included = bool(
                                rag_routed
                                or (rag_results or {}).get("results")
                            )
                            final_payload = {
                                "follow_up": True,
                                "final_response": assistant_messages[-1].get("content", ""),
                                "executive_summary": initial_state.executive_summary,
                                "detailed_findings": initial_state.detailed_findings,
                                "sql_query": audit_context.get("sql_query"),
                                "sql_results": initial_state.sql_results,
                                "policy_sources_included": policy_sources_included,
                            }
                            if policy_sources_included:
                                final_payload["rag_results"] = rag_results or {"results": []}
                            yield {
                                "event": "final",
                                "data": json.dumps(final_payload)
                            }
        except Exception as e:
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)})
            }

    return EventSourceResponse(event_generator())


@app.post("/api/upload/csv")
async def upload_csv(file: UploadFile = File(...), x_tenant_id: str | None = Header(default=None)):
    tenant_id = require_tenant_id(x_tenant_id)
    supported_extensions = {".csv", ".tsv", ".xlsx", ".xls"}
    if not file.filename or Path(file.filename).suffix.lower() not in supported_extensions:
        raise HTTPException(status_code=400, detail="Only CSV, TSV, XLSX, and XLS files are supported")

    database_url = os.getenv("DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql"):
        raise HTTPException(status_code=503, detail="PostgreSQL DATABASE_URL is not configured")

    engine = None
    try:
        dataframe = _read_tabular_file(file.filename, await file.read())
        dataframe["tenant_id"] = tenant_id
        engine_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        engine = create_engine(engine_url)
        table_name = os.getenv("CSV_TABLE_NAME", "business_data")
        dataframe.to_sql(table_name, engine, if_exists="replace", index=False)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"CSV upload failed: {error}") from error
    finally:
        if engine is not None:
            engine.dispose()

    return {"filename": file.filename, "tenant_id": tenant_id, "row_count": len(dataframe)}


@app.post("/api/upload/pdf")
async def upload_pdf(file: UploadFile = File(...), x_tenant_id: str | None = Header(default=None)):
    tenant_id = require_tenant_id(x_tenant_id)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    try:
        reader = PdfReader(BytesIO(await file.read()))
        pages = [page.extract_text() or "" for page in reader.pages]
        chunk_count = retriever.add_document_pages(pages, tenant_id, Path(file.filename).stem)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"PDF upload failed: {error}") from error

    return {"filename": file.filename, "tenant_id": tenant_id, "chunk_count": chunk_count}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=8000, reload=True)
