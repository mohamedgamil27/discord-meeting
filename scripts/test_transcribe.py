from __future__ import annotations

import argparse
import time
from pathlib import Path

import ctranslate2
from faster_whisper import WhisperModel


def format_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, round(seconds * 1000))

    total_seconds, milliseconds = divmod(
        total_milliseconds,
        1000,
    )

    minutes, seconds_part = divmod(
        total_seconds,
        60,
    )

    hours, minutes = divmod(
        minutes,
        60,
    )

    if hours > 0:
        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds_part:02d}."
            f"{milliseconds:03d}"
        )

    return (
        f"{minutes:02d}:"
        f"{seconds_part:02d}."
        f"{milliseconds:03d}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe one Discord WAV recording "
            "using faster-whisper."
        ),
    )

    parser.add_argument(
        "audio_file",
        type=Path,
        help="Path to the WAV recording.",
    )

    parser.add_argument(
        "--model",
        default="medium",
        help="Whisper model name. Default: medium",
    )

    parser.add_argument(
        "--compute-type",
        default="float16",
        choices=(
            "float16",
            "int8_float16",
            "int8",
            "float32",
        ),
        help="GPU compute type. Default: float16",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional transcript output TXT path.",
    )

    args = parser.parse_args()

    audio_file = args.audio_file.expanduser().resolve()

    if not audio_file.is_file():
        raise FileNotFoundError(
            f"Audio file does not exist: {audio_file}"
        )

    cuda_devices = ctranslate2.get_cuda_device_count()

    if cuda_devices < 1:
        raise RuntimeError(
            "CTranslate2 cannot detect an NVIDIA CUDA device."
        )

    output_path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else audio_file.with_suffix(".transcript.txt")
    )

    print(f"Audio file: {audio_file}")
    print(f"Output file: {output_path}")
    print(f"Model: {args.model}")
    print("Device: cuda")
    print(f"Compute type: {args.compute_type}")
    print(f"CUDA devices: {cuda_devices}")
    print()
    print("Loading model...")

    model_load_started = time.perf_counter()

    model = WhisperModel(
        args.model,
        device="cuda",
        compute_type=args.compute_type,
    )

    model_load_seconds = (
        time.perf_counter() - model_load_started
    )

    print(
        f"Model loaded in {model_load_seconds:.2f} seconds"
    )
    print("Transcribing...")

    transcription_started = time.perf_counter()

    segment_generator, info = model.transcribe(
        str(audio_file),
        task="transcribe",
        language=None,
        multilingual=True,
        beam_size=5,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 350,
            "speech_pad_ms": 300,
        },
        condition_on_previous_text=False,
        word_timestamps=True,
        temperature=0.0,
        initial_prompt=(
            "هذا تسجيل اجتماع يحتوي على العربية المصرية والإنجليزية. "
            "اكتب الكلام العربي بالعربية، واكتب الكلام الإنجليزي بالإنجليزية. "
            "لا تترجم بين اللغتين. "
            "This meeting contains Egyptian Arabic and English. "
            "Transcribe Arabic speech in Arabic and English speech in English. "
            "Do not translate either language."
        ),
        hotwords=(
            "project server database deployment documentation "
            "deadline meeting action items Mohamed Ahmed "
            "المشروع السيرفر قاعدة البيانات الاجتماع الموعد"
        ),
    )

    segments = list(segment_generator)

    transcription_seconds = (
        time.perf_counter() - transcription_started
    )

    transcript_lines: list[str] = []

    for segment in segments:
        text = segment.text.strip()

        if not text:
            continue

        start = format_timestamp(segment.start)
        end = format_timestamp(segment.end)

        transcript_lines.append(
            f"[{start} -> {end}] {text}"
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_content = "\n".join(
        [
            "Whisper Test Transcript",
            "=" * 70,
            f"Audio file: {audio_file.name}",
            f"Model: {args.model}",
            "Device: cuda",
            f"Compute type: {args.compute_type}",
            f"Detected language: {info.language}",
            (
                "Language probability: "
                f"{info.language_probability:.4f}"
            ),
            f"Audio duration: {info.duration:.3f} seconds",
            (
                "Transcription time: "
                f"{transcription_seconds:.3f} seconds"
            ),
            "",
            "Transcript",
            "=" * 70,
            *(
                transcript_lines
                if transcript_lines
                else ["[No speech detected]"]
            ),
            "",
        ]
    )

    output_path.write_text(
        output_content,
        encoding="utf-8",
    )

    print()
    print("Transcription complete")
    print(f"Detected language: {info.language}")
    print(
        "Language probability: "
        f"{info.language_probability:.4f}"
    )
    print(f"Audio duration: {info.duration:.3f} seconds")
    print(
        "Transcription time: "
        f"{transcription_seconds:.3f} seconds"
    )
    print()
    print("Transcript")
    print("=" * 70)

    if transcript_lines:
        for line in transcript_lines:
            print(line)
    else:
        print("[No speech detected]")

    print()
    print(f"Saved transcript to: {output_path}")


if __name__ == "__main__":
    main()