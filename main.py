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

# ---------- Logging (Uvicorn's logger, guaranteed to appear in Railway logs) ----------
log = logging.getLogger("uvicorn")

# ---------- Validate token early ----------
if not TOKEN:
    log.critical("❌ DISCORD_TOKEN environment variable is not set!")
    raise RuntimeError("DISCORD_TOKEN environment variable is not set")
else:
    # Show a masked version of the token for debugging
    masked = TOKEN[:6] + "..." + TOKEN[-4:] if len(TOKEN) > 10 else "***"
    log.info(f"🔑 Token loaded (masked): {masked}")

# ---------- Bot Setup ----------
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Global state
current_vc: discord.VoiceClient | None = None
current_file: Path = Path("current.mp3")
bot_ready = False
bot_login_error: str | None = None          # Store last login error

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    global bot_ready, bot_login_error
    bot_ready = True
    bot_login_error = None
    log.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"   Connected to {len(bot.guilds)} guild(s)")

@bot.event
async def on_disconnect():
    global bot_ready
    bot_ready = False
    log.warning("⚠️  Bot disconnected from Discord")

# ---------- FastAPI Setup ----------
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ---------- API Endpoints ----------

@app.get("/debug")
async def debug_info():
    """Return detailed status for troubleshooting."""
    return {
        "token_set": bool(TOKEN),
        "token_masked": (TOKEN[:6] + "..." + TOKEN[-4:]) if TOKEN and len(TOKEN) > 10 else "***",
        "bot_ready": bot_ready,
        "bot_user": str(bot.user) if bot_ready else None,
        "guild_count": len(bot.guilds) if bot_ready else 0,
        "last_login_error": bot_login_error,
        "voice_connected": current_vc is not None and current_vc.is_connected()
    }

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "bot_ready": bot_ready
    })

@app.get("/guilds")
async def get_guilds():
    if not bot_ready:
        detail = "Bot is not connected to Discord"
        if bot_login_error:
            detail += f": {bot_login_error}"
        raise HTTPException(status_code=503, detail=detail)
    guilds = [{"id": str(g.id), "name": g.name} for g in bot.guilds]
    return JSONResponse(guilds)

@app.get("/channels/{guild_id}")
async def get_voice_channels(guild_id: int):
    if not bot_ready:
        raise HTTPException(status_code=503, detail="Bot is not connected")
    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    channels = [{"id": str(c.id), "name": c.name} for c in guild.voice_channels]
    return JSONResponse(channels)

@app.post("/join")
async def join_voice(data: dict):
    if not bot_ready:
        raise HTTPException(status_code=503, detail="Bot is not connected")
    guild_id = int(data["guild_id"])
    channel_id = int(data["channel_id"])
    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.VoiceChannel):
        raise HTTPException(status_code=400, detail="Invalid voice channel")
    global current_vc
    if current_vc and current_vc.is_connected():
        await current_vc.disconnect()
    try:
        current_vc = await channel.connect()
    except discord.ClientException as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "connected", "channel": channel.name}

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
    global current_vc
    if not current_vc or not current_vc.is_connected():
        raise HTTPException(status_code=400, detail="Bot is not in a voice channel. Join one first.")
    if current_vc.is_playing():
        current_vc.stop()
    if not current_file.exists():
        raise HTTPException(status_code=400, detail="No MP3 file uploaded yet.")
    source = discord.FFmpegPCMAudio(str(current_file))
    current_vc.play(source)
    return {"status": "playing"}

@app.post("/stop")
async def stop_audio():
    global current_vc
    if current_vc and current_vc.is_playing():
        current_vc.stop()
    return {"status": "stopped"}

# ---------- Startup / Shutdown ----------
@app.on_event("startup")
async def startup_event():
    global bot_login_error

    async def start_bot():
        global bot_ready, bot_login_error
        try:
            log.info("🚀 Starting bot...")
            await bot.start(TOKEN)
        except discord.LoginFailure:
            bot_login_error = "Invalid bot token. Check DISCORD_TOKEN in Railway variables."
            log.error("❌ " + bot_login_error)
        except Exception as e:
            bot_login_error = f"Unexpected error: {str(e)}"
            log.error(f"❌ Bot failed to start: {e}\n{traceback.format_exc()}")
        else:
            # If bot.start() returns (which it normally shouldn't), we still log
            log.warning("⚠️  Bot stopped unexpectedly.")
            bot_ready = False

    # Launch the bot as a background task
    asyncio.create_task(start_bot())

@app.on_event("shutdown")
async def shutdown_event():
    log.info("🛑 Shutting down bot...")
    await bot.close()
    log.info("🛑 Bot shut down.")

# ---------- Main (for local dev only) ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
