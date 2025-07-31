import os
from typing import Annotated

from tavily import AsyncTavilyClient

async def search(query: Annotated[
            str,
            "Provide a detailed search instruction that incorporates specific features, goals, and contextual details related to the query. \
                                                    Identify and include relevant aspects from any provided context, such as key topics, technologies, challenges, timelines, or use cases. \
                                                    Construct the instruction to enable a targeted search by specifying important attributes, keywords, and relationships within the context.",
        ], domains: list[str] | None = None, max_results: int = 7) -> list[dict[str, str]]:
    """
    Searches the query using Tavily Search API, optionally restricting to specific domains
    Returns:
        list: List of search results with title, href and body
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    tavily_client = AsyncTavilyClient(api_key)

    results = await tavily_client.search(query=query, max_results=max_results, domains=domains)
    search_results = []

    # Normalizing results to match the format of the other search APIs
    for result in results["results"]:
        # skip youtube results
        if "youtube.com" in result["url"]:
            continue
        try:
            search_result = {
                "title": result["title"],
                "href": result["url"],
                "body": result["content"],
            }
        except Exception:
            continue
        search_results.append(search_result)

    return search_results
