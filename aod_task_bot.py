import os
import re
import time
import json
import sqlite3
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

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
LOGIN_RETRY_DELAY = int(os.getenv("LOGIN_RETRY_DELAY", "30"))
DB_FILE = os.getenv("DB_FILE", "aod_task_bot.db")

LOGIN_BASE = "https://apt1.activeaero.com"
LOGIN_FORM_URL = LOGIN_BASE + "/Login/LoginForm.cfm"
LOGIN_ACTION_URL = LOGIN_BASE + "/Login/LoginAction.cfm"

ALLOWED_TASK_TYPES = {"TRACKING", "SHIPPER", "RECEIVER", "NOTE"}
TASK_MENTIONS = "@molly_phnx @RamonPNM @Mark_update @renzo_rzz"

if not AOD_USERNAME:
    raise SystemExit("ERROR: AOD_USERNAME missing")
if not AOD_PASSWORD:
    raise SystemExit("ERROR: AOD_PASSWORD missing")
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("ERROR: TELEGRAM_BOT_TOKEN missing")
if not TELEGRAM_CHAT_ID:
    raise SystemExit("ERROR: TELEGRAM_CHAT_ID missing")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AOD_TASK_BOT")

IGNORED_TASK_CACHE = set()
NOTE_RETRY_CACHE = {}


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def escape_html(value):
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def normalize_label(value):
    return clean_text(value).replace(":", "").strip().upper()


def task_is_allowed(task_name):
    return clean_text(task_name).upper() in ALLOWED_TASK_TYPES


def db_connect():
    return sqlite3.connect(DB_FILE, timeout=10)


