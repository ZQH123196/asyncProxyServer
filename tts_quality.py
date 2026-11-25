import re
from collections.abc import Callable


QualityResult = dict[str, object]
FailureCallback = Callable[[int, QualityResult | None, Exception | None], None]


def assess_tts_audio_duration(duration_sec: float, text: str) -> QualityResult:
    """Check whether average TTS duration per written unit is plausible."""
    syllabic_chars = len(re.findall(
        r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff'
        r'\u3040-\u309f\u30a0-\u30ff'
        r'\uac00-\ud7af\u3130-\u318f]', text))
    alpha_chars = len(re.findall(r'[^\W\d_]', text, re.UNICODE)) - syllabic_chars

    if syllabic_chars + alpha_chars == 0:
        return {"passed": False, "diag": "原文无有效字符，无法校验"}

    if syllabic_chars > alpha_chars * 0.3:
        unit_count = syllabic_chars + alpha_chars
        unit_name = "字"
        crush_ms = 20
        stretch_ms = 500
    else:
        unit_count = len(re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", text, re.UNICODE))
        unit_name = "词"
        crush_ms = 80
        stretch_ms = 1500

    if unit_count == 0:
        return {"passed": False, "diag": "原文无有效单元，无法校验"}

    if duration_sec <= 0:
        return {"passed": False, "diag": "音频时长为 0（空音频）"}

    ms_per_unit = (duration_sec / unit_count) * 1000

    if ms_per_unit < crush_ms:
        return {
            "passed": False,
            "diag": f"挤碎 | 每{unit_name}={ms_per_unit:.1f}ms < {crush_ms}ms | {unit_name}数={unit_count} 时长={duration_sec:.2f}s",
        }
    if ms_per_unit > stretch_ms:
        return {
            "passed": False,
            "diag": f"拉伸 | 每{unit_name}={ms_per_unit:.1f}ms > {stretch_ms}ms | {unit_name}数={unit_count} 时长={duration_sec:.2f}s",
        }

    return {
        "passed": True,
        "diag": f"正常 | 每{unit_name}={ms_per_unit:.1f}ms | {unit_name}数={unit_count} 时长={duration_sec:.2f}s",
    }


def generate_with_quality_retry(
    generate_audio: Callable[[], bytes],
    validate_audio: Callable[[bytes], QualityResult],
    max_retries: int,
    on_failure: FailureCallback | None = None,
) -> tuple[bytes, QualityResult, int]:
    """Regenerate when quality validation fails or cannot parse the audio."""
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    for attempt in range(1, max_retries + 1):
        try:
            audio = generate_audio()
            quality = validate_audio(audio)
        except Exception as exc:
            if on_failure:
                on_failure(attempt, None, exc)
            if attempt == max_retries:
                raise RuntimeError(
                    f"TTS 音频生成或质量校验连续 {max_retries} 次失败: {exc}"
                ) from exc
            continue

        if quality.get("passed") is True:
            return audio, quality, attempt

        if on_failure:
            on_failure(attempt, quality, None)
        if attempt == max_retries:
            diag = quality.get("diag", "未知质量问题")
            raise RuntimeError(
                f"TTS 音频质量校验连续 {max_retries} 次不合格 | {diag}"
            )
