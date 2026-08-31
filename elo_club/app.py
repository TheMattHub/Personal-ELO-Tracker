from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import socket
import smtplib
import base64
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from itertools import combinations
from statistics import mean

from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from PIL import Image, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash

from .db import export_data_snapshot, get_db, import_data_snapshot, init_app as init_db_app, init_db
from .elo import calculate_elo_change, recalculate_group_ratings
from .i18n import SUPPORTED_LANGUAGES, translate_html, translate_text

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TIME_CONTROL_PRESETS = [
    {"value": "60|0", "label": "Bullet 1+0", "base_seconds": 60, "increment_seconds": 0},
    {"value": "120|1", "label": "Bullet 2+1", "base_seconds": 120, "increment_seconds": 1},
    {"value": "180|0", "label": "Blitz 3+0", "base_seconds": 180, "increment_seconds": 0},
    {"value": "180|2", "label": "Blitz 3+2", "base_seconds": 180, "increment_seconds": 2},
    {"value": "300|0", "label": "Blitz 5+0", "base_seconds": 300, "increment_seconds": 0},
    {"value": "300|3", "label": "Blitz 5+3", "base_seconds": 300, "increment_seconds": 3},
    {"value": "600|0", "label": "Rapid 10+0", "base_seconds": 600, "increment_seconds": 0},
    {"value": "600|5", "label": "Rapid 10+5", "base_seconds": 600, "increment_seconds": 5},
    {"value": "900|10", "label": "Rapid 15+10", "base_seconds": 900, "increment_seconds": 10},
    {"value": "1200|0", "label": "Rapid 20+0", "base_seconds": 1200, "increment_seconds": 0},
    {"value": "1800|0", "label": "Classical 30+0", "base_seconds": 1800, "increment_seconds": 0},
]

TREAT_UNIT_VALUES = {
    "coffee": 1,
    "pizza": 10,
    "cake": 50,
}

ACHIEVEMENT_BACKFILL_KEY = "achievement_backfill_v2026_04_28"
FORMER_EMPLOYEE_AVATAR_ICON = "former-employee-tombstone"
FORMER_EMPLOYEE_TITLE = "Former Employee"


