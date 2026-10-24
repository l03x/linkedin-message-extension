import re
from datetime import datetime
from typing import Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_RAW_TEXT = 20_000

_PROFILE_URL = re.compile(r"^https://(?:www\.)?linkedin\.com/in/([^/?#]+)", re.I)


def canonical_profile_url(url: str) -> str:
    m = _PROFILE_URL.match(url.strip())
    if m is None:
        raise ValueError("Not a LinkedIn profile URL (expected https://www.linkedin.com/in/<slug>/).")
    return f"https://www.linkedin.com/in/{unquote(m.group(1)).lower()}/"


class ProfilePayload(BaseModel):
    url: str
    name: str = Field("", max_length=300)
    headline: str = Field("", max_length=1000)
    raw_text: str = ""

    @field_validator("url")
    @classmethod
    def _canonical(cls, v: str) -> str:
        return canonical_profile_url(v)

    @field_validator("raw_text")
    @classmethod
    def _cap(cls, v: str) -> str:
        return v[:MAX_RAW_TEXT]


class GenerateRequest(BaseModel):
    profile: ProfilePayload
    mode: str
    language: str = "en"
    extra_instruction: str = Field("", max_length=1000)


class PreviousContact(BaseModel):
    generation_id: int
    mode: str
    status: str
    sent_at: datetime | None


class GenerateResponse(BaseModel):
    generation_id: int
    mode: str
    language: str
    subject: str | None
    body: str
    limits: dict[str, int]
    over_limit: bool
    over: dict[str, list[int]]
    attempts: int
    prompt_version: dict
    warnings: list[str]
    previous: list[PreviousContact]


class SentRequest(BaseModel):
    final_text: str = Field(min_length=1)
    final_subject: str | None = None


class StatusRequest(BaseModel):
    status: Literal["sent", "replied", "no_reply"]
    replied_at: datetime | None = None


class GenerationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    mode: str
    language: str
    extra_instruction: str
    prompt_version_id: int
    user_context_id: int | None
    model: str
    draft_subject: str | None
    draft_text: str
    final_subject: str | None
    final_text: str | None
    over_limit: bool
    attempts: int
    status: str
    generated_at: datetime
    sent_at: datetime | None
    replied_at: datetime | None


class PromptCreate(BaseModel):
    name: str = Field("", max_length=200)
    system_prompt: str = Field(min_length=1)
    activate: bool = True


class PromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    system_prompt: str
    created_at: datetime
    is_active: bool


class ContextIn(BaseModel):
    background: str = ""
    target_role: str = ""
    target_stack: str = ""
    availability: str = ""
    location: str = ""
    notes: str = ""


class ContextOut(ContextIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_placeholder: bool
    created_at: datetime
