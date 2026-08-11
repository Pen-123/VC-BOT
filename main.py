import os
import asyncio
import logging
import traceback
import discord
from discord.ext import commands
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import aiofiles
from pathlib import Path

# ---------- Config ----------
TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8000))

log = logging.getLogger("uvicorn")

if not TOKEN:
    log.critical("❌ DISCORD_TOKEN is missing")
    raise RuntimeError("Missing token")
else:
    masked = TOKEN[:6] + "..." + TOKEN[-4:] if len(TOKEN) > 10 else "***"
    log.info(f"🔑 Token loaded: {masked}")

# ---------- Bot Setup ----------
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

current_file: Path = Path("current.mp3")
bot_ready = False
bot_login_error: str | None = None

# Voice state – only stored when actually connected
connected_guild_id: int | None = None
connected_channel_id: int | None = None

def get_voice_client() -> discord.VoiceClient | None:
    """Return the voice client for the current guild, if connected."""
    if connected_guild_id is not None:
        for vc in bot.voice_clients:
            if vc.guild.id == connected_guild_id and vc.is_connected():
                return vc
    return None

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    global bot_ready, bot_login_error
    bot_ready = True
    bot_login_error = None
    log.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_disconnect():
    global bot_ready
    bot_ready = False
    log.warning("⚠️  Bot disconnected from Discord")

@bot.event
async def on_voice_state_update(member, before, after):
    # Only care about the bot itself
    if member != bot.user:
        return

    global connected_guild_id, connected_channel_id

    # True disconnection: bot was in a channel and now isn't, and no other channel joined
    if before.channel is not None and after.channel is None:
        log.info(f"🔁 Bot left #{before.channel.name}")

        # Clear state
        connected_guild_id = None
        connected_channel_id = None

        # If we have a stored last channel, try to reconnect (only once)
        if before.channel.guild.id and before.channel.id:
            guild = bot.get_guild(before.channel.guild.id)
            if guild:
                channel = guild.get_channel(before.channel.id)
                if channel and isinstance(channel, discord.VoiceChannel):
                    # Check that we aren't already connected (just in case)
                    already = False
                    for vc in bot.voice_clients:
                        if vc.guild.id == guild.id and vc.is_connected():
                            already = True
                            break
                    if not already:
                        try:
                            await asyncio.sleep(0.5)  # tiny delay to avoid race
                            await channel.connect()
                            connected_guild_id = guild.id
                            connected_channel_id = channel.id
                            log.info("🔁 Reconnected successfully")
                        except Exception as e:
                            log.error(f"❌ Reconnect failed: {e}")

    # Bot moved to another channel (or joined after being disconnected)
    elif after.channel is not None:
        connected_guild_id = after.channel.guild.id
        connected_channel_id = after.channel.id
        log.info(f"📢 Bot is now in #{after.channel.name}")

# ---------- FastAPI ----------
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ---------- Endpoints ----------
@app.get("/voice")
async def voice_status():
    vc = get_voice_client()
    return {
        "connected": vc is not None and vc.is_connected(),
        "guild_id": connected_guild_id,
        "channel_id": connected_channel_id,
        "channel_name": vc.channel.name if vc and vc.channel else None
    }

@app.get("/debug")
async def debug():
    return {
        "token_ok": bool(TOKEN),
        "bot_ready": bot_ready,
        "bot_user": str(bot.user) if bot_ready else None,
        "guild_count": len(bot.guilds) if bot_ready else 0,
        "voice": await voice_status(),
        "login_error": bot_login_error
    }

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/guilds")
async def get_guilds():
    if not bot_ready:
        raise HTTPException(status_code=503, detail="Bot not connected")
    return JSONResponse([{"id": str(g.id), "name": g.name} for g in bot.guilds])

@app.get("/channels/{guild_id}")
async def get_voice_channels(guild_id: int):
    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    return JSONResponse([{"id": str(c.id), "name": c.name} for c in guild.voice_channels])

@app.post("/join")
async def join_voice(data: dict):
    global connected_guild_id, connected_channel_id

    guild_id = int(data["guild_id"])
    channel_id = int(data["channel_id"])

    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        raise HTTPException(status_code=400, detail="Invalid voice channel")

    # Disconnect from any existing VC in that guild
    for vc in bot.voice_clients:
        if vc.guild.id == guild_id:
            await vc.disconnect()

    # Connect to the new channel
    try:
        await channel.connect()
        connected_guild_id = guild_id
        connected_channel_id = channel_id
        return {"status": "connected", "channel": channel.name}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/upload")
async def upload_mp3(file: UploadFile = File(...)):
    if not file.filename.endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files allowed")
    async with aiofiles.open(current_file, "wb") as out:
        content = await file.read()
        await out.write(content)
    return {"filename": file.filename, "size": len(content)}

@app.post("/play")
async def play_audio():
    vc = get_voice_client()
    if vc is None:
        raise HTTPException(status_code=400, detail="Bot is not in a voice channel")
    if vc.is_playing():
        vc.stop()
    if not current_file.exists():
        raise HTTPException(status_code=400, detail="No MP3 file uploaded")
    source = discord.FFmpegPCMAudio(str(current_file))
    vc.play(source)
    return {"status": "playing"}

@app.post("/stop")
async def stop_audio():
    vc = get_voice_client()
    if vc and vc.is_playing():
        vc.stop()
    return {"status": "stopped"}

# ---------- Startup ----------
@app.on_event("startup")
async def startup_event():
    async def start_bot():
        global bot_ready, bot_login_error
        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            bot_login_error = "Invalid token"
            log.error("❌ Invalid bot token")
        except Exception as e:
            bot_login_error = str(e)
            log.error(f"❌ Bot error: {e}")
    asyncio.create_task(start_bot())

@app.on_event("shutdown")
async def shutdown_event():
    await bot.close()
