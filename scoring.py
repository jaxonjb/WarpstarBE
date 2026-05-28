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
  6. Feedback signal         — boost/penalty from explicit thumbs up/down
  7. Already seen penalty    — reviewed, favorited, or thumbs-downed games are suppressed

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


def _feedback_signal(
    game:            dict[str, Any],
    liked_genres:    set[str],
    liked_devs:      set[str],
    disliked_genres: set[str],
    disliked_devs:   set[str],
) -> float:
    """
    Boost/penalty derived from explicit thumbs feedback on other games.
    Stronger than the implicit familiarity boost because it's a direct
    user signal. Bounded between -0.25 (heavily penalised) and +0.15.
    """
    genres = {g.lower() for g in (game.get("genres") or [])}
    devs   = {d.lower() for d in (game.get("developers") or [])}

    signal = 0.0
    signal += 0.05 * len(genres & liked_genres)
    signal += 0.08 * len(devs   & liked_devs)
    signal -= 0.08 * len(genres & disliked_genres)
    signal -= 0.12 * len(devs   & disliked_devs)

    return max(min(signal, 0.15), -0.25)


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
# Reason generation — explain *why* a game was recommended
# ---------------------------------------------------------------------------

FACTOR_FIELDS = {
    "gameplay":   ("gameplayAvg",   "gameplay"),
    "aesthetics": ("aestheticsAvg", "visuals and sound"),
    "content":    ("contentAvg",    "content depth"),
    "polish":     ("polishAvg",     "polish"),
    "narrative":  ("narrativeAvg",  "story and writing"),
}


def _fmt_score(s: float) -> str:
    """
    Format a 0-10 score the same way StarPolarDiagram does — whole numbers
    show bare (e.g. "8"), everything else gets one decimal (e.g. "8.4").
    """
    rounded = round(s, 1)
    if float(rounded).is_integer():
        return str(int(rounded))
    return f"{rounded:.1f}"


