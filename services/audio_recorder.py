from __future__ import annotations

import logging
import threading
import wave
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import discord
from discord.ext import voice_recv


logger = logging.getLogger("meeting_bot.audio")


class PerUserWaveSink(voice_recv.AudioSink):
    """
    Records decoded Discord PCM into one WAV file per Discord user.

    Active recording behavior is intentionally simple:

    - No artificial silence insertion
    - No RTP timeline reconstruction
    - No normalization
    - No gain adjustment
    - No denoising

    Only valid, frame-aligned decoded PCM is written.
    """

    SAMPLE_RATE = 48_000
    CHANNELS = 2
    SAMPLE_WIDTH = 2

    BLOCK_ALIGN = CHANNELS * SAMPLE_WIDTH
    WAV_HEADER_MINIMUM_SIZE = 44

    def __init__(
        self,
        recordings_root: Path,
        guild_id: int,
        channel_id: int,
    ) -> None:
        super().__init__()

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        self.session_directory = (
            recordings_root
            / str(guild_id)
            / f"{timestamp}_{channel_id}"
        )

        self.session_directory.mkdir(
            parents=True,
            exist_ok=False,
        )

        self.packet_counts: dict[int, int] = defaultdict(int)
        self.user_names: dict[int, str] = {}

        self.wave_files: dict[int, wave.Wave_write] = {}
        self.output_paths: dict[int, Path] = {}

        self.raw_durations: dict[int, float] = {}
        self.raw_frame_counts: dict[int, int] = {}

        self.raw_validation_status: dict[int, str] = {}
        self.raw_validation_errors: dict[int, str | None] = {}

        # Preserved for metadata compatibility.
        self.inserted_silence_seconds: dict[int, float] = {}
        self.normalized_paths: dict[int, str | None] = {}
        self.normalized_durations: dict[int, float | None] = {}
        self.normalization_status: dict[int, str] = {}

        self.trimmed_chunk_counts: dict[int, int] = defaultdict(int)
        self.trimmed_byte_counts: dict[int, int] = defaultdict(int)

        self.lock = threading.Lock()
        self.closed = False

        logger.info(
            "Created recording directory | path=%s",
            self.session_directory,
        )

    def wants_opus(self) -> bool:
        """
        Request decoded PCM instead of raw Opus packets.
        """
        return False

    def _open_user_file(
        self,
        user: discord.User | discord.Member,
    ) -> wave.Wave_write:
        output_path = self.session_directory / f"{user.id}.wav"

        wave_file = wave.open(str(output_path), "wb")
        wave_file.setnchannels(self.CHANNELS)
        wave_file.setsampwidth(self.SAMPLE_WIDTH)
        wave_file.setframerate(self.SAMPLE_RATE)

        self.wave_files[user.id] = wave_file
        self.output_paths[user.id] = output_path
        self.user_names[user.id] = user.display_name

        self.raw_durations[user.id] = 0.0
        self.raw_frame_counts[user.id] = 0

        self.raw_validation_status[user.id] = "pending"
        self.raw_validation_errors[user.id] = None

        self.inserted_silence_seconds[user.id] = 0.0
        self.normalized_paths[user.id] = None
        self.normalized_durations[user.id] = None
        self.normalization_status[user.id] = (
            "disabled_for_raw_validation"
        )

        logger.info(
            "Created user WAV file | "
            "user=%s | user_id=%s | path=%s",
            user.display_name,
            user.id,
            output_path,
        )

        return wave_file

    def _align_pcm(
        self,
        pcm: bytes,
        user_id: int,
    ) -> bytes:
        """
        Trim an incomplete trailing stereo PCM frame.

        Stereo signed 16-bit PCM requires every frame to contain:

        2 channels * 2 bytes = 4 bytes.
        """
        original_length = len(pcm)

        aligned_length = (
            original_length
            - (original_length % self.BLOCK_ALIGN)
        )

        if aligned_length <= 0:
            logger.warning(
                "Skipping PCM chunk smaller than one complete frame | "
                "user_id=%s | original_bytes=%s | block_align=%s",
                user_id,
                original_length,
                self.BLOCK_ALIGN,
            )

            return b""

        if aligned_length != original_length:
            trimmed_bytes = original_length - aligned_length

            self.trimmed_chunk_counts[user_id] += 1
            self.trimmed_byte_counts[user_id] += trimmed_bytes

            logger.warning(
                "Trimmed incomplete PCM trailing bytes | "
                "user_id=%s | original_bytes=%s | "
                "aligned_bytes=%s | trimmed_bytes=%s",
                user_id,
                original_length,
                aligned_length,
                trimmed_bytes,
            )

        aligned_pcm = pcm[:aligned_length]

        assert len(aligned_pcm) % self.BLOCK_ALIGN == 0

        return aligned_pcm

    def write(
        self,
        user: discord.User | discord.Member | None,
        data: voice_recv.VoiceData,
    ) -> None:
        if user is None:
            return

        if not data.pcm:
            return

        # Make an immutable local copy before entering shared state.
        pcm = bytes(data.pcm)

        with self.lock:
            if self.closed:
                return

            aligned_pcm = self._align_pcm(
                pcm,
                user.id,
            )

            if not aligned_pcm:
                return

            wave_file = self.wave_files.get(user.id)

            if wave_file is None:
                wave_file = self._open_user_file(user)

            wave_file.writeframesraw(aligned_pcm)

            self.packet_counts[user.id] += 1
            packet_count = self.packet_counts[user.id]

        if packet_count == 1 or packet_count % 250 == 0:
            logger.info(
                "Audio written | "
                "user=%s | user_id=%s | packets=%s | "
                "original_pcm_bytes=%s | written_pcm_bytes=%s",
                user.display_name,
                user.id,
                packet_count,
                len(pcm),
                len(aligned_pcm),
            )

    def _validate_raw_wav(
        self,
        user_id: int,
        output_path: Path,
    ) -> tuple[bool, str | None, float, int]:
        """
        Validate the finalized WAV using Python's wave module.
        """
        try:
            if not output_path.exists():
                raise FileNotFoundError(
                    f"WAV file does not exist: {output_path}"
                )

            if output_path.stat().st_size <= self.WAV_HEADER_MINIMUM_SIZE:
                raise ValueError(
                    "WAV contains no useful PCM audio"
                )

            with wave.open(str(output_path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                sample_rate = wav_file.getframerate()
                frame_count = wav_file.getnframes()

                if channels != self.CHANNELS:
                    raise ValueError(
                        f"Expected {self.CHANNELS} channels, "
                        f"received {channels}"
                    )

                if sample_width != self.SAMPLE_WIDTH:
                    raise ValueError(
                        f"Expected sample width "
                        f"{self.SAMPLE_WIDTH}, "
                        f"received {sample_width}"
                    )

                if sample_rate != self.SAMPLE_RATE:
                    raise ValueError(
                        f"Expected sample rate "
                        f"{self.SAMPLE_RATE}, "
                        f"received {sample_rate}"
                    )

                if frame_count <= 0:
                    raise ValueError(
                        "WAV contains zero PCM frames"
                    )

                expected_pcm_bytes = (
                    frame_count
                    * channels
                    * sample_width
                )

                if expected_pcm_bytes % self.BLOCK_ALIGN != 0:
                    raise ValueError(
                        "WAV PCM byte count is not block-aligned"
                    )

                # Force Python's wave reader to read the entire payload.
                pcm_data = wav_file.readframes(frame_count)

                if len(pcm_data) != expected_pcm_bytes:
                    raise ValueError(
                        "WAV PCM payload length does not match "
                        f"the frame count: expected "
                        f"{expected_pcm_bytes}, received "
                        f"{len(pcm_data)}"
                    )

                if len(pcm_data) % self.BLOCK_ALIGN != 0:
                    raise ValueError(
                        "WAV PCM payload is not block-aligned"
                    )

                duration_seconds = (
                    frame_count / sample_rate
                )

            logger.info(
                "Raw WAV validation passed | "
                "user_id=%s | path=%s | frames=%s | "
                "duration=%.3f | size=%s",
                user_id,
                output_path,
                frame_count,
                duration_seconds,
                output_path.stat().st_size,
            )

            return (
                True,
                None,
                duration_seconds,
                frame_count,
            )

        except Exception as error:
            error_text = f"{type(error).__name__}: {error}"

            logger.exception(
                "Raw WAV validation failed | "
                "user_id=%s | path=%s",
                user_id,
                output_path,
            )

            return (
                False,
                error_text,
                0.0,
                0,
            )

    def cleanup(self) -> None:
        """
        Finalize and validate every WAV.

        Safe to call more than once.
        """
        with self.lock:
            if self.closed:
                return

            self.closed = True

            open_files = list(self.wave_files.items())
            self.wave_files.clear()

        # Close outside the shared-state lock.
        for user_id, wave_file in open_files:
            output_path = self.output_paths.get(user_id)

            try:
                wave_file.close()

                logger.info(
                    "Closed user WAV file | "
                    "user_id=%s | path=%s",
                    user_id,
                    output_path,
                )

            except Exception:
                logger.exception(
                    "Failed to close WAV file | user_id=%s",
                    user_id,
                )

                with self.lock:
                    self.raw_validation_status[user_id] = "failed"
                    self.raw_validation_errors[user_id] = (
                        "WAV file could not be closed"
                    )

                continue

            if output_path is None:
                with self.lock:
                    self.raw_validation_status[user_id] = "failed"
                    self.raw_validation_errors[user_id] = (
                        "No output path was registered"
                    )

                continue

            (
                valid,
                validation_error,
                duration_seconds,
                frame_count,
            ) = self._validate_raw_wav(
                user_id,
                output_path,
            )

            with self.lock:
                self.raw_validation_status[user_id] = (
                    "ok" if valid else "failed"
                )

                self.raw_validation_errors[user_id] = (
                    validation_error
                )

                self.raw_durations[user_id] = (
                    duration_seconds
                )

                self.raw_frame_counts[user_id] = (
                    frame_count
                )

                self.inserted_silence_seconds[user_id] = 0.0
                self.normalized_paths[user_id] = None
                self.normalized_durations[user_id] = None
                self.normalization_status[user_id] = (
                    "disabled_for_raw_validation"
                )

        logger.info(
            "PerUserWaveSink cleanup completed | directory=%s",
            self.session_directory,
        )

    def get_summary(self) -> list[str]:
        with self.lock:
            user_ids = list(self.packet_counts.keys())
            packet_counts = dict(self.packet_counts)
            user_names = dict(self.user_names)
            output_paths = dict(self.output_paths)
            validation_status = dict(self.raw_validation_status)

        return [
            (
                f"{user_names.get(user_id, str(user_id))}: "
                f"{packet_counts.get(user_id, 0)} packets → "
                f"{output_paths.get(user_id, self.session_directory / f'{user_id}.wav').name} | "
                f"validation={validation_status.get(user_id, 'pending')}"
            )
            for user_id in user_ids
        ]

    def get_output_paths(self) -> list[Path]:
        with self.lock:
            return list(self.output_paths.values())

    def get_snapshot(self) -> dict[str, Any]:
        """
        Return a thread-safe snapshot for session metadata.
        """
        with self.lock:
            return {
                "packet_counts": dict(self.packet_counts),
                "user_names": dict(self.user_names),
                "output_paths": {
                    user_id: path.name
                    for user_id, path
                    in self.output_paths.items()
                },
                "inserted_silence_seconds": dict(
                    self.inserted_silence_seconds
                ),
                "raw_durations": dict(
                    self.raw_durations
                ),
                "raw_frame_counts": dict(
                    self.raw_frame_counts
                ),
                "raw_validation_status": dict(
                    self.raw_validation_status
                ),
                "raw_validation_errors": dict(
                    self.raw_validation_errors
                ),
                "normalized_paths": dict(
                    self.normalized_paths
                ),
                "normalized_durations": dict(
                    self.normalized_durations
                ),
                "normalization_status": dict(
                    self.normalization_status
                ),
                "trimmed_chunk_counts": dict(
                    self.trimmed_chunk_counts
                ),
                "trimmed_byte_counts": dict(
                    self.trimmed_byte_counts
                ),
            }