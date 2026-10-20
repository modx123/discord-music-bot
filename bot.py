"""
🎵 Discord Music Bot - Full Featured
=====================================
Features:
  - Play songs by name or URL (YouTube)
  - Queue system with pagination
  - Skip, Pause, Resume, Stop
  - Volume control
  - Loop (song/queue)
  - Lyrics via Genius API
  - Interactive Buttons UI (Now Playing panel)
"""

import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os
import re
import time
from collections import deque
from dotenv import load_dotenv

# ── Try importing lyricsgenius (optional) ─────────────────────────────────────
try:
    import lyricsgenius
    GENIUS_AVAILABLE = True
except ImportError:
    GENIUS_AVAILABLE = False

# ── Load environment variables ─────────────────────────────────────────────────
load_dotenv()
TOKEN        = os.getenv("DISCORD_TOKEN")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")

if not TOKEN:
    print("❌ ERROR: DISCORD_TOKEN غير موجود في ملف .env")
    print("   افتح .env وضع التوكن بتاعك!")
    exit(1)

# ── Genius client (optional) ───────────────────────────────────────────────────
genius_client = None
if GENIUS_AVAILABLE and GENIUS_TOKEN:
    try:
        genius_client = lyricsgenius.Genius(
            GENIUS_TOKEN, verbose=False, remove_section_headers=True
        )
    except Exception:
        pass

# ── yt-dlp options ─────────────────────────────────────────────────────────────
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

# ═════════════════════════════════════════════════════════════════════════════
# Song Data Class
# ═════════════════════════════════════════════════════════════════════════════
class Song:
    def __init__(self, data: dict, requester: discord.Member):
        self.title       = data.get("title", "Unknown")
        self.webpage_url = data.get("webpage_url", data.get("url", ""))
        self.stream_url  = data.get("url", "")
        self.duration    = data.get("duration", 0)
        self.thumbnail   = data.get("thumbnail", "")
        self.uploader    = data.get("uploader", "Unknown")
        self.requester   = requester

    @property
    def duration_str(self):
        if not self.duration:
            return "🔴 Live"
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @classmethod
    async def from_query(cls, query: str, requester: discord.Member, loop=None):
        loop   = loop or asyncio.get_event_loop()
        is_url = bool(re.match(r"https?://", query))
        search = query if is_url else f"ytsearch:{query}"

        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(search, download=False)
        )
        if "entries" in data:
            data = data["entries"][0]
        return cls(data, requester)


# ═════════════════════════════════════════════════════════════════════════════
# Music Player (per guild)
# ═════════════════════════════════════════════════════════════════════════════
class MusicPlayer:
    def __init__(self, ctx: commands.Context):
        self.ctx        = ctx
        self.bot        = ctx.bot
        self.guild      = ctx.guild
        self.queue      = deque()
        self.current    = None
        self.volume     = 0.5
        self.loop_mode  = "none"   # none | song | queue
        self.np_message = None

    def add(self, song: Song):
        self.queue.append(song)

    def skip(self):
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    def shuffle(self):
        import random
        q = list(self.queue)
        random.shuffle(q)
        self.queue = deque(q)

    async def play_next(self):
        if self.loop_mode == "song" and self.current:
            song = self.current
        elif self.loop_mode == "queue" and self.current:
            self.queue.append(self.current)
            if not self.queue:
                self.current = None
                return
            song = self.queue.popleft()
        else:
            if not self.queue:
                self.current = None
                await self._cleanup_np()
                return
            song = self.queue.popleft()

        self.current = song

        def after_playing(err):
            if err:
                print(f"Player error: {err}")
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        vc = self.guild.voice_client
        if not vc:
            return

        # Re-fetch stream URL to avoid expiry
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(song.webpage_url, download=False)
            )
            stream_url = data["url"]
        except Exception as e:
            print(f"Stream fetch error: {e}")
            await self.play_next()
            return

        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(source, volume=self.volume)
        vc.play(source, after=after_playing)

        await self._send_now_playing()

    async def _send_now_playing(self):
        await self._cleanup_np()
        embed = self._build_np_embed()
        view  = NowPlayingView(self)
        try:
            self.np_message = await self.ctx.channel.send(embed=embed, view=view)
        except Exception:
            pass

    async def _cleanup_np(self):
        if self.np_message:
            try:
                await self.np_message.delete()
            except Exception:
                pass
            self.np_message = None

    def _build_np_embed(self):
        song = self.current
        loop_emoji = {"none": "▶️", "song": "🔂", "queue": "🔁"}
        embed = discord.Embed(
            title="🎵 Now Playing",
            description=f"[{song.title}]({song.webpage_url})",
            color=0x1DB954,
        )
        embed.add_field(name="⏱ Duration",   value=song.duration_str,        inline=True)
        embed.add_field(name="🔊 Volume",     value=f"{int(self.volume*100)}%", inline=True)
        embed.add_field(name="🔄 Loop",       value=loop_emoji[self.loop_mode], inline=True)
        embed.add_field(name="📤 Requested",  value=song.requester.mention,   inline=True)
        embed.add_field(name="👤 Channel",    value=song.uploader,            inline=True)
        embed.add_field(name="📋 In Queue",   value=str(len(self.queue)),     inline=True)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.set_footer(text=f"🎶 {self.guild.name}")
        return embed


