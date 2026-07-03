from litestar import get
from litestar.connection import Request


@get("/whoami")
async def whoami(request: Request) -> dict:
    """Example protected endpoint. `request.user` is the key's team id."""
    return {"team_id": request.user}