def _generate_reasons(
    game:            dict[str, Any],
    user:            dict[str, Any],
    user_context:    dict[str, Any],
    liked_genres:    set[str],
    liked_devs:      set[str],
) -> list[dict[str, str]]:
    """
    Build a list of human-readable reasons explaining why this game was
    recommended. Each reason has a `type` (used for icon/colour theming
    in the UI) and a `text` (the user-facing string).

    Returned in priority order — strongest signals first.
    """
    reasons: list[dict[str, str]] = []

    prefs          = user.get("preferences") or {}
    user_genres    = list(prefs.get("topGenres") or [])
    user_platforms = list(prefs.get("platforms") or [])
    user_weights   = prefs.get("weights")  or {}

    game_genres = game.get("genres")     or []
    game_devs   = game.get("developers") or []
    game_pfs    = game.get("platforms")  or []

    # --- 1. Explicit thumbs-up signal (strongest — direct user feedback)
    liked_dev_overlap = [d for d in game_devs if d.lower() in liked_devs]
    if liked_dev_overlap:
        reasons.append({
            "type": "feedback",
            "text": f"You've liked other games by {liked_dev_overlap[0]}",
        })

    liked_genre_overlap = [g for g in game_genres if g.lower() in liked_genres]
    if liked_genre_overlap and not liked_dev_overlap:
        reasons.append({
            "type": "feedback",
            "text": f"Similar to games you've thumbed up ({liked_genre_overlap[0]})",
        })

    # --- 2. Review-history affinity (developer / genre)
    dev_scores   = user_context.get("dev_scores",   {})
    genre_scores = user_context.get("genre_scores", {})

    high_rated_dev = next(
        (d for d in game_devs if dev_scores.get(d.lower(), 0) >= 7.0),
        None,
    )
    if high_rated_dev:
        avg = dev_scores[high_rated_dev.lower()]
        reasons.append({
            "type": "history",
            "text": f"You've rated {high_rated_dev}'s games {_fmt_score(avg)}/10 on average",
        })

    high_rated_genre = next(
        (g for g in game_genres if genre_scores.get(g.lower(), 0) >= 7.0),
        None,
    )
    if high_rated_genre:
        avg = genre_scores[high_rated_genre.lower()]
        reasons.append({
            "type": "history",
            "text": f"You've rated {high_rated_genre} games {_fmt_score(avg)}/10 on average",
        })
    else:
        # Even a softer history signal — user has reviewed this genre before
        explored_genre = next(
            (g for g in game_genres
             if 0 < genre_scores.get(g.lower(), 0) < 7.0),
            None,
        )
        if explored_genre and not high_rated_genre:
            reasons.append({
                "type": "history",
                "text": f"You've explored {explored_genre} games before",
            })

    # --- 3. Genre match against onboarding preferences
    user_genre_set = {g.lower() for g in user_genres}
    genre_overlap  = [g for g in game_genres if g.lower() in user_genre_set]
    if genre_overlap and not high_rated_genre:
        # If it matches the user's top-listed genre, call that out specifically
        top_genre = user_genres[0] if user_genres else None
        if top_genre and top_genre.lower() in {g.lower() for g in genre_overlap}:
            reasons.append({
                "type": "genre",
                "text": f"A strong fit for your top genre: {top_genre}",
            })
        else:
            label = ", ".join(genre_overlap[:2])
            reasons.append({
                "type": "genre",
                "text": f"Matches your favourite genre{'s' if len(genre_overlap) > 1 else ''}: {label}",
            })

    # --- 4. Platform match
    user_pf_set = {p.lower() for p in user_platforms}
    pf_overlap  = [p for p in game_pfs if p.lower() in user_pf_set]
    if pf_overlap:
        reasons.append({
            "type": "platform",
            "text": f"Plays on {pf_overlap[0]}, one of your platforms",
        })

    # --- 5. Quality / reception — broken down by factor when one stands out
    factor_scores = {k: (game.get(field) or 0) for k, (field, _) in FACTOR_FIELDS.items()}
    nonzero       = {k: v for k, v in factor_scores.items() if v > 0}
    review_total  = game.get("reviewTotal") or 0
    igdb_rating   = game.get("igdbRating")  or 0

    if nonzero and review_total > 0:
        avg      = sum(nonzero.values()) / len(nonzero)
        standout = max(nonzero.items(), key=lambda kv: kv[1])
        s_key, s_score = standout
        _, s_label = FACTOR_FIELDS[s_key]

        # Standout factor — significantly above the rest
        if s_score >= 8.5 and (s_score - avg) >= 0.7:
            # If the user weighted this factor heavily, emphasise the alignment
            user_w = user_weights.get(s_key, 5)
            if user_w >= 7:
                reasons.append({
                    "type": "quality",
                    "text": f"Excels at {s_label} ({_fmt_score(s_score)}/10) — a factor you prioritise",
                })
            else:
                reasons.append({
                    "type": "quality",
                    "text": f"Particularly strong {s_label} ({_fmt_score(s_score)}/10)",
                })
        elif avg >= 8.0:
            reasons.append({
                "type": "quality",
                "text": f"Highly rated across the board ({_fmt_score(avg)}/10 over {review_total} review{'s' if review_total != 1 else ''})",
            })
        elif avg >= 7.0:
            reasons.append({
                "type": "quality",
                "text": f"Well-received ({_fmt_score(avg)}/10 from {review_total} review{'s' if review_total != 1 else ''})",
            })

        # Second quality reason — call out a weighted-factor alignment that
        # *isn't* the standout (helps when the standout reason already fired)
        weighted_factors = [
            k for k, w in user_weights.items()
            if k in FACTOR_FIELDS and w >= 7 and k != s_key
        ]
        for k in weighted_factors:
            score = factor_scores.get(k, 0)
            if score >= 7.5:
                _, label = FACTOR_FIELDS[k]
                reasons.append({
                    "type": "quality",
                    "text": f"Solid {label} ({_fmt_score(score)}/10), aligned with your weights",
                })
                break  # at most one extra quality reason

    elif review_total == 0:
        if igdb_rating >= 80:
            reasons.append({
                "type": "quality",
                "text": f"Critically acclaimed ({int(igdb_rating)}/100 on IGDB)",
            })
        elif igdb_rating >= 70:
            reasons.append({
                "type": "quality",
                "text": f"Positively received ({int(igdb_rating)}/100 on IGDB)",
            })

    # --- 6. Recency
    r = _recency_score(game.get("releaseDate"))
    if r > 0.85:
        reasons.append({"type": "recency", "text": "A recent release"})
    elif r > 0.6:
        reasons.append({"type": "recency", "text": "From the last few years"})

    # --- 7. Popularity fallback — only if nothing else fired
    if not reasons and review_total >= 5:
        reasons.append({
            "type": "popularity",
            "text": f"Popular with the Warpstar community ({review_total} reviews)",
        })

    # Cap at 4 reasons so the popup doesn't sprawl
    return reasons[:4]


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
    disliked_ids:    set[str]               = set(),
    liked_genres:    set[str]               = set(),
    liked_devs:      set[str]               = set(),
    disliked_genres: set[str]               = set(),
    disliked_devs:   set[str]               = set(),
) -> float:
    """
    Score a single game for a user. Returns 0.0–100.0.

    Parameters
    ----------
    game            : MongoDB game document (serialised to dict)
    user            : MongoDB user document (serialised to dict)
    user_context    : output of build_user_context()
    weights         : 0–10 per-key weights; falls back to user prefs then defaults
    reviewed_ids    : set of game IDs the user has already reviewed
    favorited_ids   : set of game IDs the user has favorited
    disliked_ids    : set of game IDs the user has thumbs-downed
    liked_genres    : set of genres (lowercased) the user has thumbs-upped
    liked_devs      : set of developers (lowercased) the user has thumbs-upped
    disliked_genres : set of genres (lowercased) the user has thumbs-downed
    disliked_devs   : set of developers (lowercased) the user has thumbs-downed
    """
    game_id = str(game.get("id") or game.get("_id") or "")

    # Already seen — suppress hard
    if game_id in reviewed_ids:
        return 0.0
    if game_id in disliked_ids:
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

    # 6. Explicit feedback signal (additive, -0.25 to +0.15)
    fsig = _feedback_signal(
        game,
        liked_genres, liked_devs,
        disliked_genres, disliked_devs,
    )

    # 7. Quality floor — penalise very low scored games
    review_total = game.get("reviewTotal") or 0
    if has_warpstar and f_score < 0.4:   # avg < 4.0 on Warpstar
        raw *= 0.5
    elif not has_warpstar and (game.get("igdbRating") or 0) < 40:
        raw *= 0.7

    # 8. Confidence boost for games with more reviews (log scale)
    if review_total > 1:
        confidence = min(math.log10(review_total) / 2.0, 0.1)
        raw += confidence

    final = max(min((raw + boost + fsig) * 100, 100.0), 0.0)
    return round(final, 2)


