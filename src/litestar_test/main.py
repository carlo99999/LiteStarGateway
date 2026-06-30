from litestar import Litestar, get


@get("/health")
async def health() -> dict:
    return {"status": "ok"}

app = Litestar(route_handlers=[health])
