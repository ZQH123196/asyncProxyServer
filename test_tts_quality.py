import pytest

from tts_quality import assess_tts_audio_duration, generate_with_quality_retry


@pytest.mark.parametrize(
    ("duration_sec", "expected_passed", "expected_label"),
    [
        (1.0, False, "挤碎"),
        (22.2, True, "正常"),
        (100.0, False, "拉伸"),
    ],
)
def test_assess_chinese_tts_duration(duration_sec, expected_passed, expected_label):
    result = assess_tts_audio_duration(duration_sec, "啊" * 100)

    assert result["passed"] is expected_passed
    assert expected_label in result["diag"]


def test_regenerates_after_bad_audio_or_validation_error():
    attempts = iter(["quality_failure", "invalid_wav", "valid"])

    def generate_audio():
        return next(attempts).encode()

    def validate_audio(audio):
        if audio == b"quality_failure":
            return {"passed": False, "diag": "挤碎"}
        if audio == b"invalid_wav":
            raise ValueError("not a wav")
        return {"passed": True, "diag": "正常"}

    audio, quality, attempt = generate_with_quality_retry(
        generate_audio,
        validate_audio,
        max_retries=3,
    )

    assert audio == b"valid"
    assert quality["passed"] is True
    assert attempt == 3


def test_retries_exhaustion_raises_after_three_bad_audio_results():
    def generate_audio():
        return b"bad"

    def validate_audio(_audio):
        return {"passed": False, "diag": "拉伸"}

    with pytest.raises(RuntimeError, match="连续 3 次不合格"):
        generate_with_quality_retry(generate_audio, validate_audio, max_retries=3)


def test_assess_english_tts_duration_uses_word_thresholds():
    result = assess_tts_audio_duration(0.1, "hello world")

    assert result["passed"] is False
    assert "挤碎" in result["diag"]