# ---------------------------------------------------------------------------
# Batch scoring
# ---------------------------------------------------------------------------

def rank_games(
    games:          list[dict[str, Any]],
    user:           dict[str, Any],
    user_reviews:   list[dict[str, Any]],
    feedback:       list[dict[str, Any]] | None = None,
    weights:        dict[str, float] | None = None,
    limit:          int = 20,
) -> list[dict[str, Any]]:
    """
    Score and rank a list of games for a user.
    Returns the top `limit` games sorted by score descending,
    with a `_score` field added to each game dict.

    feedback: list of {gameId, type ("up"|"down"), genres, developers}
              from the user's thumbs feedback on prior recommendations.
    """
    reviewed_ids  = {str(r.get("gameId") or "") for r in user_reviews}
    favorited_ids = {str(f) for f in (user.get("favoriteGames") or [])}
    user_context  = build_user_context(user_reviews)

    # Derive sets from explicit feedback
    feedback = feedback or []
    disliked_ids:    set[str] = set()
    liked_genres:    set[str] = set()
    liked_devs:      set[str] = set()
    disliked_genres: set[str] = set()
    disliked_devs:   set[str] = set()
    for f in feedback:
        gs = {g.lower() for g in (f.get("genres")     or [])}
        ds = {d.lower() for d in (f.get("developers") or [])}
        if f.get("type") == "up":
            liked_genres    |= gs
            liked_devs      |= ds
        elif f.get("type") == "down":
            disliked_ids.add(str(f.get("gameId") or ""))
            disliked_genres |= gs
            disliked_devs   |= ds

    scored = []
    for game in games:
        s = score_game(
            game            = game,
            user            = user,
            user_context    = user_context,
            weights         = weights,
            reviewed_ids    = reviewed_ids,
            favorited_ids   = favorited_ids,
            disliked_ids    = disliked_ids,
            liked_genres    = liked_genres,
            liked_devs      = liked_devs,
            disliked_genres = disliked_genres,
            disliked_devs   = disliked_devs,
        )
        if s > 0:
            scored.append({**game, "_score": s})

    scored.sort(key=lambda g: g["_score"], reverse=True)
    top = scored[:limit]

    # Attach reasons only to the games we'll actually return
    for g in top:
        g["_reasons"] = _generate_reasons(
            game         = g,
            user         = user,
            user_context = user_context,
            liked_genres = liked_genres,
            liked_devs   = liked_devs,
        )

    return top
