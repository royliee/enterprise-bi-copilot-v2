import os
import json
import re
from typing import Dict, Any, List
from dotenv import load_dotenv
from pathlib import Path
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from backend.app.agent.state import AgentState
from backend.app.mcp.tools.sql_tool import DATABASE_URL, DB_PATH, execute_readonly_query
from backend.app.mcp.tools.rag_tool import search_policy_documents

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
ZERO_ROW_RETRY_MESSAGE = (
    "The query executed successfully but returned 0 results. Please review your string matching "
    "(case sensitivity) and numerical filters, rewrite the query, and try again."
)

def get_llm():
    provider = os.getenv("LLM_PROVIDER", "groq").lower()
    if provider == "groq" and os.getenv("GROQ_API_KEY"):
        from langchain_groq import ChatGroq
        return ChatGroq(model_name=os.getenv("MODEL_NAME", "openai/gpt-oss-120b"), temperature=0.0)
    elif os.getenv("GEMINI_API_KEY"):
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0.0)
    else:
        raise ValueError("Missing API key in .env (configure GROQ_API_KEY or GEMINI_API_KEY)")

def get_active_table_schema(tenant_id: str) -> str:
    table_name = os.getenv("CSV_TABLE_NAME", "business_data")
    database_url = DATABASE_URL or f"sqlite:///{DB_PATH}"
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        schema_name = "public" if database_url.startswith("postgresql") else None
        if not inspector.has_table(table_name, schema=schema_name):
            available_tables = inspector.get_table_names(schema=schema_name)
            return f"No active table named '{table_name}' was found. Available tables: {available_tables}"
        columns = inspector.get_columns(table_name, schema=schema_name)
        column_lines = [f"  {column['name']} {column['type']}" for column in columns]
        qualified_name = f"{schema_name}.{table_name}" if schema_name else table_name
        metadata = MetaData()
        table = Table(table_name, metadata, schema=schema_name, autoload_with=engine)
        with engine.connect() as connection:
            sample_query = select(table).limit(3)
            if "tenant_id" in table.c:
                sample_query = select(table).where(table.c.tenant_id == tenant_id).limit(3)
            sample_rows = [
                dict(row)
                for row in connection.execute(sample_query).mappings().all()
            ]
        sample_text = json.dumps(sample_rows, indent=2, default=str)
        return (
            f"TABLE {qualified_name} (\n"
            + ",\n".join(column_lines)
            + f"\n)\n\nFIRST 3 ROWS (sample values):\n{sample_text}"
        )
    finally:
        engine.dispose()

def initialize_run_node(state: AgentState) -> Dict[str, Any]:
    return {
        "error_message": None,
        "retry_count": 0,
        "logical_retry_count": 0,
        "plan": [],
        "rag_routed": False,
        "sql_query": None,
        "sql_results": None,
        "rag_query": None,
        "rag_results": None,
        "audit_findings": None,
        "executive_summary": "",
        "detailed_findings": "",
        "final_response": "",
        "current_step": "init",
        "execution_trace": [],
    }

def router_node(state: AgentState) -> Dict[str, Any]:
    assistant_history = [
        message for message in state.messages if message.get("role") == "assistant"
    ]
    if not assistant_history:
        return {"conversation_route": "new_audit", "current_step": "routing"}

    llm = get_llm()
    recent_history = "\n".join(
        f"{message.get('role', 'user')}: {message.get('content', '')}"
        for message in state.messages[-6:]
    )
    prompt = f"""Classify the current user message for an enterprise BI assistant.
Return exactly FOLLOW_UP if it asks a clarification, explanation, or question about existing data or the current audit report.
Return exactly NEW_AUDIT if it requests new data, calculations, metrics, or a different investigation.
Do not answer the user.

CONVERSATION:
{recent_history}

CURRENT USER MESSAGE:
{state.user_query}"""
    route = llm.invoke(prompt).content.strip().upper()
    return {
        "conversation_route": "follow_up" if "FOLLOW_UP" in route else "new_audit",
        "current_step": "routing",
    }

def parse_synthesis_response(response: str) -> tuple[str, str]:
    detailed_marker = "Detailed Findings"
    detailed_index = response.lower().find(detailed_marker.lower())
    if detailed_index < 0:
        return response.strip(), ""
    executive = response[:detailed_index].strip()
    executive = re.sub(
        r"^\*\*Executive Summary\*\*:\s*",
        "",
        executive,
        flags=re.IGNORECASE,
    ).strip()
    executive = re.sub(r"(?m)^\s*\*\*\s*$", "", executive).strip()
    detailed = response[detailed_index + len(detailed_marker):].strip()
    detailed = detailed.removeprefix("**:").strip()
    return executive, detailed

