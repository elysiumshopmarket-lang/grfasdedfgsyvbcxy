import os
import random
import logging
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("buschi")

TZ = ZoneInfo("Europe/Berlin")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
VERSE_CHANNEL_ID = int(os.getenv("VERSE_CHANNEL_ID", "0"))
VOICE_CHANNEL_ID = int(os.getenv("VOICE_NOTIFY_CHANNEL_ID", "0"))
VOICE_COOLDOWN_MIN = int(os.getenv("VOICE_COOLDOWN_MIN", "10"))

VERSES = {
    "bibel": [
        {"ref": "1. Korinther 13:13", "text": "Nun aber bleibet Glaube, Hoffnung, Liebe, diese drei; aber die Liebe ist die größte unter ihnen."},
        {"ref": "Johannes 3:16", "text": "Also hat Gott die Welt geliebt, daß er seinen eingeborenen Sohn gab, auf daß alle, die an ihn glauben, nicht verloren werden, sondern das ewige Leben haben."},
        {"ref": "Philipper 4:13", "text": "Ich vermag alles durch den, der mich mächtig macht, Christus."},
        {"ref": "Römer 8:28", "text": "Wir wissen aber, daß denen, die Gott lieben, alle Dinge zum Besten dienen, denen, die nach dem Vorsatz berufen sind."},
        {"ref": "Matthäus 11:28", "text": "Kommet her zu mir, alle, die ihr mühselig und beladen seid; ich will euch erquicken."},
        {"ref": "Johannes 14:27", "text": "Den Frieden lasse ich euch, meinen Frieden gebe ich euch; nicht gebe ich euch, wie die Welt gibt. Euer Herz erschrecke nicht und fürchte sich nicht."},
        {"ref": "Matthäus 5:9", "text": "Selig sind die Friedfertigen; denn sie werden Gottes Kinder heißen."},
        {"ref": "Römer 12:12", "text": "Seid fröhlich in Hoffnung, geduldig in Trübsal, haltet an am Gebet."},
        {"ref": "Hebräer 11:1", "text": "Es ist aber der Glaube eine gewisse Zuversicht des, das man hoffet, und nicht zweifeln an dem, das man nicht siehet."},
        {"ref": "Matthäus 7:7", "text": "Bittet, so wird euch gegeben; suchet, so werdet ihr finden; klopfet an, so wird euch aufgetan."},
        {"ref": "2. Timotheus 1:7", "text": "Denn Gott hat uns nicht gegeben den Geist der Furcht, sondern der Kraft und der Liebe und der Zucht."},
        {"ref": "1. Petrus 5:7", "text": "Alle eure Sorge werfet auf ihn; denn er sorgt für euch."},
        {"ref": "Matthäus 6:34", "text": "Darum sorget nicht für den andern Morgen; denn der morgige Tag wird für das Seine sorgen. Es ist genug, daß ein jeglicher Tag seine eigene Plage habe."},
    ],
    "tanach": [
        {"ref": "Jesaja 41:10", "text": "Fürchte dich nicht, ich bin mit dir; weiche nicht, denn ich bin dein Gott; ich stärke dich, ich helfe dir auch."},
        {"ref": "Josua 1:9", "text": "Siehe, ich habe dir geboten, daß du getrost und freudig seiest. Laß dir nicht grauen und entsetze dich nicht; denn der HERR, dein Gott, ist mit dir in allem, was du tun wirst."},
        {"ref": "Psalm 23:1", "text": "Der HERR ist mein Hirte, mir wird nichts mangeln."},
        {"ref": "Psalm 121:1-2", "text": "Ich hebe meine Augen auf zu den Bergen, von welchen mir Hilfe kommt. Meine Hilfe kommt von dem HERRN, der Himmel und Erde gemacht hat."},
        {"ref": "Sprüche 3:5", "text": "Verlaß dich auf den HERRN von ganzem Herzen, und verlaß dich nicht auf deinen Verstand."},
        {"ref": "Psalm 118:24", "text": "Dies ist der Tag, den der HERR macht; laßt uns freuen und fröhlich darinnen sein."},
        {"ref": "Micha 6:8", "text": "Es ist dir gesagt, Mensch, was gut ist und was der HERR von dir fordert, nämlich Gottes Wort halten und Liebe üben und demütig sein vor deinem Gott."},
        {"ref": "Psalm 27:1", "text": "Der HERR ist mein Licht und mein Heil; vor wem sollte ich mich fürchten? Der HERR ist meines Lebens Kraft; vor wem sollte mir grauen?"},
        {"ref": "Jesaja 40:31", "text": "Aber die auf den HERRN harren, kriegen neue Kraft, daß sie auffahren mit Flügeln wie Adler, daß sie laufen und nicht matt werden, daß sie wandeln und nicht müde werden."},
        {"ref": "5. Mose 31:6", "text": "Seid getrost und unverzagt, fürchtet euch nicht und laßt euch nicht vor ihnen grauen; denn der HERR, dein Gott, wird selber mit dir wandeln und wird die Hand nicht abtun noch dich verlassen."},
        {"ref": "Prediger 3:1", "text": "Ein jegliches hat seine Zeit, und alles Vornehmen unter dem Himmel hat seine Stunde."},
        {"ref": "Psalm 46:2", "text": "Gott ist unsre Zuversicht und Stärke, eine Hilfe in den großen Nöten, die uns getroffen haben."},
        {"ref": "Psalm 37:5", "text": "Befiehl dem HERRN deine Wege und hoffe auf ihn, er wird's wohl machen."},
    ],
}

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

