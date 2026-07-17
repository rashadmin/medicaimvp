"""
MedicAI MVP — RAG Tool
=======================
tools/rag_tool.py

Handles retrieval-augmented generation for first-aid content.

RAG Pipeline:
  1. Embed the query using the same model used to embed the knowledge base
  2. Search the vector store for top-k relevant chunks
  3. Rerank results by relevance score
  4. Return structured content for the agent to use

The vector store is pre-populated with first-aid content from:
  - WHO first aid guidelines
  - Red Cross manuals
  - Emergency medical protocols

Replace the vector store client with your actual implementation
(Pinecone, Weaviate, Chroma, pgvector, etc.)
"""

from __future__ import annotations

import os
import asyncio
from typing import Any
import faiss, numpy as np, pickle
import pandas as pd
import httpx
from langchain_core.tools import tool
# from langgraph.config import get_stream_writer

# ── at the top of rag_tool.py ─────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ════════════════════════════════════════════════════════════════════════════
#  WEB TOOL TOOL
# ════════════════════════════════════════════════════════════════════════════

# @tool


from tavily import AsyncTavilyClient

FIRST_AID_DOMAINS = [
    "redcross.org",
    "sja.org.uk",
    "nhs.uk",
    "mayoclinic.org",
    "who.int",
    "firstaidforfree.com",
    "healthline.com",
    "webmd.com",
]

async def search_first_aid_web(
    query: str,
    tags: list[str] | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """
    Search trusted first-aid websites for specific protocols and procedures.

    Args:
        query : specific first-aid question or topic
                e.g. "CPR steps for adult cardiac arrest"
                e.g. "how to control severe bleeding from stab wound"
        tags  : optional topic tags (informational only — used for logging)
        top_k : number of results to retrieve (default 5)

    Returns structured first-aid content with source citations.
    """
    print({"event": "rag_search_started", "query": query, "tags": tags})

    try:
        client  = AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY", ""))
        results = await client.search(
            query=query,
            search_depth="advanced",
            max_results=top_k,
            include_domains=FIRST_AID_DOMAINS,
        )

        chunks = [
            {
                "id":       r.get("url", ""),
                "score":    r.get("score", 0.0),
                "content":  r.get("content", ""),
                "metadata": {
                    "source": r.get("url", ""),
                    "title":  r.get("title", ""),
                    "topic":  ", ".join(tags or []),
                },
            }
            for r in results.get("results", [])
            if r.get("content")
        ]

        context = "\n\n---\n\n".join([
            f"[Source: {c['metadata']['title']} | {c['metadata']['source']} | "
            f"Score: {c['score']:.2f}]\n{c['content']}"
            for c in chunks
        ])

        print({"event": "rag_search_complete", "query": query,
                "chunks_found": len(chunks)})

        return {
            "query":        query,
            "context":      context,
            "chunks":       chunks,
            "chunks_found": len(chunks),
            "status":       "success" if chunks else "no_results",
        }

    except Exception as e:
        print({"event": "rag_search_failed", "query": query, "error": str(e)})
        return {
            "query":        query,
            "context":      "",
            "chunks":       [],
            "chunks_found": 0,
            "status":       "failed",
            "error":        str(e),
        }