def planner_node(state: AgentState) -> Dict[str, Any]:
    trace = list(state.execution_trace)
    llm = get_llm()
    prompt = f"""You are planning a generic business intelligence investigation.
Create a short 3-step plan for answering the user's question using the uploaded structured data and relevant uploaded documents.
Do not assume any table names, column names, industries, regions, metrics, or business rules.
Derive all rules and thresholds only from the user's question and retrieved documents later in the pipeline.
Return one concise step per line and no numbering or explanation.

USER QUESTION:
{state.user_query}"""
    response = llm.invoke(prompt)
    plan = [line.strip(" -") for line in response.content.splitlines() if line.strip()][:3]
    trace.append({"step": "planner", "message": "Created a data-source-neutral investigation plan."})
    return {
        "plan": plan,
        "current_step": "planning_complete",
        "execution_trace": trace
    }

def rag_router_node(state: AgentState) -> Dict[str, Any]:
    trace = list(state.execution_trace)
    llm = get_llm()
    prompt = f"""Decide whether answering the user's question requires searching uploaded policy, contract, or other reference documents.
Return exactly RAG_REQUIRED if the question asks about a policy, contract, rule, compliance requirement, definition, or document-based exception, including questions that combine those topics with data.
Return exactly SQL_ONLY if it can be answered solely from structured business data such as metrics, totals, rankings, trends, or tabular comparisons.
Do not answer the question. Return only one of the two exact labels.

USER QUESTION:
{state.user_query}"""
    route = llm.invoke(prompt).content.strip().upper()
    if route not in {"RAG_REQUIRED", "SQL_ONLY"}:
        raise ValueError(f"Unexpected policy routing response: {route!r}")

    rag_routed = route == "RAG_REQUIRED"
    trace.append({
        "step": "policy_routing",
        "route": "rag" if rag_routed else "sql_only",
    })
    return {
        "rag_routed": rag_routed,
        "rag_results": state.rag_results if rag_routed else None,
        "current_step": "policy_routing_complete",
        "execution_trace": trace,
    }

def sql_node(state: AgentState) -> Dict[str, Any]:
    trace = list(state.execution_trace)
    schema = get_active_table_schema(state.tenant_id)
    llm = get_llm()
    policy_context = "\n---\n".join(
        "Metadata: " + json.dumps(result.get("metadata", {})) + "\n" + result["text"]
        for result in (state.rag_results or {}).get("results", [])
    ) or "No policy documents matched this request."
    retry_context = (
        f"PREVIOUS SQL ERROR (rewrite the query to fix it): {state.error_message}"
        if state.error_message
        else "No previous SQL attempt failed."
    )
    
    prompt = f"""You are a database analyst writing a standard PostgreSQL read-only query.
DATABASE SCHEMA:
{schema}

USER QUESTION:
"{state.user_query}"

RETRIEVED DOCUMENT CONTEXT:
{policy_context}

{retry_context}

CRITICAL RULES:
1. Use only tables and columns present in the schema. Do not invent identifiers.
2. Apply business filtering only when supported by the user's question or retrieved document context.
3. Every query MUST restrict rows to tenant_id = '{state.tenant_id}' or an equivalent tenant-safe JOIN condition.
4. When filtering text columns in the WHERE clause, you MUST use the ILIKE operator instead of '=' to ensure case-insensitive matching.
5. You are generating SQL for PostgreSQL. You MUST wrap any column names that contain spaces or uppercase letters in double quotes (for example, "Order ID" or "Sales"). Never use square brackets.
6. Do not use placeholders, parameters, variables, or destructive SQL.
7. SQL GENERATION TEMPLATE:
You must strictly follow this template for aggregations. Do not add any extra arithmetic, constants, or multipliers.
Correct: SELECT SUM("Column Name") FROM table_name WHERE "Category" = 'Value';
INCORRECT: SELECT SUM("Column Name" * 8) FROM table_name;
Only output the raw, unmodified aggregate of the requested column.
8. Return ONLY executable SQL. Do not include markdown, backticks, or explanations."""

    response = llm.invoke(prompt)
    raw_sql = response.content.strip().replace("```sql", "").replace("```", "").strip()
    
    trace.append({"step": "sql_execution", "sql_generated": raw_sql})
    if "tenant_id" not in raw_sql.lower():
        query_data = {"error": "Security violation: Generated SQL must include a tenant_id restriction"}
    else:
        query_data = execute_readonly_query(raw_sql)

    error_message = query_data.get("error") if query_data else None
    next_retry_count = state.retry_count + 1 if error_message else state.retry_count
    if error_message:
        trace.append({"step": "sql_error", "message": error_message, "retry_count": next_retry_count})

    logical_retry_count = state.logical_retry_count
    current_step = "sql_retry" if error_message else "sql_complete"
    if query_data and query_data.get("row_count") == 0 and logical_retry_count == 0:
        logical_retry_count += 1
        error_message = ZERO_ROW_RETRY_MESSAGE
        current_step = "sql_logical_retry"
        trace.append({"step": "sql_zero_rows", "message": ZERO_ROW_RETRY_MESSAGE})
    
    return {
        "sql_query": raw_sql,
        "sql_results": query_data,
        "error_message": error_message,
        "retry_count": next_retry_count,
        "logical_retry_count": logical_retry_count,
        "current_step": current_step,
        "execution_trace": trace
    }

