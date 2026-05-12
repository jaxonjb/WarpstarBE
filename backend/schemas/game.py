from pydantic import BaseModel
from datetime import datetime


class GamePublic(BaseModel):
    id: str
    name: str
    summary: str | None = None
    coverUrl: str | None = None
    releaseDate: datetime | None = None
    igdbRating: float | None = None
    gameplayAvg: float = 0
    contentAvg: float = 0
    narrativeAvg: float = 0
    aestheticsAvg: float = 0
    polishAvg: float = 0
    reviewTotal: int = 0
    platformIds: list[str] = []
    genreIds: list[str] = []
    themeIds: list[str] = []
    keywordIds: list[str] = []
    companyIds: list[str] = []
    similarTo: list[str] = []


class GameSearchParams(BaseModel):
    q: str | None = None
    genre: str | None = None
    platform: str | None = None
    theme: str | None = None
    skip: int = 0
    limit: int = 20
