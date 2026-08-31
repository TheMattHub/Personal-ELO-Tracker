# Contributing

Thanks for considering a contribution — bug reports, features, and cleanups are all welcome.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate   # or: source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env     # optional, only needed for email sending
python -m flask --app app init-db
python app.py
```

## Running tests

```bash
python -m unittest discover -s tests
```

Please add or update tests for any behavior change — the test suite drives the app
through its real routes (`elo_club/app.py`) rather than mocking internals, so most
features already have a pattern in [tests/test_elo.py](tests/test_elo.py) you can follow.

## Making a change

1. Open an issue first for anything non-trivial (new feature, schema change, UI
   redesign) so we can agree on the approach before you invest time in it. Small fixes
   and typos can go straight to a PR.
2. Keep PRs focused — one logical change per PR is easier to review.
3. Match the existing style: no framework/build step for CSS or JS, no ORM (raw SQL via
   `elo_club/db.py`), translations added to `elo_club/i18n.py` for any new user-facing string.
4. Make sure `python -m unittest discover -s tests` passes before opening the PR.
5. Describe what changed and why in the PR description; screenshots are appreciated for
   UI changes.

## Reporting bugs / requesting features

Use the issue templates. For security vulnerabilities, see [SECURITY.md](SECURITY.md)
instead of opening a public issue.
