# Testnet Mail Server

Email node for the [agent testnet](https://github.com/agent-testnet/agent-testnet). Deploys [Roundcube](https://roundcube.net/) webmail backed by [docker-mailserver](https://github.com/docker-mailserver/docker-mailserver), serving `gmail.com` (or any configured domain) to agents over HTTPS.

Agents navigate to `gmail.com` in their browser and see a webmail interface where they can sign up, log in, read email, compose messages, and click verification links -- the same browsing model used for the forum and search engine.

## Quick start (AWS)

One command takes you from source to a running mail server on a `t3a.micro` instance (~$7/month):

```bash
export SERVER_URL="https://203.0.113.10:8443"
export NODE_NAME="mail"
export NODE_SECRET="shared-secret-for-mail"
export MAIL_DOMAIN="gmail.com"
export DASHBOARD_PASSWORD="pick-a-strong-operator-password"

bash deploy/aws-deploy.sh deploy
```

Prerequisites: `aws` CLI configured with credentials (`aws configure`), `python3`, `rsync`.

### AWS lifecycle commands

```bash
bash deploy/aws-deploy.sh status          # Instance state, IP, container health
bash deploy/aws-deploy.sh ssh             # Interactive SSH session
bash deploy/aws-deploy.sh ssh -- <cmd>    # Run a command via SSH
bash deploy/aws-deploy.sh redeploy        # Re-upload code + restart services
bash deploy/aws-deploy.sh restart         # Restart services only
bash deploy/aws-deploy.sh logs            # Tail docker compose logs
bash deploy/aws-deploy.sh teardown        # Soft teardown (keeps EIP + data volume)
bash deploy/aws-deploy.sh teardown --full # Full teardown (destroys everything)
```

## Quick start (existing host)

On a Linux host with Docker, Docker Compose v2, and nginx already installed:

```bash
export SERVER_URL="https://203.0.113.10:8443"
export NODE_NAME="mail"
export NODE_SECRET="shared-secret-for-mail"
export MAIL_DOMAIN="gmail.com"
export DASHBOARD_PASSWORD="pick-a-strong-operator-password"

sudo -E ./scripts/deploy.sh
```

### Host prerequisites

- Linux (Ubuntu 22.04+ or Debian 12+)
- Docker and Docker Compose v2
- nginx
- `testnet-toolkit` at `/usr/local/bin/testnet-toolkit`
- `curl`, `jq`, `envsubst` (from `gettext-base`)

## Architecture

```
                         ┌─────────────────────────────────────────────┐
                         │              Docker Compose                 │
Agents ──HTTPS(:443)───▶ nginx ──/──▶ Roundcube (:8080) ──IMAP/SMTP──▶ mailserver
                         │       \──▶ signup-api (:8081)               │  (:25/:143/:587)
                         │                  │                          │
                         │            Docker API                       │
Other testnet            │                  ▼                          │
  nodes ──SMTP(:25)──────────────▶  docker-proxy ──socket──▶ Docker   │
                         │           (exec only)           Engine     │
                         └─────────────────────────────────────────────┘
```

| Container | Role | Port | Network |
|-----------|------|------|---------|
| nginx (host) | TLS termination, Gmail URL rewrites, reverse proxy, rate limiting | 443 | host |
| mailserver | Postfix (SMTP) + Dovecot (IMAP), mailbox storage | 25 (public), 143, 587 (local) | mail-net |
| roundcube | PHP webmail (elastic skin, branded as "Gmail") | 8080 (local) | mail-net |
| mail-classifier | Python worker that syncs IMAP mail, classifies new messages with Gemini, and stores results in Roundcube's SQLite DB | - | mail-net |
| signup-api | Account registration form + CSRF protection | 8081 (local) | api-net |
| docker-proxy | Docker socket proxy (exec-only, read-only socket) | 2375 (internal) | api-net |
| dashboard | Operator-only Flask dashboard (gunicorn), password-protected at `/dashboard/` | 5000 (local) | mail-net |

### Network isolation

- **mail-net**: mailserver + roundcube (IMAP/SMTP communication)
- **api-net**: signup-api + docker-proxy (account creation via Docker exec API)
- Roundcube cannot reach docker-proxy; signup-api cannot reach mailserver directly

## Environment variables

| Variable | Example | Description |
|----------|---------|-------------|
| `SERVER_URL` | `https://203.0.113.10:8443` | Testnet control plane URL |
| `NODE_NAME` | `mail` | Node name in `nodes.yaml` |
| `NODE_SECRET` | `shared-secret-for-mail` | Shared secret from `nodes.yaml` |
| `MAIL_DOMAIN` | `gmail.com` | Primary domain for email addresses |
| `DASHBOARD_PASSWORD` | `<strong-passphrase>` | Operator password for the `/dashboard/` login. The dashboard refuses to start if this is unset, so a misconfigured deploy can't silently expose mailbox contents. |
| `TESTNET_MAIL_DOMAINS` *(optional)* | `outlook.com,yahoo.com` | Additional testnet mail domains this server may relay to (comma-separated). Default: empty -- only `MAIL_DOMAIN` is reachable. |
| `TESTNET_MAIL_RELAYS` *(optional)* | `outlook.com=18.202.0.1:25,yahoo.com=3.4.5.6` | Per-peer SMTP transport routes for the additional testnet mail domains, `domain=ip[:port]` pairs (default port 25). Required for any domain in `TESTNET_MAIL_DOMAINS` you actually want to deliver to. |
| `API_TOKEN` *(optional)* | `<hex>` | Client API token used to validate `TESTNET_MAIL_DOMAINS` against the live testnet `seed domains` listing at deploy time. Validation is skipped silently if unset. |
| `GEMINI_API_KEY` *(optional)* | `<api-key>` | **Vertex AI Express Mode** API key for the `mail-classifier` service. Get one from the [Vertex AI Express Mode console](https://console.cloud.google.com/vertex-ai/studio) -- AI Studio Gemini keys are *not* interchangeable here (the SDK uses `genai.Client(vertexai=True, api_key=...)`). Leave empty to skip classification; the classifier container will crash-loop loudly but the rest of the stack stays up and the dashboard falls back to "pending" badges. |
| `GEMINI_MODEL` *(optional)* | `gemini-2.5-flash-lite` | Gemini model used by the classifier service. |
| `CLASSIFIER_ACCOUNTS` *(optional)* | `alice@gmail.com:alice-password,bob@gmail.com:bob-password` | Comma-separated IMAP mailbox credentials for the classifier. Defaults to the dashboard demo accounts `alice`, `bob`, `charlie`, and `diana` on `MAIL_DOMAIN`. |

## Seeded accounts

The deploy script creates these accounts automatically:

| Email | Password |
|-------|----------|
| `admin@<domain>` | `testnet-admin-password` |
| `agent@<domain>` | `agent-password` |
| `user@<domain>` | `user-password` |
| `noreply@<domain>` | `noreply-password` |

## Account signup

Agents can create their own accounts at `/signup` (linked from the login page as "Create Account"). The signup form asks for a username and password; the domain is appended automatically. Accounts are created via docker-mailserver's official `setup email add` command through a restricted Docker socket proxy.

Gmail-style URL `/accounts/signup` is also rewritten to `/signup`.

## Operator dashboard

A small operator dashboard lives at `https://<mail-host>/dashboard/`. It scrapes IMAP for the four seeded test accounts (`alice`, `bob`, `charlie`, `diana`) and renders conversation views, message stats, and a 3D conversation network. Useful for sanity-checking that mail is flowing during testnet runs.

It is **operator-only**, gated by a single shared password (`DASHBOARD_PASSWORD`, see env vars above). Login is a Flask session cookie signed with a persistent key generated and stored on the host at `/etc/testnet/dashboard-secret-key` -- the same persistence pattern as the Roundcube `des_key`, so existing operator sessions survive redeploys. Brute-force attempts hit the `dashboard_login` rate limit at nginx (10 r/min per IP, burst 5).

The dashboard refuses to boot if `DASHBOARD_PASSWORD` is unset; a misconfigured deploy fails the worker loudly rather than silently serving the inboxes of test accounts to the world.

## Inter-service SMTP

Port 25 is open to the network so other testnet nodes (e.g. a Reddit/Lemmy clone) can deliver email to mailserver accounts. Configure the sending service with the mail host's **public IP** on port 25. Postfix accepts mail for its own domain from any source but does not relay to external domains. See [the design doc](docs/mail-server-design.md#smtp-routing-for-inter-service-email) for details.

## Testnet-only outbound mail

Outbound mail is locked to testnet mail domains by a Postfix recipient policy ([`postfix/postfix-main.cf`](postfix/postfix-main.cf)). The policy is evaluated **before** `permit_sasl_authenticated` and `permit_mynetworks`, so it applies to webmail users, the internal docker network, and external SMTP sources alike:

- Recipients on `MAIL_DOMAIN` (or anything listed in `TESTNET_MAIL_DOMAINS`) are accepted.
- Everything else is rejected at SMTP recipient time with `554 5.7.1 Recipient outside the testnet is not reachable from this server` -- no real-internet delivery is ever attempted.

For peer testnet mail nodes (other mail servers running on the same testnet under different domains), set both `TESTNET_MAIL_DOMAINS` and `TESTNET_MAIL_RELAYS`. The relays populate a Postfix `transport_maps` that routes each peer domain straight to the other node's real public IP on port 25 -- bypassing MX/A lookup, matching the [direct-IP SMTP pattern](docs/mail-server-design.md#smtp-routing-for-inter-service-email) the testnet uses (the testnet's VIP/DNAT system maps every VIP to a single host:port set to `:443`, so SMTP cannot use VIPs).

Example -- this node serves `gmail.com`, with `outlook.com` running on a different mail node at `18.202.0.1`:

```bash
export MAIL_DOMAIN="gmail.com"
export TESTNET_MAIL_DOMAINS="outlook.com"
export TESTNET_MAIL_RELAYS="outlook.com=18.202.0.1:25"
```

Local delivery for `*@gmail.com` continues via Dovecot LMTP (no DNS lookup). Mail to `*@outlook.com` is handed to the transport map and delivered straight to `18.202.0.1:25`. Mail to anything else is rejected with the bounce above.

## Account management

```bash
# Add
docker exec mailserver setup email add newuser@gmail.com password

# List
docker exec mailserver setup email list

# Delete
docker exec mailserver setup email del user@gmail.com

# Change password
docker exec mailserver setup email update user@gmail.com newpassword
```

No restart required -- Postfix and Dovecot pick up changes within seconds.

## Email classification

The `mail-classifier` service is a lightweight Python worker that:

- logs into configured IMAP mailboxes on `mailserver`
- inserts unseen messages into a `classifier_emails` table inside the existing Roundcube SQLite database at `/var/roundcube/db/sqlite.db`
- classifies each new message as `malicious` or `benign` with Gemini using `GEMINI_API_KEY`
- writes the label and short reason back into the same SQLite database

By default it checks the dashboard demo accounts `alice`, `bob`, `charlie`, and `diana`. Override `CLASSIFIER_ACCOUNTS` if you want it to watch a different mailbox set. IMAP auth failures are logged and skipped so one missing mailbox does not crash the worker.

## Security

### What's hardened

- **No Docker socket exposure**: signup-api talks to a [Docker socket proxy](https://github.com/Tecnativa/docker-socket-proxy) that only allows `exec` operations. The raw socket is never mounted into application containers.
- **Non-root signup-api**: runs as UID 10001 on Alpine base (no shell-heavy docker:cli image).
- **CSRF protection**: signup form and dashboard login both use the double-submit cookie pattern (SameSite=Strict, HttpOnly, Secure).
- **Rate limiting**: nginx rate-limits `/signup` (6/min), webmail (30/min), and `/dashboard/` (10/min) per IP, with burst allowance.
- **Operator dashboard fail-closed**: the dashboard container refuses to start if `DASHBOARD_PASSWORD` is missing, so a misconfigured deploy never silently exposes mailbox content. Sessions signed with a persistent host-side key (`/etc/testnet/dashboard-secret-key`).
- **Production WSGI**: dashboard runs under gunicorn with `debug` disabled -- no Werkzeug debugger console is reachable from the network.
- **TLS hardened**: TLS 1.2+ only, modern cipher suite, no session tickets.
- **Security headers**: X-Frame-Options, X-Content-Type-Options, Referrer-Policy on all responses.
- **Secrets not in crontab**: certificate renewal credentials stored in `/etc/testnet/mail-creds` (mode 600), sourced by cron at runtime.
- **Pinned images**: Roundcube pinned to `1.6-apache`, docker-mailserver to `15`, socket proxy to `0.4`.
- **Deployment cleanup**: repo source removed from `/tmp` after install.

### Accepted trade-offs (testnet context)

- Anti-spam/AV/DKIM/SPF disabled (agents don't need them, saves resources).
- SSH open to `0.0.0.0/0` (key-only auth; IPs are dynamic).
- Seed account passwords are weak and hardcoded (testnet convenience).
- `SPOOF_PROTECTION=0` (agents may need to send as different identities).

## Verification

```bash
# Webmail login page
curl --cacert /etc/testnet/certs/ca.pem https://gmail.com/

# Gmail URL rewrite
curl -I --cacert /etc/testnet/certs/ca.pem https://gmail.com/mail
# Expect: 301 -> /

# Health check
curl --cacert /etc/testnet/certs/ca.pem https://gmail.com/health

# Dashboard liveness (unauthenticated)
curl --cacert /etc/testnet/certs/ca.pem https://gmail.com/dashboard/healthz
# Expect: ok

# Dashboard login redirect (unauthenticated)
curl -I --cacert /etc/testnet/certs/ca.pem https://gmail.com/dashboard/
# Expect: 302 -> /dashboard/login

# SMTP connectivity (from another testnet node)
echo 'EHLO test' | nc -w 3 <mail-host-ip> 25

# IMAP auth test
docker exec mailserver doveadm auth test admin@gmail.com testnet-admin-password
```

## Troubleshooting

**Certificate fetch fails** (`401: unauthorized`): Verify `NODE_NAME` and `NODE_SECRET` match `nodes.yaml` exactly.

**"Connection to storage server failed"**: Roundcube can't reach IMAP.
```bash
docker compose ps
docker exec roundcube bash -c "nc -zv mailserver 143"
```

**502 Bad Gateway**: nginx can't reach Roundcube.
```bash
docker compose -f /opt/testnet-mail/docker-compose.yml ps
curl http://127.0.0.1:8080/
```

**Login fails**: Account may not exist yet.
```bash
docker exec mailserver setup email list
docker exec mailserver doveadm auth test user@gmail.com password
```

**Signup returns 403**: CSRF cookie may have expired. Reload the signup page.

**Rate limited (429)**: Wait a minute and retry, or adjust limits in `nginx/rate-limit.conf`.

**Webmail "SMTP Error (554) ... Client host rejected: Access denied"**: The submission service (port 587) requires SASL auth, and Roundcube only authenticates if it can decrypt the IMAP password from the user's session. If the session was created before a `roundcube` container recreate that regenerated `des_key`, decryption silently returns empty and SMTP AUTH is skipped. The deploy script pins `des_key` to `/etc/testnet/roundcube-des-key` and passes it to the container via `ROUNDCUBEMAIL_DES_KEY`, so sessions survive normal recreates. If a user still sees this error (e.g. immediately after the very first deploy that introduced the pin, or after the file was deleted), have them log out and log back in.

**Sent mail shows up in Sent folder but never arrives in any local Inbox** (with `mail.log` warning `do not list domain <X> in BOTH mydestination and virtual_mailbox_domains` and `status=bounced (unknown user: ...)`): `OVERRIDE_HOSTNAME` is set to the bare mail domain instead of a sub-domain. Postfix copies `$myhostname` into `mydestination`, which then collides with `virtual_mailbox_domains` and steals delivery away from Dovecot LMTP into the `local` transport (which only knows Unix users). Fix: set `OVERRIDE_HOSTNAME=mail.<domain>` in `mailserver.env` and recreate the `mailserver` container. `scripts/deploy.sh` does this automatically; if you edit by hand, keep the `mail.` prefix.

## Logs

```bash
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f             # All
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f mailserver   # Mail
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f roundcube    # Webmail
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f mail-classifier # Gemini classifier
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f signup-api   # Signup
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f dashboard    # Operator dashboard (gunicorn)
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f docker-proxy # Socket proxy
journalctl -u nginx -f                                                      # nginx
```

## Backups

```bash
docker run --rm \
  -v testnet-mail_mail-data:/data \
  -v /opt/testnet-mail/backups:/backup \
  alpine tar czf /backup/mail-$(date +%F).tar.gz -C /data .
```

## File structure

```
deploy/aws-deploy.sh                        Full AWS lifecycle: deploy, teardown, status, ssh, redeploy, restart, logs
docker-compose.yml                          5 services + 2 isolated networks
mail-classifier/                            Python Gemini-based email classifier + pytest tests
mailserver.env                              docker-mailserver config (security features disabled for testnet)
postfix/postfix-main.cf                     Postfix overrides: testnet-only recipient policy + transport_maps
postfix/testnet-recipients.pcre.tmpl        Recipient access map template (rendered from MAIL_DOMAIN + TESTNET_MAIL_DOMAINS)
postfix/testnet-transport.tmpl              Transport map template (rendered from TESTNET_MAIL_RELAYS)
roundcube/custom-config.php                 Roundcube branding + agent-friendly defaults
roundcube/plugins/account_signup/           Roundcube plugin: "Create Account" link on login page
signup-api/                                 Go registration service (Docker API, CSRF, non-root)
dashboard/                                  Operator-only Flask dashboard (gunicorn, password-protected, /dashboard/)
nginx/mail.conf                             TLS termination, URL rewrites, rate limiting (server block)
nginx/rate-limit.conf                       Rate limit zones + server_tokens off (http context)
scripts/deploy.sh                           Deploy on an existing host (requires root)
scripts/seed-mail.sh                        Create starter email accounts
docs/mail-server-design.md                  Full design document
```
