"""
routers/notifications.py
========================
GET  /api/notifications/         — list notifications + unread count
POST /api/notifications/mark-read — mark all unread as read

Notification documents are written from inside reviews.py when:
  - someone likes a review owned by another user  (type: "review_like")
  - someone comments on a review owned by another user (type: "review_comment")

Document shape (in db.notifications):
    {
        userId:    <ObjectId>,        # recipient
        actorId:   <ObjectId>,        # who did the action
        type:      "review_like" | "review_comment",
        reviewId:  <ObjectId>,
        commentId: <ObjectId|None>,   # only for review_comment
        read:      bool,
        createdAt: datetime,
    }
"""

from fastapi import APIRouter, Depends, Query

from core.database import get_db
from core.security import get_current_user
from core.utils   import serialize_doc

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


async def _enrich(notifs: list, db) -> list:
    """Attach actor, review title, and game cover info to each notification."""
    if not notifs:
        return []

    actor_ids  = list({n["actorId"]  for n in notifs if n.get("actorId")})
    review_ids = list({n["reviewId"] for n in notifs if n.get("reviewId")})

    actors_map: dict = {}
    if actor_ids:
        async for u in db.users.find(
            {"_id": {"$in": actor_ids}},
            {"_id": 1, "username": 1, "preferences.displayName": 1, "preferences.profilePicture": 1},
        ):
            actors_map[u["_id"]] = u

    reviews_map: dict = {}
    if review_ids:
        async for r in db.reviews.find(
            {"_id": {"$in": review_ids}},
            {"_id": 1, "title": 1, "gameId": 1},
        ):
            reviews_map[r["_id"]] = r

    game_ids = list({
        r.get("gameId") for r in reviews_map.values() if r.get("gameId")
    })
    games_map: dict = {}
    if game_ids:
        async for g in db.games.find(
            {"_id": {"$in": game_ids}},
            {"_id": 1, "name": 1, "coverUrl": 1},
        ):
            games_map[g["_id"]] = g

    enriched = []
    for n in notifs:
        doc   = serialize_doc(n)
        actor = actors_map.get(n.get("actorId"))
        if actor:
            prefs = actor.get("preferences") or {}
            doc["actor"] = {
                "username":       actor.get("username"),
                "displayName":    prefs.get("displayName"),
                "profilePicture": prefs.get("profilePicture"),
            }
        review = reviews_map.get(n.get("reviewId"))
        if review:
            doc["reviewTitle"] = review.get("title")
            game = games_map.get(review.get("gameId"))
            if game:
                doc["gameId"]       = str(review.get("gameId"))
                doc["gameName"]     = game.get("name")
                doc["gameCoverUrl"] = game.get("coverUrl")
        enriched.append(doc)
    return enriched


@router.get("/")
async def list_notifications(
    skip:  int = Query(0,  ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Returns the current user's notifications, newest first, plus an unread count."""
    filt = {"userId": current_user["_id"]}
    cursor = (
        db.notifications.find(filt)
        .sort("createdAt", -1)
        .skip(skip).limit(limit)
    )
    raw    = await cursor.to_list(length=limit)
    total  = await db.notifications.count_documents(filt)
    unread = await db.notifications.count_documents({**filt, "read": False})

    return {
        "total":   total,
        "unread":  unread,
        "skip":    skip,
        "limit":   limit,
        "results": await _enrich(raw, db),
    }


@router.post("/mark-read")
async def mark_all_read(
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Mark every unread notification for the current user as read."""
    result = await db.notifications.update_many(
        {"userId": current_user["_id"], "read": False},
        {"$set": {"read": True}},
    )
    return {"ok": True, "modified": result.modified_count}