# ═════════════════════════════════════════════════════════════════════════════
# Now Playing Buttons View
# ═════════════════════════════════════════════════════════════════════════════
class NowPlayingView(discord.ui.View):
    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.secondary, custom_id="np_pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("❌ مش في channel!", ephemeral=True)
        if vc.is_playing():
            vc.pause()
            button.label = "▶️ Resume"
            button.style = discord.ButtonStyle.success
        elif vc.is_paused():
            vc.resume()
            button.label = "⏸ Pause"
            button.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.primary, custom_id="np_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.skip()
        await interaction.response.send_message("⏭ Skipped!", delete_after=3)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="np_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            self.player.queue.clear()
            self.player.loop_mode = "none"
            vc.stop()
            await vc.disconnect()
        await interaction.response.send_message("⏹ Stopped & disconnected.", delete_after=5)

    @discord.ui.button(label="🔉 Vol-", style=discord.ButtonStyle.secondary, custom_id="np_vol_down")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.source:
            new_vol = max(0.0, self.player.volume - 0.1)
            self.player.volume = new_vol
            vc.source.volume   = new_vol
            await interaction.response.send_message(f"🔉 Volume: **{int(new_vol*100)}%**", delete_after=3)
        else:
            await interaction.response.send_message("❌ مش شغال حاجة.", ephemeral=True)

    @discord.ui.button(label="🔊 Vol+", style=discord.ButtonStyle.secondary, custom_id="np_vol_up")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.source:
            new_vol = min(2.0, self.player.volume + 0.1)
            self.player.volume = new_vol
            vc.source.volume   = new_vol
            await interaction.response.send_message(f"🔊 Volume: **{int(new_vol*100)}%**", delete_after=3)
        else:
            await interaction.response.send_message("❌ مش شغال حاجة.", ephemeral=True)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.secondary, custom_id="np_loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modes = ["none", "song", "queue"]
        idx   = modes.index(self.player.loop_mode)
        self.player.loop_mode = modes[(idx + 1) % 3]
        labels = {"none": "🔁 Loop: Off", "song": "🔂 Loop: Song", "queue": "🔁 Loop: Queue"}
        button.label = labels[self.player.loop_mode]
        await interaction.response.edit_message(view=self)


# ═════════════════════════════════════════════════════════════════════════════
# Bot Setup
# ═════════════════════════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states    = True

bot     = commands.Bot(command_prefix="!", intents=intents, help_command=None)
players: dict[int, MusicPlayer] = {}


def get_player(ctx: commands.Context) -> MusicPlayer:
    gid = ctx.guild.id
    if gid not in players:
        players[gid] = MusicPlayer(ctx)
    return players[gid]


async def ensure_voice(ctx: commands.Context) -> bool:
    if not ctx.author.voice:
        await ctx.send("❌ انت لازم تكون في voice channel الأول!")
        return False
    vc = ctx.guild.voice_client
    if not vc:
        await ctx.author.voice.channel.connect()
    elif vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Events
# ═════════════════════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"✅ Bot connected: {bot.user} ({bot.user.id})")
    print(f"   Servers: {len(bot.guilds)}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="!play | 🎵")
    )


# ═════════════════════════════════════════════════════════════════════════════
# Commands
# ═════════════════════════════════════════════════════════════════════════════

