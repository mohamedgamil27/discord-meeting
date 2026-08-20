# Discord Meeting Bot

A Discord voice meeting recorder with per-user WAV capture and automatic background transcription for Arabic and English meetings.

## Features

- Records each speaker into a separate WAV file.
- Stores meeting metadata in JSON.
- Transcribes recordings automatically after `/record stop`.
- Uses `faster-whisper` with the `large-v3` model, CUDA, and `float16`.
- Produces one transcript per participant and a combined meeting transcript.
- Supports Arabic and English speech without translation or transliteration.
- Keeps transcription in a background task so the bot can continue handling Discord events.
- Writes bot logs to `logs/bot.log`.

## Requirements

- Linux
- Python 3.13 or a compatible Python version
- A Discord bot application with voice permissions
- An NVIDIA GPU with working CUDA support for the default transcription configuration
- CUDA libraries available through the Python virtual environment

CPU transcription is not configured in the current production bot. The standalone script can be adapted for another device or compute type if needed.

## Discord Bot Setup

1. Create an application and bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable the required bot permissions:
   - View Channels
   - Connect
   - Speak
   - Send Messages
   - Use Slash Commands
3. Invite the bot to your server with the `bot` and `applications.commands` scopes.
4. Make sure users are informed before recording starts.

## Installation

```bash
git clone https://github.com/mohamedgamil27/discord-meeting.git
cd discord-meeting

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_discord_bot_token
TEST_GUILD_ID=your_discord_server_id
```

Never commit `.env` or expose the bot token. `TEST_GUILD_ID` is the numeric Discord server ID where commands are registered.

## Run the Bot

Use the CUDA wrapper so `faster-whisper` can find the CUDA libraries installed in the virtual environment:

```bash
./scripts/run_with_cuda.sh python bot.py
```

The bot registers these commands:

| Command | Description |
| --- | --- |
| `/ping` | Check that the bot is online. |
| `/record start` | Start recording the current voice channel. |
| `/record status` | Show the current recording status. |
| `/record stop` | Stop recording and start background transcription. |
| `/record cancel` | Stop recording and delete the current meeting directory. |

After `/record stop`, the bot finalizes the WAV files and starts transcription in the background. The resulting transcript files are saved in the meeting recording directory.

## Storage Layout

```text
storage/
  recordings/
    <guild_id>/
      <meeting_timestamp>_<initiator_id>/
        session.json
        <user_id>.wav
        <user_id>.transcript.txt
        meeting.transcript.txt
logs/
  bot.log
```

`session.json` contains meeting metadata, speaker names, audio validation results, and transcription status.

## Standalone Transcription Test

To transcribe one WAV file manually:

```bash
source .venv/bin/activate
./scripts/run_with_cuda.sh python scripts/test_transcribe.py path/to/recording.wav
```

Options:

```bash
./scripts/run_with_cuda.sh python scripts/test_transcribe.py \
  path/to/recording.wav \
  --model medium \
  --compute-type float16 \
  --output path/to/output.transcript.txt
```

The bot's background service uses `large-v3` by default. The standalone script defaults to `medium` so it is easier to use for quick tests.

## Validation

Check Python syntax and imports:

```bash
source .venv/bin/activate
python -m compileall bot.py models services scripts
python -c "import discord, faster_whisper, ctranslate2; import services.transcriber; print('IMPORT_OK')"
```

Check that CTranslate2 can detect the GPU:

```bash
source .venv/bin/activate
python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

## Troubleshooting

### `DISCORD_TOKEN is not set in .env`

Create `.env` in the project root and add both required variables. Confirm the file is named exactly `.env`.

### CUDA libraries cannot be found

Run the bot through `scripts/run_with_cuda.sh`. If the error continues, reinstall the CUDA packages inside `.venv`:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### The bot cannot record voice

Confirm that the bot can View Channels, Connect, Speak, and Use Slash Commands. Also confirm that the bot is not server-deafened and that the user issuing `/record start` is connected to a voice channel.

### Transcription is still running

The `large-v3` model can take time to load and process audio. Check `logs/bot.log` and the meeting directory for transcript output and updated metadata.

## Project Structure

```text
bot.py                    Discord bot and slash-command handlers
models/meeting.py         Recording session models and metadata
services/audio_recorder.py Per-user WAV recording
services/transcriber.py   Background Whisper transcription
scripts/test_transcribe.py Standalone transcription test
scripts/run_with_cuda.sh  CUDA library environment setup
requirements.txt          Python dependencies
```

## License

No license has been specified for this project yet.
