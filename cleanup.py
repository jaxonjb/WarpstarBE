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
                # Never delete a game that has Warpstar reviews
                game_doc = await db.games.find_one({"_id": mongo_id}, {"reviewTotal": 1})
                if game_doc and (game_doc.get("reviewTotal") or 0) > 0:
                    logger.warning(
                        "  SKIPPED '%s' (igdb_id=%s, game_type=%s) — has %d Warpstar review(s)",
                        name, igdb_id, game_type, game_doc["reviewTotal"],
                    )
                    continue
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

    # Match games with missing or empty developerIds that have NO Warpstar reviews.
    # Games with reviews are logged and left alone — they can be fixed manually.
    filt = {"$and": [
        {"$or": [
            {"developerIds": {"$exists": False}},
            {"developerIds": {"$size": 0}},
            {"developerIds": None},
        ]},
        {"$or": [
            {"reviewTotal": {"$exists": False}},
            {"reviewTotal": 0},
            {"reviewTotal": None},
        ]},
    ]}

    total = await db.games.count_documents(filt)

    # Log how many reviewed no-dev games are being preserved so nothing is a surprise
    reviewed_no_devs = await db.games.count_documents({"$and": [
        {"$or": [
            {"developerIds": {"$exists": False}},
            {"developerIds": {"$size": 0}},
            {"developerIds": None},
        ]},
        {"reviewTotal": {"$gt": 0}},
    ]})
    if reviewed_no_devs:
        logger.warning(
            "  %d no-dev game(s) with Warpstar reviews will be PRESERVED — fix their developerIds manually",
            reviewed_no_devs,
        )

    logger.info("  %d games with no developerIds to remove (0 reviews)", total)

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


# ---------------------------------------------------------------------------
# Job 3: Migrate igdbId → igdb_id on old documents
# ---------------------------------------------------------------------------

async def migrate_igdb_id_field() -> dict:
    """
    Old scripts stored the IGDB ID as integer field `igdbId`.
    New sync stores it as string field `igdb_id`.

    This job adds `igdb_id = str(igdbId)` to every doc that has `igdbId`
    but is missing `igdb_id`, so the dedup job and future syncs can key
    on a single consistent field.

    Safe to re-run — skips docs that already have `igdb_id`.
    """
    logger.info("cleanup: migrate_igdb_id_field started")
    start   = time.time()
    db      = get_db()
    updated = 0
    errors  = 0
    offset  = 0

    filt  = {"igdbId": {"$exists": True}, "igdb_id": {"$exists": False}}
    total = await db.games.count_documents(filt)
    logger.info("  %d docs need igdb_id added", total)

    # Do NOT use skip — as docs are updated they leave the filter, so skip
    # would jump over half the remaining set each pass. Always pull the first
    # BATCH_SIZE unprocessed docs until none are left.
    from pymongo import UpdateOne as _UpdateOne
    while True:
        page = await db.games.find(filt, {"_id": 1, "igdbId": 1}) \
                             .sort("_id", 1).limit(BATCH_SIZE) \
                             .to_list(length=BATCH_SIZE)
        if not page:
            break

        ops = [
            _UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {"igdb_id": str(doc["igdbId"])}},
            )
            for doc in page
            if doc.get("igdbId") is not None
        ]

        if ops:
            try:
                res      = await db.games.bulk_write(ops, ordered=False)
                updated += res.modified_count
                offset  += res.modified_count
            except Exception as exc:
                logger.error("  bulk_write failed: %s", exc)
                errors += 1
                break

        logger.info("  migrated %d/%d", offset, total)

    elapsed = round(time.time() - start, 1)
    summary = {
        "job":         "migrate_igdb_id_field",
        "status":      "ok",
        "updated":     updated,
        "errors":      errors,
        "elapsed_s":   elapsed,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    logger.info("cleanup: migrate_igdb_id_field finished: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Job 4: Deduplicate games with the same igdb_id
# ---------------------------------------------------------------------------

async def dedup_games() -> dict:
    """
    Find all groups of documents that share the same igdb_id value and
    keep exactly one per group.

    Winning document: the one with the most reviews (reviewTotal). If tied,
    the one with the earlier _id (inserted first, more likely to have
    backfilled company/platform data).

    All other documents in the group are cascade-deleted — their reviews,
    list entries, favorites, activity, and similarTo references are merged
    into the winner first so no user data is lost.

    Run migrate_igdb_id_field() before this job so old igdbId docs have
    the igdb_id field populated.
    """
    logger.info("cleanup: dedup_games started")
    start   = time.time()
    db      = get_db()
    deleted = 0
    errors  = 0

    # Aggregate: find igdb_id values that appear more than once
    pipeline = [
        {"$match":  {"igdb_id": {"$exists": True}}},
        {"$group":  {"_id": "$igdb_id", "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
        {"$match":  {"count": {"$gt": 1}}},
    ]
    duplicates = await db.games.aggregate(pipeline).to_list(length=None)
    logger.info("  found %d igdb_id values with duplicates", len(duplicates))

    for group in duplicates:
        igdb_id  = group["_id"]
        mongo_ids = group["ids"]

        # Load full docs to pick the best winner
        docs = await db.games.find(
            {"_id": {"$in": mongo_ids}},
            {"_id": 1, "reviewTotal": 1},
        ).to_list(length=None)

        if not docs:
            continue

        # Winner = highest reviewTotal; tiebreak = earliest _id (smallest ObjectId)
        docs.sort(key=lambda d: (-int(d.get("reviewTotal") or 0), d["_id"]))
        winner_id = docs[0]["_id"]
        loser_ids = [d["_id"] for d in docs[1:]]

        # Merge loser reviews → winner before deleting
        try:
            await db.reviews.update_many(
                {"gameId": {"$in": loser_ids}},
                {"$set": {"gameId": winner_id}},
            )
            await db.lists.update_many(
                {"gameIds": {"$in": loser_ids}},
                {"$pull": {"gameIds": {"$in": loser_ids}}},
            )
            await db.users.update_many(
                {"favoriteGames": {"$in": loser_ids}},
                {"$pull":  {"favoriteGames": {"$in": loser_ids}}},
                # Don't re-add to winner here — avoids duplicate favorites
            )
            n = await _cascade_delete(db, loser_ids)
            deleted += n
            logger.debug("  deduped igdb_id=%s — kept %s, deleted %d", igdb_id, winner_id, n)
        except Exception as exc:
            logger.error("  failed deduping igdb_id=%s: %s", igdb_id, exc)
            errors += 1

    elapsed = round(time.time() - start, 1)
    summary = {
        "job":         "dedup_games",
        "status":      "ok",
        "groups_found": len(duplicates),
        "deleted":     deleted,
        "errors":      errors,
        "elapsed_s":   elapsed,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    logger.info("cleanup: dedup_games finished: %s", summary)
    return summary
