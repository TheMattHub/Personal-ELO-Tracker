# Product Brief

## Original goal

Create a private chess ranking app for company colleagues so they can track lunch-break games and maintain an internal ELO ranking.

## Refined prompt

Build a private, browser-based chess ELO platform for internal company use. The app should let colleagues create personal accounts, join a private group through invites, customize a simple player profile, record match results, edit mistakes, track leaderboard changes over time, and manage seasons without depending on a central admin for daily updates.

The product should include the core capabilities people expect from modern ELO software:

- Private groups and membership controls
- Match history and result editing
- Automatic ELO updates with support for wins and draws
- Leaderboards and player statistics
- Rating history and rivalry insights
- Seasons with optional rating resets
- Player profile customization
- Match suggestions
- Special team variants such as Braccio Mente / One Arm, One Brain
- Separate rating ladders when a team-based variant should not change the main singles ELO
- Tournament creation and tracking
- Friendly in-group coffee ledger
- Winner page and hall of fame
- CSV exports for reporting and backup
- Admin controls for invite codes and scoring configuration

The app should be easy to host from a company GitHub repository and easy for colleagues to access in the browser.

## Product decisions

- Browser-first delivery is the best fit because everyone can access it without installing anything.
- Personal accounts plus private group invite codes keep the workspace private while staying simple.
- SQLite is enough for a small internal club and keeps setup friction low.
- A laptop or shared workstation on the company network is the primary deployment target, including colleagues who connect through the company VPN.
- Portable repo snapshots make it easy for another colleague to take over and run the app themselves.
