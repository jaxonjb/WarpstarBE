from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from datetime import datetime, timezone
from pydantic import BaseModel

from core.database import get_db
from core.security import get_current_user
from core.utils import serialize_doc, serialize_docs
from schemas.review import ReviewCreate, ReviewUpdate

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


def _calc_overall(data: dict) -> float:
    fields = ["gameplay", "content", "narrative", "aesthetics", "polish"]
    scores = [data[f] for f in fields if f in data]
    return round(sum(scores) / len(scores), 2) if scores else 0.0


async def _recalculate_game_scores(game_id: ObjectId, db) -> None:
    """Recompute all average score fields on the parent game document."""
    pipeline = [
        {"$match": {"gameId": game_id}},
        {"$group": {
            "_id":          "$gameId",
            "gameplayAvg":  {"$avg": "$gameplay"},
            "contentAvg":   {"$avg": "$content"},
            "narrativeAvg": {"$avg": "$narrative"},
            "aestheticsAvg":{"$avg": "$aesthetics"},
            "polishAvg":    {"$avg": "$polish"},
            "reviewTotal":  {"$sum": 1},
        }},
    ]
    result = await db.reviews.aggregate(pipeline).to_list(length=1)
    if result:
        r = result[0]
        await db.games.update_one(
            {"_id": game_id},
            {"$set": {
                "gameplayAvg":   round(r["gameplayAvg"],   2),
                "contentAvg":    round(r["contentAvg"],    2),
                "narrativeAvg":  round(r["narrativeAvg"],  2),
                "aestheticsAvg": round(r["aestheticsAvg"], 2),
                "polishAvg":     round(r["polishAvg"],     2),
                "reviewTotal":   r["reviewTotal"],
            }},
        )
    else:
        # No reviews left — reset scores
        await db.games.update_one(
            {"_id": game_id},
            {"$set": {
                "gameplayAvg": 0, "contentAvg": 0, "narrativeAvg": 0,
                "aestheticsAvg": 0, "polishAvg": 0, "reviewTotal": 0,
            }},
        )


