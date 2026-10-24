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


class CompanyHint(BaseModel):
    """The current company as the extension read it on the profile page."""

    name: str = Field("", max_length=300)
    # From a linkedin.com/company/<slug>/ link on the page: a cache key, never fetched.
    linkedin_slug: str | None = Field(None, max_length=200)


class ProfilePayload(BaseModel):
    url: str
    name: str = Field("", max_length=300)
    headline: str = Field("", max_length=1000)
    raw_text: str = ""
    company: CompanyHint | None = None

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
    # Typed in the panel when the detected company is wrong.
    company_override: str = Field("", max_length=300)


class ResearchRequest(BaseModel):
    profile: ProfilePayload
    company_override: str = Field("", max_length=300)
    # Search again even if the cache is fresh or a failure is backing off (never overwrites your manual edits).
    force: bool = False


class CompanyFact(BaseModel):
    category: str
    fact: str
    source_url: str
    date: str | None = None


class CompanyOut(BaseModel):
    id: int
    name: str
    website: str | None
    linkedin_slug: str | None
    status: str
    facts: list[CompanyFact]
    researched_at: datetime | None
    age_days: int | None  # since the last research (or your edit)
    fresh: bool
    manually_edited: bool
    edited_at: datetime | None
    last_error: str | None
    next_retry_at: datetime | None
    web_searches: int | None
    rejected_facts: int


class ResearchResponse(BaseModel):
    company: CompanyOut | None
    used: bool  # whether drafts get it in <company_research>
    researched: bool  # a web search ran for this request
    error: str | None
    notice: str | None


class CompanyFactIn(BaseModel):
    category: str = "domain"
    fact: str = ""
    source_url: str = ""
    date: str | None = None


class CompanyEdit(BaseModel):
    website: str | None = None
    facts: list[CompanyFactIn]


class CompanyRefresh(BaseModel):
    # Required to replace research you edited by hand.
    discard_edits: bool = False


class FactUsed(BaseModel):
    fact: str
    source: str
    kind: Literal["url", "profile", "my_context", "invalid"]
    verified: bool


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
    strategy: str | None
    strategy_reason: str
    cv_match: str
    facts_used: list[FactUsed]
    company: CompanyOut | None
    research_used: bool
    research_note: str | None


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
    strategy: str | None
    strategy_reason: str | None
    cv_match: str | None
    facts_used: list[FactUsed] | None
    company_id: int | None
    research_used: bool | None
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


class CVImportOut(BaseModel):
    fields: ContextIn
    text: str
    method: str
    ocr_pages: int
    warnings: list[str]


class ModelSettings(BaseModel):
    model: str
    effort: str  # "" = no effort field is sent
    efforts: list[str]
    from_env: bool  # true until the effort is changed in the dashboard


class ModelSettingsIn(BaseModel):
    effort: str = ""
