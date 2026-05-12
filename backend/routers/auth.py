from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime, timezone
from jose import JWTError, jwt
from bson import ObjectId
from pydantic import BaseModel
import httpx
import re

from ..core.database import get_db
from ..core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    get_settings,
)
from ..core.utils import serialize_doc
from ..schemas.user import RegisterRequest, LoginRequest, TokenResponse, RefreshRequest

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


class GoogleLoginRequest(BaseModel):
    credential: str  # Google access token from the frontend


def _slugify_username(name: str) -> str:
    """Turn a Google display name into a valid username."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w]", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:28] or "user"


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db=Depends(get_db)):
    if await db.users.find_one({"email": body.email}):
        raise HTTPException(status_code=409, detail="Email already registered.")
    if await db.users.find_one({"username": body.username}):
        raise HTTPException(status_code=409, detail="Username already taken.")

    now = datetime.now(timezone.utc)
    doc = {
        "username":      body.username,
        "email":         body.email,
        "passwordHash":  hash_password(body.password),
        "favoriteGames": [],
        "followers":     [],
        "following":     [],
        "preferences":   {},
        "createdAt":     now,
    }
    result  = await db.users.insert_one(doc)
    user_id = str(result.inserted_id)

    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db=Depends(get_db)):
    user = await db.users.find_one({"email": body.email})
    if not user or not verify_password(body.password, user.get("passwordHash", "")):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    user_id = str(user["_id"])
    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@router.post("/google", response_model=TokenResponse)
async def google_login(body: GoogleLoginRequest, db=Depends(get_db)):
    """
    Accepts a Google OAuth access token from the frontend,
    fetches the user's profile from Google, then finds or creates
    the user in our DB and returns our own JWT pair.
    """
    # Verify the token by fetching the user's Google profile
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v1/userinfo",
            headers={"Authorization": f"Bearer {body.credential}"},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token.")

    google_user = resp.json()
    email       = google_user.get("email")
    name        = google_user.get("name", "")
    picture     = google_user.get("picture", "")
    google_id   = google_user.get("id", "")

    if not email:
        raise HTTPException(status_code=400, detail="Could not retrieve email from Google.")

    # Find existing user by email
    user = await db.users.find_one({"email": email})

    if user:
        # Update profile picture if changed
        if picture and user.get("preferences", {}).get("profilePicture") != picture:
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"preferences.profilePicture": picture}},
            )
        user_id = str(user["_id"])
    else:
        # Create new user — generate a unique username from their Google name
        base_username = _slugify_username(name)
        username      = base_username
        suffix        = 1
        while await db.users.find_one({"username": username}):
            username = f"{base_username}_{suffix}"
            suffix  += 1

        now = datetime.now(timezone.utc)
        doc = {
            "username":      username,
            "email":         email,
            "googleId":      google_id,
            "favoriteGames": [],
            "followers":     [],
            "following":     [],
            "preferences":   {
                "displayName":    name,
                "profilePicture": picture,
            },
            "createdAt": now,
        }
        result  = await db.users.insert_one(doc)
        user_id = str(result.inserted_id)

    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db=Depends(get_db)):
    credentials_exc = HTTPException(status_code=401, detail="Invalid refresh token.")
    try:
        payload    = jwt.decode(body.refresh_token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        user_id    = payload.get("sub")
        token_type = payload.get("type")
        if not user_id or token_type != "refresh":
            raise credentials_exc
    except JWTError:
        raise credentials_exc

    if not await db.users.find_one({"_id": ObjectId(user_id)}):
        raise credentials_exc

    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )