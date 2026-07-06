"""
Argus Status Page — Production-ready Flask application.

Monitors HTTP endpoints and TCP sockets defined in service_list.json,
exposes a live dashboard and JSON API, and persists check history to disk.
"""

import json
import logging
import os
import signal
import socket
import sys
import time
import threading
from datetime import datetime, timezone
from functools import wraps

import requests as http_requests
from flask import Flask, jsonify, render_template, request, Response

# ── .env support (optional, no hard crash if missing) ─────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("argus")

# ── Configuration ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICE_FILE      = os.getenv("SERVICE_FILE", os.path.join(BASE_DIR, "service_list.json"))
HISTORY_FILE      = os.getenv("HISTORY_FILE", os.path.join(BASE_DIR, "status_history.json"))
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", 30))
REQUEST_TIMEOUT   = int(os.getenv("REQUEST_TIMEOUT", 5))
MAX_HISTORY       = int(os.getenv("MAX_HISTORY", 90))
PORT              = int(os.getenv("PORT", 5000))
HOST              = os.getenv("HOST", "0.0.0.0")
SITE_NAME         = os.getenv("SITE_NAME", "Status Page")
SITE_DESCRIPTION  = os.getenv("SITE_DESCRIPTION", "Real-time service status monitor")
REFRESH_API_KEY   = os.getenv("REFRESH_API_KEY", "")  # optional auth for /api/refresh
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)



# ── Security middleware ───────────────────────────────────────────────────────
@app.after_request
def apply_security_headers(response: Response) -> Response:
    """Inject production security headers on every response."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

    # Cache-control: static assets are cached, HTML/API are not
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        response.headers["Cache-Control"] = "no-store"

    return response


# ── Rate-limiting helper (simple in-memory, per-IP) ──────────────────────────
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()


def rate_limit(max_calls: int = 5, window_seconds: int = 60):
    """Decorator that rate-limits a route per client IP."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            now = time.time()
            with _rate_limit_lock:
                calls = _rate_limit_store.setdefault(ip, [])
                # Prune old entries
                calls[:] = [t for t in calls if t > now - window_seconds]
                if len(calls) >= max_calls:
                    return jsonify({"error": "rate limit exceeded"}), 429
                calls.append(now)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ── In-memory status store ────────────────────────────────────────────────────
status_store: dict[str, dict] = {}
store_lock = threading.Lock()
_shutdown_event = threading.Event()


# ── Service-list loading & validation ─────────────────────────────────────────
REQUIRED_FIELDS = {"name"}
TARGET_FIELDS   = {"url", "host"}


