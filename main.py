import os
import asyncio
import discord
from discord.ext import commands
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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
intents.voice_states = True  # needed to know voice channels

bot = commands.Bot(command_prefix="!", intents=intents)

# Global state for the web interface
current_vc: discord.VoiceClient | None = None
current_file: Path = Path("current.mp3")

# ---------- FastAPI Setup ----------
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Optional: auto-join a default VC on startup (you can omit this)
    # default_guild_id = int(os.getenv("DEFAULT_GUILD", 0))
    # default_channel_id = int(os.getenv("DEFAULT_CHANNEL", 0))
    # ...

# ---------- API Endpoints ----------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the control panel."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/guilds")
async def get_guilds():
    """Return all guilds the bot is in."""
    guilds = [
        {"id": str(g.id), "name": g.name}
        for g in bot.guilds
    ]
    return JSONResponse(guilds)

@app.get("/channels/{guild_id}")
async def get_voice_channels(guild_id: int):
    """Return voice channels for a given guild."""
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
    """Make the bot join a specific voice channel."""
    guild_id = int(data["guild_id"])
    channel_id = int(data["channel_id"])

    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")

    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.VoiceChannel):
        raise HTTPException(status_code=400, detail="Invalid voice channel")

    global current_vc

    # Disconnect from current VC if any
    if current_vc and current_vc.is_connected():
        await current_vc.disconnect()

    # Connect to new channel
    try:
        current_vc = await channel.connect()
    except discord.ClientException as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "connected", "channel": channel.name}

@app.post("/upload")
async def upload_mp3(file: UploadFile = File(...)):
    """Save an uploaded MP3 file."""
    if not file.filename.endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files allowed")

    async with aiofiles.open(current_file, "wb") as out:
        content = await file.read()
        await out.write(content)

    return {"filename": file.filename, "size": len(content)}

@app.post("/play")
async def play_audio():
    """Play the uploaded MP3 in the currently connected voice channel."""
    global current_vc

    if not current_vc or not current_vc.is_connected():
        raise HTTPException(status_code=400, detail="Bot is not in a voice channel. Join one first.")

    # Stop current playback if any
    if current_vc.is_playing():
        current_vc.stop()

    # Play the file
    if not current_file.exists():
        raise HTTPException(status_code=400, detail="No MP3 file uploaded yet.")

    source = discord.FFmpegPCMAudio(str(current_file))
    current_vc.play(source)
    return {"status": "playing"}

@app.post("/stop")
async def stop_audio():
    """Stop playback."""
    global current_vc
    if current_vc and current_vc.is_playing():
        current_vc.stop()
    return {"status": "stopped"}

# ---------- Main Runner ----------
async def main():
    # Start the bot in the background
    async with bot:
        # Launch the web server in the same event loop
        config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
        server = uvicorn.Server(config)
        await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
