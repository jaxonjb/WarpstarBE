from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from datetime import datetime, timezone

from core.database import get_db
from core.security import get_current_user, get_current_user_optional
from core.utils import serialize_doc, serialize_docs
from schemas.list_comment import ListCreate, ListUpdate, ListAddGame

router = APIRouter(prefix="/api/lists", tags=["lists"])


@router.post("/", status_code=201)
async def create_list(
    body: ListCreate,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    doc = {
        **body.model_dump(),
        "userId":    current_user["_id"],
        "gameIds":   [],
        "createdAt": datetime.now(timezone.utc),
    }
    result  = await db.lists.insert_one(doc)
    created = await db.lists.find_one({"_id": result.inserted_id})
    return serialize_doc(created)


@router.get("/")
async def list_lists(
    user_id: str | None = Query(None),
    skip:    int        = Query(0,  ge=0),
    limit:   int        = Query(20, ge=1, le=100),
    current_user=Depends(get_current_user_optional),
    db=Depends(get_db),
):
    filt: dict = {"isPublic": True}

    if user_id:
        try:
            uid = ObjectId(user_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid user ID.")
        # Own lists: show private too; others: public only
        if current_user and current_user["_id"] == uid:
            filt = {"userId": uid}
        else:
            filt = {"userId": uid, "isPublic": True}

    cursor = db.lists.find(filt).sort("createdAt", -1).skip(skip).limit(limit)
    items  = await cursor.to_list(length=limit)
    total  = await db.lists.count_documents(filt)
    return {"total": total, "skip": skip, "limit": limit, "results": serialize_docs(items)}


@router.get("/{list_id}")
async def get_list(
    list_id: str,
    current_user=Depends(get_current_user_optional),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(list_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid list ID.")

    lst = await db.lists.find_one({"_id": oid})
    if not lst:
        raise HTTPException(status_code=404, detail="List not found.")
    if not lst["isPublic"]:
        if not current_user or current_user["_id"] != lst["userId"]:
            raise HTTPException(status_code=403, detail="This list is private.")
    return serialize_doc(lst)


@router.patch("/{list_id}")
async def update_list(
    list_id: str,
    body: ListUpdate,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(list_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid list ID.")

    lst = await db.lists.find_one({"_id": oid})
    if not lst:
        raise HTTPException(status_code=404, detail="List not found.")
    if lst["userId"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your list.")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    await db.lists.update_one({"_id": oid}, {"$set": updates})
    updated = await db.lists.find_one({"_id": oid})
    return serialize_doc(updated)


@router.delete("/{list_id}", status_code=204)
async def delete_list(
    list_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(list_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid list ID.")

    lst = await db.lists.find_one({"_id": oid})
    if not lst:
        raise HTTPException(status_code=404, detail="List not found.")
    if lst["userId"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your list.")

    await db.lists.delete_one({"_id": oid})
    await db.comments.delete_many({"parentId": oid, "parentType": "list"})


# ---------------------------------------------------------------------------
# Games within a list
# ---------------------------------------------------------------------------

@router.post("/{list_id}/games")
async def add_game_to_list(
    list_id: str,
    body: ListAddGame,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        list_oid = ObjectId(list_id)
        game_oid = ObjectId(body.gameId)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID.")

    lst = await db.lists.find_one({"_id": list_oid})
    if not lst:
        raise HTTPException(status_code=404, detail="List not found.")
    if lst["userId"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your list.")
    if not await db.games.find_one({"_id": game_oid}):
        raise HTTPException(status_code=404, detail="Game not found.")

    await db.lists.update_one({"_id": list_oid}, {"$addToSet": {"gameIds": game_oid}})
    return {"added": True, "gameId": body.gameId}


@router.delete("/{list_id}/games/{game_id}", status_code=204)
async def remove_game_from_list(
    list_id: str,
    game_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        list_oid = ObjectId(list_id)
        game_oid = ObjectId(game_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID.")

    lst = await db.lists.find_one({"_id": list_oid})
    if not lst:
        raise HTTPException(status_code=404, detail="List not found.")
    if lst["userId"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your list.")

    await db.lists.update_one({"_id": list_oid}, {"$pull": {"gameIds": game_oid}})


# ---------------------------------------------------------------------------
# Comments on a list
# ---------------------------------------------------------------------------

@router.get("/{list_id}/comments")
async def get_list_comments(
    list_id: str,
    skip:  int = Query(0,  ge=0),
    limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(list_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid list ID.")
    cursor   = db.comments.find({"parentId": oid, "parentType": "list"}).sort("createdAt", 1).skip(skip).limit(limit)
    comments = await cursor.to_list(length=limit)
    return serialize_docs(comments)


@router.post("/{list_id}/comments", status_code=201)
async def add_list_comment(
    list_id: str,
    content: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(list_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid list ID.")

    if not await db.lists.find_one({"_id": oid}):
        raise HTTPException(status_code=404, detail="List not found.")

    doc = {
        "userId":     current_user["_id"],
        "parentId":   oid,
        "parentType": "list",
        "content":    content,
        "createdAt":  datetime.now(timezone.utc),
    }
    result  = await db.comments.insert_one(doc)
    created = await db.comments.find_one({"_id": result.inserted_id})
    return serialize_doc(created)
