# Lunchbreak ELO — App Summary (for redesign brief)

## One-liner
A private, self-hosted (LAN-first) Flask web app that lets a company's colleagues run an internal chess ELO ladder: personal accounts, invite-only groups, match logging with automatic ELO recalculation, tournaments, seasons, achievements, and a joke "coffee ledger" for side bets.

## Current stack (implementation, not necessarily to be kept)
- **Backend:** Python/Flask, single ~4,460-line `elo_club/app.py` with all routes/business logic (no blueprints), plus `db.py` (raw SQLite via `sqlite3`, hand-written schema + additive migrations, no ORM/Alembic), `elo.py` (ELO math), `i18n.py` (1,600-line hand-rolled string-dictionary translation system for en/it/fr/es).
- **Frontend:** Server-rendered Jinja2 templates (~28 templates), one large hand-written `styles.css` (2,230 lines, custom properties, warm cream/terracotta "chess club" palette, no framework/build step), a little vanilla inline JS (mobile nav toggle).
- **DB:** SQLite file in `instance/`. Every write commits also trigger a full JSON export snapshot to `data/snapshots/` (and optional auto git-commit) — this is the app's entire backup/portability strategy, and it has produced 150+ snapshot files in the repo over time.
- **Deploy model:** Not a hosted SaaS — intended to run from a colleague's laptop on the company LAN/VPN, with a `Network` page that shows shareable LAN/VPN URLs, and a manual "handover" process (export snapshot → next host imports it) documented in `handover.md`. Optional Docker Compose.
- **Auth:** Simple username/password (Werkzeug password hash presumably), session-based, no OAuth/SSO.

## Data model (SQLite tables)
`users`, `groups_workspace` (workspaces/clubs), `teams`, `memberships` (user↔group, roles), `seasons`, `matches`, `rating_history`, `tournaments` + `tournament_entries` + `tournament_games`, `challenges`, `coffee_ledger`, `signup_notifications`(+reads), `app_meta`, `user_achievements`.

Key design points worth carrying into a redesign:
- Groups are isolated "clubs" — a user can belong to several groups with a role per group.
- Matches support **two parallel ELO ladders per group**: `standard` (1v1) and `one_arm_one_brain` ("Braccio Mente" — a 2v2 team variant with a "mente"/caller and "braccio"/hands roles) — these never mix rating pools.
- Full rating history is **recomputed from scratch** on every change (`recalculate_group_ratings` deletes and replays all confirmed matches in order) rather than incrementally patched — simple but means edits/deletes are O(all matches).
- Matches carry rich metadata: time control, PGN text, opening name/code, confirmation workflow (pending → confirmed, for player-submitted results), soft delete (`deleted_at`).
- Seasons can optionally reset ratings.
- Achievements are defined in-code (`ACHIEVEMENT_DEFINITIONS`) and unlocked server-side with a "reward avatar icon/title" mechanic and a modal popup shown on next page load.

## Feature inventory (routes in `elo_club/app.py`)
- Auth: register, login, logout, account settings (profile: bio, tagline, favorite opening, avatar color/icon/upload, avatar generated per-user JPG)
- Groups: create, join by invite code, per-group settings (K-factor, starting rating), invite link landing page
- Members: list/manage members, per-member public profile page
- Matches: log, edit, list/filter, pending-confirmation workflow
- Seasons: create/list, optional rating reset
- Stats: group-wide stats page, head-to-head comparisons, suggested matchups (freshness + closeness of rating)
- Challenges: challenge another player to a game, accept/decline
- Teams: team/department groupings, "team race" standings
- Tournaments: round-robin / knockout / swiss, entries, per-round games
- Coffee ledger: informal debt tracking tied to matches or manual entries, settle debts
- Winners / Hall of Fame page
- PGN import/export page; CSV export of matches and leaderboard; PGN export of games
- System pages: Network (LAN/VPN URL helper), Data (export/import JSON snapshot, this is the whole backup UX), Diagnostics
- Achievements + weekly missions (dashboard widgets), belt "king of the hill" holder tracking
- i18n language switcher (en/it/fr/es), persisted per-request via a `set_language` POST + cookie/session

## Current UI/visual style
- Warm, "cozy chess club" aesthetic: cream/parchment background (`--bg: #f4efe3`), terracotta accent (`--accent: #b44f33`), teal secondary, gold/silver/bronze medal colors, soft radial-gradient glows behind content, rounded cards (22–30px radius), subtle grid texture overlay.
- Font stack: "Segoe UI Variable", Aptos, Trebuchet MS — Windows-native, no custom webfont.
- Layout: sticky header with brand mark, language dropdown (flag icons), nav links as icon+label pairs; content in `.wrap` (max 1120px) with card-grid dashboard (`dashboard-grid` with many small `.card` widgets: leaderboard, recent matches, snapshot stats, suggestions, challenges, hall of fame, win streaks, nudges, missions, achievements, team race).
- Custom icon set: ~101 files in `elo_club/static/icons` (nav icons, section icons, badges, achievement reward icons — some hand-generated per `docs/challenge-achievement-icon-prompts.md`), plus 2 illustrations.
- Mobile: a single collapsible header toggle; otherwise the CSS appears to be hand-tuned per breakpoint rather than a systematic responsive framework.
- No dark mode, no design system/component library — everything is bespoke CSS classes per section.

## Notable rough edges / opportunities for a redesign to address
- **Monolith files**: one 4,460-line `app.py` and one 2,230-line CSS file — a rewrite could modularize by feature (blueprints/routers) and componentize styles.
- **Dashboard is very "kitchen sink"**: ~12 cards crammed onto one page (leaderboard, matches, snapshot, suggestions, challenges, hall of fame, streaks, nudges, missions, achievements, team race) — an opportunity to redesign information hierarchy/IA rather than a flat card grid.
- **No real-time/interactive UI** — full page reloads for everything (classic server-rendered forms), no SPA/client state, no optimistic UI.
- **Backup/portability model is manual and file-based** (JSON snapshot export/import + laptop handover) rather than a real hosted database — a redesign could target proper hosting instead of "one colleague's laptop on the LAN."
- **i18n is a hand-maintained dictionary** (1,600+ lines) rather than using a standard i18n library/format (e.g., gettext/ICU/JSON locale files).
- **Achievements/missions/belt are simple, code-defined lists** — could become richer/data-driven.
- Visual design is dense, text-heavy, table-heavy (many stat columns) — a modern redesign likely wants clearer visual hierarchy, charts for rating history over time (currently only stored, not obviously charted), and a lighter mobile experience.

## Suggested framing for the "build a much better app" prompt
Keep: the private-groups-with-invite-codes model, dual ELO ladders (singles + 2v2 team variant), season resets, tournaments (round-robin/knockout/swiss), coffee ledger as a fun/social hook, achievements, head-to-head stats, CSV/PGN export, multi-language support.
Reconsider: monolithic server-rendered architecture → could become a modern SPA/API split; laptop-hosted SQLite + manual snapshot handover → could become a properly hosted DB with real backups; kitchen-sink dashboard → redesigned IA with clearer primary/secondary views; hand-rolled i18n and CSS → standard tooling/design system; add rating-history charts, richer notifications, and a more polished mobile-first UI.
