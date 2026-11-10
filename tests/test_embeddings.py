import dataclasses

import pytest
from aiohttp import web

from services import embeddings as module


@pytest.mark.parametrize("value", [[], [0, 0], [float("nan")], [float("inf")], [True], ["1"]])
def test_invalid_vectors_are_rejected(value):
    with pytest.raises(ValueError):
        module.normalize(value)


async def test_embedding_transport_catalog_cache_and_usage(monkeypatch):
    requests, recorded = [], []
    async def endpoint(request):
        body = await request.json()
        requests.append(body)
        return web.json_response({"data": [{"index": i, "embedding": [1, i]} for i, _ in enumerate(body["input"])],
                                  "usage": {"prompt_tokens": 3, "total_tokens": 3}})
    app = web.Application()
    app.router.add_post("/embeddings", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(module, "settings", dataclasses.replace(module.settings, embedding_base_url=f"http://127.0.0.1:{port}", embedding_model="test", embedding_api_key=""))
    async def record(*args): recorded.append(args)
    monkeypatch.setattr(module.usage, "record", record)
    try:
        adapter = module.Embeddings()
        assert (await adapter.rank("query one", ["catalog a", "catalog b"]))[0][1] == 0
        await adapter.rank("query two", ["catalog a", "catalog b"])
        assert [item["input"] for item in requests] == [["catalog a", "catalog b"], ["query one"], ["query two"]]
        assert len(recorded) == 3 and all(item[0] == "embedding" for item in recorded)
        assert "query one" not in repr(adapter._catalog)
    finally:
        await runner.cleanup()
