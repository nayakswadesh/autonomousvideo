from autonomous_studio.domain.models import LongFormChapter, NarrationBlock
from scripts.replay_longform_audio import alignment_errors


def test_targeted_replay_requires_each_approved_spoken_unit():
    chapter = LongFormChapter(
        id="chapter-1",
        order=1,
        title="Example",
        purpose="Test speech alignment",
        question="What was said?",
        reveal="Every unit must be audible",
        target_duration=8.0,
        narration_blocks=[
            NarrationBlock(
                id="block-1",
                text="One clue survives; but it dates early solids, not the star's birthday.",
                estimated_duration=8.0,
            )
        ],
    )
    complete = [
        {"word": word, "start": i * 0.3, "end": (i + 1) * 0.3}
        for i, word in enumerate(
            "One clue survives but it dates early solids not the star's birthday".split()
        )
    ]
    omitted = [
        {"word": word, "start": i * 0.3, "end": (i + 1) * 0.3}
        for i, word in enumerate("One clue survives not the star's birthday".split())
    ]
    assert alignment_errors(chapter, "block-1", complete) == []
    assert any("block-1_u2" in error for error in alignment_errors(chapter, "block-1", omitted))
