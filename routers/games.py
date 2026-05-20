import re
from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId

from core.database import get_db
from core.security import get_current_user_optional
from core.utils import serialize_doc, serialize_docs

router = APIRouter(prefix="/api/games", tags=["games"])


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

async def _build_lookup_maps(db) -> dict:
    """
    Load genres, themes, platforms, and keywords into igdbId->name maps.
    Games store IGDB numeric IDs — the lookup collections store those in igdbId.
    Both int and string keys are stored so either format matches.
    """
    genres    = await db.genres.find({},    {"igdbId": 1, "name": 1}).to_list(length=None)
    themes    = await db.themes.find({},    {"igdbId": 1, "name": 1}).to_list(length=None)
    platforms = await db.platforms.find({}, {"igdbId": 1, "name": 1}).to_list(length=None)
    keywords  = await db.keywords.find({},  {"igdbId": 1, "name": 1}).to_list(length=None)

    def make_map(docs):
        m = {}
        for d in docs:
            igdb_id = d.get("igdbId")
            if igdb_id is not None:
                m[str(igdb_id)] = d["name"]
                try:
                    m[int(igdb_id)] = d["name"]
                except (ValueError, TypeError):
                    pass
        return m

    return {
        "genres":    make_map(genres),
        "themes":    make_map(themes),
        "platforms": make_map(platforms),
        "keywords":  make_map(keywords),
    }


def _resolve_game(game: dict, maps: dict) -> dict:
    """
    Serialize a game doc and add resolved name lists alongside the
    existing ID arrays so the frontend can use either.
      genreIds    -> genres    (list of name strings)
      themeIds    -> themes
      platformIds -> platforms
      keywordIds  -> keywords  (capped at 10 for response size)
    Also ensures igdbRatingCount is present (defaults to 0).
    """
    doc = serialize_doc(game)
    if doc is None:
        return doc

    doc["genres"]    = [maps["genres"].get(gid)    for gid in (doc.get("genreIds")    or []) if maps["genres"].get(gid)]
    doc["themes"]    = [maps["themes"].get(tid)    for tid in (doc.get("themeIds")    or []) if maps["themes"].get(tid)]
    doc["platforms"] = [maps["platforms"].get(pid) for pid in (doc.get("platformIds") or []) if maps["platforms"].get(pid)]
    doc["keywords"]  = [maps["keywords"].get(kid)  for kid in (doc.get("keywordIds")  or [])[:10] if maps["keywords"].get(kid)]

    # Ensure igdbRatingCount is always present
    if "igdbRatingCount" not in doc:
        doc["igdbRatingCount"] = 0

    return doc


def _resolve_games(games: list, maps: dict) -> list:
    return [_resolve_game(g, maps) for g in games]



async def _enrich_reviews(reviews: list, db) -> list:
    """Add username and avatar to each review doc."""
    if not reviews:
        return reviews
    # Collect unique user IDs
    user_ids = list({r["userId"] for r in reviews if r.get("userId")})
    users = await db.users.find(
        {"_id": {"$in": user_ids}},
        {"_id": 1, "username": 1, "preferences": 1}
    ).to_list(length=None)
    user_map = {
        str(u["_id"]): {
            "username": u.get("username", "unknown"),
            "avatar":   u.get("preferences", {}).get("profilePicture"),
        }
        for u in users
    }
    enriched = []
    for r in reviews:
        doc = serialize_doc(r)
        uid = str(r.get("userId", ""))
        doc["username"] = user_map.get(uid, {}).get("username", "unknown")
        doc["avatar"]   = user_map.get(uid, {}).get("avatar")
        enriched.append(doc)
    return enriched

async def _build_filter(
    q: str | None,
    genre: str | None,
    platform: str | None,
    theme: str | None,
    db = None,
) -> dict:
    """
    Build a MongoDB filter dict. Genre/platform/theme params can be:
      - An IGDB numeric ID (int or string like "12")
      - A genre name string like "Action" (resolved via genres collection)
    """
    f: dict = {}
    if q:
        f["name"] = {"$regex": re.escape(q.strip()), "$options": "i"}

    if genre and db is not None:
        # Try as integer IGDB ID first
        try:
            igdb_id = int(genre)
            f["genreIds"] = igdb_id
        except ValueError:
            # Otherwise look up by name in the genres collection
            genre_doc = await db.genres.find_one(
                {"name": {"$regex": f"^{re.escape(genre)}$", "$options": "i"}},
                {"igdbId": 1}
            )
            if genre_doc:
                try:
                    f["genreIds"] = int(genre_doc["igdbId"])
                except (ValueError, TypeError):
                    f["genreIds"] = genre_doc["igdbId"]

    if platform and db is not None:
        try:
            f["platformIds"] = int(platform)
        except ValueError:
            platform_doc = await db.platforms.find_one(
                {"name": {"$regex": f"^{re.escape(platform)}$", "$options": "i"}},
                {"igdbId": 1}
            )
            if platform_doc:
                try:
                    f["platformIds"] = int(platform_doc["igdbId"])
                except (ValueError, TypeError):
                    f["platformIds"] = platform_doc["igdbId"]

    if theme and db is not None:
        try:
            f["themeIds"] = int(theme)
        except ValueError:
            theme_doc = await db.themes.find_one(
                {"name": {"$regex": f"^{re.escape(theme)}$", "$options": "i"}},
                {"igdbId": 1}
            )
            if theme_doc:
                try:
                    f["themeIds"] = int(theme_doc["igdbId"])
                except (ValueError, TypeError):
                    f["themeIds"] = theme_doc["igdbId"]

    return f


