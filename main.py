from fastapi import FastAPI, Request, Depends
 # type: ignore[import]
from fastapi.middleware.cors import CORSMiddleware  # type: ignore[import]
from fastapi.responses import JSONResponse, Response
from xml.sax.saxutils import escape
from contextlib import asynccontextmanager
import traceback

from routers import auth, feed, games, lists, reviews, recommendations, users, notifications

from core.config import get_settings
from core.database import get_client, close_db, get_db
from routers import users

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — verify DB connection
    client = get_client()
    await client.admin.command("ping")
    print(f"✅  Connected to MongoDB ({settings.db_name})")
    yield
    # Shutdown
    await close_db()
    print("MongoDB connection closed.")


app = FastAPI(
    title="GameDB API",
    version="1.0.0",
    description="Backend API for the game review and discovery platform.",
    lifespan=lifespan,
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {str(exc)}"}
    )
# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(games.router)
app.include_router(recommendations.router)
app.include_router(reviews.router)
app.include_router(lists.router)
app.include_router(feed.router)
app.include_router(notifications.router)


@app.api_route("/health", methods=["GET", "HEAD"], tags=["health"])
async def health(db=Depends(get_db)):
    """
    Liveness probe used by UptimeRobot. Also issues a no-op MongoDB ping
    as a side effect so the Atlas connection pool stays warm — without
    this, the first real DB query after a quiet stretch pays cold-
    connection latency even when the container itself is warm.

    Always returns 200 (even if the DB ping fails) so transient Mongo
    blips don't make UptimeRobot false-alert or Railway restart us.
    """
    try:
        await db.command("ping")
    except Exception:
        pass
    return {"status": "ok"}


# Public site origin used to build absolute URLs in the sitemap.
SITE_ORIGIN = "https://warpstar.space"
# Stay well under the 50k-URL / 50MB sitemap limits.
_SITEMAP_GAME_CAP = 45000


@app.get("/sitemap.xml", tags=["seo"], include_in_schema=False)
async def sitemap(db=Depends(get_db)):
    """
    Generates an XML sitemap of indexable pages: the static entry points,
    every genre, and every game that has at least one review (reviewed games
    carry unique content — unreviewed pages are thin, so we skip them).
    """
    urls: list[str] = [f"{SITE_ORIGIN}/", f"{SITE_ORIGIN}/explore"]

    # Genres
    async for g in db.genres.find({}, {"name": 1}):
        name = (g.get("name") or "").strip().lower()
        if name:
            urls.append(f"{SITE_ORIGIN}/genre/{escape(name)}")

    # Reviewed games
    cursor = db.games.find({"reviewTotal": {"$gt": 0}}, {"_id": 1}).limit(_SITEMAP_GAME_CAP)
    async for game in cursor:
        urls.append(f"{SITE_ORIGIN}/game/{game['_id']}")

    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )
    return Response(content=xml, media_type="application/xml")
