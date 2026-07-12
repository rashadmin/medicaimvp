from langchain_core.tools import tool
from typing import Any
from langgraph.config import get_stream_writer
import asyncio
import json
import os
from langchain.chat_models import init_chat_model
import httpx
# writer = get_stream_writer()
from dotenv import load_dotenv
load_dotenv() 
@tool
async def search_youtube(query: str, max_results: int = 5) -> list[dict]:
    """
    Search YouTube Data API v3 for first-aid / medical instruction videos.
    Returns a list of { title, url, thumbnail, channel, description } dicts.
    Always append "first aid tutorial" or "how to" to the query for relevance.
    """
    # writer = get_stream_writer()
    # writer({"event": "youtube_search_started", "query": query})

    api_key = os.getenv("YOUTUBE_API_KEY", "")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part":        "snippet",
                "q":           f"{query} first aid tutorial",
                "type":        "video",
                "maxResults":  max_results,
                "relevanceLanguage": "en",
                "safeSearch":  "strict",
                "key":         api_key,
            },
        )
        data = resp.json()

    videos = []
    for item in data.get("items", []):
        video_id = item["id"]["videoId"]
        snippet  = item["snippet"]
        videos.append({
            "title":       snippet.get("title", ""),
            "url":         f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail":   snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
            "channel":     snippet.get("channelTitle", ""),
            "description": snippet.get("description", "")[:200],
        })

    # writer({"event": "youtube_results_ready", "count": len(videos),
    #         "videos": videos})
    return videos