"""News monitor tool: fetch fresh Google News RSS articles with deduplication."""

import logging
import os
import urllib.parse
from typing import List, Dict, Any

import feedparser
import litellm

from config import DEFAULT_MODEL, OLLAMA_API_BASE


logger = logging.getLogger("mutiny_bot.tools.news_monitor")

try:
    from mempalace.mcp_server import tool_add_drawer
    from mempalace.searcher import search_memories
except Exception as mempalace_import_error:
    tool_add_drawer = None
    search_memories = None
    _MEMPALACE_IMPORT_ERROR = mempalace_import_error
else:
    _MEMPALACE_IMPORT_ERROR = None


async def get_fresh_news(search_query: str, dedup_room: str, palace_path: str) -> List[Dict[str, Any]]:
    """Fetch Google News RSS for search_query (last 24h), dedup via MemPalace, return new articles."""
    if tool_add_drawer is None or search_memories is None:
        logger.warning(
            "MemPalace is unavailable for news deduplication: %s",
            _MEMPALACE_IMPORT_ERROR,
        )
        return []

    os.environ["MEMPALACE_PALACE_PATH"] = palace_path

    encoded_query = urllib.parse.quote_plus(search_query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en&when=1d"
    feed = feedparser.parse(url)

    new_articles = []
    for entry in feed.entries:
        link = str(getattr(entry, "link", "")).strip()
        if not link:
            continue
        # Check if already posted
        results = search_memories(link, palace_path=palace_path, wing="news-monitor", room=dedup_room)
        if not results:
            # New article — do NOT mark here; caller marks after successful broadcast
            title = entry.title
            published = getattr(entry, 'published', getattr(entry, 'updated', ''))
            summary = getattr(entry, 'summary', getattr(entry, 'description', ''))
            new_articles.append({
                "title": title,
                "link": link,
                "published": published,
                "summary": summary
            })
    return new_articles


async def execute_news_monitor(job_data: dict) -> None:
    """Execute news monitor job: fetch articles, generate blurbs, post to channel."""
    from tools.scheduler_manager import _enqueue_broadcast
    
    name = job_data.get("name", "unknown")
    search_query = job_data.get("search_query", "")
    channel_id = job_data.get("channel_id", 0)
    palace_path = job_data.get("palace_path", os.path.expanduser("~/.mutiny/palace"))
    
    articles = await get_fresh_news(search_query, name, palace_path)
    
    if not articles:
        message = f"No new articles found for {name} this morning."
    else:
        blurbs = []
        for article in articles:
            prompt = f"Summarize this news article in 1-2 sentences: {article['title']} - {article['summary']}"
            # Ensure model has provider prefix for litellm
            model_name = DEFAULT_MODEL
            if not model_name.startswith("ollama/"):
                model_name = f"ollama/{model_name}"
            response = await litellm.acompletion(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                api_base=OLLAMA_API_BASE,
            )
            choices = getattr(response, "choices", [])
            message_obj = getattr(choices[0], "message", None) if choices else None
            blurb = getattr(message_obj, "content", "").strip() if message_obj else ""
            blurbs.append(f"📰 {article['title']}\n{blurb}\n{article['link']}\n")
        message = "\n\n".join(blurbs)
    
    await _enqueue_broadcast(message, channel_id)

    # Mark articles as posted only after successful broadcast (two-phase commit).
    if articles and tool_add_drawer is not None:
        os.environ["MEMPALACE_PALACE_PATH"] = palace_path
        for article in articles:
            try:
                tool_add_drawer(
                    wing="news-monitor",
                    room=name,
                    content=article["link"],
                    added_by="news_monitor",
                )
            except Exception as e:
                logger.error("Failed to mark article as posted: %s", e)