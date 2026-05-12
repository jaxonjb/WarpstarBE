from pydantic import BaseModel, Field
from datetime import datetime


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

class ListCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    isPublic: bool = True


class ListUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    isPublic: bool | None = None


class ListPublic(BaseModel):
    id: str
    userId: str
    title: str
    description: str | None = None
    gameIds: list[str] = []
    isPublic: bool
    createdAt: datetime


class ListAddGame(BaseModel):
    gameId: str


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

class CommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)


class CommentPublic(BaseModel):
    id: str
    userId: str
    parentId: str
    parentType: str
    content: str
    createdAt: datetime
