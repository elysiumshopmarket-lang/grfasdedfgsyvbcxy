"""
Discord Call Bot  <->  MicroSIP

Der Bot läuft auf DEINEM Windows-PC (dort, wo MicroSIP läuft) und verbindet
den Discord-Sprachkanal per virtuellen Audiokabeln mit einem MicroSIP-Anruf.

    Discord-Nutzer --> Bot --> CABLE-B Input ==> MicroSIP-Mikrofon  --> Telefon
    Telefon --> MicroSIP-Lautsprecher ==> CABLE-A Output --> Bot --> Discord

Befehle: /panel   /call <nummer>   /answer   /hangup   /status
Geräteliste anzeigen:  python call_bot.py --devices
"""

import asyncio
import os
import re
import subprocess
import sys
import threading
import time

import discord
import numpy as np
import sounddevice as sd
from aiohttp import web
from discord import app_commands
from discord.ext import voice_recv
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
NOTIFY_CHANNEL_ID = int(os.getenv("NOTIFY_CHANNEL_ID", "0") or 0)
MICROSIP = os.getenv("MICROSIP_PATH", r"C:\Program Files (x86)\MicroSIP\microsip.exe")
# Name (Teilstring) der Audiogeräte, siehe --devices
SPEAKER_DEVICE = os.getenv("SPEAKER_DEVICE", "CABLE-A Output")  # hier kommt MicroSIPs Ton raus
MIC_DEVICE = os.getenv("MIC_DEVICE", "CABLE-B Input")           # hier schreibt der Bot für MicroSIP rein
HOOK_PORT = int(os.getenv("HOOK_PORT", "8765"))
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}

RATE = 48000
CHANNELS = 2
FRAME_BYTES = 3840                 # 20 ms, 48 kHz, stereo, int16
MAX_BUF = RATE * CHANNELS * 2 // 2  # max. 0,5 s Puffer, damit die Latenz nicht wächst
NUMBER_RE = re.compile(r"^[+0-9*#]{3,24}$")   # verhindert Fremd-Argumente an MicroSIP

BLURPLE = discord.Color.from_rgb(88, 101, 242)


# ----------------------------------------------------------------------------
# Audio
# ----------------------------------------------------------------------------
def find_device(name, kind):
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d[key] > 0:
            return i
    raise RuntimeError(f"Audiogerät '{name}' nicht gefunden (python call_bot.py --devices)")


class Mixer:
    """Sammelt PCM der Discord-Sprecher und mischt sie zu einem Strom."""

    def __init__(self):
        self.buffers = {}
        self.lock = threading.Lock()

    def push(self, uid, pcm):
        with self.lock:
            b = self.buffers.setdefault(uid, bytearray())
            b += pcm
            if len(b) > MAX_BUF:
                del b[: len(b) - MAX_BUF]

    def pull(self, nbytes):
        acc = np.zeros(nbytes // 2, dtype=np.int32)
        with self.lock:
            for b in self.buffers.values():
                chunk = bytes(b[:nbytes])
                del b[:nbytes]
                if chunk:
                    arr = np.frombuffer(chunk, dtype=np.int16)
                    acc[: len(arr)] += arr
        return np.clip(acc, -32768, 32767).astype(np.int16).tobytes()


class Bridge:
    def __init__(self):
        self.mixer = Mixer()
        self.in_buf = bytearray()
        self.in_lock = threading.Lock()
        self.out_stream = None
        self.in_stream = None
        self.muted = False      # Discord -> Telefon stumm
        self.volume = 1.0       # Lautstärke Telefon -> Discord

    def _out_cb(self, outdata, frames, time, status):
        pcm = self.mixer.pull(frames * CHANNELS * 2)
        if self.muted:
            pcm = b"\x00" * len(pcm)
        outdata[:] = np.frombuffer(pcm, dtype=np.int16).reshape(-1, CHANNELS)

    def _in_cb(self, indata, frames, time, status):
        with self.in_lock:
            self.in_buf += indata.tobytes()
            if len(self.in_buf) > MAX_BUF:
                del self.in_buf[: len(self.in_buf) - MAX_BUF]

    def take(self, nbytes):
        with self.in_lock:
            chunk = bytes(self.in_buf[:nbytes])
            del self.in_buf[:nbytes]
        return chunk + b"\x00" * (nbytes - len(chunk))

    def start(self):
        self.out_stream = sd.OutputStream(
            device=find_device(MIC_DEVICE, "output"), samplerate=RATE, channels=CHANNELS,
            dtype="int16", blocksize=960, callback=self._out_cb)
        self.in_stream = sd.InputStream(
            device=find_device(SPEAKER_DEVICE, "input"), samplerate=RATE, channels=CHANNELS,
            dtype="int16", blocksize=960, callback=self._in_cb)
        self.out_stream.start()
        self.in_stream.start()

    def stop(self):
        for s in (self.out_stream, self.in_stream):
            try:
                if s:
                    s.stop()
                    s.close()
            except Exception:
                pass


class CableSource(discord.AudioSource):
    """MicroSIP-Ton (vom virtuellen Kabel) -> Discord."""

    def __init__(self, bridge):
        self.bridge = bridge

    def read(self):
        data = self.bridge.take(FRAME_BYTES)
        v = self.bridge.volume
        if v != 1.0:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.int32)
            data = np.clip(arr * v, -32768, 32767).astype(np.int16).tobytes()
        return data

    def is_opus(self):
        return False


