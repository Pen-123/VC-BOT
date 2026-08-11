import os
import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import aiofiles


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", "8000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

log = logging.getLogger("bot")


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# STATE
# ============================================================

bot_ready = False
bot_login_error = None

target_guild_id: int | None = None
target_channel_id: int | None = None

voice_client: discord.VoiceClient | None = None

voice_lock = asyncio.Lock()
keepalive_task: asyncio.Task | None = None

current_file = Path("current.mp3")


# ============================================================
# HELPERS
# ============================================================

def get_voice_client() -> discord.VoiceClient | None:
    """
    Return the voice client belonging to our target guild.
    """
    global voice_client

    if voice_client and voice_client.is_connected():
        return voice_client

    voice_client = None

    if target_guild_id is None:
        return None

    guild = bot.get_guild(target_guild_id)

    if not guild:
        return None

    vc = guild.voice_client

    if vc and vc.is_connected():
        voice_client = vc
        return vc

    return None


async def connect_to_target():
    """
    Ensure the bot is connected to the target voice channel.
    """

    global voice_client

    if not bot_ready:
        return None

    if target_guild_id is None or target_channel_id is None:
        return None

    async with voice_lock:

        guild = bot.get_guild(target_guild_id)

        if guild is None:
            log.warning("Target guild is not available.")
            return None

        channel = guild.get_channel(target_channel_id)

        if not isinstance(channel, discord.VoiceChannel):
            log.warning("Target voice channel no longer exists.")
            return None

        existing = guild.voice_client

        # Already connected to correct channel
        if existing and existing.is_connected():

            if existing.channel.id == target_channel_id:
                voice_client = existing
                return existing

            # Wrong channel
            try:
                await existing.move_to(channel)
                voice_client = existing
                log.info("Moved to #%s", channel.name)
                return existing

            except Exception as e:
                log.warning("Failed to move voice client: %s", e)

                try:
                    await existing.disconnect(force=True)
                except Exception:
                    pass

                voice_client = None

        # Connect
        try:
            log.info("Connecting to #%s...", channel.name)

            vc = await channel.connect(
                timeout=20,
                reconnect=True
            )

            voice_client = vc

            log.info("✅ Connected to #%s", channel.name)

            return vc

        except Exception as e:
            log.error("❌ Voice connection failed: %s", e)
            voice_client = None
            return None


async def keepalive_loop():

    log.info("Voice keepalive started.")

    while True:

        try:
            await asyncio.sleep(10)

            if not bot_ready:
                continue

            if target_guild_id is None or target_channel_id is None:
                continue

            vc = get_voice_client()

            if vc is None or not vc.is_connected():
                log.warning("Voice connection lost. Reconnecting...")
                await connect_to_target()

        except asyncio.CancelledError:
            log.info("Voice keepalive stopped.")
            break

        except Exception:
            log.exception("Error in voice keepalive loop")


# ============================================================
# DISCORD EVENTS
# ============================================================

@bot.event
async def on_ready():

    global bot_ready
    global bot_login_error
    global keepalive_task

    bot_ready = True
    bot_login_error = None

    log.info(
        "✅ Discord ready as %s (%s)",
        bot.user,
        bot.user.id
    )

    # IMPORTANT:
    # Discord can fire on_ready multiple times after reconnecting.
    # Never create multiple keepalive loops.

    if keepalive_task is None or keepalive_task.done():
        keepalive_task = asyncio.create_task(
            keepalive_loop()
        )


@bot.event
async def on_disconnect():

    global bot_ready

    bot_ready = False

    log.warning("⚠️ Discord gateway disconnected.")


@bot.event
async def on_resumed():

    global bot_ready

    bot_ready = True

    log.info("🔄 Discord session resumed.")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()

templates = Jinja2Templates(
    directory="templates"
)


# ============================================================
# STATUS
# ============================================================

@app.get("/status")
async def status():

    vc = get_voice_client()

    return {
        "bot_connected": bot_ready,
        "bot_user": str(bot.user) if bot_ready else None,

        "voice_connected": (
            vc is not None
            and vc.is_connected()
        ),

        "guild_id": target_guild_id,
        "channel_id": target_channel_id,

        "channel_name": (
            vc.channel.name
            if vc and vc.channel
            else None
        )
    }


@app.get("/voice")
async def voice_status():

    vc = get_voice_client()

    return {
        "connected": (
            vc is not None
            and vc.is_connected()
        ),

        "guild_id": target_guild_id,
        "channel_id": target_channel_id,

        "channel_name": (
            vc.channel.name
            if vc and vc.channel
            else None
        )
    }


# ============================================================
# DEBUG
# ============================================================

