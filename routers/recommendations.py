"""
routers/recommendations.py
===========================
GET /api/recommendations
    Returns a ranked list of games for the current user.
    All weights are optional query params (0–10) that override the user's
    saved preferences for this request only.
"""

from fastapi import APIRouter, Depends, Query
from typing import Optional

from core.database import get_db
from core.security import get_current_user
from core.utils   import serialize_doc, serialize_docs
from scoring      import rank_games, DEFAULT_WEIGHTS

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


@router.get("/")
async def get_recommendations(
    # Per-request weight overrides (0–10). None = use saved prefs / defaults.
    gameplay:      Optional[float] = Query(None, ge=0, le=10),
    aesthetics:    Optional[float] = Query(None, ge=0, le=10),
    content:       Optional[float] = Query(None, ge=0, le=10),
    polish:        Optional[float] = Query(None, ge=0, le=10),
    narrative:     Optional[float] = Query(None, ge=0, le=10),
    genreMatch:    Optional[float] = Query(None, ge=0, le=10),
    platformMatch: Optional[float] = Query(None, ge=0, le=10),
    recency:       Optional[float] = Query(None, ge=0, le=10),
    limit:         int             = Query(20, ge=1, le=100),
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    # Build override dict from any weights explicitly passed
    overrides = {
        k: v for k, v in {
            "gameplay":      gameplay,
            "aesthetics":    aesthetics,
            "content":       content,
            "polish":        polish,
            "narrative":     narrative,
            "genreMatch":    genreMatch,
            "platformMatch": platformMatch,
            "recency":       recency,
        }.items() if v is not None
    }

    # Fetch user's review history (need overallScore + gameId + genres + devs)
    user_reviews_cursor = db.reviews.find({"userId": current_user["_id"]})
    user_reviews_raw    = await user_reviews_cursor.to_list(length=None)

    # Enrich reviews with game genres + developers for familiarity boost
    game_ids  = [r["gameId"] for r in user_reviews_raw if r.get("gameId")]
    games_map: dict = {}
    if game_ids:
        games_cur = db.games.find(
            {"_id": {"$in": game_ids}},
            {"_id": 1, "genres": 1, "developers": 1}
        )
        async for g in games_cur:
            games_map[g["_id"]] = g

    user_reviews = []
    for r in user_reviews_raw:
        doc = serialize_doc(r)
        g   = games_map.get(r.get("gameId"), {})
        doc["genres"]      = g.get("genres", [])
        doc["developers"]  = g.get("developers", [])
        user_reviews.append(doc)

    # Fetch candidate games
    # Strategy: pull games matching user's top genres + platforms first,
    # then fill remaining slots from highest rated games overall.
    prefs          = current_user.get("preferences") or {}
    user_genres    = prefs.get("topGenres")   or []
    user_platforms = prefs.get("platforms")   or []

    reviewed_game_ids = [r.get("gameId") for r in user_reviews_raw if r.get("gameId")]

    # Primary pool: genre/platform match candidates (up to 500)
    query_filters = []
    if user_genres:
        query_filters.append({"genres": {"$in": user_genres}})
    if user_platforms:
        query_filters.append({"platforms": {"$in": user_platforms}})

    primary_query = {"$or": query_filters} if query_filters else {}
    if reviewed_game_ids:
        primary_query["_id"] = {"$nin": reviewed_game_ids}

    # No projection — return all fields, identical to how games.py works
    primary_cursor = db.games.find(primary_query).sort("reviewTotal", -1).limit(500)
    primary_games  = await primary_cursor.to_list(length=500)

    # Secondary pool: top rated games not already in primary (fill to 800 total)
    primary_ids    = {g["_id"] for g in primary_games}
    exclude_ids    = primary_ids | set(reviewed_game_ids)
    secondary_cursor = db.games.find(
        {"_id": {"$nin": list(exclude_ids)}}
    ).sort("reviewTotal", -1).limit(300)
    secondary_games = await secondary_cursor.to_list(length=300)

    # Serialize exactly like games.py does — the name arrays (genres, platforms,
    # developers) are already stored as strings on the game documents from import
    all_games = [serialize_doc(g) for g in primary_games + secondary_games]

    user_doc = serialize_doc(current_user)

    ranked = rank_games(
        games        = all_games,
        user         = user_doc,
        user_reviews = user_reviews,
        weights      = overrides or None,
        limit        = limit,
    )

    return {
        "total":   len(ranked),
        "weights": {**DEFAULT_WEIGHTS, **(prefs.get("weights") or {}), **overrides},
        "results": ranked,
    }


@router.patch("/weights")
async def save_weights(
    gameplay:      Optional[float] = Query(None, ge=0, le=10),
    aesthetics:    Optional[float] = Query(None, ge=0, le=10),
    content:       Optional[float] = Query(None, ge=0, le=10),
    polish:        Optional[float] = Query(None, ge=0, le=10),
    narrative:     Optional[float] = Query(None, ge=0, le=10),
    genreMatch:    Optional[float] = Query(None, ge=0, le=10),
    platformMatch: Optional[float] = Query(None, ge=0, le=10),
    recency:       Optional[float] = Query(None, ge=0, le=10),
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Persist the user's weight preferences to their profile."""
    updates = {
        k: v for k, v in {
            "gameplay":      gameplay,
            "aesthetics":    aesthetics,
            "content":       content,
            "polish":        polish,
            "narrative":     narrative,
            "genreMatch":    genreMatch,
            "platformMatch": platformMatch,
            "recency":       recency,
        }.items() if v is not None
    }
    if not updates:
        return {"message": "No weights provided."}

    set_fields = {f"preferences.weights.{k}": v for k, v in updates.items()}
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$set": set_fields}
    )
    return {"message": "Weights saved.", "updated": updates}