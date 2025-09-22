import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import threading
from flask import Flask, request, jsonify
import requests
import asyncio
import os

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("Missing DISCORD_TOKEN in environment variables!")

DATABASE_PATH = "data.sqlite"

# --- Database Setup ---
db = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS links (
    discord_id TEXT PRIMARY KEY,
    roblox_id TEXT,
    verified INTEGER DEFAULT 0
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    group_id TEXT,
    role_map TEXT,
    nickname_format TEXT DEFAULT "{username}"
)
""")
db.commit()

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Helper Functions ---
def getRobloxUserIdByUsername(username: str):
    try:
        r = requests.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [username], "excludeBannedUsers": True})
        data = r.json()
        if data.get("data") and len(data["data"]):
            return {"id": str(data["data"][0]["id"]), "name": data["data"][0]["name"]}
    except:
        return None
    return None

async def sync_member(member):
    c = db.cursor()
    c.execute("SELECT roblox_id FROM links WHERE discord_id=? AND verified=1", (str(member.id),))
    row = c.fetchone()
    if not row:
        return
    roblox_id = row[0]

    # Fetch Roblox groups
    r = requests.get(f"https://groups.roblox.com/v2/users/{roblox_id}/groups/roles")
    groups = r.json().get("data", []) if r.status_code == 200 else []

    c.execute("SELECT group_id, role_map, nickname_format FROM guild_settings WHERE guild_id=?", (str(member.guild.id),))
    settings = c.fetchone()
    if not settings:
        return

    group_id, role_map, nickname_format = settings
    role_map = eval(role_map) if role_map else {}

    if group_id:
        user_rank = None
        for g in groups:
            if str(g['group']['id']) == str(group_id):
                user_rank = g['role']['name']
                break

        if user_rank and role_map.get(user_rank):
            role_id = int(role_map[user_rank])
            role = member.guild.get_role(role_id)
            if role:
                await member.add_roles(role)

    # Set nickname
    username = requests.get(f"https://users.roblox.com/v1/users/{roblox_id}").json().get("name", "RobloxUser")
    try:
        await member.edit(nick=nickname_format.format(username=username, id=roblox_id))
    except:
        pass

# --- Slash Commands ---
@bot.tree.command(name="link")
async def link(interaction: discord.Interaction, username: str):
    user = getRobloxUserIdByUsername(username)
    if not user:
        await interaction.response.send_message("❌ Roblox user not found.", ephemeral=True)
        return
    c = db.cursor()
    c.execute("INSERT OR REPLACE INTO links (discord_id, roblox_id, verified) VALUES (?, ?, 0)", (str(interaction.user.id), user['id']))
    db.commit()
    await interaction.response.send_message(f"✅ Your Roblox account `{user['name']}` is pending verification. Join the verification game to finish linking!", ephemeral=True)

@bot.tree.command(name="unlink")
async def unlink(interaction: discord.Interaction):
    c = db.cursor()
    c.execute("DELETE FROM links WHERE discord_id=?", (str(interaction.user.id),))
    db.commit()
    await interaction.response.send_message("✅ Your Roblox account has been unlinked.", ephemeral=True)

@bot.tree.command(name="setup_group")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_group(interaction: discord.Interaction, groupid: str):
    c = db.cursor()
    c.execute("INSERT OR REPLACE INTO guild_settings (guild_id, group_id, role_map) VALUES (?, ?, ?)", (str(interaction.guild.id), groupid, "{}"))
    db.commit()
    await interaction.response.send_message(f"✅ Group ID set to {groupid}", ephemeral=True)

@bot.tree.command(name="map_role")
@app_commands.checks.has_permissions(manage_guild=True)
async def map_role(interaction: discord.Interaction, rank: str, role: discord.Role):
    c = db.cursor()
    c.execute("SELECT role_map FROM guild_settings WHERE guild_id=?", (str(interaction.guild.id),))
    row = c.fetchone()
    role_map = eval(row[0]) if row and row[0] else {}
    role_map[rank] = str(role.id)
    c.execute("UPDATE guild_settings SET role_map=? WHERE guild_id=?", (str(role_map), str(interaction.guild.id)))
    db.commit()
    await interaction.response.send_message(f"✅ Mapped rank `{rank}` to role {role.mention}", ephemeral=True)

@bot.tree.command(name="list_mappings")
@app_commands.checks.has_permissions(manage_guild=True)
async def list_mappings(interaction: discord.Interaction):
    c = db.cursor()
    c.execute("SELECT role_map FROM guild_settings WHERE guild_id=?", (str(interaction.guild.id),))
    row = c.fetchone()
    if not row or not row[0]:
        await interaction.response.send_message("No mappings found.", ephemeral=True)
        return
    role_map = eval(row[0])
    mapping_str = "\\n".join([f"`{rank}` → <@&{role_id}>" for rank, role_id in role_map.items()])
    await interaction.response.send_message(f"**Current Mappings:**\\n{mapping_str}", ephemeral=True)

@bot.tree.command(name="nickname_format")
@app_commands.checks.has_permissions(manage_guild=True)
async def nickname_format(interaction: discord.Interaction, format: str):
    c = db.cursor()
    c.execute("UPDATE guild_settings SET nickname_format=? WHERE guild_id=?", (format, str(interaction.guild.id)))
    db.commit()
    await interaction.response.send_message(f"✅ Nickname format updated to `{format}`", ephemeral=True)

@bot.tree.command(name="sync_user")
@app_commands.checks.has_permissions(manage_guild=True)
async def sync_user(interaction: discord.Interaction, member: discord.Member):
    await sync_member(member)
    await interaction.response.send_message(f"✅ Synced {member.mention}", ephemeral=True)

@bot.tree.command(name="resync_all")
@app_commands.checks.has_permissions(manage_guild=True)
async def resync_all(interaction: discord.Interaction):
    for member in interaction.guild.members:
        await sync_member(member)
    await interaction.response.send_message("✅ Resynced all members.", ephemeral=True)

# --- Flask API ---
app = Flask(__name__)

@app.route('/verify', methods=['POST'])
def verify_webhook():
    data = request.json
    discord_id = data.get('discord_id')
    roblox_id = data.get('roblox_id')
    if not discord_id or not roblox_id:
        return jsonify({"error": "missing data"}), 400
    c = db.cursor()
    c.execute("UPDATE links SET verified=1 WHERE discord_id=?", (discord_id,))
    db.commit()

    async def do_sync():
        for guild in bot.guilds:
            member = guild.get_member(int(discord_id))
            if member:
                await sync_member(member)
    asyncio.run_coroutine_threadsafe(do_sync(), bot.loop)
    return jsonify({"status": "ok"})

@app.route('/pending', methods=['GET'])
def pending():
    c = db.cursor()
    c.execute("SELECT discord_id, roblox_id FROM links WHERE verified=0")
    rows = c.fetchall()
    return jsonify({row[1]: row[0] for row in rows})

def run_flask():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_flask).start()

@bot.event
async def on_ready():
    print(f"Bot ready as {bot.user}")
    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Failed to sync commands:", e)

bot.run(TOKEN)
