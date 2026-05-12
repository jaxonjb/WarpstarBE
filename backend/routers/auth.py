from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime, timezone
from jose import JWTError, jwt
from bson import ObjectId

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


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db=Depends(get_db)):
    # Check uniqueness
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
    result = await db.users.insert_one(doc)
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


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db=Depends(get_db)):
    credentials_exc = HTTPException(status_code=401, detail="Invalid refresh token.")
    try:
        payload = jwt.decode(
            body.refresh_token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: str = payload.get("sub")
        token_type: str = payload.get("type")
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
