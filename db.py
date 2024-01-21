from contextlib import contextmanager
import logging
import os

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


@contextmanager
def get_cursor():
    with DatabaseConnection() as cursor:
        yield cursor


def bootstrap_database():
    """
    Create a new SQLite database with connection handled by the context manager,
    using the reusable connection function.
    """
    with get_cursor() as c:
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
            tracked BOOLEAN DEFAULT FALSE
        )
        """
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS player_ranked_status (
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            puuid TEXT NOT NULL,
            tier TEXT,
            rank TEXT,
            leaguePoints BIGINT,
            wins BIGINT,
            losses BIGINT,
            PRIMARY KEY (timestamp, puuid),
            FOREIGN KEY (puuid) REFERENCES account_info (puuid)
        )
        """
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS player_match_info (
            puuid TEXT NOT NULL,
            matchId TEXT PRIMARY KEY,
            matchInfo JSON,
            gameStartTimestamp TIMESTAMP,
            posted BOOLEAN DEFAULT FALSE,
            FOREIGN KEY (puuid) REFERENCES account_info (puuid)
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
