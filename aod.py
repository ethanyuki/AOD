import os
import re
import json
import time
import html
import logging
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://apt6.activeaero.com"
SSKEY = os.getenv("SSKEY", "").strip()

TASKS_URL = (
    f"{BASE_URL}/Provider/statusboard/statusboard.cfm"
    f"?sskey={SSKEY}&selectedTab=tasksDIV&showGroundAddresses=false"
)

DETAIL_URL = f"{BASE_URL}/Provider/statusboard/ProviderBiddingDetail.cfm"

STATE_FILE = "sent_state.json"
POLL_SECONDS = 8
ERROR_SLEEP_SECONDS = 12
COOKIE_DEAD_SLEEP_SECONDS = 60
REQUEST_TIMEOUT = 30

COOKIE = os.getenv("COOKIE", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

if not COOKIE or not BOT_TOKEN or not CHAT_ID or not SSKEY:
    raise RuntimeError("COOKIE, BOT_TOKEN, CHAT_ID, SSKEY to'liq kiritilmagan")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cookie": COOKIE,
    "Referer": f"{BASE_URL}/Login/ForceLogout.cfm",
    "Origin": BASE_URL,
})


def cleanup(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def escape_html(text: str) -> str:
    return html.escape(text or "")


def is_login_page(html_text: str) -> bool:
    text = html_text.lower()
    return (
        "user name:" in text
        and "password:" in text
        and "loginaction.cfm" in text
    )


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("shipments", {})
            data.setdefault("meta", {})
            data["meta"].setdefault("startup_sent", False)
            data["meta"].setdefault("cookie_dead_notified", False)
            return data
    except Exception:
        return {
            "shipments": {},
            "meta": {
                "startup_sent": False,
                "cookie_dead_notified": False
            }
        }


def save_state(data: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


state = load_state()


def telegram_request(method: str, data: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def send_msg(text: str, buttons: dict | None = None, disable_preview: bool = True) -> int:
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if buttons:
        payload["reply_markup"] = json.dumps(buttons, ensure_ascii=False)

    result = telegram_request("sendMessage", payload)
    return result["result"]["message_id"]


def edit_msg(message_id: int, text: str, buttons: dict | None = None, disable_preview: bool = True) -> None:
    payload = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if buttons:
        payload["reply_markup"] = json.dumps(buttons, ensure_ascii=False)

    telegram_request("editMessageText", payload)


def send_startup_message_once() -> None:
    if state["meta"].get("startup_sent"):
        return

    try:
        send_msg(
            "✅ <b>AOD bot ishga tushdi</b>\n\n"
            f"Domain: <code>{escape_html(BASE_URL)}</code>\n"
            f"SSKEY: <code>{escape_html(SSKEY)}</code>\n"
            "Monitoring boshlandi."
        )
        state["meta"]["startup_sent"] = True
        save_state(state)
    except Exception as e:
        logging.error("Startup telegram xatoligi: %s", e)


def notify_cookie_dead_once() -> None:
    if state["meta"].get("cookie_dead_notified"):
        return

    try:
        send_msg(
            "❗ <b>Cookie o'lgan yoki session tushgan</b>\n\n"
            "Railway Variables ichidagi <code>COOKIE</code> ni yangilash kerak."
        )
        state["meta"]["cookie_dead_notified"] = True
        save_state(state)
    except Exception as e:
        logging.error("Cookie dead telegram xatoligi: %s", e)


def reset_cookie_dead_flag() -> None:
    if not state["meta"].get("cookie_dead_notified"):
        return

    state["meta"]["cookie_dead_notified"] = False
    save_state(state)


def map_btn(origin: str, dest: str) -> dict:
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin)}"
        f"&destination={quote_plus(dest)}"
        "&travelmode=driving"
    )
    return {
        "inline_keyboard": [
            [{"text": "🗺 View in Map", "url": url}]
        ]
    }


def build_initial_message(task: dict) -> str:
    return (
        "🚨 <b>NEW TASK LOAD</b>\n\n"
        f"Load ID: <code>{escape_html(task['shipment_id'])}</code>\n"
        f"Task: <b>{escape_html(task['task_name'])}</b>\n"
        f"From: <b>{escape_html(task['from'])}</b>\n"
        f"To: <b>{escape_html(task['to'])}</b>\n"
        f"Ready: <b>{escape_html(task['ready'])}</b>\n"
        f"Need: <b>{escape_html(task['need'])}</b>\n\n"
        "⏳ <i>Loading full details...</i>"
    )


