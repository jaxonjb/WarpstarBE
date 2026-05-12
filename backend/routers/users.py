from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId
from datetime import datetime, timezone

from ..core.database import get_db
from ..core.security import get_current_user
from ..core.utils import serialize_doc, serialize_docs
from ..schemas.user import UserPublic, UserUpdate, FollowResponse

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
    await db.users.update_one({"_id": current_user["_id"]}, {"$set": updates})
    updated = await db.users.find_one({"_id": current_user["_id"]})
    return _user_public(updated)


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
