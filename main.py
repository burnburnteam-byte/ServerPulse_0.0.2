"""
Bot Discord -> API Falix (v2) -> Serveur Falix
Commandes slash : /start, /stop, /status

Doc officielle : https://client.falixnodes.net/profile/apidocs
  - Auth   : Authorization: Bearer <FALIX_API_KEY>
  - Power  : POST /servers/{server_id}/power   body {"signal": "start" | "stop"}  -> 202
  - Status : GET  /servers/{server_id}/status  -> data.state (offline/starting/running/stopping)
  - Infos  : GET  /servers/{server_id}         -> data.name
  - Joueurs: GET  /servers/{server_id}/players/online -> data.online_players
             GET  /servers/{server_id}/players/max    -> data.max_players
"""

import asyncio
import logging
import os

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()  # utile en local ; sur Railway les variables sont déjà dans l'environnement

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FALIX_API_KEY = os.getenv("FALIX_API_KEY")
FALIX_SERVER_ID = os.getenv("FALIX_SERVER_ID")

missing = [n for n, v in {
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "FALIX_API_KEY": FALIX_API_KEY,
    "FALIX_SERVER_ID": FALIX_SERVER_ID,
}.items() if not v]
if missing:
    raise SystemExit(f"Variables d'environnement manquantes : {', '.join(missing)}")

API_BASE = "https://client.falixnodes.net/api/v2"
SERVER_PATH = f"/servers/{FALIX_SERVER_ID.strip()}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("falix-bot")

COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.red()
COLOR_INFO = discord.Color.blurple()
COLOR_WARN = discord.Color.orange()

STATE_LABELS = {
    "running": "🟢 Online",
    "starting": "🟡 Démarrage en cours",
    "stopping": "🟠 Arrêt en cours",
    "offline": "🔴 Offline",
}


class FalixError(Exception):
    """Erreur renvoyée par l'API Falix : {"error": {"code", "message", ...}}."""

    def __init__(self, status: int, code: str, message: str, action_url: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.action_url = action_url


# --------------------------------------------------------------------------- #
#  Client API Falix
# --------------------------------------------------------------------------- #
class FalixAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: aiohttp.ClientSession | None = None

    async def open(self):
        self.session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            async with self.session.request(method, API_BASE + path, json=json) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = None

                if resp.status >= 400:
                    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
                    if resp.status == 429:
                        retry = resp.headers.get("Retry-After", "?")
                        msg = f"Trop de requêtes, réessaie dans {retry} s."
                    else:
                        msg = err.get("message") or f"Erreur HTTP {resp.status}"
                    raise FalixError(resp.status, err.get("code", "http_error"), msg, err.get("action_url"))

                return (body or {}).get("data", {}) if isinstance(body, dict) else {}
        except aiohttp.ClientError as e:
            raise FalixError(0, "network_error", f"Impossible de joindre l'API Falix : {e}")
        except asyncio.TimeoutError:
            raise FalixError(0, "timeout", "L'API Falix n'a pas répondu à temps.")

    async def get_status(self) -> dict:
        return await self.request("GET", f"{SERVER_PATH}/status")

    async def get_server(self) -> dict:
        return await self.request("GET", SERVER_PATH)

    async def get_online_players(self) -> dict:
        return await self.request("GET", f"{SERVER_PATH}/players/online")

    async def get_max_players(self) -> dict:
        return await self.request("GET", f"{SERVER_PATH}/players/max")

    async def power(self, signal: str) -> dict:
        return await self.request("POST", f"{SERVER_PATH}/power", json={"signal": signal})


falix = FalixAPI(FALIX_API_KEY)


# --------------------------------------------------------------------------- #
#  Helpers Embeds
# --------------------------------------------------------------------------- #
def make_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="Falix Server Manager")
    return embed


def error_embed(err: FalixError) -> discord.Embed:
    hints = {
        401: "Clé API invalide ou manquante (vérifie `FALIX_API_KEY`).",
        403: "La clé API n'a pas les permissions/scopes nécessaires.",
        404: "Serveur introuvable (vérifie `FALIX_SERVER_ID`, c'est un ID numérique).",
    }
    desc = f"**{err.message}**"
    if err.status in hints:
        desc += f"\n{hints[err.status]}"
    if err.action_url:
        desc += f"\n\n👉 Action requise : {err.action_url}"
    embed = make_embed("❌ Erreur API Falix", desc, COLOR_ERR)
    embed.add_field(name="Code", value=f"`{err.code}` (HTTP {err.status})", inline=False)
    return embed


