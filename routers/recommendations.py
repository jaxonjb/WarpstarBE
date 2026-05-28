"""
routers/recommendations.py
===========================
GET /api/recommendations
    Returns a ranked list of games for the current user.
    All weights are optional query params (0–10) that override the user's
    saved preferences for this request only.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, Literal
from pydantic import BaseModel
from bson import ObjectId
from datetime import datetime, timezone

from core.database import get_db
from core.security import get_current_user
from core.utils   import serialize_doc, serialize_docs
from scoring      import rank_games, DEFAULT_WEIGHTS

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


class FeedbackBody(BaseModel):
    gameId: str
    type:   Literal["up", "down"]


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

    # Fetch user's thumbs feedback on past recommendations
    feedback_raw = await db.recommendation_feedback.find(
        {"userId": current_user["_id"]}
    ).to_list(length=None)

    # Combined game IDs we need genre/dev info for (reviews + feedback)
    game_ids = list({
        *(r["gameId"] for r in user_reviews_raw if r.get("gameId")),
        *(f["gameId"] for f in feedback_raw     if f.get("gameId")),
    })
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

    feedback = []
    for f in feedback_raw:
        g = games_map.get(f.get("gameId"), {})
        feedback.append({
            "gameId":     str(f.get("gameId")),
            "type":       f.get("type"),
            "genres":     g.get("genres", []),
            "developers": g.get("developers", []),
        })

    # Fetch candidate games
    # Strategy: pull games matching user's top genres + platforms first,
    # then fill remaining slots from highest rated games overall.
    prefs          = current_user.get("preferences") or {}
    user_genres    = prefs.get("topGenres")   or []
    user_platforms = prefs.get("platforms")   or []

    reviewed_game_ids = [r.get("gameId") for r in user_reviews_raw if r.get("gameId")]
    disliked_game_ids = [f.get("gameId") for f in feedback_raw     if f.get("type") == "down" and f.get("gameId")]

    # Anything we never want to surface in candidates (reviewed or thumbs-downed)
    excluded_ids = list({*reviewed_game_ids, *disliked_game_ids})

    # Primary pool: genre/platform match candidates (up to 500)
    query_filters = []
    if user_genres:
        query_filters.append({"genres": {"$in": user_genres}})
    if user_platforms:
        query_filters.append({"platforms": {"$in": user_platforms}})

    primary_query = {"$or": query_filters} if query_filters else {}
    if excluded_ids:
        primary_query["_id"] = {"$nin": excluded_ids}

    # No projection — return all fields, identical to how games.py works
    primary_cursor = db.games.find(primary_query).sort("reviewTotal", -1).limit(500)
    primary_games  = await primary_cursor.to_list(length=500)

    # Secondary pool: top rated games not already in primary (fill to 800 total)
    primary_ids    = {g["_id"] for g in primary_games}
    exclude_ids    = primary_ids | set(excluded_ids)
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
        feedback     = feedback,
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


# ---------------------------------------------------------------------------
# Thumbs up / down feedback on recommended games
# ---------------------------------------------------------------------------

@router.get("/feedback")
async def get_feedback(
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Returns { gameId: "up" | "down" } for every game the user has rated."""
    docs = await db.recommendation_feedback.find(
        {"userId": current_user["_id"]}
    ).to_list(length=None)
    return {str(d["gameId"]): d["type"] for d in docs}


@router.post("/feedback")
async def set_feedback(
    body: FeedbackBody,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Upsert thumbs up/down for a single game."""
    try:
        gid = ObjectId(body.gameId)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")

    now = datetime.now(timezone.utc)
    await db.recommendation_feedback.update_one(
        {"userId": current_user["_id"], "gameId": gid},
        {
            "$set":         {"type": body.type, "updatedAt": now},
            "$setOnInsert": {
                "userId":    current_user["_id"],
                "gameId":    gid,
                "createdAt": now,
            },
        },
        upsert=True,
    )
    return {"ok": True, "gameId": body.gameId, "type": body.type}


@router.delete("/feedback/{game_id}")
async def clear_feedback(
    game_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Remove thumbs feedback for a single game."""
    try:
        gid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")
    await db.recommendation_feedback.delete_one(
        {"userId": current_user["_id"], "gameId": gid}
    )
    return {"ok": True, "gameId": game_id}