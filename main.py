import os
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import traceback
import threading
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask

# ======================================================
# 🔹 環境変数読み込み
# ======================================================
env_path = os.path.join(os.path.dirname(__file__), ".env")

if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    print("⚠️ .env ファイルが見つかりません。")

TOKEN = os.getenv("TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
GLOBAL_LOG_CHANNEL_ID = int(os.getenv("GLOBAL_LOG_CHANNEL_ID", 0))
DISCORD_CHANNEL = os.getenv("DISCORD_CHANNEL")

guild_id_env = os.getenv("MY_GUILD_ID")
MY_GUILD_ID = int(guild_id_env) if guild_id_env else 1503705749606367294

if not TOKEN:
    raise RuntimeError("❌ .env に TOKEN が設定されていません。")

# ======================================================
# 🔹 Flask KeepAlive
# ======================================================
PORT = int(os.getenv("PORT", 3000))

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is online", 200

@app.route("/health")
def health():
    return "OK", 200

def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )

def keep_alive():
    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )
    flask_thread.start()

# ======================================================
# 🔹 BOT 初期化
# ======================================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

# ======================================================
# 🔹 管理者チェック（Slash用）
# ======================================================
def is_admin():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.guild_permissions.administrator:
            return True

        raise app_commands.CheckFailure(
            "このコマンドは管理者のみ使用できます。"
        )

    return app_commands.check(predicate)

# ======================================================
# 🔹 Cog 自動ロード
# ======================================================
async def load_all_cogs():

    print("===================================")
    print("🧩 --- Cog 自動ロード開始 ---")

    base_dir = os.path.join(
        os.path.dirname(__file__),
        "cogs"
    )

    if not os.path.exists(base_dir):
        os.makedirs(base_dir)

    for file in os.listdir(base_dir):

        if not file.endswith(".py"):
            continue

        if file.startswith("__"):
            continue

        cog_name = file[:-3]

        try:
            await bot.load_extension(
                f"cogs.{cog_name}"
            )

            print(
                f"✅ Loaded: cogs.{cog_name}"
            )

        except commands.errors.NoEntryPointError:

            print(
                f"⚠ setup() が無いためスキップ: {cog_name}"
            )

        except discord.ClientException as e:

            print(
                f"⚠ 既にロード済み: {cog_name} ({e})"
            )

        except Exception:

            print(f"❌ Failed: {cog_name}")
            traceback.print_exc()

    print("🧩 --- Cog 自動ロード完了 ---")

# ======================================================
# 🔹 グローバルログ送信
# ======================================================
async def send_global_log(embed: discord.Embed):

    if GLOBAL_LOG_CHANNEL_ID:

        channel = bot.get_channel(
            GLOBAL_LOG_CHANNEL_ID
        )

        if channel:
            await channel.send(embed=embed)

@bot.event
async def on_guild_join(guild: discord.Guild):

    embed = discord.Embed(
        title="🟢 Botが新しいサーバーに参加しました！",
        description=(
            f"**サーバー名:** {guild.name}\n"
            f"**ID:** `{guild.id}`\n"
            f"**メンバー数:** `{guild.member_count}`"
        ),
        color=discord.Color.green(),
        timestamp=datetime.utcnow(),
    )

    await send_global_log(embed)

@bot.event
async def on_guild_remove(guild: discord.Guild):

    embed = discord.Embed(
        title="🔴 Botがサーバーから退出しました",
        description=(
            f"**サーバー名:** {guild.name}\n"
            f"**ID:** `{guild.id}`"
        ),
        color=discord.Color.red(),
        timestamp=datetime.utcnow(),
    )

    await send_global_log(embed)

# ======================================================
# 🔹 ステータス更新
# ======================================================
async def update_activity():

    await bot.wait_until_ready()

    while not bot.is_closed():

        await bot.change_presence(
            activity=discord.Game(
                "にゃんこ大戦争自動代行"
            ),
            status=discord.Status.online
        )

        await asyncio.sleep(3600)

# ======================================================
# 🔹 setup_hook
# ======================================================
@bot.event
async def setup_hook():

    await load_all_cogs()

    try:

        synced = await bot.tree.sync()

        print(
            f"🌳 グローバルコマンド同期完了！({len(synced)}件)"
        )

        if MY_GUILD_ID:

            MY_GUILD = discord.Object(
                id=MY_GUILD_ID
            )

            guild_synced = await bot.tree.sync(
                guild=MY_GUILD
            )

            print(
                f"🏰 管理サーバー専用コマンド同期完了！({len(guild_synced)}件)"
            )

    except Exception as e:

        print(
            f"⚠️ スラッシュコマンド同期エラー: {e}"
        )

    print("===================================")

# ======================================================
# 🔹 Ready
# ======================================================
@bot.event
async def on_ready():

    print("===================================")
    print(f"Bot logged in as {bot.user}")
    print("===================================")

    if not any(
        task.get_name() == "update_activity"
        for task in asyncio.all_tasks()
    ):

        bot.loop.create_task(
            update_activity(),
            name="update_activity"
        )

    if DISCORD_CHANNEL:

        channel = bot.get_channel(
            int(DISCORD_CHANNEL)
        )

        if channel:

            embed = discord.Embed(
                title="🚀 Bot起動完了",
                description=(
                    f"{bot.user} がオンラインになりました！"
                ),
                color=discord.Color.green(),
                timestamp=datetime.utcnow(),
            )

            await channel.send(embed=embed)

# ======================================================
# 🔹 エラーハンドラ
# ======================================================
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(
        error,
        app_commands.CheckFailure
    ):

        if not interaction.response.is_done():

            await interaction.response.send_message(
                "🚫 権限がありません。",
                ephemeral=True
            )

        return

    print(f"⚠️ スラッシュコマンドエラー: {error}")
    traceback.print_exc()

# ======================================================
# 🔹 手動sync
# ======================================================
@bot.command()
@commands.is_owner()
async def sync(ctx):

    await bot.tree.sync()

    if MY_GUILD_ID:

        MY_GUILD = discord.Object(
            id=MY_GUILD_ID
        )

        await bot.tree.sync(
            guild=MY_GUILD
        )

    await ctx.send(
        "✅ スラッシュコマンドを再同期しました。"
    )

    print("🔄 手動スラッシュコマンド同期完了。")

# ======================================================
# 🔹 起動
# ======================================================
if __name__ == "__main__":

    keep_alive()

    try:

        bot.run(TOKEN)

    except KeyboardInterrupt:

        print("\n🛑 手動停止されました。")

    except Exception:

        traceback.print_exc()