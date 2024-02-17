import logging

from bot import bot, DISCORD_TOKEN
from db import bootstrap_database

# logging.basicConfig(level=logging.DEBUG)
logging.basicConfig(level=logging.INFO)
# logging.basicConfig(level=logging.WARNING)


if __name__ == "__main__":
    bootstrap_database()
    bot.run(DISCORD_TOKEN)
