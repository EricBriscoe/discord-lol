import asyncio
from datetime import datetime, timedelta
import json
import logging
import os


import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import pytz

import lol
from db import get_cursor, get_async_cursor
from dankutil import roman_to_int

load_dotenv()


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
intents = discord.Intents.all()
bot = discord.Bot()

GAME_LOG_CHANNEL_ID = int(os.environ.get("GAME_LOG_CHANNEL_ID"))
GUILD_ID = int(os.environ.get("GUILD_ID"))


class LolCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.forwardfill_matches.start()
        self.backfill_matches.start()
        self.get_match_details.start()
        self.post_match_details.start()

    @tasks.loop(seconds=120)
    async def forwardfill_matches(self):
        await asyncio.to_thread(lol.forwardfill_matches)

    @tasks.loop(seconds=1200)
    async def backfill_matches(self):
        await asyncio.to_thread(lol.backfill_matches)

    @tasks.loop(seconds=40)
    async def get_match_details(self):
        await asyncio.to_thread(lol.get_match_details)

    @tasks.loop(seconds=60)
    async def post_match_details(self):
        logging.info("Searching for matches to post")
        channel = self.bot.get_channel(GAME_LOG_CHANNEL_ID)
        if not channel:
            return

        async with get_async_cursor() as c:
            await c.execute(
                """
                SELECT 
                    mi.matchId, 
                    mi.matchInfo
                FROM 
                    match_info mi
                WHERE
                    mi.gameStartTimestamp > now() - interval '6 days'
                    and posted = FALSE
                ORDER BY 
                    mi.matchId asc;
                """
            )
            logging.info(f"Found {c.rowcount} matches to post")
            matches = await c.fetchall()

        for matchId, matchInfo in matches:
            logging.info(
                f"Posting match {matchId} from {datetime.fromtimestamp(matchInfo['info']['gameCreation']/1000)}"
            )
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
            gameMap = lol.find_queue_from_id(matchInfo["info"]["queueId"])["map"]
            queueDescription = lol.find_queue_from_id(matchInfo["info"]["queueId"])[
                "description"
            ]
            embed.add_field(
                name=gameMap,
                value=queueDescription,
            )

            # Example of adding more fields
            team = ""
            prev_team = None
            for participant in matchInfo["info"]["participants"]:
                discord_id = lol.find_discord_id_from_puuid(participant["puuid"])
                nameAddon = ""
                if discord_id:
                    result = await asyncio.to_thread(
                        lol.get_summoner_rank, participant["summonerId"]
                    )
                    try:
                        tier, rank = result
                    except TypeError:
                        tier, rank = "Unranked", None

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

                    value = f"""{participant['summonerName']}{nameAddon} - {tier.title()} {roman_to_int(rank) or ''} 
                    {participant['championName']} - {participant['kills']}/{participant['deaths']}/{participant['assists']}"""
                    embed.add_field(name="", value=value, inline=False)
                    prev_team = team
            with lol.MatchImageCreator(matchInfo) as imagePath:
                with open(imagePath, "rb") as f:
                    file = discord.File(f)
                    embed.set_image(url=f"attachment://{imagePath}")
                    await channel.send(file=file, embed=embed)
            async with get_async_cursor() as c:
                await c.execute(
                    """
                    UPDATE match_info
                    SET posted = TRUE
                    WHERE matchId = %s
                    """,
                    (matchId,),
                )
            logging.debug(
                f"Posted match {matchId} from {datetime.fromtimestamp(matchInfo['info']['gameCreation']/1000)}"
            )

bot.add_cog(LolCog(bot))

@bot.event
async def on_ready():
    logging.info(f"{bot.user.name} has connected to Discord!")


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


@bot.command(
    description="Deregister a summoner name to track",
    guildId=discord.Object(id=GUILD_ID),
)
async def deregister(ctx, name: str, tag: str = "NA1"):
    account_info = lol.summoner_lookup(name, tag=tag, tracked=True)
    name = account_info["name"]
    puuid = account_info["puuid"]
    with get_async_cursor() as c:
        await c.execute(
            """
            DELETE FROM summoner_discord_association
            WHERE puuid = %s
            """,
            (puuid,),
        )
        await c.execute(
            """
            UPDATE account_info
            SET tracked = FALSE
            WHERE puuid = %s
            """,
            (puuid,),
        )

    await ctx.respond(f"Deregistered {name}")


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