@app.get("/debug")
async def debug():

    vc = get_voice_client()

    return {
        "token_ok": bool(TOKEN),

        "bot_ready": bot_ready,

        "bot_user": (
            str(bot.user)
            if bot.user
            else None
        ),

        "discord_guild_count": len(bot.guilds),

        "target_guild_id": target_guild_id,

        "target_channel_id": target_channel_id,

        "voice_connected": (
            vc is not None
            and vc.is_connected()
        ),

        "voice_channel": (
            vc.channel.name
            if vc and vc.channel
            else None
        ),

        "login_error": bot_login_error
    }


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request
        }
    )


# ============================================================
# GUILDS
# ============================================================

@app.get("/guilds")
async def get_guilds():

    if not bot_ready:

        raise HTTPException(
            status_code=503,
            detail="Discord bot is still connecting."
        )

    return [
        {
            "id": str(g.id),
            "name": g.name
        }
        for g in bot.guilds
    ]


# ============================================================
# CHANNELS
# ============================================================

@app.get("/channels/{guild_id}")
async def get_channels(guild_id: int):

    if not bot_ready:
        raise HTTPException(
            status_code=503,
            detail="Discord bot is still connecting."
        )

    guild = bot.get_guild(guild_id)

    if guild is None:

        raise HTTPException(
            status_code=404,
            detail="Guild not found."
        )

    return [
        {
            "id": str(channel.id),
            "name": channel.name
        }
        for channel in guild.voice_channels
    ]


# ============================================================
# JOIN
# ============================================================

@app.post("/join")
async def join_voice(data: dict):

    global target_guild_id
    global target_channel_id

    try:
        guild_id = int(data["guild_id"])
        channel_id = int(data["channel_id"])

    except (KeyError, ValueError):

        raise HTTPException(
            status_code=400,
            detail="Invalid guild/channel ID."
        )

    if not bot_ready:

        raise HTTPException(
            status_code=503,
            detail="Discord bot is not connected yet."
        )

    guild = bot.get_guild(guild_id)

    if guild is None:

        raise HTTPException(
            status_code=404,
            detail="Guild not found."
        )

    channel = guild.get_channel(channel_id)

    if not isinstance(channel, discord.VoiceChannel):

        raise HTTPException(
            status_code=400,
            detail="Invalid voice channel."
        )

    target_guild_id = guild_id
    target_channel_id = channel_id

    vc = await connect_to_target()

    if vc is None:

        raise HTTPException(
            status_code=500,
            detail="Failed to connect to voice channel."
        )

    return {
        "status": "connected",
        "channel": channel.name
    }


# ============================================================
# UPLOAD
# ============================================================

@app.post("/upload")
async def upload_mp3(file: UploadFile = File(...)):

    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No file selected."
        )

    if not file.filename.lower().endswith(".mp3"):

        raise HTTPException(
            status_code=400,
            detail="Only MP3 files are allowed."
        )

    async with aiofiles.open(
        current_file,
        "wb"
    ) as out:

        content = await file.read()

        await out.write(content)

    return {
        "filename": file.filename,
        "size": len(content)
    }


# ============================================================
# PLAY
# ============================================================

@app.post("/play")
async def play_audio():

    if not bot_ready:

        raise HTTPException(
            status_code=503,
            detail="Discord bot is not connected."
        )

    if not current_file.exists():

        raise HTTPException(
            status_code=400,
            detail="No MP3 file uploaded."
        )

    # Make absolutely sure we're connected
    vc = await connect_to_target()

    if vc is None:

        raise HTTPException(
            status_code=400,
            detail="Bot is not connected to a voice channel. Click Join first."
        )

    if vc.is_playing():
        vc.stop()

    try:

        source = discord.FFmpegPCMAudio(
            str(current_file)
        )

        vc.play(source)

    except Exception as e:

        log.exception("Playback failed")

        raise HTTPException(
            status_code=500,
            detail=f"Playback failed: {e}"
        )

    return {
        "status": "playing"
    }


# ============================================================
# STOP
# ============================================================

@app.post("/stop")
async def stop_audio():

    vc = get_voice_client()

    if vc and vc.is_playing():
        vc.stop()

    return {
        "status": "stopped"
    }


# ============================================================
# START BOT
# ============================================================

@app.on_event("startup")
async def startup_event():

    async def start_bot():

        global bot_login_error

        try:

            log.info("Starting Discord bot...")

            await bot.start(TOKEN)

        except discord.LoginFailure:

            bot_login_error = "Invalid Discord token."

            log.error("❌ Invalid Discord token.")

        except Exception as e:

            bot_login_error = str(e)

            log.exception(
                "❌ Discord bot crashed."
            )

    asyncio.create_task(start_bot())


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown_event():

    global keepalive_task

    if keepalive_task:

        keepalive_task.cancel()

        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass

        keepalive_task = None

    if bot.is_ready():

        await bot.close()
