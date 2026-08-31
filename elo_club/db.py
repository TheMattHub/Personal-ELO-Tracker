from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_placeholder INTEGER NOT NULL DEFAULT 0,
    is_former_employee INTEGER NOT NULL DEFAULT 0,
    placeholder_created_by INTEGER,
    selected_title TEXT,
    bio TEXT,
    favorite_opening TEXT,
    avatar_upload_data TEXT,
    avatar_upload_format TEXT,
    avatar_color TEXT NOT NULL DEFAULT '#b5472f',
    avatar_icon TEXT NOT NULL DEFAULT 'initials',
    tagline TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (placeholder_created_by) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS groups_workspace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    company_domain TEXT,
    invite_code TEXT NOT NULL,
    starting_rating INTEGER NOT NULL DEFAULT 1200,
    default_k_factor INTEGER NOT NULL DEFAULT 24,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#247ba0',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id)
);

CREATE TABLE IF NOT EXISTS memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    team_id INTEGER,
    role TEXT NOT NULL DEFAULT 'member',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_id, user_id),
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id),
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (team_id) REFERENCES teams (id)
);

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    reset_ratings INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    season_id INTEGER,
    game_type TEXT NOT NULL DEFAULT 'standard' CHECK(game_type IN ('standard', 'one_arm_one_brain')),
    white_user_id INTEGER NOT NULL,
    white_partner_user_id INTEGER,
    black_user_id INTEGER NOT NULL,
    black_partner_user_id INTEGER,
    result TEXT NOT NULL CHECK(result IN ('white', 'black', 'draw')),
    played_at TEXT NOT NULL,
    time_control_label TEXT,
    time_control_base_seconds INTEGER,
    time_control_increment_seconds INTEGER,
    white_instruction_clarity INTEGER,
    black_instruction_clarity INTEGER,
    confirmation_status TEXT NOT NULL DEFAULT 'confirmed' CHECK(confirmation_status IN ('pending', 'confirmed')),
    confirmed_by INTEGER,
    opening_name TEXT,
    opening_code TEXT,
    pgn_text TEXT,
    notes TEXT,
    reported_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    deleted_at TEXT,
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id),
    FOREIGN KEY (season_id) REFERENCES seasons (id),
    FOREIGN KEY (white_user_id) REFERENCES users (id),
    FOREIGN KEY (white_partner_user_id) REFERENCES users (id),
    FOREIGN KEY (black_user_id) REFERENCES users (id),
    FOREIGN KEY (black_partner_user_id) REFERENCES users (id),
    FOREIGN KEY (confirmed_by) REFERENCES users (id),
    FOREIGN KEY (reported_by) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS rating_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    season_id INTEGER,
    match_id INTEGER NOT NULL,
    ladder_type TEXT NOT NULL DEFAULT 'standard',
    user_id INTEGER NOT NULL,
    rating_before REAL NOT NULL,
    rating_after REAL NOT NULL,
    delta REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id),
    FOREIGN KEY (season_id) REFERENCES seasons (id),
    FOREIGN KEY (match_id) REFERENCES matches (id),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    format TEXT NOT NULL DEFAULT 'round_robin' CHECK(format IN ('round_robin', 'knockout', 'swiss')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'active', 'completed')),
    created_by INTEGER NOT NULL,
    winner_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id),
    FOREIGN KEY (created_by) REFERENCES users (id),
    FOREIGN KEY (winner_user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS tournament_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    UNIQUE(tournament_id, user_id),
    FOREIGN KEY (tournament_id) REFERENCES tournaments (id),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS tournament_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    white_user_id INTEGER NOT NULL,
    black_user_id INTEGER NOT NULL,
    result TEXT CHECK(result IN ('white', 'black', 'draw')),
    round_name TEXT,
    played_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES tournaments (id),
    FOREIGN KEY (white_user_id) REFERENCES users (id),
    FOREIGN KEY (black_user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    challenger_user_id INTEGER NOT NULL,
    challenged_user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'accepted', 'declined', 'completed')),
    source TEXT NOT NULL DEFAULT 'manual',
    message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    responded_at TEXT,
    match_id INTEGER,
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id),
    FOREIGN KEY (challenger_user_id) REFERENCES users (id),
    FOREIGN KEY (challenged_user_id) REFERENCES users (id),
    FOREIGN KEY (match_id) REFERENCES matches (id)
);

