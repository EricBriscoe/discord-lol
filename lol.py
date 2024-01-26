from datetime import datetime, timedelta
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


def backfill_matches(puuid=None):
    """
    Backfill matches for a given player or the player with the oldest lastUpdated timestamp
    """
    logging.info("Starting backfill_matches")
    if puuid == None:
        with get_cursor() as c:
            c.execute(
                """
                SELECT puuid
                FROM account_info
                WHERE tracked = TRUE
                ORDER BY lastUpdated ASC
                LIMIT 1
                """
            )
            if c.rowcount == 0:
                return
            puuid = c.fetchone()[0]

    timestamp = datetime.now() + timedelta(days=1)
    matches = []
    while timestamp > datetime.now() - timedelta(days=365):
        new_matches = get_matches(puuid, endTime=timestamp)
        if new_matches:
            matches += new_matches
        else:
            logging.warn(f"Error while looking up matches before {timestamp}")
            break
        timestamp = get_match_details(matches[-1])["info"]["gameStartTimestamp"]
        timestamp = datetime.fromtimestamp(timestamp / 1000)

    with get_cursor() as c:
        c.executemany(
            """
            INSERT INTO match_info (matchId)
            VALUES (%s)
            ON CONFLICT (matchId) DO NOTHING
            """,
            [(matchId,) for matchId in matches],
        )
        c.execute(
            """
            UPDATE account_info
            SET lastUpdated = now()
            WHERE puuid = %s
            """,
            (puuid,),
        )
    logging.info(f"Saved {len(matches)} matches for {get_name_from_puuid(puuid)}")


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
        update_puuids = c.fetchall()
        if update_puuids == []:
            return

    matches = []
    for puuid in update_puuids:
        with get_cursor() as c:
            c.execute(
                """
                SELECT MAX(gameStartTimestamp) FROM match_info
                WHERE EXISTS (
                    SELECT 1
                    FROM json_array_elements(matchInfo->'info'->'participants') as participant
                    WHERE participant->>'puuid' = %s
                )
                """,
                (puuid[0],),
            )
            try:
                max_timestamp = c.fetchone()[0] + timedelta(minutes=5)
            except TypeError:
                continue
        new_matches = get_matches(puuid, startTime=max_timestamp)
        logging.info(
            f"Found {len(new_matches)} new matches for {get_name_from_puuid(puuid[0])}"
        )
        matches += new_matches

    with get_cursor() as c:
        c.executemany(
            """
            INSERT INTO match_info (matchId)
            VALUES (%s)
            ON CONFLICT (matchId) DO NOTHING
            """,
            [(matchId,) for matchId in matches],
        )
    if not matches:
        logging.info("No new matches found")