def validate_service(entry: dict, index: int) -> list[str]:
    """Return a list of validation errors for a single service entry."""
    errors: list[str] = []
    if not isinstance(entry, dict):
        return [f"Entry {index}: must be a JSON object, got {type(entry).__name__}"]
    if "name" not in entry:
        errors.append(f"Entry {index}: missing required field 'name'")
    if "url" not in entry and "host" not in entry:
        errors.append(f"Entry {index} ({entry.get('name', '?')}): must have either 'url' or 'host'")
    if "host" in entry and "url" not in entry and "port" not in entry:
        errors.append(f"Entry {index} ({entry.get('name', '?')}): 'host' without 'url' requires 'port'")
    if "port" in entry:
        try:
            p = int(entry["port"])
            if not (1 <= p <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            errors.append(f"Entry {index} ({entry.get('name', '?')}): 'port' must be 1-65535")
    return errors


def load_services() -> list[dict]:
    """Load and validate the service list from disk."""
    try:
        with open(SERVICE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.error("Service file not found: %s", SERVICE_FILE)
        return []
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in %s: %s", SERVICE_FILE, exc)
        return []

    if not isinstance(data, list):
        logger.error("%s must contain a JSON array at the top level.", SERVICE_FILE)
        return []

    all_errors: list[str] = []
    for i, entry in enumerate(data):
        all_errors.extend(validate_service(entry, i))

    if all_errors:
        for err in all_errors:
            logger.warning("Service config: %s", err)
        # Still return the entries that parsed correctly
        return [e for e in data if isinstance(e, dict) and "name" in e]

    return data


# ── History persistence ───────────────────────────────────────────────────────

def load_status_history() -> dict[str, list[bool]]:
    """Load past check history from disk."""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load history file: %s", exc)
        return {}


def save_status_history(history: dict[str, list[bool]]) -> None:
    """Atomically write history to disk (write-then-rename)."""
    tmp = HISTORY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(history, fh)
        # Atomic rename (works on POSIX; on Windows it replaces)
        os.replace(tmp, HISTORY_FILE)
    except OSError as exc:
        logger.error("Could not save history file: %s", exc)


# ── Notifications (Discord Webhooks) ──────────────────────────────────────────

def send_discord_alert(service_name: str, target: str, status_changed_to: str, details: str) -> None:
    """Send an embed notification to Discord via Webhook in a background thread."""
    if not DISCORD_WEBHOOK_URL:
        return

    def _post():
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        is_online = status_changed_to.lower() == "online"
        
        # Color codes: Red for offline, Green for online
        color = 3066993 if is_online else 15158332
        emoji = "🟢" if is_online else "🔴"
        title = f"{emoji} Service Status Changed: {service_name}"
        
        embed = {
            "title": title,
            "description": f"Service **{service_name}** is now **{status_changed_to.upper()}**.",
            "color": color,
            "fields": [
                {"name": "Target", "value": f"`{target}`", "inline": True},
                {"name": "Details", "value": details, "inline": True},
            ],
            "footer": {
                "text": f"Argus Monitor • {now_str}"
            }
        }
        
        payload = {
            "embeds": [embed]
        }
        
        try:
            resp = http_requests.post(
                DISCORD_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=5
            )
            if not resp.ok:
                logger.error("Discord Webhook API returned status %d: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.error("Failed to send Discord alert for '%s': %s", service_name, exc)

    threading.Thread(target=_post, daemon=True).start()


# ── Health-check functions ────────────────────────────────────────────────────


def check_http(url: str) -> tuple[bool, int | None, str]:
    """
    Perform an HTTP HEAD request.
    Returns (online, status_code, message).
    """
    try:
        resp = http_requests.head(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": "Argus-StatusPage/1.0"},
        )
        if resp.status_code >= 500:
            return False, resp.status_code, f"HTTP {resp.status_code}"
        return True, resp.status_code, f"HTTP {resp.status_code}"
    except http_requests.exceptions.SSLError:
        return False, None, "SSL certificate error"
    except http_requests.exceptions.ConnectionError:
        return False, None, "Connection refused"
    except http_requests.exceptions.Timeout:
        return False, None, "Request timed out"
    except http_requests.exceptions.RequestException as exc:
        return False, None, str(exc)[:120]


def check_tcp(host: str, port: int) -> tuple[bool, str]:
    """
    Open a raw TCP socket.
    Returns (online, message).
    """
    try:
        with socket.create_connection((host, port), timeout=REQUEST_TIMEOUT):
            return True, f"TCP OK on port {port}"
    except socket.timeout:
        return False, f"TCP timed out on port {port}"
    except ConnectionRefusedError:
        return False, f"Connection refused on port {port}"
    except OSError as exc:
        return False, str(exc)[:120]


def resolve_service_target(service: dict) -> tuple[str, str]:
    """Decide check method + human-readable target string."""
    if "url" in service:
        return "http", service["url"]
    if "host" in service and "port" in service:
        return "tcp", f"{service['host']}:{service['port']}"
    if "host" in service:
        return "http", f"http://{service['host']}"
    return "unknown", "N/A"


def check_service(service: dict) -> dict:
    """Run the appropriate check and return a result dict."""
    method, target = resolve_service_target(service)
    start = time.perf_counter()
    online = False
    status_code = None
    message = "Unknown target"

    if method == "http":
        online, status_code, message = check_http(target)
    elif method == "tcp":
        online, message = check_tcp(service["host"], int(service["port"]))
    else:
        message = "No URL or host configured"

    latency_ms = round((time.perf_counter() - start) * 1000, 1)

    return {
        "name": service.get("name", "Unnamed Service"),
        "description": service.get("description", ""),
        "target": target,
        "method": method.upper(),
        "online": online,
        "status_code": status_code,
        "message": message,
        "latency_ms": latency_ms,
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }


# ── Check orchestration ──────────────────────────────────────────────────────

def run_checks() -> None:
    """Load services, check them all, update store + history."""
    services = load_services()
    if not services:
        logger.warning("No valid services to check.")
        return

    results: dict[str, dict] = {}
    history = load_status_history()

    for service in services:
        name = service.get("name", "Unnamed Service")
        try:
            result = check_service(service)
        except Exception:
            logger.exception("Unhandled error checking service '%s'", name)
            result = {
                "name": name,
                "description": service.get("description", ""),
                "target": "N/A",
                "method": "N/A",
                "online": False,
                "status_code": None,
                "message": "Internal check error",
                "latency_ms": None,
                "last_checked": datetime.now(timezone.utc).isoformat(),
            }

        # Check status transition to send Discord alerts
        with store_lock:
            previous_status = status_store.get(name)

        if previous_status is not None:
            prev_online = previous_status.get("online", True)
            curr_online = result["online"]
            if prev_online != curr_online:
                status_str = "online" if curr_online else "offline"
                details = result.get("message", "No details provided.")
                if curr_online and result.get("latency_ms") is not None:
                    details += f" (Latency: {result['latency_ms']}ms)"
                send_discord_alert(name, result["target"], status_str, details)

        # Append to rolling history
        svc_hist = history.get(name, [])
        svc_hist.append(result["online"])
        if len(svc_hist) > MAX_HISTORY:
            svc_hist = svc_hist[-MAX_HISTORY:]
        history[name] = svc_hist
        result["history"] = list(svc_hist)  # copy for template

        up_count = sum(1 for v in svc_hist if v)
        result["uptime_pct"] = round((up_count / len(svc_hist)) * 100, 2) if svc_hist else 100.0

        results[name] = result

    save_status_history(history)

    with store_lock:
        status_store.clear()
        status_store.update(results)

    logger.info("Checked %d service(s).", len(results))



def background_checker() -> None:
    """Daemon loop — runs checks on a fixed interval until shutdown."""
    while not _shutdown_event.is_set():
        try:
            run_checks()
        except Exception:
            logger.exception("Background checker error")
        _shutdown_event.wait(CHECK_INTERVAL)


# ── Routes ────────────────────────────────────────────────────────────────────

def _compute_overall(services: list[dict]) -> tuple[str, int, int]:
    """Return (overall_status, online_count, total)."""
    total = len(services)
    online_count = sum(1 for s in services if s["online"])
    if total == 0:
        return "no_data", 0, 0
    if online_count == total:
        return "operational", online_count, total
    if online_count > 0:
        return "degraded", online_count, total
    return "major_outage", online_count, total


@app.route("/")
def index():
    with store_lock:
        services = list(status_store.values())

    filtered_services = []
    for s in services:
        filtered_services.append({
            "name": s["name"],
            "description": s["description"],
            "online": s["online"],
            "latency_ms": s["latency_ms"],
            "history": s["history"],
            "uptime_pct": s["uptime_pct"],
            "last_checked": s["last_checked"]
        })

    total = len(filtered_services)
    online_count = sum(1 for s in filtered_services if s["online"])
    all_ok = total > 0 and online_count == total
    has_issues = 0 < online_count < total
    all_down = total > 0 and online_count == 0

    overall = "operational" if all_ok else ("degraded" if has_issues else ("major_outage" if all_down else "no_data"))

    return render_template(
        "index.html",
        services=filtered_services,
        overall=overall,
        online_count=online_count,
        total=total,
        site_name=SITE_NAME,
        site_description=SITE_DESCRIPTION,
        check_interval=CHECK_INTERVAL,
    )


@app.route("/health")
def health():
    """Liveness / readiness probe for container orchestrators."""
    with store_lock:
        services = list(status_store.values())
    return jsonify({
        "status": "healthy",
        "services_loaded": len(services),
        "uptime": time.process_time(),
    }), 200


@app.route("/api/status")
def api_status():
    """Public JSON API — returns only current status of services."""
    with store_lock:
        services = list(status_store.values())

    status_map = {s["name"]: "operational" if s["online"] else "outage" for s in services}
    return jsonify(status_map)


@app.route("/api/refresh", methods=["POST"])
@rate_limit(max_calls=3, window_seconds=60)
def api_refresh():
    """Trigger an immediate re-check. Optionally protected by API key."""
    if REFRESH_API_KEY:
        provided = request.headers.get("X-API-Key", "")
        if provided != REFRESH_API_KEY:
            return jsonify({"error": "unauthorized"}), 401
    threading.Thread(target=run_checks, daemon=True).start()
    return jsonify({"status": "refresh triggered"}), 202


# ── Startup & shutdown ────────────────────────────────────────────────────────

def _graceful_shutdown(signum, frame):
    """Signal handler for clean exit."""
    sig_name = signal.Signals(signum).name
    logger.info("Received %s — shutting down gracefully…", sig_name)
    _shutdown_event.set()
    sys.exit(0)


def start_background_checker() -> threading.Thread:
    """Launch the background checker daemon thread."""
    t = threading.Thread(target=background_checker, daemon=True, name="argus-checker")
    t.start()
    return t


def create_app() -> Flask:
    """
    Application factory — used by WSGI servers (waitress / gunicorn).

    Usage:
        waitress-serve --port=5000 app:create_app
        gunicorn 'app:create_app()' -b 0.0.0.0:5000
    """
    logger.info("Running initial service checks…")
    run_checks()
    start_background_checker()
    logger.info("Argus Status Page ready.")
    return app


# ── Direct execution (development) ───────────────────────────────────────────
if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    logger.info("Running initial service checks…")
    run_checks()
    start_background_checker()

    # Use waitress if available, otherwise fall back to Flask dev server
    try:
        from waitress import serve as waitress_serve
        logger.info("Starting Argus (waitress) on http://%s:%d", HOST, PORT)
        waitress_serve(app, host=HOST, port=PORT, threads=4)
    except ImportError:
        logger.warning(
            "waitress not installed — falling back to Flask dev server. "
            "Do NOT use this in production. Install waitress: pip install waitress"
        )
        app.run(host=HOST, port=PORT, debug=False)