CREATE TABLE IF NOT EXISTS coffee_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    debtor_user_id INTEGER NOT NULL,
    creditor_user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL DEFAULT 1,
    reason TEXT,
    entry_type TEXT NOT NULL DEFAULT 'manual',
    source_match_id INTEGER,
    is_settled INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at TEXT,
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id),
    FOREIGN KEY (debtor_user_id) REFERENCES users (id),
    FOREIGN KEY (creditor_user_id) REFERENCES users (id),
    FOREIGN KEY (source_match_id) REFERENCES matches (id),
    FOREIGN KEY (created_by) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS signup_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    new_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (new_user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS signup_notification_reads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    read_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(notification_id, user_id),
    FOREIGN KEY (notification_id) REFERENCES signup_notifications (id),
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    group_id INTEGER,
    achievement_key TEXT NOT NULL,
    source_match_id INTEGER,
    source_season_id INTEGER,
    is_seen INTEGER NOT NULL DEFAULT 0,
    unlocked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, achievement_key),
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (group_id) REFERENCES groups_workspace (id),
    FOREIGN KEY (source_match_id) REFERENCES matches (id),
    FOREIGN KEY (source_season_id) REFERENCES seasons (id)
);
"""

TRACKED_TABLES = [
    "users",
    "groups_workspace",
    "teams",
    "memberships",
    "seasons",
    "matches",
    "rating_history",
    "tournaments",
    "tournament_entries",
    "tournament_games",
    "challenges",
    "coffee_ledger",
    "signup_notifications",
    "signup_notification_reads",
    "app_meta",
    "user_achievements",
]


class DBConnection:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def execute(self, sql: str, params=()):
        return self.connection.execute(sql, params)

    def executemany(self, sql: str, seq_of_params):
        return self.connection.executemany(sql, seq_of_params)

    def executescript(self, script: str):
        return self.connection.executescript(script)

    def insert_and_get_id(self, sql: str, params=()):
        cursor = self.execute(sql, params)
        return cursor.lastrowid

    def commit(self) -> None:
        self.connection.commit()
        auto_snapshot_repo(self)

    def close(self) -> None:
        self.connection.close()


def _repo_root() -> Path:
    return Path(current_app.root_path).parent


def _snapshot_dir() -> Path:
    configured = current_app.config.get("SNAPSHOT_DIR")
    if configured:
        return Path(configured)
    return _repo_root() / "data" / "snapshots"


def get_db() -> DBConnection:
    if "db" not in g:
        database_path = Path(current_app.config["DATABASE"])
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        g.db = DBConnection(connection)
    return g.db


def close_db(_: object | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def export_data_snapshot(db: DBConnection) -> dict:
    data = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
    }
    for table_name in TRACKED_TABLES:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()}
        order_by = " ORDER BY id" if "id" in columns else ""
        rows = db.execute(f"SELECT * FROM {table_name}{order_by}").fetchall()
        data["tables"][table_name] = [dict(row) for row in rows]
    return data


def auto_snapshot_repo(db: DBConnection) -> None:
    if not current_app.config.get("AUTO_SNAPSHOT_TO_REPO", True):
        return
    if g.get("snapshot_suppressed"):
        return
    snapshot_dir = _snapshot_dir()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = export_data_snapshot(db)
    payload = json.dumps(snapshot, indent=2, sort_keys=True)
    latest_path = snapshot_dir / "latest.json"
    timestamped_path = snapshot_dir / f"snapshot-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    latest_path.write_text(payload, encoding="utf-8")
    timestamped_path.write_text(payload, encoding="utf-8")
    if current_app.config.get("AUTO_GIT_COMMIT", False):
        try:
            subprocess.run(["git", "add", str(snapshot_dir)], cwd=_repo_root(), check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "commit", "-m", f"data snapshot {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"],
                cwd=_repo_root(),
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:
            pass


def init_db() -> None:
    db = get_db()
    g.snapshot_suppressed = True
    db.executescript(SCHEMA_SQL)
    ensure_migrations(db)
    db.commit()
    g.snapshot_suppressed = False


def ensure_migrations(db: DBConnection) -> None:
    user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "bio" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN bio TEXT")
    if "is_placeholder" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN is_placeholder INTEGER NOT NULL DEFAULT 0")
    if "is_former_employee" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN is_former_employee INTEGER NOT NULL DEFAULT 0")
    if "placeholder_created_by" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN placeholder_created_by INTEGER")
    if "selected_title" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN selected_title TEXT")
    if "avatar_upload_data" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN avatar_upload_data TEXT")
    if "avatar_upload_format" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN avatar_upload_format TEXT")
    if "favorite_opening" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN favorite_opening TEXT")
    if "avatar_color" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN avatar_color TEXT NOT NULL DEFAULT '#b5472f'")
    if "avatar_icon" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN avatar_icon TEXT NOT NULL DEFAULT 'initials'")
    if "tagline" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN tagline TEXT")
    match_columns = {row["name"] for row in db.execute("PRAGMA table_info(matches)").fetchall()}
    if "game_type" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN game_type TEXT NOT NULL DEFAULT 'standard'")
    if "time_control_label" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN time_control_label TEXT")
    if "time_control_base_seconds" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN time_control_base_seconds INTEGER")
    if "time_control_increment_seconds" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN time_control_increment_seconds INTEGER")
    if "opening_name" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN opening_name TEXT")
    if "opening_code" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN opening_code TEXT")
    if "white_partner_user_id" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN white_partner_user_id INTEGER")
    if "black_partner_user_id" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN black_partner_user_id INTEGER")
    if "white_instruction_clarity" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN white_instruction_clarity INTEGER")
    if "black_instruction_clarity" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN black_instruction_clarity INTEGER")
    if "confirmation_status" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN confirmation_status TEXT NOT NULL DEFAULT 'confirmed'")
    if "confirmed_by" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN confirmed_by INTEGER")
    if "pgn_text" not in match_columns:
        db.execute("ALTER TABLE matches ADD COLUMN pgn_text TEXT")
    membership_columns = {row["name"] for row in db.execute("PRAGMA table_info(memberships)").fetchall()}
    if "team_id" not in membership_columns:
        db.execute("ALTER TABLE memberships ADD COLUMN team_id INTEGER")
    season_columns = {row["name"] for row in db.execute("PRAGMA table_info(seasons)").fetchall()}
    if "end_date" not in season_columns:
        db.execute("ALTER TABLE seasons ADD COLUMN end_date TEXT")
    rating_history_columns = {row["name"] for row in db.execute("PRAGMA table_info(rating_history)").fetchall()}
    if "ladder_type" not in rating_history_columns:
        db.execute("ALTER TABLE rating_history ADD COLUMN ladder_type TEXT NOT NULL DEFAULT 'standard'")
    coffee_columns = {row["name"] for row in db.execute("PRAGMA table_info(coffee_ledger)").fetchall()}
    if "entry_type" not in coffee_columns:
        db.execute("ALTER TABLE coffee_ledger ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'manual'")
    if "source_match_id" not in coffee_columns:
        db.execute("ALTER TABLE coffee_ledger ADD COLUMN source_match_id INTEGER")
    challenge_columns = {row["name"] for row in db.execute("PRAGMA table_info(challenges)").fetchall()}
    if "source" not in challenge_columns:
        db.execute("ALTER TABLE challenges ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS signup_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            new_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (new_user_id) REFERENCES users (id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS signup_notification_reads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            read_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(notification_id, user_id),
            FOREIGN KEY (notification_id) REFERENCES signup_notifications (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER,
            achievement_key TEXT NOT NULL,
            source_match_id INTEGER,
            source_season_id INTEGER,
            is_seen INTEGER NOT NULL DEFAULT 0,
            unlocked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, achievement_key),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (group_id) REFERENCES groups_workspace (id),
            FOREIGN KEY (source_match_id) REFERENCES matches (id),
            FOREIGN KEY (source_season_id) REFERENCES seasons (id)
        )
        """
    )


def import_data_snapshot(db: DBConnection, snapshot: dict) -> None:
    g.snapshot_suppressed = True
    try:
        for table_name in reversed(TRACKED_TABLES):
            db.execute(f"DELETE FROM {table_name}")
        for table_name in TRACKED_TABLES:
            rows = snapshot.get("tables", {}).get(table_name, [])
            for row in rows:
                columns = list(row.keys())
                placeholders = ", ".join("?" for _ in columns)
                db.execute(
                    f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})",
                    tuple(row[column] for column in columns),
                )
        db.commit()
    finally:
        g.snapshot_suppressed = False
    auto_snapshot_repo(db)


def init_app(app) -> None:
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command() -> None:
        init_db()
        print("Database initialized.")
