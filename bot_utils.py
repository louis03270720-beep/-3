import discord

def create_success_embed(title="成功", description=""):
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.green()
    )
    return embed

def create_error_embed(title="エラー", description=""):
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red()
    )
    return embed

def create_warning_embed(title="注意", description=""):
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.orange()
    )
    return embed