import os
import requests
import time
import json
import re
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

BASE_URL = "https://apt3.activeaero.com"
TASKS_URL = f"{BASE_URL}/Provider/statusboard/StatusBoard.cfm?selectedTab=tasksDIV&showGroundAddresses=false"
DETAIL_URL = f"{BASE_URL}/Provider/statusboard/ProviderBiddingDetail.cfm"
STATE_FILE = "sent_state.json"

COOKIE = os.getenv("COOKIE", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

if not COOKIE or not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("COOKIE, BOT_TOKEN, CHAT_ID variables to'liq kiritilmagan")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cookie": COOKIE,
    "Referer": TASKS_URL,
    "Origin": BASE_URL,
})


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


state = load_state()


def is_login_page(html_text: str) -> bool:
    text = html_text.lower()
    return "user name:" in text and "password:" in text and "loginaction.cfm" in text


def cleanup(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def send_msg(text, buttons=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = json.dumps(buttons, ensure_ascii=False)

    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram send error: {data}")
    return data["result"]["message_id"]


def edit_msg(message_id, text, buttons=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = json.dumps(buttons, ensure_ascii=False)

    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()


def map_btn(origin, dest):
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin)}"
        f"&destination={quote_plus(dest)}"
        "&travelmode=driving"
    )
    return {"inline_keyboard": [[{"text": "🗺 View in Map", "url": url}]]}


def get_tasks():
    r = session.get(TASKS_URL, timeout=30)
    r.raise_for_status()
    html = r.text

    if is_login_page(html):
    print("❗ COOKIE O'LGAN - YANGILASH KERAK")
    send_msg("❗ Cookie o'lgan, yangilash kerak")
    time.sleep(60)
    return []

    soup = BeautifulSoup(html, "html.parser")

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
        m = re.search(r"launchBidDetail\('([^']+)','([^']+)','([^']+)'\)", onclick)
        if not m:
            continue

        shipment_id, rfq_id, selected_tab = m.groups()

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


def get_detail(task):
    data = {
        "providerId": task["provider_id"],
        "shipmentId": task["shipment_id"],
        "shipmentRFQId": task["rfq_id"],
        "selectedTab": task["selected_tab"],
    }

    r = session.post(DETAIL_URL, data=data, timeout=30)
    r.raise_for_status()
    html = r.text

    if is_login_page(html):
        raise RuntimeError("Detail ochishda login page qaytdi.")

    soup = BeautifulSoup(html, "html.parser")
    text = cleanup(soup.get_text("\n", strip=True))

    distance = ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*mi", text, re.I)
    if m:
        distance = m.group(1) + " mi"

    team_driver = "TEAM DRIVER" if "TEAM DRIVER" in text.upper() else ""

    return {
        "distance": distance,
        "team_driver": team_driver,
        "raw_text": text[:1500],
    }


def build_initial_message(task):
    return (
        "🚨 <b>NEW TASK LOAD</b>\n\n"
        f"Load ID: <code>{task['shipment_id']}</code>\n"
        f"Task: <b>{task['task_name']}</b>\n"
        f"From: <b>{task['from']}</b>\n"
        f"To: <b>{task['to']}</b>\n"
        f"Ready: <b>{task['ready']}</b>\n"
        f"Need: <b>{task['need']}</b>\n\n"
        "⏳ <i>Loading full details...</i>"
    )


def build_full_message(task, detail):
    parts = [
        "🚨 <b>NEW TASK LOAD</b>",
        "",
        f"Load ID: <code>{task['shipment_id']}</code>",
        f"Task: <b>{task['task_name']}</b>",
        f"From: <b>{task['from']}</b>",
        f"To: <b>{task['to']}</b>",
        f"Ready: <b>{task['ready']}</b>",
        f"Need: <b>{task['need']}</b>",
        "",
        "✅ <b>FULL DETAILS</b>",
    ]

    if detail["distance"]:
        parts.append(f"Distance: <b>{detail['distance']}</b>")

    if detail["team_driver"]:
        parts.append(f"Special: <b>{detail['team_driver']}</b>")

    return "\n".join(parts)


print("AOD started...")

while True:
    try:
        tasks = get_tasks()

        if not tasks:
            print("Tasks bo'sh.")
        else:
            print(f"Tasks: {len(tasks)}")

        for task in tasks:
            sid = task["shipment_id"]

            if sid in state:
                continue

            print("NEW:", sid)

            buttons = map_btn(task["from"], task["to"])
            initial_text = build_initial_message(task)
            msg_id = send_msg(initial_text, buttons=buttons)

            state[sid] = {"message_id": msg_id}
            save_state(state)

            try:
                detail = get_detail(task)
                full_text = build_full_message(task, detail)
                edit_msg(msg_id, full_text, buttons=buttons)
            except Exception as detail_error:
                print("DETAIL ERROR:", detail_error)

        time.sleep(5)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
