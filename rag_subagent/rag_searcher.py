"""
MedicAI MVP — RAG Searcher (create_react_agent)
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from .rag_tool import search_first_aid_rag

SYSTEM_PROMPT = """
You are a MedicAI RAG Search agent. You run as a background subagent.
Your task arrives as a JSON string:
{
  "query":       "<specific first-aid search query>",
  "tags":        ["<topic tags>"],
  "search_id":   "<unique label>",
  "speculative": true | false
}
Workflow:
1. Parse the task JSON
2. Call search_first_aid_rag(query=<query>, tags=<tags>)
3. Return the result clearly formatted:
SEARCH_ID: <search_id>
QUERY: <query>
SPECULATIVE: <true|false>
RESULT:
<full context from RAG>
SOURCES: <list of sources>
Rules:
- Always call search_first_aid_rag — never skip it.
- Return results immediately and completely.
- If search fails, return the error clearly.
""".strip()

llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    google_api_key=os.environ.get("GOOGLE_API_KEY"),
    temperature=0,
    max_retries=2,
    timeout=30,
)

graph = create_react_agent(
    model=llm,
    tools=[search_first_aid_rag],
    prompt=SystemMessage(SYSTEM_PROMPT),
)

print("[rag_searcher] ✅ graph compiled (create_react_agent, gemini-2.0-flash-lite)", flush=True)