last_voice_ping: dict[int, datetime] = {}


# ---------------------------------------------------------------- Verse
def day_index(d: date, offset: int, length: int) -> int:
    return (d.toordinal() + offset) % length


def bible_embed(d: date) -> discord.Embed:
    v = VERSES["bibel"][day_index(d, 0, len(VERSES["bibel"]))]
    e = discord.Embed(title="Bibelvers des Tages", description=f"*{v['text']}*", color=0xF1C40F)
    e.add_field(name="\u200b", value=f"**• {v['ref']}**", inline=False)
    e.set_footer(text=f"Lutherbibel 1912 • {d:%d.%m.%Y}")
    return e


def tanach_embed(d: date) -> discord.Embed:
    v = VERSES["tanach"][day_index(d, 7, len(VERSES["tanach"]))]
    e = discord.Embed(title="Tanach-Vers des Tages", description=f"*{v['text']}*", color=0x3498DB)
    e.add_field(name="\u200b", value=f"**• {v['ref']}**", inline=False)
    e.set_footer(text=f"Hebräische Bibel (Lutherbibel 1912) • {d:%d.%m.%Y}")
    return e


async def quran_embed(d: date) -> discord.Embed:
    n = (d.toordinal() * 37) % 6236 + 1
    url = f"https://api.alquran.cloud/v1/ayah/{n}/editions/quran-uthmani,de.bubenheim"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = (await r.json())["data"]
        arabic, german = data[0], data[1]
        desc = f"{arabic['text']}\n\n*{german['text']}*"
        ref = f"Sure {arabic['surah']['number']} ({arabic['surah']['englishName']}), Vers {arabic['numberInSurah']}"
    except Exception as ex:
        log.warning("Quran-API Fehler: %s", ex)
        desc = "*Heute konnte der Koranvers nicht geladen werden.*"
        ref = "—"
    e = discord.Embed(title="Koranvers des Tages", description=desc, color=0x2ECC71)
    e.add_field(name="\u200b", value=f"**• {ref}**", inline=False)
    e.set_footer(text=f"Vers des Tages • {d:%d.%m.%Y}")
    return e


async def all_verse_embeds(d: date) -> list[discord.Embed]:
    return [bible_embed(d), await quran_embed(d), tanach_embed(d)]


@tasks.loop(time=dtime(hour=0, minute=0, tzinfo=TZ))
async def daily_verse():
    if not VERSE_CHANNEL_ID:
        return
    channel = bot.get_channel(VERSE_CHANNEL_ID)
    if channel is None:
        log.warning("VERSE_CHANNEL_ID nicht gefunden")
        return
    embeds = await all_verse_embeds(datetime.now(TZ).date())
    await channel.send("Vers des Tages", embeds=embeds)


@bot.tree.command(name="vers", description="Zeigt den Vers des Tages")
@app_commands.describe(quelle="Welcher Vers? (Standard: alle drei)")
@app_commands.choices(quelle=[
    app_commands.Choice(name="Alle", value="alle"),
    app_commands.Choice(name="Bibel", value="bibel"),
    app_commands.Choice(name="Koran", value="koran"),
    app_commands.Choice(name="Tanach", value="tanach"),
])
async def vers(interaction: discord.Interaction, quelle: app_commands.Choice[str] | None = None):
    await interaction.response.defer()
    d = datetime.now(TZ).date()
    q = quelle.value if quelle else "alle"
    if q == "bibel":
        embeds = [bible_embed(d)]
    elif q == "koran":
        embeds = [await quran_embed(d)]
    elif q == "tanach":
        embeds = [tanach_embed(d)]
    else:
        embeds = await all_verse_embeds(d)
    await interaction.followup.send("Vers des Tages", embeds=embeds)


