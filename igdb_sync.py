"""
igdb_sync.py
Async IGDB → MongoDB sync for WarpstarBE.

Filters applied to every game (bulk sync and single-game requests):
  - game_type = (0,4,8,9,10)    — Main Game, Bundle, Remake, Remaster, Expanded Game only
  - version_parent = null        — base games only, no edition/variant entries
  - status = null|(status!=6&status!=7) — exclude Cancelled (6) and Rumored (7)
  - Edition name patterns         — cosmetic-only editions caught by name (filter_editions.py logic)
  - developers required           — games with no developer info are skipped

Games docs include:
  - developerIds / publisherIds  — IGDB company ID strings (same as backfill_company_ids.py)
  - developers / publishers      — name arrays resolved by the games router's lookup map
  - nameNormalized               — diacritic-stripped lowercase for accent-insensitive search

Warpstar review aggregates (gameplayAvg, etc.) are protected via $setOnInsert.
A companies collection {igdbId, name} is upserted as a side effect.
"""

import asyncio
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone

import igdb_client
from pymongo import UpdateOne

from core.database import get_db

logger = logging.getLogger(__name__)

BATCH_SIZE = 500  # IGDB hard limit per request

# Allowed IGDB game_type values — same as the original igdbWSdataupdate.py filter
# 0=Main Game, 4=Bundle, 8=Remake, 9=Remaster, 10=Expanded Game
ALLOWED_GAME_TYPES = {0, 4, 8, 9, 10}

# IGDB status codes to exclude  (same as igdb_status_cleanup.py DEFAULT_REMOVE_STATUSES)
EXCLUDE_STATUSES = {6, 7}  # 6=Cancelled, 7=Rumored

# ---------------------------------------------------------------------------
# Edition name filtering  (ported from WarpstarDB/Scripts/filter_editions.py)
# ---------------------------------------------------------------------------

_REMOVE_PATTERNS = [
    r"Digital Deluxe", r"Deluxe Edition", r"Deluxe Bundle", r"Deluxe Pack",
    r"Collector'?s Edition", r"Collector'?s Pack", r"Collector'?s Bundle",
    r"Premium Edition", r"Gold Edition", r"Special Edition",
    r"Legendary Edition", r"Champion'?s Edition", r"Founder'?s (?:Pack|Edition)",
    r"Season Pass Edition", r"Super Deluxe", r"VIP Edition",
    r"Elite Edition", r"Ultimate Edition", r"Bonus Edition", r"Limited Edition",
]

_KEEP_PATTERNS = [
    r"Game of the Year", r"\bGOTY\b", r"Definitive Edition", r"Complete Edition",
    r"Enhanced Edition", r"Remaster(?:ed)?", r"\bHD\b",
    r"Anniversary Edition", r"Extended Edition", r"Director'?s Cut", r"Expanded Edition",
]

_REMOVE_RE = re.compile("|".join(f"(?:{p})" for p in _REMOVE_PATTERNS), re.IGNORECASE)
_KEEP_RE   = re.compile("|".join(f"(?:{p})" for p in _KEEP_PATTERNS),   re.IGNORECASE)


def is_edition_name(name: str) -> bool:
    """True if the name looks like a cosmetic-only edition that should be excluded."""
    return bool(_REMOVE_RE.search(name)) and not bool(_KEEP_RE.search(name))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Strip diacritics and lowercase — enables accent-insensitive search."""
    nfd = unicodedata.normalize("NFD", name or "")
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


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
    Parse involved_companies into parallel name and ID lists, plus company records.

    Returns:
        developer_names   — list[str]  name of each developer company
        publisher_names   — list[str]  name of each publisher company
        developer_ids     — list[str]  IGDB company ID strings for developers
        publisher_ids     — list[str]  IGDB company ID strings for publishers
        company_records   — list[dict] {igdbId, name} ready to upsert into companies collection

    Matches the ID-first approach of WarpstarDB/Scripts/backfill_company_ids.py so
    the games router's _resolve_game() can look up names from developerIds/publisherIds.
    """
    developer_names:  list[str]  = []
    publisher_names:  list[str]  = []
    developer_ids:    list[str]  = []
    publisher_ids:    list[str]  = []
    company_records:  list[dict] = []

    for entry in (companies or []):
        company  = entry.get("company") or {}
        name     = company.get("name")
        cid      = company.get("id")
        is_dev   = entry.get("developer", False)
        is_pub   = entry.get("publisher", False)

        if not name:
            continue

        if cid:
            company_records.append({"igdbId": str(cid), "name": name})
            if is_dev:
                developer_ids.append(str(cid))
            if is_pub:
                publisher_ids.append(str(cid))

        if is_dev:
            developer_names.append(name)
        if is_pub:
            publisher_names.append(name)

    return developer_names, publisher_names, developer_ids, publisher_ids, company_records


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

