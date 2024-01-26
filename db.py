from contextlib import asynccontextmanager, contextmanager
import logging
import os

import aiopg
import psycopg2


class DatabaseConnection:
    def __init__(self):
        self.dbname = "dump"
        self.user = "postgres"
        self.password = os.environ.get("POSTGRES_PASSWORD")
        self.host = "db"

    def __enter__(self):
        self.conn = psycopg2.connect(
            dbname=self.dbname, user=self.user, password=self.password, host=self.host
        )
        self.conn.autocommit = True
        self.cursor = self.conn.cursor()
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cursor.close()
        self.conn.close()


class AsyncDatabaseConnection:
    def __init__(self):
        self.dbname = "dump"
        self.user = "postgres"
        self.password = os.environ.get("POSTGRES_PASSWORD")
        self.host = "db"

    async def __aenter__(self):
        self.conn = await aiopg.connect(
            dbname=self.dbname, user=self.user, password=self.password, host=self.host
        )
        return await self.conn.cursor()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # await self.cursor.close()
        await self.conn.close()


@contextmanager
def get_cursor():
    with DatabaseConnection() as cursor:
        yield cursor


@asynccontextmanager
async def get_async_cursor():
    async with AsyncDatabaseConnection() as cursor:
        yield cursor


def bootstrap_database():
    """
    Create a new SQLite database with connection handled by the context manager,
    using the reusable connection function.
    """
    with get_cursor() as c:
        c.execute(
            """
            CREATE OR REPLACE FUNCTION round_timestamp()
            RETURNS TRIGGER AS $$
            BEGIN
                -- Round NEW.timestamp to the nearest minute
                NEW.timestamp = date_trunc('hour', NEW.timestamp) + 
                                INTERVAL '1 min' * ROUND(date_part('minute', NEW.timestamp));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS account_info (
            accountId TEXT,
            profileIconId BIGINT,
            revisionDate TIMESTAMP,
            name TEXT,
            id TEXT,
            puuid TEXT PRIMARY KEY,
            summonerLevel BIGINT,
            tracked BOOLEAN DEFAULT FALSE,
            lastUpdated TIMESTAMP DEFAULT '1900-01-01'
        )
        """
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS player_ranked_status (
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            leagueId TEXT,
            summonerId TEXT,
            summonerName TEXT,
            queueType TEXT,
            tier TEXT,
            rank TEXT,
            leaguePoints INT,
            wins INT,
            losses INT,
            hotStreak BOOLEAN,
            veteran BOOLEAN,
            freshBlood BOOLEAN,
            inactive BOOLEAN,
            miniSeries JSON,
            PRIMARY KEY (timestamp, summonerId, queueType)
        );
        CREATE OR REPLACE TRIGGER round_timestamp_before_insert
        BEFORE INSERT ON player_ranked_status
        FOR EACH ROW EXECUTE FUNCTION round_timestamp();
        """
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS match_info (
            matchId TEXT PRIMARY KEY,
            matchInfo JSON,
            gameStartTimestamp TIMESTAMP,
            posted BOOLEAN DEFAULT FALSE
        )
        """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS summoner_discord_association (
                puuid TEXT PRIMARY KEY,
                discord_id TEXT,
                FOREIGN KEY (puuid) REFERENCES account_info (puuid)
            )
            """
        )
        logging.info("Database bootstrapped successfully.")