class MixSink(voice_recv.AudioSink):
    """Discord-Sprecher -> Mixer -> MicroSIP-Mikrofon."""

    def __init__(self, mixer):
        super().__init__()
        self.mixer = mixer

    def wants_opus(self):
        return False

    def write(self, user, data):
        if user is None or getattr(user, "bot", False) or not data.pcm:
            return
        self.mixer.push(user.id, data.pcm)

    def cleanup(self):
        pass


# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------
def microsip(*args):
    subprocess.Popen([MICROSIP, *args])


class CallBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.vc = None
        self.bridge = None
        self.panel = None
        self.incoming_msg = None

    async def setup_hook(self):
        # Hauptpanel bleibt auch nach einem Neustart des Bots bedienbar
        self.add_view(MainPanel())
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        # kleiner lokaler Webserver für MicroSIP-Events (eingehender Anruf / Anruf beendet)
        app = web.Application()
        app.router.add_get("/incoming", self.h_incoming)
        app.router.add_get("/ended", self.h_ended)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", HOOK_PORT).start()

    async def on_ready(self):
        print(f"Eingeloggt als {self.user}")
        if not ALLOWED:
            print("WARNUNG: ALLOWED_USER_IDS ist leer - niemand darf Befehle nutzen!")

    # --- MicroSIP-Hooks ---
    async def h_incoming(self, request):
        ch = self.get_channel(NOTIFY_CHANNEL_ID) if NOTIFY_CHANNEL_ID else None
        if ch and not self.vc:
            e = discord.Embed(title="📞 Eingehender Anruf",
                              description="Geh in einen Sprachkanal und klick auf **Annehmen**.",
                              color=discord.Color.green())
            self.incoming_msg = await ch.send(embed=e, view=IncomingView())
        return web.Response(text="ok")

    async def h_ended(self, request):
        await self.end_call()
        msg, self.incoming_msg = self.incoming_msg, None
        if msg:
            try:
                await msg.edit(content="📵 Anruf verpasst / beendet.", embed=None, view=None)
            except Exception:
                pass
        return web.Response(text="ok")

    # --- Anruf-Logik ---
    async def join_and_bridge(self, member):
        if self.vc:
            return "Es läuft schon ein Anruf."
        if not member.voice or not member.voice.channel:
            return "Geh zuerst in einen Sprachkanal."
        bridge = Bridge()
        try:
            bridge.start()
        except Exception as e:
            bridge.stop()
            return f"Audio-Fehler: {e}"
        try:
            vc = await member.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as e:
            bridge.stop()
            return f"Konnte dem Sprachkanal nicht beitreten: {e}"
        vc.listen(MixSink(bridge.mixer))
        vc.play(CableSource(bridge))
        self.vc, self.bridge = vc, bridge
        return None

    async def end_call(self):
        vc, bridge, panel = self.vc, self.bridge, self.panel
        self.vc = self.bridge = self.panel = None
        if bridge:
            bridge.stop()
        if vc:
            try:
                await vc.disconnect()
            except Exception:
                pass
        if panel:
            await panel.finish()


def fmt_dur(sec):
    m, sec = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class AllowedView(discord.ui.View):
    """Nur Nutzer aus ALLOWED_USER_IDS dürfen Buttons drücken."""

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id in ALLOWED:
            return True
        await interaction.response.send_message("Dafür hast du keine Berechtigung.", ephemeral=True)
        return False