def follow_up_chat_node(state: AgentState) -> Dict[str, Any]:
    trace = list(state.execution_trace)
    llm = get_llm()
    report = "\n\n".join(
        part for part in (state.executive_summary, state.detailed_findings) if part
    ) or state.final_response or "No audit report is available."
    prompt = f"""You are an elite executive BI analyst and compliance auditor answering a follow-up question about an existing Enterprise BI audit.
Use only the current audit report, its database findings, and its retrieved policy context below. Do not run SQL, invent calculations, or introduce policies, thresholds, citations, or facts that are not explicitly present in this context.

RESPONSE STYLE:
- Answer the user's primary question directly in the very first sentence. Do not begin with conversational filler such as "Sure, I can help with that", "Based on the provided data", or "Here is the summary".
- Be authoritative, concise, and high in relevant information. Use Markdown bullets for supporting details and bold key metrics, categories, entities, and verdicts.
- For structured-data questions, state the answer or primary metric immediately. Format supporting breakdowns as concise bullets with bold labels.
- Never display raw floating-point values for currency or counts. Format monetary values to exactly 2 decimal places with thousands separators and the currency symbol supported by the question or source (for example, **$110,568.71**). If the currency is not identified, use 2 decimal places and thousands separators without inventing a symbol.
- Format whole-number counts and volumes as integers with thousands separators (for example, **1,250** or **328**). Preserve meaningful precision for non-currency, non-count measurements.
- Rounding a reported monetary value to 2 decimal places is permitted for display only. Do not calculate, convert, or infer metrics, currencies, or units.
- For structured-data questions, end with exactly one short analytical takeaway sentence, prefixed with "*Takeaway:*".
- For policy questions, state the definitive rule or verdict in the first sentence. Itemize relevant clauses, timelines, thresholds, and exceptions as bullets, and cite the exact document section or rule name in square brackets when it appears in the supplied context.
- Never output raw JSON, database dumps, or unstyled source excerpts in the conversational answer unless the user explicitly requests them. Summarize only the relevant facts.
- If the context does not contain enough evidence, state that limitation directly; do not guess.

CURRENT AUDIT REPORT:
{report}

DATABASE FINDINGS:
{json.dumps(state.sql_results, indent=2) if state.sql_results else "No database findings available."}

RETRIEVED POLICY CONTEXT:
{json.dumps(state.rag_results, indent=2) if state.rag_results else "No policy context available."}

FOLLOW-UP QUESTION:
{state.user_query}

Answer directly and concisely."""
    response = llm.invoke(prompt).content
    messages = [*state.messages, {"role": "assistant", "content": response}]
    trace.append({"step": "follow_up", "message": "Answered from the current audit context without rerunning SQL."})
    return {
        "messages": messages,
        "current_step": "done",
        "execution_trace": trace,
    }

def rag_node(state: AgentState) -> Dict[str, Any]:
    trace = list(state.execution_trace)
    llm = get_llm()
    
    prompt = f"""Formulate a concise semantic search query (3-6 words) to retrieve relevant rules, definitions, constraints, or policies from the user's uploaded documents:
User request: "{state.user_query}"
Return ONLY the search query string with no explanation."""

    response = llm.invoke(prompt)
    search_query = response.content.strip().strip('"')
    
    trace.append({"step": "rag_search", "rag_query": search_query})
    retrieved = search_policy_documents(search_query, tenant_id=state.tenant_id, top_k=8)
    
    return {
        "rag_query": search_query,
        "rag_results": retrieved,
        "current_step": "rag_complete",
        "execution_trace": trace
    }

