# bot.andx.ai — gateway

Server-side code for **https://bot.andx.ai**. This is infrastructure, **not**
part of the student bot install — if you are a student setting up your bot,
everything you need is in the repository root and you can ignore this folder.

## What it does

One URL for every ANDX user. A user signs in with their ANDX API key and
secret; the key is validated against ANDX and becomes their identity
(`uid = "u" + sha256(key)[:16]`). On first sign-in the gateway creates that
user's own isolated bot container, pre-loaded with their key, and every
authenticated request is reverse-proxied to that container. No user can reach
another's.

API keys are wrapped with Google Cloud KMS and mounted into the container from
a tmpfs, so a plaintext key is never written to disk.

## Running it

Runs as a non-root user under systemd, behind Caddy for TLS:

```
gunicorn -w 1 --threads 8 -b 127.0.0.1:8000 gateway:app
```

Requires Python 3.10+, Flask, gunicorn, requests, Docker, Caddy, and a GCP KMS
key. Configuration comes from the environment (`ANDX_KMS_KEY`, `MAX_BOTS`) and
from mode-0600 files the gateway creates in its working directory on first run
(`.gateway_secret`, `.admin_key`, `.signup_view_key`). None of those are in
this repository.

The gateway is deliberately not root. Its one privileged action — starting and
restarting a user's container — goes through a fixed helper script installed at
`/usr/local/sbin/andx-bot` and allowed by a single sudoers line. That helper
validates the user id itself before doing anything, and is not published here.

## Embedding

The ANDX platform shows this site inside an "AI Trading Bot" tab. A
`Content-Security-Policy: frame-ancestors` header restricts framing to the four
platform origins, and the admin console is never frameable. Users still sign in
on the bot page itself; the platform never sees or forwards their API key.
