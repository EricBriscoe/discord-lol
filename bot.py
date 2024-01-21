import asyncio
from datetime import timedelta
import json
import logging
import os


import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import pytz

import lol
from db import get_cursor

load_dotenv()


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
intents = discord.Intents.all()
bot = discord.Bot()

GAME_LOG_CHANNEL_ID = int(os.environ.get("GAME_LOG_CHANNEL_ID"))
GUILD_ID = int(os.environ.get("GUILD_ID"))


class LolCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.save_matches.start()
        self.save_match_details.start()
        self.post_match_details.start()

    @tasks.loop(seconds=200)
    async def save_matches(self):
        lol.backfill_matches()
        await asyncio.sleep(15)
        lol.forwardfill_matches()

    @tasks.loop(seconds=8)
    async def save_match_details(self):
        lol.save_match_details()

    @tasks.loop(seconds=30)
    async def post_match_details(self):
        logging.info("Searching for matches to post")
        channel = self.bot.get_channel(GAME_LOG_CHANNEL_ID)

        with get_cursor() as c:
            c.execute(
                """
                SELECT 
                  ai.name, 
                  pmi.puuid, 
                  pmi.matchId, 
                  pmi.matchInfo
                FROM 
                    player_match_info pmi
                JOIN account_info ai
                  ON ai.puuid = pmi.puuid
                WHERE
                    pmi.gameStartTimestamp = (
                        SELECT MIN(gameStartTimestamp)
                        FROM player_match_info
                        WHERE puuid = pmi.puuid 
                        AND gameStartTimestamp > now() - interval '1 day'
                        AND posted = FALSE
                    )
                ORDER BY pmi.gameStartTimestamp asc;
                """
            )
            logging.info(f"Found {c.rowcount} matches to post")

            for name, puuid, matchId, matchInfo in c.fetchall():
                logging.info(f"Posting match {matchId} for {name}")
                start_time = lol.epoch_to_datetime(matchInfo["info"]["gameCreation"])
                central_timezone = pytz.timezone("US/Central")
                start_time = start_time.astimezone(central_timezone)
                readable_start = start_time.strftime("%B %d, %Y at %I:%M:%S %p %Z")
                gameDuration = timedelta(seconds=matchInfo["info"]["gameDuration"])
                embed = discord.Embed(
                    title="MATCH UPDATE!",
                    description=f"Start: {readable_start}\nDuration: {gameDuration}",
                    color=0x00FF00,
                )

                # Example of adding more fields
                team = ""
                prev_team = None
                for participant in matchInfo["info"]["participants"]:
                    discord_id = lol.find_discord_id_from_puuid(participant["puuid"])
                    nameAddon = ""
                    if discord_id:
                        if not participant["win"]:
                            # make the embed's color red
                            embed.color = 0xFF0000
                        discord_user = await bot.fetch_user(discord_id)
                        nameAddon = f" ({discord_user.mention})"

                    if participant["teamId"] == 100:
                        team = "Blue"
                    elif participant["teamId"] == 200:
                        team = "Red"

                    if team != prev_team:
                        if participant["win"]:
                            name = team + " (WINNER)"
                        else:
                            name = team + " (LOSER)"
                        embed.add_field(
                            name=name, value="------------------", inline=False
                        )

                    value = f"{participant['summonerName']}{nameAddon} - {participant['championName']} - {participant['kills']}/{participant['deaths']}/{participant['assists']}"
                    embed.add_field(name="", value=value, inline=False)
                    prev_team = team
                with lol.MatchImageCreator(matchInfo) as imagePath:
                    with open(imagePath, "rb") as f:
                        file = discord.File(f)
                        embed.set_image(url=f"attachment://{imagePath}")
                        await channel.send(file=file, embed=embed)
                # await channel.send(embed=embed)

                c.execute(
                    """
                    UPDATE player_match_info
                    SET posted = TRUE
                    WHERE puuid = %s
                    """,
                    (puuid,),
                )


@bot.event
async def on_ready():
    print(f"{bot.user.name} has connected to Discord!")
    bot.add_cog(LolCog(bot))


@bot.command(
    description="Register a summoner name to track", guildId=discord.Object(id=GUILD_ID)
)
async def register(ctx, name: str, tag: str = "NA1", associated_user: str = None):
    associated_user = await get_user_from_mention(
        bot, associated_user if associated_user else ctx.author.mention
    )

    account_info = lol.summoner_lookup(name, tag=tag, tracked=True)
    name = account_info["name"]
    puuid = account_info["puuid"]
    with get_cursor() as c:
        c.execute(
            """
            INSERT INTO summoner_discord_association (puuid, discord_id)
            VALUES (%s, %s)
            ON CONFLICT (puuid) DO UPDATE SET discord_id = %s
            """,
            (puuid, associated_user.id, associated_user.id),
        )

    await ctx.respond(f"Registering {name} for {associated_user.mention}")


async def get_user_from_mention(discord_client, mention):
    # Extract the user ID from the mention string
    user_id = mention[3:-1] if mention[2] == "!" else mention[2:-1]
    logging.info(f"Found user_id: {user_id}")
    try:
        # Fetch the user object using the extracted user ID
        user = await discord_client.fetch_user(user_id)
        logging.info(f"Found user: {user}")
        return user
    except discord.NotFound:
        logging.warn(f"User not found: {user_id}")
    except discord.HTTPException:
        logging.warn(f"Discord HTTP exception while fetching user: {user_id}")
