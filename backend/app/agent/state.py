from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

class AgentState(BaseModel):
    user_query: str
    tenant_id: str
    messages: List[Dict[str, str]] = Field(default_factory=list)
    conversation_route: str = "new_audit"
    error_message: str | None = None
    retry_count: int = 0
    logical_retry_count: int = 0
    plan: List[str] = Field(default_factory=list)
    rag_routed: bool = False
    sql_query: Optional[str] = None
    sql_results: Optional[Dict[str, Any]] = None
    rag_query: Optional[str] = None
    rag_results: Optional[Dict[str, Any]] = None
    audit_findings: Optional[str] = None
    executive_summary: str = ""
    detailed_findings: str = ""
    final_response: str = ""
    current_step: str = "init"
    execution_trace: List[Dict[str, Any]] = Field(default_factory=list)
