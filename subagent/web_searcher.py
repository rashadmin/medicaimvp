"""
MedicAI MVP — WEB Searcher (create_react_agent)
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

from .web_tool import search_first_aid_web

SYSTEM_PROMPT = """
You are a MedicAI First Aid Search agent. You run as a background subagent.

Your task arrives as a JSON string:
{
  "query":       "<specific first-aid search query>",
  "tags":        ["<topic tags>"],
  "search_id":   "<unique label>",
  "speculative": true | false
}

Workflow:
1. Parse the task JSON
2. Call search_first_aid_web(query=<query>, tags=<from langchain_google_genai import GoogleGenerativeAIEmbeddings
tags>)
3. Return the result clearly formatted:

SEARCH_ID: <search_id>
QUERY: <query>
SPECULATIVE: <true|false>
RESULT:
<full context from web search>
SOURCES: <list of source URLs>

Rules:
- Always call search_first_aid_web — never skip it.
- Return results immediately and completely.
- If search returns no results, return: RESULT: No results found.
- If search fails, return the error clearly.
- Do not summarise or editorialise — return the full content.
""".strip()

llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    google_api_key=os.environ.get("SUBAGENT_GOOGLE_API_KEY"),
    temperature=0,
    max_retries=2,
    timeout=30,
)

graph = create_react_agent(
    model=llm,
    tools=[search_first_aid_web],
    prompt=SystemMessage(SYSTEM_PROMPT),
)

print("[web_searcher] ✅ graph compiled (create_react_agent, gemini-2.0-flash-lite)", flush=True)