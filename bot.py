from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, voice_recv
from dotenv import load_dotenv

from models.meeting import (
    RecordingSession,
    RecordingSessionManager,
)
from services.audio_recorder import PerUserWaveSink
from services.transcriber import MeetingTranscriber


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
RECORDINGS_DIR = BASE_DIR / "storage" / "recordings"

LOGS_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set in .env"
    )

if not TEST_GUILD_ID:
    raise RuntimeError(
        "TEST_GUILD_ID is not set in .env"
    )

try:
    GUILD_ID = int(TEST_GUILD_ID)
except ValueError as error:
    raise RuntimeError(
        "TEST_GUILD_ID must contain numbers only"
    ) from error


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
    handlers=[
        logging.FileHandler(
            LOGS_DIR / "bot.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("meeting_bot")

logging.getLogger(
    "discord.ext.voice_recv.reader"
).setLevel(logging.WARNING)


class RecordCommands(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(
            name="record",
            description="Meeting recording commands",
        )

    @staticmethod
    def _can_manage_recording(
        user: discord.Member,
        session: RecordingSession,
    ) -> bool:
        if user.id == session.initiator_id:
            return True

        return bool(
            user.guild_permissions.manage_guild
        )

    @staticmethod
    async def _disconnect_voice_client(
        voice_client: discord.VoiceClient | None,
    ) -> None:
        if voice_client is None:
            return

        if not voice_client.is_connected():
            return

        try:
            await voice_client.disconnect(force=True)

        except Exception:
            logger.exception(
                "Failed to disconnect voice client "
                "during cleanup"
            )

    @staticmethod
    async def _finalize_session(
        guild_id: int,
        *,
        voice_client: discord.VoiceClient | None = None,
        delete_directory: bool = False,
        write_metadata: bool = True,
    ) -> RecordingSession | None:
        """
        Caller must already hold the guild session lock.
        """
        session = bot.session_manager.pop(guild_id)

        if session is None:
            return None

        if voice_client is None:
            guild = bot.get_guild(guild_id)

            voice_client = (
                guild.voice_client
                if guild is not None
                else None
            )

        if isinstance(
            voice_client,
            voice_recv.VoiceRecvClient,
        ):
            try:
                if voice_client.is_listening():
                    voice_client.stop_listening()

            except Exception:
                logger.exception(
                    "Failed to stop listening before "
                    "final cleanup | guild=%s",
                    guild_id,
                )

        if session.sink is not None:
            try:
                session.sink.cleanup()

            except Exception:
                logger.exception(
                    "Failed to clean up WAV sink | guild=%s",
                    guild_id,
                )

        session.end_time = datetime.now()
        session.refresh_from_sink()

        if (
            write_metadata
            and not delete_directory
            and session.directory.exists()
        ):
            try:
                metadata_path = session.write_metadata()

                logger.info(
                    "Session metadata written | "
                    "guild=%s | path=%s",
                    guild_id,
                    metadata_path,
                )

            except Exception:
                logger.exception(
                    "Failed to write session metadata | "
                    "guild=%s",
                    guild_id,
                )

        if (
            delete_directory
            and session.directory.exists()
        ):
            try:
                shutil.rmtree(session.directory)

                logger.info(
                    "Deleted canceled meeting directory | "
                    "path=%s",
                    session.directory,
                )

            except Exception:
                logger.exception(
                    "Failed to delete canceled meeting "
                    "directory | path=%s",
                    session.directory,
                )

        await RecordCommands._disconnect_voice_client(
            voice_client
        )

        return session

    @app_commands.command(
        name="start",
        description=(
            "Start recording your current voice channel"
        ),
    )
    async def start(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not isinstance(
            interaction.user,
            discord.Member,
        ):
            await interaction.response.send_message(
                "❌ الأمر ده لازم يتستخدم جوه سيرفر.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ مش قادر أحدد السيرفر.",
                ephemeral=True,
            )
            return

        voice_state = interaction.user.voice

        if (
            voice_state is None
            or voice_state.channel is None
        ):
            await interaction.response.send_message(
                "❌ لازم تدخل Voice Channel الأول.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        voice_channel = voice_state.channel

        await interaction.response.defer(
            ephemeral=False
        )

        async with bot.get_session_lock(guild_id):
            if bot.session_manager.has_active_session(
                guild_id
            ):
                await interaction.followup.send(
                    "⚠️ فيه تسجيل شغال بالفعل "
                    "في السيرفر ده.",
                    ephemeral=True,
                )
                return

            voice_client = interaction.guild.voice_client
            sink: PerUserWaveSink | None = None
            session: RecordingSession | None = None

            try:
                if voice_client is None:
                    voice_client = (
                        await voice_channel.connect(
                            cls=voice_recv.VoiceRecvClient,
                            self_deaf=False,
                            timeout=20.0,
                            reconnect=True,
                        )
                    )

                elif voice_client.channel != voice_channel:
                    await voice_client.move_to(
                        voice_channel
                    )

                if not isinstance(
                    voice_client,
                    voice_recv.VoiceRecvClient,
                ):
                    raise TypeError(
                        "The active voice connection is "
                        "not a VoiceRecvClient"
                    )

                if voice_client.is_listening():
                    await interaction.followup.send(
                        "⚠️ البوت بيستقبل صوت بالفعل.",
                        ephemeral=True,
                    )
                    return

                sink = PerUserWaveSink(
                    recordings_root=RECORDINGS_DIR,
                    guild_id=guild_id,
                    channel_id=voice_channel.id,
                )

                voice_client.listen(sink)

                session = RecordingSession(
                    guild_id=guild_id,
                    guild_name=interaction.guild.name,
                    channel_id=voice_channel.id,
                    channel_name=voice_channel.name,
                    initiator_id=interaction.user.id,
                    initiator_display_name=(
                        interaction.user.display_name
                    ),
                    started_at=datetime.now(),
                    directory=sink.session_directory,
                    sink=sink,
                    voice_client=voice_client,
                )

                bot.session_manager.set(session)

            except Exception:
                logger.exception(
                    "Failed to initialize recording | "
                    "guild=%s | channel=%s",
                    guild_id,
                    voice_channel.id,
                )

                if isinstance(
                    voice_client,
                    voice_recv.VoiceRecvClient,
                ):
                    try:
                        if voice_client.is_listening():
                            voice_client.stop_listening()

                    except Exception:
                        logger.exception(
                            "Failed to stop listening after "
                            "startup failure | guild=%s",
                            guild_id,
                        )

                if sink is not None:
                    try:
                        sink.cleanup()

                    except Exception:
                        logger.exception(
                            "Failed to clean sink after "
                            "startup failure | guild=%s",
                            guild_id,
                        )

                partial_directory: Path | None = None

                if session is not None:
                    partial_directory = session.directory

                elif sink is not None:
                    partial_directory = (
                        sink.session_directory
                    )

                if (
                    partial_directory is not None
                    and partial_directory.exists()
                ):
                    try:
                        shutil.rmtree(partial_directory)

                    except Exception:
                        logger.exception(
                            "Failed to remove partial "
                            "recording directory | path=%s",
                            partial_directory,
                        )

                bot.session_manager.clear(guild_id)

                await self._disconnect_voice_client(
                    voice_client
                )

                await interaction.followup.send(
                    "❌ فشل بدء التسجيل، وتم تنظيف "
                    "الملفات الجزئية.",
                    ephemeral=True,
                )

                return

            logger.info(
                "Recording started | "
                "guild=%s | channel=%s | "
                "initiator=%s | directory=%s",
                guild_id,
                voice_channel.id,
                interaction.user.id,
                session.directory,
            )

            await interaction.followup.send(
                "🔴 **بدأ تسجيل الاجتماع**\n\n"
                f"🎙️ القناة: **{voice_channel.name}**\n"
                f"👤 بدأه: "
                f"**{interaction.user.display_name}**\n\n"
                "⚠️ كل المشاركين لازم يكونوا "
                "عارفين إن التسجيل شغال.",
                ephemeral=False,
            )

    @app_commands.command(
        name="status",
        description="Show the recording status",
    )
    async def status(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ الأمر ده لازم يتستخدم جوه سيرفر.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        voice_client = interaction.guild.voice_client
        session = bot.session_manager.get(
            interaction.guild.id
        )

        if (
            voice_client is None
            or not voice_client.is_connected()
        ):
            await interaction.followup.send(
                "⚪ البوت مش متصل بـVoice Channel حاليًا.",
                ephemeral=True,
            )
            return

        receiving_audio = (
            isinstance(
                voice_client,
                voice_recv.VoiceRecvClient,
            )
            and voice_client.is_listening()
        )

        bot_member = interaction.guild.me

        bot_voice_state = (
            bot_member.voice
            if bot_member is not None
            else None
        )

        deafened = bool(
            bot_voice_state
            and (
                bot_voice_state.deaf
                or bot_voice_state.self_deaf
            )
        )

        lines = [
            "🟢 **حالة التسجيل**",
            (
                "🎙️ القناة: "
                f"**{voice_client.channel.name}**"
            ),
            (
                "🎧 استقبال الصوت: "
                f"**{'شغال' if receiving_audio else 'متوقف'}**"
            ),
            f"🔇 Deafened: **{deafened}**",
        ]

        if session is not None:
            session.refresh_from_sink()

            duration_seconds = int(
                session.meeting_duration
            )

            minutes, seconds = divmod(
                duration_seconds,
                60,
            )

            lines.extend(
                [
                    (
                        "⏱️ مدة التسجيل: "
                        f"**{minutes}m {seconds}s**"
                    ),
                    (
                        "👥 المتحدثين: "
                        f"**{session.speaker_count}**"
                    ),
                    (
                        "📦 إجمالي الحزم: "
                        f"**{session.total_packets}**"
                    ),
                ]
            )

        await interaction.followup.send(
            "\n".join(lines),
            ephemeral=True,
        )

    @app_commands.command(
        name="stop",
        description="Stop the current meeting recording",
    )
    async def stop(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ الأمر ده لازم يتستخدم جوه سيرفر.",
                ephemeral=True,
            )
            return

        if not isinstance(
            interaction.user,
            discord.Member,
        ):
            await interaction.response.send_message(
                "❌ مش قادر أحدد صلاحيات المستخدم.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild.id

        async with bot.get_session_lock(guild_id):
            current_session = bot.session_manager.get(
                guild_id
            )

            if current_session is None:
                await interaction.followup.send(
                    "⚪ مفيش تسجيل شغال حاليًا.",
                    ephemeral=True,
                )
                return

            if not self._can_manage_recording(
                interaction.user,
                current_session,
            ):
                await interaction.followup.send(
                    "❌ مش مسموح لك توقف التسجيل ده.",
                    ephemeral=True,
                )
                return

            session = await self._finalize_session(
                guild_id,
                voice_client=interaction.guild.voice_client,
                delete_directory=False,
                write_metadata=True,
            )

            if session is None:
                await interaction.followup.send(
                    "⚪ التسجيل انتهى بالفعل.",
                    ephemeral=True,
                )
                return

            summary = (
                session.sink.get_summary()
                if session.sink is not None
                else []
            )

            duration_total = int(
                session.meeting_duration
            )

            minutes, seconds = divmod(
                duration_total,
                60,
            )

            summary_text = (
                "\n".join(
                    f"• {line}" for line in summary
                )
                if summary
                else "• مفيش صوت اتسجل."
            )

            failed_validations = [
                user_id
                for user_id, status
                in session.raw_validation_status.items()
                if status != "ok"
            ]

            validation_text = (
                "✅ ملفات WAV سليمة."
                if not failed_validations
                else (
                    "⚠️ فيه ملفات WAV فشلت في الفحص: "
                    + ", ".join(
                        str(user_id)
                        for user_id
                        in failed_validations
                    )
                )
            )

            destination_channel = interaction.channel

            if destination_channel is None or not hasattr(
                destination_channel,
                "send",
            ):
                logger.warning(
                    "Cannot schedule transcript upload because the destination channel is not sendable | guild=%s | directory=%s",
                    guild_id,
                    session.directory,
                )
                await interaction.followup.send(
                    "⚠️ تم حفظ التسجيل، لكن القناة المستخدمة لإرسال النتيجة غير صالحة، لذلك تم إيقاف إرسال النتيجة في الخلفية.",
                    ephemeral=True,
                )
                return

            task = asyncio.create_task(
                bot._handle_background_transcription(
                    session,
                    destination_channel,
                )
            )
            bot.background_tasks.add(task)
            task.add_done_callback(bot.background_tasks.discard)

            logger.info(
                "Recording stop finalized and transcription queued | guild=%s | directory=%s | channel=%s",
                guild_id,
                session.directory,
                destination_channel,
            )

            await interaction.followup.send(
                "⏹️ **تم حفظ التسجيل**\n\n"
                "🧾 تم حفظ ملفات WAV ومعلومات الجلسة بنجاح.\n"
                "🎙️ النسخ التلقائي بدأ في الخلفية.\n"
                "📤 سيتم نشر النص النهائي في نفس القناة عند الانتهاء.\n\n"
                f"⏱️ المدة: **{minutes}m {seconds}s**\n"
                f"👥 عدد المتحدثين: **{session.speaker_count}**\n"
                f"{summary_text}\n\n"
                f"{validation_text}\n"
                "📁 المسار:\n"
                f"`{session.relative_directory(BASE_DIR)}`",
                ephemeral=True,
            )

    @app_commands.command(
        name="cancel",
        description=(
            "Cancel the current recording "
            "and delete its files"
        ),
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ الأمر ده لازم يتستخدم جوه سيرفر.",
                ephemeral=True,
            )
            return

        if not isinstance(
            interaction.user,
            discord.Member,
        ):
            await interaction.response.send_message(
                "❌ مش قادر أحدد صلاحيات المستخدم.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild.id

        async with bot.get_session_lock(guild_id):
            current_session = bot.session_manager.get(
                guild_id
            )

            if current_session is None:
                await interaction.followup.send(
                    "⚪ مفيش تسجيل شغال عشان يتلغي.",
                    ephemeral=True,
                )
                return

            if not self._can_manage_recording(
                interaction.user,
                current_session,
            ):
                await interaction.followup.send(
                    "❌ مش مسموح لك تلغي التسجيل ده.",
                    ephemeral=True,
                )
                return

            session = await self._finalize_session(
                guild_id,
                voice_client=interaction.guild.voice_client,
                delete_directory=True,
                write_metadata=False,
            )

            if session is None:
                await interaction.followup.send(
                    "⚪ التسجيل انتهى بالفعل.",
                    ephemeral=True,
                )
                return

            if session.directory.exists():
                message = (
                    "⚠️ التسجيل اتلغى، لكن فشل حذف "
                    "مجلد التسجيل بالكامل."
                )
            else:
                message = (
                    "✅ التسجيل اتلغى وكل ملفاته اتحذفت."
                )

            await interaction.followup.send(
                message,
                ephemeral=True,
            )


class MeetingBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True

        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.session_manager = (
            RecordingSessionManager()
        )
        self.transcriber = MeetingTranscriber()
        self.background_tasks: set[
            asyncio.Task[object]
        ] = set()

        self._session_locks: dict[
            int,
            asyncio.Lock,
        ] = {}

    def get_session_lock(
        self,
        guild_id: int,
    ) -> asyncio.Lock:
        return self._session_locks.setdefault(
            guild_id,
            asyncio.Lock(),
        )

    async def _handle_background_transcription(
        self,
        session: RecordingSession,
        destination_channel: discord.abc.Messageable | None,
    ) -> None:
        try:
            result = await self.transcriber.transcribe_meeting(
                session,
            )

            if destination_channel is None or not hasattr(
                destination_channel,
                "send",
            ):
                logger.warning(
                    "Skipping transcript upload because the destination channel is not sendable | guild=%s | directory=%s",
                    session.guild_id,
                    session.directory,
                )
                return

            model_name = "large-v3"
            meeting_duration = session.meeting_duration
            message = (
                "✅ تم إنهاء النسخ الفوري للقاء\n\n"
                f"⏱️ المدة: **{meeting_duration:.2f} ثانية**\n"
                f"👥 عدد المشاركين: **{session.speaker_count}**\n"
                f"✅ ناجح: **{result.success_count}**\n"
                f"⚠️ فشل: **{result.failure_count}**\n"
                f"⏳ وقت المعالجة: **{result.total_transcription_duration:.2f} ثانية**\n"
                f"🤖 النموذج: **{model_name}**"
            )

            transcript_path = result.meeting_transcript_path
            if transcript_path is None or not transcript_path.exists():
                await destination_channel.send(message)
                return

            try:
                await destination_channel.send(
                    content=message,
                    file=discord.File(
                        transcript_path,
                        filename=transcript_path.name,
                    ),
                )
                logger.info(
                    "Discord transcript uploaded | guild=%s | channel=%s | file=%s",
                    session.guild_id,
                    destination_channel,
                    transcript_path,
                )
            except Exception:
                logger.exception(
                    "Failed to upload transcript file to Discord | guild=%s | path=%s",
                    session.guild_id,
                    transcript_path,
                )
                await destination_channel.send(
                    "⚠️ تم إنشاء ملف النص النهائي محليًا، لكن فشل تحميله إلى الدردشة.\n"
                    f"المسار المحلي: `{transcript_path}`",
                )

        except Exception:
            logger.exception(
                "Background transcription failure | guild=%s | directory=%s",
                session.guild_id,
                session.directory,
            )

            if destination_channel is not None and hasattr(
                destination_channel,
                "send",
            ):
                await destination_channel.send(
                    "❌ فشل النسخ التلقائي في الخلفية.\n"
                    f"المجلد: `{session.directory}`\n"
                    "تم الاحتفاظ بكل ملفات WAV وكل نصوص المشاركين محليًا.",
                )

    async def setup_hook(self) -> None:
        guild = discord.Object(id=GUILD_ID)

        self.tree.add_command(
            RecordCommands(),
            guild=guild,
        )

        self.tree.add_command(
            ping,
            guild=guild,
        )

        await self.tree.sync(guild=guild)

        logger.info(
            "Synced application commands for guild %s",
            GUILD_ID,
        )

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if (
            self.user is None
            or member.id != self.user.id
        ):
            return

        guild = member.guild

        async with self.get_session_lock(guild.id):
            session = self.session_manager.get(
                guild.id
            )

            if session is None:
                return

            voice_client = guild.voice_client

            if (
                voice_client is None
                or not voice_client.is_connected()
            ):
                logger.warning(
                    "Unexpected voice disconnect | "
                    "guild=%s | session=%s",
                    guild.id,
                    session.directory,
                )

                await RecordCommands._finalize_session(
                    guild.id,
                    voice_client=voice_client,
                    delete_directory=False,
                    write_metadata=True,
                )


bot = MeetingBot()


@app_commands.command(
    name="ping",
    description="Check whether the bot is online",
)
async def ping(
    interaction: discord.Interaction,
) -> None:
    latency_ms = round(bot.latency * 1000)

    await interaction.response.send_message(
        "✅ البوت شغال\n"
        f"📡 Latency: `{latency_ms} ms`",
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(
        DISCORD_TOKEN,
        log_handler=None,
    )