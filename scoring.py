"""
scoring.py
==========
Recommendation scoring algorithm for Warpstar.

Each candidate game is scored 0–100 by combining:
  1. Weighted factor score   — gameplay/aesthetics/content/polish/narrative
  2. Genre match             — overlap with user's top genres
  3. Platform match          — game supports at least one of user's platforms
  4. Recency                 — newer releases score higher
  5. Familiarity boost       — from developers/genres the user has rated well
  6. Already seen penalty    — reviewed or favorited games are suppressed

Default weights are all 5 (neutral, 0–10 scale). The algorithm normalises
weights internally so only relative values matter.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[str, float] = {
    "gameplay":      5.0,
    "aesthetics":    5.0,
    "content":       5.0,
    "polish":        5.0,
    "narrative":     5.0,
    "genreMatch":    5.0,
    "platformMatch": 5.0,
    "recency":       5.0,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(weights: dict[str, float]) -> dict[str, float]:
    """Scale weights so the factor group and signal group each sum to 1."""
    factor_keys  = ["gameplay", "aesthetics", "content", "polish", "narrative"]
    signal_keys  = ["genreMatch", "platformMatch", "recency"]

    def group_norm(keys: list[str]) -> dict[str, float]:
        total = sum(max(weights.get(k, 0), 0) for k in keys)
        if total == 0:
            return {k: 1 / len(keys) for k in keys}
        return {k: max(weights.get(k, 0), 0) / total for k in keys}

    return {**group_norm(factor_keys), **group_norm(signal_keys)}


def _recency_score(release_date: str | None) -> float:
    """
    Returns 0–1. Games released in the last year score ~1.0,
    games from 10+ years ago score ~0.1. Uses exponential decay.
    """
    if not release_date:
        return 0.3  # unknown date — neutral
    try:
        dt      = datetime.fromisoformat(release_date.replace("Z", "+00:00"))
        now     = datetime.now(timezone.utc)
        age_yrs = max((now - dt).days / 365.25, 0)
        # Half-life of ~4 years: score = e^(-0.17 * age)
        return math.exp(-0.17 * age_yrs)
    except (ValueError, TypeError):
        return 0.3


def _genre_overlap(game_genres: list[str], user_genres: list[str]) -> float:
    """Jaccard-style overlap, returns 0–1."""
    if not game_genres or not user_genres:
        return 0.0
    g = set(g.lower() for g in game_genres)
    u = set(g.lower() for g in user_genres)
    return len(g & u) / len(g | u)


def _platform_match(game_platforms: list[str], user_platforms: list[str]) -> float:
    """1.0 if any overlap, 0.0 otherwise."""
    if not game_platforms or not user_platforms:
        return 0.5  # unknown — neutral
    g = set(p.lower() for p in game_platforms)
    u = set(p.lower() for p in user_platforms)
    return 1.0 if g & u else 0.0


def _factor_score(game: dict[str, Any], norm_weights: dict[str, float]) -> float:
    """Weighted average of the game's per-factor averages, 0–10, normalised to 0–1."""
    mapping = {
        "gameplay":   "gameplayAvg",
        "aesthetics": "aestheticsAvg",
        "content":    "contentAvg",
        "polish":     "polishAvg",
        "narrative":  "narrativeAvg",
    }
    score = sum(
        norm_weights[k] * (game.get(v) or 0)
        for k, v in mapping.items()
    )
    return score / 10.0  # normalise to 0–1


def _familiarity_boost(
    game: dict[str, Any],
    user_genre_scores: dict[str, float],   # genre -> avg score user gave games in that genre
    user_dev_scores:   dict[str, float],   # developer -> avg score user gave their games
) -> float:
    """
    0–1 boost if the game shares genres/developers the user has historically
    rated well (avg > 7). Soft boost — adds up to ~0.15 to the final score.
    """
    boost = 0.0

    genres = [g.lower() for g in (game.get("genres") or [])]
    for genre in genres:
        avg = user_genre_scores.get(genre, 0)
        if avg >= 8.0:
            boost += 0.06
        elif avg >= 7.0:
            boost += 0.03

    devs = [d.lower() for d in (game.get("developers") or [])]
    for dev in devs:
        avg = user_dev_scores.get(dev, 0)
        if avg >= 8.0:
            boost += 0.05
        elif avg >= 7.0:
            boost += 0.02

    return min(boost, 0.15)


# ---------------------------------------------------------------------------
# Build user context from their review history
# ---------------------------------------------------------------------------

