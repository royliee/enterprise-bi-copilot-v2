from langgraph.graph import StateGraph, END
from backend.app.agent.state import AgentState
from backend.app.agent.nodes import (
    follow_up_chat_node,
    initialize_run_node,
    planner_node,
    rag_router_node,
    router_node,
    sql_node,
    rag_node,
    synthesizer_node,
    sql_failure_node,
)

def build_bi_agent_graph():
    workflow = StateGraph(AgentState)

    # Register nodes
    workflow.add_node("planner", planner_node)
    workflow.add_node("sql_executor", sql_node)
    workflow.add_node("rag_executor", rag_node)
    workflow.add_node("synthesizer", synthesizer_node)
    workflow.add_node("sql_failure", sql_failure_node)
    workflow.add_node("initialize_run", initialize_run_node)
    workflow.add_node("router", router_node)
    workflow.add_node("follow_up_chat", follow_up_chat_node)
    workflow.add_node("rag_router", rag_router_node)

    # Set edge flow
    workflow.set_entry_point("router")
    workflow.add_conditional_edges(
        "router",
        lambda state: state.conversation_route,
        {"new_audit": "initialize_run", "follow_up": "rag_router"},
    )
    workflow.add_edge("initialize_run", "planner")
    workflow.add_edge("planner", "rag_router")
    workflow.add_conditional_edges(
        "rag_router",
        lambda state: (
            "rag" if state.rag_routed else
            "follow_up" if state.conversation_route == "follow_up" else
            "sql_only"
        ),
        {
            "rag": "rag_executor",
            "follow_up": "follow_up_chat",
            "sql_only": "sql_executor",
        },
    )
    workflow.add_conditional_edges(
        "rag_executor",
        lambda state: (
            "follow_up" if state.conversation_route == "follow_up" else
            "sql" if state.sql_routed else
            "synthesis"
        ),
        {
            "follow_up": "follow_up_chat",
            "sql": "sql_executor",
            "synthesis": "synthesizer",
        },
    )
    workflow.add_conditional_edges(
        "sql_executor",
        lambda state: (
            "logical_retry" if state.current_step == "sql_logical_retry" else
            "retry" if state.error_message and state.retry_count <= 3 else
            "failed" if state.error_message else "success"
        ),
        {
            "logical_retry": "sql_executor",
            "retry": "sql_executor",
            "failed": "sql_failure",
            "success": "synthesizer",
        },
    )
    workflow.add_edge("sql_failure", END)
    workflow.add_edge("synthesizer", END)
    workflow.add_edge("follow_up_chat", END)

    return workflow.compile()

bi_agent = build_bi_agent_graph()
