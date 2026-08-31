from __future__ import annotations

from collections import defaultdict
from math import pow


def expected_score(player_rating: float, opponent_rating: float) -> float:
    return 1 / (1 + pow(10, (opponent_rating - player_rating) / 400))


def calculate_elo_change(
    player_rating: float,
    opponent_rating: float,
    actual_score: float,
    k_factor: int,
) -> float:
    expected = expected_score(player_rating, opponent_rating)
    return k_factor * (actual_score - expected)


def recalculate_group_ratings(db, group_id: int) -> None:
    group = db.execute(
        "SELECT starting_rating, default_k_factor FROM groups_workspace WHERE id = ?",
        (group_id,),
    ).fetchone()
    db.execute("DELETE FROM rating_history WHERE group_id = ?", (group_id,))

    starting_rating = float(group["starting_rating"])
    k_factor = int(group["default_k_factor"])
    for ladder_type, game_type in (("standard", "standard"), ("braccio_mente", "one_arm_one_brain")):
        matches = db.execute(
            """
            SELECT m.*, s.reset_ratings
            FROM matches m
            LEFT JOIN seasons s ON s.id = m.season_id
            WHERE m.group_id = ? AND m.deleted_at IS NULL AND m.confirmation_status = 'confirmed' AND m.game_type = ?
            ORDER BY m.played_at ASC, m.id ASC
            """,
            (group_id, game_type),
        ).fetchall()

        ratings = defaultdict(lambda: starting_rating)
        reset_done_for_season = set()

        for match in matches:
            season_id = match["season_id"]
            if season_id and match["reset_ratings"] and season_id not in reset_done_for_season:
                ratings = defaultdict(lambda: starting_rating)
                reset_done_for_season.add(season_id)

            white_team = [match["white_user_id"]]
            black_team = [match["black_user_id"]]
            if match["white_partner_user_id"]:
                white_team.append(match["white_partner_user_id"])
            if match["black_partner_user_id"]:
                black_team.append(match["black_partner_user_id"])

            white_before = sum(ratings[user_id] for user_id in white_team) / len(white_team)
            black_before = sum(ratings[user_id] for user_id in black_team) / len(black_team)

            if match["result"] == "white":
                white_score, black_score = 1.0, 0.0
            elif match["result"] == "black":
                white_score, black_score = 0.0, 1.0
            else:
                white_score = black_score = 0.5

            white_delta = calculate_elo_change(white_before, black_before, white_score, k_factor)
            black_delta = calculate_elo_change(black_before, white_before, black_score, k_factor)

            history_rows = []
            for user_id in white_team:
                player_before = ratings[user_id]
                player_after = player_before + white_delta
                ratings[user_id] = player_after
                history_rows.append((group_id, season_id, match["id"], ladder_type, user_id, player_before, player_after, white_delta))
            for user_id in black_team:
                player_before = ratings[user_id]
                player_after = player_before + black_delta
                ratings[user_id] = player_after
                history_rows.append((group_id, season_id, match["id"], ladder_type, user_id, player_before, player_after, black_delta))

            db.executemany(
                """
                INSERT INTO rating_history (
                    group_id, season_id, match_id, ladder_type, user_id, rating_before, rating_after, delta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                history_rows,
            )

    db.commit()
