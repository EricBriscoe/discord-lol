from datetime import datetime
from functools import cache
import hashlib
import json
import logging
import random
import string
import time
import os

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
import requests

from db import get_cursor


load_dotenv()

RIOT_API_KEY = os.environ.get("RIOT_API_KEY")
RIOT_API_BASE_URL = "https://na1.api.riotgames.com"


@cache
def get_name_from_puuid(puuid: str, tracked: bool = False):
    with get_cursor() as c:
        c.execute(
            """
            SELECT name FROM account_info WHERE puuid = %s
            """,
            (puuid,),
        )
        retval = c.fetchone()
    if retval:
        return retval[0]

    url = f"{RIOT_API_BASE_URL}/lol/summoner/v4/summoners/by-puuid/{puuid}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        account_info = response.json()
        with get_cursor() as conn:
            conn.execute(
                """
                INSERT INTO account_info (
                    accountId,
                    profileIconId,
                    revisionDate,
                    name,
                    id,
                    puuid,
                    summonerLevel,
                    tracked
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (puuid) DO UPDATE SET
                    accountId = excluded.accountId,
                    profileIconId = excluded.profileIconId,
                    revisionDate = excluded.revisionDate,
                    name = excluded.name,
                    puuid = excluded.puuid,
                    summonerLevel = excluded.summonerLevel
            """,
                (
                    account_info["accountId"],
                    account_info["profileIconId"],
                    account_info["revisionDate"],
                    account_info["name"],
                    account_info["id"],
                    account_info["puuid"],
                    account_info["summonerLevel"],
                    tracked,
                ),
            )
        return account_info["name"]


def summoner_lookup(name: str, tag: str = "NA1", tracked: bool = False):
    url = f"https://americas.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name.replace(' ', '%20')}/{tag}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        puuid = response.json()["puuid"]
    else:
        raise Exception(
            f"Error while looking up summoner: {response.status_code} - {response.text}"
        )

    url = f"{RIOT_API_BASE_URL}/lol/summoner/v4/summoners/by-puuid/{puuid}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        account_info = response.json()
        logging.info(f"{account_info}")
        # Store the account information in the database
        account_id = account_info["accountId"]
        profile_icon_id = account_info["profileIconId"]
        revision_date = account_info["revisionDate"]
        name = account_info["name"]
        summoner_id = account_info["id"]
        summoner_level = account_info["summonerLevel"]
        with get_cursor() as c:
            c.execute(
                """
                INSERT INTO account_info (
                    accountId,
                    profileIconId,
                    revisionDate,
                    name,
                    id,
                    puuid,
                    summonerLevel,
                    tracked
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (puuid) DO UPDATE SET
                    accountId = EXCLUDED.accountId,
                    profileIconId = EXCLUDED.profileIconId,
                    revisionDate = EXCLUDED.revisionDate,
                    name = EXCLUDED.name,
                    id = EXCLUDED.id,
                    summonerLevel = EXCLUDED.summonerLevel,
                    tracked = EXCLUDED.tracked
                """,
                (
                    account_id,
                    profile_icon_id,
                    epoch_to_datetime(revision_date),
                    name,
                    summoner_id,
                    puuid,
                    summoner_level,
                    tracked,
                ),
            )

        return account_info
    else:
        raise Exception(
            f"Error while looking up summoner: {response.status_code} - {response.text}"
        )


def epoch_to_datetime(epoch_seconds):
    # If epoch_seconds is a string, convert it to a float or integer first.
    if isinstance(epoch_seconds, str):
        epoch_seconds = int(epoch_seconds)

    # Convert epoch time to a datetime object.
    dt = datetime.utcfromtimestamp(epoch_seconds / 1000)
    return dt


# Function to convert to epoch seconds if not None and is datetime
# dt strings formatted like 2024-01-04%2021:41:55.660000
def datetime_to_epoch(dt):
    if isinstance(dt, str):
        dt = dt.replace("%20", " ")
        dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S.%f")

    if isinstance(dt, datetime):
        epoch = datetime.utcfromtimestamp(0)
        delta = dt - epoch
        return int(delta.total_seconds())


def get_matches(
    puuid: str,
    startTime=None,
    endTime=None,
    queue=None,
    matchType=None,
    start=None,
    count=None,
):
    startTime = datetime_to_epoch(startTime)
    endTime = datetime_to_epoch(endTime)

    params = {}
    params["startTime"] = startTime
    params["endTime"] = endTime
    params["queue"] = queue
    params["type"] = matchType
    params["start"] = start
    params["count"] = count if count else 100

    url = (
        f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    )
    headers = {"X-Riot-Token": RIOT_API_KEY}
    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        return response.json()
    else:
        logging.warn(
            f"Error while looking up summoner: {response.status_code} - {response.text}"
        )