def load_local_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def create_app(test_config: dict | None = None) -> Flask:
    load_local_env()
    app = Flask(__name__, instance_relative_config=True)
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        # No shared, publicly-known default: a static fallback here would let anyone who
        # reads this open-source repo forge session cookies for any install that forgot to
        # set SECRET_KEY. A random per-process key keeps `python app.py` working out of the
        # box; set SECRET_KEY in .env for sessions that survive a restart.
        secret_key = secrets.token_hex(32)
        if not (test_config or {}).get("TESTING"):
            app.logger.warning(
                "SECRET_KEY is not set in the environment; using a temporary random key for this run. "
                "Set SECRET_KEY in .env so logins survive a restart."
            )
    app.config.from_mapping(
        SECRET_KEY=secret_key,
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "elo-club.sqlite3")),
        AUTO_SNAPSHOT_TO_REPO=os.environ.get("AUTO_SNAPSHOT_TO_REPO", "0") == "1",
        AUTO_GIT_COMMIT=os.environ.get("AUTO_GIT_COMMIT", "0") == "1",
        SNAPSHOT_DIR=os.environ.get("SNAPSHOT_DIR", os.path.join(os.path.dirname(app.root_path), "data", "snapshots")),
        DEFAULT_LANGUAGE=os.environ.get("DEFAULT_LANGUAGE", "en"),
        SMTP_HOST=os.environ.get("SMTP_HOST", "").strip(),
        SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USERNAME=os.environ.get("SMTP_USERNAME", "").strip(),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", "").strip(),
        SMTP_USE_TLS=os.environ.get("SMTP_USE_TLS", "1") == "1",
        MAIL_SENDER=os.environ.get("MAIL_SENDER", "").strip(),
        MAIL_OUTBOX=os.environ.get("MAIL_OUTBOX", os.path.join(app.instance_path, "mail-outbox.log")),
    )

    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    init_db_app(app)

    def ui_icon(name: str) -> str:
        png_path = Path(app.static_folder) / "icons" / f"{name}.png"
        if png_path.exists():
            return url_for("static", filename=f"icons/{name}.png")
        return url_for("static", filename=f"icons/{name}.svg")

    app.jinja_env.globals["ui_icon"] = ui_icon

    def language_flag(code: str) -> str:
        return url_for("static", filename=f"icons/flag-{code}.svg")

    BASE_AVATAR_CHOICES = [
        ("initials", "Initials"),
        ("king", "King"),
        ("queen", "Queen"),
        ("rook", "Rook"),
        ("bishop", "Bishop"),
        ("knight", "Knight"),
        ("pawn", "Pawn"),
        ("card-king", "Card King"),
        ("card-queen", "Card Queen"),
        ("card-jack", "Card Jack"),
        ("card-ace", "Card Ace"),
        ("card-jolly", "Card Jolly"),
        ("crown", "Crown"),
        ("trophy", "Trophy"),
        ("clock", "Clock"),
        ("coffee", "Coffee"),
        ("bolt", "Lightning"),
        ("snail", "Snail"),
        ("gambit", "Gambit"),
    ]

    ACHIEVEMENT_AVATAR_CHOICES = [
        ("laurel", "Laurel"),
        ("comet", "Comet"),
        ("phoenix", "Phoenix"),
        ("dragon", "Dragon"),
        ("mask", "Mask"),
        ("orbit", "Orbit"),
        ("fortress", "Fortress"),
        ("hourglass", "Hourglass"),
    ]

    ACHIEVEMENT_DEFINITIONS = [
        {"key": "first_win", "name": "First Blood", "description": "Win your first standard 1v1 game.", "reward_title": "On the Board", "reward_avatar_icon": None, "threshold": 1, "metric": "standard_wins"},
        {"key": "loss_first", "name": "First Loss", "description": "Lose your first standard 1v1 game.", "reward_title": "First Loss", "reward_avatar_icon": None, "threshold": 1, "metric": "standard_losses"},
        {"key": "loss_streak_3", "name": "Repeated Losses", "description": "Lose 3 standard matches in a row.", "reward_title": "Repeated Losses", "reward_avatar_icon": None, "threshold": 3, "metric": "max_standard_loss_streak"},
        {"key": "loss_streak_survived", "name": "Surviving a Losing Streak", "description": "Play another standard match after a 3-match losing streak.", "reward_title": "Surviving a Losing Streak", "reward_avatar_icon": None, "threshold": 1, "metric": "survived_standard_loss_streak"},
        {"key": "comeback_after_losses", "name": "Comeback After Losses", "description": "Win a standard match right after losing at least 2 in a row.", "reward_title": "Comeback After Losses", "reward_avatar_icon": None, "threshold": 1, "metric": "comeback_after_loss_streak"},
        {"key": "upset_victim_100", "name": "Upset by a Lower-Rated Player", "description": "Lose a standard match to someone rated 100 ELO points or more below you.", "reward_title": "Upset by a Lower-Rated Player", "reward_avatar_icon": None, "threshold": 100, "metric": "worst_upset_loss_gap"},
        {"key": "games_10", "name": "Regular", "description": "Play 10 confirmed matches in the club.", "reward_title": "Regular", "reward_avatar_icon": None, "threshold": 10, "metric": "total_games"},
        {"key": "games_25", "name": "Table Fixture", "description": "Play 25 confirmed matches in the club.", "reward_title": "Table Fixture", "reward_avatar_icon": "orbit", "threshold": 25, "metric": "total_games"},
        {"key": "games_50", "name": "Lunchroom Legend", "description": "Play 50 confirmed matches in the club.", "reward_title": "Lunchroom Legend", "reward_avatar_icon": "laurel", "threshold": 50, "metric": "total_games"},
        {"key": "streak_3", "name": "Hot Hand", "description": "Reach a 3-game standard win streak.", "reward_title": "Hot Hand", "reward_avatar_icon": None, "threshold": 3, "metric": "max_standard_win_streak"},
        {"key": "streak_5", "name": "Untouchable", "description": "Reach a 5-game standard win streak.", "reward_title": "Untouchable", "reward_avatar_icon": "comet", "threshold": 5, "metric": "max_standard_win_streak"},
        {"key": "streak_8", "name": "Avalanche", "description": "Reach an 8-game standard win streak.", "reward_title": "Avalanche", "reward_avatar_icon": "phoenix", "threshold": 8, "metric": "max_standard_win_streak"},
        {"key": "giant_100", "name": "Underdog Hunter", "description": "Win a standard match while trailing by 100 ELO points or more.", "reward_title": "Underdog Hunter", "reward_avatar_icon": None, "threshold": 100, "metric": "best_upset_gap"},
        {"key": "giant_180", "name": "Dragon Slayer", "description": "Win a standard match while trailing by 180 ELO points or more.", "reward_title": "Dragon Slayer", "reward_avatar_icon": "dragon", "threshold": 180, "metric": "best_upset_gap"},
        {"key": "giant_250", "name": "Kingbreaker", "description": "Win a standard match while trailing by 250 ELO points or more.", "reward_title": "Kingbreaker", "reward_avatar_icon": "fortress", "threshold": 250, "metric": "best_upset_gap"},
        {"key": "draws_5", "name": "Peacekeeper", "description": "Record 5 standard draws.", "reward_title": "Peacekeeper", "reward_avatar_icon": None, "threshold": 5, "metric": "standard_draws"},
        {"key": "two_colors_3", "name": "Two-Color Threat", "description": "Win at least 3 standard games as White and 3 as Black.", "reward_title": "Two-Color Threat", "reward_avatar_icon": "mask", "threshold": 1, "metric": "two_color_mastery"},
        {"key": "openings_5", "name": "Bookworm", "description": "Win or lose with 5 distinct tagged openings.", "reward_title": "Bookworm", "reward_avatar_icon": None, "threshold": 5, "metric": "unique_openings"},
        {"key": "openings_12", "name": "Opening Collector", "description": "Log 12 distinct tagged openings.", "reward_title": "Opening Collector", "reward_avatar_icon": "hourglass", "threshold": 12, "metric": "unique_openings"},
        {"key": "coffee_5", "name": "Coffee Earner", "description": "Earn 5 credited coffees.", "reward_title": "Coffee Earner", "reward_avatar_icon": None, "threshold": 5, "metric": "coffee_credited"},
        {"key": "coffee_15", "name": "Coffee Baron", "description": "Earn 15 credited coffees.", "reward_title": "Coffee Baron", "reward_avatar_icon": None, "threshold": 15, "metric": "coffee_credited"},
        {"key": "coffee_debt_1", "name": "Owing Coffee", "description": "Owe your first coffee.", "reward_title": "Owing Coffee", "reward_avatar_icon": None, "threshold": 1, "metric": "coffee_owed"},
        {"key": "coffee_debt_10", "name": "Owing a Pizza Worth of Coffee", "description": "Owe 10 coffees in total.", "reward_title": "Owing a Pizza Worth of Coffee", "reward_avatar_icon": None, "threshold": 10, "metric": "coffee_owed"},
        {"key": "coffee_debt_50", "name": "Owing a Cake Worth of Coffee", "description": "Owe 50 coffees in total.", "reward_title": "Owing a Cake Worth of Coffee", "reward_avatar_icon": None, "threshold": 50, "metric": "coffee_owed"},
        {"key": "coffee_credit_1", "name": "Earning Coffee Credit", "description": "Earn your first coffee credit.", "reward_title": "Earning Coffee Credit", "reward_avatar_icon": None, "threshold": 1, "metric": "coffee_credited"},
        {"key": "coffee_credit_10", "name": "Earning Pizza-Tier Credit", "description": "Earn 10 coffee credits.", "reward_title": "Earning Pizza-Tier Credit", "reward_avatar_icon": None, "threshold": 10, "metric": "coffee_credited"},
        {"key": "coffee_credit_50", "name": "Earning Cake-Tier Credit", "description": "Earn 50 coffee credits.", "reward_title": "Earning Cake-Tier Credit", "reward_avatar_icon": None, "threshold": 50, "metric": "coffee_credited"},
        {"key": "coffee_top_creditor", "name": "Being the Top Creditor", "description": "Hold the largest open positive balance in the group.", "reward_title": "Being the Top Creditor", "reward_avatar_icon": None, "threshold": 1, "metric": "is_top_creditor"},
        {"key": "coffee_chain_link", "name": "Debt-Chain Optimization", "description": "Be owed coffees while also owing someone else at the same time.", "reward_title": "Debt-Chain Optimization", "reward_avatar_icon": None, "threshold": 1, "metric": "coffee_chain_links"},
        {"key": "rematch_punished_3", "name": "Frequent Rematch Punishment", "description": "Lose 3 standard matches to the same opponent.", "reward_title": "Frequent Rematch Punishment", "reward_avatar_icon": None, "threshold": 3, "metric": "max_standard_losses_vs_same_opponent"},
        {"key": "team_wins_3", "name": "Mind Meld", "description": "Win 3 One Arm, One Brain matches.", "reward_title": "Mind Meld", "reward_avatar_icon": None, "threshold": 3, "metric": "team_wins"},
        {"key": "team_wins_10", "name": "Telepath Supreme", "description": "Win 10 One Arm, One Brain matches.", "reward_title": "Telepath Supreme", "reward_avatar_icon": "orbit", "threshold": 10, "metric": "team_wins"},
        {"key": "peak_1400", "name": "1400 Club", "description": "Reach a 1400 standard rating peak.", "reward_title": "1400 Club", "reward_avatar_icon": None, "threshold": 1400, "metric": "peak_standard_rating"},
        {"key": "peak_1600", "name": "Master of Lunch", "description": "Reach a 1600 standard rating peak.", "reward_title": "Master of Lunch", "reward_avatar_icon": "laurel", "threshold": 1600, "metric": "peak_standard_rating"},
        {"key": "season_podium", "name": "Podium Finisher", "description": "Finish in the top 3 of a closed season.", "reward_title": "Podium Finisher", "reward_avatar_icon": None, "threshold": 1, "metric": "season_podiums"},
        {"key": "season_champion", "name": "Season Champion", "description": "Win a closed season.", "reward_title": "Season Champion", "reward_avatar_icon": "laurel", "threshold": 1, "metric": "seasons_won"},
        {"key": "season_dynasty", "name": "Dynasty Builder", "description": "Win 3 closed seasons.", "reward_title": "Dynasty Builder", "reward_avatar_icon": "fortress", "threshold": 3, "metric": "seasons_won"},
        {"key": "challenge_callout_1", "name": "Glove Dropper", "description": "Send your first chess challenge.", "reward_title": "Glove Dropper", "reward_avatar_icon": None, "threshold": 1, "metric": "challenges_sent"},
        {"key": "suggestion_spark_1", "name": "Pairing Whisperer", "description": "Launch a challenge from a suggested opponent.", "reward_title": "Pairing Whisperer", "reward_avatar_icon": None, "threshold": 1, "metric": "suggestion_challenges_sent"},
        {"key": "challenge_completed_3", "name": "Duel Finisher", "description": "Complete 3 challenge matches.", "reward_title": "Duel Finisher", "reward_avatar_icon": "bolt", "threshold": 3, "metric": "challenge_matches_completed"},
        {"key": "challenge_wins_3", "name": "Gauntlet Keeper", "description": "Win 3 completed challenge matches.", "reward_title": "Gauntlet Keeper", "reward_avatar_icon": "crown", "threshold": 3, "metric": "challenge_match_wins"},
    ]

    achievement_definitions_by_key = {item["key"]: item for item in ACHIEVEMENT_DEFINITIONS}
    coffee_achievement_keys = {
        "coffee_debt_1",
        "coffee_debt_10",
        "coffee_debt_50",
        "coffee_credit_1",
        "coffee_credit_10",
        "coffee_credit_50",
        "coffee_top_creditor",
        "coffee_chain_link",
    }
    reward_avatar_icon_codes = {item["reward_avatar_icon"] for item in ACHIEVEMENT_DEFINITIONS if item["reward_avatar_icon"]}

    def achievement_avatar_code(achievement_key: str) -> str:
        return f"achievement:{achievement_key}"

    def achievement_avatar_key(icon_name: str | None) -> str | None:
        if icon_name and icon_name.startswith("achievement:"):
            return icon_name.split(":", 1)[1]
        return None

    valid_avatar_icons = {code for code, _ in BASE_AVATAR_CHOICES} | reward_avatar_icon_codes | {"uploaded", FORMER_EMPLOYEE_AVATAR_ICON}

    def avatar_asset(icon_name: str | None, user_id: int | None = None) -> str | None:
        if not icon_name or icon_name == "initials":
            return None
        if icon_name == "uploaded" and user_id:
            return url_for("uploaded_avatar", user_id=user_id)
        if icon_name == FORMER_EMPLOYEE_AVATAR_ICON:
            return url_for("static", filename="icons/avatar-former-employee-tombstone.png")
        achievement_key = achievement_avatar_key(icon_name)
        if achievement_key:
            return achievement_reward_asset(achievement_key)
        return url_for("static", filename=f"icons/avatar-{icon_name}.svg")

    def achievement_reward_asset(achievement_key: str, reward_avatar_icon: str | None = None) -> str:
        achievement_path = Path(app.static_folder) / "icons" / f"achievement-reward-{achievement_key}.png"
        if achievement_path.exists():
            return url_for("static", filename=f"icons/achievement-reward-{achievement_key}.png")
        achievement_svg_path = Path(app.static_folder) / "icons" / f"achievement-reward-{achievement_key}.svg"
        if achievement_svg_path.exists():
            return url_for("static", filename=f"icons/achievement-reward-{achievement_key}.svg")
        fallback = avatar_asset(reward_avatar_icon)
        return fallback or ui_icon("section-achievement")

    def tr(key: str, **kwargs) -> str:
        lang = getattr(g, "lang", app.config["DEFAULT_LANGUAGE"])
        return translate_text(lang, key, **kwargs)

    @app.before_request
    def load_language() -> None:
        session_language = session.get("language")
        if session_language in SUPPORTED_LANGUAGES:
            g.lang = session_language
        else:
            g.lang = request.accept_languages.best_match(list(SUPPORTED_LANGUAGES.keys())) or app.config["DEFAULT_LANGUAGE"]

    @app.before_request
    def load_theme() -> None:
        cookie_theme = request.cookies.get("theme")
        g.theme = cookie_theme if cookie_theme in {"light", "dark"} else "light"

    @app.before_request
    def load_logged_in_user() -> None:
        user_id = session.get("user_id")
        if user_id is None:
            g.user = None
            return
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    @app.before_request
    def load_pending_achievement_unlocks() -> None:
        g.pending_achievement_unlocks = []
        if getattr(g, "user", None):
            g.pending_achievement_unlocks = get_pending_achievement_unlocks(get_db(), g.user["id"])

    @app.after_request
    def translate_html_response(response: Response) -> Response:
        if response.mimetype == "text/html" and not response.direct_passthrough:
            response.set_data(translate_html(getattr(g, "lang", app.config["DEFAULT_LANGUAGE"]), response.get_data(as_text=True)))
        return response

    @app.context_processor
    def inject_helpers():
        def avatar_initials(name: str) -> str:
            parts = [part for part in name.split() if part]
            return "".join(part[0] for part in parts[:2]).upper() or "?"

        def achievement_badge(title: str) -> dict[str, str]:
            badge_map = {
                "Table Boss": {"icon": ui_icon("badge-crown"), "tone": "gold"},
                "Draw Specialist": {"icon": ui_icon("badge-shield"), "tone": "slate"},
                "Giant Killer": {"icon": ui_icon("badge-sword"), "tone": "ember"},
                "Heat Check": {"icon": ui_icon("badge-bolt"), "tone": "crimson"},
                "Coffee Shark": {"icon": ui_icon("badge-coffee"), "tone": "teal"},
            }
            return badge_map.get(title, {"icon": ui_icon("badge-star"), "tone": "indigo"})

        def placement_tier(position: int) -> str:
            if position == 1:
                return "gold"
            if position == 2:
                return "silver"
            if position == 3:
                return "bronze"
            return "default"

        return {
            "now": datetime.now(timezone.utc),
            "avatar_initials": avatar_initials,
            "avatar_asset": avatar_asset,
            "achievement_reward_asset": achievement_reward_asset,
            "avatar_choices": BASE_AVATAR_CHOICES,
            "ui_icon": ui_icon,
            "language_flag": language_flag,
            "t": tr,
            "current_language": getattr(g, "lang", app.config["DEFAULT_LANGUAGE"]),
            "current_theme": getattr(g, "theme", "light"),
            "language_options": SUPPORTED_LANGUAGES,
            "achievement_badge": achievement_badge,
            "placement_tier": placement_tier,
            "game_type_label": game_type_label,
            "match_side_label": match_side_label,
            "match_role_summary": match_role_summary,
            "match_result_label": match_result_label,
            "time_control_label": time_control_label,
            "time_control_preset_value": time_control_preset_value,
            "role_label": role_label,
            "challenge_status_label": challenge_status_label,
            "treat_breakdown": treat_breakdown,
            "treat_label": treat_label,
            "can_manage_app_data": user_can_manage_app_data(g.user["id"]) if getattr(g, "user", None) else False,
            "pending_achievement_unlocks": getattr(g, "pending_achievement_unlocks", []),
        }

    def pending_invite_target():
        pending = session.get("pending_invite")
        if not pending:
            return None
        return url_for("join_group", slug=pending["slug"], invite_code=pending["invite_code"])

    def get_host_access_urls():
        seen = set()
        candidates = []
        port = request.environ.get("SERVER_PORT") or "5000"

        def add_candidate(ip_address: str, label: str) -> None:
            if not ip_address:
                return
            ip_address = ip_address.strip()
            if not ip_address or ip_address.startswith("127.") or ip_address == "0.0.0.0":
                return
            if ip_address in seen:
                return
            seen.add(ip_address)
            candidates.append(
                {
                    "label": label,
                    "ip_address": ip_address,
                    "url": f"http://{ip_address}:{port}",
                }
            )

        try:
            probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe_socket.connect(("8.8.8.8", 80))
            add_candidate(probe_socket.getsockname()[0], "Primary outbound address")
            probe_socket.close()
        except OSError:
            pass

        try:
            for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
                add_candidate(result[4][0], "Detected host address")
        except OSError:
            pass

        return candidates

    def invite_urls_for_group(group_row):
        urls = []
        seen = set()
        direct_path = url_for("invite_link", slug=group_row["slug"], invite_code=group_row["invite_code"])
        manual_path = url_for("join_group", slug=group_row["slug"], invite_code=group_row["invite_code"])
        for item in get_host_access_urls():
            for label, path in [
                ("Direct invite", direct_path),
                ("Join page", manual_path),
            ]:
                full_url = f"{item['url']}{path}"
                if full_url in seen:
                    continue
                seen.add(full_url)
                urls.append({"network_label": item["label"], "link_label": label, "url": full_url})
        if not urls:
            base_url = request.host_url.rstrip("/")
            urls = [
                {"network_label": "Current browser host", "link_label": "Direct invite", "url": f"{base_url}{direct_path}"},
                {"network_label": "Current browser host", "link_label": "Join page", "url": f"{base_url}{manual_path}"},
            ]
        return urls

    def row_value(row, key: str, default=None):
        if row is None:
            return default
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default

    def game_type_label(game_type: str | None) -> str:
        if game_type == "one_arm_one_brain":
            return tr("One Arm, One Brain")
        return tr("Standard")

    def format_clock_seconds(total_seconds: int | None) -> str:
        if total_seconds is None:
            return ""
        minutes, seconds = divmod(int(total_seconds), 60)
        if seconds:
            return f"{minutes}m {seconds}s"
        return f"{minutes}m"

    def build_time_control_label(base_seconds: int | None, increment_seconds: int | None, custom_label: str | None = None) -> str | None:
        if custom_label:
            return custom_label
        if base_seconds is None:
            return None
        increment_seconds = increment_seconds or 0
        label = format_clock_seconds(base_seconds)
        if increment_seconds:
            label = f"{label} + {increment_seconds}s increment"
        return label

    def time_control_label(match_row) -> str | None:
        return build_time_control_label(
            row_value(match_row, "time_control_base_seconds"),
            row_value(match_row, "time_control_increment_seconds"),
            row_value(match_row, "time_control_label"),
        )

    def time_control_preset_value(match_row) -> str:
        base_seconds = row_value(match_row, "time_control_base_seconds")
        if base_seconds is None:
            return ""
        increment_seconds = row_value(match_row, "time_control_increment_seconds") or 0
        value = f"{base_seconds}|{increment_seconds}"
        return value if any(preset["value"] == value for preset in TIME_CONTROL_PRESETS) else "custom"

    def email_looks_valid(email: str) -> bool:
        return bool(EMAIL_PATTERN.match(email.strip()))

    def placeholder_name_from_email(email: str) -> str:
        local_part = email.split("@", 1)[0]
        pieces = [piece for piece in re.split(r"[._+-]+", local_part) if piece]
        if not pieces:
            return email
        return " ".join(piece[:1].upper() + piece[1:] for piece in pieces[:3])

    def direct_invite_url_for_group(group_row) -> str:
        invite_urls = invite_urls_for_group(group_row)
        for item in invite_urls:
            if item["link_label"] == "Direct invite":
                return item["url"]
        return invite_urls[0]["url"]

    def match_side_player_ids(match_row, side: str) -> list[int]:
        player_ids = [row_value(match_row, f"{side}_user_id")]
        partner_id = row_value(match_row, f"{side}_partner_user_id")
        if partner_id:
            player_ids.append(partner_id)
        return [player_id for player_id in player_ids if player_id]

    def match_side_label(match_row, side: str) -> str:
        lead_name = row_value(match_row, f"{side}_name", "Unknown")
        partner_name = row_value(match_row, f"{side}_partner_name")
        if row_value(match_row, "game_type") == "one_arm_one_brain" and partner_name:
            return f"{lead_name} + {partner_name}"
        return lead_name

    def clarity_label(score: int | None) -> str:
        labels = {
            1: tr("chaotic hint"),
            2: tr("loose idea"),
            3: tr("piece-first call"),
            4: tr("clear square plan"),
            5: tr("laser precise"),
        }
        return labels.get(score, tr("Not rated"))

    def treat_breakdown(amount: int | None) -> dict[str, int]:
        total_coffees = max(0, int(amount or 0))
        cakes, remainder = divmod(total_coffees, TREAT_UNIT_VALUES["cake"])
        pizzas, coffees = divmod(remainder, TREAT_UNIT_VALUES["pizza"])
        return {
            "total_coffees": total_coffees,
            "cakes": cakes,
            "pizzas": pizzas,
            "coffees": coffees,
        }

    def treat_label(amount: int | None) -> str:
        breakdown = treat_breakdown(amount)
        parts = []
        for key in ("cakes", "pizzas", "coffees"):
            value = breakdown[key]
            if not value:
                continue
            noun = key[:-1] if value == 1 else key
            parts.append(f"{value} {noun}")
        return ", ".join(parts) if parts else "0 coffees"

    def match_role_summary(match_row, side: str) -> str | None:
        if row_value(match_row, "game_type") != "one_arm_one_brain":
            return None
        lead_name = row_value(match_row, f"{side}_name", "Unknown")
        partner_name = row_value(match_row, f"{side}_partner_name")
        if not partner_name:
            return None
        clarity = row_value(match_row, f"{side}_instruction_clarity")
        summary = f"{tr('Mente')}: {lead_name} · {tr('Braccio')}: {partner_name}"
        if clarity:
            summary += f" · {tr('clarity')} {clarity}/5 ({clarity_label(clarity)})"
        return summary

    def role_label(role: str) -> str:
        labels = {"owner": tr("Owner"), "admin": tr("Admin"), "member": tr("Member")}
        return labels.get(role, role.title())

    def challenge_status_label(status: str) -> str:
        labels = {
            "open": tr("Open"),
            "accepted": tr("Accepted"),
            "declined": tr("Declined"),
            "completed": tr("Completed"),
        }
        return labels.get(status, status.title())

    def match_result_label(match_row) -> str:
        return result_label(
            row_value(match_row, "result", "draw"),
            match_side_label(match_row, "white"),
            match_side_label(match_row, "black"),
        )

    def login_required(view):
        @wraps(view)
        def wrapped_view(**kwargs):
            if g.user is None:
                flash(tr("Please sign in to continue."), "warning")
                return redirect(url_for("login"))
            return view(**kwargs)

        return wrapped_view

    def group_membership_or_404(slug: str):
        db = get_db()
        group = db.execute(
            """
            SELECT g.*, m.role AS membership_role
            FROM groups_workspace g
            JOIN memberships m ON m.group_id = g.id
            WHERE g.slug = ? AND m.user_id = ? AND m.is_active = 1
            """,
            (slug, g.user["id"]),
        ).fetchone()
        if group is None:
            raise PermissionError
        return group

    def group_admin_required(group) -> None:
        if group["membership_role"] not in {"owner", "admin"}:
            raise PermissionError

    def system_data_admin_required() -> None:
        row = get_db().execute(
            """
            SELECT 1
            FROM memberships
            WHERE user_id = ? AND role IN ('owner', 'admin') AND is_active = 1
            LIMIT 1
            """,
            (g.user["id"],),
        ).fetchone()
        if row is None:
            raise PermissionError

    def user_can_manage_app_data(user_id: int) -> bool:
        row = get_db().execute(
            """
            SELECT 1
            FROM memberships
            WHERE user_id = ? AND role IN ('owner', 'admin') AND is_active = 1
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return row is not None

    def get_signup_notifications(db, user_id: int):
        if not user_can_manage_app_data(user_id):
            return []
        return db.execute(
            """
            SELECT sn.id,
                   sn.created_at,
                   u.name AS new_user_name,
                   u.email AS new_user_email
            FROM signup_notifications sn
            JOIN users u ON u.id = sn.new_user_id
            LEFT JOIN signup_notification_reads r
                   ON r.notification_id = sn.id AND r.user_id = ?
            WHERE r.id IS NULL
            ORDER BY sn.created_at DESC
            LIMIT 12
            """,
            (user_id,),
        ).fetchall()

    def owner_group_or_none(group_id: int):
        return get_db().execute(
            """
            SELECT g.*
            FROM groups_workspace g
            JOIN memberships m ON m.group_id = g.id
            WHERE g.id = ? AND m.user_id = ? AND m.role = 'owner' AND m.is_active = 1
            """,
            (group_id, g.user["id"]),
        ).fetchone()

    def user_has_history(db, user_id: int) -> bool:
        checks = [
            ("SELECT 1 FROM matches WHERE white_user_id = ? OR black_user_id = ? OR white_partner_user_id = ? OR black_partner_user_id = ? OR reported_by = ? OR confirmed_by = ? LIMIT 1", (user_id, user_id, user_id, user_id, user_id, user_id)),
            ("SELECT 1 FROM rating_history WHERE user_id = ? LIMIT 1", (user_id,)),
            ("SELECT 1 FROM challenges WHERE challenger_user_id = ? OR challenged_user_id = ? LIMIT 1", (user_id, user_id)),
            ("SELECT 1 FROM coffee_ledger WHERE debtor_user_id = ? OR creditor_user_id = ? OR created_by = ? LIMIT 1", (user_id, user_id, user_id)),
            ("SELECT 1 FROM tournaments WHERE created_by = ? OR winner_user_id = ? LIMIT 1", (user_id, user_id)),
            ("SELECT 1 FROM tournament_entries WHERE user_id = ? LIMIT 1", (user_id,)),
            ("SELECT 1 FROM tournament_games WHERE white_user_id = ? OR black_user_id = ? LIMIT 1", (user_id, user_id)),
            ("SELECT 1 FROM groups_workspace WHERE created_by = ? LIMIT 1", (user_id,)),
        ]
        return any(db.execute(sql, params).fetchone() is not None for sql, params in checks)

    def can_delete_user_fully(db, user_id: int) -> bool:
        other_memberships = db.execute(
            "SELECT COUNT(*) AS total FROM memberships WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()["total"]
        return other_memberships == 0 and not user_has_history(db, user_id)

    def delete_user_record(db, user_id: int) -> None:
        notification_ids = [
            row["id"]
            for row in db.execute("SELECT id FROM signup_notifications WHERE new_user_id = ?", (user_id,)).fetchall()
        ]
        if notification_ids:
            db.executemany(
                "DELETE FROM signup_notification_reads WHERE notification_id = ?",
                [(notification_id,) for notification_id in notification_ids],
            )
        db.execute("DELETE FROM signup_notification_reads WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM signup_notifications WHERE new_user_id = ?", (user_id,))
        db.execute("DELETE FROM user_achievements WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM memberships WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def send_app_email(recipient: str, subject: str, body: str, reply_to: str | None = None) -> str:
        if not recipient:
            return "skipped"
        smtp_host = app.config.get("SMTP_HOST", "").strip()
        sender = app.config.get("MAIL_SENDER", "").strip()
        if smtp_host and sender:
            message = EmailMessage()
            message["From"] = sender
            message["To"] = recipient
            message["Subject"] = subject
            if reply_to:
                message["Reply-To"] = reply_to
            message.set_content(body)
            try:
                with smtplib.SMTP(smtp_host, app.config.get("SMTP_PORT", 587), timeout=10) as smtp:
                    if app.config.get("SMTP_USE_TLS", True):
                        smtp.starttls()
                    username = app.config.get("SMTP_USERNAME", "").strip()
                    password = app.config.get("SMTP_PASSWORD", "")
                    if username:
                        smtp.login(username, password)
                    smtp.send_message(message)
                return "sent"
            except OSError:
                pass
        outbox_path = Path(app.config["MAIL_OUTBOX"])
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with outbox_path.open("a", encoding="utf-8") as outbox:
            outbox.write(
                json.dumps(
                    {
                        "to": recipient,
                        "subject": subject,
                        "body": body,
                        "reply_to": reply_to,
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                )
            )
            outbox.write("\n")
        return "logged"

    def send_group_removal_email(
        member_name: str,
        member_email: str,
        group_name: str,
        removed_by_name: str,
        removed_by_email: str,
    ) -> str:
        subject = tr("Removed from {group_name}", group_name=group_name)
        body = tr(
            "Hello {member_name},\n\nYou have been removed from the group \"{group_name}\" in Lunchbreak ELO by {removed_by_name} ({removed_by_email}).\nIf you think this was a mistake, reply to this email or contact that person directly.\n",
            member_name=member_name,
            group_name=group_name,
            removed_by_name=removed_by_name,
            removed_by_email=removed_by_email,
        )
        return send_app_email(member_email, subject, body, reply_to=removed_by_email or None)

    def send_account_deletion_email(
        member_name: str,
        member_email: str,
        removed_by_name: str,
        removed_by_email: str,
    ) -> str:
        subject = tr("Account deleted")
        body = tr(
            "Hello {member_name},\n\nYour Lunchbreak ELO account has been deleted by {removed_by_name} ({removed_by_email}).\nIf you think this was a mistake, reply to this email or contact that person directly.\n",
            member_name=member_name,
            removed_by_name=removed_by_name,
            removed_by_email=removed_by_email,
        )
        return send_app_email(member_email, subject, body, reply_to=removed_by_email or None)

    def send_placeholder_invite_email(
        member_email: str,
        member_name: str,
        group_name: str,
        invite_url: str,
        invited_by_name: str,
        invited_by_email: str,
    ) -> str:
        subject = tr("You have been invited to {group_name}", group_name=group_name)
        body = tr(
            "Hello {member_name},\n\n{invited_by_name} ({invited_by_email}) logged a match for you in \"{group_name}\" using this email address.\nA temporary placeholder account has been created for you. Register with this same email to claim your games, ratings, and coffees.\n\nUse this invite link to open the app:\n{invite_url}\n",
            member_name=member_name,
            invited_by_name=invited_by_name,
            invited_by_email=invited_by_email,
            group_name=group_name,
            invite_url=invite_url,
        )
        return send_app_email(member_email, subject, body, reply_to=invited_by_email or None)

    def default_challenge_message(challenger_name: str, opponent_name: str) -> str:
        return tr(
            "{challenger_name} has thrown down a chess gauntlet for {opponent_name}. The pieces are warming up, the kings are nervous, and at least one pawn has already packed a tiny suitcase.",
            challenger_name=challenger_name,
            opponent_name=opponent_name,
        )

    def send_challenge_email(group, challenger, opponent, message: str) -> str:
        subject = tr("Chess challenge from {challenger_name}", challenger_name=challenger["name"])
        challenge_url = url_for("group_challenges", slug=group["slug"], _external=True)
        body = tr(
            "Hello {opponent_name},\n\n{challenger_name} challenged you in \"{group_name}\".\n\nMessage:\n{message}\n\nOpen the challenge board here:\n{challenge_url}\n\nMay your blunders be instructive, your forks be legal, and your king avoid unnecessary cardio.\n",
            opponent_name=opponent["name"],
            challenger_name=challenger["name"],
            group_name=group["name"],
            message=message,
            challenge_url=challenge_url,
        )
        return send_app_email(opponent["email"], subject, body, reply_to=challenger["email"] or None)

    def challenge_age_label(created_at: str | None) -> str:
        if not created_at:
            return tr("Launched today")
        try:
            launched_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            try:
                launched_at = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return tr("Launched recently")
        days = max(0, (datetime.now() - launched_at.replace(tzinfo=None)).days)
        if days == 0:
            return tr("Launched today")
        if days == 1:
            return tr("Launched 1 day ago")
        return tr("Launched {days} days ago", days=days)

    def get_unlocked_achievements_for_user(db, user_id: int, group_id: int | None = None):
        group_filter = "AND ua.group_id = ?" if group_id is not None else ""
        params = (user_id, group_id) if group_id is not None else (user_id,)
        rows = db.execute(
            f"""
            SELECT ua.*, g.name AS group_name
            FROM user_achievements ua
            LEFT JOIN groups_workspace g ON g.id = ua.group_id
            WHERE ua.user_id = ?
              {group_filter}
            ORDER BY ua.unlocked_at DESC, ua.id DESC
            """,
            params,
        ).fetchall()
        results = []
        for row in rows:
            definition = achievement_definitions_by_key.get(row["achievement_key"])
            if definition is None:
                continue
            item = dict(row)
            item["name"] = definition["name"]
            item["description"] = definition["description"]
            item["reward_title"] = definition["reward_title"]
            item["reward_avatar_icon"] = definition["reward_avatar_icon"]
            results.append(item)
        return results

    def get_unlocked_reward_choices(db, user_id: int, group_id: int | None = None) -> tuple[list[str], list[dict[str, str]]]:
        unlocked = get_unlocked_achievements_for_user(db, user_id, group_id)
        titles = []
        seen_titles = set()
        icons: list[dict[str, str]] = []
        seen_icons = set()
        for item in unlocked:
            reward_title = item.get("reward_title")
            reward_icon = item.get("reward_avatar_icon")
            if reward_title and reward_title not in seen_titles:
                seen_titles.add(reward_title)
                titles.append(reward_title)
            achievement_key = item["achievement_key"]
            achievement_code = achievement_avatar_code(achievement_key)
            if achievement_code not in seen_icons:
                seen_icons.add(achievement_code)
                icons.append(
                    {
                        "code": achievement_code,
                        "label": item["name"],
                        "asset": achievement_reward_asset(achievement_key, reward_icon),
                    }
                )
            if reward_icon and reward_icon not in seen_icons:
                seen_icons.add(reward_icon)
                label = next((label for code, label in ACHIEVEMENT_AVATAR_CHOICES if code == reward_icon), reward_icon.title())
                icons.append({"code": reward_icon, "label": label, "asset": avatar_asset(reward_icon)})
        return titles, icons

    def get_pending_achievement_unlocks(db, user_id: int):
        rows = db.execute(
            """
            SELECT ua.*, g.name AS group_name
            FROM user_achievements ua
            LEFT JOIN groups_workspace g ON g.id = ua.group_id
            WHERE ua.user_id = ? AND ua.is_seen = 0
            ORDER BY ua.unlocked_at ASC, ua.id ASC
            """,
            (user_id,),
        ).fetchall()
        pending = []
        for row in rows:
            definition = achievement_definitions_by_key.get(row["achievement_key"])
            if definition is None:
                continue
            pending.append(
                {
                    "id": row["id"],
                    "achievement_key": row["achievement_key"],
                    "name": definition["name"],
                    "description": definition["description"],
                    "reward_title": definition["reward_title"],
                    "reward_avatar_icon": definition["reward_avatar_icon"],
                    "group_name": row["group_name"],
                }
            )
        return pending

    def get_group_achievement_icons_by_user(db, group_id: int, allowed_keys: set[str] | None = None) -> dict[int, list[dict[str, str]]]:
        rows = db.execute(
            """
            SELECT user_id, achievement_key
            FROM user_achievements
            WHERE group_id = ?
            ORDER BY unlocked_at ASC, id ASC
            """,
            (group_id,),
        ).fetchall()
        icons_by_user: dict[int, list[dict[str, str]]] = defaultdict(list)
        seen_pairs: set[tuple[int, str]] = set()
        for row in rows:
            achievement_key = row["achievement_key"]
            if allowed_keys is not None and achievement_key not in allowed_keys:
                continue
            if (row["user_id"], achievement_key) in seen_pairs:
                continue
            seen_pairs.add((row["user_id"], achievement_key))
            definition = achievement_definitions_by_key.get(achievement_key)
            if definition is None:
                continue
            icons_by_user[row["user_id"]].append(
                {
                    "key": achievement_key,
                    "name": definition["name"],
                    "asset": achievement_reward_asset(achievement_key, definition["reward_avatar_icon"]),
                }
            )
        return icons_by_user

    def normalize_uploaded_avatar(file_storage) -> tuple[str, str] | tuple[None, None]:
        if file_storage is None or not getattr(file_storage, "filename", ""):
            return None, None
        image = Image.open(file_storage.stream)
        image = ImageOps.exif_transpose(image)
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        else:
            image = image.convert("RGB")
        image = ImageOps.fit(image, (160, 160), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=72, optimize=True)
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        return payload, "image/jpeg"

    def get_group_members(db, group_id: int):
        return db.execute(
            """
            SELECT u.id, u.name, u.email, u.avatar_color, u.avatar_icon, u.selected_title,
                   u.is_placeholder, u.is_former_employee, m.role, m.created_at
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            WHERE m.group_id = ? AND m.is_active = 1
            ORDER BY lower(u.name)
            """,
            (group_id,),
        ).fetchall()

    def get_match_eligible_group_members(db, group_id: int):
        return [member for member in get_group_members(db, group_id) if not member["is_former_employee"]]

    def get_group_member_profile(db, group_id: int, user_id: int):
        return db.execute(
            """
            SELECT u.id, u.name, u.bio, u.favorite_opening, u.avatar_color, u.avatar_icon,
                   u.selected_title, u.tagline, u.is_placeholder, u.is_former_employee, m.role, m.created_at AS joined_at
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            WHERE m.group_id = ? AND m.user_id = ? AND m.is_active = 1
            """,
            (group_id, user_id),
        ).fetchone()

    def empty_achievement_stats(user_id: int, name: str = "") -> dict:
        return {
            "user_id": user_id,
            "name": name,
            "total_games": 0,
            "standard_games": 0,
            "standard_wins": 0,
            "standard_losses": 0,
            "standard_draws": 0,
            "white_standard_wins": 0,
            "black_standard_wins": 0,
            "team_games": 0,
            "team_wins": 0,
            "best_upset_gap": 0,
            "worst_upset_loss_gap": 0,
            "max_standard_win_streak": 0,
            "max_standard_loss_streak": 0,
            "survived_standard_loss_streak": 0,
            "comeback_after_loss_streak": 0,
            "max_standard_losses_vs_same_opponent": 0,
            "unique_openings": set(),
            "coffee_credited": 0,
            "coffee_owed": 0,
            "is_top_creditor": 0,
            "coffee_chain_links": 0,
            "peak_standard_rating": 0,
            "season_podiums": 0,
            "seasons_won": 0,
            "challenges_sent": 0,
            "suggestion_challenges_sent": 0,
            "challenge_matches_completed": 0,
            "challenge_match_wins": 0,
            "_current_standard_streak": 0,
            "_current_standard_loss_streak": 0,
            "_awaiting_loss_streak_survival": 0,
            "_losses_by_opponent": defaultdict(int),
        }

    def build_user_achievement_stats(db, group_id: int) -> dict[int, dict]:
        member_rows = db.execute(
            """
            SELECT DISTINCT u.id, u.name
            FROM users u
            LEFT JOIN memberships m ON m.user_id = u.id AND m.group_id = ? AND m.is_active = 1
            LEFT JOIN matches mt ON mt.group_id = ? AND mt.deleted_at IS NULL AND mt.confirmation_status = 'confirmed'
                 AND (mt.white_user_id = u.id OR mt.black_user_id = u.id OR mt.white_partner_user_id = u.id OR mt.black_partner_user_id = u.id)
            WHERE m.id IS NOT NULL OR mt.id IS NOT NULL
            """,
            (group_id, group_id),
        ).fetchall()
        stats_by_user = {row["id"]: empty_achievement_stats(row["id"], row["name"]) for row in member_rows}
        group_row = db.execute("SELECT starting_rating FROM groups_workspace WHERE id = ?", (group_id,)).fetchone()
        starting_rating = group_row["starting_rating"] if group_row else 1200

        rating_rows = db.execute(
            """
            SELECT rh.match_id, rh.user_id, rh.ladder_type, rh.rating_before, rh.rating_after
            FROM rating_history rh
            JOIN matches m ON m.id = rh.match_id
            WHERE rh.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
            ORDER BY m.played_at, m.id, rh.id
            """,
            (group_id,),
        ).fetchall()
        rating_before = {
            (row["match_id"], row["user_id"], row["ladder_type"]): row["rating_before"]
            for row in rating_rows
        }
        for row in rating_rows:
            if row["ladder_type"] == "standard":
                stats = stats_by_user.setdefault(row["user_id"], empty_achievement_stats(row["user_id"]))
                stats["peak_standard_rating"] = max(stats["peak_standard_rating"], row["rating_after"])

        match_rows = db.execute(
            """
            SELECT *
            FROM matches
            WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
            ORDER BY played_at, id
            """,
            (group_id,),
        ).fetchall()
        for match in match_rows:
            if match["game_type"] == "standard":
                participants = [match["white_user_id"], match["black_user_id"]]
                for user_id in participants:
                    stats_by_user.setdefault(user_id, empty_achievement_stats(user_id))
                    if stats_by_user[user_id]["_awaiting_loss_streak_survival"]:
                        stats_by_user[user_id]["survived_standard_loss_streak"] = 1
                        stats_by_user[user_id]["_awaiting_loss_streak_survival"] = 0
                    stats_by_user[user_id]["total_games"] += 1
                    stats_by_user[user_id]["standard_games"] += 1
                if match["opening_name"] or match["opening_code"]:
                    opening_key = (match["opening_name"] or match["opening_code"] or "").strip().lower()
                    if opening_key:
                        for user_id in participants:
                            stats_by_user[user_id]["unique_openings"].add(opening_key)
                if match["result"] == "draw":
                    for user_id in participants:
                        stats_by_user[user_id]["standard_draws"] += 1
                        stats_by_user[user_id]["_current_standard_streak"] = 0
                        stats_by_user[user_id]["_current_standard_loss_streak"] = 0
                else:
                    winner_id = match["white_user_id"] if match["result"] == "white" else match["black_user_id"]
                    loser_id = match["black_user_id"] if match["result"] == "white" else match["white_user_id"]
                    winner_stats = stats_by_user[winner_id]
                    loser_stats = stats_by_user[loser_id]
                    if winner_stats["_current_standard_loss_streak"] >= 2:
                        winner_stats["comeback_after_loss_streak"] = 1
                    winner_stats["standard_wins"] += 1
                    winner_stats["_current_standard_streak"] += 1
                    winner_stats["_current_standard_loss_streak"] = 0
                    winner_stats["max_standard_win_streak"] = max(
                        winner_stats["max_standard_win_streak"], winner_stats["_current_standard_streak"]
                    )
                    if winner_id == match["white_user_id"]:
                        winner_stats["white_standard_wins"] += 1
                    else:
                        winner_stats["black_standard_wins"] += 1
                    loser_stats["standard_losses"] += 1
                    loser_stats["_current_standard_streak"] = 0
                    loser_stats["_current_standard_loss_streak"] += 1
                    loser_stats["max_standard_loss_streak"] = max(
                        loser_stats["max_standard_loss_streak"], loser_stats["_current_standard_loss_streak"]
                    )
                    if loser_stats["_current_standard_loss_streak"] >= 3:
                        loser_stats["_awaiting_loss_streak_survival"] = 1
                    opponent_losses = loser_stats["_losses_by_opponent"]
                    opponent_losses[winner_id] += 1
                    loser_stats["max_standard_losses_vs_same_opponent"] = max(
                        loser_stats["max_standard_losses_vs_same_opponent"], opponent_losses[winner_id]
                    )
                    winner_before = rating_before.get((match["id"], winner_id, "standard"), starting_rating)
                    loser_before = rating_before.get((match["id"], loser_id, "standard"), starting_rating)
                    winner_stats["best_upset_gap"] = max(winner_stats["best_upset_gap"], max(0, loser_before - winner_before))
                    loser_stats["worst_upset_loss_gap"] = max(
                        loser_stats["worst_upset_loss_gap"], max(0, loser_before - winner_before)
                    )
            else:
                white_ids = match_side_player_ids(match, "white")
                black_ids = match_side_player_ids(match, "black")
                for user_id in white_ids + black_ids:
                    stats_by_user.setdefault(user_id, empty_achievement_stats(user_id))
                    stats_by_user[user_id]["total_games"] += 1
                    stats_by_user[user_id]["team_games"] += 1
                if match["result"] == "white":
                    for user_id in white_ids:
                        stats_by_user[user_id]["team_wins"] += 1
                elif match["result"] == "black":
                    for user_id in black_ids:
                        stats_by_user[user_id]["team_wins"] += 1

        coffee_rows = db.execute(
            """
            SELECT creditor_user_id AS user_id, SUM(amount) AS total
            FROM coffee_ledger
            WHERE group_id = ?
            GROUP BY creditor_user_id
            """,
            (group_id,),
        ).fetchall()
        for row in coffee_rows:
            stats_by_user.setdefault(row["user_id"], empty_achievement_stats(row["user_id"]))
            stats_by_user[row["user_id"]]["coffee_credited"] = row["total"] or 0

        coffee_debt_rows = db.execute(
            """
            SELECT debtor_user_id AS user_id, SUM(amount) AS total
            FROM coffee_ledger
            WHERE group_id = ?
            GROUP BY debtor_user_id
            """,
            (group_id,),
        ).fetchall()
        for row in coffee_debt_rows:
            stats_by_user.setdefault(row["user_id"], empty_achievement_stats(row["user_id"]))
            stats_by_user[row["user_id"]]["coffee_owed"] = row["total"] or 0

        open_balance_rows = db.execute(
            """
            SELECT u.id AS user_id,
                   SUM(CASE WHEN c.creditor_user_id = u.id AND c.is_settled = 0 THEN c.amount ELSE 0 END) AS open_credit,
                   SUM(CASE WHEN c.debtor_user_id = u.id AND c.is_settled = 0 THEN c.amount ELSE 0 END) AS open_debt
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            LEFT JOIN coffee_ledger c ON c.group_id = m.group_id
            WHERE m.group_id = ? AND m.is_active = 1
            GROUP BY u.id
            """,
            (group_id,),
        ).fetchall()
        top_positive_balance = max(
            (
                (row["open_credit"] or 0) - (row["open_debt"] or 0)
                for row in open_balance_rows
            ),
            default=0,
        )
        for row in open_balance_rows:
            stats_by_user.setdefault(row["user_id"], empty_achievement_stats(row["user_id"]))
            open_credit = int(row["open_credit"] or 0)
            open_debt = int(row["open_debt"] or 0)
            net_open = open_credit - open_debt
            stats_by_user[row["user_id"]]["coffee_chain_links"] = int(open_credit > 0 and open_debt > 0)
            stats_by_user[row["user_id"]]["is_top_creditor"] = int(top_positive_balance > 0 and net_open == top_positive_balance)

        challenge_rows = db.execute(
            """
            SELECT *
            FROM challenges
            WHERE group_id = ?
            """,
            (group_id,),
        ).fetchall()
        for challenge in challenge_rows:
            stats_by_user.setdefault(challenge["challenger_user_id"], empty_achievement_stats(challenge["challenger_user_id"]))
            stats_by_user[challenge["challenger_user_id"]]["challenges_sent"] += 1
            if row_value(challenge, "source") == "suggestion":
                stats_by_user[challenge["challenger_user_id"]]["suggestion_challenges_sent"] += 1
            if challenge["status"] != "completed" or not challenge["match_id"]:
                continue
            match = db.execute(
                "SELECT * FROM matches WHERE id = ? AND group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'",
                (challenge["match_id"], group_id),
            ).fetchone()
            if match is None:
                continue
            participants = [challenge["challenger_user_id"], challenge["challenged_user_id"]]
            for user_id in participants:
                stats_by_user.setdefault(user_id, empty_achievement_stats(user_id))
                stats_by_user[user_id]["challenge_matches_completed"] += 1
            if match["result"] == "draw":
                continue
            winner_id = match["white_user_id"] if match["result"] == "white" else match["black_user_id"]
            if winner_id in participants:
                stats_by_user[winner_id]["challenge_match_wins"] += 1

        closed_seasons = db.execute(
            "SELECT id FROM seasons WHERE group_id = ? AND is_active = 0 ORDER BY start_date, id",
            (group_id,),
        ).fetchall()
        for season in closed_seasons:
            final_rows = db.execute(
                """
                WITH final_rankings AS (
                    SELECT rh.user_id, rh.rating_after,
                           ROW_NUMBER() OVER (
                               PARTITION BY rh.user_id
                               ORDER BY m.played_at DESC, m.id DESC, rh.id DESC
                           ) AS rn
                    FROM rating_history rh
                    JOIN matches m ON m.id = rh.match_id
                    WHERE rh.group_id = ? AND rh.season_id = ? AND rh.ladder_type = 'standard'
                      AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
                )
                SELECT user_id, rating_after
                FROM final_rankings
                WHERE rn = 1
                ORDER BY rating_after DESC, user_id ASC
                """,
                (group_id, season["id"]),
            ).fetchall()
            if not final_rows:
                continue
            for position, row in enumerate(final_rows[:3], start=1):
                stats_by_user.setdefault(row["user_id"], empty_achievement_stats(row["user_id"]))
                stats_by_user[row["user_id"]]["season_podiums"] += 1
                if position == 1:
                    stats_by_user[row["user_id"]]["seasons_won"] += 1

        for stats in stats_by_user.values():
            stats["peak_standard_rating"] = stats["peak_standard_rating"] or starting_rating
            stats["unique_openings"] = len(stats["unique_openings"])
            stats["two_color_mastery"] = int(
                stats["white_standard_wins"] >= 3 and stats["black_standard_wins"] >= 3
            )
            stats.pop("_current_standard_streak", None)
            stats.pop("_current_standard_loss_streak", None)
            stats.pop("_awaiting_loss_streak_survival", None)
            stats.pop("_losses_by_opponent", None)
        return stats_by_user

    def evaluate_group_achievements(
        db,
        group_id: int,
        *,
        source_match_id: int | None = None,
        mark_seen: bool = False,
    ) -> list[int]:
        stats_by_user = build_user_achievement_stats(db, group_id)
        unlocked_ids: list[int] = []
        for user_id, stats in stats_by_user.items():
            for definition in ACHIEVEMENT_DEFINITIONS:
                if stats.get(definition["metric"], 0) < definition["threshold"]:
                    continue
                try:
                    achievement_id = db.insert_and_get_id(
                        """
                        INSERT INTO user_achievements (user_id, group_id, achievement_key, source_match_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, group_id, definition["key"], source_match_id),
                    )
                    if mark_seen:
                        db.execute("UPDATE user_achievements SET is_seen = 1 WHERE id = ?", (achievement_id,))
                    unlocked_ids.append(achievement_id)
                except Exception:
                    continue
        return unlocked_ids

    def backfill_achievements_once(db) -> None:
        already_ran = db.execute("SELECT value FROM app_meta WHERE key = ?", (ACHIEVEMENT_BACKFILL_KEY,)).fetchone()
        if already_ran:
            return
        group_rows = db.execute("SELECT id FROM groups_workspace ORDER BY id").fetchall()
        for row in group_rows:
            evaluate_group_achievements(db, row["id"], mark_seen=True)
        db.execute(
            """
            INSERT INTO app_meta (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (ACHIEVEMENT_BACKFILL_KEY, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()

    def parse_pgn_bundle(raw_pgn: str):
        chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n(?=\[Event )", raw_pgn.strip()) if chunk.strip()]
        games = []
        for chunk in chunks:
            tags = dict(re.findall(r'\[(\w+)\s+"([^"]*)"\]', chunk))
            moves = re.sub(r'(\[(\w+)\s+"([^"]*)"\]\s*)+', "", chunk, flags=re.MULTILINE).strip()
            games.append({"tags": tags, "moves": moves})
        return games

    def build_pgn(match_row):
        tags = [
            ("Event", "Lunchbreak ELO"),
            ("Date", match_row["played_at"].replace("-", ".")),
            ("White", match_side_label(match_row, "white")),
            ("Black", match_side_label(match_row, "black")),
            ("Result", "1-0" if match_row["result"] == "white" else "0-1" if match_row["result"] == "black" else "1/2-1/2"),
        ]
        if match_row["opening_name"]:
            tags.append(("Opening", match_row["opening_name"]))
        if match_row["opening_code"]:
            tags.append(("ECO", match_row["opening_code"]))
        if row_value(match_row, "time_control_base_seconds"):
            tags.append(("TimeControl", f"{match_row['time_control_base_seconds']}+{match_row['time_control_increment_seconds'] or 0}"))
        tag_text = "\n".join(f'[{key} "{value}"]' for key, value in tags)
        return f"{tag_text}\n\n{match_row['pgn_text'] or ''}".strip()

    def parse_pgn_time_control(value: str | None) -> tuple[int | None, int | None]:
        if not value or value in {"-", "?"}:
            return None, None
        first_control = value.split(":", 1)[0]
        match = re.match(r"^(\d+)(?:\+(\d+))?$", first_control)
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2) or 0)

    def swiss_pairings(db, tournament_id: int):
        standings = db.execute(
            """
            SELECT u.id AS user_id, u.name,
                   SUM(points) AS total_points
            FROM tournament_entries te
            JOIN users u ON u.id = te.user_id
            LEFT JOIN (
                SELECT tournament_id, white_user_id AS user_id,
                       CASE result WHEN 'white' THEN 1 WHEN 'draw' THEN 0.5 ELSE 0 END AS points
                FROM tournament_games
                UNION ALL
                SELECT tournament_id, black_user_id AS user_id,
                       CASE result WHEN 'black' THEN 1 WHEN 'draw' THEN 0.5 ELSE 0 END AS points
                FROM tournament_games
            ) score ON score.tournament_id = te.tournament_id AND score.user_id = te.user_id
            WHERE te.tournament_id = ?
            GROUP BY u.id, u.name
            ORDER BY total_points DESC, lower(u.name)
            """,
            (tournament_id,),
        ).fetchall()
        existing_pairs = {
            tuple(sorted((row["white_user_id"], row["black_user_id"])))
            for row in db.execute(
                "SELECT white_user_id, black_user_id FROM tournament_games WHERE tournament_id = ?",
                (tournament_id,),
            ).fetchall()
        }
        pairings = []
        remaining = [row["user_id"] for row in standings]
        round_number = db.execute(
            "SELECT COUNT(DISTINCT round_name) AS total FROM tournament_games WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()["total"] + 1
        while len(remaining) >= 2:
            left = remaining.pop(0)
            opponent_index = 0
            while opponent_index < len(remaining):
                candidate = remaining[opponent_index]
                if tuple(sorted((left, candidate))) not in existing_pairs:
                    break
                opponent_index += 1
            if opponent_index >= len(remaining):
                opponent_index = 0
            right = remaining.pop(opponent_index)
            pairings.append((left, right, f"Round {round_number}"))
        return pairings

    def get_group_leaderboard(db, group_id: int):
        return get_ladder_leaderboard(db, group_id, ladder_type="standard")

    def get_ladder_leaderboard(db, group_id: int, ladder_type: str = "standard"):
        game_type = "standard" if ladder_type == "standard" else "one_arm_one_brain"
        return db.execute(
            """
            WITH latest AS (
                SELECT rh.user_id, rh.rating_after, rh.delta, rh.match_id,
                       ROW_NUMBER() OVER (PARTITION BY rh.user_id ORDER BY m.played_at DESC, rh.id DESC) AS rn
                FROM rating_history rh
                JOIN matches m ON m.id = rh.match_id
                WHERE rh.group_id = ? AND rh.ladder_type = ?
                  AND m.deleted_at IS NULL
                  AND m.confirmation_status = 'confirmed'
                  AND m.game_type = ?
            ),
            stats AS (
                SELECT user_id,
                       COUNT(*) AS games,
                       SUM(draw_value) AS draws,
                       SUM(win_value) AS wins
                FROM (
                    SELECT m.id, m.white_user_id AS user_id, 'white' AS side,
                           CASE WHEN m.result = 'draw' THEN 1 ELSE 0 END AS draw_value,
                           CASE WHEN m.result = 'white' THEN 1 ELSE 0 END AS win_value
                    FROM matches m
                    WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = ?
                    UNION ALL
                    SELECT m.id, m.black_user_id AS user_id, 'black' AS side,
                           CASE WHEN m.result = 'draw' THEN 1 ELSE 0 END AS draw_value,
                           CASE WHEN m.result = 'black' THEN 1 ELSE 0 END AS win_value
                    FROM matches m
                    WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = ?
                    UNION ALL
                    SELECT m.id, m.white_partner_user_id AS user_id, 'white' AS side,
                           CASE WHEN m.result = 'draw' THEN 1 ELSE 0 END AS draw_value,
                           CASE WHEN m.result = 'white' THEN 1 ELSE 0 END AS win_value
                    FROM matches m
                    WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = ? AND m.white_partner_user_id IS NOT NULL
                    UNION ALL
                    SELECT m.id, m.black_partner_user_id AS user_id, 'black' AS side,
                           CASE WHEN m.result = 'draw' THEN 1 ELSE 0 END AS draw_value,
                           CASE WHEN m.result = 'black' THEN 1 ELSE 0 END AS win_value
                    FROM matches m
                    WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = ? AND m.black_partner_user_id IS NOT NULL
                )
                GROUP BY user_id
             )
            SELECT u.id, u.name, u.email, u.avatar_color, u.avatar_icon, u.tagline, COALESCE(latest.rating_after, g.starting_rating) AS rating,
                    COALESCE(latest.delta, 0) AS last_delta, COALESCE(stats.games, 0) AS games,
                    COALESCE(stats.wins, 0) AS wins, COALESCE(stats.draws, 0) AS draws,
                    COALESCE(stats.games, 0) - COALESCE(stats.wins, 0) - COALESCE(stats.draws, 0) AS losses
             FROM memberships m
            JOIN users u ON u.id = m.user_id
            JOIN groups_workspace g ON g.id = m.group_id
            LEFT JOIN latest ON latest.user_id = u.id AND latest.rn = 1
            LEFT JOIN stats ON stats.user_id = u.id
            WHERE m.group_id = ? AND m.is_active = 1
            ORDER BY rating DESC, wins DESC, lower(u.name)
            """,
            (group_id, ladder_type, game_type, group_id, game_type, group_id, game_type, group_id, game_type, group_id, game_type, group_id),
        ).fetchall()

    def get_recent_matches(db, group_id: int, limit: int = 12):
        return db.execute(
            """
            SELECT m.*, w.name AS white_name, wp.name AS white_partner_name,
                   b.name AS black_name, bp.name AS black_partner_name,
                   r.name AS reporter_name, s.name AS season_name
            FROM matches m
            JOIN users w ON w.id = m.white_user_id
            LEFT JOIN users wp ON wp.id = m.white_partner_user_id
            JOIN users b ON b.id = m.black_user_id
            LEFT JOIN users bp ON bp.id = m.black_partner_user_id
            JOIN users r ON r.id = m.reported_by
            LEFT JOIN seasons s ON s.id = m.season_id
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
            ORDER BY m.played_at DESC, m.id DESC
            LIMIT ?
            """,
            (group_id, limit),
        ).fetchall()

    def get_active_season(db, group_id: int):
        return db.execute(
            """
            SELECT * FROM seasons
            WHERE group_id = ? AND is_active = 1
            ORDER BY start_date DESC, id DESC
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()

    def match_confirmation_fields(group) -> tuple[str, int | None]:
        is_admin_report = group["membership_role"] in {"owner", "admin"}
        return ("confirmed", g.user["id"]) if is_admin_report else ("pending", None)

    def parse_match_form(form) -> dict:
        time_control_preset = form.get("time_control_preset", "").strip()
        time_control_base_seconds = None
        time_control_increment_seconds = None
        time_control_custom_label = None
        if time_control_preset == "custom":
            custom_minutes = int(form.get("time_control_custom_minutes") or 0)
            custom_seconds = int(form.get("time_control_custom_seconds") or 0)
            time_control_base_seconds = (custom_minutes * 60) + custom_seconds
            time_control_increment_seconds = int(form.get("time_control_custom_increment") or 0)
            time_control_custom_label = form.get("time_control_custom_label", "").strip() or None
        elif "|" in time_control_preset:
            base_seconds, increment_seconds = time_control_preset.split("|", 1)
            time_control_base_seconds = int(base_seconds)
            time_control_increment_seconds = int(increment_seconds)
            preset = next((item for item in TIME_CONTROL_PRESETS if item["value"] == time_control_preset), None)
            time_control_custom_label = preset["label"] if preset else None

        payload = {
            "game_type": form.get("game_type", "standard"),
            "white_user_id": int(form["white_user_id"]) if form.get("white_user_id") else None,
            "black_user_id": int(form["black_user_id"]) if form.get("black_user_id") else None,
            "white_partner_user_id": int(form["white_partner_user_id"]) if form.get("white_partner_user_id") else None,
            "black_partner_user_id": int(form["black_partner_user_id"]) if form.get("black_partner_user_id") else None,
            "white_instruction_clarity": int(form["white_instruction_clarity"]) if form.get("white_instruction_clarity") else None,
            "black_instruction_clarity": int(form["black_instruction_clarity"]) if form.get("black_instruction_clarity") else None,
            "white_use_guest": form.get("white_use_guest") == "1",
            "black_use_guest": form.get("black_use_guest") == "1",
            "white_guest_email": form.get("white_guest_email", "").strip().lower(),
            "black_guest_email": form.get("black_guest_email", "").strip().lower(),
            "result": form.get("result", ""),
            "played_at": form.get("played_at", ""),
            "time_control_label": build_time_control_label(
                time_control_base_seconds,
                time_control_increment_seconds,
                time_control_custom_label,
            ),
            "time_control_base_seconds": time_control_base_seconds,
            "time_control_increment_seconds": time_control_increment_seconds,
            "season_id": int(form["season_id"]) if form.get("season_id") else None,
            "opening_name": form.get("opening_name", "").strip() or None,
            "opening_code": form.get("opening_code", "").strip() or None,
            "pgn_text": form.get("pgn_text", "").strip() or None,
            "notes": form.get("notes", "").strip(),
            "challenge_id": int(form["challenge_id"]) if form.get("challenge_id") else None,
        }
        if payload["game_type"] == "standard":
            payload["white_partner_user_id"] = None
            payload["black_partner_user_id"] = None
            payload["white_instruction_clarity"] = None
            payload["black_instruction_clarity"] = None
        return payload

    def resolve_guest_player(db, group, email: str, *, created_by_user_id: int) -> tuple[int, str | None]:
        normalized_email = email.strip().lower()
        if not normalized_email:
            raise ValueError("Guest email is required.")
        if not email_looks_valid(normalized_email):
            raise ValueError("Use a valid guest email address.")

        existing_user = db.execute("SELECT * FROM users WHERE email = ?", (normalized_email,)).fetchone()
        if existing_user and not existing_user["is_placeholder"]:
            membership = db.execute(
                "SELECT 1 FROM memberships WHERE group_id = ? AND user_id = ? AND is_active = 1",
                (group["id"], existing_user["id"]),
            ).fetchone()
            if membership:
                return existing_user["id"], None
            raise ValueError("This email already belongs to a registered account. Ask that player to join the group first.")

        invite_status = None
        if existing_user is None:
            user_id = db.insert_and_get_id(
                """
                INSERT INTO users (name, email, password_hash, is_placeholder, placeholder_created_by)
                VALUES (?, ?, ?, 1, ?)
                """,
                (
                    placeholder_name_from_email(normalized_email),
                    normalized_email,
                    generate_password_hash(secrets.token_urlsafe(32)),
                    created_by_user_id,
                ),
            )
            existing_user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        membership = db.execute(
            "SELECT id, is_active FROM memberships WHERE group_id = ? AND user_id = ?",
            (group["id"], existing_user["id"]),
        ).fetchone()
        if membership is None:
            db.execute(
                "INSERT INTO memberships (group_id, user_id, role) VALUES (?, ?, 'member')",
                (group["id"], existing_user["id"]),
            )
            invite_status = send_placeholder_invite_email(
                existing_user["email"],
                existing_user["name"],
                group["name"],
                direct_invite_url_for_group(group),
                g.user["name"],
                g.user["email"],
            )
        elif not membership["is_active"]:
            db.execute("UPDATE memberships SET is_active = 1 WHERE id = ?", (membership["id"],))
            invite_status = send_placeholder_invite_email(
                existing_user["email"],
                existing_user["name"],
                group["name"],
                direct_invite_url_for_group(group),
                g.user["name"],
                g.user["email"],
            )
        return existing_user["id"], invite_status

    def resolve_match_payload_players(db, group, payload: dict, *, created_by_user_id: int) -> tuple[str | None, list[str]]:
        invite_statuses: list[str] = []
        for side in ("white", "black"):
            if payload[f"{side}_use_guest"]:
                try:
                    payload[f"{side}_user_id"], invite_status = resolve_guest_player(
                        db,
                        group,
                        payload[f"{side}_guest_email"],
                        created_by_user_id=created_by_user_id,
                    )
                except ValueError as exc:
                    return str(exc), []
                if invite_status:
                    invite_statuses.append(invite_status)
            elif payload[f"{side}_user_id"] is None:
                return "Choose a player or guest email for both sides.", []
        return None, invite_statuses

    def validate_match_payload(db, group_id: int, payload: dict, *, allow_former_employees: bool = False) -> str | None:
        members = get_group_members(db, group_id)
        member_ids = {member["id"] for member in members}
        former_employee_ids = {member["id"] for member in members if member["is_former_employee"]}
        participant_ids = [payload["white_user_id"], payload["black_user_id"]]
        if payload["white_partner_user_id"]:
            participant_ids.append(payload["white_partner_user_id"])
        if payload["black_partner_user_id"]:
            participant_ids.append(payload["black_partner_user_id"])

        if payload["game_type"] not in {"standard", "one_arm_one_brain"}:
            return "Invalid game type."
        if payload["white_user_id"] == payload["black_user_id"]:
            return "A match needs two different players."
        if any(player_id not in member_ids for player_id in participant_ids):
            return "All selected players must be members of the group."
        if not allow_former_employees and any(player_id in former_employee_ids for player_id in participant_ids):
            return "Former employee accounts cannot be selected for new matches."
        if len(participant_ids) != len(set(participant_ids)):
            return "Each player can only appear once in a match."
        if payload["game_type"] == "one_arm_one_brain" and (
            not payload["white_partner_user_id"] or not payload["black_partner_user_id"]
        ):
            return "One Arm, One Brain matches need a caller and a mover for both teams."
        if payload["result"] not in {"white", "black", "draw"}:
            return "Invalid result."
        if not payload["played_at"]:
            return "Played date is required."
        if payload["time_control_base_seconds"] is not None and payload["time_control_base_seconds"] <= 0:
            return "Time control needs a positive starting time."
        if payload["time_control_increment_seconds"] is not None and payload["time_control_increment_seconds"] < 0:
            return "Time increment cannot be negative."
        return None

    def save_match_record(db, group, payload: dict, *, reporter_id: int, match_id: int | None = None) -> tuple[int, str]:
        confirmation_status, confirmed_by = match_confirmation_fields(group)
        if match_id is None:
            match_id = db.insert_and_get_id(
                """
                INSERT INTO matches
                (group_id, season_id, game_type, white_user_id, white_partner_user_id, black_user_id, black_partner_user_id, result, played_at, time_control_label, time_control_base_seconds, time_control_increment_seconds, white_instruction_clarity, black_instruction_clarity, confirmation_status, confirmed_by, opening_name, opening_code, pgn_text, notes, reported_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group["id"],
                    payload["season_id"],
                    payload["game_type"],
                    payload["white_user_id"],
                    payload["white_partner_user_id"],
                    payload["black_user_id"],
                    payload["black_partner_user_id"],
                    payload["result"],
                    payload["played_at"],
                    payload["time_control_label"],
                    payload["time_control_base_seconds"],
                    payload["time_control_increment_seconds"],
                    payload["white_instruction_clarity"],
                    payload["black_instruction_clarity"],
                    confirmation_status,
                    confirmed_by,
                    payload["opening_name"],
                    payload["opening_code"],
                    payload["pgn_text"],
                    payload["notes"],
                    reporter_id,
                ),
            )
        else:
            db.execute(
                """
                UPDATE matches
                SET game_type = ?, white_user_id = ?, white_partner_user_id = ?, black_user_id = ?, black_partner_user_id = ?,
                    result = ?, played_at = ?, time_control_label = ?, time_control_base_seconds = ?, time_control_increment_seconds = ?,
                    season_id = ?, white_instruction_clarity = ?, black_instruction_clarity = ?,
                    confirmation_status = ?, confirmed_by = ?, opening_name = ?, opening_code = ?, pgn_text = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    payload["game_type"],
                    payload["white_user_id"],
                    payload["white_partner_user_id"],
                    payload["black_user_id"],
                    payload["black_partner_user_id"],
                    payload["result"],
                    payload["played_at"],
                    payload["time_control_label"],
                    payload["time_control_base_seconds"],
                    payload["time_control_increment_seconds"],
                    payload["season_id"],
                    payload["white_instruction_clarity"],
                    payload["black_instruction_clarity"],
                    confirmation_status,
                    confirmed_by,
                    payload["opening_name"],
                    payload["opening_code"],
                    payload["pgn_text"],
                    payload["notes"],
                    match_id,
                ),
            )
        return match_id, confirmation_status

    def sync_match_coffee_debts(db, group_id: int, match_id: int) -> None:
        db.execute(
            "DELETE FROM coffee_ledger WHERE group_id = ? AND source_match_id = ?",
            (group_id, match_id),
        )
        match_row = db.execute(
            """
            SELECT *
            FROM matches
            WHERE id = ? AND group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
            """,
            (match_id, group_id),
        ).fetchone()
        if match_row is None or match_row["result"] == "draw":
            return

        winner_side = "white" if match_row["result"] == "white" else "black"
        loser_side = "black" if winner_side == "white" else "white"
        creditor_ids = match_side_player_ids(match_row, winner_side)
        debtor_ids = match_side_player_ids(match_row, loser_side)
        if len(creditor_ids) != len(debtor_ids):
            return

        reason = tr("Match result coffee")
        rows = [
            (group_id, debtor_user_id, creditor_user_id, 1, reason, "match_auto", match_id, match_row["confirmed_by"] or match_row["reported_by"])
            for debtor_user_id, creditor_user_id in zip(debtor_ids, creditor_ids)
        ]
        db.executemany(
            """
            INSERT INTO coffee_ledger
            (group_id, debtor_user_id, creditor_user_id, amount, reason, entry_type, source_match_id, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def optimize_group_coffee_debts(db, group_id: int) -> bool:
        open_rows = db.execute(
            """
            SELECT id, debtor_user_id, creditor_user_id, amount
            FROM coffee_ledger
            WHERE group_id = ? AND is_settled = 0
            ORDER BY id
            """,
            (group_id,),
        ).fetchall()
        if not open_rows:
            return False

        original_edges = defaultdict(int)
        balances = defaultdict(int)
        for row in open_rows:
            original_edges[(row["debtor_user_id"], row["creditor_user_id"])] += row["amount"]
            balances[row["debtor_user_id"]] -= row["amount"]
            balances[row["creditor_user_id"]] += row["amount"]

        debtors = [[user_id, -amount] for user_id, amount in balances.items() if amount < 0]
        creditors = [[user_id, amount] for user_id, amount in balances.items() if amount > 0]
        optimized_rows = []
        debtor_index = 0
        creditor_index = 0
        while debtor_index < len(debtors) and creditor_index < len(creditors):
            debtor_user_id, debt_amount = debtors[debtor_index]
            creditor_user_id, credit_amount = creditors[creditor_index]
            transfer_amount = min(debt_amount, credit_amount)
            optimized_rows.append((debtor_user_id, creditor_user_id, transfer_amount))
            debtors[debtor_index][1] -= transfer_amount
            creditors[creditor_index][1] -= transfer_amount
            if debtors[debtor_index][1] == 0:
                debtor_index += 1
            if creditors[creditor_index][1] == 0:
                creditor_index += 1

        optimized_edges = defaultdict(int)
        for debtor_user_id, creditor_user_id, amount in optimized_rows:
            optimized_edges[(debtor_user_id, creditor_user_id)] += amount

        if dict(original_edges) == dict(optimized_edges):
            return False

        db.executemany(
            """
            UPDATE coffee_ledger
            SET is_settled = 1, settled_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [(row["id"],) for row in open_rows],
        )
        if optimized_rows:
            db.executemany(
                """
                INSERT INTO coffee_ledger
                (group_id, debtor_user_id, creditor_user_id, amount, reason, entry_type, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (group_id, debtor_user_id, creditor_user_id, amount, tr("Optimized from open balances"), "optimization", g.user["id"])
                    for debtor_user_id, creditor_user_id, amount in optimized_rows
                ],
            )
        return True

    def can_edit_match(group, match_row) -> bool:
        return group["membership_role"] in {"owner", "admin"} or match_row["reported_by"] == g.user["id"]

    def can_confirm_match(group, match_row) -> bool:
        if row_value(match_row, "confirmation_status") != "pending":
            return False
        if group["membership_role"] in {"owner", "admin"}:
            return True
        participants = set(match_side_player_ids(match_row, "white") + match_side_player_ids(match_row, "black"))
        return g.user["id"] in participants and row_value(match_row, "reported_by") != g.user["id"]

    def find_active_challenge_for_match(db, group_id: int, payload: dict):
        if payload["game_type"] != "standard":
            return None
        white_user_id = payload["white_user_id"]
        black_user_id = payload["black_user_id"]
        if not white_user_id or not black_user_id:
            return None
        return db.execute(
            """
            SELECT *
            FROM challenges
            WHERE group_id = ? AND status IN ('open', 'accepted')
              AND (
                (challenger_user_id = ? AND challenged_user_id = ?)
                OR (challenger_user_id = ? AND challenged_user_id = ?)
              )
            ORDER BY CASE status WHEN 'accepted' THEN 0 ELSE 1 END, created_at ASC, id ASC
            LIMIT 1
            """,
            (group_id, white_user_id, black_user_id, black_user_id, white_user_id),
        ).fetchone()

    def complete_challenge_for_match(db, group_id: int, match_id: int, payload: dict) -> int | None:
        challenge_id = payload["challenge_id"]
        if not challenge_id:
            challenge = find_active_challenge_for_match(db, group_id, payload)
            challenge_id = challenge["id"] if challenge else None
        if challenge_id:
            db.execute(
                """
                UPDATE challenges
                SET status = 'completed', match_id = ?, responded_at = CURRENT_TIMESTAMP
                WHERE id = ? AND group_id = ? AND status IN ('open', 'accepted')
                """,
                (match_id, challenge_id, group_id),
            )
        return challenge_id

    def result_label(result: str, white_name: str, black_name: str) -> str:
        if result == "white":
            return f"{white_name} {tr('won')}"
        if result == "black":
            return f"{black_name} {tr('won')}"
        return tr("Draw")

    def get_color_stats(db, group_id: int):
        return db.execute(
            """
            SELECT u.id, u.name,
                   SUM(CASE WHEN m.white_user_id = u.id OR m.white_partner_user_id = u.id THEN 1 ELSE 0 END) AS white_games,
                   SUM(CASE WHEN m.black_user_id = u.id OR m.black_partner_user_id = u.id THEN 1 ELSE 0 END) AS black_games,
                   SUM(CASE WHEN (m.white_user_id = u.id OR m.white_partner_user_id = u.id) AND m.result = 'white' THEN 1 ELSE 0 END) AS white_wins,
                   SUM(CASE WHEN (m.black_user_id = u.id OR m.black_partner_user_id = u.id) AND m.result = 'black' THEN 1 ELSE 0 END) AS black_wins
            FROM memberships ms
            JOIN users u ON u.id = ms.user_id
            LEFT JOIN matches m ON m.group_id = ms.group_id AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
                AND (m.white_user_id = u.id OR m.black_user_id = u.id OR m.white_partner_user_id = u.id OR m.black_partner_user_id = u.id)
            WHERE ms.group_id = ? AND ms.is_active = 1
            GROUP BY u.id, u.name
            ORDER BY lower(u.name)
            """,
            (group_id,),
        ).fetchall()

    def get_opening_stats(db, group_id: int):
        return db.execute(
            """
            SELECT COALESCE(opening_name, 'Unknown opening') AS opening_name,
                   COALESCE(opening_code, '') AS opening_code,
                   COUNT(*) AS games,
                   SUM(CASE WHEN result = 'white' THEN 1 ELSE 0 END) AS white_wins,
                   SUM(CASE WHEN result = 'black' THEN 1 ELSE 0 END) AS black_wins,
                   SUM(CASE WHEN result = 'draw' THEN 1 ELSE 0 END) AS draws
            FROM matches
            WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
            GROUP BY COALESCE(opening_name, 'Unknown opening'), COALESCE(opening_code, '')
            ORDER BY games DESC, opening_name
            LIMIT 12
            """,
            (group_id,),
        ).fetchall()

    def get_belt_history(db, group_id: int):
        matches = db.execute(
            """
            SELECT m.*, w.name AS white_name, wp.name AS white_partner_name,
                   b.name AS black_name, bp.name AS black_partner_name
            FROM matches m
            JOIN users w ON w.id = m.white_user_id
            LEFT JOIN users wp ON wp.id = m.white_partner_user_id
            JOIN users b ON b.id = m.black_user_id
            LEFT JOIN users bp ON bp.id = m.black_partner_user_id
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
            ORDER BY m.played_at ASC, m.id ASC
            """,
            (group_id,),
        ).fetchall()
        holder_id = None
        holder_name = None
        history = []
        for match in matches:
            if match["result"] == "draw":
                continue
            winner_side = "white" if match["result"] == "white" else "black"
            loser_side = "black" if winner_side == "white" else "white"
            winner_id = tuple(sorted(match_side_player_ids(match, winner_side)))
            winner_name = match_side_label(match, winner_side)
            loser_id = tuple(sorted(match_side_player_ids(match, loser_side)))
            loser_name = match_side_label(match, loser_side)
            if holder_id is None:
                holder_id = winner_id
                holder_name = winner_name
                history.append({"played_at": match["played_at"], "holder_name": holder_name, "reason": "Claimed the belt"})
            elif loser_id == holder_id:
                holder_id = winner_id
                holder_name = winner_name
                history.append({"played_at": match["played_at"], "holder_name": holder_name, "reason": f"Took the belt from {loser_name}"})
        return {"current_holder_id": holder_id, "current_holder_name": holder_name, "history": list(reversed(history[:12]))}

    def get_player_trends(db, group_id: int):
        members = {row["id"]: row["name"] for row in get_group_members(db, group_id)}
        matches = db.execute(
            """
            SELECT *
            FROM matches
            WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
            ORDER BY played_at ASC, id ASC
            """,
            (group_id,),
        ).fetchall()
        history = defaultdict(list)
        for match in matches:
            white_team = match_side_player_ids(match, "white")
            black_team = match_side_player_ids(match, "black")
            if match["result"] == "white":
                outcomes = {user_id: "W" for user_id in white_team} | {user_id: "L" for user_id in black_team}
            elif match["result"] == "black":
                outcomes = {user_id: "L" for user_id in white_team} | {user_id: "W" for user_id in black_team}
            else:
                outcomes = {user_id: "D" for user_id in white_team + black_team}
            for user_id, outcome in outcomes.items():
                history[user_id].append({"outcome": outcome, "played_at": match["played_at"]})

        trends = []
        for user_id, name in members.items():
            series = history[user_id]
            recent = "".join(item["outcome"] for item in series[-5:]) or "-"
            win_streak = 0
            unbeaten_streak = 0
            for item in reversed(series):
                if item["outcome"] == "W":
                    win_streak += 1
                    unbeaten_streak += 1
                elif item["outcome"] == "D":
                    unbeaten_streak += 1
                    if win_streak == 0:
                        pass
                    else:
                        break
                else:
                    break
            last_played = series[-1]["played_at"] if series else None
            trends.append(
                {
                    "user_id": user_id,
                    "name": name,
                    "recent_form": recent,
                    "win_streak": win_streak,
                    "unbeaten_streak": unbeaten_streak,
                    "games_played": len(series),
                    "last_played": last_played,
                }
            )
        return trends

    def get_group_records(db, group_id: int):
        biggest_upset = db.execute(
            """
            SELECT winner.name AS winner_name, loser.name AS loser_name, ABS(winner_history.rating_before - loser_history.rating_before) AS gap,
                   m.played_at
            FROM rating_history winner_history
            JOIN rating_history loser_history ON loser_history.match_id = winner_history.match_id AND loser_history.user_id != winner_history.user_id
            JOIN matches m ON m.id = winner_history.match_id
            JOIN users winner ON winner.id = winner_history.user_id
            JOIN users loser ON loser.id = loser_history.user_id
            WHERE winner_history.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
              AND (
                (m.result = 'white' AND m.white_user_id = winner_history.user_id) OR
                (m.result = 'black' AND m.black_user_id = winner_history.user_id)
              )
              AND m.game_type = 'standard'
              AND winner_history.rating_before < loser_history.rating_before
            ORDER BY gap DESC, m.played_at DESC
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()
        biggest_gain = db.execute(
            """
            SELECT u.name, MAX(rh.delta) AS best_gain
            FROM rating_history rh
            JOIN users u ON u.id = rh.user_id
            WHERE rh.group_id = ? AND rh.ladder_type = 'standard'
            GROUP BY u.id, u.name
            ORDER BY best_gain DESC, lower(u.name)
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()
        peak_rating = db.execute(
            """
            SELECT u.name, MAX(rh.rating_after) AS peak_rating
            FROM rating_history rh
            JOIN users u ON u.id = rh.user_id
            WHERE rh.group_id = ? AND rh.ladder_type = 'standard'
            GROUP BY u.id, u.name
            ORDER BY peak_rating DESC, lower(u.name)
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()
        hottest_month = db.execute(
            """
            SELECT u.name, SUM(rh.delta) AS monthly_gain, substr(m.played_at, 1, 7) AS month_key
            FROM rating_history rh
            JOIN matches m ON m.id = rh.match_id
            JOIN users u ON u.id = rh.user_id
            WHERE rh.group_id = ? AND rh.ladder_type = 'standard'
            GROUP BY u.id, u.name, month_key
            ORDER BY monthly_gain DESC, month_key DESC
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()
        return {
            "biggest_upset": biggest_upset,
            "biggest_gain": biggest_gain,
            "peak_rating": peak_rating,
            "hottest_month": hottest_month,
        }

    def get_activity_nudges(db, group_id: int):
        current_month = datetime.now().strftime("%Y-%m")
        monthly = db.execute(
            """
            SELECT u.name, COUNT(*) AS total
            FROM (
                SELECT white_user_id AS user_id, played_at FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
                UNION ALL
                SELECT black_user_id AS user_id, played_at FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
                UNION ALL
                SELECT white_partner_user_id AS user_id, played_at FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed' AND white_partner_user_id IS NOT NULL
                UNION ALL
                SELECT black_partner_user_id AS user_id, played_at FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed' AND black_partner_user_id IS NOT NULL
            ) played
            JOIN users u ON u.id = played.user_id
            WHERE substr(played_at, 1, 7) = ?
            GROUP BY u.id, u.name
            ORDER BY total DESC, lower(u.name)
            LIMIT 3
            """,
            (group_id, group_id, group_id, group_id, current_month),
        ).fetchall()
        inactive = db.execute(
            """
            SELECT u.name, MAX(played.played_at) AS last_played
            FROM memberships ms
            JOIN users u ON u.id = ms.user_id
            LEFT JOIN (
                SELECT white_user_id AS user_id, played_at FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
                UNION ALL
                SELECT black_user_id AS user_id, played_at FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
                UNION ALL
                SELECT white_partner_user_id AS user_id, played_at FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed' AND white_partner_user_id IS NOT NULL
                UNION ALL
                SELECT black_partner_user_id AS user_id, played_at FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed' AND black_partner_user_id IS NOT NULL
            ) played ON played.user_id = u.id
            WHERE ms.group_id = ? AND ms.is_active = 1
            GROUP BY u.id, u.name
            ORDER BY (last_played IS NOT NULL) DESC, last_played ASC, lower(u.name)
            """,
            (group_id, group_id, group_id, group_id, group_id),
        ).fetchall()
        return {"monthly_leaders": monthly, "inactive_players": inactive[:5]}

    def get_achievements(db, group_id: int):
        leaderboard = get_group_leaderboard(db, group_id)
        trends = get_player_trends(db, group_id)
        by_id = {row["id"]: row for row in leaderboard}
        trend_by_id = {row["user_id"]: row for row in trends}
        results = []
        if leaderboard:
            top = leaderboard[0]
            results.append({"title": "Table Boss", "player": top["name"], "description": "Current rating leader."})
        most_draws = max(leaderboard, key=lambda row: row["draws"], default=None)
        if most_draws and most_draws["draws"] > 0:
            results.append({"title": "Draw Specialist", "player": most_draws["name"], "description": f"{most_draws['draws']} drawn games."})
        giant_killer = get_group_records(db, group_id)["biggest_upset"]
        if giant_killer:
            results.append({"title": "Giant Killer", "player": giant_killer["winner_name"], "description": f"Beat {giant_killer['loser_name']} as the underdog."})
        hottest = max(trends, key=lambda row: row["win_streak"], default=None)
        if hottest and hottest["win_streak"] > 1:
            results.append({"title": "Heat Check", "player": hottest["name"], "description": f"{hottest['win_streak']} straight wins."})
        coffee_shark = db.execute(
            """
            SELECT u.name, SUM(amount) AS total
            FROM coffee_ledger c
            JOIN users u ON u.id = c.creditor_user_id
            WHERE c.group_id = ?
            GROUP BY u.id, u.name
            ORDER BY total DESC, lower(u.name)
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()
        if coffee_shark and coffee_shark["total"]:
            results.append({"title": "Coffee Shark", "player": coffee_shark["name"], "description": f"Has earned {coffee_shark['total']} coffee credits."})
        return results

    def get_weekly_missions(db, group_id: int, user_id: int):
        missions = []
        recent_date = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        this_week_games = db.execute(
            """
            SELECT COUNT(*) AS total
            FROM matches
            WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
              AND (white_user_id = ? OR black_user_id = ?)
              AND played_at >= ?
            """,
            (group_id, user_id, user_id, recent_date),
        ).fetchone()["total"]
        color_split = db.execute(
            """
            SELECT
                SUM(CASE WHEN white_user_id = ? THEN 1 ELSE 0 END) AS white_games,
                SUM(CASE WHEN black_user_id = ? THEN 1 ELSE 0 END) AS black_games
            FROM matches
            WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
              AND played_at >= ?
            """,
            (user_id, user_id, group_id, recent_date),
        ).fetchone()
        above_me = db.execute(
            """
            WITH board AS (
                SELECT u.id, COALESCE(latest.rating_after, g.starting_rating) AS rating
                FROM memberships m
                JOIN users u ON u.id = m.user_id
                JOIN groups_workspace g ON g.id = m.group_id
                LEFT JOIN (
                    SELECT rh.user_id, rh.rating_after,
                           ROW_NUMBER() OVER (PARTITION BY rh.user_id ORDER BY rh.id DESC) AS rn
                    FROM rating_history rh
                    WHERE rh.group_id = ? AND rh.ladder_type = 'standard'
                ) latest ON latest.user_id = u.id AND latest.rn = 1
                WHERE m.group_id = ? AND m.is_active = 1
            )
            SELECT COUNT(*) AS total
            FROM matches m
            JOIN board me ON me.id = ?
            JOIN board opp ON opp.id = CASE WHEN m.white_user_id = ? THEN m.black_user_id ELSE m.white_user_id END
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
              AND m.game_type = 'standard'
              AND (m.white_user_id = ? OR m.black_user_id = ?)
              AND m.played_at >= ?
              AND opp.rating > me.rating
              AND ((m.result = 'white' AND m.white_user_id = ?) OR (m.result = 'black' AND m.black_user_id = ?))
            """,
            (group_id, group_id, user_id, user_id, group_id, user_id, user_id, recent_date, user_id, user_id),
        ).fetchone()["total"]
        missions.append({"title": tr("Three-game week"), "progress": min(this_week_games, 3), "target": 3})
        missions.append({"title": tr("Play both colors"), "progress": int((color_split["white_games"] or 0) > 0) + int((color_split["black_games"] or 0) > 0), "target": 2})
        missions.append({"title": tr("Beat someone above you"), "progress": min(above_me, 1), "target": 1})
        return missions

    def get_team_standings(db, group_id: int):
        teams = db.execute(
            """
            SELECT t.id, t.name, t.color,
                   COUNT(DISTINCT m.user_id) AS members
            FROM teams t
            LEFT JOIN memberships m ON m.team_id = t.id AND m.is_active = 1
            WHERE t.group_id = ?
            GROUP BY t.id, t.name, t.color
            ORDER BY lower(t.name)
            """,
            (group_id,),
        ).fetchall()
        standings = []
        for team in teams:
            row = db.execute(
                """
                SELECT
                    SUM(CASE
                        WHEN ms.team_id = ? AND (
                            (matches.result = 'white' AND (matches.white_user_id = ms.user_id OR matches.white_partner_user_id = ms.user_id)) OR
                            (matches.result = 'black' AND (matches.black_user_id = ms.user_id OR matches.black_partner_user_id = ms.user_id))
                        )
                        THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE
                        WHEN ms.team_id = ? AND (
                            (matches.result = 'white' AND (matches.black_user_id = ms.user_id OR matches.black_partner_user_id = ms.user_id)) OR
                            (matches.result = 'black' AND (matches.white_user_id = ms.user_id OR matches.white_partner_user_id = ms.user_id))
                        )
                        THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN ms.team_id = ? AND matches.result = 'draw' THEN 1 ELSE 0 END) AS draws
                FROM memberships ms
                LEFT JOIN matches ON matches.group_id = ms.group_id AND matches.deleted_at IS NULL AND matches.confirmation_status = 'confirmed'
                    AND (
                        matches.white_user_id = ms.user_id OR matches.black_user_id = ms.user_id
                        OR matches.white_partner_user_id = ms.user_id OR matches.black_partner_user_id = ms.user_id
                    )
                WHERE ms.group_id = ? AND ms.is_active = 1
                """,
                (team["id"], team["id"], team["id"], group_id),
            ).fetchone()
            standings.append(
                {
                    "id": team["id"],
                    "name": team["name"],
                    "color": team["color"],
                    "members": team["members"],
                    "wins": row["wins"] or 0,
                    "losses": row["losses"] or 0,
                    "draws": row["draws"] or 0,
                    "points": (row["wins"] or 0) * 3 + (row["draws"] or 0),
                }
            )
        return sorted(standings, key=lambda row: (-row["points"], -row["wins"], row["name"].lower()))

    def get_match_suggestions(db, group_id: int, limit: int = 4, user_id: int | None = None):
        players = get_group_leaderboard(db, group_id)
        group_row = db.execute(
            "SELECT default_k_factor FROM groups_workspace WHERE id = ?",
            (group_id,),
        ).fetchone()
        k_factor = int(group_row["default_k_factor"]) if group_row else 24
        pair_history = {
            tuple(sorted((row["white_user_id"], row["black_user_id"]))): row
            for row in db.execute(
                """
                SELECT MIN(white_user_id, black_user_id) AS white_user_id,
                       MAX(white_user_id, black_user_id) AS black_user_id,
                       COUNT(*) AS games,
                       MAX(played_at) AS last_played
                FROM matches
                WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed' AND game_type = 'standard'
                GROUP BY MIN(white_user_id, black_user_id), MAX(white_user_id, black_user_id)
                """,
                (group_id,),
            ).fetchall()
        }

        suggestions = []
        player_pairs = []
        if user_id is None:
            player_pairs = list(combinations(players, 2))
        else:
            current_player = next((player for player in players if player["id"] == user_id), None)
            if current_player is None:
                return []
            player_pairs = [(current_player, opponent) for opponent in players if opponent["id"] != user_id]

        for left, right in player_pairs:
            pair_key = tuple(sorted((left["id"], right["id"])))
            history = pair_history.get(pair_key)
            games = history["games"] if history else 0
            last_played = history["last_played"] if history else None
            focus = left if user_id is None else next(player for player in (left, right) if player["id"] == user_id)
            opponent = right if focus["id"] == left["id"] else left
            rating_gap = opponent["rating"] - focus["rating"]
            win_delta = calculate_elo_change(focus["rating"], opponent["rating"], 1.0, k_factor)
            loss_delta = calculate_elo_change(focus["rating"], opponent["rating"], 0.0, k_factor)
            suggestions.append(
                {
                    "left": left,
                    "right": right,
                    "player": focus,
                    "opponent": opponent,
                    "games": games,
                    "last_played": last_played,
                    "rating_gap": round(rating_gap),
                    "win_points": round(win_delta, 1),
                    "loss_points": round(abs(loss_delta), 1),
                    "score": win_delta,
                }
            )

        return sorted(suggestions, key=lambda item: (-item["score"], item["opponent"]["name"].lower()))[:limit]

    def get_hall_of_fame(db, group_id: int):
        champions = get_group_leaderboard(db, group_id)
        champion = champions[0] if champions else None
        most_wins = db.execute(
            """
            SELECT u.name, COUNT(*) AS total
            FROM matches m
            JOIN users u
              ON (u.id = m.white_user_id AND m.result = 'white')
              OR (u.id = m.black_user_id AND m.result = 'black')
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = 'standard'
            GROUP BY u.id, u.name
            ORDER BY total DESC, lower(u.name)
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()
        busiest = db.execute(
            """
            SELECT u.name, COUNT(*) AS total
            FROM (
                SELECT white_user_id AS user_id FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed' AND game_type = 'standard'
                UNION ALL
                SELECT black_user_id AS user_id FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed' AND game_type = 'standard'
            ) played
            JOIN users u ON u.id = played.user_id
            GROUP BY u.id, u.name
            ORDER BY total DESC, lower(u.name)
            LIMIT 1
            """,
            (group_id, group_id),
        ).fetchone()
        tournament_winners = db.execute(
            """
            SELECT u.name, COUNT(*) AS titles
            FROM tournaments t
            JOIN users u ON u.id = t.winner_user_id
            WHERE t.group_id = ? AND t.winner_user_id IS NOT NULL
            GROUP BY u.id, u.name
            ORDER BY titles DESC, lower(u.name)
            LIMIT 5
            """,
            (group_id,),
        ).fetchall()
        return {
            "champion": champion,
            "most_wins": most_wins,
            "busiest": busiest,
            "tournament_winners": tournament_winners,
        }

    def chart_side_name(primary_name: str | None, partner_name: str | None) -> str:
        if partner_name:
            return f"{primary_name} + {partner_name}"
        return primary_name or "Unknown"

    def build_rating_series(rows, max_series: int = 8) -> dict | None:
        if not rows:
            return None

        matches = []
        match_index = {}
        for row in rows:
            if row["match_id"] not in match_index:
                match_index[row["match_id"]] = len(matches)
                matches.append(
                    {
                        "date": row["played_at"],
                        "label": f"{chart_side_name(row['white_name'], row['white_partner_name'])} vs {chart_side_name(row['black_name'], row['black_partner_name'])}",
                    }
                )

        series_map = defaultdict(list)
        for row in rows:
            series_map[row["name"]].append(
                {
                    "i": match_index[row["match_id"]],
                    "r": round(row["rating_after"], 1),
                    "d": round(row["delta"], 1),
                }
            )

        included = list(series_map.items())
        if not included or len(matches) < 2:
            return None
        included.sort(key=lambda item: (-len(item[1]), item[0].lower()))
        overflow = max(0, len(included) - max_series)
        included = included[:max_series]

        series = [
            {"name": name, "points": points, "last": round(points[-1]["r"])}
            for name, points in included
        ]
        return {"matches": matches, "series": series, "overflow": overflow}

    def get_rating_sparklines(db, group_id: int, ladder_type: str = "standard", points: int = 20) -> dict[int, list[float]]:
        game_type = "standard" if ladder_type == "standard" else "one_arm_one_brain"
        rows = db.execute(
            """
            SELECT rh.user_id, rh.rating_after
            FROM rating_history rh
            JOIN matches m ON m.id = rh.match_id
            WHERE rh.group_id = ? AND rh.ladder_type = ?
              AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = ?
            ORDER BY m.played_at ASC, m.id ASC, rh.id ASC
            """,
            (group_id, ladder_type, game_type),
        ).fetchall()
        sparklines: dict[int, list[float]] = defaultdict(list)
        for row in rows:
            sparklines[row["user_id"]].append(round(row["rating_after"], 1))
        return {user_id: values[-points:] for user_id, values in sparklines.items()}

    def get_monthly_activity(db, group_id: int, months: int = 12):
        rows = db.execute(
            """
            SELECT substr(played_at, 1, 7) AS month,
                   COUNT(*) AS total,
                   SUM(CASE WHEN result = 'draw' THEN 1 ELSE 0 END) AS draws
            FROM matches
            WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
            GROUP BY month
            ORDER BY month DESC
            LIMIT ?
            """,
            (group_id, months),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def build_member_series(db, group_id: int, user_id: int) -> dict:
        results = {}
        for ladder_type, game_type in (("standard", "standard"), ("braccio_mente", "one_arm_one_brain")):
            rows = db.execute(
                """
                SELECT rh.rating_after, rh.delta, m.played_at, m.result,
                       m.white_user_id, m.white_partner_user_id, m.black_user_id, m.black_partner_user_id,
                       white.name AS white_name, white_partner.name AS white_partner_name,
                       black.name AS black_name, black_partner.name AS black_partner_name
                FROM rating_history rh
                JOIN matches m ON m.id = rh.match_id
                JOIN users white ON white.id = m.white_user_id
                LEFT JOIN users white_partner ON white_partner.id = m.white_partner_user_id
                JOIN users black ON black.id = m.black_user_id
                LEFT JOIN users black_partner ON black_partner.id = m.black_partner_user_id
                WHERE rh.group_id = ? AND rh.ladder_type = ? AND rh.user_id = ?
                  AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = ?
                ORDER BY m.played_at ASC, m.id ASC, rh.id ASC
                """,
                (group_id, ladder_type, user_id, game_type),
            ).fetchall()
            points = []
            for position, row in enumerate(rows):
                on_white = user_id in {row["white_user_id"], row["white_partner_user_id"]}
                if row["result"] == "draw":
                    outcome = "D"
                elif (row["result"] == "white") == on_white:
                    outcome = "W"
                else:
                    outcome = "L"
                opponent_side = "black" if on_white else "white"
                opponent = chart_side_name(row[f"{opponent_side}_name"], row[f"{opponent_side}_partner_name"])
                points.append(
                    {
                        "i": position,
                        "r": round(row["rating_after"], 1),
                        "d": round(row["delta"], 1),
                        "date": row["played_at"],
                        "vs": opponent,
                        "o": outcome,
                    }
                )
            results[ladder_type] = points
        return results

    @app.route("/")
    def index():
        if g.user is None:
            return render_template("home.html")
        db = get_db()
        groups = db.execute(
            """
            SELECT g.*, m.role
            FROM memberships m
            JOIN groups_workspace g ON g.id = m.group_id
            WHERE m.user_id = ? AND m.is_active = 1
            ORDER BY lower(g.name)
            """,
            (g.user["id"],),
        ).fetchall()
        signup_notifications = get_signup_notifications(db, g.user["id"])
        can_manage_app_data = user_can_manage_app_data(g.user["id"])
        return render_template(
            "dashboard_home.html",
            groups=groups,
            signup_notifications=signup_notifications,
            can_manage_app_data=can_manage_app_data,
        )

    @app.route("/register", methods=("GET", "POST"))
    def register():
        if request.method == "POST":
            name = request.form["name"].strip()
            email = request.form["email"].strip().lower()
            password = request.form["password"]
            db = get_db()
            error = None

            if not name:
                error = "Name is required."
            elif not email:
                error = "Email is required."
            elif not password or len(password) < 8:
                error = "Use a password with at least 8 characters."
            else:
                existing_user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if existing_user and not existing_user["is_placeholder"]:
                    error = "An account with this email already exists."

            if error is None:
                if existing_user and existing_user["is_placeholder"]:
                    db.execute(
                        """
                        UPDATE users
                        SET name = ?, password_hash = ?, is_placeholder = 0, placeholder_created_by = NULL
                        WHERE id = ?
                        """,
                        (name, generate_password_hash(password), existing_user["id"]),
                    )
                    user_id = existing_user["id"]
                else:
                    user_id = db.insert_and_get_id(
                        "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                        (name, email, generate_password_hash(password)),
                    )
                db.execute("INSERT INTO signup_notifications (new_user_id) VALUES (?)", (user_id,))
                db.commit()
                flash(tr("Account claimed. You can sign in now.") if existing_user and existing_user["is_placeholder"] else tr("Account created. You can sign in now."), "success")
                target = pending_invite_target()
                return redirect(target or url_for("login"))

            flash(error, "danger")

        return render_template("auth/register.html")

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if request.method == "POST":
            email = request.form["email"].strip().lower()
            password = request.form["password"]
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

            if user and user["is_placeholder"]:
                flash(tr("This email already has a placeholder account. Create your account with this same email to claim it."), "danger")
            elif user is None or not check_password_hash(user["password_hash"], password):
                flash(tr("Invalid email or password."), "danger")
            else:
                pending = session.get("pending_invite")
                session.clear()
                if pending:
                    session["pending_invite"] = pending
                session["user_id"] = user["id"]
                flash(tr("Welcome back."), "success")
                target = pending_invite_target()
                return redirect(target or url_for("index"))

        return render_template("auth/login.html", pending_invite=session.get("pending_invite"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.post("/language")
    def set_language():
        language = request.form.get("language", "").strip().lower()
        if language in SUPPORTED_LANGUAGES:
            session["language"] = language
        next_url = request.form.get("next_url", "").strip()
        if not next_url.startswith("/"):
            next_url = url_for("index")
        return redirect(next_url)

    @app.route("/avatars/<int:user_id>.jpg")
    def uploaded_avatar(user_id: int):
        row = get_db().execute(
            "SELECT avatar_upload_data, avatar_upload_format FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None or not row["avatar_upload_data"]:
            return Response(status=404)
        return Response(
            base64.b64decode(row["avatar_upload_data"]),
            mimetype=row["avatar_upload_format"] or "image/jpeg",
        )

    @app.route("/account", methods=("GET", "POST"))
    @login_required
    def account():
        db = get_db()
        unlocked_titles, unlocked_reward_icons = get_unlocked_reward_choices(db, g.user["id"])
        reward_icon_codes = {item["code"] for item in unlocked_reward_icons}
        if request.method == "POST":
            avatar_color = request.form["avatar_color"].strip() or "#b5472f"
            avatar_icon = request.form.get("avatar_icon", "initials").strip()
            selected_title = request.form.get("selected_title", "").strip() or None
            uploaded_avatar_data, uploaded_avatar_format = normalize_uploaded_avatar(request.files.get("avatar_upload"))
            allowed_avatar_icons = {code for code, _ in BASE_AVATAR_CHOICES} | reward_icon_codes
            if uploaded_avatar_data:
                avatar_icon = "uploaded"
            elif avatar_icon == "uploaded" and not g.user["avatar_upload_data"]:
                avatar_icon = "initials"
            elif avatar_icon not in allowed_avatar_icons | {"uploaded"}:
                avatar_icon = "initials"
            if selected_title and selected_title not in unlocked_titles:
                selected_title = None
            db.execute(
                """
                UPDATE users
                SET name = ?, bio = ?, favorite_opening = ?, avatar_color = ?, avatar_icon = ?, tagline = ?, selected_title = ?,
                    avatar_upload_data = COALESCE(?, avatar_upload_data),
                    avatar_upload_format = COALESCE(?, avatar_upload_format)
                WHERE id = ?
                """,
                (
                    request.form["name"].strip(),
                    request.form["bio"].strip(),
                    request.form["favorite_opening"].strip(),
                    avatar_color,
                    avatar_icon,
                    request.form["tagline"].strip(),
                    selected_title,
                    uploaded_avatar_data,
                    uploaded_avatar_format,
                    g.user["id"],
                ),
            )
            db.commit()
            flash(tr("Profile updated."), "success")
            return redirect(url_for("account"))

        profile = db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
        unlocked_achievements = get_unlocked_achievements_for_user(db, g.user["id"])
        return render_template(
            "account.html",
            profile=profile,
            base_avatar_choices=BASE_AVATAR_CHOICES,
            reward_avatar_choices=unlocked_reward_icons,
            unlocked_titles=unlocked_titles,
            unlocked_achievements=unlocked_achievements,
        )

    @app.route("/system/data", methods=("GET", "POST"))
    @login_required
    def system_data():
        try:
            system_data_admin_required()
        except PermissionError:
            flash(tr("Only group owners or admins can manage app data."), "danger")
            return redirect(url_for("index"))

        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "import_snapshot")
            if action == "remove_member":
                group_id = int(request.form["group_id"])
                member_id = int(request.form["member_id"])
                owned_group = owner_group_or_none(group_id)
                if owned_group is None:
                    flash(tr("Only group owners can remove members."), "danger")
                    return redirect(url_for("system_data"))
                if member_id == g.user["id"]:
                    flash(tr("You cannot remove yourself from this page."), "danger")
                    return redirect(url_for("system_data"))
                target_member = db.execute(
                    """
                    SELECT u.id, u.name, u.email, m.role
                    FROM memberships m
                    JOIN users u ON u.id = m.user_id
                    WHERE m.group_id = ? AND m.user_id = ? AND m.is_active = 1
                    """,
                    (group_id, member_id),
                ).fetchone()
                if target_member is None:
                    flash(tr("Member not found in this group."), "danger")
                    return redirect(url_for("system_data"))
                if target_member["role"] == "owner":
                    flash(tr("Owners cannot be removed from their group."), "danger")
                    return redirect(url_for("system_data"))
                db.execute(
                    """
                    UPDATE memberships
                    SET is_active = 0, team_id = NULL
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (group_id, member_id),
                )
                removed_from_db = False
                if can_delete_user_fully(db, member_id):
                    delete_user_record(db, member_id)
                    removed_from_db = True
                db.commit()
                email_status = send_group_removal_email(
                    target_member["name"],
                    target_member["email"],
                    owned_group["name"],
                    g.user["name"],
                    g.user["email"],
                )
                if removed_from_db:
                    flash(tr("Member removed from the group and deleted from the database."), "success")
                else:
                    flash(tr("Member removed from the group. Account kept in the database to preserve history."), "success")
                if email_status == "sent":
                    flash(tr("Removal email sent."), "success")
                elif email_status == "logged":
                    flash(tr("Removal email saved to the local outbox."), "info")
                return redirect(url_for("system_data"))
            if action == "delete_orphan_account":
                user_id = int(request.form["user_id"])
                target_user = db.execute(
                    """
                    SELECT id, name, email
                    FROM users
                    WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if target_user is None:
                    flash(tr("Account not found."), "danger")
                    return redirect(url_for("system_data"))
                active_memberships = db.execute(
                    "SELECT COUNT(*) AS total FROM memberships WHERE user_id = ? AND is_active = 1",
                    (user_id,),
                ).fetchone()["total"]
                if active_memberships > 0:
                    flash(tr("This account still belongs to an active group."), "danger")
                    return redirect(url_for("system_data"))
                if not can_delete_user_fully(db, user_id):
                    flash(tr("This account cannot be deleted because it has historical records."), "danger")
                    return redirect(url_for("system_data"))
                email_status = send_account_deletion_email(
                    target_user["name"],
                    target_user["email"],
                    g.user["name"],
                    g.user["email"],
                )
                delete_user_record(db, user_id)
                db.commit()
                flash(tr("Orphan account deleted from the database."), "success")
                if email_status == "sent":
                    flash(tr("Removal email sent."), "success")
                elif email_status == "logged":
                    flash(tr("Removal email saved to the local outbox."), "info")
                return redirect(url_for("system_data"))

            uploaded = request.files.get("snapshot_file")
            if uploaded and uploaded.filename:
                payload = json.load(uploaded.stream)
                import_data_snapshot(db, payload)
                flash(tr("Snapshot imported."), "success")
                return redirect(url_for("system_data"))
            flash(tr("Choose a snapshot JSON file to import."), "danger")
        snapshot = export_data_snapshot(db)
        lookup_email = request.args.get("email", "").strip().lower()
        lookup_user = None
        lookup_memberships = []
        if lookup_email:
            lookup_user = db.execute(
                "SELECT id, name, email, created_at FROM users WHERE lower(email) = ?",
                (lookup_email,),
            ).fetchone()
            if lookup_user:
                lookup_memberships = db.execute(
                    """
                    SELECT g.name, g.slug, m.role, m.is_active, m.created_at
                    FROM memberships m
                    JOIN groups_workspace g ON g.id = m.group_id
                    WHERE m.user_id = ?
                    ORDER BY lower(g.name)
                    """,
                    (lookup_user["id"],),
                ).fetchall()
        recent_signups = db.execute(
            """
            SELECT u.id, u.name, u.email, u.created_at
            FROM users u
            ORDER BY u.created_at DESC, u.id DESC
            LIMIT 20
            """
        ).fetchall()
        groups = db.execute(
            """
            SELECT g.id, g.name, g.slug, g.created_at, owner.name AS owner_name
            FROM groups_workspace g
            LEFT JOIN users owner ON owner.id = g.created_by
            ORDER BY lower(g.name)
            """
        ).fetchall()
        totals = {
            "users": db.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"],
            "groups": db.execute("SELECT COUNT(*) AS total FROM groups_workspace").fetchone()["total"],
            "memberships": db.execute("SELECT COUNT(*) AS total FROM memberships WHERE is_active = 1").fetchone()["total"],
            "matches": db.execute("SELECT COUNT(*) AS total FROM matches WHERE deleted_at IS NULL").fetchone()["total"],
        }
        owned_groups = db.execute(
            """
            SELECT g.id, g.name, g.slug
            FROM groups_workspace g
            JOIN memberships m ON m.group_id = g.id
            WHERE m.user_id = ? AND m.role = 'owner' AND m.is_active = 1
            ORDER BY lower(g.name)
            """,
            (g.user["id"],),
        ).fetchall()
        groups_with_members = []
        for group_row in owned_groups:
            members = db.execute(
                """
                SELECT u.id, u.name, u.email, m.role, m.created_at
                FROM memberships m
                JOIN users u ON u.id = m.user_id
                WHERE m.group_id = ? AND m.is_active = 1
                ORDER BY CASE WHEN m.role = 'owner' THEN 0 ELSE 1 END, lower(u.name)
                """,
                (group_row["id"],),
            ).fetchall()
            groups_with_members.append({"group": group_row, "members": members})
        orphan_rows = db.execute(
            """
            SELECT u.id, u.name, u.email, u.created_at
            FROM users u
            LEFT JOIN memberships m ON m.user_id = u.id AND m.is_active = 1
            WHERE m.id IS NULL
            ORDER BY u.created_at DESC, lower(u.name)
            """
        ).fetchall()
        orphan_accounts = [
            {
                "id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "created_at": row["created_at"],
                "can_delete": can_delete_user_fully(db, row["id"]),
            }
            for row in orphan_rows
        ]
        achievement_unlock_counts = {
            row["achievement_key"]: row["total"]
            for row in db.execute(
                """
                SELECT achievement_key, COUNT(*) AS total
                FROM user_achievements
                GROUP BY achievement_key
                """
            ).fetchall()
        }
        achievement_catalog = [
            {
                **item,
                "unlock_count": achievement_unlock_counts.get(item["key"], 0),
            }
            for item in ACHIEVEMENT_DEFINITIONS
        ]
        db_path = Path(app.config["DATABASE"]).resolve()
        snapshot_dir = Path(app.config["SNAPSHOT_DIR"]).resolve()
        return render_template(
            "system_data.html",
            snapshot=snapshot,
            db_path=str(db_path),
            snapshot_dir=str(snapshot_dir),
            recent_signups=recent_signups,
            groups=groups,
            totals=totals,
            lookup_email=lookup_email,
            lookup_user=lookup_user,
            lookup_memberships=lookup_memberships,
            groups_with_members=groups_with_members,
            orphan_accounts=orphan_accounts,
            achievement_catalog=achievement_catalog,
        )

    @app.route("/system/data/export.json")
    @login_required
    def export_system_data():
        try:
            system_data_admin_required()
        except PermissionError:
            flash(tr("Only group owners or admins can download app data."), "danger")
            return redirect(url_for("index"))
        payload = json.dumps(export_data_snapshot(get_db()), indent=2, sort_keys=True)
        return Response(
            payload,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=lunchbreak-elo-data.json"},
        )

    @app.route("/system/network")
    @login_required
    def system_network():
        manageable_groups = get_db().execute(
            """
            SELECT g.*
            FROM groups_workspace g
            JOIN memberships m ON m.group_id = g.id
            WHERE m.user_id = ? AND m.is_active = 1 AND m.role IN ('owner', 'admin')
            ORDER BY lower(g.name)
            """,
            (g.user["id"],),
        ).fetchall()
        group_invites = [
            {"group": group_row, "invite_urls": invite_urls_for_group(group_row)}
            for group_row in manageable_groups
        ]
        return render_template("system_network.html", access_urls=get_host_access_urls(), group_invites=group_invites)

    @app.route("/system/diagnostics")
    @login_required
    def system_diagnostics():
        return redirect(url_for("system_data", **request.args))

    @app.route("/groups/create", methods=("GET", "POST"))
    @login_required
    def create_group():
        if request.method == "POST":
            name = request.form["name"].strip()
            slug = request.form["slug"].strip().lower()
            description = request.form["description"].strip()
            company_domain = request.form["company_domain"].strip().lower()
            starting_rating = int(request.form.get("starting_rating", 1200))
            default_k_factor = int(request.form.get("default_k_factor", 24))
            invite_code = secrets.token_hex(4)
            db = get_db()
            error = None

            if not name:
                error = "Group name is required."
            elif not slug or not slug.replace("-", "").isalnum():
                error = "Slug can contain letters, numbers, and hyphens."
            elif db.execute("SELECT id FROM groups_workspace WHERE slug = ?", (slug,)).fetchone():
                error = "Slug is already in use."
            elif starting_rating < 100 or default_k_factor < 4:
                error = "Starting rating and K-factor should be sensible values."

            if error is None:
                group_id = db.insert_and_get_id(
                    """
                    INSERT INTO groups_workspace
                    (name, slug, description, company_domain, invite_code, starting_rating, default_k_factor, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        slug,
                        description,
                        company_domain or None,
                        invite_code,
                        starting_rating,
                        default_k_factor,
                        g.user["id"],
                    ),
                )
                db.execute(
                    "INSERT INTO memberships (group_id, user_id, role) VALUES (?, ?, 'owner')",
                    (group_id, g.user["id"]),
                )
                db.execute(
                    """
                    INSERT INTO seasons (group_id, name, start_date, is_active, reset_ratings)
                    VALUES (?, ?, ?, 1, 0)
                    """,
                    (group_id, "Launch Season", datetime.now().strftime("%Y-%m-%d")),
                )
                db.commit()
                flash(tr("Group created. Share the invite code with your colleagues."), "success")
                return redirect(url_for("group_dashboard", slug=slug))

            flash(error, "danger")

        return render_template("groups/create.html")

    @app.route("/groups/join", methods=("GET", "POST"))
    @login_required
    def join_group():
        prefilled_slug = request.args.get("slug", "").strip().lower()
        prefilled_code = request.args.get("invite_code", "").strip()
        if prefilled_slug and prefilled_code:
            session["pending_invite"] = {"slug": prefilled_slug, "invite_code": prefilled_code}
        if request.method == "POST":
            slug = request.form["slug"].strip().lower()
            invite_code = request.form["invite_code"].strip()
            db = get_db()
            group = db.execute(
                "SELECT * FROM groups_workspace WHERE slug = ? AND invite_code = ?",
                (slug, invite_code),
            ).fetchone()
            error = None

            if group is None:
                error = "Group or invite code not found."
            elif group["company_domain"] and not g.user["email"].endswith(f"@{group['company_domain']}"):
                error = f"Only @{group['company_domain']} accounts can join this group."
            elif db.execute(
                "SELECT id FROM memberships WHERE group_id = ? AND user_id = ?",
                (group["id"], g.user["id"]),
            ).fetchone():
                error = "You are already in this group."

            if error is None:
                db.execute(
                    "INSERT INTO memberships (group_id, user_id, role) VALUES (?, ?, 'member')",
                    (group["id"], g.user["id"]),
                )
                db.commit()
                session.pop("pending_invite", None)
                flash(tr("You joined the group."), "success")
                return redirect(url_for("group_dashboard", slug=group["slug"]))

            flash(error, "danger")

        return render_template(
            "groups/join.html",
            prefilled_slug=prefilled_slug,
            prefilled_code=prefilled_code,
            pending_invite=session.get("pending_invite"),
        )

    @app.route("/invite/<slug>/<invite_code>")
    def invite_link(slug: str, invite_code: str):
        session["pending_invite"] = {"slug": slug, "invite_code": invite_code}
        group = get_db().execute(
            "SELECT id, name, slug, invite_code, description FROM groups_workspace WHERE slug = ? AND invite_code = ?",
            (slug, invite_code),
        ).fetchone()
        if g.user is None:
            return render_template("invite_landing.html", group=group, invite_slug=slug, invite_code=invite_code)
        if group and get_db().execute(
            "SELECT 1 FROM memberships WHERE group_id = ? AND user_id = ? AND is_active = 1",
            (group["id"], g.user["id"]),
        ).fetchone():
            flash(tr("You are already in this group."), "success")
            return redirect(url_for("group_dashboard", slug=slug))
        return redirect(url_for("join_group", slug=slug, invite_code=invite_code))

    @app.route("/groups/<slug>")
    @login_required
    def group_dashboard(slug: str):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        db = get_db()
        leaderboard = get_group_leaderboard(db, group["id"])
        braccio_leaderboard = get_ladder_leaderboard(db, group["id"], ladder_type="braccio_mente")
        recent_matches = get_recent_matches(db, group["id"])
        members = get_group_members(db, group["id"])
        active_season = get_active_season(db, group["id"])
        suggestions = get_match_suggestions(db, group["id"], user_id=g.user["id"])
        hall_of_fame = get_hall_of_fame(db, group["id"])
        belt = get_belt_history(db, group["id"])
        trends = get_player_trends(db, group["id"])
        records = get_group_records(db, group["id"])
        nudges = get_activity_nudges(db, group["id"])
        achievements = get_achievements(db, group["id"])
        missions = get_weekly_missions(db, group["id"], g.user["id"])
        team_standings = get_team_standings(db, group["id"])
        open_challenges = db.execute(
            """
            SELECT c.*, challenger.name AS challenger_name, challenged.name AS challenged_name
            FROM challenges c
            JOIN users challenger ON challenger.id = c.challenger_user_id
            JOIN users challenged ON challenged.id = c.challenged_user_id
            WHERE c.group_id = ? AND c.status IN ('open', 'accepted')
            ORDER BY c.created_at DESC
            LIMIT 6
            """,
            (group["id"],),
        ).fetchall()
        coffee_balance = db.execute(
            """
            SELECT
                SUM(CASE WHEN creditor_user_id = ? AND is_settled = 0 THEN amount ELSE 0 END) AS owed_to_me,
                SUM(CASE WHEN debtor_user_id = ? AND is_settled = 0 THEN amount ELSE 0 END) AS i_owe
            FROM coffee_ledger
            WHERE group_id = ?
            """,
            (g.user["id"], g.user["id"], group["id"]),
        ).fetchone()
        stats = db.execute(
            """
            SELECT
                COUNT(*) AS total_matches,
                SUM(CASE WHEN game_type = 'standard' THEN 1 ELSE 0 END) AS standard_matches,
                SUM(CASE WHEN game_type = 'one_arm_one_brain' THEN 1 ELSE 0 END) AS braccio_matches,
                SUM(CASE WHEN result = 'draw' THEN 1 ELSE 0 END) AS draws
            FROM matches
            WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
            """,
            (group["id"],),
        ).fetchone()
        sparklines = get_rating_sparklines(db, group["id"], "standard")
        braccio_sparklines = get_rating_sparklines(db, group["id"], "braccio_mente")
        pending_count = db.execute(
            "SELECT COUNT(*) AS total FROM matches WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'pending'",
            (group["id"],),
        ).fetchone()["total"]

        return render_template(
            "groups/dashboard.html",
            group=group,
            leaderboard=leaderboard,
            braccio_leaderboard=braccio_leaderboard,
            sparklines=sparklines,
            braccio_sparklines=braccio_sparklines,
            pending_count=pending_count,
            recent_matches=recent_matches,
            members=members,
            active_season=active_season,
            suggestions=suggestions,
            hall_of_fame=hall_of_fame,
            belt=belt,
            trends=trends,
            records=records,
            nudges=nudges,
            achievements=achievements,
            missions=missions,
            team_standings=team_standings,
            open_challenges=open_challenges,
            coffee_balance=coffee_balance,
            stats=stats,
            result_label=result_label,
        )

    @app.route("/groups/<slug>/members", methods=("GET", "POST"))
    @login_required
    def group_members(slug: str):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash("You do not have access to this group.", "danger")
            return redirect(url_for("index"))

        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "role")
            member_id = int(request.form["member_id"])
            if action == "remove":
                if group["membership_role"] != "owner":
                    flash(tr("Only group owners can remove members."), "danger")
                    return redirect(url_for("group_members", slug=slug))
                db.execute(
                    """
                    UPDATE memberships
                    SET is_active = 0, team_id = NULL
                    WHERE group_id = ? AND user_id = ? AND role != 'owner'
                    """,
                    (group["id"], member_id),
                )
                db.commit()
                flash(tr("Member removed from the group."), "success")
                return redirect(url_for("group_members", slug=slug))

            if action == "mark_former_employee":
                try:
                    group_admin_required(group)
                except PermissionError:
                    flash(tr("Only admins can update member accounts."), "danger")
                    return redirect(url_for("group_members", slug=slug))
                if member_id == g.user["id"]:
                    flash(tr("You cannot mark your own account as former employee."), "danger")
                    return redirect(url_for("group_members", slug=slug))
                target_member = db.execute(
                    """
                    SELECT u.id, m.role
                    FROM memberships m
                    JOIN users u ON u.id = m.user_id
                    WHERE m.group_id = ? AND m.user_id = ? AND m.is_active = 1
                    """,
                    (group["id"], member_id),
                ).fetchone()
                if target_member is None:
                    flash(tr("Member not found in this group."), "danger")
                    return redirect(url_for("group_members", slug=slug))
                if target_member["role"] == "owner":
                    flash(tr("Owners cannot be marked as former employees from this page."), "danger")
                    return redirect(url_for("group_members", slug=slug))
                db.execute(
                    """
                    UPDATE users
                    SET is_former_employee = 1,
                        selected_title = ?,
                        avatar_icon = ?,
                        avatar_color = '#5e6973',
                        tagline = COALESCE(NULLIF(tagline, ''), ?)
                    WHERE id = ?
                    """,
                    (FORMER_EMPLOYEE_TITLE, FORMER_EMPLOYEE_AVATAR_ICON, FORMER_EMPLOYEE_TITLE, member_id),
                )
                db.commit()
                flash(tr("Member marked as former employee."), "success")
                return redirect(url_for("group_members", slug=slug))

            try:
                group_admin_required(group)
            except PermissionError:
                flash(tr("Only admins can update member roles."), "danger")
                return redirect(url_for("group_members", slug=slug))

            role = request.form["role"]
            if role in {"member", "admin"}:
                db.execute(
                    "UPDATE memberships SET role = ? WHERE group_id = ? AND user_id = ? AND role != 'owner'",
                    (role, group["id"], member_id),
                )
                db.commit()
                flash(tr("Member role updated."), "success")
            return redirect(url_for("group_members", slug=slug))

        members = get_group_members(db, group["id"])
        leaderboard = get_group_leaderboard(db, group["id"])
        stats_by_user = {row["id"]: row for row in leaderboard}
        return render_template(
            "groups/members.html",
            group=group,
            members=members,
            stats_by_user=stats_by_user,
        )

    @app.get("/groups/<slug>/members/<int:user_id>")
    @login_required
    def group_member_profile(slug: str, user_id: int):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash("You do not have access to this group.", "danger")
            return redirect(url_for("index"))

        db = get_db()
        profile = get_group_member_profile(db, group["id"], user_id)
        if profile is None:
            flash(tr("Member not found in this group."), "danger")
            return redirect(url_for("group_members", slug=slug))

        leaderboard = get_group_leaderboard(db, group["id"])
        stats_by_user = {row["id"]: row for row in leaderboard}
        braccio_stats_by_user = {row["id"]: row for row in get_ladder_leaderboard(db, group["id"], "braccio_mente")}
        unlocked_achievements = get_unlocked_achievements_for_user(db, user_id, group["id"])
        unlocked_titles, unlocked_reward_icons = get_unlocked_reward_choices(db, user_id, group["id"])
        profile_series = build_member_series(db, group["id"], user_id)
        color_row = next((row for row in get_color_stats(db, group["id"]) if row["id"] == user_id), None)
        trend = next((row for row in get_player_trends(db, group["id"]) if row["user_id"] == user_id), None)
        rank = next((index for index, row in enumerate(leaderboard, start=1) if row["id"] == user_id), None)
        return render_template(
            "groups/member_profile.html",
            group=group,
            profile=profile,
            stats=stats_by_user.get(user_id),
            braccio_stats=braccio_stats_by_user.get(user_id),
            profile_series=profile_series,
            color_row=color_row,
            trend=trend,
            rank=rank,
            member_count=len(leaderboard),
            unlocked_achievements=unlocked_achievements,
            unlocked_titles=unlocked_titles,
            unlocked_reward_icons=unlocked_reward_icons,
        )

    @app.post("/notifications/signups/read")
    @login_required
    def mark_signup_notifications_read():
        if not user_can_manage_app_data(g.user["id"]):
            flash(tr("Only group owners or admins can manage app data."), "danger")
            return redirect(url_for("index"))
        db = get_db()
        unread = get_signup_notifications(db, g.user["id"])
        for item in unread:
            db.execute(
                """
                INSERT OR IGNORE INTO signup_notification_reads (notification_id, user_id)
                VALUES (?, ?)
                """,
                (item["id"], g.user["id"]),
            )
        db.commit()
        flash(tr("Signup notifications marked as read."), "success")
        return redirect(url_for("index"))

    @app.post("/notifications/achievements/read")
    @login_required
    def mark_achievement_notifications_read():
        next_url = request.form.get("next_url", "").strip()
        if not next_url.startswith("/"):
            next_url = url_for("index")
        db = get_db()
        db.execute("UPDATE user_achievements SET is_seen = 1 WHERE user_id = ? AND is_seen = 0", (g.user["id"],))
        db.commit()
        return redirect(next_url)

    @app.route("/groups/<slug>/matches", methods=("GET", "POST"))
    @login_required
    def group_matches(slug: str):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        db = get_db()
        if request.method == "POST":
            payload = parse_match_form(request.form)
            guest_error, invite_statuses = resolve_match_payload_players(db, group, payload, created_by_user_id=g.user["id"])
            if guest_error is not None:
                flash(tr(guest_error), "danger")
                return redirect(url_for("group_matches", slug=slug))
            error = validate_match_payload(db, group["id"], payload)
            if error is None:
                match_id, confirmation_status = save_match_record(db, group, payload, reporter_id=g.user["id"])
                completed_challenge_id = complete_challenge_for_match(db, group["id"], match_id, payload)
                sync_match_coffee_debts(db, group["id"], match_id)
                db.commit()
                recalculate_group_ratings(db, group["id"])
                evaluate_group_achievements(db, group["id"], source_match_id=match_id)
                db.commit()
                flash(tr("Match saved.") + (" " + tr("Ratings were recalculated.") if confirmation_status == "confirmed" else " " + tr("Waiting for confirmation.")), "success")
                if completed_challenge_id:
                    flash(tr("Linked challenge completed."), "success")
                if "sent" in invite_statuses:
                    flash(tr("Guest invite email sent."), "success")
                elif "logged" in invite_statuses:
                    flash(tr("Guest invite email saved to the local outbox."), "success")
                return redirect(url_for("group_matches", slug=slug))

            flash(error, "danger")

        season_filter = request.args.get("season", "").strip()
        player_filter = request.args.get("player", "").strip()
        challenge_prefill = request.args.get("challenge", "").strip()
        query = """
            SELECT m.*, w.name AS white_name, wp.name AS white_partner_name,
                   b.name AS black_name, bp.name AS black_partner_name,
                   r.name AS reporter_name, s.name AS season_name
            FROM matches m
            JOIN users w ON w.id = m.white_user_id
            LEFT JOIN users wp ON wp.id = m.white_partner_user_id
            JOIN users b ON b.id = m.black_user_id
            LEFT JOIN users bp ON bp.id = m.black_partner_user_id
            JOIN users r ON r.id = m.reported_by
            LEFT JOIN seasons s ON s.id = m.season_id
            WHERE m.group_id = ? AND m.deleted_at IS NULL
        """
        params = [group["id"]]
        if season_filter.isdigit():
            query += " AND m.season_id = ?"
            params.append(int(season_filter))
        if player_filter.isdigit():
            query += " AND (m.white_user_id = ? OR m.black_user_id = ? OR m.white_partner_user_id = ? OR m.black_partner_user_id = ?)"
            params.extend([int(player_filter), int(player_filter), int(player_filter), int(player_filter)])
        query += " ORDER BY m.played_at DESC, m.id DESC"
        matches = db.execute(query, tuple(params)).fetchall()
        seasons = db.execute(
            "SELECT * FROM seasons WHERE group_id = ? ORDER BY start_date DESC, id DESC",
            (group["id"],),
        ).fetchall()
        active_season = get_active_season(db, group["id"])
        challenges = db.execute(
            """
            SELECT c.*,
                   challenger.name AS challenger_name,
                   challenger.avatar_color AS challenger_avatar_color,
                   challenger.avatar_icon AS challenger_avatar_icon,
                   challenged.name AS challenged_name,
                   challenged.avatar_color AS challenged_avatar_color,
                   challenged.avatar_icon AS challenged_avatar_icon
            FROM challenges c
            JOIN users challenger ON challenger.id = c.challenger_user_id
            JOIN users challenged ON challenged.id = c.challenged_user_id
            WHERE c.group_id = ? AND c.status IN ('open', 'accepted')
            ORDER BY c.created_at DESC
            """,
            (group["id"],),
        ).fetchall()
        selected_challenge = None
        if challenge_prefill.isdigit():
            selected_challenge = db.execute(
                """
                SELECT *
                FROM challenges
                WHERE id = ? AND group_id = ? AND status IN ('open', 'accepted')
                """,
                (int(challenge_prefill), group["id"]),
            ).fetchone()
        members = get_group_members(db, group["id"])
        match_members = get_match_eligible_group_members(db, group["id"])
        return render_template(
            "groups/matches.html",
            group=group,
            matches=matches,
            seasons=seasons,
            challenges=challenges,
            selected_challenge=selected_challenge,
            members=members,
            match_members=match_members,
            active_season=active_season,
            default_played_at=datetime.now().strftime("%Y-%m-%d"),
            time_control_presets=TIME_CONTROL_PRESETS,
            result_label=result_label,
            season_filter=season_filter,
            player_filter=player_filter,
        )

    @app.route("/groups/<slug>/confirmations", methods=("GET", "POST"))
    @login_required
    def group_confirmations(slug: str):
        group = group_membership_or_404(slug)
        db = get_db()
        if request.method == "POST":
            match_id = int(request.form["match_id"])
            match_row = db.execute(
                "SELECT * FROM matches WHERE id = ? AND group_id = ? AND deleted_at IS NULL",
                (match_id, group["id"]),
            ).fetchone()
            if match_row and can_confirm_match(group, match_row):
                db.execute(
                    """
                    UPDATE matches
                    SET confirmation_status = 'confirmed', confirmed_by = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (g.user["id"], match_id),
                )
                sync_match_coffee_debts(db, group["id"], match_id)
                db.commit()
                recalculate_group_ratings(db, group["id"])
                evaluate_group_achievements(db, group["id"], source_match_id=match_id)
                db.commit()
                flash(tr("Match confirmed."), "success")
            else:
                flash(tr("Only an opponent or an admin can confirm this match."), "danger")
            return redirect(url_for("group_confirmations", slug=slug))

        pending_matches = db.execute(
            """
            SELECT m.*, w.name AS white_name, wp.name AS white_partner_name,
                   b.name AS black_name, bp.name AS black_partner_name, r.name AS reporter_name
            FROM matches m
            JOIN users w ON w.id = m.white_user_id
            LEFT JOIN users wp ON wp.id = m.white_partner_user_id
            JOIN users b ON b.id = m.black_user_id
            LEFT JOIN users bp ON bp.id = m.black_partner_user_id
            JOIN users r ON r.id = m.reported_by
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'pending'
            ORDER BY m.played_at DESC, m.id DESC
            """,
            (group["id"],),
        ).fetchall()
        return render_template(
            "groups/confirmations.html",
            group=group,
            pending_matches=pending_matches,
            result_label=result_label,
            can_confirm_match=can_confirm_match,
        )

    @app.route("/groups/<slug>/matches/<int:match_id>/edit", methods=("GET", "POST"))
    @login_required
    def edit_match(slug: str, match_id: int):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        db = get_db()
        match_row = db.execute(
            "SELECT * FROM matches WHERE id = ? AND group_id = ? AND deleted_at IS NULL",
            (match_id, group["id"]),
        ).fetchone()
        if match_row is None:
            flash(tr("Match not found."), "danger")
            return redirect(url_for("group_matches", slug=slug))
        if not can_edit_match(group, match_row):
            flash(tr("You cannot edit this match."), "danger")
            return redirect(url_for("group_matches", slug=slug))

        if request.method == "POST":
            payload = parse_match_form(request.form)
            error = validate_match_payload(db, group["id"], payload, allow_former_employees=True)
            if error == "All selected players must be members of the group." or error == "Each player can only appear once in a match.":
                flash(tr("Choose distinct group members for the match."), "danger")
                return redirect(url_for("edit_match", slug=slug, match_id=match_id))
            if error is not None:
                flash(tr(error), "danger")
                return redirect(url_for("edit_match", slug=slug, match_id=match_id))
            save_match_record(db, group, payload, reporter_id=match_row["reported_by"], match_id=match_id)
            sync_match_coffee_debts(db, group["id"], match_id)
            db.commit()
            recalculate_group_ratings(db, group["id"])
            evaluate_group_achievements(db, group["id"], source_match_id=match_id)
            db.commit()
            flash(tr("Match updated."), "success")
            return redirect(url_for("group_matches", slug=slug))

        seasons = db.execute(
            "SELECT * FROM seasons WHERE group_id = ? ORDER BY start_date DESC, id DESC",
            (group["id"],),
        ).fetchall()
        members = get_group_members(db, group["id"])
        return render_template(
            "groups/edit_match.html",
            group=group,
            match=match_row,
            seasons=seasons,
            members=members,
            time_control_presets=TIME_CONTROL_PRESETS,
        )

    @app.post("/groups/<slug>/matches/<int:match_id>/delete")
    @login_required
    def delete_match(slug: str, match_id: int):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        db = get_db()
        match_row = db.execute(
            "SELECT * FROM matches WHERE id = ? AND group_id = ? AND deleted_at IS NULL",
            (match_id, group["id"]),
        ).fetchone()
        if match_row is None:
            flash(tr("Match not found."), "danger")
        elif not can_edit_match(group, match_row):
            flash(tr("You cannot delete this match."), "danger")
        else:
            db.execute("UPDATE matches SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (match_id,))
            sync_match_coffee_debts(db, group["id"], match_id)
            db.commit()
            recalculate_group_ratings(db, group["id"])
            flash(tr("Match deleted."), "success")

        return redirect(url_for("group_matches", slug=slug))

    @app.route("/groups/<slug>/seasons", methods=("GET", "POST"))
    @login_required
    def group_seasons(slug: str):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        db = get_db()
        if request.method == "POST":
            try:
                group_admin_required(group)
            except PermissionError:
                flash(tr("Only admins can manage seasons."), "danger")
                return redirect(url_for("group_seasons", slug=slug))

            action = request.form["action"]
            if action == "create":
                name = request.form["name"].strip()
                start_date = request.form["start_date"]
                end_date = request.form.get("end_date", "").strip() or None
                reset_ratings = 1 if request.form.get("reset_ratings") else 0
                if name and start_date:
                    db.execute("UPDATE seasons SET is_active = 0 WHERE group_id = ?", (group["id"],))
                    db.execute(
                        """
                        INSERT INTO seasons (group_id, name, start_date, end_date, is_active, reset_ratings)
                        VALUES (?, ?, ?, ?, 1, ?)
                        """,
                        (group["id"], name, start_date, end_date, reset_ratings),
                    )
                    db.commit()
                    recalculate_group_ratings(db, group["id"])
                    evaluate_group_achievements(db, group["id"])
                    db.commit()
                    flash(tr("Season created and activated."), "success")
            elif action == "activate":
                season_id = int(request.form["season_id"])
                db.execute("UPDATE seasons SET is_active = 0 WHERE group_id = ?", (group["id"],))
                db.execute("UPDATE seasons SET is_active = 1 WHERE id = ? AND group_id = ?", (season_id, group["id"]))
                db.commit()
                evaluate_group_achievements(db, group["id"])
                db.commit()
                flash(tr("Active season changed."), "success")

            return redirect(url_for("group_seasons", slug=slug))

        seasons = db.execute(
            "SELECT * FROM seasons WHERE group_id = ? ORDER BY start_date DESC, id DESC",
            (group["id"],),
        ).fetchall()
        coffee_leaders = {
            row["season_id"]: row
            for row in db.execute(
                """
                WITH ranked_credits AS (
                    SELECT
                        m.season_id,
                        u.name AS player_name,
                        SUM(c.amount) AS credited_coffees,
                        ROW_NUMBER() OVER (
                            PARTITION BY m.season_id
                            ORDER BY SUM(c.amount) DESC, lower(u.name)
                        ) AS rn
                    FROM coffee_ledger c
                    JOIN matches m ON m.id = c.source_match_id
                    JOIN users u ON u.id = c.creditor_user_id
                    WHERE m.group_id = ? AND m.season_id IS NOT NULL
                    GROUP BY m.season_id, u.id, u.name
                )
                SELECT season_id, player_name, credited_coffees
                FROM ranked_credits
                WHERE rn = 1
                """,
                (group["id"],),
            ).fetchall()
        }
        seasons_view = []
        for season in seasons:
            item = dict(season)
            item["coffee_leader"] = coffee_leaders.get(season["id"])
            seasons_view.append(item)
        return render_template("groups/seasons.html", group=group, seasons=seasons_view)

    @app.route("/groups/<slug>/settings", methods=("GET", "POST"))
    @login_required
    def group_settings(slug: str):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        db = get_db()
        if request.method == "POST":
            try:
                group_admin_required(group)
            except PermissionError:
                flash(tr("Only admins can update settings."), "danger")
                return redirect(url_for("group_settings", slug=slug))

            db.execute(
                """
                UPDATE groups_workspace
                SET description = ?, company_domain = ?, starting_rating = ?, default_k_factor = ?
                WHERE id = ?
                """,
                (
                    request.form["description"].strip(),
                    request.form["company_domain"].strip().lower() or None,
                    int(request.form["starting_rating"]),
                    int(request.form["default_k_factor"]),
                    group["id"],
                ),
            )
            if request.form.get("rotate_invite_code"):
                db.execute(
                    "UPDATE groups_workspace SET invite_code = ? WHERE id = ?",
                    (secrets.token_hex(4), group["id"]),
                )
            db.commit()
            recalculate_group_ratings(db, group["id"])
            flash(tr("Settings updated."), "success")
            return redirect(url_for("group_settings", slug=slug))

        refreshed_group = db.execute("SELECT * FROM groups_workspace WHERE id = ?", (group["id"],)).fetchone()
        return render_template(
            "groups/settings.html",
            group=refreshed_group,
            role=group["membership_role"],
            invite_urls=invite_urls_for_group(refreshed_group),
        )

    @app.route("/groups/<slug>/stats")
    @login_required
    def group_stats(slug: str):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        db = get_db()
        members = get_group_members(db, group["id"])
        match_rows = get_recent_matches(db, group["id"], limit=500)
        ratings = db.execute(
            """
            SELECT rh.*, u.name, m.played_at, m.result, m.white_user_id, m.white_partner_user_id, m.black_user_id, m.black_partner_user_id,
                   m.notes, white.name AS white_name, white_partner.name AS white_partner_name,
                    black.name AS black_name, black_partner.name AS black_partner_name
            FROM rating_history rh
            JOIN matches m ON m.id = rh.match_id
            JOIN users u ON u.id = rh.user_id
            JOIN users white ON white.id = m.white_user_id
            LEFT JOIN users white_partner ON white_partner.id = m.white_partner_user_id
            JOIN users black ON black.id = m.black_user_id
            LEFT JOIN users black_partner ON black_partner.id = m.black_partner_user_id
            WHERE rh.group_id = ? AND rh.ladder_type = 'standard'
              AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = 'standard'
            ORDER BY m.played_at ASC, m.id ASC, rh.id ASC
            """,
            (group["id"],),
        ).fetchall()
        braccio_ratings = db.execute(
            """
            SELECT rh.*, u.name, m.played_at, m.result, m.white_user_id, m.white_partner_user_id, m.black_user_id, m.black_partner_user_id,
                   m.notes, white.name AS white_name, white_partner.name AS white_partner_name,
                   black.name AS black_name, black_partner.name AS black_partner_name
            FROM rating_history rh
            JOIN matches m ON m.id = rh.match_id
            JOIN users u ON u.id = rh.user_id
            JOIN users white ON white.id = m.white_user_id
            LEFT JOIN users white_partner ON white_partner.id = m.white_partner_user_id
            JOIN users black ON black.id = m.black_user_id
            LEFT JOIN users black_partner ON black_partner.id = m.black_partner_user_id
            WHERE rh.group_id = ? AND rh.ladder_type = 'braccio_mente'
              AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = 'one_arm_one_brain'
            ORDER BY m.played_at ASC, m.id ASC, rh.id ASC
            """,
            (group["id"],),
        ).fetchall()

        rivalry_rows = db.execute(
            """
            SELECT
                MIN(u1.name, u2.name) AS player_one,
                MAX(u1.name, u2.name) AS player_two,
                COUNT(*) AS games,
                SUM(CASE WHEN m.result = 'draw' THEN 1 ELSE 0 END) AS draws
            FROM matches m
            JOIN users u1 ON u1.id = m.white_user_id
            JOIN users u2 ON u2.id = m.black_user_id
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
            GROUP BY player_one, player_two
            HAVING COUNT(*) > 0
            ORDER BY games DESC, player_one
            LIMIT 10
            """,
            (group["id"],),
        ).fetchall()

        busiest_day = db.execute(
            """
            SELECT played_at, COUNT(*) AS total
            FROM matches
            WHERE group_id = ? AND deleted_at IS NULL AND confirmation_status = 'confirmed'
            GROUP BY played_at
            ORDER BY total DESC, played_at DESC
            LIMIT 1
            """,
            (group["id"],),
        ).fetchone()

        chart_data = build_rating_series(ratings)
        braccio_chart_data = build_rating_series(braccio_ratings)
        monthly_activity = get_monthly_activity(db, group["id"])

        average_games = round(mean([row["games"] for row in get_group_leaderboard(db, group["id"])]) if members else 0, 1)
        leaderboard = get_group_leaderboard(db, group["id"])
        braccio_average_games = round(mean([row["games"] for row in get_ladder_leaderboard(db, group["id"], "braccio_mente")]) if members else 0, 1)
        color_stats = get_color_stats(db, group["id"])
        opening_stats = get_opening_stats(db, group["id"])
        belt = get_belt_history(db, group["id"])
        trends = get_player_trends(db, group["id"])
        records = get_group_records(db, group["id"])
        nudges = get_activity_nudges(db, group["id"])
        achievements = get_achievements(db, group["id"])
        team_standings = get_team_standings(db, group["id"])

        return render_template(
            "groups/stats.html",
            group=group,
            members=members,
            match_rows=match_rows,
            rivalry_rows=rivalry_rows,
            busiest_day=busiest_day,
            chart_data=chart_data,
            braccio_chart_data=braccio_chart_data,
            monthly_activity=monthly_activity,
            leaderboard=leaderboard,
            average_games=average_games,
            braccio_average_games=braccio_average_games,
            braccio_leaderboard=get_ladder_leaderboard(db, group["id"], "braccio_mente"),
            color_stats=color_stats,
            opening_stats=opening_stats,
            belt=belt,
            trends=trends,
            records=records,
            nudges=nudges,
            achievements=achievements,
            team_standings=team_standings,
        )

    @app.route("/groups/<slug>/suggestions")
    @login_required
    def group_suggestions(slug: str):
        group = group_membership_or_404(slug)
        suggestions = get_match_suggestions(get_db(), group["id"], limit=12, user_id=g.user["id"])
        return render_template("groups/suggestions.html", group=group, suggestions=suggestions)

    @app.route("/groups/<slug>/head-to-head")
    @login_required
    def group_head_to_head(slug: str):
        group = group_membership_or_404(slug)
        db = get_db()
        members = get_group_members(db, group["id"])
        left_id = request.args.get("left", "").strip()
        right_id = request.args.get("right", "").strip()
        comparison = None
        summary = None
        selected_players = None
        if left_id.isdigit() and right_id.isdigit() and left_id != right_id:
            selected_players = {
                int(left_id): next((member for member in members if member["id"] == int(left_id)), None),
                int(right_id): next((member for member in members if member["id"] == int(right_id)), None),
            }
            comparison = db.execute(
                """
                SELECT m.*, w.name AS white_name, b.name AS black_name
                FROM matches m
                JOIN users w ON w.id = m.white_user_id
                JOIN users b ON b.id = m.black_user_id
                WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = 'standard'
                  AND ((m.white_user_id = ? AND m.black_user_id = ?) OR (m.white_user_id = ? AND m.black_user_id = ?))
                ORDER BY m.played_at DESC, m.id DESC
                """,
                (group["id"], int(left_id), int(right_id), int(right_id), int(left_id)),
            ).fetchall()
            if selected_players[int(left_id)] and selected_players[int(right_id)]:
                left_wins = 0
                right_wins = 0
                draws = 0
                latest_match = comparison[0] if comparison else None
                for match in comparison:
                    if match["result"] == "draw":
                        draws += 1
                    elif (
                        (match["result"] == "white" and match["white_user_id"] == int(left_id))
                        or (match["result"] == "black" and match["black_user_id"] == int(left_id))
                    ):
                        left_wins += 1
                    else:
                        right_wins += 1
                summary = {
                    "left_player": selected_players[int(left_id)],
                    "right_player": selected_players[int(right_id)],
                    "total_games": len(comparison),
                    "left_wins": left_wins,
                    "right_wins": right_wins,
                    "draws": draws,
                    "latest_match": latest_match,
                }
        return render_template(
            "groups/head_to_head.html",
            group=group,
            members=members,
            comparison=comparison,
            summary=summary,
            selected_players=selected_players,
            left_id=left_id,
            right_id=right_id,
            result_label=result_label,
        )

    @app.route("/groups/<slug>/challenges", methods=("GET", "POST"))
    @login_required
    def group_challenges(slug: str):
        group = group_membership_or_404(slug)
        db = get_db()
        members = get_group_members(db, group["id"])
        member_ids = {member["id"] for member in members}
        if request.method == "POST":
            action = request.form["action"]
            if action == "create":
                challenged_user_id = int(request.form["challenged_user_id"])
                if challenged_user_id in member_ids and challenged_user_id != g.user["id"]:
                    existing_challenge = db.execute(
                        """
                        SELECT id
                        FROM challenges
                        WHERE group_id = ? AND status IN ('open', 'accepted')
                          AND (
                            (challenger_user_id = ? AND challenged_user_id = ?)
                            OR (challenger_user_id = ? AND challenged_user_id = ?)
                          )
                        """,
                        (group["id"], g.user["id"], challenged_user_id, challenged_user_id, g.user["id"]),
                    ).fetchone()
                    if existing_challenge:
                        flash(tr("You already have an active challenge with that opponent."), "warning")
                    else:
                        opponent = db.execute("SELECT * FROM users WHERE id = ?", (challenged_user_id,)).fetchone()
                        message = request.form.get("message", "").strip()
                        if not message:
                            message = default_challenge_message(g.user["name"], opponent["name"])
                        challenge_source = request.form.get("challenge_source", "manual").strip()
                        if challenge_source not in {"manual", "suggestion"}:
                            challenge_source = "manual"
                        db.execute(
                            """
                            INSERT INTO challenges (group_id, challenger_user_id, challenged_user_id, source, message)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (group["id"], g.user["id"], challenged_user_id, challenge_source, message),
                        )
                        email_status = send_challenge_email(group, g.user, opponent, message)
                        evaluate_group_achievements(db, group["id"])
                        db.commit()
                        flash(tr("Challenge sent."), "success")
                        if email_status == "sent":
                            flash(tr("Challenge email sent."), "success")
                        elif email_status == "logged":
                            flash(tr("Challenge email saved to the local outbox."), "success")
            elif action in {"accept", "decline"}:
                status = "accepted" if action == "accept" else "declined"
                db.execute(
                    """
                    UPDATE challenges
                    SET status = ?, responded_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND group_id = ? AND challenged_user_id = ?
                    """,
                    (status, int(request.form["challenge_id"]), group["id"], g.user["id"]),
                )
                db.commit()
                flash(tr("Challenge {status}.", status=tr(status)), "success")
            return redirect(url_for("group_challenges", slug=slug))

        challenges = db.execute(
            """
            SELECT c.*, challenger.name AS challenger_name, challenged.name AS challenged_name
            FROM challenges c
            JOIN users challenger ON challenger.id = c.challenger_user_id
            JOIN users challenged ON challenged.id = c.challenged_user_id
            WHERE c.group_id = ?
            ORDER BY c.created_at DESC, c.id DESC
            """,
            (group["id"],),
        ).fetchall()
        return render_template(
            "groups/challenges.html",
            group=group,
            members=members,
            challenges=challenges,
            challenge_age_label=challenge_age_label,
        )

    @app.route("/groups/<slug>/teams", methods=("GET", "POST"))
    @login_required
    def group_teams(slug: str):
        group = group_membership_or_404(slug)
        db = get_db()
        members = get_group_members(db, group["id"])
        if request.method == "POST":
            action = request.form["action"]
            if action == "create":
                db.execute(
                    "INSERT INTO teams (group_id, name, color) VALUES (?, ?, ?)",
                    (group["id"], request.form["name"].strip(), request.form["color"].strip() or "#247ba0"),
                )
                db.commit()
                flash(tr("Team created."), "success")
            elif action == "assign":
                db.execute(
                    "UPDATE memberships SET team_id = ? WHERE group_id = ? AND user_id = ?",
                    (
                        int(request.form["team_id"]) if request.form.get("team_id") else None,
                        group["id"],
                        int(request.form["member_id"]),
                    ),
                )
                db.commit()
                flash(tr("Team assignment updated."), "success")
            return redirect(url_for("group_teams", slug=slug))

        teams = db.execute("SELECT * FROM teams WHERE group_id = ? ORDER BY lower(name)", (group["id"],)).fetchall()
        assignments = db.execute(
            """
            SELECT m.user_id, m.team_id, t.name AS team_name, t.color
            FROM memberships m
            LEFT JOIN teams t ON t.id = m.team_id
            WHERE m.group_id = ? AND m.is_active = 1
            """,
            (group["id"],),
        ).fetchall()
        assignment_map = {row["user_id"]: row for row in assignments}
        standings = get_team_standings(db, group["id"])
        return render_template(
            "groups/teams.html",
            group=group,
            teams=teams,
            members=members,
            assignment_map=assignment_map,
            standings=standings,
        )

    @app.route("/groups/<slug>/winners")
    @login_required
    def group_winners(slug: str):
        group = group_membership_or_404(slug)
        db = get_db()
        hall_of_fame = get_hall_of_fame(db, group["id"])
        season_summaries = db.execute(
            """
            WITH season_rank AS (
                SELECT s.id AS season_id, s.name AS season_name, s.end_date, s.is_active, u.name AS player_name,
                       rh.rating_after,
                       ROW_NUMBER() OVER (
                            PARTITION BY s.id
                            ORDER BY rh.rating_after DESC, lower(u.name)
                        ) AS rn
                FROM seasons s
                JOIN matches m ON m.season_id = s.id AND m.deleted_at IS NULL AND m.game_type = 'standard'
                JOIN rating_history rh ON rh.match_id = m.id AND rh.season_id = s.id AND rh.ladder_type = 'standard'
                JOIN users u ON u.id = rh.user_id
                WHERE s.group_id = ?
            )
            SELECT s.id AS season_id, s.name AS season_name, s.end_date, s.is_active,
                   season_rank.player_name, season_rank.rating_after
            FROM seasons s
            LEFT JOIN season_rank ON season_rank.season_id = s.id AND season_rank.rn = 1
            WHERE s.group_id = ?
            ORDER BY s.is_active DESC, s.start_date DESC, s.id DESC
            """,
            (group["id"], group["id"]),
        ).fetchall()
        return render_template("groups/winners.html", group=group, hall_of_fame=hall_of_fame, season_summaries=season_summaries)

    @app.route("/groups/<slug>/coffee", methods=("GET", "POST"))
    @login_required
    def group_coffee(slug: str):
        group = group_membership_or_404(slug)
        db = get_db()
        members = get_group_members(db, group["id"])
        member_ids = {member["id"] for member in members}

        if request.method == "POST":
            action = request.form["action"]
            if action == "add":
                debtor_user_id = int(request.form["debtor_user_id"])
                creditor_user_id = int(request.form["creditor_user_id"])
                raw_amount = request.form.get("amount", "").strip()
                unit = request.form.get("unit", "coffee").strip()
                unit_count = max(1, int(request.form.get("unit_count", 1) or 1))
                amount = int(raw_amount) if raw_amount else unit_count * TREAT_UNIT_VALUES.get(unit, 1)
                if debtor_user_id in member_ids and creditor_user_id in member_ids and debtor_user_id != creditor_user_id:
                    db.execute(
                        """
                        INSERT INTO coffee_ledger
                        (group_id, debtor_user_id, creditor_user_id, amount, reason, entry_type, created_by)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            group["id"],
                            debtor_user_id,
                            creditor_user_id,
                            max(1, amount),
                            request.form["reason"].strip(),
                            "manual",
                            g.user["id"],
                        ),
                    )
                    evaluate_group_achievements(db, group["id"], mark_seen=False)
                    db.commit()
                    flash(tr("Coffee debt recorded."), "success")
            elif action == "optimize":
                if group["membership_role"] not in {"owner", "admin"}:
                    flash(tr("Only admins can optimize coffee debts."), "danger")
                    return redirect(url_for("group_coffee", slug=slug))
                if optimize_group_coffee_debts(db, group["id"]):
                    evaluate_group_achievements(db, group["id"], mark_seen=False)
                    db.commit()
                    flash(tr("Coffee debts optimized."), "success")
                else:
                    flash(tr("No coffee debts needed optimization."), "info")
            elif action == "settle":
                ledger_id = int(request.form["ledger_id"])
                db.execute(
                    """
                    UPDATE coffee_ledger
                    SET is_settled = 1, settled_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND group_id = ?
                    """,
                    (ledger_id, group["id"]),
                )
                evaluate_group_achievements(db, group["id"], mark_seen=False)
                db.commit()
                flash(tr("Coffee debt settled."), "success")
            return redirect(url_for("group_coffee", slug=slug))

        open_items = db.execute(
            """
            SELECT c.*,
                   debtor.name AS debtor_name, debtor.avatar_color AS debtor_avatar_color, debtor.avatar_icon AS debtor_avatar_icon,
                   creditor.name AS creditor_name, creditor.avatar_color AS creditor_avatar_color, creditor.avatar_icon AS creditor_avatar_icon
            FROM coffee_ledger c
            JOIN users debtor ON debtor.id = c.debtor_user_id
            JOIN users creditor ON creditor.id = c.creditor_user_id
            WHERE c.group_id = ? AND c.is_settled = 0
            ORDER BY c.created_at DESC
            """,
            (group["id"],),
        ).fetchall()
        balances = db.execute(
            """
            SELECT u.id, u.name, u.avatar_color, u.avatar_icon,
                   SUM(CASE WHEN c.creditor_user_id = u.id AND c.is_settled = 0 THEN c.amount ELSE 0 END) -
                   SUM(CASE WHEN c.debtor_user_id = u.id AND c.is_settled = 0 THEN c.amount ELSE 0 END) AS net_coffees
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            LEFT JOIN coffee_ledger c ON c.group_id = m.group_id
            WHERE m.group_id = ? AND m.is_active = 1
            GROUP BY u.id, u.name
            ORDER BY net_coffees DESC, lower(u.name)
            """,
            (group["id"],),
        ).fetchall()
        open_items = [
            {
                **dict(item),
                "amount_breakdown": treat_breakdown(item["amount"]),
                "amount_label": treat_label(item["amount"]),
            }
            for item in open_items
        ]
        balances = [
            {
                **dict(row),
                "net_coffees": int(row["net_coffees"] or 0),
                "net_breakdown": treat_breakdown(abs(int(row["net_coffees"] or 0))),
                "net_label": treat_label(abs(int(row["net_coffees"] or 0))),
            }
            for row in balances
        ]
        coffee_achievement_icons = get_group_achievement_icons_by_user(db, group["id"], coffee_achievement_keys)
        for row in balances:
            row["achievement_icons"] = coffee_achievement_icons.get(row["id"], [])
        total_outstanding = sum(item["amount"] for item in open_items)
        top_creditor = next((row for row in balances if row["net_coffees"] > 0), None)
        top_debtor = next((row for row in reversed(balances) if row["net_coffees"] < 0), None)
        coffee_summary = {
            "total_open_items": len(open_items),
            "total_outstanding": total_outstanding,
            "total_outstanding_label": treat_label(total_outstanding),
            "top_creditor": top_creditor,
            "top_debtor": top_debtor,
        }
        return render_template(
            "groups/coffee.html",
            group=group,
            members=members,
            open_items=open_items,
            balances=balances,
            coffee_summary=coffee_summary,
            treat_units=TREAT_UNIT_VALUES,
        )

    @app.route("/groups/<slug>/tournaments", methods=("GET", "POST"))
    @login_required
    def group_tournaments(slug: str):
        group = group_membership_or_404(slug)
        db = get_db()
        members = get_group_members(db, group["id"])

        if request.method == "POST":
            action = request.form["action"]
            if action == "create":
                name = request.form["name"].strip()
                tournament_format = request.form.get("format", "round_robin")
                selected = [int(user_id) for user_id in request.form.getlist("participant_ids")]
                if name and len(selected) >= 2:
                    tournament_id = db.insert_and_get_id(
                        "INSERT INTO tournaments (group_id, name, format, status, created_by) VALUES (?, ?, ?, 'active', ?)",
                        (group["id"], name, tournament_format, g.user["id"]),
                    )
                    db.executemany(
                        "INSERT INTO tournament_entries (tournament_id, user_id) VALUES (?, ?)",
                        [(tournament_id, user_id) for user_id in selected],
                    )
                    if tournament_format == "knockout":
                        bracket_players = selected[:]
                        if len(bracket_players) % 2 == 1:
                            bracket_players = bracket_players[:-1]
                        for index in range(0, len(bracket_players), 2):
                            white_id = bracket_players[index]
                            black_id = bracket_players[index + 1]
                            db.execute(
                                """
                                INSERT INTO tournament_games (tournament_id, white_user_id, black_user_id, round_name)
                                VALUES (?, ?, ?, 'Quarterfinal')
                                """,
                                (tournament_id, white_id, black_id),
                            )
                    elif tournament_format == "swiss":
                        seeded = [row["id"] for row in get_group_leaderboard(db, group["id"]) if row["id"] in selected]
                        seeded += [user_id for user_id in selected if user_id not in seeded]
                        for index in range(0, len(seeded) - 1, 2):
                            db.execute(
                                """
                                INSERT INTO tournament_games (tournament_id, white_user_id, black_user_id, round_name)
                                VALUES (?, ?, ?, 'Round 1')
                                """,
                                (tournament_id, seeded[index], seeded[index + 1]),
                            )
                    else:
                        for white_id, black_id in combinations(selected, 2):
                            db.execute(
                                """
                                INSERT INTO tournament_games (tournament_id, white_user_id, black_user_id, round_name)
                                VALUES (?, ?, ?, ?)
                                """,
                                (tournament_id, white_id, black_id, f"Round {tournament_id}"),
                            )
                    db.commit()
                    flash(tr("Tournament created."), "success")
            elif action == "record":
                db.execute(
                    """
                    UPDATE tournament_games
                    SET result = ?, played_at = ?
                    WHERE id = ?
                    """,
                    (request.form["result"], request.form["played_at"], int(request.form["game_id"])),
                )
                db.commit()
                flash(tr("Tournament game updated."), "success")
            elif action == "pair_next":
                tournament_id = int(request.form["tournament_id"])
                tournament = db.execute(
                    "SELECT * FROM tournaments WHERE id = ? AND group_id = ?",
                    (tournament_id, group["id"]),
                ).fetchone()
                if tournament and tournament["format"] == "swiss":
                    pairings = swiss_pairings(db, tournament_id)
                    db.executemany(
                        """
                        INSERT INTO tournament_games (tournament_id, white_user_id, black_user_id, round_name)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(tournament_id, white_id, black_id, round_name) for white_id, black_id, round_name in pairings],
                    )
                    db.commit()
                    flash(tr("Next Swiss round generated."), "success")
            elif action == "finish":
                tournament_id = int(request.form["tournament_id"])
                standings = db.execute(
                    """
                    SELECT user_id, SUM(points) AS total_points
                    FROM (
                        SELECT white_user_id AS user_id,
                               CASE result WHEN 'white' THEN 1 WHEN 'draw' THEN 0.5 ELSE 0 END AS points
                        FROM tournament_games WHERE tournament_id = ?
                        UNION ALL
                        SELECT black_user_id AS user_id,
                               CASE result WHEN 'black' THEN 1 WHEN 'draw' THEN 0.5 ELSE 0 END AS points
                        FROM tournament_games WHERE tournament_id = ?
                    )
                    GROUP BY user_id
                    ORDER BY total_points DESC, user_id
                    LIMIT 1
                    """,
                    (tournament_id, tournament_id),
                ).fetchone()
                db.execute(
                    "UPDATE tournaments SET status = 'completed', winner_user_id = ? WHERE id = ? AND group_id = ?",
                    (standings["user_id"] if standings else None, tournament_id, group["id"]),
                )
                db.commit()
                flash(tr("Tournament marked as completed."), "success")
            return redirect(url_for("group_tournaments", slug=slug))

        tournaments = db.execute(
            """
            SELECT t.*, u.name AS winner_name
            FROM tournaments t
            LEFT JOIN users u ON u.id = t.winner_user_id
            WHERE t.group_id = ?
            ORDER BY t.created_at DESC, t.id DESC
            """,
            (group["id"],),
        ).fetchall()
        games_by_tournament = {}
        standings_by_tournament = {}
        for tournament in tournaments:
            games_by_tournament[tournament["id"]] = db.execute(
                """
                SELECT tg.*, w.name AS white_name, b.name AS black_name
                FROM tournament_games tg
                JOIN users w ON w.id = tg.white_user_id
                JOIN users b ON b.id = tg.black_user_id
                WHERE tg.tournament_id = ?
                ORDER BY tg.id
                """,
                (tournament["id"],),
            ).fetchall()
            standings_by_tournament[tournament["id"]] = db.execute(
                """
                SELECT u.name, user_id, SUM(points) AS total_points
                FROM (
                    SELECT white_user_id AS user_id,
                           CASE result WHEN 'white' THEN 1 WHEN 'draw' THEN 0.5 ELSE 0 END AS points
                    FROM tournament_games WHERE tournament_id = ?
                    UNION ALL
                    SELECT black_user_id AS user_id,
                           CASE result WHEN 'black' THEN 1 WHEN 'draw' THEN 0.5 ELSE 0 END AS points
                    FROM tournament_games WHERE tournament_id = ?
                ) scoring
                JOIN users u ON u.id = scoring.user_id
                GROUP BY user_id, u.name
                ORDER BY total_points DESC, lower(u.name)
                """,
                (tournament["id"], tournament["id"]),
            ).fetchall()
        return render_template(
            "groups/tournaments.html",
            group=group,
            members=members,
            tournaments=tournaments,
            games_by_tournament=games_by_tournament,
            standings_by_tournament=standings_by_tournament,
        )

    @app.route("/groups/<slug>/pgn", methods=("GET", "POST"))
    @login_required
    def group_pgn(slug: str):
        group = group_membership_or_404(slug)
        db = get_db()
        if request.method == "POST":
            imported = 0
            for game in parse_pgn_bundle(request.form["pgn_bundle"]):
                tags = game["tags"]
                white = db.execute("SELECT id FROM users WHERE name = ?", (tags.get("White", ""),)).fetchone()
                black = db.execute("SELECT id FROM users WHERE name = ?", (tags.get("Black", ""),)).fetchone()
                if not white or not black:
                    continue
                result_map = {"1-0": "white", "0-1": "black", "1/2-1/2": "draw"}
                time_control_base_seconds, time_control_increment_seconds = parse_pgn_time_control(tags.get("TimeControl"))
                db.execute(
                    """
                    INSERT INTO matches
                    (group_id, white_user_id, black_user_id, result, played_at, time_control_label, time_control_base_seconds, time_control_increment_seconds, confirmation_status, confirmed_by, opening_name, opening_code, pgn_text, notes, reported_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group["id"],
                        white["id"],
                        black["id"],
                        result_map.get(tags.get("Result", "1/2-1/2"), "draw"),
                        tags.get("Date", datetime.now().strftime("%Y-%m-%d")).replace(".", "-"),
                        build_time_control_label(time_control_base_seconds, time_control_increment_seconds),
                        time_control_base_seconds,
                        time_control_increment_seconds,
                        g.user["id"],
                        tags.get("Opening") or None,
                        tags.get("ECO") or None,
                        game["moves"] or None,
                        "Imported from PGN",
                        g.user["id"],
                    ),
                )
                imported += 1
            db.commit()
            recalculate_group_ratings(db, group["id"])
            flash(tr("Imported {count} PGN games.", count=imported), "success")
            return redirect(url_for("group_pgn", slug=slug))

        matches = db.execute(
            """
            SELECT m.*, w.name AS white_name, wp.name AS white_partner_name,
                   b.name AS black_name, bp.name AS black_partner_name
            FROM matches m
            JOIN users w ON w.id = m.white_user_id
            LEFT JOIN users wp ON wp.id = m.white_partner_user_id
            JOIN users b ON b.id = m.black_user_id
            LEFT JOIN users bp ON bp.id = m.black_partner_user_id
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.pgn_text IS NOT NULL
            ORDER BY m.played_at DESC, m.id DESC
            LIMIT 20
            """,
            (group["id"],),
        ).fetchall()
        return render_template("groups/pgn.html", group=group, matches=matches, build_pgn=build_pgn)

    @app.route("/groups/<slug>/export/matches.csv")
    @login_required
    def export_matches(slug: str):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        rows = get_recent_matches(get_db(), group["id"], limit=5000)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "played_at", "time_control", "game_type", "season", "white", "white_partner", "black", "black_partner", "result", "reported_by", "notes"])
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["played_at"],
                    time_control_label(row) or "",
                    row["game_type"],
                    row["season_name"] or "",
                    row["white_name"],
                    row["white_partner_name"] or "",
                    row["black_name"],
                    row["black_partner_name"] or "",
                    row["result"],
                    row["reporter_name"],
                    row["notes"] or "",
                ]
            )
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={group['slug']}-matches.csv"},
        )

    @app.route("/groups/<slug>/export/leaderboard.csv")
    @login_required
    def export_leaderboard(slug: str):
        try:
            group = group_membership_or_404(slug)
        except PermissionError:
            flash(tr("You do not have access to this group."), "danger")
            return redirect(url_for("index"))

        rows = get_group_leaderboard(get_db(), group["id"])
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["name", "email", "rating", "games", "wins", "draws"])
        for row in rows:
            writer.writerow([row["name"], row["email"], round(row["rating"]), row["games"], row["wins"], row["draws"]])
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={group['slug']}-leaderboard.csv"},
        )

    @app.route("/groups/<slug>/export/games.pgn")
    @login_required
    def export_pgn(slug: str):
        group = group_membership_or_404(slug)
        rows = get_db().execute(
            """
            SELECT m.*, w.name AS white_name, wp.name AS white_partner_name,
                   b.name AS black_name, bp.name AS black_partner_name
            FROM matches m
            JOIN users w ON w.id = m.white_user_id
            LEFT JOIN users wp ON wp.id = m.white_partner_user_id
            JOIN users b ON b.id = m.black_user_id
            LEFT JOIN users bp ON bp.id = m.black_partner_user_id
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed'
            ORDER BY m.played_at DESC, m.id DESC
            """,
            (group["id"],),
        ).fetchall()
        bundle = "\n\n".join(build_pgn(row) for row in rows)
        return Response(
            bundle,
            mimetype="application/x-chess-pgn",
            headers={"Content-Disposition": f"attachment; filename={group['slug']}-games.pgn"},
        )

    @app.errorhandler(PermissionError)
    def handle_permission_error(_: PermissionError):
        flash(tr("You do not have permission for that action."), "danger")
        return redirect(url_for("index"))

    with app.app_context():
        init_db()
        backfill_achievements_once(get_db())

    return app
