"""
igdb_sync.py
Async IGDB → MongoDB sync for WarpstarBE.

Filters — applied at every level (IGDB query, _build_doc, upsert key):
  - game_type = (0,4,8,9,10)              Main Game, Bundle, Remake, Remaster, Expanded Game
  - version_parent = null                  Base games only — no edition/variant entries
  - status != 6 & status != 7             Exclude Cancelled and Rumored titles
  - Edition name patterns                  Cosmetic-only editions caught by name regex
  - developers required                    Games with no developer info are skipped

Duplicate safeguards:
  - Upsert filter: {"$or": [{"igdb_id": str_id}, {"igdbId": int_id}]}
    Matches BOTH the new string field AND the legacy integer field, so old documents
    created by the original import scripts are updated in-place rather than duplicated.
  - igdb_id (string) AND igdbId (int) are both written to every document so either
    field can serve as a lookup key.
  - ensure_indexes() creates a unique index on igdb_id — enforced at the DB level.
  - post_sync_integrity_check() runs after every sync and logs any anomalies.

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
from pymongo import UpdateOne, ASCENDING

from core.database import get_db

logger = logging.getLogger(__name__)

BATCH_SIZE = 500  # IGDB hard limit per request

# Allowed IGDB game_type values — 0=Main Game, 4=Bundle, 8=Remake, 9=Remaster, 10=Expanded Game
ALLOWED_GAME_TYPES = {0, 4, 8, 9, 10}

# IGDB status codes to exclude — 6=Cancelled, 7=Rumored
EXCLUDE_STATUSES = {6, 7}

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

# Combined Mongo regex string for the integrity check query (no Python re flags needed;
# MongoDB $regex uses case-insensitive option separately)
_EDITION_MONGO_REGEX = "|".join(_REMOVE_PATTERNS)
_KEEP_MONGO_REGEX    = "|".join(_KEEP_PATTERNS)


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
    Parse involved_companies into parallel name/ID lists and company records.

    Returns:
        developer_names  — list[str]  developer company names
        publisher_names  — list[str]  publisher company names
        developer_ids    — list[str]  IGDB company ID strings for developers
        publisher_ids    — list[str]  IGDB company ID strings for publishers
        company_records  — list[dict] {igdbId, name} for the companies collection
    """
    developer_names: list[str]  = []
    publisher_names: list[str]  = []
    developer_ids:   list[str]  = []
    publisher_ids:   list[str]  = []
    company_records: list[dict] = []

    for entry in (companies or []):
        company = entry.get("company") or {}
        name    = company.get("name")
        cid     = company.get("id")
        is_dev  = entry.get("developer", False)
        is_pub  = entry.get("publisher", False)

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
# Index management
# ---------------------------------------------------------------------------

async def ensure_indexes() -> None:
    """
    Create indexes required for correct sync behaviour.
    Called once at app startup (idempotent — safe to call repeatedly).

      games.igdb_id  — unique, sparse  (sparse so old docs without the field
                        don't all collide on null)
      games.nameNormalized — for accent-insensitive search
    """
    db = get_db()
    try:
        await db.games.create_index(
            [("igdb_id", ASCENDING)],
            unique=True,
            sparse=True,
            name="igdb_id_unique",
            background=True,
        )
        await db.games.create_index(
            [("nameNormalized", ASCENDING)],
            name="nameNormalized_1",
            background=True,
        )
        logger.info("DB indexes verified.")
    except Exception as exc:
        logger.warning("ensure_indexes: %s", exc)


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

async def _fetch_batch(offset: int) -> list[dict]:
    """Fetch one page of games from IGDB with all required sub-fields.

    Server-side IGDB filters:
      - game_type = (0,4,8,9,10)                      allowed types only
      - version_parent = null                          base games only
      - status = null | (status != 6 & status != 7)   exclude Cancelled/Rumored
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
    Returns None if the game fails any client-side filter.

    Client-side filters (after IGDB query):
      - Edition name pattern match  → skip
      - No developer IDs            → skip
    """
    name = game.get("name", "")

    if is_edition_name(name):
        return None

    (developer_names, publisher_names,
     developer_ids, publisher_ids,
     company_records) = _split_companies(game.get("involved_companies"))

    if not developer_ids:
        return None

    igdb_id_int = int(game["id"])
    igdb_id_str = str(game["id"])

    doc = {
        # Both key formats — igdb_id (str) is the canonical upsert key;
        # igdbId (int) preserves compatibility with legacy scripts.
        "igdb_id":         igdb_id_str,
        "igdbId":          igdb_id_int,
        "name":            name,
        "nameNormalized":  normalize_name(name),
        "summary":         game.get("summary"),
        "coverUrl":        _cover_url(game.get("cover")),
        "releaseDate":     _release_date(game.get("first_release_date")),
        "igdbRating":      round(game["rating"] / 10, 2) if game.get("rating") else None,
        "igdbRatingCount": game.get("rating_count", 0),
        # Warpstar review aggregates — written only on first insert via $setOnInsert
        "gameplayAvg":     0,
        "contentAvg":      0,
        "narrativeAvg":    0,
        "aestheticsAvg":   0,
        "polishAvg":       0,
        "reviewTotal":     0,
        # ID arrays for filtering/lookup
        "platformIds":     _ids(game.get("platforms")),
        "genreIds":        _ids(game.get("genres")),
        "themeIds":        _ids(game.get("themes")),
        # Name arrays (denormalised for quick display)
        "platforms":       _names(game.get("platforms")),
        "genres":          _names(game.get("genres")),
        "themes":          _names(game.get("themes")),
        "keywords":        _names(game.get("keywords")),
        # Company IDs — used by _resolve_game() in routers/games.py
        "developerIds":    developer_ids,
        "publisherIds":    publisher_ids,
        # Company names — denormalised fallback
        "developers":      developer_names,
        "publishers":      publisher_names,
        "similarTo":       [str(s) for s in (game.get("similar_games") or [])],
        "lastSynced":      datetime.now(tz=timezone.utc),
    }
    return doc, company_records


