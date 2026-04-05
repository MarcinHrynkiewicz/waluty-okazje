import os
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

_DB_URL = os.environ.get("DATABASE_URL", "")
# Render zwraca URL z prefiksem "postgres://", SQLAlchemy wymaga "postgresql://"
if _DB_URL.startswith("postgres://"):
    _DB_URL = _DB_URL.replace("postgres://", "postgresql://", 1)
if not _DB_URL:
    # fallback lokalny (SQLite)
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _DB_URL = f"sqlite:///{os.path.join(_BASE_DIR, 'waluty.db')}"
app.config["SQLALCHEMY_DATABASE_URI"] = _DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class RateHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    currency = db.Column(db.String(8), nullable=False)
    rate = db.Column(db.Float, nullable=False)
    source = db.Column(db.String(32), default="NBP")
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)


class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    currency = db.Column(db.String(8), nullable=False)
    direction = db.Column(db.String(8), nullable=False)   # "below" | "above"
    threshold = db.Column(db.Float, nullable=False)
    email = db.Column(db.String(256), nullable=False)
    active = db.Column(db.Boolean, default=True)
    last_triggered = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False)
    value = db.Column(db.String(512), nullable=False)


# ---------------------------------------------------------------------------
# NBP helpers
# ---------------------------------------------------------------------------

NBP_BASE = "https://api.nbp.pl/api/exchangerates/rates/a"
SUPPORTED = ["USD", "EUR", "GBP", "CHF", "JPY", "CZK", "NOK", "SEK", "DKK", "HUF"]


def fetch_nbp_rate(currency: str) -> float | None:
    url = f"{NBP_BASE}/{currency.lower()}/?format=json"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()["rates"][0]["mid"]
    except Exception as e:
        log.warning("NBP fetch failed for %s: %s", currency, e)
        return None


def get_setting(key: str, default: str = "") -> str:
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default