def build_full_message(task: dict, detail: dict) -> str:
    parts = [
        "🚨 <b>NEW TASK LOAD</b>",
        "",
        f"Load ID: <code>{escape_html(task['shipment_id'])}</code>",
        f"Task: <b>{escape_html(task['task_name'])}</b>",
        f"From: <b>{escape_html(task['from'])}</b>",
        f"To: <b>{escape_html(task['to'])}</b>",
        f"Ready: <b>{escape_html(task['ready'])}</b>",
        f"Need: <b>{escape_html(task['need'])}</b>",
        "",
        "✅ <b>FULL DETAILS</b>",
    ]

    if detail.get("customer"):
        parts.append(f"Customer: <b>{escape_html(detail['customer'])}</b>")

    if detail.get("distance"):
        parts.append(f"Distance: <b>{escape_html(detail['distance'])}</b>")

    if detail.get("team_driver"):
        parts.append(f"Special: <b>{escape_html(detail['team_driver'])}</b>")

    return "\n".join(parts)


def get_tasks() -> list[dict]:
    r = session.get(TASKS_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    html_text = r.text

    if is_login_page(html_text):
        notify_cookie_dead_once()
        logging.warning("Cookie o'lgan yoki login page qaytdi.")
        time.sleep(COOKIE_DEAD_SLEEP_SECONDS)
        return []

    reset_cookie_dead_flag()

    soup = BeautifulSoup(html_text, "html.parser")

    provider_id = ""
    provider_input = soup.find("input", {"id": "providerId"})
    if provider_input and provider_input.get("value"):
        provider_id = provider_input["value"].strip()

    main_table = soup.find("table", class_=lambda c: c and "striped" in c and "full-width" in c)
    if not main_table:
        return []

    tasks = []

    for row in main_table.find_all("tr"):
        link = row.find("a", onclick=True)
        if not link:
            continue

        onclick = link.get("onclick", "")
        match = re.search(r"launchBidDetail\('([^']+)','([^']+)','([^']+)'\)", onclick)
        if not match:
            continue

        shipment_id, rfq_id, selected_tab = match.groups()

        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        task_name = cleanup(cells[2].get_text(" ", strip=True))
        from_loc = ""
        to_loc = ""
        ready = ""
        need = ""

        info_table = cells[3].find("table")
        if info_table:
            info_rows = info_table.find_all("tr")

            if len(info_rows) >= 1:
                tds = info_rows[0].find_all("td")
                if len(tds) >= 4:
                    from_loc = cleanup(tds[1].get_text(" ", strip=True))
                    to_loc = cleanup(tds[3].get_text(" ", strip=True))

            if len(info_rows) >= 2:
                tds = info_rows[1].find_all("td")
                if len(tds) >= 4:
                    ready = cleanup(tds[1].get_text(" ", strip=True))
                    need = cleanup(tds[3].get_text(" ", strip=True))

        tasks.append({
            "shipment_id": shipment_id,
            "rfq_id": rfq_id,
            "selected_tab": selected_tab,
            "provider_id": provider_id,
            "task_name": task_name,
            "from": from_loc,
            "to": to_loc,
            "ready": ready,
            "need": need,
        })

    return tasks


def get_detail(task: dict) -> dict:
    data = {
        "providerId": task["provider_id"],
        "shipmentId": task["shipment_id"],
        "shipmentRFQId": task["rfq_id"],
        "selectedTab": task["selected_tab"],
    }

    r = session.post(DETAIL_URL, data=data, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    html_text = r.text

    if is_login_page(html_text):
        raise RuntimeError("Detail ochishda login page qaytdi.")

    soup = BeautifulSoup(html_text, "html.parser")
    text = cleanup(soup.get_text("\n", strip=True))

    distance = ""
    m = re.search(r"(\d+(?:,\d+)?(?:\.\d+)?)\s*mi", text, re.I)
    if m:
        distance = f"{m.group(1)} mi"

    team_driver = "TEAM DRIVER" if "TEAM DRIVER" in text.upper() else ""

    customer = ""
    m = re.search(r"Customer\s+(.+?)\s+Include cost of fuel", text, re.I)
    if m:
        customer = cleanup(m.group(1))

    return {
        "distance": distance,
        "team_driver": team_driver,
        "customer": customer,
        "raw_text": text[:1500],
    }


def main():
    logging.info("AOD started...")
    send_startup_message_once()

    while True:
        try:
            tasks = get_tasks()

            if tasks:
                logging.info("Tasks: %s", len(tasks))

            for task in tasks:
                sid = task["shipment_id"]

                if sid in state["shipments"]:
                    continue

                logging.info("NEW: %s", sid)

                buttons = map_btn(task["from"], task["to"])
                initial_text = build_initial_message(task)
                msg_id = send_msg(initial_text, buttons=buttons)

                state["shipments"][sid] = {
                    "message_id": msg_id,
                    "created_at": int(time.time())
                }
                save_state(state)

                try:
                    detail = get_detail(task)
                    full_text = build_full_message(task, detail)
                    edit_msg(msg_id, full_text, buttons=buttons)
                except Exception as detail_error:
                    logging.error("DETAIL ERROR %s: %s", sid, detail_error)

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            logging.info("Stopped by user.")
            break
        except Exception as e:
            logging.error("MAIN ERROR: %s", e)
            time.sleep(ERROR_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