def power_result_text(data: dict) -> str:
    state = data.get("state")
    queue = data.get("queue")
    if state == "queued" and queue:
        return f"Ajouté à la file de démarrage (position **{queue.get('position', '?')}** / {queue.get('length', '?')})."
    if state == "transferring":
        return "Le serveur est en cours de transfert vers un nœud, il démarrera ensuite."
    return "Signal envoyé au serveur."


# --------------------------------------------------------------------------- #
#  Bot Discord
# --------------------------------------------------------------------------- #
class FalixBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await falix.open()
        synced = await self.tree.sync()  # sync global des slash commands
        log.info("%d commande(s) slash synchronisée(s).", len(synced))

    async def close(self):
        await falix.close()
        await super().close()

    async def on_ready(self):
        log.info("Connecté en tant que %s (id %s)", self.user, self.user.id)


bot = FalixBot()


@bot.tree.command(name="start", description="Démarre le serveur Falix")
async def start_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        state = (await falix.get_status()).get("state")
        if state == "running":
            await interaction.followup.send(embed=make_embed(
                "🟢 Déjà en ligne", "Le serveur est déjà démarré.", COLOR_INFO))
            return
        if state == "starting":
            await interaction.followup.send(embed=make_embed(
                "🟡 Déjà en démarrage", "Le serveur est déjà en train de démarrer.", COLOR_INFO))
            return

        data = await falix.power("start")
        await interaction.followup.send(embed=make_embed(
            "🚀 Démarrage en cours", f"Le serveur est en cours de démarrage.\n{power_result_text(data)}", COLOR_OK))
    except FalixError as e:
        log.warning("/start : %s (%s)", e.message, e.code)
        await interaction.followup.send(embed=error_embed(e))


@bot.tree.command(name="stop", description="Arrête le serveur Falix")
async def stop_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        state = (await falix.get_status()).get("state")
        if state == "offline":
            await interaction.followup.send(embed=make_embed(
                "🔴 Déjà arrêté", "Le serveur est déjà arrêté.", COLOR_INFO))
            return
        if state == "stopping":
            await interaction.followup.send(embed=make_embed(
                "🟠 Déjà en arrêt", "Le serveur est déjà en train de s'arrêter.", COLOR_INFO))
            return

        await falix.power("stop")
        await interaction.followup.send(embed=make_embed(
            "🛑 Arrêt demandé", "L'arrêt du serveur a été demandé.", COLOR_WARN))
    except FalixError as e:
        log.warning("/stop : %s (%s)", e.message, e.code)
        await interaction.followup.send(embed=error_embed(e))


@bot.tree.command(name="status", description="Affiche le statut du serveur Falix")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        status = await falix.get_status()
    except FalixError as e:
        await interaction.followup.send(embed=error_embed(e))
        return

    # Infos secondaires : on ne bloque pas si elles échouent (ex : serveur non-Minecraft)
    server, online, maxp = await asyncio.gather(
        falix.get_server(), falix.get_online_players(), falix.get_max_players(),
        return_exceptions=True,
    )

    state = status.get("state", "offline")
    is_online = state == "running"
    name = server.get("name") if isinstance(server, dict) else None

    if isinstance(online, dict) and is_online:
        players = str(online.get("online_players", 0))
    elif not is_online:
        players = "0"
    else:
        players = "N/A"
    max_players = str(maxp.get("max_players", "N/A")) if isinstance(maxp, dict) else "N/A"

    embed = discord.Embed(
        title=f"📊 {name}" if name else "📊 Statut du serveur",
        color=COLOR_OK if is_online else (COLOR_WARN if state in ("starting", "stopping") else COLOR_ERR),
    )
    embed.add_field(name="État", value=STATE_LABELS.get(state, f"❔ {state}"), inline=False)
    embed.add_field(name="👥 Joueurs connectés", value=players, inline=True)
    embed.add_field(name="👥 Joueurs max", value=max_players, inline=True)
    if not status.get("node_ready", True):
        embed.add_field(name="⚠️ Nœud", value="Le nœud Falix ne répond pas actuellement.", inline=False)
    embed.set_footer(text="Falix Server Manager")
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
