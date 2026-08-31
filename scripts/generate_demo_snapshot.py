"""Regenerate demo-data/sample-snapshot.json.

Drives the real app through its normal routes (like the test suite does) so ratings,
achievements, and history are all computed by the app's own logic rather than hand-faked.
All players and matches below are fictional. Run from the repo root:

    python scripts/generate_demo_snapshot.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from elo_club import create_app  # noqa: E402
from elo_club.db import export_data_snapshot, get_db  # noqa: E402

PLAYERS = [
    ("Alice Nakamura", "alice@example.com", "Queen's Gambit", "Studies openings more than she plays them."),
    ("Ben Ortiz", "ben@example.com", "Sicilian Defense", "Never resigns before move 60."),
    ("Priya Shah", "priya@example.com", "Caro-Kann Defense", "Slow and steady wins the game."),
    ("Sam Fischer", "sam@example.com", "King's Indian Defense", "Lives for the endgame."),
    ("Yuki Tanaka", "yuki@example.com", "Italian Game", "Brings snacks to every match."),
]

# (white, black, result, played_at, opening_name, opening_code)
MATCHES = [
    (0, 1, "white", "2026-03-02", "Queen's Gambit", "D06"),
    (2, 3, "black", "2026-03-03", "King's Indian Defense", "E60"),
    (1, 4, "white", "2026-03-05", "Sicilian Defense", "B20"),
    (0, 2, "draw", "2026-03-06", "Caro-Kann Defense", "B10"),
    (3, 4, "white", "2026-03-09", "Italian Game", "C50"),
    (0, 4, "white", "2026-03-10", "Italian Game", "C50"),
    (1, 2, "white", "2026-03-12", "Sicilian Defense", "B20"),
    (2, 4, "black", "2026-03-13", "Italian Game", "C50"),
    (0, 1, "black", "2026-03-16", "Sicilian Defense", "B20"),
    (3, 0, "white", "2026-03-17", "King's Indian Defense", "E60"),
    (1, 3, "draw", "2026-03-19", "Queen's Gambit", "D06"),
    (2, 0, "white", "2026-03-20", "Caro-Kann Defense", "B10"),
    (4, 1, "white", "2026-03-23", "Italian Game", "C50"),
    (3, 2, "white", "2026-03-24", "King's Indian Defense", "E60"),
    (0, 3, "white", "2026-03-26", "Queen's Gambit", "D06"),
]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "demo-generator",
                "DATABASE": str(tmp_path / "demo.sqlite3"),
                "SNAPSHOT_DIR": str(tmp_path / "snapshots"),
                "AUTO_SNAPSHOT_TO_REPO": False,
                "AUTO_GIT_COMMIT": False,
                "SMTP_HOST": "",
                "MAIL_SENDER": "",
                "MAIL_OUTBOX": str(tmp_path / "mail-outbox.log"),
            }
        )
        client = app.test_client()

        for name, email, opening, bio in PLAYERS:
            client.post(
                "/register",
                data={"name": name, "email": email, "password": "demo-password"},
                follow_redirects=True,
            )
            client.post("/login", data={"email": email, "password": "demo-password"}, follow_redirects=True)
            client.post(
                "/account",
                data={
                    "name": name,
                    "tagline": bio,
                    "favorite_opening": opening,
                    "avatar_color": "#b5472f",
                    "avatar_icon": "initials",
                    "bio": bio,
                },
                follow_redirects=True,
            )
            client.get("/logout", follow_redirects=True)

        owner_email = PLAYERS[0][1]
        client.post("/login", data={"email": owner_email, "password": "demo-password"}, follow_redirects=True)
        client.post(
            "/groups/create",
            data={
                "name": "Lunch Break Chess Club",
                "slug": "lunch-break-chess",
                "description": "Demo data - fictional players and matches for trying out the app.",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with app.app_context():
            invite_code = get_db().execute(
                "SELECT invite_code FROM groups_workspace WHERE slug = 'lunch-break-chess'"
            ).fetchone()["invite_code"]
        client.get("/logout", follow_redirects=True)

        for _, email, _, _ in PLAYERS[1:]:
            client.post("/login", data={"email": email, "password": "demo-password"}, follow_redirects=True)
            client.post(
                "/groups/join",
                data={"slug": "lunch-break-chess", "invite_code": invite_code},
                follow_redirects=True,
            )
            client.get("/logout", follow_redirects=True)

        client.post("/login", data={"email": owner_email, "password": "demo-password"}, follow_redirects=True)
        for white_idx, black_idx, result, played_at, opening_name, opening_code in MATCHES:
            client.post(
                "/groups/lunch-break-chess/matches",
                data={
                    "game_type": "standard",
                    "white_user_id": str(white_idx + 1),
                    "black_user_id": str(black_idx + 1),
                    "result": result,
                    "played_at": played_at,
                    "season_id": "",
                    "challenge_id": "",
                    "opening_name": opening_name,
                    "opening_code": opening_code,
                    "pgn_text": "",
                    "notes": "",
                },
                follow_redirects=True,
            )

        with app.app_context():
            snapshot = export_data_snapshot(get_db())

    output_path = REPO_ROOT / "demo-data" / "sample-snapshot.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
