from pydantic import BaseModel, Field
from datetime import datetime


class ReviewCreate(BaseModel):
    gameplay: float = Field(..., ge=0, le=10)
    content: float = Field(..., ge=0, le=10)
    narrative: float = Field(..., ge=0, le=10)
    aesthetics: float = Field(..., ge=0, le=10)
    polish: float = Field(..., ge=0, le=10)
    title: str = Field(..., min_length=1, max_length=200)
    body: str | None = None
    gp_body: str | None = None
    con_body: str | None = None
    ntv_body: str | None = None
    aes_body: str | None = None
    pol_body: str | None = None
    containsSpoilers: bool = False


class ReviewUpdate(BaseModel):
    gameplay: float | None = Field(None, ge=0, le=10)
    content: float | None = Field(None, ge=0, le=10)
    narrative: float | None = Field(None, ge=0, le=10)
    aesthetics: float | None = Field(None, ge=0, le=10)
    polish: float | None = Field(None, ge=0, le=10)
    title: str | None = Field(None, min_length=1, max_length=200)
    body: str | None = None
    gp_body: str | None = None
    con_body: str | None = None
    ntv_body: str | None = None
    aes_body: str | None = None
    pol_body: str | None = None
    containsSpoilers: bool | None = None


class ReviewPublic(BaseModel):
    id: str
    userId: str
    gameId: str
    gameplay: float
    content: float
    narrative: float
    aesthetics: float
    polish: float
    overallScore: float
    title: str
    body: str | None = None
    gp_body: str | None = None
    con_body: str | None = None
    ntv_body: str | None = None
    aes_body: str | None = None
    pol_body: str | None = None
    containsSpoilers: bool
    likes: int = 0
    commentsCount: int = 0
    createdAt: datetime