def _upsert_update(doc: dict) -> dict:
    """
    Build the MongoDB update document for a game upsert.
    Review aggregates are in $setOnInsert so they are never overwritten.
    """
    protected = {"gameplayAvg", "contentAvg", "narrativeAvg",
                 "aestheticsAvg", "polishAvg", "reviewTotal"}
    return {
        "$set": {k: v for k, v in doc.items() if k not in protected},
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
    """
    Build a bulk UpdateOne that:
      - matches on igdb_id (string) OR igdbId (int) — prevents duplicates
        when the DB contains legacy documents from the original import scripts
      - writes both igdb_id and igdbId on every doc going forward
      - never overwrites Warpstar review aggregates
    """
    igdb_id_str = doc["igdb_id"]
    igdb_id_int = doc["igdbId"]
    return UpdateOne(
        {"$or": [{"igdb_id": igdb_id_str}, {"igdbId": igdb_id_int}]},
        _upsert_update(doc),
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Post-sync integrity check
# ---------------------------------------------------------------------------

async def post_sync_integrity_check() -> dict:
    """
    Run after every sync. Counts anomalies and logs warnings — reads only,
    never modifies data.

    Checks:
      1. Duplicate igdb_id values             (should be 0 with unique index)
      2. Games with no developerIds           (should be 0 after sync filters)
      3. Games with edition-style names       (should be 0 after sync filters)
      4. Games without nameNormalized         (would break accent-insensitive search)
    """
    db = get_db()
    issues: dict[str, int] = {}

    # 1. Duplicates
    pipeline = [
        {"$match":  {"igdb_id": {"$exists": True}}},
        {"$group":  {"_id": "$igdb_id", "count": {"$sum": 1}}},
        {"$match":  {"count": {"$gt": 1}}},
        {"$count":  "total"},
    ]
    dup_result = await db.games.aggregate(pipeline).to_list(length=1)
    dupes = dup_result[0]["total"] if dup_result else 0
    issues["duplicate_igdb_ids"] = dupes
    if dupes:
        logger.warning("INTEGRITY: %d duplicate igdb_id group(s) found — run /admin/cleanup/dedup", dupes)

    # 2. No developerIds
    no_devs = await db.games.count_documents({"$or": [
        {"developerIds": {"$exists": False}},
        {"developerIds": {"$size": 0}},
    ]})
    issues["no_developer_ids"] = no_devs
    if no_devs:
        logger.warning("INTEGRITY: %d game(s) have no developerIds — run /admin/cleanup/no-devs", no_devs)

    # 3. Edition-style names (matches remove pattern AND doesn't match keep pattern)
    edition_candidates = await db.games.count_documents({
        "name": {"$regex": _EDITION_MONGO_REGEX, "$options": "i"},
    })
    # Subtract the ones protected by keep patterns (rough count — good enough for a warning)
    keep_protected = await db.games.count_documents({
        "name": {"$regex": _KEEP_MONGO_REGEX, "$options": "i"},
    })
    edition_estimate = max(0, edition_candidates - keep_protected)
    issues["edition_name_estimate"] = edition_estimate
    if edition_estimate:
        logger.warning(
            "INTEGRITY: ~%d game(s) may have cosmetic edition names — review manually",
            edition_estimate,
        )

    # 4. Missing nameNormalized
    missing_norm = await db.games.count_documents({"nameNormalized": {"$exists": False}})
    issues["missing_nameNormalized"] = missing_norm
    if missing_norm:
        logger.warning("INTEGRITY: %d game(s) missing nameNormalized field", missing_norm)

    if not any(issues.values()):
        logger.info("INTEGRITY: all checks passed — DB looks clean.")

    return issues


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

async def run_sync() -> dict:
    """
    Pull all qualifying games from IGDB and upsert them into MongoDB.
    Also upserts a companies collection as a side effect.
    Runs a read-only integrity check at the end and includes results in summary.
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

        await asyncio.sleep(0.3)  # IGDB rate limit ~4 req/s

        if len(batch) < BATCH_SIZE:
            break

    elapsed = round(time.time() - start, 1)

    # Read-only integrity check — logged as warnings if anything is off
    logger.info("Running post-sync integrity check...")
    integrity = await post_sync_integrity_check()

    summary = {
        "status":      "ok",
        "total_seen":  total_seen,
        "upserted":    upserted,
        "skipped":     skipped,
        "errors":      errors,
        "elapsed_s":   elapsed,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "integrity":   integrity,
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
        raise ValueError(
            f"IGDB {igdb_id} is a version/edition of another game — add the base game instead."
        )

    game_type = game.get("game_type")
    if game_type not in ALLOWED_GAME_TYPES:
        labels  = {0: "Main Game", 4: "Bundle", 8: "Remake", 9: "Remaster", 10: "Expanded Game"}
        label   = labels.get(game_type, f"type {game_type}")
        allowed = ", ".join(labels.values())
        raise ValueError(
            f"IGDB {igdb_id} has game_type '{label}'. Only allowed: {allowed}."
        )

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
        {"$or": [{"igdb_id": doc["igdb_id"]}, {"igdbId": doc["igdbId"]}]},
        _upsert_update(doc),
        upsert=True,
    )

    if company_records:
        ops = [UpdateOne({"igdbId": r["igdbId"]}, {"$set": r}, upsert=True)
               for r in company_records]
        await db.companies.bulk_write(ops, ordered=False)

    return await db.games.find_one({"igdb_id": doc["igdb_id"]})