class CallPanel(AllowedView):
    def __init__(self, label, channel):
        super().__init__(timeout=None)
        self.label = label
        self.channel = channel
        self.started = int(time.time())
        self.ended = None
        self.message = None
        self._sync()

    def _sync(self):
        muted = bool(client.bridge and client.bridge.muted)
        self.mute_btn.label = "Stumm aus" if muted else "Stumm"
        self.mute_btn.emoji = "🔇" if muted else "🎤"
        self.mute_btn.style = discord.ButtonStyle.primary if muted else discord.ButtonStyle.secondary

    def embed(self):
        done = self.ended is not None
        e = discord.Embed(title="📴 Anruf beendet" if done else "📞 Anruf läuft",
                          color=discord.Color.dark_grey() if done else discord.Color.green())
        e.add_field(name="Nummer", value=f"`{self.label}`")
        e.add_field(name="Dauer" if done else "Gestartet",
                    value=fmt_dur(self.ended - self.started) if done else f"<t:{self.started}:R>")
        e.add_field(name="Kanal", value=self.channel.mention)
        b = client.bridge
        if not done and b:
            e.add_field(name="Mikro (Discord → Telefon)", value="🔇 stumm" if b.muted else "🎤 an")
            e.add_field(name="Lautstärke (Telefon)", value=f"{int(b.volume * 100)} %")
        return e

    async def finish(self):
        self.ended = int(time.time())
        for c in self.children:
            c.disabled = True
        self.stop()
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except Exception:
            pass

    @discord.ui.button(label="Auflegen", emoji="📴", style=discord.ButtonStyle.danger, row=0)
    async def hangup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        microsip("/hangupall")
        await client.end_call()

    @discord.ui.button(label="Stumm", emoji="🎤", style=discord.ButtonStyle.secondary, row=0)
    async def mute_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if client.bridge:
            client.bridge.muted = not client.bridge.muted
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Leiser", emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if client.bridge:
            client.bridge.volume = max(0.25, round(client.bridge.volume - 0.25, 2))
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Lauter", emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        if client.bridge:
            client.bridge.volume = min(2.0, round(client.bridge.volume + 0.25, 2))
        await interaction.response.edit_message(embed=self.embed(), view=self)


class IncomingView(AllowedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Annehmen", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        err = await client.join_and_bridge(interaction.user)
        if err:
            return await interaction.followup.send(f"❌ {err}", ephemeral=True)
        microsip("/answer")
        client.incoming_msg = None
        panel = CallPanel("Eingehender Anruf", client.vc.channel)
        panel.message = interaction.message
        client.panel = panel
        await interaction.edit_original_response(embed=panel.embed(), view=panel)

    @discord.ui.button(label="Ablehnen", emoji="❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        microsip("/hangupall")
        client.incoming_msg = None
        await interaction.response.edit_message(content="❌ Anruf abgelehnt.", embed=None, view=None)


client = CallBot()


def allowed(interaction):
    return interaction.user.id in ALLOWED


async def open_panel(interaction, label):
    """Postet das Anruf-Panel (normale Nachricht, damit sie dauerhaft editierbar bleibt)."""
    if client.incoming_msg:
        try:
            await client.incoming_msg.edit(content="✅ Angenommen.", embed=None, view=None)
        except Exception:
            pass
        client.incoming_msg = None
    panel = CallPanel(label, client.vc.channel)
    panel.message = await interaction.channel.send(embed=panel.embed(), view=panel)
    client.panel = panel


async def start_call(interaction, nummer):
    """Gemeinsame Anruf-Logik. Die Interaction muss schon defer(ephemeral=True) sein."""
    nummer = nummer.replace(" ", "").replace("-", "")
    if not NUMBER_RE.match(nummer):
        return await interaction.followup.send("❌ Ungültige Nummer.", ephemeral=True)
    err = await client.join_and_bridge(interaction.user)
    if err:
        return await interaction.followup.send(f"❌ {err}", ephemeral=True)
    microsip(nummer)
    await open_panel(interaction, nummer)
    await interaction.followup.send(f"📞 Rufe **{nummer}** an …", ephemeral=True)


# ----------------------------------------------------------------------------
# Hauptpanel (dauerhaftes Bedienfeld)
# ----------------------------------------------------------------------------
def main_embed():
    active = bool(client.vc)
    e = discord.Embed(
        title="☎️  Telefon-Zentrale",
        description="Steuere deine MicroSIP-Anrufe direkt aus Discord.\n"
                    "Geh in einen **Sprachkanal** und nutze die Buttons unten.",
        color=discord.Color.green() if active else BLURPLE,
    )
    e.add_field(name="Status", value="🟢 Anruf aktiv" if active else "⚪ Bereit", inline=True)
    e.add_field(name="Kanal", value=client.vc.channel.mention if active else "—", inline=True)
    e.add_field(name="Befehle", value="`/call` · `/answer` · `/hangup` · `/status`", inline=False)
    e.set_footer(text="Nur freigegebene Nutzer können Buttons verwenden")
    return e


class NumberModal(discord.ui.Modal, title="📞 Nummer anrufen"):
    nummer = discord.ui.TextInput(label="Telefonnummer", placeholder="+491701234567",
                                  min_length=3, max_length=24)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await start_call(interaction, str(self.nummer))


class MainPanel(AllowedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Anrufen", emoji="📞", style=discord.ButtonStyle.success,
                       custom_id="panel:call", row=0)
    async def call_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NumberModal())

    @discord.ui.button(label="Annehmen", emoji="✅", style=discord.ButtonStyle.primary,
                       custom_id="panel:answer", row=0)
    async def answer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        err = await client.join_and_bridge(interaction.user)
        if err:
            return await interaction.followup.send(f"❌ {err}", ephemeral=True)
        microsip("/answer")
        await open_panel(interaction, "Eingehender Anruf")
        await interaction.followup.send("✅ Anruf angenommen.", ephemeral=True)

    @discord.ui.button(label="Auflegen", emoji="📴", style=discord.ButtonStyle.danger,
                       custom_id="panel:hangup", row=0)
    async def hangup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        microsip("/hangupall")
        await client.end_call()
        await interaction.response.send_message("📴 Aufgelegt.", ephemeral=True)

    @discord.ui.button(label="Status aktualisieren", emoji="🔄", style=discord.ButtonStyle.secondary,
                       custom_id="panel:refresh", row=1)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=main_embed(), view=self)


