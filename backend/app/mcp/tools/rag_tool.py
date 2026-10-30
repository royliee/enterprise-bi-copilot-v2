import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Sequence

from rank_bm25 import BM25Okapi

ROOT_DIR = Path(__file__).resolve().parents[4]
CONTRACTS_DIR = ROOT_DIR / "data" / "sample_contracts"
CHROMA_DIR = ROOT_DIR / "data" / "chroma_store"
DEFAULT_TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "user_123")
CHUNK_SIZE = 900
CHUNK_OVERLAP = 180
RRF_K = 60
logger = logging.getLogger(__name__)


def _section_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if re.match(r"^(section\s+[A-Za-z0-9]+|[A-Z][.)]|[0-9]+[.)])\b", candidate, re.IGNORECASE):
            return candidate[:200]
    return fallback


def _overlapping_chunks(text: str, metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    words = re.findall(r"\S+", text.replace("\r\n", "\n"))
    if not words:
        return []
    chunks: List[Dict[str, Any]] = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for start in range(0, len(words), step):
        chunk_text = " ".join(words[start:start + CHUNK_SIZE]).strip()
        if not chunk_text:
            continue
        chunk_metadata = dict(metadata)
        chunk_metadata["section_title"] = _section_title(chunk_text, metadata.get("parent_title", "Policy"))
        chunk_metadata["chunk_index"] = len(chunks)
        chunks.append({"text": chunk_text, "metadata": chunk_metadata})
        if start + CHUNK_SIZE >= len(words):
            break
    return chunks


class HybridRetriever:
    def __init__(self):
        self.dense_enabled = os.getenv("ENABLE_DENSE_RAG", "true").lower() == "true"
        self.collection = None
        self.chroma_client = None
        self._index_lock = threading.RLock()
        if self.dense_enabled:
            import chromadb

            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                self.chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
                self.collection = self.chroma_client.get_or_create_collection(
                    name="enterprise_policies"
                )
            except BlockingIOError as error:
                self.dense_enabled = False
                logger.warning(
                    "Chroma persistent index is locked; continuing with BM25 retrieval: %s",
                    error,
                )
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower():
                    raise
                self.dense_enabled = False
                logger.warning(
                    "Chroma persistent index is locked; continuing with BM25 retrieval: %s",
                    error,
                )
        self.documents: List[str] = []
        self.doc_ids: List[str] = []
        self.document_tenants: List[str] = []
        self.document_metadata: List[Dict[str, Any]] = []
        self.bm25 = None
        self._index_documents()

    def _index_documents(self):
        chunks: List[Dict[str, Any]] = []
        if CONTRACTS_DIR.exists():
            for file in CONTRACTS_DIR.glob("*.txt"):
                chunks.extend(_overlapping_chunks(file.read_text(encoding="utf-8"), {
                    "source": file.name,
                    "document_title": file.stem,
                    "parent_title": file.stem,
                    "page": "unknown",
                    "tenant_id": DEFAULT_TENANT_ID,
                }))
        if not chunks:
            return
        self._append_chunks(chunks, DEFAULT_TENANT_ID, "sample")

    def _append_chunks(self, chunks: Sequence[Dict[str, Any]], tenant_id: str, source: str) -> int:
        with self._index_lock:
            start = len(self.documents)
            ids = [f"{tenant_id}_{source}_{start + index}" for index in range(len(chunks))]
            documents = [*self.documents, *(chunk["text"] for chunk in chunks)]
            doc_ids = [*self.doc_ids, *ids]
            document_tenants = [*self.document_tenants, *([tenant_id] * len(chunks))]
            document_metadata = [
                *self.document_metadata,
                *(chunk["metadata"] for chunk in chunks),
            ]
            bm25 = BM25Okapi(
                [re.findall(r"\w+", document.lower()) for document in documents]
            ) if documents else None
            if self.dense_enabled and self.collection is not None and chunks:
                self.collection.upsert(
                    documents=[chunk["text"] for chunk in chunks],
                    ids=ids,
                    metadatas=[chunk["metadata"] for chunk in chunks],
                )
            self.documents = documents
            self.doc_ids = doc_ids
            self.document_tenants = document_tenants
            self.document_metadata = document_metadata
            self.bm25 = bm25
            return len(chunks)

    def add_document_pages(self, pages: Sequence[str], tenant_id: str, source: str) -> int:
        chunks: List[Dict[str, Any]] = []
        for page_number, page_text in enumerate(pages, start=1):
            chunks.extend(_overlapping_chunks(page_text, {
                "source": source,
                "document_title": source,
                "parent_title": source,
                "page": page_number,
                "tenant_id": tenant_id,
            }))
        return self._append_chunks(chunks, tenant_id, source)

    def add_documents(self, chunks: List[str], tenant_id: str, source: str) -> int:
        return self._append_chunks([
            {"text": chunk.strip(), "metadata": {
                "source": source,
                "document_title": source,
                "parent_title": source,
                "page": "unknown",
                "tenant_id": tenant_id,
            }}
            for chunk in chunks if chunk.strip()
        ], tenant_id, source)

    def _result(self, index: int, source: str, score: float) -> Dict[str, Any]:
        metadata = dict(self.document_metadata[index])
        return {
            "text": self.documents[index],
            "source": source,
            "score": score,
            "metadata": metadata,
        }

    def search(self, query: str, tenant_id: str, top_k: int = 8) -> List[Dict[str, Any]]:
        query_clean = query.strip()
        if not query_clean or not self.documents:
            return []
        limit = max(top_k, 8)
        tenant_indices = [i for i, owner in enumerate(self.document_tenants) if owner == tenant_id]
        sparse_ranked: List[int] = []
        if self.bm25 and tenant_indices:
            tokens = re.findall(r"\w+", query_clean.lower())
            scores = self.bm25.get_scores(tokens)
            sparse_ranked = sorted(tenant_indices, key=lambda i: scores[i], reverse=True)[:limit]
        dense_ranked: List[int] = []
        if self.dense_enabled and self.collection is not None:
            try:
                dense = self.collection.query(
                    query_texts=[query_clean],
                    n_results=limit,
                    where={"tenant_id": tenant_id},
                    include=["documents", "metadatas", "distances"],
                )
                for metadata in (dense.get("metadatas") or [[]])[0]:
                    doc_id = next((i for i, item in enumerate(self.document_metadata) if item == metadata), None)
                    if doc_id is not None:
                        dense_ranked.append(doc_id)
            except BlockingIOError as error:
                logger.warning(
                    "Chroma query was blocked; returning sparse retrieval results: %s",
                    error,
                )
                dense_ranked = []
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower():
                    raise
                logger.warning(
                    "Chroma query encountered a locked index; returning sparse retrieval results: %s",
                    error,
                )
                dense_ranked = []
        fused: Dict[int, float] = {}
        for rank, index in enumerate(sparse_ranked):
            fused[index] = fused.get(index, 0.0) + 1 / (RRF_K + rank + 1)
        for rank, index in enumerate(dense_ranked):
            fused[index] = fused.get(index, 0.0) + 1 / (RRF_K + rank + 1)
        if not fused:
            return []
        selected = sorted(fused, key=fused.get, reverse=True)[:limit]
        for index in list(selected):
            for neighbor in (index - 1, index + 1):
                if 0 <= neighbor < len(self.documents) and self.document_tenants[neighbor] == tenant_id:
                    if self.document_metadata[neighbor].get("source") == self.document_metadata[index].get("source"):
                        selected.append(neighbor)
        results: List[Dict[str, Any]] = []
        seen = set()
        for index in selected:
            if index not in seen:
                results.append(self._result(index, "hybrid_rrf", fused.get(index, 0.0)))
                seen.add(index)
        return results[:limit]


retriever = HybridRetriever()


def search_policy_documents(query: str, tenant_id: str, top_k: int = 8) -> Dict[str, Any]:
    matches = retriever.search(query, tenant_id=tenant_id, top_k=max(top_k, 8))
    return {"query": query, "match_count": len(matches), "results": matches}
