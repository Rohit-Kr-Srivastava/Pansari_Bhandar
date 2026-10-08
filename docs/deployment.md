# Deployment: Docker, Railway, PostgreSQL, GitHub

## 1. What runs where

| Piece | Local | Railway |
| --- | --- | --- |
| Web app | `uvicorn` from the venv, or the Docker image | Docker image built from `Dockerfile` |
| Database | PostgreSQL 16 in `docker compose` | Railway PostgreSQL plugin |
| Migrations | `alembic upgrade head` | `preDeployCommand` in `railway.json` (runs before each deploy) |
| Uploaded images | `./media` | Railway **volume** mounted at `/data` (`MEDIA_ROOT=/data/media`) |
| Static files | served by the app from `app/static` with content-hash URLs | same |
| Health check | `GET /healthz` (checks the database) | `healthcheckPath` in `railway.json` |
| Logs | stdout, one line per request with a request id | Railway log viewer |

The app runs **one process** (`WEB_CONCURRENCY=1`). The login rate limiter and the 60-second
settings cache live in process memory; more processes would each have their own. That is plenty
for one shop. To scale out, move the rate limiter to Redis first.

## 2. Environment variables

| Variable | Required in production | Example / notes |
| --- | --- | --- |
| `ENVIRONMENT` | yes | `production` (turns on HSTS, secure cookies, indexing in robots.txt and the guards below) |
| `SECRET_KEY` | yes | 48+ random characters: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Changing it signs everyone out. |
| `DATABASE_URL` | yes | Railway: reference `${{Postgres.DATABASE_URL}}`. `postgres://` / `postgresql://` URLs are converted to the psycopg driver automatically. **Do not add `sslmode=require` for Railway's private network URL** (it caused 502s before). |
| `SITE_URL` | yes | `https://www.example.in` (canonical URLs, sitemap, Open Graph) |
| `ALLOWED_HOSTS` | yes | `www.example.in,example.in,yourapp.up.railway.app` (comma separated; `*` is refused in production) |
| `MEDIA_ROOT` | set by image | `/data/media`. Mount a Railway volume at `/data`. |
| `MAX_UPLOAD_MB` | no | `5` |
| `LOG_LEVEL` | no | `INFO` |
| `DEBUG` | no | must be `false` in production (the app refuses to start otherwise) |
| `WEB_CONCURRENCY` | no | keep `1` (see above) |
| `TRUSTED_PROXY_HOPS` | set by the image to `1` | Number of reverse proxies in front of the app (Railway: 1). The visitor's IP is the Nth entry from the right of `X-Forwarded-For`, so a spoofed header cannot dodge rate limits. Use `0` when there is no proxy. |
| `STAFF_SESSION_HOURS` | no | `12`. Seller and admin sign-ins expire sooner than customers'. |

No special permission variable is needed for the uploads volume: the container starts as root
only to give the `app` user ownership of `/data` (Railway mounts volumes as root), then drops
to that unprivileged user before the web server starts (`scripts/start.sh`).

`/healthz` is exempt from the `ALLOWED_HOSTS` check, because Railway's probe sends its own
`Host` header (`healthcheck.railway.app`). Every other URL still rejects unknown hosts.

The WhatsApp number, delivery fees, pincodes, homepage text and store details are **not**
environment variables. They are edited in the admin dashboard.

## 3. Commands

