from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from datetime import datetime, timezone

from ..core.database import get_db
from ..core.security import get_current_user
from ..core.utils import serialize_doc, serialize_docs
from ..schemas.review import ReviewCreate, ReviewUpdate

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
        "likes":        0,
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

    liked_key = f"likedReviews"
    user_liked = oid in (current_user.get(liked_key) or [])

    if user_liked:
        await db.reviews.update_one({"_id": oid}, {"$inc": {"likes": -1}})
        await db.users.update_one({"_id": current_user["_id"]}, {"$pull": {liked_key: oid}})
        return {"liked": False}
    else:
        await db.reviews.update_one({"_id": oid}, {"$inc": {"likes": 1}})
        await db.users.update_one({"_id": current_user["_id"]}, {"$addToSet": {liked_key: oid}})
        return {"liked": True}


# ---------------------------------------------------------------------------
# Comments on a review
# ---------------------------------------------------------------------------

@router.get("/{review_id}/comments")
async def get_review_comments(
    review_id: str,
    skip:  int = Query(0,  ge=0),
    limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")
    cursor   = db.comments.find({"parentId": oid, "parentType": "review"}).sort("createdAt", 1).skip(skip).limit(limit)
    comments = await cursor.to_list(length=limit)
    return serialize_docs(comments)


@router.post("/{review_id}/comments", status_code=201)
async def add_review_comment(
    review_id: str,
    content: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(review_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid review ID.")

    if not await db.reviews.find_one({"_id": oid}):
        raise HTTPException(status_code=404, detail="Review not found.")

    doc = {
        "userId":     current_user["_id"],
        "parentId":   oid,
        "parentType": "review",
        "content":    content,
        "createdAt":  datetime.now(timezone.utc),
    }
    result = await db.comments.insert_one(doc)
    await db.reviews.update_one({"_id": oid}, {"$inc": {"commentsCount": 1}})
    created = await db.comments.find_one({"_id": result.inserted_id})
    return serialize_doc(created)