def set_setting(key: str, value: str):
    s = Settings.query.filter_by(key=key).first()
    if s:
        s.value = value
    else:
        db.session.add(Settings(key=key, value=value))
    db.session.commit()


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_alert_email(alert: Alert, current_rate: float):
    smtp_host = get_setting("smtp_host", os.getenv("SMTP_HOST", ""))
    smtp_port = int(get_setting("smtp_port", os.getenv("SMTP_PORT", "587")))
    smtp_user = get_setting("smtp_user", os.getenv("SMTP_USER", ""))
    smtp_pass = get_setting("smtp_pass", os.getenv("SMTP_PASS", ""))

    if not smtp_host or not smtp_user:
        log.warning("SMTP not configured — skipping email")
        return False

    direction_pl = "poniżej" if alert.direction == "below" else "powyżej"
    subject = f"[Waluty Okazje] Alert: {alert.currency} {direction_pl} {alert.threshold:.4f} PLN"
    body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
    <h2 style="color:#1a73e8">Kurs waluty osiągnął próg alertu!</h2>
    <table style="border-collapse:collapse;width:100%">
      <tr><td style="padding:8px;background:#f5f5f5"><b>Waluta</b></td><td style="padding:8px">{alert.currency} / PLN</td></tr>
      <tr><td style="padding:8px;background:#f5f5f5"><b>Aktualny kurs</b></td><td style="padding:8px;font-size:1.3em;color:#1a73e8"><b>{current_rate:.4f} PLN</b></td></tr>
      <tr><td style="padding:8px;background:#f5f5f5"><b>Twój próg</b></td><td style="padding:8px">{direction_pl} {alert.threshold:.4f} PLN</td></tr>
      <tr><td style="padding:8px;background:#f5f5f5"><b>Czas</b></td><td style="padding:8px">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
    </table>
    <p style="margin-top:20px;color:#666;font-size:0.9em">Źródło: Narodowy Bank Polski (NBP)</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = alert.email
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, alert.email, msg.as_string())
        log.info("Alert email sent to %s for %s", alert.email, alert.currency)
        return True
    except Exception as e:
        log.error("Email send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Scheduler job
# ---------------------------------------------------------------------------

def check_rates():
    with app.app_context():
        log.info("Checking rates...")
        currencies = get_setting("tracked_currencies", ",".join(SUPPORTED[:5])).split(",")
        currencies = [c.strip().upper() for c in currencies if c.strip()]

        rates: dict[str, float] = {}
        for currency in currencies:
            rate = fetch_nbp_rate(currency)
            if rate is None:
                continue
            rates[currency] = rate
            db.session.add(RateHistory(currency=currency, rate=rate, source="NBP"))

        db.session.commit()

        # Cooldown: don't re-trigger same alert within 6 hours
        cooldown = timedelta(hours=6)
        now = datetime.utcnow()

        alerts = Alert.query.filter_by(active=True).all()
        for alert in alerts:
            rate = rates.get(alert.currency)
            if rate is None:
                continue
            triggered = (
                (alert.direction == "below" and rate < alert.threshold) or
                (alert.direction == "above" and rate > alert.threshold)
            )
            if triggered:
                if alert.last_triggered and (now - alert.last_triggered) < cooldown:
                    continue
                if send_alert_email(alert, rate):
                    alert.last_triggered = now
                    db.session.commit()


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", supported=SUPPORTED)


@app.route("/api/rates/current")
def api_current_rates():
    currencies = request.args.get("currencies", ",".join(SUPPORTED[:5])).split(",")
    currencies = [c.strip().upper() for c in currencies if c.strip()]
    result = {}
    for currency in currencies:
        row = (RateHistory.query
               .filter_by(currency=currency)
               .order_by(RateHistory.fetched_at.desc())
               .first())
        if row:
            result[currency] = {"rate": row.rate, "fetched_at": row.fetched_at.isoformat()}
    return jsonify(result)


@app.route("/api/rates/history")
def api_rate_history():
    currency = request.args.get("currency", "EUR").upper()
    hours = int(request.args.get("hours", 48))
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (RateHistory.query
            .filter(RateHistory.currency == currency, RateHistory.fetched_at >= since)
            .order_by(RateHistory.fetched_at.asc())
            .all())
    data = [{"rate": r.rate, "fetched_at": r.fetched_at.isoformat()} for r in rows]
    return jsonify(data)


@app.route("/api/alerts", methods=["GET"])
def api_get_alerts():
    alerts = Alert.query.order_by(Alert.created_at.desc()).all()
    return jsonify([{
        "id": a.id, "currency": a.currency, "direction": a.direction,
        "threshold": a.threshold, "email": a.email, "active": a.active,
        "last_triggered": a.last_triggered.isoformat() if a.last_triggered else None
    } for a in alerts])


@app.route("/api/alerts", methods=["POST"])
def api_create_alert():
    data = request.json
    required = ["currency", "direction", "threshold", "email"]
    if not all(k in data for k in required):
        return jsonify({"error": "Brakujące pola"}), 400
    if data["direction"] not in ("below", "above"):
        return jsonify({"error": "direction musi być 'below' lub 'above'"}), 400
    if data["currency"].upper() not in SUPPORTED:
        return jsonify({"error": f"Nieobsługiwana waluta: {data['currency']}"}), 400

    alert = Alert(
        currency=data["currency"].upper(),
        direction=data["direction"],
        threshold=float(data["threshold"]),
        email=data["email"],
        active=True
    )
    db.session.add(alert)
    db.session.commit()
    return jsonify({"id": alert.id, "message": "Alert utworzony"}), 201


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def api_delete_alert(alert_id):
    alert = Alert.query.get_or_404(alert_id)
    db.session.delete(alert)
    db.session.commit()
    return jsonify({"message": "Alert usunięty"})


@app.route("/api/alerts/<int:alert_id>/toggle", methods=["POST"])
def api_toggle_alert(alert_id):
    alert = Alert.query.get_or_404(alert_id)
    alert.active = not alert.active
    db.session.commit()
    return jsonify({"active": alert.active})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    keys = ["smtp_host", "smtp_port", "smtp_user", "tracked_currencies", "check_interval_minutes"]
    return jsonify({k: get_setting(k) for k in keys})


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.json
    allowed = ["smtp_host", "smtp_port", "smtp_user", "smtp_pass",
               "tracked_currencies", "check_interval_minutes"]
    for key in allowed:
        if key in data:
            set_setting(key, str(data[key]))

    interval = int(get_setting("check_interval_minutes", "30"))
    job = scheduler.get_job("check_rates")
    if job:
        scheduler.reschedule_job("check_rates", trigger="interval", minutes=interval)
        log.info("Scheduler rescheduled to every %d minutes", interval)

    return jsonify({"message": "Ustawienia zapisane"})


@app.route("/api/check_now", methods=["POST"])
def api_check_now():
    check_rates()
    return jsonify({"message": "Sprawdzono kursy"})


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(timezone="Europe/Warsaw")


def init_app():
    with app.app_context():
        db.create_all()
        if not get_setting("tracked_currencies"):
            set_setting("tracked_currencies", "USD,EUR,GBP,CHF")
        if not get_setting("check_interval_minutes"):
            set_setting("check_interval_minutes", "30")
        if not get_setting("smtp_port"):
            set_setting("smtp_port", "587")
        interval = int(get_setting("check_interval_minutes") or 30)

    if not scheduler.running:
        scheduler.add_job(check_rates, "interval", minutes=interval, id="check_rates",
                          next_run_time=datetime.now())
        scheduler.start()
        log.info("Scheduler started, interval=%d min", interval)


# Initialise on import so gunicorn workers pick it up
init_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
