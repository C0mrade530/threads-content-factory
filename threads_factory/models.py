from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
import re

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(Model):
    name: str = Field(min_length=1, max_length=120)
    niche: str = Field(min_length=3, max_length=1000)
    audience: str = Field(min_length=3, max_length=2000)
    offer: str = Field(default="", max_length=1500)
    goal: str = Field(default="Полезные посты и узнаваемость", max_length=1000)
    voice: str = Field(default="Разговорно, конкретно, без канцелярита", max_length=1500)
    facts: list[str] = Field(default_factory=list, max_length=50)
    topics: list[str] = Field(min_length=1, max_length=30)
    forbidden: list[str] = Field(default_factory=list, max_length=30)
    cta: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_items(self):
        if any(not s.strip() or len(s) > 1000 for s in self.facts + self.topics + self.forbidden):
            raise ValueError("Пункты профиля должны содержать от 1 до 1000 символов")
        return self


class Schedule(Model):
    enabled: StrictBool = False
    posts_per_day: StrictInt = Field(default=3, ge=1, le=24)
    timezone: str = "Europe/Moscow"
    times: list[str] = Field(default_factory=list, max_length=24)
    autonomous_generation: StrictBool = False

    @model_validator(mode="after")
    def validate_schedule(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, KeyError):
            raise ValueError("Неизвестный часовой пояс") from None
        if self.times and (len(self.times) != self.posts_per_day or len(set(self.times)) != len(self.times)
                or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in self.times)):
            raise ValueError("Укажите уникальное время для каждого поста")
        return self

    def slots(self, now: datetime, days=3):
        tz = ZoneInfo(self.timezone)
        times = self.times or (["12:00"] if self.posts_per_day == 1 else [
            f"{m//60:02d}:{m%60:02d}" for m in
            [540 + i * 720 // (self.posts_per_day - 1) for i in range(self.posts_per_day)]])
        result = []
        for offset in range(days):
            date = now.astimezone(tz).date() + timedelta(days=offset)
            for value in sorted(times):
                hour, minute = map(int, value.split(":"))
                local = datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz, fold=0)
                utc = local.astimezone(UTC)
                if utc.astimezone(tz).replace(tzinfo=None) != local.replace(tzinfo=None):
                    continue
                if utc >= now - timedelta(minutes=30):
                    result.append(utc)
        return sorted(set(result))


class ScheduleChange(Schedule):
    confirmed: StrictBool = False


class Binding(Model):
    confirmed: StrictBool
    account_id: str = Field(min_length=1, max_length=200)


class Batch(Model):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    posts: list[str] = Field(min_length=1, max_length=50)
    approved: StrictBool

    @model_validator(mode="after")
    def check_posts(self):
        if any(not 10 <= len(p.strip()) <= 5000 for p in self.posts):
            raise ValueError("Текст поста: 10–5000 символов")
        return self
