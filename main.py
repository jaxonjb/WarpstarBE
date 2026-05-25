from fastapi import FastAPI, Request
 # type: ignore[import]
from fastapi.middleware.cors import CORSMiddleware  # type: ignore[import]
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import traceback

from routers import auth, feed, games, lists, reviews, recommendations, users

from core.config import get_settings
from core.database import get_client, close_db
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


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}
