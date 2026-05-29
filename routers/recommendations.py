"""
routers/recommendations.py
===========================
GET /api/recommendations
    Returns a ranked list of games for the current user.
    All weights are optional query params (0–10) that override the user's
    saved preferences for this request only.
"""

import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, Literal
from pydantic import BaseModel
from bson import ObjectId
from datetime import datetime, timezone

from core.database import get_db
from core.security import get_current_user
from core.utils   import serialize_doc, serialize_docs
from scoring      import rank_games, DEFAULT_WEIGHTS
from routers.games import _build_lookup_maps, _resolve_game

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


# Minimal projection for candidate game docs — everything scoring and the
# resolver/UI use, nothing more. Skips description, screenshots, similarTo,
# urls, etc., which trim ~kb per doc across hundreds of docs.
CANDIDATE_FIELDS = {
    "name": 1, "coverUrl": 1,
    "genreIds": 1, "platformIds": 1, "developerIds": 1,
    "gameplayAvg": 1, "aestheticsAvg": 1, "contentAvg": 1,
    "polishAvg": 1, "narrativeAvg": 1, "reviewTotal": 1,
    "igdbRating": 1, "igdbRatingCount": 1, "releaseDate": 1,
}

# Even smaller projection for history games — we only ever read genreIds
# and developerIds off these to build the user's affinity context.
HISTORY_FIELDS = {"_id": 1, "genreIds": 1, "developerIds": 1}

PRIMARY_FETCH  = 250   # over-fetch slightly to absorb post-filter exclusions
PRIMARY_POOL   = 200
SECONDARY_POOL = 100


class FeedbackBody(BaseModel):
    gameId: str
    type:   Literal["up", "down"]


def _resolve_names(g: dict, maps: dict) -> tuple[list, list]:
    """Resolve a game doc's genre + developer IDs to name lists."""
    genres = [maps["genres"].get(gid)    for gid in (g.get("genreIds")     or []) if maps["genres"].get(gid)]
    devs   = [maps["companies"].get(cid) for cid in (g.get("developerIds") or [])[:5] if maps["companies"].get(cid)]
    return genres, devs


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

    prefs          = current_user.get("preferences") or {}
    user_genres    = prefs.get("topGenres") or []
    user_platforms = prefs.get("platforms") or []

    # Primary pool match: any genre OR platform overlap. We don't $nin exclude
    # in the query itself — pulling a few extra rows and filtering in Python
    # lets this query run in parallel with the user-history fetches.
    query_filters = []
    if user_genres:
        query_filters.append({"genres":    {"$in": user_genres}})
    if user_platforms:
        query_filters.append({"platforms": {"$in": user_platforms}})
    primary_query = {"$or": query_filters} if query_filters else {}

    # ----- Phase 1: independent queries fire in parallel -----
    user_reviews_raw, feedback_raw, maps, primary_raw_pool = await asyncio.gather(
        db.reviews.find({"userId": current_user["_id"]}).to_list(length=None),
        db.recommendation_feedback.find({"userId": current_user["_id"]}).to_list(length=None),
        _build_lookup_maps(db),
        db.games.find(primary_query, CANDIDATE_FIELDS)
                .sort("reviewTotal", -1)
                .limit(PRIMARY_FETCH)
                .to_list(length=PRIMARY_FETCH),
    )

    # Build exclusion + history sets from the user's review/feedback history
    reviewed_ids  = {r["gameId"] for r in user_reviews_raw if r.get("gameId")}
    disliked_ids  = {f["gameId"] for f in feedback_raw     if f.get("type") == "down" and f.get("gameId")}
    excluded_ids  = reviewed_ids | disliked_ids
    history_ids   = list(reviewed_ids | {f["gameId"] for f in feedback_raw if f.get("gameId")})

    # Trim the primary pool now that we know what to exclude
    primary_games = [g for g in primary_raw_pool if g["_id"] not in excluded_ids][:PRIMARY_POOL]
    primary_ids   = {g["_id"] for g in primary_games}

    # ----- Phase 2: history lookup + secondary pool fire in parallel -----
    secondary_excluded = list(primary_ids | excluded_ids)

    async def _fetch_history():
        if not history_ids:
            return []
        return await db.games.find(
            {"_id": {"$in": history_ids}}, HISTORY_FIELDS,
        ).to_list(length=None)

    history_games, secondary_games = await asyncio.gather(
        _fetch_history(),
        db.games.find({"_id": {"$nin": secondary_excluded}}, CANDIDATE_FIELDS)
                .sort("reviewTotal", -1)
                .limit(SECONDARY_POOL)
                .to_list(length=SECONDARY_POOL),
    )

    history_map = {g["_id"]: g for g in history_games}

    # Enrich reviews with genre/dev names for the affinity model
    user_reviews = []
    for r in user_reviews_raw:
        doc = serialize_doc(r)
        genres, devs = _resolve_names(history_map.get(r.get("gameId"), {}), maps)
        doc["genres"]     = genres
        doc["developers"] = devs
        user_reviews.append(doc)

    # Enrich feedback the same way
    feedback = []
    for f in feedback_raw:
        genres, devs = _resolve_names(history_map.get(f.get("gameId"), {}), maps)
        feedback.append({
            "gameId":     str(f.get("gameId")),
            "type":       f.get("type"),
            "genres":     genres,
            "developers": devs,
        })

    # Fully resolve candidates so scoring sees string arrays AND the response
    # carries genres/platforms/developers — eliminates the frontend N+1.
    all_games = [_resolve_game(g, maps) for g in primary_games + secondary_games]

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