def build_user_context(user_reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Derives genre/developer affinity scores from the user's past reviews.
    Call once per recommendation request and pass to score_game().

    user_reviews: list of review dicts, each must have:
        overallScore, genres (list), developers (list)
    """
    genre_scores: dict[str, list[float]] = {}
    dev_scores:   dict[str, list[float]] = {}

    for r in user_reviews:
        s = r.get("overallScore") or 0
        for g in (r.get("genres") or []):
            genre_scores.setdefault(g.lower(), []).append(s)
        for d in (r.get("developers") or []):
            dev_scores.setdefault(d.lower(), []).append(s)

    return {
        "genre_scores": {k: sum(v) / len(v) for k, v in genre_scores.items()},
        "dev_scores":   {k: sum(v) / len(v) for k, v in dev_scores.items()},
    }


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_game(
    game:            dict[str, Any],
    user:            dict[str, Any],
    user_context:    dict[str, Any],
    weights:         dict[str, float] | None = None,
    reviewed_ids:    set[str]               = set(),
    favorited_ids:   set[str]               = set(),
) -> float:
    """
    Score a single game for a user. Returns 0.0–100.0.

    Parameters
    ----------
    game          : MongoDB game document (serialised to dict)
    user          : MongoDB user document (serialised to dict)
    user_context  : output of build_user_context()
    weights       : 0–10 per-key weights; falls back to user prefs then defaults
    reviewed_ids  : set of game IDs the user has already reviewed
    favorited_ids : set of game IDs the user has favorited
    """
    game_id = str(game.get("id") or game.get("_id") or "")

    # Already seen — suppress hard
    if game_id in reviewed_ids:
        return 0.0
    if game_id in favorited_ids:
        return 5.0  # still show but very low

    # Merge weight sources: request > user prefs > defaults
    prefs_weights = (user.get("preferences") or {}).get("weights") or {}
    merged = {**DEFAULT_WEIGHTS, **prefs_weights, **(weights or {})}
    norm   = _normalise(merged)

    # 1. Weighted factor score (0–1)
    has_warpstar = (game.get("reviewTotal") or 0) > 0
    if has_warpstar:
        f_score = _factor_score(game, norm)
    else:
        # Fall back to IGDB rating normalised to 0–1
        f_score = (game.get("igdbRating") or 0) / 100.0

    # 2. Genre match (0–1)
    user_genres  = (user.get("preferences") or {}).get("topGenres") or []
    game_genres  = game.get("genres") or []
    g_score      = _genre_overlap(game_genres, user_genres)

    # 3. Platform match (0–1)
    user_platforms = (user.get("preferences") or {}).get("platforms") or []
    game_platforms = game.get("platforms") or []
    p_score        = _platform_match(game_platforms, user_platforms)

    # 4. Recency (0–1)
    r_score = _recency_score(game.get("releaseDate"))

    # --- Weighted combination ---
    # Factor group contributes 70% of the raw score, signals 30%
    FACTOR_WEIGHT = 0.70
    SIGNAL_WEIGHT = 0.30

    signal_score = (
        norm["genreMatch"]    * g_score +
        norm["platformMatch"] * p_score +
        norm["recency"]       * r_score
    )

    raw = FACTOR_WEIGHT * f_score + SIGNAL_WEIGHT * signal_score

    # 5. Familiarity boost (additive, capped at +0.15)
    boost = _familiarity_boost(
        game,
        user_context.get("genre_scores", {}),
        user_context.get("dev_scores",   {}),
    )

    # 6. Quality floor — penalise very low scored games
    review_total = game.get("reviewTotal") or 0
    if has_warpstar and f_score < 0.4:   # avg < 4.0 on Warpstar
        raw *= 0.5
    elif not has_warpstar and (game.get("igdbRating") or 0) < 40:
        raw *= 0.7

    # 7. Confidence boost for games with more reviews (log scale)
    if review_total > 1:
        confidence = min(math.log10(review_total) / 2.0, 0.1)
        raw += confidence

    final = min((raw + boost) * 100, 100.0)
    return round(final, 2)


# ---------------------------------------------------------------------------
# Batch scoring
# ---------------------------------------------------------------------------

def rank_games(
    games:          list[dict[str, Any]],
    user:           dict[str, Any],
    user_reviews:   list[dict[str, Any]],
    weights:        dict[str, float] | None = None,
    limit:          int = 20,
) -> list[dict[str, Any]]:
    """
    Score and rank a list of games for a user.
    Returns the top `limit` games sorted by score descending,
    with a `_score` field added to each game dict.
    """
    reviewed_ids  = {str(r.get("gameId") or "") for r in user_reviews}
    favorited_ids = {str(f) for f in (user.get("favoriteGames") or [])}
    user_context  = build_user_context(user_reviews)

    scored = []
    for game in games:
        s = score_game(
            game          = game,
            user          = user,
            user_context  = user_context,
            weights       = weights,
            reviewed_ids  = reviewed_ids,
            favorited_ids = favorited_ids,
        )
        if s > 0:
            scored.append({**game, "_score": s})

    scored.sort(key=lambda g: g["_score"], reverse=True)
    return scored[:limit]
