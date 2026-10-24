"""Message modes and output languages. Add a language here and the extension picks it up via /api/options."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    # Hard character limits per field ("subject", "body"), counted in Unicode code points.
    limits: dict[str, int]
    # What the message should ask for; goes into the task block.
    ask: str
    # Soft length target stated to the model (None = just the hard limit).
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
    instruction: str
    rtl: bool = False
    short_label: str = ""
    # Accepted labels for the InMail subject line, besides "Subject".
    subject_labels: tuple[str, ...] = field(default_factory=tuple)


MODES: dict[str, Mode] = {
    m.key: m
    for m in [
        Mode(
            key="connection_note",
            label="Connection note",
            short_label="Note",
            limits={"body": 300},
            ask="A connection request note. Give one honest reason to connect. Do not ask for a job, a call or a referral.",
        ),
        Mode(
            key="message",
            label="Message (already connected)",
            short_label="Message",
            limits={"body": 8000},
            target_chars=600,
            ask=(
                "A direct message to someone I am already connected with. Make one internship-related, "
                "low-friction ask, e.g. whether their team takes interns, who the right person to ask is, "
                "or a short 15-minute chat."
            ),
        ),
        Mode(
            key="inmail",
            label="InMail",
            short_label="InMail",
            limits={"subject": 200, "body": 1900},
            target_chars=900,
            ask=(
                "An InMail to someone I am not connected with. Introduce me in one or two sentences, "
                "give the hook, then make one internship-related, low-friction ask. "
                "The subject must be specific and plain, not clickbait."
            ),
        ),
    ]
}

LANGUAGES: dict[str, Language] = {
    lang.key: lang
    for lang in [
        Language(
            key="en",
            label="English",
            short_label="EN",
            instruction="Write in natural, plain English.",
        ),
        Language(
            key="fr",
            label="Français",
            short_label="FR",
            instruction=(
                "Write entirely in natural French as a French-speaking student would. "
                "Use vouvoiement (vous) unless the extra instruction says otherwise. "
                "Do not translate English idioms literally."
            ),
            subject_labels=("Objet", "Sujet"),
        ),
        Language(
            key="ar",
            label="العربية (فصحى)",
            short_label="عربي",
            instruction=(
                "Write entirely in Modern Standard Arabic (الفصحى), not a dialect. "
                "Use a natural, respectful register with native phrasing; do not translate English idioms literally. "
                "Keep names of technologies and companies in their usual Latin spelling."
            ),
            rtl=True,
            subject_labels=("الموضوع",),
        ),
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
