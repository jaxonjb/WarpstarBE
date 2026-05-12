from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId

from ..core.database import get_db
from ..core.security import get_current_user_optional
from ..core.utils import serialize_doc, serialize_docs

router = APIRouter(prefix="/api/games", tags=["games"])


def _build_filter(
    q: str | None,
    genre: str | None,
    platform: str | None,
    theme: str | None,
) -> dict:
    f: dict = {}
    if q:
        f["$text"] = {"$search": q}
    if genre:
        try:
            f["genreIds"] = ObjectId(genre)
        except Exception:
            pass
    if platform:
        try:
            f["platformIds"] = ObjectId(platform)
        except Exception:
            pass
    if theme:
        try:
            f["themeIds"] = ObjectId(theme)
        except Exception:
            pass
    return f


@router.get("/")
async def list_games(
    q:        str | None = Query(None, description="Full-text search"),
    genre:    str | None = Query(None),
    platform: str | None = Query(None),
    theme:    str | None = Query(None),
    sort:     str        = Query("reviewTotal", enum=["reviewTotal", "igdbRating", "releaseDate", "name"]),
    skip:     int        = Query(0,  ge=0),
    limit:    int        = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    filt      = _build_filter(q, genre, platform, theme)
    direction = 1 if sort == "name" else -1
    cursor    = db.games.find(filt).sort(sort, direction).skip(skip).limit(limit)
    games     = await cursor.to_list(length=limit)
    total     = await db.games.count_documents(filt)
    return {"total": total, "skip": skip, "limit": limit, "results": serialize_docs(games)}


@router.get("/{game_id}")
async def get_game(game_id: str, db=Depends(get_db)):
    try:
        oid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")

    game = await db.games.find_one({"_id": oid})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found.")
    return serialize_doc(game)


@router.get("/{game_id}/similar")
async def get_similar_games(game_id: str, limit: int = Query(10, ge=1, le=50), db=Depends(get_db)):
    try:
        oid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")

    game = await db.games.find_one({"_id": oid}, {"similarTo": 1})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found.")

    similar_ids = (game.get("similarTo") or [])[:limit]
    if not similar_ids:
        return []

    games = await db.games.find({"_id": {"$in": similar_ids}}).to_list(length=limit)
    return serialize_docs(games)


@router.get("/{game_id}/reviews")
async def get_game_reviews(
    game_id: str,
    skip:  int = Query(0,  ge=0),
    limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")

    cursor  = db.reviews.find({"gameId": oid}).sort("createdAt", -1).skip(skip).limit(limit)
    reviews = await cursor.to_list(length=limit)
    total   = await db.reviews.count_documents({"gameId": oid})
    return {"total": total, "skip": skip, "limit": limit, "results": serialize_docs(reviews)}


# ---------------------------------------------------------------------------
# Lookup endpoints (platforms, genres, themes)
# ---------------------------------------------------------------------------

@router.get("/meta/platforms")
async def list_platforms(db=Depends(get_db)):
    return serialize_docs(await db.platforms.find().sort("name", 1).to_list(length=None))


@router.get("/meta/genres")
async def list_genres(db=Depends(get_db)):
    return serialize_docs(await db.genres.find().sort("name", 1).to_list(length=None))


@router.get("/meta/themes")
async def list_themes(db=Depends(get_db)):
    return serialize_docs(await db.themes.find().sort("name", 1).to_list(length=None))
