import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta
import os
from flask import Flask
from threading import Thread

# --- Flask keep-alive ---
app = Flask('')

@app.route('/')
def home():
    return "I'm alive!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- Discord bot setup ---
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=None, intents=intents)
vc_category_name = "Voice Channels"
user_temp_channels = {}  # user_id: channel_id
channel_owners = {}      # channel_id: user_id
channel_expiry = {}      # channel_id: datetime

@bot.event
async def on_ready():
    print(f"Bot ready as {bot.user}")
    try:
        await bot.tree.sync()
        print("Commands synced globally.")
    except Exception as e:
        print(f"Global sync failed: {e}")
    cleanup_empty_channels.start()

# --- Setup command ---
@bot.tree.command(name="setup", description="Create the 'Join to Create' system.")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    guild = interaction.guild
    category = discord.utils.get(guild.categories, name=vc_category_name)
    if not category:
        category = await guild.create_category(vc_category_name)

    existing = discord.utils.get(category.voice_channels, name="Join to Create")
    if not existing:
        await category.create_voice_channel("Join to Create")
        await interaction.response.send_message("Setup complete!", ephemeral=True)
    else:
        await interaction.response.send_message("'Join to Create' already exists.", ephemeral=True)

# --- Auto Temp VC Creation ---
@bot.event
async def on_voice_state_update(member, before, after):
    if after.channel and after.channel.name == "Join to Create":
        category = after.channel.category
        channel_name = f"{member.display_name}'s channel"
        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(connect=True),
            member: discord.PermissionOverwrite(manage_channels=True, connect=True),
        }
        new_channel = await category.create_voice_channel(channel_name, overwrites=overwrites)
        await member.move_to(new_channel)
        user_temp_channels[member.id] = new_channel.id
        channel_owners[new_channel.id] = member.id
        print(f"Created channel: {channel_name} for {member.display_name}")

# --- Auto Delete Empty Channels ---
@tasks.loop(seconds=30)
async def cleanup_empty_channels():
    for user_id, channel_id in list(user_temp_channels.items()):
        channel = bot.get_channel(channel_id)
        if not channel or len(channel.members) == 0:
            await channel.delete(reason="Temporary VC auto-deletion (empty)")
            user_temp_channels.pop(user_id, None)
            channel_owners.pop(channel_id, None)
            channel_expiry.pop(channel_id, None)
            print(f"Deleted empty temp channel: {channel_id}")

# --- Voice Channel Command Group ---
vc_group = app_commands.Group(name="vc", description="Voice channel controls")

@vc_group.command(name="lock", description="Lock your voice channel.")
async def vc_lock(interaction: discord.Interaction):
    await modify_channel_permission(interaction, connect=False, message="Channel locked.")

@vc_group.command(name="permit", description="Allow a user or role to join your VC.")
@app_commands.describe(target="User or role to allow")
async def vc_permit(interaction: discord.Interaction, target: discord.Member | discord.Role):
    await modify_channel_permission(interaction, target=target, connect=True,
                                     message=f"{target.mention} is now permitted.")

@vc_group.command(name="reject", description="Block a user or role from your VC.")
@app_commands.describe(target="User or role to block")
async def vc_reject(interaction: discord.Interaction, target: discord.Member | discord.Role):
    await modify_channel_permission(interaction, target=target, connect=False,
                                     message=f"{target.mention} is now blocked.")

@vc_group.command(name="rename", description="Rename your voice channel.")
@app_commands.describe(name="New name for the channel")
async def vc_rename(interaction: discord.Interaction, name: str):
    channel = await get_owned_channel(interaction)
    if channel:
        await channel.edit(name=name)
        await interaction.response.send_message(f"Channel renamed to **{name}**", ephemeral=True)

@vc_group.command(name="expire", description="Auto-delete your channel after X minutes.")
@app_commands.describe(minutes="Minutes until the channel deletes itself")
async def vc_expire(interaction: discord.Interaction, minutes: int):
    channel = await get_owned_channel(interaction)
    if channel:
        expire_time = datetime.utcnow() + timedelta(minutes=minutes)
        channel_expiry[channel.id] = expire_time
        await interaction.response.send_message(f"Channel will delete in {minutes} minutes.", ephemeral=True)

@vc_group.command(name="transfer", description="Transfer ownership of your VC to another member.")
@app_commands.describe(new_owner="The member to transfer ownership to")
async def vc_transfer(interaction: discord.Interaction, new_owner: discord.Member):
    channel = await get_owned_channel(interaction)
    if channel:
        old_owner_id = interaction.user.id
        user_temp_channels.pop(old_owner_id, None)
        user_temp_channels[new_owner.id] = channel.id
        channel_owners[channel.id] = new_owner.id

        await channel.set_permissions(new_owner, manage_channels=True)
        await channel.set_permissions(interaction.user, manage_channels=False)

        await interaction.response.send_message(f"Ownership transferred to {new_owner.mention}", ephemeral=True)

# --- Utility Functions ---
async def get_owned_channel(interaction: discord.Interaction):
    user_id = interaction.user.id
    channel_id = user_temp_channels.get(user_id)
    if not channel_id:
        await interaction.response.send_message("You don't own a temporary channel.", ephemeral=True)
        return None
    channel = interaction.guild.get_channel(channel_id)
    if not channel:
        await interaction.response.send_message("Channel not found.", ephemeral=True)
        return None
    return channel

async def modify_channel_permission(interaction, target=None, connect=False, message=""):
    channel = await get_owned_channel(interaction)
    if channel:
        if not target:
            target = interaction.guild.default_role
        await channel.set_permissions(target, connect=connect)
        await interaction.response.send_message(message, ephemeral=True)

# --- Register Command Group ---
bot.tree.add_command(vc_group)

# --- Purge Command ---
@bot.tree.command(name="purge", description="Delete a number of messages from the current channel.")
@app_commands.describe(amount="The number of messages to delete (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("You can only delete between 1 and 100 messages.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)  # Acknowledge the command
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)

# --- Start ---
keep_alive()
bot.run(os.getenv("TOKEN"))
