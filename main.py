import os
import asyncio
import traceback
import discord
from discord.ext import commands
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import aiofiles
from pathlib import Path

# ---------- Config ----------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable not set")

# ---------- Bot Setup ----------
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Global state for the web interface
current_vc: discord.VoiceClient | None = None
current_file: Path = Path("current.mp3")
bot_ready = False  # Track if the bot successfully logged in

# ---------- FastAPI Setup ----------
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    global bot_ready
    bot_ready = True
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_disconnect():
    global bot_ready
    bot_ready = False
    print("⚠️  Bot disconnected")

# ---------- API Endpoints ----------

@app.get("/status")
async def status():
    """Check if the bot is online."""
    return {"online": bot_ready, "guilds": len(bot.guilds) if bot_ready else 0}

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the control panel (with optional warning)."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "bot_ready": bot_ready
    })

@app.get("/guilds")
async def get_guilds():
    """Return all guilds the bot is in."""
    if not bot_ready:
        raise HTTPException(status_code=503, detail="Bot is not connected yet")
    guilds = [
        {"id": str(g.id), "name": g.name}
        for g in bot.guilds
    ]
    return JSONResponse(guilds)

@app.get("/channels/{guild_id}")
async def get_voice_channels(guild_id: int):
    if not bot_ready:
        raise HTTPException(status_code=503, detail="Bot is not connected")
    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    channels = [
        {"id": str(c.id), "name": c.name}
        for c in guild.voice_channels
    ]
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

# ---------- Main Runner ----------
async def main():
    async with bot:
        async def run_bot():
            try:
                await bot.start(TOKEN)
            except Exception as e:
                print("❌ Bot failed to start:")
                traceback.print_exc()
                raise  # re-raise so the task doesn't hang silently

        bot_task = asyncio.create_task(run_bot())

        config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
        server = uvicorn.Server(config)
        await server.serve()

        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    asyncio.run(main())
