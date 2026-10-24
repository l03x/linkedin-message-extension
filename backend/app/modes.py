"""Message modes and output languages. Add a language here and the extension picks it up via /api/options.

The keys are what the generation contract sends in <mode> and <language>; how to write for each is up to the
system prompt."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    # Hard character limits per field ("subject", "body"), counted in Unicode code points.
    limits: dict[str, int]
    # Soft length target shown in the panel (None = just the hard limit).
    target_chars: int | None = None
    # Compact label for chips and segmented controls.
    short_label: str = ""

    @property
    def has_subject(self) -> bool:
        return "subject" in self.limits


@dataclass(frozen=True)
class Language:
    key: str
    label: str
    rtl: bool = False
    short_label: str = ""


MODES: dict[str, Mode] = {
    m.key: m
    for m in [
        Mode(key="connection_note", label="Connection note", short_label="Note", limits={"body": 300}),
        Mode(key="message", label="Message (already connected)", short_label="Message", limits={"body": 8000}, target_chars=600),
        Mode(key="inmail", label="InMail", short_label="InMail", limits={"subject": 200, "body": 1900}, target_chars=900),
    ]
}

LANGUAGES: dict[str, Language] = {
    lang.key: lang
    for lang in [
        Language(key="en", label="English", short_label="EN"),
        Language(key="fr", label="Français", short_label="FR"),
        Language(key="ar", label="العربية (فصحى)", short_label="عربي", rtl=True),
    ]
}


def options_payload() -> dict:
    return {
        "modes": [
            {"key": m.key, "label": m.label, "short_label": m.short_label, "limits": m.limits, "target_chars": m.target_chars}
            for m in MODES.values()
        ],
        "languages": [
            {"key": lang.key, "label": lang.label, "short_label": lang.short_label, "rtl": lang.rtl}
            for lang in LANGUAGES.values()
        ],
    }