@router.post("/{game_id}", status_code=201)
async def create_review(
    game_id: str,
    body: ReviewCreate,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        game_oid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")

    if not await db.games.find_one({"_id": game_oid}):
        raise HTTPException(status_code=404, detail="Game not found.")

    if await db.reviews.find_one({"gameId": game_oid, "userId": current_user["_id"]}):
        raise HTTPException(status_code=409, detail="You have already reviewed this game.")

    data = body.model_dump()
    doc  = {
        **data,
        "gameId":      game_oid,
        "userId":      current_user["_id"],
        "overallScore": _calc_overall(data),
        "likes":    0,
        "dislikes": 0,
        "commentsCount": 0,
        "createdAt":    datetime.now(timezone.utc),
    }

    result = await db.reviews.insert_one(doc)
    await _recalculate_game_scores(game_oid, db)

    await db.activity.insert_one({
        "userId":     current_user["_id"],
        "type":       "review",
        "targetId":   result.inserted_id,
        "targetType": "review",
        "createdAt":  datetime.now(timezone.utc),
    })

    created = await db.reviews.find_one({"_id": result.inserted_id})
    return serialize_doc(created)


@router.get("/recent")
async def get_recent_reviews(
    limit: int = Query(10, ge=1, le=50),
    db=Depends(get_db),
):
    """
    Returns the most recently posted reviews across the whole site,
    newest first. Public (no auth) so it can power the homepage.
    Each review is enriched with game + reviewer info to avoid N+1
    round-trips on the frontend.
    """
    cursor = db.reviews.find().sort("createdAt", -1).limit(limit)
    reviews_raw = await cursor.to_list(length=limit)
    if not reviews_raw:
        return {"results": []}

    game_ids = list({r["gameId"] for r in reviews_raw if r.get("gameId")})
    user_ids = list({r["userId"] for r in reviews_raw if r.get("userId")})

    games_map: dict = {}
    if game_ids:
        async for g in db.games.find(
            {"_id": {"$in": game_ids}},
            {"_id": 1, "name": 1, "coverUrl": 1},
        ):
            games_map[g["_id"]] = g

    users_map: dict = {}
    if user_ids:
        async for u in db.users.find(
            {"_id": {"$in": user_ids}},
            {"_id": 1, "username": 1, "preferences": 1},
        ):
            users_map[u["_id"]] = u

    enriched = []
    for r in reviews_raw:
        doc = serialize_doc(r)
        g   = games_map.get(r.get("gameId"))
        if g:
            doc["gameName"]     = g.get("name")
            doc["gameCoverUrl"] = g.get("coverUrl")
        u = users_map.get(r.get("userId"))
        if u:
            prefs = u.get("preferences") or {}
            doc["reviewer"] = {
                "username":       u.get("username"),
                "displayName":    prefs.get("displayName"),
                "profilePicture": prefs.get("profilePicture"),
            }
        enriched.append(doc)

    return {"results": enriched}


@router.get("/{review_id}")
async def get_review(review_id: str, db=Depends(get_db)):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")
    review = await db.reviews.find_one({"_id": oid})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")
    return serialize_doc(review)


@router.patch("/{review_id}")
async def update_review(
    review_id: str,
    body: ReviewUpdate,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")

    review = await db.reviews.find_one({"_id": oid})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")
    if review["userId"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your review.")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    # Recalculate overallScore with merged values
    merged = {**review, **updates}
    updates["overallScore"] = _calc_overall(merged)

    await db.reviews.update_one({"_id": oid}, {"$set": updates})
    await _recalculate_game_scores(review["gameId"], db)

    updated = await db.reviews.find_one({"_id": oid})
    return serialize_doc(updated)


@router.delete("/{review_id}", status_code=204)
async def delete_review(
    review_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")

    review = await db.reviews.find_one({"_id": oid})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")
    if review["userId"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your review.")

    game_id = review["gameId"]
    await db.reviews.delete_one({"_id": oid})
    await db.comments.delete_many({"parentId": oid, "parentType": "review"})
    await _recalculate_game_scores(game_id, db)


@router.post("/{review_id}/like")
async def toggle_like(
    review_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")

    review = await db.reviews.find_one({"_id": oid})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")

    user_liked    = oid in (current_user.get("likedReviews")    or [])
    user_disliked = oid in (current_user.get("dislikedReviews") or [])

    if user_liked:
        # Un-like
        await db.reviews.update_one({"_id": oid}, {"$inc": {"likes": -1}})
        await db.users.update_one({"_id": current_user["_id"]}, {"$pull": {"likedReviews": oid}})
        # Pull the matching notification so the badge doesn't lie
        await db.notifications.delete_one({
            "userId":   review["userId"],
            "actorId":  current_user["_id"],
            "type":     "review_like",
            "reviewId": oid,
        })
        return {"liked": False, "disliked": False}
    else:
        updates = {"$inc": {"likes": 1}, "$set": {}}
        user_update = {"$addToSet": {"likedReviews": oid}}
        # If they had disliked, remove that first
        if user_disliked:
            updates["$inc"]["dislikes"] = -1
            user_update["$pull"] = {"dislikedReviews": oid}
        await db.reviews.update_one({"_id": oid}, updates)
        await db.users.update_one({"_id": current_user["_id"]}, user_update)
        # Notify the review author (skip if liking your own review)
        if review["userId"] != current_user["_id"]:
            await db.notifications.insert_one({
                "userId":    review["userId"],
                "actorId":   current_user["_id"],
                "type":      "review_like",
                "reviewId":  oid,
                "read":      False,
                "createdAt": datetime.now(timezone.utc),
            })
        return {"liked": True, "disliked": False}


@router.post("/{review_id}/dislike")
async def toggle_dislike(
    review_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")

    review = await db.reviews.find_one({"_id": oid})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")

    user_liked    = oid in (current_user.get("likedReviews")    or [])
    user_disliked = oid in (current_user.get("dislikedReviews") or [])

    if user_disliked:
        # Un-dislike
        await db.reviews.update_one({"_id": oid}, {"$inc": {"dislikes": -1}})
        await db.users.update_one({"_id": current_user["_id"]}, {"$pull": {"dislikedReviews": oid}})
        return {"liked": False, "disliked": False}
    else:
        updates = {"$inc": {"dislikes": 1}}
        user_update = {"$addToSet": {"dislikedReviews": oid}}
        # If they had liked, remove that first
        if user_liked:
            updates["$inc"]["likes"] = -1
            user_update["$pull"] = {"likedReviews": oid}
        await db.reviews.update_one({"_id": oid}, updates)
        await db.users.update_one({"_id": current_user["_id"]}, user_update)
        return {"liked": False, "disliked": True}


# ---------------------------------------------------------------------------
# User reviews — enriched with game name + cover for profile pages
# ---------------------------------------------------------------------------

@router.get("/user/{user_id}")
async def get_user_reviews(
    user_id: str,
    skip:  int = Query(0,  ge=0),
    limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    """Returns a user's reviews enriched with game name and cover URL."""
    try:
        uid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user ID.")

    cursor  = db.reviews.find({"userId": uid}).sort("createdAt", -1).skip(skip).limit(limit)
    reviews = await cursor.to_list(length=limit)
    total   = await db.reviews.count_documents({"userId": uid})

    # Fetch all referenced games in one query
    game_ids = list({r["gameId"] for r in reviews if r.get("gameId")})
    games    = await db.games.find(
        {"_id": {"$in": game_ids}},
        {"_id": 1, "name": 1, "coverUrl": 1}
    ).to_list(length=None)
    game_map = {str(g["_id"]): g for g in games}

    enriched = []
    for r in reviews:
        doc     = serialize_doc(r)
        game_id = str(r.get("gameId", ""))
        game    = game_map.get(game_id, {})
        doc["gameId"]       = game_id
        doc["gameName"]     = game.get("name")
        doc["gameCoverUrl"] = game.get("coverUrl")
        enriched.append(doc)

    return {"total": total, "skip": skip, "limit": limit, "results": enriched}


# ---------------------------------------------------------------------------
# Comments on a review
# ---------------------------------------------------------------------------

class CommentCreate(BaseModel):
    content: str


async def _enrich_comments(comments: list, db) -> list:
    """Attach username + avatar to each comment."""
    user_ids = list({c["userId"] for c in comments if c.get("userId")})
    users    = await db.users.find(
        {"_id": {"$in": user_ids}},
        {"_id": 1, "username": 1, "preferences.profilePicture": 1}
    ).to_list(length=None)
    user_map = {str(u["_id"]): u for u in users}

    enriched = []
    for c in comments:
        doc      = serialize_doc(c)
        uid      = str(c.get("userId", ""))
        u        = user_map.get(uid, {})
        doc["username"] = u.get("username", "unknown")
        doc["avatar"]   = (u.get("preferences") or {}).get("profilePicture")
        enriched.append(doc)
    return enriched


@router.get("/{review_id}/comments")
async def get_review_comments(
    review_id: str,
    skip:  int = Query(0,  ge=0),
    limit: int = Query(50, ge=1, le=100),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")
    cursor   = db.comments.find({"parentId": oid, "parentType": "review"}).sort("createdAt", 1).skip(skip).limit(limit)
    comments = await cursor.to_list(length=limit)
    total    = await db.comments.count_documents({"parentId": oid, "parentType": "review"})
    enriched = await _enrich_comments(comments, db)
    return {"total": total, "results": enriched}


@router.post("/{review_id}/comments", status_code=201)
async def add_review_comment(
    review_id: str,
    body: CommentCreate,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")

    review = await db.reviews.find_one({"_id": oid})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")

    doc = {
        "userId":     current_user["_id"],
        "parentId":   oid,
        "parentType": "review",
        "content":    body.content,
        "createdAt":  datetime.now(timezone.utc),
    }
    result   = await db.comments.insert_one(doc)
    await db.reviews.update_one({"_id": oid}, {"$inc": {"commentsCount": 1}})

    # Notify the review author (skip if commenting on your own review)
    if review["userId"] != current_user["_id"]:
        await db.notifications.insert_one({
            "userId":    review["userId"],
            "actorId":   current_user["_id"],
            "type":      "review_comment",
            "reviewId":  oid,
            "commentId": result.inserted_id,
            "read":      False,
            "createdAt": datetime.now(timezone.utc),
        })

    created  = await db.comments.find_one({"_id": result.inserted_id})
    enriched = await _enrich_comments([created], db)
    return enriched[0]


@router.delete("/{review_id}/comments/{comment_id}", status_code=204)
async def delete_review_comment(
    review_id: str,
    comment_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        r_oid = ObjectId(review_id)
        c_oid = ObjectId(comment_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID.")

    comment = await db.comments.find_one({"_id": c_oid, "parentId": r_oid})
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found.")
    if comment["userId"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your comment.")

    await db.comments.delete_one({"_id": c_oid})
    await db.reviews.update_one({"_id": r_oid}, {"$inc": {"commentsCount": -1}})
    # Clean up the related notification if it still exists
    await db.notifications.delete_one({"commentId": c_oid, "type": "review_comment"})