from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from services.audio_recorder import PerUserWaveSink


@dataclass(slots=True)
class RecordingSession:
    guild_id: int
    guild_name: str

    channel_id: int
    channel_name: str

    initiator_id: int
    initiator_display_name: str

    started_at: datetime
    directory: Path

    sink: PerUserWaveSink | None
    voice_client: discord.VoiceClient | None

    end_time: datetime | None = None

    user_ids: set[int] = field(default_factory=set)
    user_names: dict[int, str] = field(default_factory=dict)
    wav_files: dict[int, str] = field(default_factory=dict)
    packet_counts: dict[int, int] = field(default_factory=dict)

    inserted_silence_seconds: dict[int, float] = field(
        default_factory=dict
    )

    raw_durations: dict[int, float] = field(
        default_factory=dict
    )

    raw_frame_counts: dict[int, int] = field(
        default_factory=dict
    )

    raw_validation_status: dict[int, str] = field(
        default_factory=dict
    )

    raw_validation_errors: dict[int, str | None] = field(
        default_factory=dict
    )

    normalized_wav_files: dict[int, str | None] = field(
        default_factory=dict
    )

    normalized_durations: dict[int, float | None] = field(
        default_factory=dict
    )

    normalization_status: dict[int, str] = field(
        default_factory=dict
    )

    trimmed_chunk_counts: dict[int, int] = field(
        default_factory=dict
    )

    trimmed_byte_counts: dict[int, int] = field(
        default_factory=dict
    )

    def refresh_from_sink(self) -> None:
        if self.sink is None:
            return

        snapshot = self.sink.get_snapshot()

        self.packet_counts = dict(
            snapshot["packet_counts"]
        )

        self.user_names = dict(
            snapshot["user_names"]
        )

        self.wav_files = dict(
            snapshot["output_paths"]
        )

        self.user_ids = set(self.packet_counts.keys())

        self.inserted_silence_seconds = dict(
            snapshot["inserted_silence_seconds"]
        )

        self.raw_durations = dict(
            snapshot["raw_durations"]
        )

        self.raw_frame_counts = dict(
            snapshot["raw_frame_counts"]
        )

        self.raw_validation_status = dict(
            snapshot["raw_validation_status"]
        )

        self.raw_validation_errors = dict(
            snapshot["raw_validation_errors"]
        )

        self.normalized_wav_files = dict(
            snapshot["normalized_paths"]
        )

        self.normalized_durations = dict(
            snapshot["normalized_durations"]
        )

        self.normalization_status = dict(
            snapshot["normalization_status"]
        )

        self.trimmed_chunk_counts = dict(
            snapshot["trimmed_chunk_counts"]
        )

        self.trimmed_byte_counts = dict(
            snapshot["trimmed_byte_counts"]
        )

    @property
    def meeting_duration(self) -> float:
        ending_time = self.end_time or datetime.now()

        return max(
            0.0,
            (ending_time - self.started_at).total_seconds(),
        )

    @property
    def speaker_count(self) -> int:
        return len(self.user_ids)

    @property
    def total_packets(self) -> int:
        return sum(self.packet_counts.values())

    def relative_directory(self, base_directory: Path) -> str:
        try:
            return str(
                self.directory.relative_to(base_directory)
            )
        except ValueError:
            return str(self.directory)

    def _participant_metadata(
        self,
        user_id: int,
    ) -> dict[str, object]:
        normalized_file = self.normalized_wav_files.get(
            user_id
        )

        return {
            "user_id": user_id,
            "display_name": self.user_names.get(
                user_id,
                str(user_id),
            ),
            "wav_file": self.wav_files.get(
                user_id,
                f"{user_id}.wav",
            ),
            "raw_wav_file": self.wav_files.get(
                user_id,
                f"{user_id}.wav",
            ),
            "normalized_wav_file": normalized_file,
            "packet_count": self.packet_counts.get(
                user_id,
                0,
            ),
            "inserted_silence_seconds": (
                self.inserted_silence_seconds.get(
                    user_id,
                    0.0,
                )
            ),
            "raw_duration_seconds": self.raw_durations.get(
                user_id,
                0.0,
            ),
            "raw_frame_count": self.raw_frame_counts.get(
                user_id,
                0,
            ),
            "raw_validation_status": (
                self.raw_validation_status.get(
                    user_id,
                    "pending",
                )
            ),
            "raw_validation_error": (
                self.raw_validation_errors.get(
                    user_id
                )
            ),
            "normalized_duration_seconds": (
                self.normalized_durations.get(
                    user_id
                )
            ),
            "normalization_status": (
                self.normalization_status.get(
                    user_id,
                    "disabled_for_raw_validation",
                )
            ),
            "trimmed_chunk_count": (
                self.trimmed_chunk_counts.get(
                    user_id,
                    0,
                )
            ),
            "trimmed_byte_count": (
                self.trimmed_byte_counts.get(
                    user_id,
                    0,
                )
            ),
        }

    def write_metadata(self) -> Path:
        self.refresh_from_sink()

        metadata_path = self.directory / "session.json"

        metadata = {
            "guild_id": self.guild_id,
            "guild_name": self.guild_name,
            "voice_channel_id": self.channel_id,
            "voice_channel_name": self.channel_name,
            "meeting_start_time": (
                self.started_at.isoformat(timespec="seconds")
            ),
            "meeting_end_time": (
                self.end_time.isoformat(timespec="seconds")
                if self.end_time is not None
                else None
            ),
            "meeting_duration_seconds": round(
                self.meeting_duration,
                6,
            ),
            "recording_initiator_id": self.initiator_id,
            "recording_initiator_display_name": (
                self.initiator_display_name
            ),
            "participants": [
                self._participant_metadata(user_id)
                for user_id in sorted(self.user_ids)
            ],
            "user_ids": sorted(self.user_ids),
            "display_names": {
                str(user_id): name
                for user_id, name
                in self.user_names.items()
            },
            "wav_filenames": {
                str(user_id): filename
                for user_id, filename
                in self.wav_files.items()
            },
            "packet_counts": {
                str(user_id): packet_count
                for user_id, packet_count
                in self.packet_counts.items()
            },
            "inserted_silence_seconds": {
                str(user_id): value
                for user_id, value
                in self.inserted_silence_seconds.items()
            },
            "raw_duration_seconds": {
                str(user_id): value
                for user_id, value
                in self.raw_durations.items()
            },
            "raw_frame_counts": {
                str(user_id): value
                for user_id, value
                in self.raw_frame_counts.items()
            },
            "raw_validation_status": {
                str(user_id): value
                for user_id, value
                in self.raw_validation_status.items()
            },
            "raw_validation_errors": {
                str(user_id): value
                for user_id, value
                in self.raw_validation_errors.items()
            },
            "normalization_status": {
                str(user_id): value
                for user_id, value
                in self.normalization_status.items()
            },
            "normalized_wav_files": {
                str(user_id): value
                for user_id, value
                in self.normalized_wav_files.items()
            },
            "trimmed_chunk_counts": {
                str(user_id): value
                for user_id, value
                in self.trimmed_chunk_counts.items()
            },
            "trimmed_byte_counts": {
                str(user_id): value
                for user_id, value
                in self.trimmed_byte_counts.items()
            },
        }

        with metadata_path.open(
            "w",
            encoding="utf-8",
        ) as metadata_file:
            json.dump(
                metadata,
                metadata_file,
                ensure_ascii=False,
                indent=4,
            )

        return metadata_path


class RecordingSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[int, RecordingSession] = {}

    def get(
        self,
        guild_id: int,
    ) -> RecordingSession | None:
        return self._sessions.get(guild_id)

    def has_active_session(
        self,
        guild_id: int,
    ) -> bool:
        return guild_id in self._sessions

    def set(
        self,
        session: RecordingSession,
    ) -> None:
        if session.guild_id in self._sessions:
            raise RuntimeError(
                "A recording session is already active "
                f"for guild {session.guild_id}"
            )

        self._sessions[session.guild_id] = session

    def pop(
        self,
        guild_id: int,
    ) -> RecordingSession | None:
        return self._sessions.pop(guild_id, None)

    def clear(
        self,
        guild_id: int,
    ) -> None:
        self._sessions.pop(guild_id, None)