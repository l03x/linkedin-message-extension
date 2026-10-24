"""Work out the recipient's current company from what the extension read on their profile page, and find or
create its cache row. Nothing here touches the network: a LinkedIn company slug is only used as a key."""

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company

# Legal-form and filler words dropped from the cache key, so "Acme SARL" and "ACME" share one row.
_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "llp", "plc",
    "gmbh", "ag", "sa", "sas", "sarl", "sarlau", "sasu", "spa", "srl", "bv", "nv", "ab", "oy", "as",
    "group", "groupe", "holding", "holdings",
}  # fmt: skip
# Values that mean "no employer to research".
_NOT_A_COMPANY = {
    "self employed", "selfemployed", "freelance", "freelancer", "independent", "independant", "student",
    "etudiant", "etudiante", "stealth", "stealth startup", "confidential", "unemployed", "retired",
    "looking for opportunities", "open to work",
}  # fmt: skip

# "Backend engineer at Acme", "Ingénieure chez Acme", "SWE @ Acme", "Développeur à Acme", "مهندس لدى أكمي".
_HEADLINE = re.compile(r"(?:\s(?:at|chez|à|لدى)\s|\s?@\s?)(?P<company>[^|·•,;()\n]+)", re.I)
# The first entry of the Experience section: "Experience\n<title>\n<company> · Full-time\n...".
_EXPERIENCE = re.compile(r"^(?:Experience|Expérience|الخبرة)\n[^\n]+\n(?P<company>[^\n·]+?)(?:\s·[^\n]*)?$", re.M)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,199}$")


@dataclass
class CompanyRef:
    name: str
    linkedin_slug: str | None = None
    source: str = ""  # "override" | "page" | "headline" | "experience"


def normalize_name(name: str) -> str:
    """Case-, accent- and punctuation-insensitive key without legal suffixes. "" when nothing is left."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    words = re.sub(r"[^\w]+", " ", text.replace(".", "")).split()  # "S.A." → "sa"
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def _clean(name: str | None) -> str:
    return re.sub(r"\s+", " ", name or "").strip(" -–—|·•,.")[:300]


def _usable(name: str) -> bool:
    key = normalize_name(name)
    return bool(key) and key not in _NOT_A_COMPANY and len(key) <= 200


def _slug(value: str | None) -> str | None:
    value = (value or "").strip().strip("/").lower()
    return value if _SLUG.match(value) else None


def identify(headline: str, raw_text: str, hint: dict | None = None, override: str = "") -> CompanyRef | None:
    """In order: what you typed in the panel, what the page shows as current company, the headline, then the
    first Experience entry. None when nothing looks like an employer."""
    if _usable(_clean(override)):
        return CompanyRef(_clean(override), source="override")
    hint = hint or {}
    name, slug = _clean(hint.get("name")), _slug(hint.get("linkedin_slug"))
    if _usable(name):
        return CompanyRef(name, slug, source="page")
    m = _HEADLINE.search(headline or "")
    if m and _usable(_clean(m.group("company"))):
        return CompanyRef(_clean(m.group("company")), slug, source="headline")
    m = _EXPERIENCE.search(raw_text or "")
    if m and _usable(_clean(m.group("company"))):
        return CompanyRef(_clean(m.group("company")), slug, source="experience")
    return None


def find_or_create(db: Session, ref: CompanyRef) -> Company:
    """By LinkedIn slug first (stable across spellings), then by normalised name. Flushes, doesn't commit."""
    key = normalize_name(ref.name)
    company = None
    if ref.linkedin_slug:
        company = db.scalar(select(Company).where(Company.linkedin_slug == ref.linkedin_slug))
    if company is None:
        company = db.scalar(select(Company).where(Company.name_key == key))
        if company is not None and ref.linkedin_slug and company.linkedin_slug is None:
            company.linkedin_slug = ref.linkedin_slug
    if company is None:
        company = Company(name=ref.name, name_key=key, linkedin_slug=ref.linkedin_slug, status="pending", facts=[])
        try:
            with db.begin_nested():  # two first-time requests for one company: the loser re-reads the winner's row
                db.add(company)
        except IntegrityError:
            company = db.scalar(select(Company).where(Company.name_key == key))
            if company is None and ref.linkedin_slug:
                company = db.scalar(select(Company).where(Company.linkedin_slug == ref.linkedin_slug))
            if company is None:
                raise
    db.flush()
    return company