def backfill_matches():
    """
    Do 1 api call per player and backfill their matches
    """
    logging.info("Starting backfill_matches")
    with get_cursor() as c:
        c.execute(
            """
            SELECT puuid FROM account_info WHERE tracked = TRUE
            """
        )
        for puuid in c.fetchall():
            c.execute(
                """
                SELECT MIN(gameStartTimestamp) FROM player_match_info
                WHERE EXISTS (
                    SELECT 1
                    FROM json_array_elements(matchInfo->'info'->'participants') as participant
                    WHERE participant->>'puuid' = %s
                ) AND gameStartTimestamp > '2000-01-01'
                """,
                (puuid[0],),
            )
            min_timestamp = c.fetchone()[0]
            logging.info(f"min_timestamp: {min_timestamp}")

            matches = get_matches(puuid, endTime=min_timestamp)
            c.executemany(
                """
                INSERT INTO player_match_info (puuid, matchId)
                VALUES (%s, %s)
                ON CONFLICT (matchId) DO NOTHING
                """,
                [(puuid[0], matchId) for matchId in matches],
            )

            logging.info(
                f"Saved {len(matches)} matches for {get_name_from_puuid(puuid[0])}"
            )


def forwardfill_matches():
    """
    Do 1 api call per player and forwardfill their matches
    """
    logging.info("Starting forwardfill_matches")
    with get_cursor() as c:
        c.execute(
            """
            SELECT puuid FROM account_info WHERE tracked = TRUE
            """
        )
        for puuid in c.fetchall():
            c.execute(
                """
                SELECT MAX(gameStartTimestamp) FROM player_match_info
                WHERE EXISTS (
                    SELECT 1
                    FROM json_array_elements(matchInfo->'info'->'participants') as participant
                    WHERE participant->>'puuid' = %s
                )
                """,
                (puuid[0],),
            )
            max_timestamp = c.fetchone()[0]

            matches = get_matches(puuid, startTime=max_timestamp)
            c.executemany(
                """
                INSERT INTO player_match_info (puuid, matchId)
                VALUES (%s, %s)
                ON CONFLICT (matchId) DO NOTHING
                """,
                [(puuid[0], matchId) for matchId in matches],
            )
            logging.info(
                f"Saved {len(matches)} matches for {get_name_from_puuid(puuid[0])}"
            )


def randfill_matches():
    """
    Select a random timestamp and forwardfill matches from there for each player
    """
    pass  # TODO


def save_match_details():
    """
    Find a match without details and look it up
    """
    with get_cursor() as c:
        c.execute(
            """
            SELECT puuid, matchId
            FROM player_match_info
            WHERE matchInfo IS NULL
            ORDER BY matchId DESC
            """
        )
        to_update = c.fetchone()
    if to_update:
        puuid, matchId = to_update
    else:
        return

    url = f"https://americas.api.riotgames.com/lol/match/v5/matches/{matchId}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        match_info = response.json()
        # Store the account information in the database
        with get_cursor() as c:
            c.execute(
                """
                INSERT INTO player_match_info (puuid, matchId, matchInfo, gameStartTimestamp)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (matchId) DO UPDATE SET
                    matchInfo = excluded.matchInfo,
                    gameStartTimestamp = excluded.gameStartTimestamp
            """,
                (
                    puuid,
                    matchId,
                    json.dumps(match_info),
                    datetime.fromtimestamp(
                        match_info["info"]["gameStartTimestamp"] / 1000
                    ),
                ),
            )
        logging.info(
            f"Saved match details of {matchId} for {get_name_from_puuid(puuid)}"
        )
    else:
        with get_cursor() as c:
            c.execute(
                """
                UPDATE player_match_info
                SET matchInfo = '{"error": "error"}', posted = TRUE
                WHERE matchId = %s
                """,
                (matchId,),
            )
        logging.warn(
            f"Error while looking up match: {response.status_code} - {response.text}"
        )


def find_discord_id_from_puuid(puuid):
    with get_cursor() as c:
        c.execute(
            """
            SELECT discord_id FROM summoner_discord_association WHERE puuid = %s
            """,
            (puuid,),
        )
        retval = c.fetchone()
    if retval:
        return retval[0]


class MatchImageCreator:
    def __init__(self, matchInfo, directory="./"):
        self.matchInfo = matchInfo
        self.directory = directory
        self.filepath = None

    def create_hashed_filename(self):
        random_str = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=10)
        )
        return hashlib.sha256(random_str.encode()).hexdigest() + ".png"

    def __enter__(self):
        # Create an image with desired dimensions
        background_path = os.path.join(os.getcwd(), "background.webp")
        font_path = os.path.join(os.getcwd(), "Spiegel_TT_Bold.ttf")
        img = Image.open(background_path)
        d = ImageDraw.Draw(img)
        fnt = ImageFont.truetype(font_path, 30)

        # Define starting positions
        start_x = 10
        start_y = 10
        row_height = 40

        # Add column headers
        d.text((start_x, start_y), "Player Name", font=fnt, fill=(255, 255, 255))
        d.text((start_x + 300, start_y), "K/D/A", font=fnt, fill=(255, 255, 255))

        # Add player data
        for i, participant in enumerate(self.matchInfo["info"]["participants"]):
            y = start_y + ((i + 1) * row_height)
            d.text(
                (start_x, y),
                participant["summonerName"],
                font=fnt,
                fill=(255, 255, 255),
            )
            d.text(
                (start_x + 300, y),
                f"{participant['kills']}/{participant['deaths']}/{participant['assists']}",
                font=fnt,
                fill=(255, 255, 255),
            )

        # Save the image
        filename = self.create_hashed_filename()
        self.filepath = os.path.join(self.directory, filename)
        img.save(self.filepath)

        return self.filepath

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Delete the file
        if self.filepath and os.path.exists(self.filepath):
            os.remove(self.filepath)