def init_database():
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_key TEXT PRIMARY KEY,
            shipment_id TEXT,
            task_name TEXT,
            telegram_message_id INTEGER,
            active INTEGER DEFAULT 1,
            first_seen TEXT,
            last_seen TEXT,
            last_snapshot TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_task(task_key):
    conn = db_connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM tasks WHERE task_key = ?",
        (task_key,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def snapshot_dict(task):
    return {
        "due": task.get("due", ""),
        "info": task.get("info", {}),
        "note": task.get("note_data", {}),
    }


def make_task_snapshot(task):
    return json.dumps(snapshot_dict(task), sort_keys=True, ensure_ascii=False)


def snapshot_note(existing):
    if not existing:
        return None
    raw = existing.get("last_snapshot") or ""
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        note = obj.get("note")
        return note if isinstance(note, dict) and note.get("note") else None
    except Exception:
        return None


def save_task(task, message_id=None, active=True):
    now = datetime.now(timezone.utc).isoformat()
    snapshot = make_task_snapshot(task)
    conn = db_connect()

    row = conn.execute(
        "SELECT first_seen FROM tasks WHERE task_key = ?",
        (task["task_key"],),
    ).fetchone()
    first_seen = row[0] if row and row[0] else now

    conn.execute(
        """
        INSERT INTO tasks (
            task_key, shipment_id, task_name, telegram_message_id,
            active, first_seen, last_seen, last_snapshot
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_key) DO UPDATE SET
            shipment_id = excluded.shipment_id,
            task_name = excluded.task_name,
            telegram_message_id = excluded.telegram_message_id,
            active = excluded.active,
            last_seen = excluded.last_seen,
            last_snapshot = excluded.last_snapshot
        """,
        (
            task["task_key"],
            task.get("shipment_id", ""),
            task.get("task_name", ""),
            message_id,
            1 if active else 0,
            first_seen,
            now,
            snapshot,
        ),
    )
    conn.commit()
    conn.close()


def mark_missing_tasks_inactive(current_keys):
    conn = db_connect()
    rows = conn.execute(
        "SELECT task_key FROM tasks WHERE active = 1"
    ).fetchall()

    for (task_key,) in rows:
        if task_key not in current_keys:
            conn.execute(
                "UPDATE tasks SET active = 0 WHERE task_key = ?",
                (task_key,),
            )
            NOTE_RETRY_CACHE.pop(task_key, None)
            logger.info("TASK REMOVED | %s", task_key)

    conn.commit()
    conn.close()


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


class AODClient:
    def __init__(self):
        self.session = create_session()
        self.logged_in = False
        self.admin_url = None
        self.board_base_url = None
        self.tasks_url = None
        self.tasks_view_url = None

    @staticmethod
    def is_login_page(response):
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

    def force_old_session_logout(self, location):
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
            raise RuntimeError("ForceLogout form not found.")

        force_data = {}
        for inp in force_form.find_all("input"):
            name = inp.get("name")
            if name:
                force_data[name] = inp.get("value", "")

        force_url = urljoin(message_url, force_form.get("action", ""))
        logger.info("Forcing old session logout...")

        response = self.session.post(
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
        response.raise_for_status()
        logger.info("ForceLogout status: %s", response.status_code)

    def login(self):
        self.logged_in = False
        try:
            self.session.close()
        except Exception:
            pass
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
            raise RuntimeError("AOD login redirect missing.")
        if "admin/index.cfm" not in location.lower():
            raise RuntimeError(f"AOD login failed. Redirect: {location}")

        logger.info("AOD LOGIN SUCCESS")

        admin_url = urljoin(LOGIN_ACTION_URL, location)
        logger.info("Opening AOD admin...")
        response = self.session.get(
            admin_url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()

        if self.is_login_page(response):
            raise RuntimeError("Admin redirected to login.")

        self.admin_url = response.url
        logger.info("Admin final URL: %s", self.admin_url)

        parsed = urlparse(self.admin_url)
        self.board_base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.tasks_url = self.board_base_url + "/Provider/statusboard/StatusBoard.cfm"
        self.tasks_view_url = (
            self.tasks_url
            + "?selectedTab=tasksDIV&showGroundAddresses=false"
        )
        self.logged_in = True

    def get_tasks_board(self):
        if not self.logged_in:
            self.login()

        params = {
            "selectedTab": "tasksDIV",
            "showGroundAddresses": "false",
        }

        response = self.session.get(
            self.tasks_url,
            params=params,
            headers={"Referer": self.admin_url},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        if self.is_login_page(response):
            logger.warning("AOD session expired.")
            self.logged_in = False
            self.login()
            response = self.session.get(
                self.tasks_url,
                params=params,
                headers={"Referer": self.admin_url},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

        response.raise_for_status()
        if self.is_login_page(response):
            raise RuntimeError("Tasks page returned login page.")

        self.tasks_view_url = response.url
        return response

    def authenticated_get(self, url, referer=None):
        response = self.session.get(
            url,
            headers={"Referer": referer or self.tasks_view_url or self.admin_url},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        if self.is_login_page(response):
            self.logged_in = False
            self.login()
            response = self.session.get(
                url,
                headers={"Referer": referer or self.tasks_view_url or self.admin_url},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

        response.raise_for_status()
        return response

    def open_shipment_detail(self, task):
        shipment_link = task.get("shipment_link", "")
        if shipment_link:
            return self.authenticated_get(
                shipment_link,
                referer=self.tasks_view_url,
            )

        rfq_id = task.get("shipment_rfq_id", "")
        provider_id = task.get("provider_id", "")
        shipment_id = task.get("shipment_id", "")
        selected_tab = task.get("selected_tab", "tasksDIV") or "tasksDIV"

        if not shipment_id:
            return None

        detail_url = (
            self.board_base_url
            + "/Provider/statusboard/ProviderBiddingDetail.cfm"
        )

        response = self.session.post(
            detail_url,
            data={
                "providerId": provider_id,
                "shipmentId": shipment_id,
                "shipmentRFQId": rfq_id,
                "selectedTab": selected_tab,
            },
            headers={
                "Referer": self.tasks_view_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()

        if self.is_login_page(response):
            self.logged_in = False
            self.login()
            return self.open_shipment_detail(task)

        return response

    def get_latest_note(self, task):
        detail_response = self.open_shipment_detail(task)
        if not detail_response:
            return None

        note_data = parse_latest_note(detail_response.text)
        if note_data:
            return note_data

        soup = BeautifulSoup(detail_response.text, "html.parser")

        # Normal Notes tab href
        for anchor in soup.find_all("a"):
            text = clean_text(anchor.get_text(" ", strip=True)).upper()
            anchor_id = clean_text(anchor.get("id", "")).upper()
            href = anchor.get("href", "")
            onclick = anchor.get("onclick", "")

            if text == "NOTES" or "NOTES" in anchor_id:
                if href and not href.lower().startswith("javascript:"):
                    notes_url = urljoin(detail_response.url, href)
                    notes_response = self.authenticated_get(
                        notes_url,
                        referer=detail_response.url,
                    )
                    parsed = parse_latest_note(notes_response.text)
                    if parsed:
                        return parsed

                match = re.search(
                    r"""['"]([^'"]*note[^'"]*)['"]""",
                    onclick,
                    re.IGNORECASE,
                )
                if match:
                    notes_url = urljoin(detail_response.url, match.group(1))
                    notes_response = self.authenticated_get(
                        notes_url,
                        referer=detail_response.url,
                    )
                    parsed = parse_latest_note(notes_response.text)
                    if parsed:
                        return parsed

        # Any link whose href contains note
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            if "note" not in href.lower():
                continue
            if href.lower().startswith("javascript:"):
                continue
            notes_url = urljoin(detail_response.url, href)
            notes_response = self.authenticated_get(
                notes_url,
                referer=detail_response.url,
            )
            parsed = parse_latest_note(notes_response.text)
            if parsed:
                return parsed

        logger.warning(
            "NOTE DATA NOT FOUND ON DETAIL PAGE | %s",
            task.get("shipment_id", ""),
        )
        return None


def extract_tasks_tab_count(html):
    match = re.search(
        r"getElementById\(\s*['\"]tasksDIV['\"]\s*\).*?"
        r"Tasks\s*\(\s*['\"]?\s*\+\s*(\d+)",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return int(match.group(1))

    match = re.search(
        r"Tasks\s*\(\s*(\d+)\s*\)",
        html,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def parse_information_cell(cell):
    info = {}

    for table in cell.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            i = 0
            while i + 1 < len(cells):
                label = normalize_label(cells[i].get_text(" ", strip=True))
                value = clean_text(cells[i + 1].get_text(" ", strip=True))
                if label:
                    info[label] = value
                i += 2

    full_text = clean_text(cell.get_text(" ", strip=True))
    labels = [
        "FROM", "TO", "DEST", "READY", "NEED",
        "CURRENT ETA", "ETA", "ARRIVE"
    ]

    for label in labels:
        if info.get(label):
            continue
        pattern = (
            rf"{re.escape(label)}\s*:\s*(.*?)"
            rf"(?=\s+(?:FROM|TO|DEST|READY|NEED|CURRENT ETA|ETA|ARRIVE)\s*:|$)"
        )
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            value = clean_text(match.group(1))
            if value:
                info[label] = value

    return info


def parse_launch_data(row, shipment_id, provider_id):
    rfq_id = ""
    selected_tab = "tasksDIV"

    for anchor in row.find_all("a"):
        onclick = anchor.get("onclick", "")
        match = re.search(
            r"launchBidDetail\(\s*'([^']+)'\s*,\s*'([^']*)'\s*,\s*'([^']+)'\s*\)",
            onclick,
            re.IGNORECASE,
        )
        if match and match.group(1) == shipment_id:
            rfq_id = match.group(2)
            selected_tab = match.group(3) or "tasksDIV"
            break

    return {
        "provider_id": provider_id,
        "shipment_rfq_id": rfq_id,
        "selected_tab": selected_tab,
    }


def parse_tasks_board(html):
    soup = BeautifulSoup(html, "html.parser")
    tasks = []

    tasks_div = soup.find("div", id="Tasks")
    if tasks_div is None:
        logger.error("Tasks DIV not found.")
        return []

    task_table = tasks_div.select_one("table.table.striped.full-width")
    if task_table is None:
        logger.error("Tasks table not found.")
        return []

    launch_form = soup.find("form", id="launchForm") or soup.find(
        "form", attrs={"name": "launchForm"}
    )
    provider_id = ""
    if launch_form:
        provider_input = launch_form.find("input", attrs={"name": "providerId"})
        if provider_input:
            provider_id = provider_input.get("value", "")

    for row in task_table.find_all("tr"):
        if row.find_parent("table") is not task_table:
            continue

        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue

        due = clean_text(cells[0].get_text(" ", strip=True))
        shipment_text = clean_text(cells[1].get_text(" ", strip=True))
        shipment_match = re.search(r"\b\d{6,10}\b", shipment_text)
        if not shipment_match:
            continue

        shipment_id = shipment_match.group(0)
        task_name = clean_text(cells[2].get_text(" ", strip=True)).upper()
        if not task_name:
            continue

        if not task_is_allowed(task_name):
            ignored_key = f"{shipment_id}:{task_name}"
            if ignored_key not in IGNORED_TASK_CACHE:
                logger.info("IGNORED TASK | %s | %s", shipment_id, task_name)
                IGNORED_TASK_CACHE.add(ignored_key)
            continue

        shipment_link = ""
        shipment_anchor = cells[1].find("a")
        if shipment_anchor:
            href = shipment_anchor.get("href", "")
            if href and not href.lower().startswith("javascript:"):
                shipment_link = urljoin(LOGIN_BASE, href)

        launch_data = parse_launch_data(row, shipment_id, provider_id)

        task = {
            "task_key": f"{shipment_id}:{task_name}",
            "shipment_id": shipment_id,
            "shipment_link": shipment_link,
            "task_name": task_name,
            "due": due,
            "info": parse_information_cell(cells[3]),
            "note_data": None,
            **launch_data,
        }
        tasks.append(task)

    unique = {task["task_key"]: task for task in tasks}
    return list(unique.values())


def parse_latest_note(html):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_map = None
        header_row = None

        for row in rows:
            cells = row.find_all(["th", "td"])
            headers = [
                normalize_label(cell.get_text(" ", strip=True))
                for cell in cells
            ]

            if (
                "USER" in headers
                and any(header.startswith("DATE") for header in headers)
                and "NOTE TYPE" in headers
                and "NOTE" in headers
            ):
                header_map = {
                    header: index
                    for index, header in enumerate(headers)
                }
                header_row = row
                break

        if not header_map or header_row is None:
            continue

        start = rows.index(header_row) + 1

        for row in rows[start:]:
            cells = row.find_all("td", recursive=False)
            if not cells:
                cells = row.find_all("td")
            if not cells:
                continue

            values = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in cells
            ]

            def column(exact=None, prefix=None):
                for header, index in header_map.items():
                    if exact and header == exact and index < len(values):
                        return values[index]
                    if prefix and header.startswith(prefix) and index < len(values):
                        return values[index]
                return ""

            note = {
                "sender": column(exact="USER"),
                "date": column(prefix="DATE"),
                "note_type": column(exact="NOTE TYPE"),
                "note": column(exact="NOTE"),
            }

            if note["note"]:
                candidates.append(note)

    if not candidates:
        return None

    # Broker/RFQ note takes priority over internal cargo notes.
    for note in candidates:
        if "RFQ" in note.get("note_type", "").upper():
            return note

    return candidates[0]


def format_due_status(due):
    due = clean_text(due)
    if not due:
        return ""

    match = re.search(r"-?\d+", due)
    number = int(match.group(0)) if match else None

    if "OVERDUE" in due.upper():
        if number is not None:
            return f"⏰ <b>OVERDUE TIME:</b> <b>{abs(number)} MIN</b>"
        return f"⏰ <b>OVERDUE TIME:</b> <b>{escape_html(due)}</b>"

    if number is not None:
        return f"⏳ <b>DUE IN:</b> <b>{abs(number)} MIN</b>"

    return f"⏳ <b>DUE IN:</b> <b>{escape_html(due)}</b>"


def format_normal_task_message(task):
    shipment = escape_html(task.get("shipment_id", ""))
    task_name = escape_html(task.get("task_name", ""))
    info = task.get("info", {})

    from_loc = escape_html(info.get("FROM", ""))
    to_loc = escape_html(info.get("TO", "") or info.get("DEST", ""))
    ready = escape_html(info.get("READY", ""))
    need = escape_html(info.get("NEED", ""))
    current_eta = escape_html(info.get("CURRENT ETA", ""))

    lines = [
        "🚨 <b>AOD NEW TASK</b>",
        "",
        f"🆔 <b>Shipment:</b> <code>{shipment}</code>",
        f"📋 <b>Task:</b> {task_name}",
        "",
    ]

    if from_loc:
        lines += ["🟢 <b>FROM</b>", f"📍 {from_loc}", ""]
    if to_loc:
        lines += ["🔴 <b>TO</b>", f"📍 {to_loc}", ""]

    due_line = format_due_status(task.get("due", ""))
    if due_line:
        lines += [due_line, ""]

    additional = []
    if ready:
        additional.append(f"• <b>READY:</b> {ready}")
    if need:
        additional.append(f"• <b>NEED:</b> {need}")
    if current_eta:
        additional.append(f"• <b>CURRENT ETA:</b> {current_eta}")

    reserved = {"FROM", "TO", "DEST", "READY", "NEED", "CURRENT ETA"}
    for key, value in info.items():
        if key in reserved:
            continue
        value = clean_text(value)
        if value:
            additional.append(
                f"• <b>{escape_html(key)}:</b> {escape_html(value)}"
            )

    if additional:
        lines.append("ℹ️ <b>Additional Info</b>")
        lines.extend(additional)

    lines += ["", f"👥 {TASK_MENTIONS}"]
    return "\n".join(lines)


def format_note_message(task):
    shipment = escape_html(task.get("shipment_id", ""))
    note = task.get("note_data") or {}

    lines = [
        "📝 <b>AOD NEW NOTE</b>",
        "",
        f"🆔 <b>Shipment:</b> <code>{shipment}</code>",
        "📋 <b>Task:</b> NOTE",
        "",
    ]

    due_line = format_due_status(task.get("due", ""))
    if due_line:
        lines += [due_line, ""]

    sender = escape_html(note.get("sender", ""))
    note_time = escape_html(note.get("date", ""))
    note_type = escape_html(note.get("note_type", ""))
    note_text = escape_html(note.get("note", ""))

    if sender:
        lines.append(f"👤 <b>Sender:</b> {sender}")
    if note_time:
        lines.append(f"🕐 <b>Time:</b> {note_time}")
    if note_type:
        lines.append(f"📋 <b>Type:</b> {note_type}")

    if sender or note_time or note_type:
        lines.append("")

    if note_text:
        lines += ["💬 <b>NOTE:</b>", note_text]
    else:
        lines.append(
            "⚠️ <i>Note text could not be loaded automatically.</i>"
        )

    lines += ["", f"👥 {TASK_MENTIONS}"]
    return "\n".join(lines)


def format_task_message(task):
    if task.get("task_name", "").upper() == "NOTE":
        return format_note_message(task)
    return format_normal_task_message(task)


def telegram_api(method, payload):
    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/{method}"
    )
    response = requests.post(
        url,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Telegram API error {response.status_code}: {response.text[:500]}"
        )

    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")

    return data


def telegram_payload(task, message_id=None, aod_url=None):
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": format_task_message(task),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if message_id is not None:
        payload["message_id"] = message_id

    open_url = task.get("shipment_link") or aod_url
    if open_url:
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": "🔗 OPEN AOD", "url": open_url}]
            ]
        }

    return payload


def send_task_message(task, aod_url=None):
    if not task_is_allowed(task.get("task_name", "")):
        logger.error(
            "TELEGRAM BLOCKED | %s | %s",
            task.get("shipment_id", ""),
            task.get("task_name", ""),
        )
        return None

    data = telegram_api(
        "sendMessage",
        telegram_payload(task, aod_url=aod_url),
    )
    message_id = data["result"]["message_id"]

    logger.info(
        "TELEGRAM SENT | %s | %s | MSG=%s",
        task.get("shipment_id"),
        task.get("task_name"),
        message_id,
    )
    return message_id


def edit_task_message(task, message_id, aod_url=None):
    try:
        telegram_api(
            "editMessageText",
            telegram_payload(task, message_id=message_id, aod_url=aod_url),
        )
        logger.info(
            "TELEGRAM UPDATED | %s | %s | %s",
            task.get("shipment_id"),
            task.get("task_name"),
            task.get("due"),
        )
        return True
    except RuntimeError as exc:
        if "message is not modified" in str(exc).lower():
            return True
        logger.error("Telegram edit failed: %s", exc)
        return False


def load_note_data(client, task, existing=None):
    old_note = snapshot_note(existing)
    if old_note:
        task["note_data"] = old_note
        return task

    now = time.monotonic()
    last_try = NOTE_RETRY_CACHE.get(task["task_key"], 0)
    if existing and now - last_try < 10:
        return task

    NOTE_RETRY_CACHE[task["task_key"]] = now

    try:
        note_data = client.get_latest_note(task)
        task["note_data"] = note_data
        if note_data:
            logger.info(
                "NOTE LOADED | %s | %s | %s",
                task.get("shipment_id"),
                note_data.get("sender", ""),
                note_data.get("note_type", ""),
            )
        else:
            logger.warning(
                "NOTE DATA NOT FOUND | %s",
                task.get("shipment_id"),
            )
    except Exception as exc:
        logger.error(
            "NOTE FETCH FAILED | %s | %s",
            task.get("shipment_id"),
            exc,
        )
        task["note_data"] = None

    return task


def task_changed(existing, task):
    return (existing.get("last_snapshot") or "") != make_task_snapshot(task)


def process_tasks(client, tasks, first_run, aod_url):
    current_keys = set()

    for task in tasks:
        if not task_is_allowed(task.get("task_name", "")):
            continue

        task_key = task.get("task_key")
        if not task_key:
            continue

        current_keys.add(task_key)
        existing = get_task(task_key)

        if task.get("task_name") == "NOTE":
            task = load_note_data(client, task, existing=existing)

        if (
            first_run
            and not POST_EXISTING_ON_START
            and existing is None
        ):
            save_task(task, message_id=None, active=True)
            logger.info(
                "BASELINE IGNORED | %s | %s",
                task.get("shipment_id"),
                task.get("task_name"),
            )
            continue

        if existing is None:
            logger.info(
                "NEW TASK | %s | %s | %s",
                task.get("shipment_id"),
                task.get("task_name"),
                task.get("due"),
            )
            try:
                message_id = send_task_message(task, aod_url=aod_url)
                if message_id:
                    save_task(task, message_id=message_id, active=True)
            except Exception as exc:
                logger.error(
                    "Telegram send failed | %s | %s",
                    task.get("shipment_id"),
                    exc,
                )
            continue

        if not existing.get("active"):
            logger.info(
                "TASK RETURNED | %s | %s",
                task.get("shipment_id"),
                task.get("task_name"),
            )
            try:
                message_id = send_task_message(task, aod_url=aod_url)
                if message_id:
                    save_task(task, message_id=message_id, active=True)
            except Exception as exc:
                logger.error(
                    "Returned task send failed | %s | %s",
                    task.get("shipment_id"),
                    exc,
                )
            continue

        if task_changed(existing, task):
            message_id = existing.get("telegram_message_id")
            if not message_id:
                save_task(task, message_id=None, active=True)
                continue

            if edit_task_message(task, message_id, aod_url=aod_url):
                save_task(task, message_id=message_id, active=True)
        else:
            save_task(
                task,
                message_id=existing.get("telegram_message_id"),
                active=True,
            )

    mark_missing_tasks_inactive(current_keys)


def main():
    print("=" * 65)
    print("AOD TASKS ONLY -> TELEGRAM MONITOR")
    print("=" * 65)

    init_database()
    client = AODClient()

    logger.info("Starting AOD Task Bot...")
    logger.info("Polling interval: %.2f seconds", POLL_INTERVAL)
    logger.info("Telegram destination: %s", TELEGRAM_CHAT_ID)
    logger.info(
        "Allowed tasks: %s",
        ", ".join(sorted(ALLOWED_TASK_TYPES)),
    )
    logger.info("RFQ TAB: COMPLETELY IGNORED")

    first_run = True
    last_tab_count = None
    last_allowed_count = None

    while True:
        started = time.monotonic()

        try:
            response = client.get_tasks_board()
            tasks = parse_tasks_board(response.text)
            tab_count = extract_tasks_tab_count(response.text)
            allowed_count = len(tasks)

            if (
                tab_count != last_tab_count
                or allowed_count != last_allowed_count
            ):
                logger.info(
                    "TASKS | TOTAL=%s | ALLOWED=%s",
                    tab_count if tab_count is not None else "?",
                    allowed_count,
                )
                last_tab_count = tab_count
                last_allowed_count = allowed_count

            process_tasks(
                client=client,
                tasks=tasks,
                first_run=first_run,
                aod_url=client.tasks_view_url or client.admin_url,
            )

            first_run = False

        except KeyboardInterrupt:
            logger.info("Bot stopped.")
            break

        except requests.RequestException as exc:
            logger.error("Network error: %s", exc)
            client.logged_in = False
            time.sleep(5)

        except RuntimeError as exc:
            logger.error("Runtime error: %s", exc)
            client.logged_in = False
            logger.warning(
                "Retrying in %s seconds...",
                LOGIN_RETRY_DELAY,
            )
            time.sleep(LOGIN_RETRY_DELAY)

        except Exception as exc:
            logger.exception("Unexpected error: %s", exc)
            client.logged_in = False
            time.sleep(10)

        elapsed = time.monotonic() - started
        time.sleep(max(0.1, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
