"""
MedicAI — YouTube Searcher (Async Subagent)
=============================================
youtube_searcher.py

Registered in langgraph.json as graph_id="youtube_searcher".
Launched non-blocking by the supervisor whenever the user asks a question
that would benefit from a visual demonstration (CPR, recovery position, etc).

Responsibilities:
  1. Receive a search query from the supervisor task description
  2. Search YouTube Data API v3 for relevant first-aid videos
  3. Write structured video results into its state
  4. The supervisor's GET /session/videos endpoint reads this state via
     check_async_task and returns the URLs to the frontend

State written back to supervisor:
  { "videos": [ { title, url, thumbnail, channel, duration }, ... ] }
"""

from __future__ import annotations

import json
import os
from typing import TypedDict, Annotated

import httpx
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.config import get_stream_writer
from .youtube import search_youtube

import os
from dotenv import load_dotenv
load_dotenv() 

# ════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ════════════════════════════════════════════════════════════════════════════

YOUTUBE_PROMPT = """
You are the MedicAI YouTube Searcher, a background subagent.
Your task arrives as a JSON string: { "query": "..." }

Workflow:
1. write_todos:
   - [ ] Search YouTube for query

2. search_youtube(query=<from task>, max_results=5)

3. Return the full list of video results as your final response.
   Format:
   VIDEOS_READY: <JSON array of video objects>

Rules:
- Always run search_youtube — never skip it.
- Return results immediately. Do not summarise or editoralise.
- If the search returns no results, return: VIDEOS_READY: []
""".strip()


# ════════════════════════════════════════════════════════════════════════════
#  AGENT
# ════════════════════════════════════════════════════════════════════════════

_llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    google_api_key=os.environ.get("SUBAGENT_GOOGLE_API_KEY"),
    temperature=0,
    max_retries=2,
    timeout=30,
)

_agent = create_react_agent(
    model=_llm,
    tools=[search_youtube],
    prompt=YOUTUBE_PROMPT,
)

# langgraph.json entry point
graph = _agent