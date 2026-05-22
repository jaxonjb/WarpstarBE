import re
import time
import asyncio
import unicodedata
from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId

from core.database import get_db
from core.utils import serialize_doc, serialize_docs

router = APIRouter(prefix="/api/games", tags=["games"])


# ---------------------------------------------------------------------------
# In-memory lookup map cache
# Lookup tables (genres, platforms etc.) rarely change so we cache them for
# 10 minutes instead of hitting MongoDB on every request.
# ---------------------------------------------------------------------------

_LOOKUP_CACHE: dict = {}
_CACHE_TTL    = 600  # seconds


async def _build_lookup_maps(db) -> dict:
    global _LOOKUP_CACHE

    now = time.monotonic()
    if _LOOKUP_CACHE.get("_expires", 0) > now:
        return _LOOKUP_CACHE

    genres, themes, platforms, keywords, companies = await asyncio.gather(
        db.genres.find({},    {"igdbId": 1, "name": 1}).to_list(length=None),
        db.themes.find({},    {"igdbId": 1, "name": 1}).to_list(length=None),
        db.platforms.find({}, {"igdbId": 1, "name": 1}).to_list(length=None),
        db.keywords.find({},  {"igdbId": 1, "name": 1}).to_list(length=None),
        db.companies.find({}, {"igdbId": 1, "name": 1}).to_list(length=None),
    )

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

    _LOOKUP_CACHE = {
        "genres":    make_map(genres),
        "themes":    make_map(themes),
        "platforms": make_map(platforms),
        "keywords":  make_map(keywords),
        "companies": make_map(companies),
        "_expires":  now + _CACHE_TTL,
    }
    return _LOOKUP_CACHE


# ---------------------------------------------------------------------------
# Game resolver
# ---------------------------------------------------------------------------

def _resolve_game(game: dict, maps: dict) -> dict:
    doc = serialize_doc(game)
    if doc is None:
        return doc

    doc["genres"]     = [maps["genres"].get(gid)    for gid in (doc.get("genreIds")     or []) if maps["genres"].get(gid)]
    doc["themes"]     = [maps["themes"].get(tid)    for tid in (doc.get("themeIds")     or []) if maps["themes"].get(tid)]
    doc["platforms"]  = [maps["platforms"].get(pid) for pid in (doc.get("platformIds")  or []) if maps["platforms"].get(pid)]
    doc["keywords"]   = [maps["keywords"].get(kid)  for kid in (doc.get("keywordIds")   or [])[:10] if maps["keywords"].get(kid)]
    doc["developers"] = [maps["companies"].get(cid) for cid in (doc.get("developerIds") or [])[:5]  if maps["companies"].get(cid)]
    doc["publishers"] = [maps["companies"].get(cid) for cid in (doc.get("publisherIds") or [])[:5]  if maps["companies"].get(cid)]

    if "igdbRatingCount" not in doc:
        doc["igdbRatingCount"] = 0

    return doc


def _resolve_games(games: list, maps: dict) -> list:
    return [_resolve_game(g, maps) for g in games]


# ---------------------------------------------------------------------------
# Filter builder
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


async def _build_filter(q, genre, platform, theme, db=None) -> dict:
    f: dict = {}

    if q:
        raw       = q.strip()
        ascii_q   = _normalize(raw)
        esc_raw   = re.escape(raw)
        esc_ascii = re.escape(ascii_q)

        # Always search nameNormalized with the ascii-stripped query —
        # it's indexed and handles accents efficiently.
        # Only add name search as a fallback for the original query.
        if esc_raw == esc_ascii:
            f["$or"] = [
                {"nameNormalized": {"$regex": esc_ascii, "$options": "i"}},
                {"name":           {"$regex": esc_raw,   "$options": "i"}},
            ]
        else:
            f["$or"] = [
                {"nameNormalized": {"$regex": esc_ascii, "$options": "i"}},
                {"name":           {"$regex": esc_raw,   "$options": "i"}},
            ]

    if genre and db is not None:
        try:
            f["genreIds"] = int(genre)
        except ValueError:
            genre_doc = await db.genres.find_one(
                {"name": {"$regex": f"^{re.escape(genre)}$", "$options": "i"}},
                {"igdbId": 1}
            )
            if genre_doc:
                try:    f["genreIds"] = int(genre_doc["igdbId"])
                except: f["genreIds"] = genre_doc["igdbId"]

    if platform and db is not None:
        try:
            f["platformIds"] = int(platform)
        except ValueError:
            pdoc = await db.platforms.find_one(
                {"name": {"$regex": f"^{re.escape(platform)}$", "$options": "i"}},
                {"igdbId": 1}
            )
            if pdoc:
                try:    f["platformIds"] = int(pdoc["igdbId"])
                except: f["platformIds"] = pdoc["igdbId"]

    if theme and db is not None:
        try:
            f["themeIds"] = int(theme)
        except ValueError:
            tdoc = await db.themes.find_one(
                {"name": {"$regex": f"^{re.escape(theme)}$", "$options": "i"}},
                {"igdbId": 1}
            )
            if tdoc:
                try:    f["themeIds"] = int(tdoc["igdbId"])
                except: f["themeIds"] = tdoc["igdbId"]

    return f


