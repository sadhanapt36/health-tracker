"""Plain data types shared between modules. No logic, no I/O."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RawMessage:
    """One message exactly as collected, before any health parsing."""
    source_message_id: Optional[str]  # WhatsApp's data-id attribute, if present
    sender: Optional[str]
    sent_at_raw: Optional[str]        # timestamp text as WhatsApp displayed it
    sent_at: Optional[str]            # ISO string if we could parse it, else None
    text: str
    has_image: bool
    attachment: Optional[str] = None  # media file name from a chat export


@dataclass
class DraftReading:
    """Suggested values pulled from a message. Nothing here is saved or trusted
    until a person reviews it. None means "not found / not sure"."""
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    pulse: Optional[int] = None
    weight: Optional[float] = None
    weight_unit: Optional[str] = None      # 'kg' or 'lb', only if the message said so
    glucose: Optional[float] = None
    glucose_unit: Optional[str] = None     # 'mg/dL' or 'mmol/L', only if stated
    notes: list = field(default_factory=list)   # plain-language hints for the reviewer

    def has_values(self) -> bool:
        return any(v is not None for v in (self.systolic, self.diastolic, self.pulse,
                                           self.weight, self.glucose))