async def _fetch_batch(offset: int) -> list[dict]:
    """Fetch one page of games from IGDB with all needed sub-fields.

    Filters applied server-side:
      - game_type = (0,4,8,9,10)                    → allowed game types only
      - version_parent = null                        → base games only
      - status = null | (status != 6 & status != 7) → exclude Cancelled/Rumored
    """
    body = (
        "fields id, name, summary, cover.image_id, first_release_date, "
        "rating, rating_count, status, game_type, "
        "platforms.name, platforms.id, genres.name, genres.id, "
        "themes.name, themes.id, keywords.name, "
        "involved_companies.company.id, involved_companies.company.name, "
        "involved_companies.developer, involved_companies.publisher, "
        "similar_games; "
        "where game_type = (0,4,8,9,10) "
        "& version_parent = null "
        "& (status = null | (status != 6 & status != 7)); "
        "sort rating_count desc; "
        f"limit {BATCH_SIZE}; offset {offset};"
    )
    return await igdb_client.query("games", body)


def _build_doc(game: dict) -> tuple[dict, list[dict]] | None:
    """
    Transform a raw IGDB game dict into (game_doc, company_records).
    Returns None if the game should be skipped.

    Skip conditions:
      - Name matches a cosmetic-edition pattern
      - No developer companies
    """
    name = game.get("name", "")

    # Skip cosmetic-only editions that slipped through the IGDB version_parent filter
    if is_edition_name(name):
        return None

    (developer_names, publisher_names,
     developer_ids, publisher_ids,
     company_records) = _split_companies(game.get("involved_companies"))

    # Skip games with no developer info — consistent with user requirement
    if not developer_ids:
        return None

    doc = {
        "igdb_id":         str(game["id"]),
        "name":            name,
        "nameNormalized":  normalize_name(name),
        "summary":         game.get("summary"),
        "coverUrl":        _cover_url(game.get("cover")),
        "releaseDate":     _release_date(game.get("first_release_date")),
        "igdbRating":      round(game["rating"] / 10, 2) if game.get("rating") else None,
        "igdbRatingCount": game.get("rating_count", 0),
        # Warpstar review aggregates — default 0 on insert, never overwritten by sync
        "gameplayAvg":     0,
        "contentAvg":      0,
        "narrativeAvg":    0,
        "aestheticsAvg":   0,
        "polishAvg":       0,
        "reviewTotal":     0,
        # Platform / genre / theme ID arrays (for filtering)
        "platformIds":     _ids(game.get("platforms")),
        "genreIds":        _ids(game.get("genres")),
        "themeIds":        _ids(game.get("themes")),
        # Name arrays stored for direct use; also resolved by the games router lookup map
        "platforms":       _names(game.get("platforms")),
        "genres":          _names(game.get("genres")),
        "themes":          _names(game.get("themes")),
        "keywords":        _names(game.get("keywords")),
        # Company IDs (strings) — used by _resolve_game() in routers/games.py
        "developerIds":    developer_ids,
        "publisherIds":    publisher_ids,
        # Company name arrays — fallback / denormalized copy
        "developers":      developer_names,
        "publishers":      publisher_names,
        "similarTo":       [str(s) for s in (game.get("similar_games") or [])],
        "lastSynced":      datetime.now(tz=timezone.utc),
    }
    return doc, company_records


