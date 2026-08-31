# Handover Guide

Use this when another colleague needs to take over hosting the app from their own laptop.

## What the current host should do

1. Open the app as an owner or admin.
2. Go to the `Data` page.
3. Download the latest snapshot JSON.
4. If useful, also note the active invite links from the `Network` page.
5. Share the repo URL and the snapshot JSON with the next host.

## What the next host should do

1. Clone the repo from GitHub.
2. Open the repo folder on their laptop.
3. Run `start-lan.bat`.
4. Create an account in the fresh local app if needed.
5. Create a temporary group only if the app is completely empty and you need admin access before importing.
6. Open the `Data` page.
7. Import the snapshot JSON from the previous host.
8. Open the `Network` page and pick the LAN or VPN URL to share with colleagues.

## What changes after takeover

- The app URL changes to the new host laptop IP.
- Existing users, groups, matches, ratings, tournaments, and settings come from the imported snapshot.
- New invite links should be copied from the new host's `Network` or group `Settings` page because they include the new laptop address.

## Safe checks after import

1. Verify the leaderboard loads.
2. Open one group dashboard and confirm recent matches appear.
3. Test one invite link from another machine on the same LAN or VPN.
4. Log one small test match only if the group agrees, because it will affect ratings.

## Optional repo backup habit

- Keep `AUTO_GIT_COMMIT=1` only if the host laptop has Git configured and the group wants
  automatic snapshot commits. This will write player data (names, emails) into your git
  history on every change — fine for a private repo, but do not enable it on a public one.
- Otherwise, rely on the snapshot files in `data/snapshots/` (gitignored by default) and
  occasional manual exports from the `Data` page.
