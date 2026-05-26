from pydantic import BaseModel, EmailStr, Field
from typing import Any
from datetime import datetime


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    email: EmailStr
    password: str = Field(..., min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserPublic(BaseModel):
    id: str
    username: str
    createdAt: datetime
    favoriteGames: list[str] = []
    followers: list[str] = []
    following: list[str] = []


class UserUpdate(BaseModel):
    username:           str | None             = Field(None, min_length=3, max_length=20, pattern=r'^[a-z0-9_]+$')
    preferences:        dict[str, Any] | None = None
    onboardingComplete: bool | None           = None


class FollowResponse(BaseModel):
    following: bool
    follower_count: int