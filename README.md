# Argus Status Page 🛡️

A clean, self-hosted status page built with **Flask**. Define your services in `service_list.json` — Argus pings them on a configurable interval and renders a live Cloudflare-style dashboard with 90-point uptime timelines.

![Status Page Preview](static/img/logo.png)

---

## ✨ Features

| Feature | Description |
|---|---|
| **HTTP & TCP monitoring** | Point at a URL _or_ any IP:port combination |
| **90-point uptime timeline** | Cloudflare-style coloured tick bars per service |
| **Auto-refresh** | Page reloads automatically after each check cycle |
| **JSON API** | `GET /api/status` for bots, webhooks, dashboards |
| **Health endpoint** | `GET /health` for container orchestration probes |
| **Security headers** | X-Frame-Options, CSP hints, referrer policy |
| **Rate limiting** | Built-in per-IP rate limiting on the refresh API |
| **API key auth** | Optional `REFRESH_API_KEY` to protect `/api/refresh` |
| **Persistent history** | Atomic writes to `status_history.json` (survives restarts) |
| **Production WSGI** | Ships with [waitress](https://docs.pylonsproject.org/projects/waitress/) — no nginx needed for small deploys |
| **Docker-ready** | `Dockerfile` + `docker-compose.yml` included |
| **Zero database** | All config lives in one JSON file |

---

## 🚀 Quick Start

### 1. Clone

```bash
git clone https://github.com/qwertyguy16/Argus-Group-Status-Page.git
cd Argus-Group-Status-Page
```

### 2. Install

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure services

Edit **`service_list.json`** — see [Configuration](#%EF%B8%8F-configuration) below.

### 4. Run

```bash
python app.py
```

Argus auto-detects [waitress](https://docs.pylonsproject.org/projects/waitress/) and uses it as the WSGI server. If waitress is not installed it falls back to the Flask dev server with a warning.

Open **[http://localhost:5000](http://localhost:5000)** 🎉

---

## ⚙️ Configuration

### `service_list.json`

A JSON array where each object is one monitored service.

#### Monitor a URL (HTTP / HTTPS)

```json
{
  "name": "Main Website",
  "url": "https://example.com",
  "description": "Public-facing website"
}
```

Argus sends an HTTP `HEAD` request and marks the service **online** if the response status is < 500.

#### Monitor a host + port (TCP)

```json
{
  "name": "Game Server",
  "host": "game.example.com",
  "port": 25565,
  "description": "Minecraft game server"
}
```

Argus opens a raw TCP socket and marks the service **online** if the connection succeeds within the timeout.

#### Full example

```json
[
  {
    "name": "Main Website",
    "url": "https://example.com",
    "description": "Primary public-facing website"
  },
  {
    "name": "API Server",
    "host": "192.168.1.10",
    "port": 8080,
    "description": "Core REST API"
  },
  {
    "name": "Game Server",
    "host": "game.example.com",
    "port": 25565,
    "description": "Minecraft game server"
  }
]
```

#### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | ✅ | Display name shown on the dashboard |
| `url` | string | ✅ (or `host`) | Full URL — checked via HTTP HEAD |
| `host` | string | ✅ (or `url`) | Hostname or IP for TCP check |
| `port` | integer | Required with `host` | TCP port (1–65 535) |
| `description` | string | ❌ | Subtitle shown on the service card |

> **Note:** If both `url` and `host` are provided, `url` takes priority.

---

### Environment Variables

Copy `.env.example` → `.env` and adjust as needed. All variables have sensible defaults.

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `5000` | Bind port |
| `SITE_NAME` | `Status Page` | Header title |
| `SITE_DESCRIPTION` | `Real-time service status monitor` | Header subtitle |
| `CHECK_INTERVAL` | `30` | Seconds between check rounds |
| `REQUEST_TIMEOUT` | `5` | Per-request HTTP / TCP timeout (s) |
| `MAX_HISTORY` | `90` | Number of historical checks stored per service |
| `SERVICE_FILE` | `./service_list.json` | Path to the service definition file |
| `HISTORY_FILE` | `./status_history.json` | Path to the persistent history file |
| `REFRESH_API_KEY` | _(empty)_ | If set, `POST /api/refresh` requires `X-API-Key` header |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 🔌 API Reference

### `GET /health`

Liveness / readiness probe for container orchestrators.

```json
{ "status": "healthy", "services_loaded": 3, "uptime": 42.7 }
```

### `GET /api/status`

Simple status payload. Returns a dictionary mapping service names to their status (`"operational"` or `"outage"`).

```json
{
  "Main Website": "operational",
  "API Server": "outage",
  "Game Server": "outage"
}
```

### `POST /api/refresh`

Triggers an immediate re-check (returns `202 Accepted`).  
Rate-limited to 3 calls / 60 s per IP.

If `REFRESH_API_KEY` is set:

```bash
curl -X POST http://localhost:5000/api/refresh \
     -H "X-API-Key: your-secret-key"
```



---

## 🖥️ Production Deployment (bare-metal / VM)

```bash
# 1. Clone & install
git clone https://github.com/qwertyguy16/Argus-Group-Status-Page.git
cd Argus-Group-Status-Page
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
nano .env
nano service_list.json

# 3. Run with waitress (production WSGI)
python app.py
```

Argus ships with **waitress** as a production WSGI server. For high-traffic deployments behind a reverse proxy:

### With gunicorn (Linux)

```bash
pip install gunicorn
gunicorn 'app:create_app()' -b 0.0.0.0:5000 -w 2 --threads 4
```

### Systemd service

```ini
# /etc/systemd/system/argus.service
[Unit]
Description=Argus Status Page
After=network.target

[Service]
Type=simple
User=argus
WorkingDirectory=/opt/argus
EnvironmentFile=/opt/argus/.env
ExecStart=/opt/argus/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now argus
```

### Nginx reverse proxy

```nginx
server {
    listen 80;
    server_name status.example.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## 📁 Project Structure

```
Argus-Group-Status-Page/
├── app.py                  # Flask app + background checker + WSGI factory
├── service_list.json       # ← Define your services here
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── .gitignore
├── LICENSE
├── templates/
│   └── index.html          # Jinja2 dashboard template
└── static/
    ├── css/style.css        # Dark-theme stylesheet
    ├── js/main.js           # Auto-refresh + countdown
    └── img/logo.png         # Site logo / favicon
```

---

## 🔒 Security Notes

- **Security headers** are injected on every response (X-Frame-Options, X-Content-Type-Options, etc.)
- **Rate limiting** protects the `/api/refresh` endpoint (3 req / 60 s per IP)
- **API key auth** can be enabled for `/api/refresh` via `REFRESH_API_KEY`
- Static assets are served with `Cache-Control: public, max-age=3600`; dynamic routes use `no-store`
- History writes are **atomic** (write-to-temp then rename) to prevent corruption on crash

---

## 📜 License

MIT — free for personal and commercial use. See [LICENSE](LICENSE).