def synthesizer_node(state: AgentState) -> Dict[str, Any]:
    trace = list(state.execution_trace)
    llm = get_llm()
    
    sql_text = json.dumps(state.sql_results, indent=2) if state.sql_results else "No SQL data"
    rag_text = "\n---\n".join(
        "Metadata: "
        + json.dumps(result.get("metadata", {}), ensure_ascii=False, default=str)
        + "\n"
        + result["text"]
        for result in (state.rag_results or {}).get("results", [])
    ) or "No documents found"
    
    prompt = f"""You are an elite executive BI analyst and compliance auditor. Synthesize the database results and, when retrieved, relevant corporate policy into a direct, accurate answer to the user's question.

RESPONSE FORMAT:
1. The very first sentence must directly answer the user's primary question. Never begin with conversational filler such as "Sure, I can help with that", "Based on the provided data", or "Here is the summary".
2. For structured-data / SQL questions:
   - State the primary answer or metric immediately.
   - Present supporting numbers and breakdowns as clean Markdown bullets with bold category or entity labels, for example: "**Food and beverages**: Total revenue = **$56,144.84** | Gross income = **$2,673.56**".
   - Never display raw floating-point values for currency or counts. Format monetary values to exactly 2 decimal places with thousands separators and the currency symbol supported by the question or source (for example, **$110,568.71** or **$5,265.18**). If the currency is not identified, use 2 decimal places and thousands separators without inventing a symbol.
   - Format whole-number counts and volumes as integers with thousands separators (for example, **1,250** or **328**). Preserve meaningful precision for non-currency, non-count measurements.
   - Rounding a reported monetary value to 2 decimal places is permitted for display only. Do not calculate, convert, or infer metrics, currencies, or units.
   - Format percentages consistently (for example, **4.76%**) when the result provides the value and its unit.
   - Conclude with exactly one concise analytical sentence prefixed with "*Takeaway:*". Only state comparisons or conclusions directly supported by the query results; do not calculate them yourself.
3. For policy / RAG questions:
   - State the definitive verdict or rule in the first sentence, with the key verdict bolded (for example, "**No.** Staff members are prohibited from …").
   - Itemize relevant clauses, timelines, thresholds, and exceptions as concise bullets.
   - Cite the exact document section or rule name using square brackets (for example, "[Section 5: Vendor Eligibility]") when that title or rule name is present in the retrieved text or metadata. Do not invent a section, rule name, document title, or page.
4. For questions requiring both data and policy, answer directly first, then use clearly labeled **Data** and **Policy** bullets as relevant; finish with one *Takeaway* sentence only when structured-data findings are part of the answer.
5. Use authoritative, concise, high-density Markdown. Bold important metrics, categories, entity names, and key verdicts.
6. Never show raw JSON, raw result dumps, or unstyled document excerpts in the answer unless the user explicitly asks for them. Do not include an **Evidence**, **Interpretation**, **Recommended Next Steps**, or **Action Items** section.

GROUNDING AND ANALYSIS:
- Report only values explicitly present in the database query results. ZERO MATH: do not calculate sums, averages, percentages, differences, rankings, or other metrics yourself. Display-only currency rounding and count separators are permitted as specified above. Do not infer units or format a value as currency or a percentage unless the source supports that unit.
- Never invent a number, tier, threshold, policy, exception, citation, or organization type. Use only facts explicitly present in the database results, retrieved policy context, and the user's question.
- Review all retrieved context chunks, including adjacent clauses, subsections, conditions, exceptions, and membership or option language, before stating that a policy rule or provision is missing. When a policy applies, state the complete rule supported by the retrieved text rather than an isolated sentence.
- If there is insufficient evidence, identify the specific limitation plainly rather than guessing.

User Question:
{state.user_query}

Retrieved Policy Terms (Contract Documentation):
{rag_text}

Database Query Results:
{sql_text}
"""

    response = llm.invoke(prompt)
    executive_summary, detailed_findings = parse_synthesis_response(response.content)
    final_response = "\n\n".join(
        part for part in (executive_summary, detailed_findings) if part
    )
    trace.append({"step": "synthesis", "message": "Final compliance audit generated successfully."})
    
    return {
        "final_response": final_response,
        "executive_summary": executive_summary,
        "detailed_findings": detailed_findings,
        "messages": [*state.messages, {"role": "assistant", "content": response.content}],
        "current_step": "done",
        "execution_trace": trace
    }

def sql_failure_node(state: AgentState) -> Dict[str, Any]:
    trace = list(state.execution_trace)
    trace.append({"step": "sql_failed", "message": "SQL execution failed after the maximum retry count."})
    return {
        "final_response": (
            "The structured-data query could not be completed after "
            f"an initial attempt and {state.retry_count - 1} retries.\n\nError: {state.error_message}"
        ),
        "current_step": "failed",
        "execution_trace": trace,
    }
