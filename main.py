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

# ---------- Voice persistence ----------
TARGET_GUILD_ID: int | None = None   # Set when the user clicks "Join"
TARGET_CHANNEL_ID: int | None = None
reconnect_lock = asyncio.Lock()
voice_keepalive_task: asyncio.Task | None = None

def get_any_voice_client() -> discord.VoiceClient | None:
    """Return any connected voice client the bot has."""
    for vc in bot.voice_clients:
        if vc.is_connected():
            return vc
    return None

async def ensure_connected():
    """Reconnect to the target channel if we aren't already there."""
    if TARGET_GUILD_ID is None or TARGET_CHANNEL_ID is None:
        return
    async with reconnect_lock:
        # Are we already in the correct channel?
        for vc in bot.voice_clients:
            if vc.guild.id == TARGET_GUILD_ID and vc.is_connected():
                if vc.channel and vc.channel.id == TARGET_CHANNEL_ID:
                    return  # We're in the right place, do nothing
                else:
                    # In the same guild but wrong channel – move
                    try:
                        await vc.move_to(bot.get_channel(TARGET_CHANNEL_ID))
                        return
                    except Exception as e:
                        log.warning(f"Could not move: {e}")
        # Not connected at all – connect
        guild = bot.get_guild(TARGET_GUILD_ID)
        if not guild:
            return
        channel = guild.get_channel(TARGET_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            return
        try:
            await channel.connect()
            log.info(f"🔁 Reconnected to #{channel.name}")
        except Exception as e:
            log.error(f"Reconnect failed: {e}")

async def keepalive_loop():
    """Every 15 seconds, ensure we're still in the target voice channel."""
    while True:
        await asyncio.sleep(15)
        if bot_ready:
            await ensure_connected()

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    global bot_ready, bot_login_error
    bot_ready = True
    bot_login_error = None
    log.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # Start the keepalive task
    global voice_keepalive_task
    voice_keepalive_task = asyncio.create_task(keepalive_loop())

@bot.event
async def on_disconnect():
    global bot_ready
    bot_ready = False
    log.warning("⚠️  Bot disconnected")

# ---------- FastAPI ----------
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ---------- Endpoints ----------
@app.get("/voice")
async def voice_status():
    vc = get_any_voice_client()
    return {
        "connected": vc is not None and vc.is_connected(),
        "guild_id": TARGET_GUILD_ID,
        "channel_id": TARGET_CHANNEL_ID,
        "channel_name": vc.channel.name if vc and vc.channel else None,
        "is_target": (vc is not None and vc.channel.id == TARGET_CHANNEL_ID) if vc else False
    }

@app.get("/debug")
async def debug():
    return {
        "token_ok": bool(TOKEN),
        "bot_ready": bot_ready,
        "bot_user": str(bot.user) if bot_ready else None,
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
    global TARGET_GUILD_ID, TARGET_CHANNEL_ID

    guild_id = int(data["guild_id"])
    channel_id = int(data["channel_id"])

    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        raise HTTPException(status_code=400, detail="Invalid voice channel")

    # Update target
    TARGET_GUILD_ID = guild_id
    TARGET_CHANNEL_ID = channel_id

    # Disconnect from any existing VC in that guild
    for vc in bot.voice_clients:
        if vc.guild.id == guild_id:
            await vc.disconnect()

    # Connect to target channel immediately
    try:
        await channel.connect()
        log.info(f"📢 Joined #{channel.name}")
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
    # Force a connection check before playing
    await ensure_connected()
    vc = get_any_voice_client()
    if vc is None:
        raise HTTPException(status_code=400, detail="Bot is not in any voice channel. Please click 'Join' first.")
    if vc.is_playing():
        vc.stop()
    if not current_file.exists():
        raise HTTPException(status_code=400, detail="No MP3 file uploaded")
    source = discord.FFmpegPCMAudio(str(current_file))
    vc.play(source)
    return {"status": "playing"}

@app.post("/stop")
async def stop_audio():
    vc = get_any_voice_client()
    if vc and vc.is_playing():
        vc.stop()
    return {"status": "stopped"}

# ---------- Startup / Shutdown ----------
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
    if voice_keepalive_task:
        voice_keepalive_task.cancel()
    await bot.close()