@bot.command(name="play", aliases=["p"])
async def play(ctx: commands.Context, *, query: str):
    """شغّل أغنية باسمها أو رابطها"""
    if not await ensure_voice(ctx):
        return
    async with ctx.typing():
        try:
            song = await Song.from_query(query, ctx.author, bot.loop)
        except Exception as e:
            return await ctx.send(f"❌ حصل خطأ: `{e}`")

    player = get_player(ctx)
    vc     = ctx.guild.voice_client

    if vc and (vc.is_playing() or vc.is_paused()):
        player.add(song)
        embed = discord.Embed(
            title="📋 Added to Queue",
            description=f"[{song.title}]({song.webpage_url})",
            color=0x3498db,
        )
        embed.add_field(name="⏱ Duration", value=song.duration_str)
        embed.add_field(name="📋 Position", value=f"#{len(player.queue)}")
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        await ctx.send(embed=embed)
    else:
        player.add(song)
        await player.play_next()


@bot.command(name="skip", aliases=["s"])
async def skip(ctx: commands.Context):
    """اتخطى الأغنية الحالية"""
    player = get_player(ctx)
    if not ctx.guild.voice_client or not ctx.guild.voice_client.is_playing():
        return await ctx.send("❌ مفيش أغنية شغّالة دلوقتي.")
    player.skip()
    await ctx.send("⏭ Skipped!", delete_after=5)


@bot.command(name="pause")
async def pause(ctx: commands.Context):
    """وقّف الأغنية مؤقتاً"""
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸ Paused.", delete_after=5)
    else:
        await ctx.send("❌ مفيش أغنية شغّالة.")


@bot.command(name="resume", aliases=["r"])
async def resume(ctx: commands.Context):
    """استكمل الأغنية"""
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Resumed.", delete_after=5)
    else:
        await ctx.send("❌ مفيش حاجة موقوفة.")


@bot.command(name="stop", aliases=["dc", "leave"])
async def stop(ctx: commands.Context):
    """وقّف الموسيقى واطلع من الـ channel"""
    player = get_player(ctx)
    player.queue.clear()
    player.loop_mode = "none"
    vc = ctx.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    await ctx.send("⏹ Stopped & left the channel.", delete_after=5)
    players.pop(ctx.guild.id, None)


@bot.command(name="volume", aliases=["vol", "v"])
async def volume(ctx: commands.Context, vol: int):
    """غيّر مستوى الصوت (0-200)"""
    if not 0 <= vol <= 200:
        return await ctx.send("❌ الصوت لازم يكون بين 0 و200.")
    player = get_player(ctx)
    player.volume = vol / 100
    vc = ctx.guild.voice_client
    if vc and vc.source:
        vc.source.volume = player.volume
    await ctx.send(f"🔊 Volume set to **{vol}%**", delete_after=5)


@bot.command(name="loop", aliases=["l"])
async def loop_cmd(ctx: commands.Context, mode: str = None):
    """غيّر وضع الـ Loop (none / song / queue)"""
    player = get_player(ctx)
    modes  = ["none", "song", "queue"]
    if mode is None:
        mode = modes[(modes.index(player.loop_mode) + 1) % 3]
    elif mode.lower() not in modes:
        return await ctx.send("❌ الوضع لازم يكون: `none`, `song`, أو `queue`")
    else:
        mode = mode.lower()
    player.loop_mode = mode
    emojis = {"none": "▶️ Loop Off", "song": "🔂 Loop Song", "queue": "🔁 Loop Queue"}
    await ctx.send(emojis[mode], delete_after=5)


