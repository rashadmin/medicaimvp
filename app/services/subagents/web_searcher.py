"""Web Searcher Subagent - Searches first-aid knowledge base"""
import os
from deepagents import AsyncSubAgent

web_searcher = AsyncSubAgent(
    name="web_searcher",
    description=(
        "Async web search agent. Searches the first-aid knowledge base for a specific query. "
        "Launch one per search query — multiple can run in parallel simultaneously. "
        "Pass task JSON: { query, tags, search_id, speculative }. "
        "Non-blocking — returns task_id immediately. "
        "Check results with check_async_task. "
        "Cancel speculative searches with cancel_async_task if no longer needed."
    ),
    graph_id="web_searcher",
    url=os.getenv("SUBAGENT_URL", "http://localhost:8000")
)
