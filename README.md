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
| signup-api | Account registration form + CSRF protection | 8081 (local) | api-net |
| docker-proxy | Docker socket proxy (exec-only, read-only socket) | 2375 (internal) | api-net |

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

## Seeded accounts

The deploy script creates these accounts automatically:

| Email | Password |
|-------|----------|
| `admin@<domain>` | `testnet-admin-password` |
| `agent@<domain>` | `agent-password` |
| `user@<domain>` | `user-password` |
| `noreply@<domain>` | `noreply-password` |

## Seed test mail

Create the test accounts and sample conversations with:

```bash
export MAIL_DOMAIN="gmail.com"
./scripts/seed-conversations.sh
```

## Account signup

Agents can create their own accounts at `/signup` (linked from the login page as "Create Account"). The signup form asks for a username and password; the domain is appended automatically. Accounts are created via docker-mailserver's official `setup email add` command through a restricted Docker socket proxy.

Gmail-style URL `/accounts/signup` is also rewritten to `/signup`.

## Inter-service SMTP

Port 25 is open to the network so other testnet nodes (e.g. a Reddit/Lemmy clone) can deliver email to mailserver accounts. Configure the sending service with the mail host's **public IP** on port 25. Postfix accepts mail for its own domain from any source but does not relay to external domains. See [the design doc](docs/mail-server-design.md#smtp-routing-for-inter-service-email) for details.

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

## Security

### What's hardened

- **No Docker socket exposure**: signup-api talks to a [Docker socket proxy](https://github.com/Tecnativa/docker-socket-proxy) that only allows `exec` operations. The raw socket is never mounted into application containers.
- **Non-root signup-api**: runs as UID 10001 on Alpine base (no shell-heavy docker:cli image).
- **CSRF protection**: signup form uses double-submit cookie pattern (SameSite=Strict, HttpOnly, Secure).
- **Rate limiting**: nginx rate-limits `/signup` (6/min) and webmail (30/min) per IP, with burst allowance.
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

## Logs

```bash
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f             # All
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f mailserver   # Mail
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f roundcube    # Webmail
docker compose -f /opt/testnet-mail/docker-compose.yml logs -f signup-api   # Signup
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
docker-compose.yml                          4 services + 2 isolated networks
mailserver.env                              docker-mailserver config (security features disabled for testnet)
roundcube/custom-config.php                 Roundcube branding + agent-friendly defaults
roundcube/plugins/account_signup/           Roundcube plugin: "Create Account" link on login page
signup-api/                                 Go registration service (Docker API, CSRF, non-root)
nginx/mail.conf                             TLS termination, URL rewrites, rate limiting (server block)
nginx/rate-limit.conf                       Rate limit zones + server_tokens off (http context)
scripts/deploy.sh                           Deploy on an existing host (requires root)
scripts/seed-mail.sh                        Create starter email accounts
docs/mail-server-design.md                  Full design document
```