def get_match_details(matchId=None):
    """
    Retrieve match details from the database and update the database if necessary
    If no matchId is provided, get the most recent match and update the database
    """
    if matchId == None:
        with get_cursor() as c:
            c.execute(
                """
                SELECT matchId FROM match_info WHERE matchInfo is null
                ORDER BY matchId DESC
                LIMIT 1
                """
            )
            if c.rowcount == 0:
                return
            matchId = c.fetchone()[0]

    logging.debug(f"Searching db for match: {matchId}")
    with get_cursor() as c:
        c.execute(
            """
            SELECT matchInfo FROM match_info
            WHERE matchId = %s
                AND matchInfo is not null
            """,
            (matchId,),
        )
        if c.rowcount > 0:
            logging.debug(f"Found match details for {matchId} in the database")
            return c.fetchone()[0]

    url = f"https://americas.api.riotgames.com/lol/match/v5/matches/{matchId}"
    logging.debug(f"Not found in database, pinging url: {url}")
    headers = {"X-Riot-Token": RIOT_API_KEY}
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        logging.debug(f"Found match details for {matchId}")
        match_info = response.json()
        # Store the account information in the database
        with get_cursor() as c:
            c.execute(
                """
                INSERT INTO match_info (matchId, matchInfo, gameStartTimestamp)
                VALUES (%s, %s, %s)
                ON CONFLICT (matchId) DO UPDATE SET
                    matchInfo = excluded.matchInfo,
                    gameStartTimestamp = excluded.gameStartTimestamp
            """,
                (
                    matchId,
                    json.dumps(match_info),
                    datetime.fromtimestamp(
                        match_info["info"]["gameStartTimestamp"] / 1000
                    ),
                ),
            )
        logging.info(
            f"Saved match details of {matchId} on {datetime.fromtimestamp(match_info['info']['gameStartTimestamp'] / 1000)}"
        )
        return match_info
    elif response.status_code == 429:
        logging.warn(
            f"Rate limit exceeded while looking up match: {response.status_code} - {response.text}"
        )
    else:
        with get_cursor() as c:
            c.execute(
                """
                UPDATE match_info
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


@cache
def find_queue_from_id(queueId):
    logging.info("Downloading queue info")
    url = "https://static.developer.riotgames.com/docs/lol/queues.json"
    # download the json file from the url above
    r = requests.get(url)
    if r.status_code == 200:
        queues = r.json()
        for queue in queues:
            if queue["queueId"] == queueId:
                return queue


def get_summoner_rank(summonerId, queue="RANKED_SOLO_5x5"):
    """
    Check to see if the most recently stored rank was in the last minute
    and if so, return that rank. Otherwise, ping the API and store the new rank.
    """
    logging.debug(f"Getting rank for {summonerId}")
    with get_cursor() as c:
        c.execute(
            """
            SELECT timestamp, tier, rank
            FROM player_ranked_status
            WHERE summonerId = %s AND queueType = %s
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (summonerId, queue),
        )
        if c.rowcount > 0:
            timestamp, tier, rank = c.fetchone()
            if timestamp > datetime.now() - timedelta(minutes=1):
                logging.debug(f"Found rank for {summonerId} in the database")
                return tier, rank

    url = f"{RIOT_API_BASE_URL}/lol/league/v4/entries/by-summoner/{summonerId}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        league_entries = response.json()
        logging.debug(f"Found {league_entries} league entries for {summonerId}")
        with get_cursor() as c:
            c.executemany(
                """
                INSERT INTO player_ranked_status (
                    leagueId, summonerId, summonerName, queueType, tier, rank,
                    leaguePoints, wins, losses, hotStreak, veteran, freshBlood,
                    inactive, miniSeries
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (timestamp, summonerId, queueType) DO NOTHING
                """,
                [
                    (
                        league_entry["leagueId"],
                        league_entry["summonerId"],
                        league_entry["summonerName"],
                        league_entry["queueType"],
                        league_entry["tier"],
                        league_entry["rank"],
                        league_entry["leaguePoints"],
                        league_entry["wins"],
                        league_entry["losses"],
                        league_entry["hotStreak"],
                        league_entry["veteran"],
                        league_entry["freshBlood"],
                        league_entry["inactive"],
                        json.dumps(league_entry.get("miniSeries")),
                    )
                    for league_entry in league_entries
                ],
            )
        for league_entry in league_entries:
            if league_entry["queueType"] == queue:
                logging.debug(f"Found rank for {summonerId} in the API")
                return league_entry["tier"], league_entry["rank"]
        logging.debug(f"No rank found for {summonerId}")


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

    def draw_damage_bar(
        self,
        d,
        x,
        y,
        participant_damage,
        max_damage,
        bar_width,
        bar_height,
        color,
        draw_from_right,
    ):
        # Calculate the length of the participant's damage bar relative to max damage
        participant_bar_length = (participant_damage / max_damage) * bar_width

        # Draw the black background bar (max damage)
        d.rectangle([x, y, x + bar_width, y + bar_height], fill="#000000")

        # Adjust starting point for the participant's damage bar based on the side
        if draw_from_right:
            start_x = x + bar_width - participant_bar_length + 1
        else:
            start_x = x + 1

        # Draw the participant's damage bar on top
        d.rectangle(
            [start_x, y + 1, start_x + participant_bar_length - 2, y + bar_height - 2],
            fill=color,
        )

    def __enter__(self):
        # Create an image with desired dimensions
        background_path = os.path.join(os.getcwd(), "background.webp")
        font_path = os.path.join(os.getcwd(), "Spiegel_TT_Bold.ttf")
        img = Image.open(background_path)
        d = ImageDraw.Draw(img)
        fntSize = 40
        gold = "#C89B3C"
        black = "#000000"
        stroke_width = 4
        fnt = ImageFont.truetype(font_path, fntSize)

        center_x = img.width // 2 - 3

        # Add vertical line
        line_y_start = 0
        line_y_end = img.height
        line_width = 5
        d.line(
            [(center_x, line_y_start), (center_x, line_y_end)],
            fill=black,
            width=line_width,
        )

        # Define starting positions
        start_x_left = center_x - 10
        start_x_right = center_x + 10
        start_y = 10
        row_height = fntSize + 10

        playerCount = len(self.matchInfo["info"]["participants"])
        maxDamage = max(
            [
                p["totalDamageDealtToChampions"]
                for p in self.matchInfo["info"]["participants"]
            ]
        )
        bar_width = 200  # Width of the damage bar
        bar_height = 20  # Height of the damage bar

        # Add player data
        for i, participant in enumerate(self.matchInfo["info"]["participants"]):
            y = start_y + ((i % (playerCount / 2) + 1) * 3 * row_height)
            if i < playerCount / 2:
                d.text(
                    (start_x_left, y),
                    participant["summonerName"],
                    font=fnt,
                    fill=gold,
                    anchor="ra",
                    stroke_width=stroke_width,
                    stroke_fill=black,
                )
                d.text(
                    (start_x_left, y + row_height),
                    f"{participant['championName']} - {participant['kills']}/{participant['deaths']}/{participant['assists']}",
                    font=fnt,
                    fill=gold,
                    anchor="ra",
                    stroke_width=stroke_width,
                    stroke_fill=black,
                )
                self.draw_damage_bar(
                    d,
                    start_x_left - bar_width,
                    y + 2 * row_height,
                    participant["totalDamageDealtToChampions"],
                    maxDamage,
                    bar_width,
                    bar_height,
                    gold,
                    True,
                )

            else:
                d.text(
                    (start_x_right, y),
                    participant["summonerName"],
                    font=fnt,
                    fill=gold,
                    stroke_width=stroke_width,
                    stroke_fill=black,
                )
                d.text(
                    (start_x_right, y + row_height),
                    f"{participant['kills']}/{participant['deaths']}/{participant['assists']} - {participant['championName']}",
                    font=fnt,
                    fill=gold,
                    stroke_width=stroke_width,
                    stroke_fill=black,
                )
                self.draw_damage_bar(
                    d,
                    start_x_right,
                    y + 2 * row_height,
                    participant["totalDamageDealtToChampions"],
                    maxDamage,
                    bar_width,
                    bar_height,
                    gold,
                    False,
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
