from datetime import datetime

from pydantic import BaseModel


class AnalyticsSummaryResponse(BaseModel):
    open_count: int
    avg_resolution_time: float | None
    top_categories: dict[str, int]
    total_items: int


class TrendPoint(BaseModel):
    date: datetime
    count: int


class TrendResponse(BaseModel):
    category: str
    daily: list[TrendPoint]
    weekly: list[TrendPoint]
