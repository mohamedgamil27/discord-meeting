from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel

from models.meeting import RecordingSession

logger = logging.getLogger("meeting_bot.transcriber")

MODEL_NAME = "large-v3"
MODEL_DEVICE = "cuda"
MODEL_COMPUTE_TYPE = "float16"

TRANSCRIBE_PROMPT = (
    "هذا اجتماع يحتوي على العربية المصرية والإنجليزية.\n"
    "اكتب الكلام العربي بحروف عربية، والكلام الإنجليزي بحروف إنجليزية.\n"
    "لا تترجم ولا تعرّب الكلمات الإنجليزية.\n"
    "This meeting contains Egyptian Arabic and English.\n"
    "Write Arabic speech in Arabic and English speech in English.\n"
    "Do not translate or transliterate either language."
)

HOTWORDS = (
    "project server settings deployment documentation deadline meeting "
    "Thursday Friday action items Mohamed Ahmed "
    "المشروع السيرفر الإعدادات الاجتماع الخميس الجمعة الموعد المهام"
)


def format_duration(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000)))
    total_seconds, milliseconds = divmod(total_milliseconds, 1000)
    minutes, seconds_part = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours > 0:
        return (
            f"{hours:02d}:{minutes:02d}:{seconds_part:02d}."
            f"{milliseconds:03d}"
        )

    return (
        f"{minutes:02d}:{seconds_part:02d}.{milliseconds:03d}"
    )


