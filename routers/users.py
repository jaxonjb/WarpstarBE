import re
from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from datetime import datetime, timezone, timedelta

from core.database import get_db
from core.security import get_current_user
from core.utils import serialize_doc, serialize_docs
from schemas.user import UserPublic, UserUpdate, FollowResponse

router = APIRouter(prefix="/api/users", tags=["users"])


def _user_public(user: dict) -> dict:
    """Strip private fields before returning a user doc."""
    user = serialize_doc(user)
    user.pop("passwordHash", None)
    user.pop("email", None)
    return user


@router.get("/me")
async def get_me(current_user=Depends(get_current_user)):
    return _user_public(current_user)


@router.patch("/me")
async def update_me(body: UserUpdate, current_user=Depends(get_current_user), db=Depends(get_db)):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    # If username is being changed, enforce cooldown + uniqueness
    if "username" in updates:
        new_username = updates["username"]

        # Check uniqueness
        existing = await db.users.find_one({"username": new_username, "_id": {"$ne": current_user["_id"]}})
        if existing:
            raise HTTPException(status_code=409, detail="Username already taken.")

        # Check 30-day cooldown
        last_change = current_user.get("usernameChangedAt")
        if last_change:
            cooldown_end = last_change + timedelta(days=30)
            if datetime.now(timezone.utc) < cooldown_end.replace(tzinfo=timezone.utc) if last_change.tzinfo is None else cooldown_end:
                days_left = (cooldown_end.replace(tzinfo=timezone.utc) if last_change.tzinfo is None else cooldown_end - datetime.now(timezone.utc)).days + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"You can change your username again in {days_left} day{'s' if days_left != 1 else ''}."
                )

        # Record the change time
        updates["usernameChangedAt"] = datetime.now(timezone.utc)

    await db.users.update_one({"_id": current_user["_id"]}, {"$set": updates})
    updated = await db.users.find_one({"_id": current_user["_id"]})
    return _user_public(updated)


@router.get("/search")
async def search_users(
    q:     str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    db=Depends(get_db),
):
    """Search users by username or display name (partial, case-insensitive)."""
    pattern = re.escape(q.strip())
    cursor  = db.users.find({
        "$or": [
            {"username":                  {"$regex": pattern, "$options": "i"}},
            {"preferences.displayName":   {"$regex": pattern, "$options": "i"}},
        ]
    }).limit(limit)
    users = await cursor.to_list(length=limit)
    return [_user_public(u) for u in users]


@router.get("/{username}")
async def get_user(username: str, db=Depends(get_db)):
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return _user_public(user)


# ---------------------------------------------------------------------------
# Follow / unfollow
# ---------------------------------------------------------------------------

@router.post("/{username}/follow", response_model=FollowResponse)
async def follow_user(username: str, current_user=Depends(get_current_user), db=Depends(get_db)):
    target = await db.users.find_one({"username": username})
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target["_id"] == current_user["_id"]:
        raise HTTPException(status_code=400, detail="You cannot follow yourself.")

    target_id  = target["_id"]
    current_id = current_user["_id"]

    already_following = target_id in (current_user.get("following") or [])

    if already_following:
        # Unfollow
        await db.users.update_one({"_id": current_id}, {"$pull": {"following": target_id}})
        await db.users.update_one({"_id": target_id},  {"$pull": {"followers": current_id}})
        following = False
    else:
        # Follow + log activity
        await db.users.update_one({"_id": current_id}, {"$addToSet": {"following": target_id}})
        await db.users.update_one({"_id": target_id},  {"$addToSet": {"followers": current_id}})
        await db.activity.insert_one({
            "userId":     current_id,
            "type":       "follow",
            "targetId":   target_id,
            "targetType": "user",
            "createdAt":  datetime.now(timezone.utc),
        })
        following = True

    updated_target = await db.users.find_one({"_id": target_id})
    return FollowResponse(following=following, follower_count=len(updated_target.get("followers", [])))


# ---------------------------------------------------------------------------
# Favorite games
# ---------------------------------------------------------------------------

@router.post("/me/favorites/{game_id}")
async def toggle_favorite(game_id: str, current_user=Depends(get_current_user), db=Depends(get_db)):
    try:
        oid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid game ID.")

    if not await db.games.find_one({"_id": oid}):
        raise HTTPException(status_code=404, detail="Game not found.")

    favorites = current_user.get("favoriteGames") or []
    if oid in favorites:
        await db.users.update_one({"_id": current_user["_id"]}, {"$pull": {"favoriteGames": oid}})
        return {"favorited": False}
    else:
        await db.users.update_one({"_id": current_user["_id"]}, {"$addToSet": {"favoriteGames": oid}})
        return {"favorited": True}


@router.get("/me/favorites")
async def get_favorites(current_user=Depends(get_current_user), db=Depends(get_db)):
    ids = current_user.get("favoriteGames") or []
    games = await db.games.find({"_id": {"$in": ids}}).to_list(length=None)
    return serialize_docs(games)

# ---------------------------------------------------------------------------
# User reviews — public endpoint for profile page
# ---------------------------------------------------------------------------

@router.get("/{user_id}/reviews")
async def get_user_reviews(
    user_id: str,
    skip:  int = 0,
    limit: int = 20,
    db=Depends(get_db),
):
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user ID.")

    cursor  = db.reviews.find({"userId": oid}).sort("createdAt", -1).skip(skip).limit(limit)
    reviews = await cursor.to_list(length=limit)
    total   = await db.reviews.count_documents({"userId": oid})

    # Look up the user once to attach username/avatar to every review
    profile = await db.users.find_one({"_id": oid}, {"username": 1, "preferences": 1})
    enriched = []
    for r in reviews:
        doc = serialize_doc(r)
        doc["username"] = profile.get("username", "unknown") if profile else "unknown"
        doc["avatar"]   = profile.get("preferences", {}).get("profilePicture") if profile else None
        enriched.append(doc)

    return {"total": total, "skip": skip, "limit": limit, "results": enriched}