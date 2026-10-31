from agent.diagnose import DEFAULT_MIN_VISION_CONFIDENCE, Diagnosis, Verdict, diagnose


def test_pass() -> None:
    assert diagnose(
        spec_matches_tags=True, tags_match_camera=True, vision_confidence=0.9
    ) == Diagnosis(Verdict.PASS, "record evidence")


def test_logic_bug() -> None:
    assert diagnose(
        spec_matches_tags=False, tags_match_camera=True, vision_confidence=0.9
    ) == Diagnosis(Verdict.LOGIC_BUG, "fix the ST code and retry")


def test_physical_fault() -> None:
    assert diagnose(
        spec_matches_tags=True, tags_match_camera=False, vision_confidence=0.9
    ) == Diagnosis(Verdict.PHYSICAL_FAULT, "stop; report the address and the observed state")


def test_unclear() -> None:
    assert diagnose(
        spec_matches_tags=False, tags_match_camera=False, vision_confidence=0.9
    ) == Diagnosis(Verdict.UNCLEAR, "stop; ask the human; never loop")


def test_low_confidence_always_inconclusive() -> None:
    for spec_ok in (True, False):
        for tags_ok in (True, False):
            result = diagnose(
                spec_matches_tags=spec_ok,
                tags_match_camera=tags_ok,
                vision_confidence=0.1,
            )
            assert result == Diagnosis(Verdict.VISION_INCONCLUSIVE, "re-capture once, then report")


def test_confidence_at_threshold_counts_as_confident() -> None:
    result = diagnose(
        spec_matches_tags=True,
        tags_match_camera=True,
        vision_confidence=DEFAULT_MIN_VISION_CONFIDENCE,
    )
    assert result.verdict is Verdict.PASS


def test_custom_threshold() -> None:
    result = diagnose(
        spec_matches_tags=True,
        tags_match_camera=True,
        vision_confidence=0.8,
        min_confidence=0.9,
    )
    assert result.verdict is Verdict.VISION_INCONCLUSIVE