# ---------------------------------------------------------------------------
# Game list + search
# ---------------------------------------------------------------------------

@router.get("/")
async def list_games(
    q:        str | None = Query(None, description="Search by name"),
    genre:    str | None = Query(None, description="Filter by genre ID"),
    platform: str | None = Query(None, description="Filter by platform ID"),
    theme:    str | None = Query(None, description="Filter by theme ID"),
    sort:     str        = Query("reviewTotal", enum=["reviewTotal", "igdbRating", "igdbRatingCount", "topRated", "releaseDate", "name"]),
    skip:     int        = Query(0,  ge=0),
    limit:    int        = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    filt = await _build_filter(q, genre, platform, theme, db)

    # topRated: prioritise games reviewed on Warpstar first (reviewTotal > 0),
    # then fall back to IGDB rating with a minimum of 20 IGDB ratings
    if sort == "topRated":
        IGDB_MIN_RATINGS = 50
        pipeline = [
            {"$match": filt},
            {"$addFields": {
                # Tier 0 = has Warpstar reviews, Tier 1 = IGDB only (meets threshold)
                "_tier": {
                    "$cond": [{"$gt": ["$reviewTotal", 0]}, 0, 1]
                },
                # Within tier 1, exclude games below the IGDB rating count threshold
                "_igdbCount": {"$ifNull": ["$igdbRatingCount", 0]},
            }},
            # Drop IGDB-only games that don't meet the minimum rating count
            {"$match": {
                "$or": [
                    {"reviewTotal": {"$gt": 0}},
                    {"_igdbCount": {"$gte": IGDB_MIN_RATINGS}},
                ]
            }},
            {"$sort": {
                "_tier":        1,   # Warpstar-reviewed games first
                "reviewTotal":  -1,  # Within tier 0: most reviewed first
                "igdbRating":   -1,  # Within tier 1: highest IGDB rating first
            }},
            {"$skip":  skip},
            {"$limit": limit},
        ]
        count_pipeline = [
            {"$match": filt},
            {"$addFields": {"_igdbCount": {"$ifNull": ["$igdbRatingCount", 0]}}},
            {"$match": {"$or": [
                {"reviewTotal": {"$gt": 0}},
                {"_igdbCount": {"$gte": IGDB_MIN_RATINGS}},
            ]}},
            {"$count": "total"},
        ]
        games_cursor  = db.games.aggregate(pipeline)
        count_cursor  = db.games.aggregate(count_pipeline)
        games         = await games_cursor.to_list(length=limit)
        count_result  = await count_cursor.to_list(length=1)
        total         = count_result[0]["total"] if count_result else 0
        maps          = await _build_lookup_maps(db)
        return {
            "total":   total,
            "skip":    skip,
            "limit":   limit,
            "results": _resolve_games(games, maps),
        }

    direction = 1 if sort == "name" else -1
    cursor    = db.games.find(filt).sort(sort, direction).skip(skip).limit(limit)
    games     = await cursor.to_list(length=limit)
    total     = await db.games.count_documents(filt)
    maps      = await _build_lookup_maps(db)

    return {
        "total":   total,
        "skip":    skip,
        "limit":   limit,
        "results": _resolve_games(games, maps),
    }


# ---------------------------------------------------------------------------
# Meta endpoints — must be defined BEFORE /{game_id} to avoid route conflict
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


# ---------------------------------------------------------------------------
# Single game
# ---------------------------------------------------------------------------

@router.get("/{game_id}")
async def get_game(game_id: str, db=Depends(get_db)):
    try:
        oid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")

    game = await db.games.find_one({"_id": oid})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found.")

    maps = await _build_lookup_maps(db)
    return _resolve_game(game, maps)


@router.get("/{game_id}/similar")
async def get_similar_games(
    game_id: str,
    limit: int = Query(10, ge=1, le=50),
    db=Depends(get_db),
):
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
    maps  = await _build_lookup_maps(db)
    return _resolve_games(games, maps)


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
    enriched = await _enrich_reviews(reviews, db)
    return {"total": total, "skip": skip, "limit": limit, "results": enriched}