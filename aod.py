import os
import re
import time
import sqlite3
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    import pgeocode
except Exception:
    pgeocode = None


# ============================================================
# ENV / CONFIG
# ============================================================

load_dotenv()

AOD_USERNAME = os.getenv("AOD_USERNAME", "").strip()
AOD_PASSWORD = os.getenv("AOD_PASSWORD", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2"))
POST_EXISTING_ON_START = os.getenv("POST_EXISTING_ON_START", "false").strip().lower() in {
    "1", "true", "yes", "y"
}
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
LOGIN_RETRY_DELAY = int(os.getenv("LOGIN_RETRY_DELAY", "60"))

LOGIN_BASE = "https://apt1.activeaero.com"
LOGIN_FORM_URL = f"{LOGIN_BASE}/Login/LoginForm.cfm"
LOGIN_ACTION_URL = f"{LOGIN_BASE}/Login/LoginAction.cfm"
DB_FILE = os.getenv("DB_FILE", "aod_bot.db")

if not AOD_USERNAME or not AOD_PASSWORD:
    raise SystemExit("AOD_USERNAME and AOD_PASSWORD are required")
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AOD_BOT")


# ============================================================
# ZIP -> CITY, STATE
# ============================================================

_zip_cache: dict[str, str] = {}
_zip_lookup = None


def _get_zip_lookup():
    global _zip_lookup
    if _zip_lookup is not None:
        return _zip_lookup
    if pgeocode is None:
        return None
    try:
        _zip_lookup = pgeocode.Nominatim("us")
    except Exception as exc:
        logger.warning("ZIP lookup initialization failed: %s", exc)
        _zip_lookup = False
    return _zip_lookup if _zip_lookup is not False else None


def zip_to_city_state(zip_code: str) -> str:
    zip_code = str(zip_code or "").strip()
    if not zip_code:
        return ""
    if zip_code in _zip_cache:
        return _zip_cache[zip_code]

    lookup = _get_zip_lookup()
    if lookup is None:
        return zip_code

    try:
        result = lookup.query_postal_code(zip_code)
        city = str(getattr(result, "place_name", "") or "").strip()
        state = str(getattr(result, "state_code", "") or "").strip()
        if city.lower() == "nan":
            city = ""
        if state.lower() == "nan":
            state = ""
        value = f"{city}, {state}" if city and state else (city or zip_code)
        _zip_cache[zip_code] = value
        return value
    except Exception as exc:
        logger.warning("ZIP lookup failed for %s: %s", zip_code, exc)
        return zip_code


# ============================================================
# DATABASE
# ============================================================


def db_connect():
    return sqlite3.connect(DB_FILE, timeout=10)


def init_database():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shipments (
                shipment_id TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                telegram_sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def shipment_status(shipment_id: str):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT telegram_sent FROM shipments WHERE shipment_id = ?",
            (shipment_id,),
        ).fetchone()
    return None if row is None else int(row[0])


def upsert_shipment(shipment_id: str, telegram_sent: bool):
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO shipments (shipment_id, first_seen, telegram_sent)
            VALUES (?, ?, ?)
            ON CONFLICT(shipment_id) DO UPDATE SET telegram_sent = excluded.telegram_sent
            """,
            (shipment_id, now, 1 if telegram_sent else 0),
        )
        conn.commit()


# ============================================================
# HTTP SESSION
# ============================================================


def create_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/153.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return session


# ============================================================
# AOD CLIENT
# ============================================================


class AODClient:
    def __init__(self):
        self.session = create_session()
        self.logged_in = False
        self.admin_url = None
        self.board_base_url = None
        self.rfq_url = None

    @staticmethod
    def is_login_page(response: requests.Response) -> bool:
        if "loginform.cfm" in response.url.lower():
            return True
        text = response.text.lower()
        return 'name="userloginid"' in text and 'name="password"' in text

    def open_login_page(self):
        logger.info("Opening AOD login page...")
        response = self.session.get(LOGIN_FORM_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.info("Login page status: %s", response.status_code)
        return response

    def submit_login(self):
        return self.session.post(
            LOGIN_ACTION_URL,
            data={
                "locale": "en",
                "languageId": "1",
                "UserLoginID": AOD_USERNAME,
                "Password": AOD_PASSWORD,
            },
            headers={
                "Referer": LOGIN_FORM_URL,
                "Origin": LOGIN_BASE,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )

    def force_old_session_logout(self, location: str):
        logger.warning("Existing AOD session detected.")
        message_url = urljoin(LOGIN_ACTION_URL, location)
        logger.info("Opening session-conflict page...")
        response = self.session.get(message_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        force_form = None
        for form in soup.find_all("form"):
            if "forcelogout.cfm" in form.get("action", "").lower():
                force_form = form
                break
        if force_form is None:
            raise RuntimeError("message=8 detected but ForceLogout form was not found")

        force_data = {}
        for inp in force_form.find_all("input"):
            name = inp.get("name")
            if name:
                force_data[name] = inp.get("value", "")

        force_url = urljoin(message_url, force_form.get("action"))
        logger.info("Forcing old AOD session logout...")
        force_response = self.session.post(
            force_url,
            data=force_data,
            headers={
                "Referer": message_url,
                "Origin": LOGIN_BASE,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        force_response.raise_for_status()
        logger.info("ForceLogout status: %s", force_response.status_code)

    def login(self):
        self.logged_in = False
        self.session.close()
        self.session = create_session()

        self.open_login_page()
        logger.info("Logging into AOD...")
        response = self.submit_login()
        location = response.headers.get("Location", "")
        logger.info("Login response: %s", response.status_code)
        logger.info("Login redirect: %s", location)

        if "message=8" in location.lower():
            self.force_old_session_logout(location)
            logger.info("Creating new clean session...")
            self.session.close()
            self.session = create_session()
            time.sleep(1)
            self.open_login_page()
            logger.info("Retrying AOD login...")
            response = self.submit_login()
            location = response.headers.get("Location", "")
            logger.info("Retry login response: %s", response.status_code)
            logger.info("Retry login redirect: %s", location)

        if response.status_code not in (301, 302, 303):
            raise RuntimeError("AOD login did not return a redirect")
        if "admin/index.cfm" not in location.lower():
            raise RuntimeError(f"AOD login failed. Redirect: {location}")

        logger.info("AOD LOGIN SUCCESS")
        admin_url = urljoin(LOGIN_ACTION_URL, location)
        logger.info("Opening AOD admin...")
        admin_response = self.session.get(
            admin_url, timeout=REQUEST_TIMEOUT, allow_redirects=True
        )
        admin_response.raise_for_status()
        if self.is_login_page(admin_response):
            raise RuntimeError("Admin redirected back to login")

        self.admin_url = admin_response.url
        logger.info("Admin final URL: %s", self.admin_url)
        parsed = urlparse(self.admin_url)
        self.board_base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.rfq_url = f"{self.board_base_url}/Provider/statusboard/StatusBoard.cfm"
        self.logged_in = True

    def get_rfq_board(self):
        if not self.logged_in:
            self.login()

        params = {
            "selectedTab": "rfqDIV",
            "showGroundAddresses": "false",
        }
        response = self.session.get(
            self.rfq_url,
            params=params,
            headers={"Referer": self.admin_url},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        if self.is_login_page(response):
            logger.warning("AOD session expired; logging in again...")
            self.logged_in = False
            self.login()
            response = self.session.get(
                self.rfq_url,
                params=params,
                headers={"Referer": self.admin_url},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

        response.raise_for_status()
        if self.is_login_page(response):
            raise RuntimeError("RFQ board returned login page after re-login")
        return response


# ============================================================
# PARSER
# ============================================================


def clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_header(header: str) -> str:
    header = clean_text(header).lower()
    header = re.sub(r"\(.*?\)", "", header)
    return clean_text(header)


def parse_rfq_board(html_text: str, board_base_url: str):
    soup = BeautifulSoup(html_text, "html.parser")
    shipments = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_row = None
        headers = []
        for row in rows:
            cells = row.find_all(["th", "td"])
            texts = [normalize_header(c.get_text(" ", strip=True)) for c in cells]
            if "shipment" in texts and ("origin" in texts or "dest" in texts):
                header_row = row
                headers = texts
                break
        if header_row is None:
            continue

        for row in rows[rows.index(header_row) + 1 :]:
            cells = row.find_all("td")
            if not cells:
                continue
            values = [clean_text(c.get_text(" ", strip=True)) for c in cells]
            row_data = {
                header: values[i]
                for i, header in enumerate(headers)
                if i < len(values)
            }

            shipment_match = re.search(r"\b\d{6,10}\b", row_data.get("shipment", ""))
            if not shipment_match:
                shipment_match = re.search(
                    r"\b8\d{6}\b", clean_text(row.get_text(" ", strip=True))
                )
            if not shipment_match:
                continue
            shipment_id = shipment_match.group(0)

            shipment_link = ""
            for anchor in row.find_all("a", href=True):
                if shipment_id in clean_text(anchor.get_text(" ", strip=True)):
                    shipment_link = urljoin(board_base_url, anchor["href"])
                    break

            shipments.append(
                {
                    "shipment_id": shipment_id,
                    "mode": row_data.get("mode", ""),
                    "rfq_date": row_data.get("rfq date", ""),
                    "origin": row_data.get("origin", ""),
                    "ready_time": row_data.get("ready time", ""),
                    "destination": row_data.get("dest", "")
                    or row_data.get("destination", ""),
                    "need_time": row_data.get("need time", ""),
                    "containers": row_data.get("containers", ""),
                    "weight": row_data.get("weight", ""),
                    "last_bid": row_data.get("last bid", ""),
                    "csr": row_data.get("csr", ""),
                    "link": shipment_link,
                }
            )

    unique = {item["shipment_id"]: item for item in shipments}
    return list(unique.values())


# ============================================================
# TELEGRAM
# ============================================================


def escape_html(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_load_message(load: dict) -> str:
    shipment = escape_html(load.get("shipment_id", ""))
    mode = escape_html(load.get("mode", ""))
    origin = escape_html(zip_to_city_state(load.get("origin", "")))
    destination = escape_html(zip_to_city_state(load.get("destination", "")))
    ready = escape_html(load.get("ready_time", ""))
    need = escape_html(load.get("need_time", ""))
    weight = escape_html(load.get("weight", ""))
    containers = escape_html(load.get("containers", ""))
    last_bid = escape_html(load.get("last_bid", ""))
    csr = escape_html(load.get("csr", ""))

    lines = [
        "🚨 <b>AOD NEW LOAD</b>",
        "",
        f"🆔 <b>Shipment:</b> <code>{shipment}</code>",
    ]
    if mode:
        lines.append(f"🚛 <b>Mode:</b> {mode}")

    lines.extend(["", "🟢 <b>PICK UP</b>"])
    if origin:
        lines.append(f"📍 {origin}")
    if ready:
        lines.append(f"🕐 {ready}")

    lines.extend(["", "🔴 <b>DELIVERY</b>"])
    if destination:
        lines.append(f"📍 {destination}")
    if need:
        lines.append(f"🕒 {need}")

    lines.append("")
    if weight:
        lines.append(f"⚖️ <b>Weight:</b> {weight}")
    if containers:
        lines.append(f"📦 <b>Containers:</b> {containers}")
    if last_bid:
        if last_bid.upper() == "DECLINE":
            lines.append("💰 <b>Last Bid:</b> ❌ DECLINE")
        else:
            lines.append(f"💰 <b>Last Bid:</b> {last_bid}")
    if csr:
        lines.append(f"👤 <b>CSR:</b> {csr}")

    lines.extend(["", "⚡ <i>New load detected by AOD Bot</i>"])
    return "\n".join(lines)


def send_telegram(load: dict) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": format_load_message(load),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if load.get("link"):
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "🔗 OPEN AOD", "url": load["link"]}]]
        }

    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Telegram network error: %s", exc)
        return False

    if not response.ok:
        logger.error(
            "Telegram API error %s: %s", response.status_code, response.text[:500]
        )
        return False

    logger.info("Telegram sent | Shipment %s", load.get("shipment_id"))
    return True


# ============================================================
# SHIPMENT PROCESSING
# ============================================================


def initialize_baseline(shipments):
    if POST_EXISTING_ON_START:
        logger.warning("Existing loads WILL be posted on startup")
        return

    ignored = 0
    for load in shipments:
        sid = load["shipment_id"]
        if shipment_status(sid) is None:
            upsert_shipment(sid, telegram_sent=True)
            ignored += 1
    logger.info("Baseline complete. %s existing load(s) ignored.", ignored)


def process_shipments(shipments, first_run: bool):
    if first_run:
        initialize_baseline(shipments)
        if not POST_EXISTING_ON_START:
            return

    for load in shipments:
        sid = load.get("shipment_id")
        if not sid:
            continue

        status = shipment_status(sid)
        if status == 1:
            continue

        if status is None:
            logger.info(
                "NEW LOAD | %s | %s -> %s",
                sid,
                zip_to_city_state(load.get("origin", "")),
                zip_to_city_state(load.get("destination", "")),
            )

        if send_telegram(load):
            upsert_shipment(sid, telegram_sent=True)
        else:
            upsert_shipment(sid, telegram_sent=False)


# ============================================================
# MAIN
# ============================================================


def main():
    print("=" * 65)
    print("AOD DIRECT LOAD BOARD -> TELEGRAM BOT")
    print("=" * 65)

    init_database()
    client = AODClient()

    logger.info("Starting AOD Bot...")
    logger.info("Polling interval: %.2f sec", POLL_INTERVAL)
    logger.info("Telegram: ENABLED")
    logger.info(
        "Existing loads %s on startup",
        "WILL BE POSTED" if POST_EXISTING_ON_START else "will be ignored",
    )

    first_run = True
    last_count = None

    while True:
        started = time.monotonic()
        try:
            response = client.get_rfq_board()
            shipments = parse_rfq_board(response.text, client.board_base_url)
            current_count = len(shipments)

            if current_count != last_count:
                logger.info("RFQ Board: %s load(s)", current_count)
                last_count = current_count

            process_shipments(shipments, first_run=first_run)
            first_run = False

        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break
        except requests.RequestException as exc:
            logger.error("Network error: %s", exc)
            client.logged_in = False
            time.sleep(5)
        except RuntimeError as exc:
            logger.error("AOD error: %s", exc)
            client.logged_in = False
            logger.warning("Waiting %s sec before login retry...", LOGIN_RETRY_DELAY)
            time.sleep(LOGIN_RETRY_DELAY)
        except Exception as exc:
            logger.exception("Unexpected error: %s", exc)
            client.logged_in = False
            time.sleep(10)

        elapsed = time.monotonic() - started
        time.sleep(max(0.1, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
