from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime, timezone
from jose import JWTError, jwt
from bson import ObjectId
from pydantic import BaseModel
import httpx
import re

from core.database import get_db
from core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    get_settings,
)
from core.utils import serialize_doc
from schemas.user import RegisterRequest, LoginRequest, TokenResponse, RefreshRequest

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
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
    import os
    import traceback
    print(f"DEBUG client_id: '{settings.google_client_id}'")
    print(f"DEBUG mongodb_uri starts with: '{settings.mongodb_uri[:20] if settings.mongodb_uri else 'EMPTY'}'")
    client_id = os.getenv("VITE_GOOGLE_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="Google client ID not configured.")

    try:
        id_info = id_token.verify_oauth2_token(
            body.credential,
            google_requests.Request(),
            client_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {e}")
    except Exception as e:
        print(f"ERROR verifying token: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Token verification error: {type(e).__name__}: {e}")

    email     = id_info.get("email")
    name      = id_info.get("name", "")
    picture   = id_info.get("picture", "")
    google_id = id_info.get("sub", "")

    if not email:
        raise HTTPException(status_code=400, detail="Could not retrieve email from Google.")

    try:
        user = await db.users.find_one({"email": email})

        if user:
            prefs           = user.get("preferences", {})
            current_pic     = prefs.get("profilePicture", "")
            google_pic      = prefs.get("googleAvatar", "")

            # Only update the profile picture if the user is still using their
            # original Google photo (i.e. hasn't set a custom avatar).
            # If they changed it during onboarding or settings, leave it alone.
            still_using_google = (not current_pic) or (current_pic == google_pic)
            if picture and still_using_google and current_pic != picture:
                await db.users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {
                        "preferences.profilePicture": picture,
                        "preferences.googleAvatar":   picture,
                    }},
                )
            user_id = str(user["_id"])
        else:
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
    except Exception as e:
        print(f"ERROR in DB operations: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Database error: {type(e).__name__}: {e}")

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