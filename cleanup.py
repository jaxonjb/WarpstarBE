"""
cleanup.py
One-time (and recurring) database cleanup jobs for WarpstarBE.

Functions
---------
remove_wrong_game_types()
    Pages through all games in MongoDB, checks each one's game_type via IGDB,
    and cascade-deletes anything not in ALLOWED_GAME_TYPES.
    Same IGDB rate-limiting as the bulk sync (0.3 s between batches).

remove_no_developer_games()
    Deletes all games in MongoDB whose developerIds array is empty or absent.
    Pure MongoDB operation — no IGDB calls needed.

Both functions perform a full cascade delete (reviews, comments, list entries,
favorites, activity, similarTo references) matching filter_editions.py / igdb_status_cleanup.py.
Both return a summary dict.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import igdb_client
from core.database import get_db
from igdb_sync import ALLOWED_GAME_TYPES

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Shared cascade delete
# ---------------------------------------------------------------------------

async def _cascade_delete(db, mongo_ids: list) -> int:
    """Delete games and all related documents. Returns the number of games deleted."""
    if not mongo_ids:
        return 0

    review_cursor = db.reviews.find({"gameId": {"$in": mongo_ids}}, {"_id": 1})
    review_ids = [r["_id"] async for r in review_cursor]

    if review_ids:
        await db.comments.delete_many({"parentId": {"$in": review_ids}, "parentType": "review"})

    await asyncio.gather(
        db.reviews.delete_many({"gameId": {"$in": mongo_ids}}),
        db.lists.update_many({"gameIds":       {"$in": mongo_ids}}, {"$pull": {"gameIds":       {"$in": mongo_ids}}}),
        db.users.update_many({"favoriteGames": {"$in": mongo_ids}}, {"$pull": {"favoriteGames": {"$in": mongo_ids}}}),
        db.games.update_many({"similarTo":     {"$in": mongo_ids}}, {"$pull": {"similarTo":     {"$in": mongo_ids}}}),
        db.activity.delete_many({"targetId": {"$in": mongo_ids}, "targetType": "game"}),
    )

    result = await db.games.delete_many({"_id": {"$in": mongo_ids}})
    return result.deleted_count


# ---------------------------------------------------------------------------
# Job 1: Remove wrong game types
# ---------------------------------------------------------------------------

async def _fetch_game_types(igdb_ids: list[int]) -> dict[int, int | None]:
    """
    Returns {igdb_id: game_type} for the given IDs.
    game_type is None if the field is absent (IGDB omits it for some older games).
    """
    id_str = ",".join(str(i) for i in igdb_ids)
    body   = f"fields id, game_type; where id = ({id_str}); limit {len(igdb_ids)};"
    try:
        results = await igdb_client.query("games", body)
        return {r["id"]: r.get("game_type") for r in results}
    except Exception as exc:
        logger.warning("IGDB game_type fetch failed: %s", exc)
        return {}


async def remove_wrong_game_types() -> dict:
    """
    Page through all games in MongoDB and remove any whose IGDB game_type is
    not in ALLOWED_GAME_TYPES {0=Main Game, 4=Bundle, 8=Remake, 9=Remaster, 10=Expanded}.

    Games whose igdb_id is missing or unparseable are left alone.
    Returns a summary dict.
    """
    logger.info("cleanup: remove_wrong_game_types started")
    start      = time.time()
    db         = get_db()
    offset     = 0
    deleted    = 0
    errors     = 0

    total = await db.games.count_documents({"igdb_id": {"$exists": True}})
    logger.info("  %d games to scan", total)

    while offset < total:
        page = await db.games.find(
            {"igdb_id": {"$exists": True}},
            {"_id": 1, "igdb_id": 1, "name": 1},
        ).sort("_id", 1).skip(offset).limit(BATCH_SIZE).to_list(length=BATCH_SIZE)

        if not page:
            break

        # Convert stored igdb_id strings to ints for the IGDB query
        parseable = []
        for doc in page:
            try:
                parseable.append((doc["_id"], int(doc["igdb_id"]), doc.get("name", "")))
            except (ValueError, TypeError):
                pass

        igdb_ids    = [t[1] for t in parseable]
        by_igdb_int = {t[1]: (t[0], t[2]) for t in parseable}

        type_map = await _fetch_game_types(igdb_ids)

        to_delete = []
        for igdb_id, (mongo_id, name) in by_igdb_int.items():
            game_type = type_map.get(igdb_id)
            if game_type not in ALLOWED_GAME_TYPES:
                logger.debug("  removing '%s' (igdb_id=%s, game_type=%s)", name, igdb_id, game_type)
                to_delete.append(mongo_id)

        if to_delete:
            try:
                n = await _cascade_delete(db, to_delete)
                deleted += n
                logger.info("  offset %d — deleted %d games (running total: %d)", offset, n, deleted)
            except Exception as exc:
                logger.error("  cascade delete failed at offset %d: %s", offset, exc)
                errors += 1

        offset += len(page)
        await asyncio.sleep(0.3)  # IGDB rate limit

    elapsed = round(time.time() - start, 1)
    summary = {
        "job":         "remove_wrong_game_types",
        "status":      "ok",
        "scanned":     offset,
        "deleted":     deleted,
        "errors":      errors,
        "elapsed_s":   elapsed,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    logger.info("cleanup: remove_wrong_game_types finished: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Job 2: Remove games with no developers
# ---------------------------------------------------------------------------

async def remove_no_developer_games() -> dict:
    """
    Delete all games in MongoDB whose developerIds array is empty or absent.
    Pure MongoDB operation — no IGDB calls needed.
    Returns a summary dict.
    """
    logger.info("cleanup: remove_no_developer_games started")
    start  = time.time()
    db     = get_db()
    offset = 0
    deleted = 0
    errors  = 0

    # Match games with missing or empty developerIds
    filt = {"$or": [
        {"developerIds": {"$exists": False}},
        {"developerIds": {"$size": 0}},
        {"developerIds": None},
    ]}

    total = await db.games.count_documents(filt)
    logger.info("  %d games with no developerIds to remove", total)

    while True:
        page = await db.games.find(filt, {"_id": 1, "name": 1}) \
                             .sort("_id", 1).limit(BATCH_SIZE) \
                             .to_list(length=BATCH_SIZE)
        if not page:
            break

        mongo_ids = [doc["_id"] for doc in page]
        try:
            n = await _cascade_delete(db, mongo_ids)
            deleted += n
            offset  += n
            logger.info("  deleted batch of %d (running total: %d)", n, deleted)
        except Exception as exc:
            logger.error("  cascade delete failed: %s", exc)
            errors += 1
            break  # avoid infinite loop if delete keeps failing

    elapsed = round(time.time() - start, 1)
    summary = {
        "job":         "remove_no_developer_games",
        "status":      "ok",
        "deleted":     deleted,
        "errors":      errors,
        "elapsed_s":   elapsed,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    logger.info("cleanup: remove_no_developer_games finished: %s", summary)
    return summary
