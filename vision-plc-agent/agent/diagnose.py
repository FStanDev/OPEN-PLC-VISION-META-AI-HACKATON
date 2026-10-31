"""Three-way diagnosis: spec vs PLC tags vs camera (see ``docs/SPEC.md``)."""

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_MIN_VISION_CONFIDENCE = 0.5


class Verdict(StrEnum):
    PASS = "pass"
    LOGIC_BUG = "logic_bug"
    PHYSICAL_FAULT = "physical_fault"
    UNCLEAR = "unclear"
    VISION_INCONCLUSIVE = "vision_inconclusive"


@dataclass(frozen=True)
class Diagnosis:
    verdict: Verdict
    action: str


def diagnose(
    *,
    spec_matches_tags: bool,
    tags_match_camera: bool,
    vision_confidence: float,
    min_confidence: float = DEFAULT_MIN_VISION_CONFIDENCE,
) -> Diagnosis:
    """Classify one expectation per the SPEC diagnosis table."""
    if vision_confidence < min_confidence:
        return Diagnosis(Verdict.VISION_INCONCLUSIVE, "re-capture once, then report")
    if spec_matches_tags and tags_match_camera:
        return Diagnosis(Verdict.PASS, "record evidence")
    if not spec_matches_tags and tags_match_camera:
        return Diagnosis(Verdict.LOGIC_BUG, "fix the ST code and retry")
    if spec_matches_tags and not tags_match_camera:
        return Diagnosis(Verdict.PHYSICAL_FAULT, "stop; report the address and the observed state")
    return Diagnosis(Verdict.UNCLEAR, "stop; ask the human; never loop")
