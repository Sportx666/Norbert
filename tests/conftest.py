import os
from pathlib import Path

import pytest

from firstthings.db import Database, migrate
from firstthings.template import load_template, parse_template

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "config" / "week_template.yaml"
DB_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def t():
    return load_template(TEMPLATE_PATH)


@pytest.fixture(scope="session")
def template_text():
    return TEMPLATE_PATH.read_text()


@pytest.fixture(scope="session")
def make_template(template_text):
    def make(*replacements):
        text = template_text
        for old, new in replacements:
            assert old in text, old
            text = text.replace(old, new, 1)
        return parse_template(text)
    return make


@pytest.fixture(scope="session")
def _database():
    if not DB_URL:
        pytest.skip("set TEST_DATABASE_URL to run database tests")
    if ("supabase" in DB_URL or DB_URL == os.environ.get("DATABASE_URL")) and not os.environ.get("FT_ALLOW_DESTRUCTIVE"):
        pytest.exit("Refusing to run: tests DROP the ft schema. Point TEST_DATABASE_URL at a throwaway Postgres.")
    db = Database(DB_URL)
    yield db
    db.close()


@pytest.fixture
def db(_database):
    with _database.tx() as conn:
        conn.execute("drop schema if exists ft cascade")
        migrate(conn)
    return _database
