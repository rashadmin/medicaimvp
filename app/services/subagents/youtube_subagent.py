"""YouTube Subagent - Finds instructional first-aid videos"""
import os
from deepagents import AsyncSubAgent

youtube_subagent = AsyncSubAgent(
    name="youtube_subagent",
    description=(
        "Async YouTube search agent. Finds instructional first-aid videos. "
        "Launch after web results are in and you know what technique to demonstrate. "
        "Pass task JSON: { query: '<specific technique>' }. "
        "Non-blocking — frontend polls /session/videos for results."
    ),
    graph_id="youtube_subagent",
    url=os.getenv("SUBAGENT_URL", "http://localhost:8000")
)
