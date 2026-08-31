import io
import json
import re
import unittest
from datetime import datetime
from pathlib import Path

from elo_club import create_app
from elo_club.db import get_db
from elo_club.elo import recalculate_group_ratings
from elo_club.i18n import repair_text_encoding, translate_html, translate_text
from PIL import Image
from werkzeug.security import generate_password_hash


class EloAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path.cwd() / "instance" / "test-data"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        database_path = self.temp_dir / "test.sqlite3"
        snapshot_dir = self.temp_dir / "snapshots"
        mail_outbox = self.temp_dir / "mail-outbox.log"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        if database_path.exists():
            database_path.unlink()
        latest_snapshot = snapshot_dir / "latest.json"
        if latest_snapshot.exists():
            latest_snapshot.unlink()
        if mail_outbox.exists():
            mail_outbox.unlink()
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(database_path),
                "SECRET_KEY": "test",
                "SNAPSHOT_DIR": str(snapshot_dir),
                "AUTO_GIT_COMMIT": False,
                "MAIL_OUTBOX": str(mail_outbox),
                "SMTP_HOST": "",
                "MAIL_SENDER": "",
            }
        )
        self.client = self.app.test_client()
        self.snapshot_dir = snapshot_dir
        self.mail_outbox = mail_outbox

    def test_rating_recalculation_with_draw_and_reset(self):
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO users (name, email, password_hash) VALUES ('Alice', 'alice@example.com', 'x')")
            db.execute("INSERT INTO users (name, email, password_hash) VALUES ('Bob', 'bob@example.com', 'x')")
            db.execute(
                """
                INSERT INTO groups_workspace
                (name, slug, invite_code, created_by, starting_rating, default_k_factor)
                VALUES ('Club', 'club', 'abc123', 1, 1200, 24)
                """
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 1, 'owner')")
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute(
                "INSERT INTO seasons (group_id, name, start_date, is_active, reset_ratings) VALUES (1, 'S1', '2026-01-01', 1, 0)"
            )
            db.execute(
                "INSERT INTO seasons (group_id, name, start_date, is_active, reset_ratings) VALUES (1, 'S2', '2026-02-01', 0, 1)"
            )
            db.execute(
                """
                INSERT INTO matches
                (group_id, season_id, white_user_id, black_user_id, result, played_at, reported_by)
                VALUES (1, 1, 1, 2, 'white', '2026-01-02', 1)
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (group_id, season_id, white_user_id, black_user_id, result, played_at, reported_by)
                VALUES (1, 2, 1, 2, 'draw', '2026-02-02', 1)
                """
            )
            db.commit()

            recalculate_group_ratings(db, 1)

            history = db.execute(
                "SELECT user_id, rating_before, rating_after FROM rating_history ORDER BY match_id, user_id"
            ).fetchall()
            self.assertEqual(len(history), 4)
            self.assertEqual(round(history[0]["rating_after"], 1), 1212.0)
            self.assertEqual(round(history[1]["rating_after"], 1), 1188.0)
            self.assertEqual(round(history[2]["rating_before"], 1), 1200.0)
            self.assertEqual(round(history[3]["rating_before"], 1), 1200.0)

    def test_homepage_and_registration_flow(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Run your company chess ladder", response.data)

        register = self.client.post(
            "/register",
            data={"name": "Test User", "email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.assertEqual(register.status_code, 200)
        self.assertIn(b"Account created", register.data)

    def test_register_claims_placeholder_account(self):
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO users (name, email, password_hash, is_placeholder)
                VALUES (?, ?, ?, 1)
                """,
                ("Guest Player", "guest@example.com", generate_password_hash("placeholder")),
            )
            db.commit()

        register = self.client.post(
            "/register",
            data={"name": "Real Player", "email": "guest@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.assertEqual(register.status_code, 200)
        self.assertIn(b"Account claimed", register.data)

        login = self.client.post(
            "/login",
            data={"email": "guest@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn(b"Welcome back", login.data)

        with self.app.app_context():
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE email = 'guest@example.com'").fetchone()
        self.assertEqual(user["name"], "Real Player")
        self.assertEqual(user["is_placeholder"], 0)

    def test_placeholder_account_cannot_log_in_before_claim(self):
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO users (name, email, password_hash, is_placeholder)
                VALUES (?, ?, ?, 1)
                """,
                ("Guest Player", "guest@example.com", generate_password_hash("placeholder")),
            )
            db.commit()

        login = self.client.post(
            "/login",
            data={"email": "guest@example.com", "password": "placeholder"},
            follow_redirects=True,
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn(b"placeholder account", login.data)

    def test_match_can_create_guest_placeholder_and_send_invite(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )

        response = self.client.post(
            "/groups/club/matches",
            data={
                "game_type": "standard",
                "white_use_guest": "1",
                "white_guest_email": "guest.player@example.com",
                "black_user_id": "1",
                "result": "white",
                "played_at": "2026-04-20",
                "season_id": "",
                "opening_name": "",
                "opening_code": "",
                "pgn_text": "",
                "notes": "",
                "challenge_id": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Match saved", response.data)
        self.assertTrue(self.mail_outbox.exists())

        with self.app.app_context():
            db = get_db()
            guest = db.execute("SELECT * FROM users WHERE email = 'guest.player@example.com'").fetchone()
            membership = db.execute(
                "SELECT * FROM memberships WHERE group_id = 1 AND user_id = ? AND is_active = 1",
                (guest["id"],),
            ).fetchone()
            match = db.execute("SELECT * FROM matches WHERE white_user_id = ? AND black_user_id = 1", (guest["id"],)).fetchone()
        self.assertIsNotNone(guest)
        self.assertEqual(guest["is_placeholder"], 1)
        self.assertIsNotNone(membership)
        self.assertIsNotNone(match)

        outbox = self.mail_outbox.read_text(encoding="utf-8")
        self.assertIn("guest.player@example.com", outbox)
        self.assertIn("/invite/club/", outbox)

    def test_match_form_prefills_active_season(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE seasons SET is_active = 0 WHERE group_id = 1")
            db.execute(
                """
                INSERT INTO seasons (id, group_id, name, start_date, end_date, is_active, reset_ratings)
                VALUES (2, 1, 'Spring 2026', '2026-04-01', '2026-06-30', 1, 0)
                """
            )
            db.commit()

        response = self.client.get("/groups/club/matches")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<label>Season', response.data)
        self.assertIn(b'<option value="2" selected>Spring 2026</option>', response.data)
        self.assertIn(f'value="{datetime.now().strftime("%Y-%m-%d")}"'.encode(), response.data)
        self.assertIn(b"Blitz 5+0", response.data)

    def test_suggestions_are_personalized_and_ranked_by_win_gain(self):
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO users (name, email, password_hash) VALUES ('Alice', 'alice@example.com', ?)", (generate_password_hash("password123"),))
            db.execute("INSERT INTO users (name, email, password_hash) VALUES ('Bob', 'bob@example.com', ?)", (generate_password_hash("password123"),))
            db.execute("INSERT INTO users (name, email, password_hash) VALUES ('Carol', 'carol@example.com', ?)", (generate_password_hash("password123"),))
            db.execute(
                """
                INSERT INTO groups_workspace
                (name, slug, invite_code, created_by, starting_rating, default_k_factor)
                VALUES ('Club', 'club', 'abc123', 1, 1200, 24)
                """
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 1, 'owner')")
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 3, 'member')")
            db.execute(
                """
                INSERT INTO matches
                (group_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 1, 2, 'black', '2026-04-01', 1, 'confirmed', 1)
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (group_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 1, 3, 'white', '2026-04-05', 1, 'confirmed', 1)
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (group_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 2, 3, 'white', '2026-04-08', 1, 'confirmed', 1)
                """
            )
            db.commit()
            recalculate_group_ratings(db, 1)

        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        response = self.client.get("/groups/club/suggestions")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your suggested opponents", response.data)
        self.assertIn(b"Alice vs Bob", response.data)
        self.assertIn(b"Alice vs Carol", response.data)
        self.assertNotIn(b"Bob vs Carol", response.data)
        self.assertIn(b"Win gain", response.data)
        self.assertIn(b"Loss cost", response.data)
        self.assertIn(b'data-sort-column="opponent"', response.data)
        self.assertIn(b'data-sort-column="last-played"', response.data)
        self.assertIn(b"Opponent gap", response.data)
        self.assertIn(b"opponent higher", response.data)
        self.assertIn(b"you higher", response.data)
        self.assertIn(b"2026-04-01", response.data)
        self.assertIn(b"VS", response.data)
        self.assertIn(b"Challenge", response.data)
        self.assertLess(response.data.index(b"Alice vs Bob"), response.data.index(b"Alice vs Carol"))

        challenge = self.client.post(
            "/groups/club/challenges",
            data={
                "action": "create",
                "challenged_user_id": "2",
                "challenge_source": "suggestion",
                "message": "Alice has spotted 13 shiny ELO points on the board. The pawns have already started trash-talking.",
            },
            follow_redirects=True,
        )
        self.assertEqual(challenge.status_code, 200)
        self.assertIn(b"Challenge sent.", challenge.data)
        self.assertIn(b"Challenge email saved to the local outbox.", challenge.data)
        self.assertIn(b"Alice", challenge.data)
        self.assertIn(b"Bob", challenge.data)
        self.assertIn(b"Launched today", challenge.data)
        self.assertIn(b"class=\"challenge-clash\"", challenge.data)
        self.assertIn(b"Log result", challenge.data)
        outbox_text = self.mail_outbox.read_text(encoding="utf-8")
        self.assertIn("bob@example.com", outbox_text)
        self.assertIn("The pawns have already started trash-talking.", outbox_text)
        self.assertIn("May your blunders be instructive", outbox_text)
        with self.app.app_context():
            db = get_db()
            challenge_row = db.execute("SELECT source FROM challenges WHERE id = 1").fetchone()
            achievements = {
                row["achievement_key"]
                for row in db.execute("SELECT achievement_key FROM user_achievements WHERE user_id = 1").fetchall()
            }
        self.assertEqual(challenge_row["source"], "suggestion")
        self.assertIn("challenge_callout_1", achievements)
        self.assertIn("suggestion_spark_1", achievements)

    def test_owner_sees_signup_notifications_on_home(self):
        self.client.post(
            "/register",
            data={"name": "Owner", "email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/register",
            data={"name": "New User", "email": "new@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"New signups", home.data)
        self.assertIn(b"new@example.com", home.data)

    def test_owner_can_remove_member_from_group(self):
        self.client.post(
            "/register",
            data={"name": "Owner", "email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Member", "email": "member@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            member = db.execute("SELECT id FROM users WHERE email = 'member@example.com'").fetchone()
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, ?, 'member')", (member["id"],))
            db.commit()
        removed = self.client.post(
            "/groups/club/members",
            data={"action": "remove", "member_id": "2"},
            follow_redirects=True,
        )
        self.assertEqual(removed.status_code, 200)
        self.assertIn(b"Member removed from the group.", removed.data)
        with self.app.app_context():
            db = get_db()
            membership = db.execute("SELECT is_active FROM memberships WHERE group_id = 1 AND user_id = 2").fetchone()
        self.assertEqual(membership["is_active"], 0)

    def test_member_can_inspect_public_profile_for_group_member(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO users
                (name, email, password_hash, selected_title, tagline, favorite_opening, bio, avatar_color, avatar_icon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Bob",
                    "bob@example.com",
                    generate_password_hash("password123"),
                    "On the Board",
                    "Endgame enjoyer",
                    "Caro-Kann Defense",
                    "Always trades queens.",
                    "#215d75",
                    "achievement:first_win",
                ),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute(
                """
                INSERT INTO groups_workspace
                (name, slug, invite_code, created_by, starting_rating, default_k_factor)
                VALUES ('Other Club', 'other-club', 'other123', 1, 1200, 24)
                """
            )
            db.execute(
                "INSERT INTO user_achievements (user_id, group_id, achievement_key, is_seen) VALUES (2, 1, 'first_win', 1)"
            )
            db.execute(
                "INSERT INTO user_achievements (user_id, group_id, achievement_key, is_seen) VALUES (2, 2, 'games_10', 1)"
            )
            db.commit()

        members = self.client.get("/groups/club/members")
        self.assertEqual(members.status_code, 200)
        self.assertIn(b"/groups/club/members/2", members.data)
        self.assertIn(b"View profile", members.data)

        profile = self.client.get("/groups/club/members/2")
        self.assertEqual(profile.status_code, 200)
        self.assertIn(b"Bob", profile.data)
        self.assertIn(b"Endgame enjoyer", profile.data)
        self.assertIn(b"On the Board", profile.data)
        self.assertIn(b"First Blood", profile.data)
        self.assertIn(b"achievement-reward-first_win.png", profile.data)
        self.assertIn(b"Caro-Kann Defense", profile.data)
        self.assertIn(b"Always trades queens.", profile.data)
        self.assertNotIn(b"Other Club", profile.data)
        self.assertNotIn(b"Regular", profile.data)
        self.assertNotIn(b"bob@example.com", profile.data)

    def test_admin_can_mark_member_as_former_employee_and_exclude_from_new_matches(self):
        self.client.post(
            "/register",
            data={"name": "Admin", "email": "admin@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "admin@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Bob", "bob@example.com", generate_password_hash("password123")),
            )
            db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Carol", "carol@example.com", generate_password_hash("password123")),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 3, 'member')")
            db.commit()

        marked = self.client.post(
            "/groups/club/members",
            data={"action": "mark_former_employee", "member_id": "2"},
            follow_redirects=True,
        )
        self.assertEqual(marked.status_code, 200)
        self.assertIn(b"Member marked as former employee.", marked.data)
        self.assertIn(b"Former Employee", marked.data)
        self.assertIn(b"avatar-former-employee-tombstone.png", marked.data)

        with self.app.app_context():
            db = get_db()
            bob = db.execute("SELECT * FROM users WHERE id = 2").fetchone()
        self.assertEqual(bob["is_former_employee"], 1)
        self.assertEqual(bob["selected_title"], "Former Employee")
        self.assertEqual(bob["avatar_icon"], "former-employee-tombstone")

        dashboard = self.client.get("/groups/club")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Bob", dashboard.data)
        self.assertIn(b"avatar-former-employee-tombstone.png", dashboard.data)

        matches = self.client.get("/groups/club/matches")
        self.assertEqual(matches.status_code, 200)
        match_page = matches.data.decode("utf-8")
        new_match_form = match_page.split('<article class="card span-2">', 1)[0]
        self.assertNotIn('<option value="2">Bob', new_match_form)
        self.assertIn('<option value="3">Carol', new_match_form)
        self.assertIn('<option value="2" >Bob</option>', match_page)

        blocked_match = self.client.post(
            "/groups/club/matches",
            data={
                "game_type": "standard",
                "white_user_id": "2",
                "black_user_id": "3",
                "result": "white",
                "played_at": "2026-01-02",
                "season_id": "",
                "challenge_id": "",
                "opening_name": "",
                "opening_code": "",
                "pgn_text": "",
                "notes": "",
                "white_partner_user_id": "",
                "black_partner_user_id": "",
                "white_instruction_clarity": "",
                "black_instruction_clarity": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(blocked_match.status_code, 200)
        self.assertIn(b"Former employee accounts cannot be selected for new matches.", blocked_match.data)
        with self.app.app_context():
            db = get_db()
            match_count = db.execute("SELECT COUNT(*) AS total FROM matches").fetchone()["total"]
        self.assertEqual(match_count, 0)

    def test_owner_can_remove_member_from_system_data_and_send_email(self):
        self.client.post(
            "/register",
            data={"name": "Owner", "email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Member", "email": "member@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            member = db.execute("SELECT id FROM users WHERE email = 'member@example.com'").fetchone()
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, ?, 'member')", (member["id"],))
            db.commit()

        removed = self.client.post(
            "/system/data",
            data={"action": "remove_member", "group_id": "1", "member_id": "2"},
            follow_redirects=True,
        )
        self.assertEqual(removed.status_code, 200)
        self.assertIn(b"deleted from the database", removed.data)
        self.assertIn(b"local outbox", removed.data)

        with self.app.app_context():
            db = get_db()
            membership = db.execute("SELECT * FROM memberships WHERE group_id = 1 AND user_id = 2").fetchone()
            user = db.execute("SELECT * FROM users WHERE id = 2").fetchone()
        self.assertIsNone(membership)
        self.assertIsNone(user)
        self.assertTrue(self.mail_outbox.exists())
        outbox = self.mail_outbox.read_text(encoding="utf-8")
        self.assertIn("member@example.com", outbox)
        self.assertIn("owner@example.com", outbox)

    def test_system_data_can_delete_orphan_account(self):
        self.client.post(
            "/register",
            data={"name": "Owner", "email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Orphan", "email": "orphan@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        removed = self.client.post(
            "/system/data",
            data={"action": "delete_orphan_account", "user_id": "2"},
            follow_redirects=True,
        )
        self.assertEqual(removed.status_code, 200)
        self.assertIn(b"Orphan account deleted from the database.", removed.data)
        self.assertIn(b"Removal email", removed.data)
        with self.app.app_context():
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE id = 2").fetchone()
        self.assertIsNone(user)
        outbox = self.mail_outbox.read_text(encoding="utf-8")
        self.assertIn("orphan@example.com", outbox)
        self.assertIn("owner@example.com", outbox)

    def test_language_switch_to_italian_updates_shared_ui(self):
        switched = self.client.post("/language", data={"language": "it", "next_url": "/"}, follow_redirects=True)
        self.assertEqual(switched.status_code, 200)
        self.assertIn("Lingua".encode("utf-8"), switched.data)
        self.assertIn("Accedi".encode("utf-8"), switched.data)
        self.assertIn("Classifica privata di gruppo".encode("utf-8"), switched.data)

    def test_account_customization_and_group_fun_pages(self):
        self.client.post(
            "/register",
            data={"name": "Test User", "email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        account = self.client.post(
            "/account",
            data={
                "name": "Test User",
                "tagline": "Blitz gremlin",
                "favorite_opening": "Italian Game",
                "avatar_color": "#123456",
                "avatar_icon": "queen",
                "bio": "Always asks for rematches.",
            },
            follow_redirects=True,
        )
        self.assertEqual(account.status_code, 200)
        self.assertIn(b"Profile updated", account.data)
        self.assertIn(b"avatar-queen.svg", account.data)

        create_group = self.client.post(
            "/groups/create",
            data={
                "name": "Lunch Club",
                "slug": "lunch-club",
                "description": "Office chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        self.assertEqual(create_group.status_code, 200)
        self.assertIn(b"Suggested matchups", create_group.data)

        suggestions = self.client.get("/groups/lunch-club/suggestions")
        winners = self.client.get("/groups/lunch-club/winners")
        coffee = self.client.get("/groups/lunch-club/coffee")
        tournaments = self.client.get("/groups/lunch-club/tournaments")
        challenges = self.client.get("/groups/lunch-club/challenges")
        head_to_head = self.client.get("/groups/lunch-club/head-to-head")
        self.assertEqual(suggestions.status_code, 200)
        self.assertEqual(winners.status_code, 200)
        self.assertEqual(coffee.status_code, 200)
        self.assertEqual(tournaments.status_code, 200)
        self.assertEqual(challenges.status_code, 200)
        self.assertEqual(head_to_head.status_code, 200)

    def test_account_can_use_unlocked_title_and_reward_icon(self):
        self.client.post(
            "/register",
            data={"name": "Test User", "email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO user_achievements (user_id, achievement_key, is_seen)
                VALUES (1, 'season_champion', 1)
                """
            )
            db.commit()
        self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        account = self.client.post(
            "/account",
            data={
                "name": "Test User",
                "tagline": "Champion mode",
                "favorite_opening": "Italian Game",
                "avatar_color": "#123456",
                "avatar_icon": "laurel",
                "selected_title": "Season Champion",
                "bio": "Always asks for rematches.",
            },
            follow_redirects=True,
        )
        self.assertEqual(account.status_code, 200)
        self.assertIn(b"avatar-laurel.svg", account.data)
        self.assertIn(b"Season Champion", account.data)

    def test_account_can_use_unlocked_achievement_badge_as_avatar(self):
        self.client.post(
            "/register",
            data={"name": "Test User", "email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO user_achievements (user_id, achievement_key, is_seen)
                VALUES (1, 'peak_1400', 1)
                """
            )
            db.commit()
        self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        account = self.client.post(
            "/account",
            data={
                "name": "Test User",
                "tagline": "Fourteen hundred and climbing",
                "favorite_opening": "Italian Game",
                "avatar_color": "#123456",
                "avatar_icon": "achievement:peak_1400",
                "selected_title": "1400 Club",
                "bio": "Always asks for rematches.",
            },
            follow_redirects=True,
        )
        self.assertEqual(account.status_code, 200)
        self.assertIn(b"achievement-reward-peak_1400.png", account.data)
        self.assertIn(b"1400 Club", account.data)

        with self.app.app_context():
            db = get_db()
            user = db.execute("SELECT avatar_icon, selected_title FROM users WHERE id = 1").fetchone()
        self.assertEqual(user["avatar_icon"], "achievement:peak_1400")
        self.assertEqual(user["selected_title"], "1400 Club")

    def test_account_can_upload_custom_avatar_image(self):
        self.client.post(
            "/register",
            data={"name": "Test User", "email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        image = Image.new("RGB", (480, 320), "#336699")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)

        account = self.client.post(
            "/account",
            data={
                "name": "Test User",
                "tagline": "Photo finish",
                "favorite_opening": "Italian Game",
                "avatar_color": "#123456",
                "avatar_icon": "initials",
                "selected_title": "",
                "bio": "Always asks for rematches.",
                "avatar_upload": (buffer, "avatar.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(account.status_code, 200)
        self.assertIn(b"/avatars/1.jpg", account.data)

        avatar = self.client.get("/avatars/1.jpg")
        self.assertEqual(avatar.status_code, 200)
        self.assertEqual(avatar.mimetype, "image/jpeg")

        with self.app.app_context():
            db = get_db()
            user = db.execute("SELECT avatar_icon, avatar_upload_data, avatar_upload_format FROM users WHERE id = 1").fetchone()
        self.assertEqual(user["avatar_icon"], "uploaded")
        self.assertIsNotNone(user["avatar_upload_data"])
        self.assertEqual(user["avatar_upload_format"], "image/jpeg")

    def test_admin_system_data_shows_hidden_achievement_catalog(self):
        self.client.post(
            "/register",
            data={"name": "Owner", "email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "owner@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        page = self.client.get("/system/data")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Hidden achievement catalog", page.data)
        self.assertIn(b"first_win", page.data)

    def test_match_unlocks_achievement_popup_and_account_section(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.commit()

        response = None
        for played_at in ["2026-04-20", "2026-04-21", "2026-04-22"]:
            response = self.client.post(
                "/groups/club/matches",
                data={
                    "game_type": "standard",
                    "white_user_id": "1",
                    "black_user_id": "2",
                    "result": "white",
                    "played_at": played_at,
                    "season_id": "",
                    "opening_name": "",
                    "opening_code": "",
                    "pgn_text": "",
                    "notes": "",
                    "challenge_id": "",
                },
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Achievement unlocked", response.data)
        self.assertIn(b"Hot Hand", response.data)

        account = self.client.get("/account")
        self.assertEqual(account.status_code, 200)
        self.assertIn(b"Unlocked titles and rewards", account.data)
        self.assertIn(b"Hot Hand", account.data)

    def test_match_opening_and_belt_features_render(self):
        with self.client:
            self.client.post(
                "/register",
                data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
                follow_redirects=True,
            )
            self.client.post(
                "/login",
                data={"email": "alice@example.com", "password": "password123"},
                follow_redirects=True,
            )
            self.client.post(
                "/groups/create",
                data={
                    "name": "Club",
                    "slug": "club",
                    "description": "Chess",
                    "company_domain": "",
                    "starting_rating": "1200",
                    "default_k_factor": "24",
                },
                follow_redirects=True,
            )
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Bob", "bob@example.com", generate_password_hash("password123")),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute(
                """
                INSERT INTO matches
                (group_id, white_user_id, black_user_id, result, played_at, opening_name, opening_code, reported_by)
                VALUES (1, 1, 2, 'white', '2026-01-02', 'Italian Game', 'C50', 1)
                """
            )
            db.commit()
            stats = self.client.get("/groups/club/stats")
            dashboard = self.client.get("/groups/club")
            self.assertEqual(stats.status_code, 200)
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn(b"Italian Game", stats.data)
            self.assertIn(b"Belt holder", dashboard.data)

    def test_challenge_acceptance_prefills_match_logging(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/join",
            data={"slug": "club", "invite_code": invite_code},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/club/challenges",
            data={"action": "create", "challenged_user_id": "1", "message": "Lunch duel"},
            follow_redirects=True,
        )
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        accept = self.client.post(
            "/groups/club/challenges",
            data={"action": "accept", "challenge_id": "1"},
            follow_redirects=True,
        )
        self.assertEqual(accept.status_code, 200)
        match_page = self.client.get("/groups/club/matches?challenge=1")
        stats_page = self.client.get("/groups/club/stats")
        self.assertEqual(match_page.status_code, 200)
        self.assertEqual(stats_page.status_code, 200)
        self.assertIn(b"This match will complete the linked challenge.", match_page.data)
        self.assertIn(b"Records and milestones", stats_page.data)
        board = self.client.get("/groups/club/challenges")
        self.assertIn(b"Launched today", board.data)
        self.assertIn(b'class="challenge-clash"', board.data)
        self.assertIn(b"Log result", board.data)
        completed = self.client.post(
            "/groups/club/matches",
            data={
                "game_type": "standard",
                "white_user_id": "1",
                "black_user_id": "2",
                "result": "white",
                "played_at": "2026-04-20",
                "time_control_preset": "",
                "season_id": "",
                "challenge_id": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(completed.status_code, 200)
        self.assertIn(b"Linked challenge completed.", completed.data)
        with self.app.app_context():
            db = get_db()
            challenge = db.execute("SELECT status, match_id FROM challenges WHERE id = 1").fetchone()
        self.assertEqual(challenge["status"], "completed")
        self.assertEqual(challenge["match_id"], 1)

    def test_head_to_head_compare_page_renders_rivalry_summary(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/join",
            data={"slug": "club", "invite_code": invite_code},
            follow_redirects=True,
        )
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/club/matches",
            data={
                "game_type": "standard",
                "white_user_id": "1",
                "black_user_id": "2",
                "result": "white",
                "played_at": "2026-01-02",
                "season_id": "",
                "challenge_id": "",
                "opening_name": "",
                "opening_code": "",
                "pgn_text": "",
                "notes": "",
                "white_partner_user_id": "",
                "black_partner_user_id": "",
                "white_instruction_clarity": "",
                "black_instruction_clarity": "",
            },
            follow_redirects=True,
        )
        page = self.client.get("/groups/club/head-to-head?left=1&right=2")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Rivalry snapshot", page.data)
        self.assertIn(b"Games", page.data)
        self.assertIn(b"Alice", page.data)
        self.assertIn(b"Bob", page.data)
        self.assertIn(b"Alice won", page.data)

    def test_team_management_and_dashboard_missions_render(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        create_team = self.client.post(
            "/groups/club/teams",
            data={"action": "create", "name": "Engineering", "color": "#112233"},
            follow_redirects=True,
        )
        assign_team = self.client.post(
            "/groups/club/teams",
            data={"action": "assign", "member_id": "1", "team_id": "1"},
            follow_redirects=True,
        )
        dashboard = self.client.get("/groups/club")
        stats = self.client.get("/groups/club/stats")
        self.assertEqual(create_team.status_code, 200)
        self.assertEqual(assign_team.status_code, 200)
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(stats.status_code, 200)
        self.assertIn(b"Weekly missions", dashboard.data)
        self.assertIn(b"Achievement board", stats.data)
        self.assertIn(b"Engineering", stats.data)

    def test_match_confirmation_flow_and_pgn_tools(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/join",
            data={"slug": "club", "invite_code": invite_code},
            follow_redirects=True,
        )
        pending_match = self.client.post(
            "/groups/club/matches",
            data={
                "white_user_id": "2",
                "black_user_id": "1",
                "result": "white",
                "played_at": "2026-04-01",
                "time_control_preset": "300|0",
                "season_id": "",
                "challenge_id": "",
                "opening_name": "French Defense",
                "opening_code": "C00",
                "pgn_text": "1. e4 e6 2. d4 d5 1-0",
                "notes": "Imported later",
            },
            follow_redirects=True,
        )
        self.assertEqual(pending_match.status_code, 200)
        self.assertIn(b"Waiting for confirmation", pending_match.data)
        rejected_self_confirm = self.client.post(
            "/groups/club/confirmations",
            data={"match_id": "1"},
            follow_redirects=True,
        )
        self.assertEqual(rejected_self_confirm.status_code, 200)
        self.assertIn(b"Only an opponent or an admin can confirm this match.", rejected_self_confirm.data)
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        confirmations = self.client.get("/groups/club/confirmations")
        self.assertEqual(confirmations.status_code, 200)
        self.assertIn(b"French Defense", self.client.get("/groups/club/matches").data)
        confirmed = self.client.post(
            "/groups/club/confirmations",
            data={"match_id": "1"},
            follow_redirects=True,
        )
        pgn_page = self.client.get("/groups/club/pgn")
        pgn_export = self.client.get("/groups/club/export/games.pgn")
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(pgn_page.status_code, 200)
        self.assertEqual(pgn_export.status_code, 200)
        self.assertIn(b"Import PGN", pgn_page.data)
        self.assertIn(b"French Defense", pgn_export.data)
        self.assertIn(b'[TimeControl "300+0"]', pgn_export.data)
        with self.app.app_context():
            db = get_db()
            match = db.execute("SELECT time_control_label, time_control_base_seconds, time_control_increment_seconds FROM matches WHERE id = 1").fetchone()
        self.assertEqual(match["time_control_label"], "Blitz 5+0")
        self.assertEqual(match["time_control_base_seconds"], 300)
        self.assertEqual(match["time_control_increment_seconds"], 0)

    def test_non_admin_opponent_can_confirm_reported_match(self):
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO users (name, email, password_hash) VALUES ('Owner', 'owner@example.com', ?)", (generate_password_hash("password123"),))
            db.execute("INSERT INTO users (name, email, password_hash) VALUES ('Bob', 'bob@example.com', ?)", (generate_password_hash("password123"),))
            db.execute("INSERT INTO users (name, email, password_hash) VALUES ('Carol', 'carol@example.com', ?)", (generate_password_hash("password123"),))
            db.execute(
                """
                INSERT INTO groups_workspace
                (name, slug, invite_code, created_by, starting_rating, default_k_factor)
                VALUES ('Club', 'club', 'abc123', 1, 1200, 24)
                """
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 1, 'owner')")
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 3, 'member')")
            db.commit()

        self.client.post(
            "/login",
            data={"email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/club/matches",
            data={
                "white_user_id": "2",
                "black_user_id": "3",
                "result": "draw",
                "played_at": "2026-04-02",
                "time_control_preset": "custom",
                "time_control_custom_minutes": "19",
                "time_control_custom_seconds": "20",
                "time_control_custom_increment": "0",
                "season_id": "",
                "challenge_id": "",
            },
            follow_redirects=True,
        )
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "carol@example.com", "password": "password123"},
            follow_redirects=True,
        )
        confirmed = self.client.post(
            "/groups/club/confirmations",
            data={"match_id": "1"},
            follow_redirects=True,
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertIn(b"Match confirmed.", confirmed.data)
        with self.app.app_context():
            db = get_db()
            match = db.execute("SELECT confirmation_status, confirmed_by, time_control_label FROM matches WHERE id = 1").fetchone()
        self.assertEqual(match["confirmation_status"], "confirmed")
        self.assertEqual(match["confirmed_by"], 3)
        self.assertEqual(match["time_control_label"], "19m 20s")

    def test_swiss_tournament_creation(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Cara", "email": "cara@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        for email in ["bob@example.com", "cara@example.com"]:
            self.client.get("/logout", follow_redirects=True)
            self.client.post("/login", data={"email": email, "password": "password123"}, follow_redirects=True)
            self.client.post("/groups/join", data={"slug": "club", "invite_code": invite_code}, follow_redirects=True)
        self.client.get("/logout", follow_redirects=True)
        self.client.post("/login", data={"email": "alice@example.com", "password": "password123"}, follow_redirects=True)
        swiss = self.client.post(
            "/groups/club/tournaments",
            data={"action": "create", "name": "Swiss Cup", "format": "swiss", "participant_ids": ["1", "2", "3"]},
            follow_redirects=True,
        )
        self.assertEqual(swiss.status_code, 200)
        self.assertIn(b"Swiss Cup", swiss.data)

    def test_one_arm_one_brain_match_updates_all_four_players(self):
        for name, email in [
            ("Alice", "alice@example.com"),
            ("Bob", "bob@example.com"),
            ("Cara", "cara@example.com"),
            ("Diego", "diego@example.com"),
        ]:
            self.client.post(
                "/register",
                data={"name": name, "email": email, "password": "password123"},
                follow_redirects=True,
            )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        for email in ["bob@example.com", "cara@example.com", "diego@example.com"]:
            self.client.get("/logout", follow_redirects=True)
            self.client.post("/login", data={"email": email, "password": "password123"}, follow_redirects=True)
            self.client.post("/groups/join", data={"slug": "club", "invite_code": invite_code}, follow_redirects=True)
        self.client.get("/logout", follow_redirects=True)
        self.client.post("/login", data={"email": "alice@example.com", "password": "password123"}, follow_redirects=True)
        saved = self.client.post(
            "/groups/club/matches",
            data={
                "game_type": "one_arm_one_brain",
                "white_user_id": "1",
                "white_partner_user_id": "2",
                "black_user_id": "3",
                "black_partner_user_id": "4",
                "result": "white",
                "played_at": "2026-04-02",
                "season_id": "",
                "challenge_id": "",
                "opening_name": "Italian Game",
                "opening_code": "C50",
                "white_instruction_clarity": "4",
                "black_instruction_clarity": "2",
                "pgn_text": "",
                "notes": "Braccio Mente lunch final",
            },
            follow_redirects=True,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertIn(b"Braccio Mente", saved.data)
        self.assertIn(b"Alice + Bob", saved.data)
        with self.app.app_context():
            db = get_db()
            rows = db.execute(
                "SELECT user_id, delta, ladder_type FROM rating_history WHERE match_id = 1 ORDER BY user_id"
            ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["user_id"] for row in rows}, {1, 2, 3, 4})
        self.assertTrue(all(row["delta"] != 0 for row in rows))
        self.assertEqual({row["ladder_type"] for row in rows}, {"braccio_mente"})

    def test_braccio_rating_does_not_affect_standard_leaderboard(self):
        for name, email in [
            ("Alice", "alice@example.com"),
            ("Bob", "bob@example.com"),
            ("Cara", "cara@example.com"),
            ("Diego", "diego@example.com"),
        ]:
            self.client.post(
                "/register",
                data={"name": name, "email": email, "password": "password123"},
                follow_redirects=True,
            )
        self.client.post("/login", data={"email": "alice@example.com", "password": "password123"}, follow_redirects=True)
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        for email in ["bob@example.com", "cara@example.com", "diego@example.com"]:
            self.client.get("/logout", follow_redirects=True)
            self.client.post("/login", data={"email": email, "password": "password123"}, follow_redirects=True)
            self.client.post("/groups/join", data={"slug": "club", "invite_code": invite_code}, follow_redirects=True)
        self.client.get("/logout", follow_redirects=True)
        self.client.post("/login", data={"email": "alice@example.com", "password": "password123"}, follow_redirects=True)
        self.client.post(
            "/groups/club/matches",
            data={
                "game_type": "one_arm_one_brain",
                "white_user_id": "1",
                "white_partner_user_id": "2",
                "black_user_id": "3",
                "black_partner_user_id": "4",
                "result": "white",
                "played_at": "2026-04-02",
                "season_id": "",
                "challenge_id": "",
                "opening_name": "",
                "opening_code": "",
                "white_instruction_clarity": "4",
                "black_instruction_clarity": "2",
                "pgn_text": "",
                "notes": "",
            },
            follow_redirects=True,
        )
        dashboard = self.client.get("/groups/club")
        stats = self.client.get("/groups/club/stats")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(stats.status_code, 200)
        self.assertIn(b"One Arm, One Brain ladder", dashboard.data)
        self.assertIn(b"One Arm, One Brain Matches", dashboard.data)
        self.assertIn(b"avatar-profile-link", dashboard.data)
        self.assertIn(b'href="/groups/club/members/1"', dashboard.data)
        self.assertIn(b'href="/groups/club/members/2"', dashboard.data)
        self.assertIn(b"avatar-profile-link", stats.data)
        self.assertIn(b'href="/groups/club/members/1"', stats.data)
        self.assertIn(b'href="/groups/club/members/2"', stats.data)
        with self.app.app_context():
            db = get_db()
            standard_rows = db.execute("SELECT * FROM rating_history WHERE ladder_type = 'standard'").fetchall()
            braccio_rows = db.execute("SELECT * FROM rating_history WHERE ladder_type = 'braccio_mente'").fetchall()
        self.assertEqual(len(standard_rows), 0)
        self.assertEqual(len(braccio_rows), 4)

    def test_dashboard_match_counts_only_include_confirmed_matches_by_type(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Cara", "email": "cara@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Diego", "email": "diego@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post("/login", data={"email": "alice@example.com", "password": "password123"}, follow_redirects=True)
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        for email in ["bob@example.com", "cara@example.com", "diego@example.com"]:
            self.client.get("/logout", follow_redirects=True)
            self.client.post("/login", data={"email": email, "password": "password123"}, follow_redirects=True)
            self.client.post("/groups/join", data={"slug": "club", "invite_code": invite_code}, follow_redirects=True)
        self.client.get("/logout", follow_redirects=True)
        self.client.post("/login", data={"email": "alice@example.com", "password": "password123"}, follow_redirects=True)
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO matches
                (group_id, game_type, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 'standard', 1, 2, 'white', '2026-04-01', 1, 'confirmed', 1)
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (group_id, game_type, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status)
                VALUES (1, 'standard', 2, 1, 'black', '2026-04-02', 2, 'pending')
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (group_id, game_type, white_user_id, white_partner_user_id, black_user_id, black_partner_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 'one_arm_one_brain', 1, 2, 3, 4, 'white', '2026-04-03', 1, 'confirmed', 1)
                """
            )
            db.commit()
            recalculate_group_ratings(db, 1)
        dashboard = self.client.get("/groups/club")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"1 Standard Matches", dashboard.data)
        self.assertIn(b"1 One Arm, One Brain Matches", dashboard.data)
        self.assertIn(b"Matches logged", dashboard.data)
        self.assertIn(b'<span class="stat-tile-value">2</span>', dashboard.data)

    def test_invite_link_flow_prefills_join_for_logged_out_user(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        self.client.get("/logout", follow_redirects=True)
        invite = self.client.get(f"/invite/club/{invite_code}", follow_redirects=True)
        self.assertEqual(invite.status_code, 200)
        self.assertIn(b"Group invite", invite.data)
        self.assertIn(invite_code.encode(), invite.data)

        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        joined_page = self.client.post(
            "/login",
            data={"email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.assertEqual(joined_page.status_code, 200)
        self.assertIn(invite_code.encode(), joined_page.data)
        join_result = self.client.post(
            "/groups/join",
            data={"slug": "club", "invite_code": invite_code},
            follow_redirects=True,
        )
        self.assertEqual(join_result.status_code, 200)
        self.assertIn(b"You joined the group", join_result.data)

    def test_repo_snapshot_and_data_export(self):
        self.app.config["AUTO_SNAPSHOT_TO_REPO"] = True
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        latest_snapshot = self.snapshot_dir / "latest.json"
        self.assertTrue(latest_snapshot.exists())
        export_response = self.client.get("/system/data/export.json")
        self.assertEqual(export_response.status_code, 200)
        self.assertIn(b'"groups_workspace"', export_response.data)

    def test_network_page_shows_shareable_urls(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        page = self.client.get("/system/network")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Network access", page.data)
        self.assertIn(b"5000", page.data)

    def test_system_data_page_shows_recent_signups_for_owner(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        page = self.client.get("/system/data")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"System data", page.data)
        self.assertIn(b"Recent signups", page.data)
        self.assertIn(b"alice@example.com", page.data)
        self.assertIn(b"test.sqlite3", page.data)

    def test_system_data_page_requires_owner_or_admin(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.commit()
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        page = self.client.get("/system/data", follow_redirects=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Only group owners or admins can manage app data.", page.data)

    def test_diagnostics_route_redirects_to_system_data(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        page = self.client.get("/system/diagnostics", follow_redirects=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"System data", page.data)

    def test_stats_page_renders_with_no_matches(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        stats = self.client.get("/groups/club/stats")
        self.assertEqual(stats.status_code, 200)
        self.assertIn(b"Competition trends", stats.data)
        self.assertIn(b"No rivalries yet", stats.data)

    def test_group_settings_show_full_invite_links(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        page = self.client.get("/groups/club/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/invite/club/", page.data)

    def test_create_season_stores_end_date(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        page = self.client.post(
            "/groups/club/seasons",
            data={
                "action": "create",
                "name": "Q2 2026",
                "start_date": "2026-04-01",
                "end_date": "2026-06-30",
                "reset_ratings": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"2026-06-30", page.data)
        with self.app.app_context():
            db = get_db()
            season = db.execute("SELECT end_date FROM seasons WHERE group_id = 1 ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(season["end_date"], "2026-06-30")

    def test_seasons_page_shows_coffee_leader_per_season(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/register",
            data={"name": "Bob", "email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            invite_code = db.execute("SELECT invite_code FROM groups_workspace WHERE id = 1").fetchone()["invite_code"]
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "bob@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/join",
            data={"slug": "club", "invite_code": invite_code},
            follow_redirects=True,
        )
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/club/seasons",
            data={
                "action": "create",
                "name": "Q2 2026",
                "start_date": "2026-04-01",
                "end_date": "2026-06-30",
                "reset_ratings": "",
            },
            follow_redirects=True,
        )
        self.client.post(
            "/groups/club/matches",
            data={
                "game_type": "standard",
                "white_user_id": "1",
                "black_user_id": "2",
                "result": "white",
                "played_at": "2026-04-10",
                "season_id": "2",
                "challenge_id": "",
                "opening_name": "",
                "opening_code": "",
                "pgn_text": "",
                "notes": "",
                "white_partner_user_id": "",
                "black_partner_user_id": "",
                "white_instruction_clarity": "",
                "black_instruction_clarity": "",
            },
            follow_redirects=True,
        )
        page = self.client.get("/groups/club/seasons")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Coffee leader: Alice", page.data)

    def test_winners_page_shows_closed_winner_and_active_leader(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (id, name, email, password_hash) VALUES (?, ?, ?, ?)",
                (2, "Bob", "bob@example.com", generate_password_hash("password123")),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute("UPDATE seasons SET name = 'Spring', start_date = '2026-03-01', end_date = '2026-03-31', is_active = 0 WHERE id = 1")
            db.execute(
                "INSERT INTO seasons (id, group_id, name, start_date, end_date, is_active, reset_ratings) VALUES (2, 1, 'Summer', '2026-04-01', '2026-06-30', 1, 0)"
            )
            db.execute(
                """
                INSERT INTO matches
                (id, group_id, season_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 1, 1, 1, 2, 'white', '2026-03-15', 1, 'confirmed', 1)
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (id, group_id, season_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (2, 1, 2, 2, 1, 'white', '2026-04-10', 1, 'confirmed', 1)
                """
            )
            db.commit()
            recalculate_group_ratings(db, 1)
        page = self.client.get("/groups/club/winners")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Spring", page.data)
        self.assertIn(b"Alice finished on", page.data)
        self.assertIn(b"Summer", page.data)
        self.assertIn(b"Season is not closed yet.", page.data)
        self.assertIn(b"2026-06-30", page.data)
        self.assertIn(b"Current leader: Bob on", page.data)

    def test_encoding_repair_fixes_mojibake_in_text_and_html(self):
        self.assertEqual(repair_text_encoding("FranÃ§ais"), "Français")
        self.assertEqual(translate_text("it", "Coffee"), "Caffè")
        repaired_html = translate_html("en", "<button>Menu â–¾</button><p>Stats Â· Club</p>")
        self.assertIn("Menu ▾", repaired_html)
        self.assertIn("Stats · Club", repaired_html)

    def test_stats_page_includes_standard_leaderboard(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Bob", "bob@example.com", generate_password_hash("password123")),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.commit()
        self.client.post(
            "/groups/club/matches",
            data={
                "game_type": "standard",
                "white_user_id": "1",
                "black_user_id": "2",
                "result": "white",
                "played_at": "2026-01-02",
                "season_id": "",
                "challenge_id": "",
                "opening_name": "",
                "opening_code": "",
                "pgn_text": "",
                "notes": "",
                "white_partner_user_id": "",
                "black_partner_user_id": "",
                "white_instruction_clarity": "",
                "black_instruction_clarity": "",
            },
            follow_redirects=True,
        )
        stats = self.client.get("/groups/club/stats")
        self.assertEqual(stats.status_code, 200)
        self.assertIn(b"Leaderboard", stats.data)
        self.assertIn(b"Alice", stats.data)
        self.assertIn(b"Bob", stats.data)
        self.assertIn(b"1212", stats.data)
        self.assertIn(b"avatar-profile-link", stats.data)
        self.assertIn(b'href="/groups/club/members/1"', stats.data)
        self.assertIn(b'href="/groups/club/members/2"', stats.data)

    def test_rating_history_chart_shows_axes_and_late_joiner_timeline(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            for name, email, user_id in [
                ("Bob", "bob@example.com", 2),
                ("Charlie", "charlie@example.com", 3),
            ]:
                db.execute(
                    "INSERT INTO users (id, name, email, password_hash) VALUES (?, ?, ?, ?)",
                    (user_id, name, email, generate_password_hash("password123")),
                )
                db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, ?, 'member')", (user_id,))
            db.execute(
                """
                INSERT INTO matches
                (group_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 1, 2, 'white', '2026-01-02', 1, 'confirmed', 1)
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (group_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 2, 1, 'black', '2026-01-03', 1, 'confirmed', 1)
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (group_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 3, 1, 'white', '2026-01-04', 1, 'confirmed', 1)
                """
            )
            db.commit()
            recalculate_group_ratings(db, 1)
        stats = self.client.get("/groups/club/stats")
        self.assertEqual(stats.status_code, 200)
        self.assertIn(b"Matches tracked", stats.data)
        page = stats.data.decode("utf-8")
        chart = re.search(r'data-chart="line"[^>]*>\s*<script type="application/json">(.*?)</script>', page, re.S)
        self.assertIsNotNone(chart)
        payload = json.loads(chart.group(1))
        self.assertEqual([m["date"] for m in payload["matches"]], ["2026-01-02", "2026-01-03", "2026-01-04"])
        self.assertIn("Alice vs Bob", payload["matches"][0]["label"])
        series_by_name = {series["name"]: series for series in payload["series"]}
        self.assertIn("Charlie", series_by_name)
        self.assertEqual(series_by_name["Charlie"]["points"][0]["i"], 2)
        self.assertEqual(series_by_name["Alice"]["points"][0]["d"], 12.0)

    def test_rating_history_chart_orders_matches_by_played_at_not_match_id(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (id, name, email, password_hash) VALUES (?, ?, ?, ?)",
                (2, "Bob", "bob@example.com", generate_password_hash("password123")),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute(
                """
                INSERT INTO matches
                (id, group_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (1, 1, 1, 2, 'white', '2026-01-03', 1, 'confirmed', 1)
                """
            )
            db.execute(
                """
                INSERT INTO matches
                (id, group_id, white_user_id, black_user_id, result, played_at, reported_by, confirmation_status, confirmed_by)
                VALUES (2, 1, 2, 1, 'black', '2026-01-02', 1, 'confirmed', 1)
                """
            )
            db.commit()
            recalculate_group_ratings(db, 1)
        stats = self.client.get("/groups/club/stats")
        self.assertEqual(stats.status_code, 200)
        page = stats.data.decode("utf-8")
        chart = re.search(r'data-chart="line"[^>]*>\s*<script type="application/json">(.*?)</script>', page, re.S)
        self.assertIsNotNone(chart)
        payload = json.loads(chart.group(1))
        self.assertEqual([m["date"] for m in payload["matches"]][:2], ["2026-01-02", "2026-01-03"])

    def test_match_results_create_default_coffee_debts_and_can_be_optimized(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Bob", "bob@example.com", generate_password_hash("password123")),
            )
            db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Charlie", "charlie@example.com", generate_password_hash("password123")),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 3, 'member')")
            db.commit()

        for white_user_id, black_user_id, result, played_at in [
            ("1", "2", "white", "2026-01-02"),
            ("3", "1", "white", "2026-01-03"),
        ]:
            self.client.post(
                "/groups/club/matches",
                data={
                    "game_type": "standard",
                    "white_user_id": white_user_id,
                    "black_user_id": black_user_id,
                    "result": result,
                    "played_at": played_at,
                    "season_id": "",
                    "challenge_id": "",
                    "opening_name": "",
                    "opening_code": "",
                    "pgn_text": "",
                    "notes": "",
                    "white_partner_user_id": "",
                    "black_partner_user_id": "",
                    "white_instruction_clarity": "",
                    "black_instruction_clarity": "",
                },
                follow_redirects=True,
            )

        coffee_before = self.client.get("/groups/club/coffee")
        self.assertEqual(coffee_before.status_code, 200)
        self.assertIn(b"Bob owes Alice", coffee_before.data)
        self.assertIn(b"Alice owes Charlie", coffee_before.data)

        optimized = self.client.post(
            "/groups/club/coffee",
            data={"action": "optimize"},
            follow_redirects=True,
        )
        self.assertEqual(optimized.status_code, 200)
        self.assertIn(b"Coffee debts optimized.", optimized.data)
        self.assertIn(b"Bob owes Charlie", optimized.data)
        self.assertNotIn(b"Bob owes Alice", optimized.data)
        self.assertNotIn(b"Alice owes Charlie", optimized.data)

        with self.app.app_context():
            db = get_db()
            optimized_rows = db.execute(
                """
                SELECT debtor_user_id, creditor_user_id, amount, entry_type
                FROM coffee_ledger
                WHERE group_id = 1 AND is_settled = 0
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(len(optimized_rows), 1)
        self.assertEqual(optimized_rows[0]["debtor_user_id"], 2)
        self.assertEqual(optimized_rows[0]["creditor_user_id"], 3)
        self.assertEqual(optimized_rows[0]["amount"], 1)
        self.assertEqual(optimized_rows[0]["entry_type"], "optimization")

    def test_coffee_page_displays_pizza_and_cake_breakdown(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Bob", "bob@example.com", generate_password_hash("password123")),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.execute(
                """
                INSERT INTO coffee_ledger
                (group_id, debtor_user_id, creditor_user_id, amount, reason, entry_type, created_by)
                VALUES (1, 2, 1, 53, 'Tournament finale', 'manual', 1)
                """
            )
            db.commit()

        coffee = self.client.get("/groups/club/coffee")
        self.assertEqual(coffee.status_code, 200)
        self.assertIn(b"1 cake, 3 coffees", coffee.data)
        self.assertIn(b"Top creditor", coffee.data)
        self.assertIn(b"Treat type", coffee.data)
        self.assertIn(b'class="treat-pill"', coffee.data)
        self.assertIn(b'alt="cake"', coffee.data)
        self.assertIn(b'alt="coffee"', coffee.data)

    def test_new_achievements_are_awarded_and_shown_on_coffee_page(self):
        self.client.post(
            "/register",
            data={"name": "Alice", "email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "password123"},
            follow_redirects=True,
        )
        self.client.post(
            "/groups/create",
            data={
                "name": "Club",
                "slug": "club",
                "description": "Chess",
                "company_domain": "",
                "starting_rating": "1200",
                "default_k_factor": "24",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Bob", "bob@example.com", generate_password_hash("password123")),
            )
            db.execute("INSERT INTO memberships (group_id, user_id, role) VALUES (1, 2, 'member')")
            db.commit()

        for index, result in enumerate(["white", "white", "white", "white", "white", "black", "black", "black"], start=1):
            self.client.post(
                "/groups/club/matches",
                data={
                    "game_type": "standard",
                    "white_user_id": "1",
                    "black_user_id": "2",
                    "result": result,
                    "played_at": f"2026-01-{index:02d}",
                    "season_id": "",
                    "challenge_id": "",
                    "opening_name": "",
                    "opening_code": "",
                    "pgn_text": "",
                    "notes": "",
                    "white_partner_user_id": "",
                    "black_partner_user_id": "",
                    "white_instruction_clarity": "",
                    "black_instruction_clarity": "",
                },
                follow_redirects=True,
            )

        self.client.post(
            "/groups/club/coffee",
            data={
                "action": "add",
                "debtor_user_id": "2",
                "creditor_user_id": "1",
                "unit": "coffee",
                "unit_count": "5",
                "reason": "Extra tab",
            },
            follow_redirects=True,
        )
        coffee = self.client.post(
            "/groups/club/coffee",
            data={
                "action": "add",
                "debtor_user_id": "1",
                "creditor_user_id": "2",
                "unit": "cake",
                "unit_count": "1",
                "reason": "Settled in cake theory",
            },
            follow_redirects=True,
        )
        self.assertEqual(coffee.status_code, 200)
        self.assertIn(b"achievement-reward-coffee_debt_50.png", coffee.data)
        self.assertIn(b"achievement-reward-coffee_top_creditor.png", coffee.data)

        with self.app.app_context():
            db = get_db()
            alice_achievements = {
                row["achievement_key"]
                for row in db.execute("SELECT achievement_key FROM user_achievements WHERE user_id = 1").fetchall()
            }
            bob_achievements = {
                row["achievement_key"]
                for row in db.execute("SELECT achievement_key FROM user_achievements WHERE user_id = 2").fetchall()
            }

        self.assertTrue(
            {
                "upset_victim_100",
                "coffee_debt_1",
                "coffee_debt_10",
                "coffee_debt_50",
                "coffee_chain_link",
                "rematch_punished_3",
            }.issubset(alice_achievements)
        )
        self.assertTrue(
            {
                "loss_first",
                "loss_streak_3",
                "loss_streak_survived",
                "comeback_after_losses",
                "coffee_credit_1",
                "coffee_credit_10",
                "coffee_credit_50",
                "coffee_top_creditor",
                "coffee_chain_link",
            }.issubset(bob_achievements)
        )


if __name__ == "__main__":
    unittest.main()