### Local development

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt pytest httpx ruff mypy
copy .env.example .env            # then set SECRET_KEY  (macOS/Linux: cp)
docker compose up -d db
alembic upgrade head
python -m scripts.seed_dev --pincode 829111 --accounts   # demo data, prints demo logins
uvicorn app.main:app_factory --factory --reload --port 8000
```

Tests and checks:

```bash
pytest                                                    # SQLite
set TEST_DATABASE_URL=postgresql+psycopg://pansari:pansari@localhost:5432/pansari_test
pytest                                                    # the same tests on PostgreSQL, plus the migration test
ruff check . && ruff format --check .
```

### Docker development

```bash
docker compose up --build          # PostgreSQL + the app on http://localhost:8000, migrations run on start
docker compose exec app python -m scripts.seed_dev --pincode 829111 --accounts
docker compose exec app python -m scripts.create_admin --phone 9XXXXXXXXX --name "Owner"
docker compose down                # add -v to delete the local database and images
```

### Database migrations

```bash
alembic upgrade head               # apply everything
alembic current                    # what the database is on
alembic history                    # list migrations
alembic revision --autogenerate -m "describe change"   # after changing models; review the file!
alembic check                      # models and migrations agree (run in CI)
alembic downgrade -1               # undo the last migration (see rollback below)
```

### Git

```bash
git init -b main                   # once, if the folder is not a repository yet
git add -A
git status                         # confirm .env, media/ and *.tar.gz are NOT listed
git commit -m "Pansari Bhandar: shop, checkout, dashboards, deployment"
git remote add origin https://github.com/<you>/pansari-bhandar.git
git push -u origin main
```

### Railway (first time)

1. railway.com → New Project → **Deploy from GitHub repo** → choose the repository.
2. In the project: **+ New → Database → PostgreSQL**.
3. App service → **Variables**: `ENVIRONMENT=production`, `SECRET_KEY=<random>`,
   `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `SITE_URL=https://<domain>`,
   `ALLOWED_HOSTS=<domain>,<service>.up.railway.app`.
4. App service → **Settings → Volumes → New volume**, mount path `/data`.
5. **Settings → Networking → Generate domain** (and add your own domain later).
6. Deploy. Railway builds the `Dockerfile`, runs `alembic upgrade head` (pre-deploy), starts the
   app and waits for `/healthz`.
7. Create the owner's admin account once (Railway CLI):

```bash
npm i -g @railway/cli
railway login
railway link
railway ssh                        # opens a shell inside the running app container
python -m scripts.create_admin --phone 9XXXXXXXXX --name "Owner Name"   # run inside that shell
```

8. Sign in at `/login`, then fill in **Admin → Store information** (address, phone,
   WhatsApp number, hours, story), **Delivery** (pincodes, fees, slots, pickup), **Categories**
   and **Homepage**. Never run `seed_dev` in production (it refuses when `ENVIRONMENT=production`).

Later deploys: `git push` to the connected branch. Railway redeploys automatically.

## 4. Pre-launch verification checklist

Run against the Railway URL after the first deploy:

- [ ] `GET /healthz` returns `ok` (database reachable)
- [ ] `railway logs` shows `alembic upgrade head` finished and no errors at start-up
- [ ] Home page loads with styling (static files work); response headers include
      `strict-transport-security` and `content-security-policy`
- [ ] `/robots.txt` lists `Sitemap:` (it only allows indexing when `ENVIRONMENT=production`)
- [ ] Sign in as admin; open every admin menu item
- [ ] Add a product **with a photo**, reload, photo still there after a redeploy (volume works)
- [ ] Register a customer, add to cart, check out with COD to a configured pincode, see the
      order in Admin → Orders, move it to Delivered
- [ ] Wrong password six times → "Too many sign-in attempts"
- [ ] `git ls-files | findstr /i ".env media"` prints nothing (no secrets or uploads committed)

## 5. Backups

- Railway PostgreSQL: enable **Backups** on the database service (daily) if your plan offers it.
- Independent of Railway, keep a weekly off-site copy:

```bash
pg_dump --format=custom --no-owner "$DATABASE_PUBLIC_URL" > pansari-$(date +%F).dump
```

  Restore into an empty database with
  `pg_restore --no-owner --dbname "$TARGET_URL" pansari-YYYY-MM-DD.dump`. Test a restore once.
- Product photos live on the volume. Copy them occasionally:
  from a `railway ssh` session, `tar czf /tmp/media.tgz -C /data media` and download it, or sync /data/media to cloud storage.
- Keep `SECRET_KEY` in a password manager. Losing it only signs people out; leaking it lets
  someone forge sessions, so rotate it if exposed.

## 6. Rollback

- **Bad code, no migration in the release**: Railway → Deployments → previous deployment →
  **Redeploy** (instant). Or `git revert <sha> && git push`.
- **Release included a migration**: first decide whether the old code works with the new
  schema. Migrations here are additive where possible, so usually just redeploy the previous
  build. If the schema must go back: take a backup, then
  `alembic downgrade <previous_revision>` (inside `railway ssh`) and redeploy the old build.
  Both migrations in this project have tested downgrades.
- **Data mistake** (for example a bulk delete): restore the latest backup into a new database,
  copy the needed rows back, or point `DATABASE_URL` at the restored database.
