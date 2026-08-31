# Security Policy

This is a small hobby project, run at your own risk — but if you find a security
vulnerability, please report it responsibly.

## Reporting a vulnerability

**Do not open a public issue.** Instead, email matteo.brunetti1@gmail.com with:

- A description of the vulnerability and its potential impact
- Steps to reproduce it
- Any suggested fix, if you have one

I'll aim to acknowledge reports within a few days. This project has no paid support or
SLA, so please be patient — it's maintained in spare time.

## Scope

This app is designed to be self-hosted on a trusted local network by a single admin for
their own group, not exposed directly to the public internet. Some things to know:

- There is no built-in HTTPS, rate limiting, or CSRF protection — if you expose this
  beyond a trusted LAN/VPN, put it behind a reverse proxy that provides those.
- `AUTO_GIT_COMMIT` (off by default) will write your app's data into your git history if
  enabled — don't turn it on for a repo you don't fully control.
