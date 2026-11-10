"""Optional OpenAI-compatible embeddings transport, independent of the chat provider."""
import asyncio
import math
import time

import aiohttp

from app.core.config import settings
from app.services import usage


def normalize(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 8192:
        raise ValueError("Invalid embedding dimensions")
    if any(type(item) not in (int, float) or not math.isfinite(item) for item in value):
        raise ValueError("Invalid embedding values")
    length = math.sqrt(sum(item * item for item in value))
    if not length or not math.isfinite(length):
        raise ValueError("Invalid embedding norm")
    return tuple(item / length for item in value)


class Embeddings:
    def __init__(self):
        self._catalog = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self):
        return bool(settings.embedding_base_url and settings.embedding_model)

    async def encode(self, texts):
        if not self.enabled or not 1 <= len(texts) <= 128 or any(not isinstance(text, str) or len(text) > 2000 for text in texts):
            raise ValueError("Embedding request unavailable or too large")
        headers = {"Authorization": f"Bearer {settings.embedding_api_key}"} if settings.embedding_api_key else {}
        started = time.monotonic()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as client:
            async with client.post(settings.embedding_base_url + "/embeddings", headers=headers,
                                   json={"model": settings.embedding_model, "input": texts, "encoding_format": "float"}) as response:
                if response.status != 200:
                    raise ValueError("Embedding provider unavailable")
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("Embedding response too large")
        import json
        body = json.loads(raw)
        rows = sorted(body["data"], key=lambda row: row["index"])
        if [row["index"] for row in rows] != list(range(len(texts))):
            raise ValueError("Invalid embedding indexes")
        vectors = [normalize(row["embedding"]) for row in rows]
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError("Inconsistent embedding dimensions")
        await usage.record("embedding", settings.embedding_model, body.get("usage"), int((time.monotonic() - started) * 1000))
        return vectors

    async def rank(self, text, catalog):
        """Cache only a bounded, public static catalog. User text and its vectors aren't retained."""
        key = (settings.embedding_base_url, settings.embedding_model, tuple(catalog))
        async with self._lock:
            vectors = self._catalog.get(key)
            if vectors is None:
                vectors = await self.encode(list(catalog))
                if len(self._catalog) >= 4:
                    self._catalog.clear()
                self._catalog[key] = vectors
        query = (await self.encode([text]))[0]
        if len(query) != len(vectors[0]):
            raise ValueError("Embedding model dimensions changed")
        return sorted(((sum(a * b for a, b in zip(query, vector)), index) for index, vector in enumerate(vectors)), reverse=True)


embeddings = Embeddings()