@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx: commands.Context, page: int = 1):
    """اعرض قائمة الانتظار"""
    player = get_player(ctx)
    items_per_page = 10

    if not player.queue and not player.current:
        return await ctx.send("📋 القائمة فاضية.")

    queue_list = list(player.queue)
    pages      = max(1, (len(queue_list) + items_per_page - 1) // items_per_page)
    page       = max(1, min(page, pages))
    start      = (page - 1) * items_per_page
    end        = start + items_per_page

    embed = discord.Embed(title="📋 Queue", color=0x9b59b6)

    if player.current:
        embed.add_field(
            name="🎵 Now Playing",
            value=f"[{player.current.title}]({player.current.webpage_url}) `{player.current.duration_str}`",
            inline=False,
        )

    if queue_list:
        lines = []
        for i, song in enumerate(queue_list[start:end], start=start + 1):
            lines.append(f"`{i}.` [{song.title}]({song.webpage_url}) `{song.duration_str}`")
        embed.add_field(name=f"⏳ Up Next (Page {page}/{pages})", value="\n".join(lines), inline=False)

    loop_labels = {"none": "▶️ Off", "song": "🔂 Song", "queue": "🔁 Queue"}
    embed.set_footer(
        text=f"🔄 Loop: {loop_labels[player.loop_mode]}  |  🔊 {int(player.volume*100)}%  |  📋 {len(queue_list)} songs"
    )
    await ctx.send(embed=embed)


@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying(ctx: commands.Context):
    """اعرض الأغنية اللي شغّالة دلوقتي"""
    player = get_player(ctx)
    if not player.current:
        return await ctx.send("❌ مفيش أغنية شغّالة دلوقتي.")
    embed = player._build_np_embed()
    view  = NowPlayingView(player)
    await ctx.send(embed=embed, view=view)


@bot.command(name="shuffle")
async def shuffle(ctx: commands.Context):
    """اخلط القائمة عشوائياً"""
    player = get_player(ctx)
    if len(player.queue) < 2:
        return await ctx.send("❌ القائمة محتاجة أكتر من أغنية للخلط.")
    player.shuffle()
    await ctx.send("🔀 Queue shuffled!", delete_after=5)


@bot.command(name="clear", aliases=["cl"])
async def clear(ctx: commands.Context):
    """امسح قائمة الانتظار"""
    player = get_player(ctx)
    player.queue.clear()
    await ctx.send("🗑️ Queue cleared!", delete_after=5)


@bot.command(name="remove", aliases=["rm"])
async def remove(ctx: commands.Context, index: int):
    """شيل أغنية من القائمة برقمها"""
    player = get_player(ctx)
    if index < 1 or index > len(player.queue):
        return await ctx.send(f"❌ الرقم لازم يكون بين 1 و{len(player.queue)}.")
    q       = list(player.queue)
    removed = q.pop(index - 1)
    player.queue = deque(q)
    await ctx.send(f"🗑️ Removed: **{removed.title}**", delete_after=5)


@bot.command(name="lyrics", aliases=["ly"])
async def lyrics(ctx: commands.Context, *, song_name: str = None):
    """جيب كلمات أغنية"""
    if not genius_client:
        return await ctx.send(
            "❌ Genius API مش مفعّل.\n"
            "ضيف `GENIUS_TOKEN` في Railway Environment Variables من: https://genius.com/api-clients"
        )
    player = get_player(ctx)
    query  = song_name or (player.current.title if player.current else None)
    if not query:
        return await ctx.send("❌ اكتب اسم الأغنية أو شغّل أغنية أول.")

    async with ctx.typing():
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: genius_client.search_song(query)
            )
        except Exception as e:
            return await ctx.send(f"❌ حصل خطأ: `{e}`")

    if not result:
        return await ctx.send(f"❌ مش لاقي كلمات لـ: **{query}**")

    lyrics_text = result.lyrics[:3900]
    embed = discord.Embed(
        title=f"🎤 {result.title}",
        description=f"```\n{lyrics_text}\n```",
        color=0xf1c40f,
        url=result.url,
    )
    embed.set_footer(text=f"via Genius | {result.artist.name}")
    await ctx.send(embed=embed)


@bot.command(name="help", aliases=["h"])
async def help_cmd(ctx: commands.Context):
    """اعرض الأوامر المتاحة"""
    embed = discord.Embed(
        title="🎵 Music Bot — الأوامر",
        description="Prefix: `!`",
        color=0x1DB954,
    )
    cmds = [
        ("!play [name/url]", "شغّل أغنية أو ضيفها للقائمة"),
        ("!skip / !s",       "اتخطى الأغنية الحالية"),
        ("!pause",           "وقّف مؤقتاً"),
        ("!resume / !r",     "استكمل"),
        ("!stop",            "وقّف واطلع من الـ channel"),
        ("!volume [0-200]",  "غيّر مستوى الصوت"),
        ("!loop [mode]",     "Loop: none / song / queue"),
        ("!queue [page]",    "اعرض قائمة الانتظار"),
        ("!nowplaying",      "اعرض الأغنية الحالية + أزرار"),
        ("!shuffle",         "اخلط القائمة"),
        ("!clear",           "امسح القائمة"),
        ("!remove [#]",      "شيل أغنية من القائمة"),
        ("!lyrics [name]",   "جيب كلمات الأغنية"),
    ]
    for cmd, desc in cmds:
        embed.add_field(name=f"`{cmd}`", value=desc, inline=False)
    embed.set_footer(text="🎶 Made with ❤️ using discord.py")
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ ناقصك: `{error.param.name}`")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"❌ خطأ: `{error}`")
        raise error


# ═════════════════════════════════════════════════════════════════════════════
# Run
# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("🚀 Starting Discord Music Bot...")
    bot.run(TOKEN)