# ---------------------------------------------------------------- Voice
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or not VOICE_CHANNEL_ID:
        return
    if after.channel is None or before.channel == after.channel:
        return
    if len(after.channel.members) != 1:
        return

    now = datetime.now(TZ)
    last = last_voice_ping.get(after.channel.id)
    if last and (now - last).total_seconds() < VOICE_COOLDOWN_MIN * 60:
        return
    last_voice_ping[after.channel.id] = now

    target = bot.get_channel(VOICE_CHANNEL_ID)
    if target is None:
        return
    e = discord.Embed(
        title="Voice Aktiv",
        description=(
            f"**! ! {member.display_name} ist im Call!**\n\n"
            f"**Kanal:** 🔊 • {after.channel.name}\n"
            f"**Beigetreten:** {now:%H:%M} Uhr\n\n"
            "Der Voice-Chat ist jetzt aktiv!"
        ),
        color=0x2ECC71,
    )
    e.set_thumbnail(url=member.display_avatar.url)
    e.set_footer(text="Voice Notification")
    await target.send(
        "@everyone", embed=e,
        allowed_mentions=discord.AllowedMentions(everyone=True),
    )


# ---------------------------------------------------------------- Extras
@bot.tree.command(name="wuerfel", description="Würfelt für dich")
@app_commands.describe(seiten="Anzahl der Seiten (Standard 6)", anzahl="Wie viele Würfel (Standard 1)")
async def wuerfel(interaction: discord.Interaction,
                  seiten: app_commands.Range[int, 2, 1000] = 6,
                  anzahl: app_commands.Range[int, 1, 20] = 1):
    rolls = [random.randint(1, seiten) for _ in range(anzahl)]
    text = f"🎲 {interaction.user.display_name} würfelt **{anzahl}d{seiten}**: " + ", ".join(map(str, rolls))
    if anzahl > 1:
        text += f"  (Summe: **{sum(rolls)}**)"
    await interaction.response.send_message(text)


@bot.tree.command(name="muenze", description="Münzwurf")
async def muenze(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 {random.choice(['Kopf', 'Zahl'])}!")


@bot.tree.command(name="entscheide", description="Entscheidet zwischen Optionen (mit Komma trennen)")
@app_commands.describe(optionen="z.B. Pizza, Döner, Sushi")
async def entscheide(interaction: discord.Interaction, optionen: str):
    opts = [o.strip() for o in optionen.split(",") if o.strip()]
    if len(opts) < 2:
        await interaction.response.send_message("Gib mir mindestens 2 Optionen, getrennt mit Komma.", ephemeral=True)
        return
    await interaction.response.send_message(f"🤔 Ich entscheide: **{random.choice(opts)}**")


@bot.tree.command(name="wer", description="Wählt zufällig jemanden aus dem Server aus")
async def wer(interaction: discord.Interaction):
    members = [m for m in interaction.guild.members if not m.bot]
    await interaction.response.send_message(f"👉 {random.choice(members).mention}")


NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


@bot.tree.command(name="umfrage", description="Erstellt eine Umfrage (bis 5 Optionen, mit Komma trennen)")
@app_commands.describe(frage="Die Frage", optionen="z.B. Ja, Nein, Vielleicht")
async def umfrage(interaction: discord.Interaction, frage: str, optionen: str):
    opts = [o.strip() for o in optionen.split(",") if o.strip()][:5]
    if len(opts) < 2:
        await interaction.response.send_message("Mindestens 2 Optionen, getrennt mit Komma.", ephemeral=True)
        return
    e = discord.Embed(title=f"📊 {frage}", description="\n".join(f"{NUM[i]} {o}" for i, o in enumerate(opts)),
                      color=0x5865F2)
    e.set_footer(text=f"Umfrage von {interaction.user.display_name}")
    await interaction.response.send_message(embed=e)
    msg = await interaction.original_response()
    for i in range(len(opts)):
        await msg.add_reaction(NUM[i])


@bot.tree.command(name="ping", description="Zeigt die Latenz des Bots")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! {round(bot.latency * 1000)} ms")


# ---------------------------------------------------------------- Start
@bot.event
async def setup_hook():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    daily_verse.start()


@bot.event
async def on_ready():
    log.info("Eingeloggt als %s", bot.user)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/vers"))


bot.run(TOKEN)