@dataclass(slots=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str
    language: str | None = None


@dataclass(slots=True)
class ParticipantTranscript:
    user_id: int
    display_name: str
    wav_filename: str
    detected_primary_language: str | None
    language_probability: float
    audio_duration: float
    transcription_duration: float
    transcript_segments: list[TranscriptSegment] = field(
        default_factory=list,
    )
    output_txt_filename: str = ""
    success: bool = False
    error: str | None = None


@dataclass(slots=True)
class MeetingTranscriptResult:
    meeting_transcript_path: Path | None
    participant_transcripts: list[ParticipantTranscript]
    total_transcription_duration: float
    success_count: int
    failure_count: int


class MeetingTranscriber:
    def __init__(self) -> None:
        self._model: WhisperModel | None = None
        self._model_lock = asyncio.Lock()
        self._gpu_lock = asyncio.Lock()

    @staticmethod
    def _validate_cuda_runtime() -> None:
        required_libraries = (
            "libcublas.so.12",
            "libcublasLt.so.12",
            "libcudnn.so.9",
        )

        errors: list[str] = []

        for library_name in required_libraries:
            try:
                ctypes.CDLL(library_name)
            except OSError as exc:
                errors.append(f"{library_name}: {exc}")

        if errors:
            raise RuntimeError(
                "CUDA runtime libraries could not be loaded. "
                "Please launch the bot via scripts/run_with_cuda.sh "
                "python -u bot.py. Missing: "
                + "; ".join(errors)
            )

    def _load_model(self) -> WhisperModel:
        logger.info(
            "Whisper model loading started | model=%s | device=%s | compute_type=%s",
            MODEL_NAME,
            MODEL_DEVICE,
            MODEL_COMPUTE_TYPE,
        )

        self._validate_cuda_runtime()
        model = WhisperModel(
            MODEL_NAME,
            device=MODEL_DEVICE,
            compute_type=MODEL_COMPUTE_TYPE,
        )

        logger.info(
            "Whisper model loaded | model=%s | device=%s | compute_type=%s",
            MODEL_NAME,
            MODEL_DEVICE,
            MODEL_COMPUTE_TYPE,
        )

        return model

    async def _ensure_model(self) -> WhisperModel:
        if self._model is not None:
            return self._model

        async with self._model_lock:
            if self._model is not None:
                return self._model

            self._model = await asyncio.to_thread(self._load_model)
            return self._model

    @staticmethod
    def _transcribe_once(
        model: WhisperModel,
        audio_path: str,
    ) -> tuple[Any, Any]:
        return model.transcribe(
            audio_path,
            task="transcribe",
            language=None,
            multilingual=True,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            word_timestamps=True,
            temperature=0.0,
            vad_parameters={
                "min_silence_duration_ms": 350,
                "speech_pad_ms": 300,
            },
            initial_prompt=TRANSCRIBE_PROMPT,
            hotwords=HOTWORDS,
        )

    async def transcribe_participant(
        self,
        session: RecordingSession,
        wav_path: Path,
    ) -> ParticipantTranscript:
        user_id = int(wav_path.stem)
        display_name = session.user_names.get(user_id, str(user_id))
        wav_filename = wav_path.name

        logger.info(
            "Participant transcription started | guild=%s | user_id=%s | file=%s",
            session.guild_id,
            user_id,
            wav_path.name,
        )

        transcription_started = time.perf_counter()

        try:
            model = await self._ensure_model()

            async with self._gpu_lock:
                segment_generator, info = await asyncio.to_thread(
                    self._transcribe_once,
                    model,
                    str(wav_path),
                )

            segments = []
            for segment in segment_generator:
                text = segment.text.strip()
                if not text:
                    continue

                segments.append(
                    TranscriptSegment(
                        start_seconds=float(segment.start),
                        end_seconds=float(segment.end),
                        text=text,
                        language=getattr(segment, "language", None),
                    )
                )

            transcription_duration = (
                time.perf_counter() - transcription_started
            )

            output_txt_filename = f"{user_id}.transcript.txt"
            output_path = session.directory / output_txt_filename
            output_path.write_text(
                self._format_participant_transcript(
                    user_id=user_id,
                    display_name=display_name,
                    wav_filename=wav_filename,
                    detected_language=(info.language or None),
                    language_probability=float(info.language_probability),
                    audio_duration=float(info.duration),
                    transcription_duration=transcription_duration,
                    segments=segments,
                ),
                encoding="utf-8",
            )

            participant = ParticipantTranscript(
                user_id=user_id,
                display_name=display_name,
                wav_filename=wav_filename,
                detected_primary_language=info.language or None,
                language_probability=float(info.language_probability),
                audio_duration=float(info.duration),
                transcription_duration=transcription_duration,
                transcript_segments=segments,
                output_txt_filename=output_txt_filename,
                success=True,
            )

            logger.info(
                "Participant transcription completed | guild=%s | user_id=%s | file=%s | duration=%.3fs",
                session.guild_id,
                user_id,
                wav_path.name,
                transcription_duration,
            )

            return participant

        except Exception as exc:  # pragma: no cover - exercised in runtime
            transcription_duration = (
                time.perf_counter() - transcription_started
            )
            error_text = f"{type(exc).__name__}: {exc}"

            participant = ParticipantTranscript(
                user_id=user_id,
                display_name=display_name,
                wav_filename=wav_filename,
                detected_primary_language=None,
                language_probability=0.0,
                audio_duration=0.0,
                transcription_duration=transcription_duration,
                transcript_segments=[],
                output_txt_filename=f"{user_id}.transcript.txt",
                success=False,
                error=error_text,
            )

            logger.exception(
                "Participant transcription failed | guild=%s | user_id=%s | file=%s",
                session.guild_id,
                user_id,
                wav_path.name,
            )

            return participant

    @staticmethod
    def _format_participant_transcript(
        *,
        user_id: int,
        display_name: str,
        wav_filename: str,
        detected_language: str | None,
        language_probability: float,
        audio_duration: float,
        transcription_duration: float,
        segments: list[TranscriptSegment],
    ) -> str:
        lines = [
            "Participant Transcript",
            "======================",
            "",
            f"Participant: {display_name}",
            f"Discord User ID: {user_id}",
            f"Audio file: {wav_filename}",
            "Model: large-v3",
            "Device: cuda",
            "Compute type: float16",
            (
                "Detected primary language: "
                f"{detected_language if detected_language is not None else 'unknown'}"
            ),
            (
                "Language probability: "
                f"{language_probability:.4f}"
            ),
            f"Audio duration: {audio_duration:.3f} seconds",
            f"Transcription duration: {transcription_duration:.3f} seconds",
            "",
            "Transcript",
            "==========",
            "",
        ]

        if segments:
            for segment in segments:
                lines.append(
                    (
                        f"[{format_duration(segment.start_seconds)} -> "
                        f"{format_duration(segment.end_seconds)}] "
                        f"{segment.text}"
                    )
                )
        else:
            lines.append("[No speech detected]")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _build_meeting_transcript(
        *,
        session: RecordingSession,
        participants: list[ParticipantTranscript],
    ) -> str:
        meeting_duration = session.meeting_duration
        meeting_end = (
            session.end_time.isoformat(timespec="seconds")
            if session.end_time is not None
            else datetime.now().isoformat(timespec="seconds")
        )

        lines = [
            "Meeting Transcript",
            "==================",
            "",
            f"Server: {session.guild_name}",
            f"Voice Channel: {session.channel_name}",
            f"Meeting Started: {session.started_at.isoformat(timespec='seconds')}",
            f"Meeting Ended: {meeting_end}",
            f"Meeting Duration: {meeting_duration:.3f} seconds",
            "Transcription Model: large-v3",
            "Device: CUDA",
            "Compute Type: float16",
            "",
            "Important:",
            "Timestamps are relative to each participant's recorded WAV file.",
            "They are not yet a shared chronological meeting timeline.",
            "",
            "Participants",
            "============",
            "",
        ]

        for participant in participants:
            lines.append(
                f"- {participant.display_name} ({participant.user_id})"
            )

        lines.extend(["", ""])

        for participant in participants:
            lines.extend(
                [
                    f"Participant: {participant.display_name}",
                    f"Discord User ID: {participant.user_id}",
                    f"Audio File: {participant.wav_filename}",
                    (
                        "Detected Primary Language: "
                        f"{participant.detected_primary_language or 'unknown'}"
                    ),
                    (
                        "Language Probability: "
                        f"{participant.language_probability:.4f}"
                    ),
                    "",
                ]
            )

            if participant.success and participant.transcript_segments:
                for segment in participant.transcript_segments:
                    lines.append(
                        (
                            f"[{format_duration(segment.start_seconds)} -> "
                            f"{format_duration(segment.end_seconds)}]"
                        )
                    )
                    lines.append(segment.text)
                    lines.append("")
            elif participant.success:
                lines.append("[No speech detected]")
                lines.append("")
            else:
                lines.append(f"Transcription failed: {participant.error}")
                lines.append("")

            lines.extend(["", ""])

        failed = [
            participant
            for participant in participants
            if not participant.success
        ]
        if failed:
            lines.extend(
                [
                    "Transcription Failures",
                    "======================",
                    "",
                ]
            )
            for participant in failed:
                lines.append(
                    f"- {participant.display_name} ({participant.user_id}): {participant.error}"
                )

        return "\n".join(lines) + "\n"

    @staticmethod
    def _update_session_metadata(
        session: RecordingSession,
        *,
        meeting_transcript_path: Path | None,
        participant_transcripts: list[ParticipantTranscript],
        total_duration: float,
    ) -> None:
        metadata_path = session.directory / "session.json"
        metadata: dict[str, Any] = {}

        if metadata_path.exists():
            try:
                with metadata_path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                if isinstance(raw, dict):
                    metadata = raw
            except json.JSONDecodeError:
                metadata = {}

        successful = [
            participant for participant in participant_transcripts if participant.success
        ]
        failed = [
            participant for participant in participant_transcripts if not participant.success
        ]

        metadata["transcription"] = {
            "status": "completed" if failed else "completed",
            "model": MODEL_NAME,
            "device": MODEL_DEVICE,
            "compute_type": MODEL_COMPUTE_TYPE,
            "started_at": session.started_at.isoformat(timespec="seconds"),
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "processing_duration_seconds": round(total_duration, 6),
            "meeting_transcript_file": (
                meeting_transcript_path.name if meeting_transcript_path is not None else None
            ),
            "successful_participants": [
                str(participant.user_id) for participant in successful
            ],
            "failed_participants": [
                str(participant.user_id) for participant in failed
            ],
        }

        participant_entries = metadata.setdefault("participants", [])
        if isinstance(participant_entries, list):
            by_user_id = {
                str(participant.user_id): participant for participant in participant_transcripts
            }
            for entry in participant_entries:
                if not isinstance(entry, dict):
                    continue
                user_id = str(entry.get("user_id"))
                if user_id not in by_user_id:
                    continue
                participant = by_user_id[user_id]
                entry["transcript_file"] = participant.output_txt_filename
                entry["transcription_status"] = (
                    "success" if participant.success else "failed"
                )
                entry["transcription_error"] = participant.error
                entry["detected_language"] = participant.detected_primary_language
                entry["language_probability"] = participant.language_probability
                entry["transcription_duration_seconds"] = (
                    participant.transcription_duration
                )
                entry["transcript_segment_count"] = (
                    len(participant.transcript_segments)
                )

        temp_path = metadata_path.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=4)
            handle.write("\n")

        temp_path.replace(metadata_path)

        logger.info(
            "Session metadata updated | path=%s | success=%s | failed=%s",
            metadata_path,
            len(successful),
            len(failed),
        )

    async def transcribe_meeting(
        self,
        session: RecordingSession,
    ) -> MeetingTranscriptResult:
        session.refresh_from_sink()
        wav_files = []

        for user_id in sorted(session.user_ids):
            filename = session.wav_files.get(user_id)
            if filename is None:
                continue
            wav_path = session.directory / filename
            if wav_path.exists() and wav_path.suffix.lower() == ".wav":
                wav_files.append(wav_path)

        logger.info(
            "Transcription queued | guild=%s | meeting_dir=%s | wav_count=%s",
            session.guild_id,
            session.directory,
            len(wav_files),
        )

        total_started = time.perf_counter()
        participant_results: list[ParticipantTranscript] = []

        for wav_path in wav_files:
            participant_results.append(
                await self.transcribe_participant(session, wav_path)
            )

        meeting_transcript_path = session.directory / "meeting_transcript.txt"
        meeting_transcript_path.write_text(
            self._build_meeting_transcript(
                session=session,
                participants=participant_results,
            ),
            encoding="utf-8",
        )

        total_duration = time.perf_counter() - total_started
        success_count = sum(1 for p in participant_results if p.success)
        failure_count = sum(1 for p in participant_results if not p.success)

        self._update_session_metadata(
            session,
            meeting_transcript_path=meeting_transcript_path,
            participant_transcripts=participant_results,
            total_duration=total_duration,
        )

        logger.info(
            "Combined transcript created | guild=%s | path=%s | success=%s | failed=%s | total_duration=%.3fs",
            session.guild_id,
            meeting_transcript_path,
            success_count,
            failure_count,
            total_duration,
        )

        return MeetingTranscriptResult(
            meeting_transcript_path=meeting_transcript_path,
            participant_transcripts=participant_results,
            total_transcription_duration=total_duration,
            success_count=success_count,
            failure_count=failure_count,
        )