def _upsert_update(doc: dict) -> dict:
    """Build the $set / $setOnInsert update dict for a game document."""
    return {
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
    }


def _game_upsert_op(doc: dict) -> UpdateOne:
    """Build a bulk UpdateOne that protects Warpstar review aggregates."""
    return UpdateOne({"igdb_id": doc["igdb_id"]}, _upsert_update(doc), upsert=True)


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
    skipped    = 0
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
                result = _build_doc(game)
                if result is None:
                    skipped += 1
                    continue

                doc, company_records = result
                game_ops.append(_game_upsert_op(doc))

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
            res      = await db.games.bulk_write(game_ops, ordered=False)
            upserted += res.upserted_count + res.modified_count

        if company_ops:
            await db.companies.bulk_write(company_ops, ordered=False)

        total_seen += len(batch)
        logger.info("  processed %d games (offset %d, skipped %d)",
                    total_seen, offset, skipped)
        offset += BATCH_SIZE

        # IGDB rate limit: ~4 req/s
        await asyncio.sleep(0.3)

        if len(batch) < BATCH_SIZE:
            break

    elapsed = round(time.time() - start, 1)
    summary = {
        "status":      "ok",
        "total_seen":  total_seen,
        "upserted":    upserted,
        "skipped":     skipped,
        "errors":      errors,
        "elapsed_s":   elapsed,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    logger.info("IGDB sync finished: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Single-game fetch — used by the developer game-request endpoint
# ---------------------------------------------------------------------------

async def fetch_and_upsert_by_igdb_id(igdb_id: int) -> dict:
    """
    Fetch one game from IGDB by its numeric ID, apply the same filters used
    during the bulk sync, and upsert it into MongoDB.

    Returns the upserted game document (with _id).
    Raises ValueError with a human-readable reason if the game is rejected.
    """
    body = (
        "fields id, name, summary, cover.image_id, first_release_date, "
        "rating, rating_count, status, version_parent, game_type, "
        "platforms.name, platforms.id, genres.name, genres.id, "
        "themes.name, themes.id, keywords.name, "
        "involved_companies.company.id, involved_companies.company.name, "
        "involved_companies.developer, involved_companies.publisher, "
        "similar_games; "
        f"where id = {igdb_id};"
    )
    results = await igdb_client.query("games", body)
    if not results:
        raise ValueError(f"IGDB ID {igdb_id} not found.")

    game = results[0]

    if game.get("version_parent") is not None:
        raise ValueError(f"IGDB {igdb_id} is a version/edition of another game — add the base game instead.")

    game_type = game.get("game_type")
    if game_type not in ALLOWED_GAME_TYPES:
        type_labels = {0: "Main Game", 4: "Bundle", 8: "Remake", 9: "Remaster", 10: "Expanded Game"}
        label = type_labels.get(game_type, f"type {game_type}")
        allowed = ", ".join(type_labels.values())
        raise ValueError(f"IGDB {igdb_id} has game_type '{label}'. Only allowed: {allowed}.")

    status = game.get("status")
    if status in EXCLUDE_STATUSES:
        label = {6: "Cancelled", 7: "Rumored"}.get(status, str(status))
        raise ValueError(f"IGDB {igdb_id} has status '{label}' and cannot be added.")

    result = _build_doc(game)
    if result is None:
        name = game.get("name", "")
        if is_edition_name(name):
            raise ValueError(f"'{name}' matched an edition name pattern and was rejected.")
        raise ValueError(f"IGDB {igdb_id} has no developer info and cannot be added.")

    doc, company_records = result
    db = get_db()

    await db.games.update_one(
        {"igdb_id": doc["igdb_id"]},
        _upsert_update(doc),
        upsert=True,
    )

    if company_records:
        ops = [UpdateOne({"igdbId": r["igdbId"]}, {"$set": r}, upsert=True)
               for r in company_records]
        await db.companies.bulk_write(ops, ordered=False)

    return await db.games.find_one({"igdb_id": doc["igdb_id"]})
