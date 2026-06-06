"""
igdb_sync.py
Async port of WarpstarDB/backend/sync.py.

Strategy:
  - Fetches games in batches of 500 (IGDB hard limit), paginating until all
    games with rating_count >= MIN_RATING_COUNT are covered.
  - Upserts each game by igdb_id — same document shape as sync.py.
  - As a side effect, upserts a companies collection {igdbId, name} for
    every developer/publisher seen, so company data stays current.
  - Preserves Warpstar review aggregates (gameplayAvg, etc.) on existing docs
    via $setOnInsert — they are never overwritten by the sync.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import igdb_client
from pymongo import UpdateOne

from core.database import get_db

logger = logging.getLogger(__name__)

MIN_RATING_COUNT = 10
BATCH_SIZE       = 500  # IGDB hard limit per request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cover_url(cover: dict | None) -> str | None:
    if not cover:
        return None
    image_id = cover.get("image_id") or cover.get("url", "").split("/")[-1].replace(".jpg", "")
    if not image_id:
        return None
    return f"https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg"


def _names(items: list[dict] | None) -> list[str]:
    return [i["name"] for i in (items or []) if "name" in i]


def _ids(items: list[dict] | None) -> list[str]:
    return [str(i["id"]) for i in (items or []) if "id" in i]


def _release_date(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


def _split_companies(companies: list[dict] | None):
    """
    Return (developer_names, publisher_names, company_records).
    company_records is a list of {igdbId, name} dicts for the companies collection.
    """
    developers: list[str] = []
    publishers: list[str] = []
    company_records: list[dict] = []

    for entry in (companies or []):
        company = entry.get("company") or {}
        name    = company.get("name")
        cid     = company.get("id")
        if not name:
            continue
        if cid:
            company_records.append({"igdbId": str(cid), "name": name})
        if entry.get("developer"):
            developers.append(name)
        if entry.get("publisher"):
            publishers.append(name)

    return developers, publishers, company_records


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

async def _fetch_batch(offset: int) -> list[dict]:
    """Fetch one page of games from IGDB with all needed sub-fields."""
    body = (
        f"fields id, name, summary, cover.image_id, first_release_date, "
        f"rating, rating_count, "
        f"platforms.name, platforms.id, genres.name, genres.id, "
        f"themes.name, themes.id, keywords.name, "
        f"involved_companies.company.id, involved_companies.company.name, "
        f"involved_companies.developer, involved_companies.publisher, "
        f"similar_games; "
        f"where rating_count >= {MIN_RATING_COUNT} & version_parent = null; "
        f"sort rating_count desc; "
        f"limit {BATCH_SIZE}; offset {offset};"
    )
    return await igdb_client.query("games", body)


def _build_doc(game: dict) -> tuple[dict, list[dict]]:
    """
    Transform raw IGDB game dict into (game_doc, company_records).
    company_records are ready to upsert into the companies collection.
    """
    developers, publishers, company_records = _split_companies(game.get("involved_companies"))
    doc = {
        "igdb_id":         str(game["id"]),
        "name":            game.get("name", ""),
        "summary":         game.get("summary"),
        "coverUrl":        _cover_url(game.get("cover")),
        "releaseDate":     _release_date(game.get("first_release_date")),
        "igdbRating":      round(game["rating"] / 10, 2) if game.get("rating") else None,
        "igdbRatingCount": game.get("rating_count", 0),
        # Warpstar review aggregates — default 0 until reviews come in
        "gameplayAvg":     0,
        "contentAvg":      0,
        "narrativeAvg":    0,
        "aestheticsAvg":   0,
        "polishAvg":       0,
        "reviewTotal":     0,
        # Lookup arrays
        "platformIds":     _ids(game.get("platforms")),
        "genreIds":        _ids(game.get("genres")),
        "themeIds":        _ids(game.get("themes")),
        "platforms":       _names(game.get("platforms")),
        "genres":          _names(game.get("genres")),
        "themes":          _names(game.get("themes")),
        "keywords":        _names(game.get("keywords")),
        "developers":      developers,
        "publishers":      publishers,
        "similarTo":       [str(s) for s in (game.get("similar_games") or [])],
        "lastSynced":      datetime.now(tz=timezone.utc),
    }
    return doc, company_records


async def run_sync() -> dict:
    """
    Pull all qualifying games from IGDB and upsert them into MongoDB.
    Also upserts a companies collection as a side effect.
    Returns a summary dict with counts.
    """
    logger.info("IGDB sync started")
    start      = time.time()
    offset     = 0
    total_seen = 0
    upserted   = 0
    errors     = 0
    db         = get_db()

    while True:
        try:
            batch = await _fetch_batch(offset)
        except Exception as exc:
            logger.error("IGDB fetch failed at offset %d: %s", offset, exc)
            errors += 1
            break

        if not batch:
            break

        game_ops:    list[UpdateOne] = []
        company_ops: list[UpdateOne] = []

        for game in batch:
            try:
                doc, company_records = _build_doc(game)

                # Game upsert — never overwrite Warpstar review aggregates
                game_ops.append(UpdateOne(
                    {"igdb_id": doc["igdb_id"]},
                    {
                        "$set": {k: v for k, v in doc.items()
                                 if k not in ("gameplayAvg", "contentAvg", "narrativeAvg",
                                              "aestheticsAvg", "polishAvg", "reviewTotal")},
                        "$setOnInsert": {
                            "gameplayAvg":   0,
                            "contentAvg":    0,
                            "narrativeAvg":  0,
                            "aestheticsAvg": 0,
                            "polishAvg":     0,
                            "reviewTotal":   0,
                        },
                    },
                    upsert=True,
                ))

                # Companies upsert (additive — doesn't touch game docs)
                for rec in company_records:
                    company_ops.append(UpdateOne(
                        {"igdbId": rec["igdbId"]},
                        {"$set": rec},
                        upsert=True,
                    ))

            except Exception as exc:
                logger.warning("Skipping game id=%s: %s", game.get("id"), exc)
                errors += 1

        if game_ops:
            result   = await db.games.bulk_write(game_ops, ordered=False)
            upserted += result.upserted_count + result.modified_count

        if company_ops:
            await db.companies.bulk_write(company_ops, ordered=False)

        total_seen += len(batch)
        logger.info("  processed %d games (offset %d)", total_seen, offset)
        offset += BATCH_SIZE

        # IGDB rate limit: ~4 req/s — yield to event loop and sleep briefly
        await asyncio.sleep(0.3)

        if len(batch) < BATCH_SIZE:
            break

    elapsed = round(time.time() - start, 1)
    summary = {
        "status":      "ok",
        "total_seen":  total_seen,
        "upserted":    upserted,
        "errors":      errors,
        "elapsed_s":   elapsed,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    logger.info("IGDB sync finished: %s", summary)
    return summary
