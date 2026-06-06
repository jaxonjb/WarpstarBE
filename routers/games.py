import re
import time
import asyncio
import unicodedata
from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from pydantic import BaseModel

from core.database import get_db
from core.security import get_current_developer
from core.utils import serialize_doc, serialize_docs
import igdb_sync

router = APIRouter(prefix="/api/games", tags=["games"])


# ---------------------------------------------------------------------------
# In-memory lookup map cache
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
# Developer: request a game by IGDB ID
# ---------------------------------------------------------------------------

class GameRequestBody(BaseModel):
    igdb_id: int


@router.post("/request", status_code=201)
async def request_game(
    body: GameRequestBody,
    db=Depends(get_db),
    _dev=Depends(get_current_developer),
):
    """
    Fetch a game from IGDB by its numeric ID and upsert it into the database.
    Applies the same filters as the nightly sync:
      - Must be a base game (no version_parent)
      - Must not be Cancelled (6) or Rumored (7)
      - Must not match a cosmetic-edition name pattern
    Requires developer or admin role.
    """
    try:
        game_doc = await igdb_sync.fetch_and_upsert_by_igdb_id(body.igdb_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"IGDB error: {exc}")

    maps = await _build_lookup_maps(db)
    return _resolve_game(game_doc, maps)


# ---------------------------------------------------------------------------
# List games
# ---------------------------------------------------------------------------

# Warpstar per-factor average fields. Sorting by these only makes sense for
# games that actually have reviews, so they get a reviewTotal > 0 filter.
CATEGORY_SORTS = {"gameplayAvg", "contentAvg", "narrativeAvg", "aestheticsAvg", "polishAvg"}


@router.get("/")
async def list_games(
    q:         str | None = Query(None),
    genre:     str | None = Query(None),
    platform:  str | None = Query(None),
    theme:     str | None = Query(None),
    sort:      str        = Query("reviewTotal", enum=[
        "reviewTotal", "igdbRating", "igdbRatingCount", "topRated", "releaseDate", "name",
        "gameplayAvg", "contentAvg", "narrativeAvg", "aestheticsAvg", "polishAvg",
    ]),
    direction: str        = Query("desc", enum=["asc", "desc"]),
    skip:      int        = Query(0,  ge=0),
    limit:     int        = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    filt    = await _build_filter(q, genre, platform, theme, db)
    dir_val = 1 if direction == "asc" else -1

    if sort == "topRated":
        # Top rated on Warpstar: only games that have at least one review on
        # the site. Rank by the Warpstar average (mean of the five factor
        # averages); ties go to the game with more reviews.
        # Final _id tiebreaker keeps pagination stable across calls.
        top_filter = {**filt, "reviewTotal": {"$gt": 0}}
        pipeline = [
            {"$match": top_filter},
            {"$addFields": {
                "_warpstarAvg": {"$avg": [
                    {"$ifNull": ["$gameplayAvg",   0]},
                    {"$ifNull": ["$aestheticsAvg", 0]},
                    {"$ifNull": ["$contentAvg",    0]},
                    {"$ifNull": ["$polishAvg",     0]},
                    {"$ifNull": ["$narrativeAvg",  0]},
                ]},
            }},
            {"$sort": {"_warpstarAvg": dir_val, "reviewTotal": -1, "_id": 1}},
            {"$skip": skip},
            {"$limit": limit},
        ]
        games_coro = db.games.aggregate(pipeline).to_list(length=limit)
        count_coro = db.games.count_documents(top_filter)
        games, total, maps = await asyncio.gather(games_coro, count_coro, _build_lookup_maps(db))
        return {"total": total, "skip": skip, "limit": limit, "results": _resolve_games(games, maps)}

    if sort in CATEGORY_SORTS:
        # Only rank games that have reviews — unreviewed games have a 0 average
        # and would otherwise flood the results.
        filt = {**filt, "reviewTotal": {"$gt": 0}}
        games_coro = db.games.find(filt).sort([(sort, dir_val), ("_id", 1)]).skip(skip).limit(limit).to_list(length=limit)
        count_coro = db.games.count_documents(filt)
        games, total, maps = await asyncio.gather(games_coro, count_coro, _build_lookup_maps(db))
        return {"total": total, "skip": skip, "limit": limit, "results": _resolve_games(games, maps)}

    games_coro = db.games.find(filt).sort(sort, dir_val).skip(skip).limit(limit).to_list(length=limit)
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


# Fields used for similarity ranking — per-factor Warpstar averages.
_SIMILAR_FACTORS = ["gameplayAvg", "contentAvg", "narrativeAvg", "aestheticsAvg", "polishAvg"]
# Max possible Euclidean distance across 5 factors each on a 0–10 scale.
_SIMILAR_MAX_DIST = (5 * 100) ** 0.5
_SIMILAR_PROJ = {
    "name": 1, "coverUrl": 1, "genreIds": 1, "reviewTotal": 1,
    "gameplayAvg": 1, "contentAvg": 1, "narrativeAvg": 1,
    "aestheticsAvg": 1, "polishAvg": 1,
}


@router.get("/{game_id}/similar")
async def get_similar_games(game_id: str, limit: int = Query(10, ge=1, le=50), db=Depends(get_db)):
    """
    Finds the games most similar to this one by comparing per-category Warpstar
    score averages and genre overlap. Only games that have been reviewed are
    considered, and a game with no Warpstar reviews gets no suggestions at all.
    """
    try:
        oid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")

    game = await db.games.find_one({"_id": oid}, _SIMILAR_PROJ)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found.")

    # No reviews on this game → no basis for comparison, so no similar games.
    if not game.get("reviewTotal", 0):
        return []

    target_scores = [float(game.get(f) or 0) for f in _SIMILAR_FACTORS]
    target_genres = set(game.get("genreIds") or [])

    # Candidate pool: every other game that has at least one review.
    candidates = await db.games.find(
        {"_id": {"$ne": oid}, "reviewTotal": {"$gt": 0}},
        _SIMILAR_PROJ,
    ).to_list(length=None)
    if not candidates:
        return []

    def similarity(c: dict) -> float:
        # Category similarity: 1 minus the normalised Euclidean distance.
        cs   = [float(c.get(f) or 0) for f in _SIMILAR_FACTORS]
        dist = sum((a - b) ** 2 for a, b in zip(target_scores, cs)) ** 0.5
        cat_sim = 1 - (dist / _SIMILAR_MAX_DIST)
        # Genre similarity: Jaccard overlap of genre IDs.
        cg        = set(c.get("genreIds") or [])
        union     = target_genres | cg
        genre_sim = (len(target_genres & cg) / len(union)) if union else 0.0
        return 0.5 * cat_sim + 0.5 * genre_sim

    ranked = sorted(candidates, key=similarity, reverse=True)[:limit]
    maps   = await _build_lookup_maps(db)
    return _resolve_games(ranked, maps)


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

    reviews_coro = db.reviews.find({"gameId": oid}).sort("overallScore", -1).skip(skip).limit(limit).to_list(length=limit)
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