@client.tree.command(name="panel", description="Das Telefon-Bedienfeld posten")
@app_commands.guild_only()
async def panel_cmd(interaction: discord.Interaction):
    if not allowed(interaction):
        return await interaction.response.send_message("Dafür hast du keine Berechtigung.", ephemeral=True)
    await interaction.response.send_message(embed=main_embed(), view=MainPanel())


# ----------------------------------------------------------------------------
# Slash-Befehle
# ----------------------------------------------------------------------------
@client.tree.command(name="call", description="Eine Telefonnummer über MicroSIP anrufen")
@app_commands.describe(nummer="z.B. +491701234567")
@app_commands.guild_only()
async def call(interaction: discord.Interaction, nummer: str):
    if not allowed(interaction):
        return await interaction.response.send_message("Dafür hast du keine Berechtigung.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await start_call(interaction, nummer)


@client.tree.command(name="answer", description="Eingehenden MicroSIP-Anruf annehmen")
@app_commands.guild_only()
async def answer(interaction: discord.Interaction):
    if not allowed(interaction):
        return await interaction.response.send_message("Dafür hast du keine Berechtigung.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    err = await client.join_and_bridge(interaction.user)
    if err:
        return await interaction.followup.send(f"❌ {err}", ephemeral=True)
    microsip("/answer")
    await open_panel(interaction, "Eingehender Anruf")
    await interaction.followup.send("✅ Anruf angenommen.", ephemeral=True)


@client.tree.command(name="hangup", description="Anruf beenden")
@app_commands.guild_only()
async def hangup(interaction: discord.Interaction):
    if not allowed(interaction):
        return await interaction.response.send_message("Dafür hast du keine Berechtigung.", ephemeral=True)
    microsip("/hangupall")
    await client.end_call()
    await interaction.response.send_message("📴 Aufgelegt.", ephemeral=True)


@client.tree.command(name="status", description="Zeigt, ob gerade ein Anruf läuft")
async def status(interaction: discord.Interaction):
    msg = "📞 Anruf aktiv." if client.vc else "Kein Anruf aktiv."
    await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if "--devices" in sys.argv:
        print(sd.query_devices())
        sys.exit()
    if not TOKEN:
        sys.exit("DISCORD_TOKEN fehlt in der .env")
    client.run(TOKEN)
