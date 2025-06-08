# -*- coding: utf-8 -*-
"""
逆おみくじBot（/gyaku コマンド, MCP＋GPT＋DB履歴＋1パネル維持＋押下権限制御, ボタン絵文字なし版, グローバル同期のみ）
"""

import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
import asyncpg
from openai import AsyncOpenAI
from datetime import datetime, timedelta, timezone

# ────────── 環境変数 ──────────
TOKEN      = os.getenv("DISCORD_BOT_TOKEN")
DB_URL     = os.getenv("DATABASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

ADMIN_IDS = {802807293070278676, 822460191118721034}
JST = timezone(timedelta(hours=9))
openai_client = AsyncOpenAI(api_key=OPENAI_KEY)

# ────────── ボタン並び・ラベル ──────────
BUTTON_LAYOUT = [
    ["大吉", "吉"],
    ["中吉", "小吉", "末吉"],
    ["凶", "大凶"],
    ["鯖の女神降臨", "救いようがない日"]
]

# ────────── DB ──────────
db_pool: asyncpg.Pool = None

async def get_omikuji_result(user_id: int, date: datetime.date):
    async with db_pool.acquire() as conn:
        rec = await conn.fetchrow(
            "SELECT result FROM omikuji_results WHERE user_id=$1 AND date=$2",
            user_id, date
        )
        return rec['result'] if rec else None

async def save_gyaku_history(user_id: int, date: datetime.date, result: str, gpt_text: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO gyaku_omikuji_history(user_id, date, result, gpt_text) VALUES($1,$2,$3,$4)",
            user_id, date, result, gpt_text
        )

# ────────── 胡散臭い関西弁占い師プロンプト ──────────
def build_gpt_prompt(result: str, user_name: str):
    return (
        "あなたは胡散臭い関西弁の占い師です。\n"
        f"{user_name}さんが今日「{result}」を引いたと報告してきました。\n"
        "まるで最初からそれを知っていたかのように、神秘的かつ胡散臭い関西弁で“予言”してください。\n"
        "煽り・自信満々・おせっかい、どれでもOKです。\n"
        "必ず関西弁で、語尾や雰囲気もそれっぽく。"
    )

async def generate_gpt_text(user_id: int, user_name: str, result: str) -> str:
    try:
        rsp = await openai_client.chat.completions.create(
            model="gpt-4o",
            user=str(user_id),
            messages=[
                {"role": "system", "content": "あなたは胡散臭い関西弁の占い師です。必ず関西弁で、神秘的に話してください。"},
                {"role": "user",   "content": build_gpt_prompt(result, user_name)}
            ],
            max_tokens=120,
            temperature=1.0,
        )
        return rsp.choices[0].message.content.strip()
    except Exception as e:
        print(f"GPT Error: {e}")
        return "…あれ？今日はちょっとだけ未来が見えへんかったわ！"

# ────────── Discord Bot ──────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

gyaku_panel_msg_id = None

async def delete_old_panel(channel: discord.TextChannel):
    global gyaku_panel_msg_id
    if gyaku_panel_msg_id:
        try:
            msg = await channel.fetch_message(gyaku_panel_msg_id)
            await msg.delete()
        except Exception:
            pass
    gyaku_panel_msg_id = None

# ────────── ボタンView（絵文字なし） ──────────
class GyakuOmikujiView(discord.ui.View):
    def __init__(self, today_omikuji: dict, invoker_id: int):
        super().__init__(timeout=None)
        self.today_omikuji = today_omikuji
        self.invoker_id = invoker_id
        for row in BUTTON_LAYOUT:
            for label in row:
                self.add_item(GyakuOmikujiButton(label, today_omikuji, invoker_id))

class GyakuOmikujiButton(discord.ui.Button):
    def __init__(self, label: str, today_omikuji: dict, invoker_id: int):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"gyaku_{label}"
        )
        self.label_val = label
        self.today_omikuji = today_omikuji
        self.invoker_id = invoker_id

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        today = datetime.now(JST).date()
        result = await get_omikuji_result(user_id, today)
        is_admin = user_id in ADMIN_IDS
        if self.label_val in ("鯖の女神降臨", "救いようがない日"):
            if not (is_admin or (result == self.label_val)):
                await interaction.response.send_message(
                    "今日はまだその運勢は引いてへんで！まず本家おみくじで当ててから押してや。",
                    ephemeral=True)
                return
        else:
            if not (is_admin or (result == self.label_val)):
                await interaction.response.send_message(
                    f"今日は「{self.label_val}」は引いてへんみたいやで！まずおみくじで引いてきてな。",
                    ephemeral=True)
                return

        channel = interaction.channel
        await delete_old_panel(channel)

        user_name = interaction.user.display_name
        gpt_text = await generate_gpt_text(user_id, user_name, self.label_val)

        embed = discord.Embed(
            title=f"🔮 逆おみくじ予言：{self.label_val}",
            description=gpt_text,
            color=discord.Color.purple(),
            timestamp=datetime.now(JST)
        )
        embed.set_footer(text=f"by 胡散臭い関西弁の占い師")
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        view = GyakuOmikujiView(self.today_omikuji, self.invoker_id)
        new_msg = await channel.send(embed=make_panel_embed(), view=view)
        global gyaku_panel_msg_id
        gyaku_panel_msg_id = new_msg.id

        await channel.send(embed=embed)
        await save_gyaku_history(user_id, today, self.label_val, gpt_text)

def make_panel_embed():
    embed = discord.Embed(
        title="🌀 逆おみくじパネル",
        description=(
            "今日本家おみくじで引いた運勢を押してや！\n\n"
            "　[大吉] [吉]\n"
            "　[中吉] [小吉] [末吉]\n"
            "　[凶] [大凶]\n"
            "　[鯖の女神降臨] [救いようがない日]\n"
            "\n"
            "※鯖の女神降臨/救いようがない日は本当に引いた人 or 管理者のみ押せます"
        ),
        color=discord.Color.purple()
    )
    embed.set_footer(text="本家おみくじBotで運勢を引いてから押してね！")
    return embed

# ────────── コマンド（/gyaku）───────────
@tree.command(name="gyaku", description="逆おみくじパネルを出す（管理者専用）")
async def gyaku_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id not in ADMIN_IDS:
        await interaction.response.send_message("このコマンドは管理者専用です。", ephemeral=True)
        return

    today = datetime.now(JST).date()
    async with db_pool.acquire() as conn:
        recs = await conn.fetch("SELECT user_id, result FROM omikuji_results WHERE date=$1", today)
    today_omikuji = {r['user_id']: r['result'] for r in recs}

    await delete_old_panel(interaction.channel)
    panel_embed = make_panel_embed()
    view = GyakuOmikujiView(today_omikuji, user_id)
    msg = await interaction.channel.send(embed=panel_embed, view=view)
    global gyaku_panel_msg_id
    gyaku_panel_msg_id = msg.id

    await interaction.response.send_message("逆おみくじパネルを設置したで！", ephemeral=True)

# ────────── SETUP/起動処理 ──────────
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | ID: {bot.user.id}")
    try:
        await tree.sync()  # グローバル同期のみ
        print("グローバルコマンド同期完了")
    except Exception as e:
        print(f"[ERROR] コマンド同期失敗: {e}")

@bot.event
async def setup_hook():
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DB_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS gyaku_omikuji_history (
                user_id BIGINT,
                date DATE,
                result TEXT,
                gpt_text TEXT
            )
        ''')

if __name__ == "__main__":
    bot.run(TOKEN)