# ---------------------------------------------------------------------------
# List games
# ---------------------------------------------------------------------------

@router.get("/")
async def list_games(
    q:        str | None = Query(None),
    genre:    str | None = Query(None),
    platform: str | None = Query(None),
    theme:    str | None = Query(None),
    sort:     str        = Query("reviewTotal", enum=["reviewTotal", "igdbRating", "igdbRatingCount", "topRated", "releaseDate", "name"]),
    skip:     int        = Query(0,  ge=0),
    limit:    int        = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    filt = await _build_filter(q, genre, platform, theme, db)

    if sort == "topRated":
        IGDB_MIN = 20
        pipeline = [
            {"$match": filt},
            {"$addFields": {
                "_tier":      {"$cond": [{"$gt": ["$reviewTotal", 0]}, 0, 1]},
                "_igdbCount": {"$ifNull": ["$igdbRatingCount", 0]},
            }},
            {"$match": {"$or": [
                {"reviewTotal": {"$gt": 0}},
                {"_igdbCount":  {"$gte": IGDB_MIN}},
            ]}},
            {"$sort": {"_tier": 1, "reviewTotal": -1, "igdbRating": -1}},
            {"$skip": skip},
            {"$limit": limit},
        ]
        count_pipeline = [
            {"$match": filt},
            {"$addFields": {"_igdbCount": {"$ifNull": ["$igdbRatingCount", 0]}}},
            {"$match": {"$or": [
                {"reviewTotal": {"$gt": 0}},
                {"_igdbCount":  {"$gte": IGDB_MIN}},
            ]}},
            {"$count": "total"},
        ]
        # Run games fetch and lookup maps in parallel
        games_coro  = db.games.aggregate(pipeline).to_list(length=limit)
        count_coro  = db.games.aggregate(count_pipeline).to_list(length=1)
        games, count_result, maps = await asyncio.gather(games_coro, count_coro, _build_lookup_maps(db))
        total = count_result[0]["total"] if count_result else 0
        return {"total": total, "skip": skip, "limit": limit, "results": _resolve_games(games, maps)}

    direction = 1 if sort == "name" else -1

    # Run find, count, and lookup maps in parallel
    games_coro = db.games.find(filt).sort(sort, direction).skip(skip).limit(limit).to_list(length=limit)
    count_coro = db.games.count_documents(filt)
    games, total, maps = await asyncio.gather(games_coro, count_coro, _build_lookup_maps(db))

    return {"total": total, "skip": skip, "limit": limit, "results": _resolve_games(games, maps)}


# ---------------------------------------------------------------------------
# Meta endpoints — must be before /{game_id}
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
    games_coro = db.games.find({"_id": {"$in": similar_ids}}).to_list(length=limit)
    games, maps = await asyncio.gather(games_coro, _build_lookup_maps(db))
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

    reviews_coro = db.reviews.find({"gameId": oid}).sort("createdAt", -1).skip(skip).limit(limit).to_list(length=limit)
    count_coro   = db.reviews.count_documents({"gameId": oid})
    reviews, total = await asyncio.gather(reviews_coro, count_coro)

    user_ids = list({r["userId"] for r in reviews if r.get("userId")})
    users    = await db.users.find(
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

    return {"total": total, "skip": skip, "limit": limit, "results": enriched}