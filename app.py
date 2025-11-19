import os, urllib.parse, logging, sys, time, threading, json, re
from flask import Flask, request
from dotenv import load_dotenv
import requests

# --------------------------------------------------------------------------
# LOGGING
# --------------------------------------------------------------------------
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("halo-api")
log.info("✅ Logging gestart")

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
load_dotenv()
required = ["HALO_AUTH_URL", "HALO_API_BASE", "HALO_CLIENT_ID", "HALO_CLIENT_SECRET", "WEBEX_BOT_TOKEN"]
missing = [k for k in required if not os.getenv(k)]
if missing:
    log.critical(f"❌ Ontbrekende .env-variabelen: {missing}")
    sys.exit(1)

app = Flask(__name__)
HALO_AUTH_URL  = os.getenv("HALO_AUTH_URL")
HALO_API_BASE  = os.getenv("HALO_API_BASE").rstrip('/')
HALO_CLIENT_ID = os.getenv("HALO_CLIENT_ID")
HALO_CLIENT_SECRET = os.getenv("HALO_CLIENT_SECRET")
HALO_TICKET_TYPE_ID = int(os.getenv("HALO_TICKET_TYPE_ID", 66))
HALO_CLIENT_ID_NUM  = int(os.getenv("HALO_CLIENT_ID_NUM", 12))
HALO_SITE_ID        = int(os.getenv("HALO_SITE_ID", 18))
WEBEX_TOKEN         = os.getenv("WEBEX_BOT_TOKEN")
WEBEX_HEADERS = {"Authorization": f"Bearer {WEBEX_TOKEN}",
                 "Content-Type": "application/json"} if WEBEX_TOKEN else {}

# Gebruikers cache (alleen voor client 18 & site 18)
USER_CACHE = {
    "users": [],
    "timestamp": 0,
    "source": "none",
    "max_users": 200
}
# Nu: {room_id: [ticket_id1, ticket_id2, ...]}
USER_TICKET_MAP = {}
# Per room per gebruiker: {room_id: {user_email_lower: [ticket_id, ...]}}
ROOM_TICKETS_BY_USER = {}
# Om statuswijzigingen te detecteren: {ticket_id: {status: "Oud status", last_checked: timestamp}}
TICKET_STATUS_TRACKER = {}
# Bijhouden van laatste verwerkte actie per ticket: {ticket_id: last_action_id}
ACTION_TRACKER = {}
CACHE_DURATION = 24 * 60 * 60  # 24 uur
MAX_PAGES = 3  # Max 3 pagina's (100 + 100 + 100 = 300 users)

# Nieuwe variabelen voor HALO actie-ID en notitieveld
ACTION_ID_PUBLIC = int(os.getenv("ACTION_ID_PUBLIC", 145))
NOTE_FIELD_NAME = os.getenv("NOTE_FIELD_NAME", "Note")

# --------------------------------------------------------------------------
# Controleer of WEBEX_TOKEN is ingesteld
# --------------------------------------------------------------------------
if not WEBEX_TOKEN:
    log.error("❌ WEBEX_BOT_TOKEN is niet ingesteld in .env bestand")
    sys.exit(1)
else:
    log.info("✅ Webex bot token is ingesteld")

# --------------------------------------------------------------------------
# HALO AUTH
# --------------------------------------------------------------------------
def get_halo_headers():
    payload = {
        "grant_type": "client_credentials",
        "client_id": HALO_CLIENT_ID,
        "client_secret": HALO_CLIENT_SECRET,
        "scope": "all"
    }
    log.info("➡️ Verbinding maken met HALO auth endpoint")
    r = requests.post(HALO_AUTH_URL,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      data=urllib.parse.urlencode(payload),
                      timeout=10)
    r.raise_for_status()
    token_info = r.json()
    log.info(f"✅ HALO auth succesvol: token expires in {token_info.get('expires_in', 'onbekend')} seconden")
    return {"Authorization": f"Bearer {token_info['access_token']}",
            "Content-Type": "application/json"}

# --------------------------------------------------------------------------
# HELPER FUNCTIE VOOR HALO REQUESTS MET RATE LIMIT HANDLING
# --------------------------------------------------------------------------
def halo_request(url, method='GET', headers=None, params=None, json=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            if method == 'GET':
                r = requests.get(url, headers=headers, params=params, json=json, timeout=15)
            elif method == 'POST':
                r = requests.post(url, headers=headers, params=params, json=json, timeout=15)
            else:
                raise ValueError(f"Onbekende methode: {method}")
        except Exception as e:
            log.error(f"Request mislukt: {e}")
            if attempt < max_retries - 1:
                wait_time = 1 * (attempt + 1)
                log.warning(f"Retrying in {wait_time} seconden (poging {attempt+1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                raise e

        # Rate limit handling
        if r.status_code == 429:
            retry_after = r.headers.get('Retry-After')
            wait_time = int(retry_after) if retry_after else 10
            log.warning(f"Rate limit bereikt, wachten {wait_time} seconden")
            time.sleep(wait_time)
            continue

        # Andere status codes
        return r

    return r  # Na max_retries, retourneer laatste response

# --------------------------------------------------------------------------
# STATUS NAAM CONVERSIE (ID → NAAM)
# --------------------------------------------------------------------------
def get_status_name(status_id):
    try:
        # Alleen converteren als status_id een nummer is
        if isinstance(status_id, str) and status_id.isdigit():
            status_id = int(status_id)
        if not isinstance(status_id, int):
            return str(status_id)
            
        h = get_halo_headers()
        url = f"{HALO_API_BASE}/api/Status/{status_id}"
        r = halo_request(url, headers=h)
        if r.status_code == 200:
            status_data = r.json()
            # Check meerdere mogelijke veldnamen voor statusnaam
            name = status_data.get("name") or status_data.get("StatusName") or status_data.get("status_name") or status_data.get("Status")
            if name:
                return name
        return str(status_id)
    except Exception as e:
        log.error(f"❌ Fout bij statusnaam conversie voor ID {status_id}: {e}")
        return str(status_id)

# --------------------------------------------------------------------------
# USERS
# --------------------------------------------------------------------------
def fetch_users(client_id, site_id):
    h = get_halo_headers()
    all_users = []
    page = 0
    page_size = 100
    
    while len(all_users) < USER_CACHE["max_users"]:
        params = {
            "client_id": client_id,
            "site_id": site_id,
            "pageinate": True,
            "page": page,
            "page_size": page_size
        }
        log.info(f"➡️ Fetching users page {page} (client={client_id}, site={site_id})")
        
        r = halo_request(f"{HALO_API_BASE}/api/Users",
                         params=params, 
                         headers=h)
        
        if r.status_code != 200:
            log.warning(f"⚠️ /api/Users pagina {page} gaf {r.status_code}: {r.text[:200]}")
            break
            
        response_json = r.json()
        users = []
        if isinstance(response_json, list):
            users = response_json
        elif isinstance(response_json, dict):
            users = response_json.get("users", []) or response_json.get("items", []) or response_json.get("data", [])
        
        if not users:
            break
            
        for u in users:
            if "id" in u: u["id"] = int(u["id"])
            if "client_id" in u: u["client_id"] = int(u["client_id"])
            if "site_id" in u: u["site_id"] = int(u["site_id"])
            if (
                u.get("use") == "user" and
                not u.get("inactive", True) and
                u.get("emailaddress") and
                "@" in u["emailaddress"]
            ):
                all_users.append(u)
        
        if len(users) < page_size or len(all_users) >= USER_CACHE["max_users"]:
            break
            
        page += 1
    
    all_users = all_users[:USER_CACHE["max_users"]]
    log.info(f"✅ {len(all_users)} gebruikers opgehaald (client={client_id}, site={site_id})")
    return all_users

def get_users():
    now = time.time()
    source = f"client{HALO_CLIENT_ID_NUM}_site{HALO_SITE_ID}"
    
    if USER_CACHE["users"] and \
       (now - USER_CACHE["timestamp"] < CACHE_DURATION) and \
       USER_CACHE["source"] == source:
        log.info(f"✅ Gebruikers uit cache (bron: {source})")
        return USER_CACHE["users"]
    
    users = fetch_users(HALO_CLIENT_ID_NUM, HALO_SITE_ID)
    USER_CACHE["users"] = users
    USER_CACHE["timestamp"] = now
    USER_CACHE["source"] = source
    log.info(f"✅ Gebruikers opgehaald en gecached (bron: {source})")
    return users

def get_user(email):
    if not email:
        return None
    email = email.lower().strip()
    for u in get_users():
        for field in ["EmailAddress", "emailaddress", "PrimaryEmail", "login", "email", "email1"]:
            if u.get(field) and u[field].lower() == email:
                return u
    return None

# -------------------------------------------------------------------------- 
# WEBEX HELPERS
# --------------------------------------------------------------------------
def send_message(room_id, text):
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return
    try:
        log.info(f"➡️ Sturen Webex bericht naar room {room_id}: '{text[:50]}...'") 
        response = requests.post("https://webexapis.com/v1/messages",
                      headers=WEBEX_HEADERS,
                      json={"roomId": room_id, "markdown": text}, timeout=10)
        log.info(f"✅ Webex bericht verstuurd naar room {room_id} (status: {response.status_code})")
        return response
    except Exception as e:
        log.error(f"❌ Webex send: {e}")
        return None

def send_adaptive_card(room_id):
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return
    log.info(f"➡️ Sturen adaptive card naar room {room_id}")
    payload = {
        "roomId": room_id,
        "text": "✍ Vul dit formulier in:",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.0",
                "body": [
                    {"type": "TextBlock", "text": "🆕 Nieuwe melding", "weight": "bolder"},
                    {"type": "Input.Text", "id": "email", "placeholder": "E-mailadres van gebruiker", "required": True},
                    {"type": "Input.Text", "id": "omschrijving", "placeholder": "Korte omschrijving", "required": True},
                    {"type": "Input.Text", "id": "sindswanneer", "placeholder": "Sinds wanneer?"},
                    {"type": "Input.Text", "id": "watwerktniet", "placeholder": "Wat werkt niet?"},
                    {"type": "Input.Text", "id": "zelfgeprobeerd", "placeholder": "Wat heb je al geprobeerd?"},
                    {"type": "Input.Text", "id": "impacttoelichting", "placeholder": "Impact toelichting (optioneel)"},
                    {"type": "Input.ChoiceSet", "id": "impact", "label": "Impact",
                     "choices": [
                         {"title": "Gehele bedrijf (1)", "value": "1"},
                         {"title": "Meerdere gebruikers (2)", "value": "2"},
                         {"title": "Één gebruiker (3)", "value": "3"}],
                     "value": "3", "required": True},
                    {"type": "Input.ChoiceSet", "id": "urgency", "label": "Urgency",
                     "choices": [
                         {"title": "High (1)", "value": "1"},
                         {"title": "Medium (2)", "value": "2"},
                         {"title": "Low (3)", "value": "3"}],
                     "value": "3", "required": True}
                ],
                "actions": [{"type": "Action.Submit", "title": "✅ Ticket aanmaken"}]
            }
        }]
    }
    try:
        requests.post("https://webexapis.com/v1/messages",
                      headers=WEBEX_HEADERS, json=payload, timeout=10)
        log.info(f"✅ Adaptive card verstuurd naar room {room_id}")
    except Exception as e:
        log.error(f"❌ Adaptive card versturen mislukt: {e}")

def send_reply_card(room_id, sender_email):
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return
    tickets = ROOM_TICKETS_BY_USER.get(room_id, {}).get((sender_email or "").lower(), [])
    choices = [{"title": f"Ticket #{t}", "value": str(t)} for t in tickets]
    if not choices:
        subtitle = "Ik zie nog geen tickets die aan jou gekoppeld zijn in deze ruimte."
    else:
        subtitle = "Kies hieronder je ticket of vul een nummer in."
    log.info(f"➡️ Sturen reply card naar room {room_id} voor {sender_email} met {len(choices)} opties")
    card = {
        "roomId": room_id,
        "text": "Reageer op een ticket",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": "💬 Reageer op ticket", "weight": "bolder", "size": "medium"},
                    {"type": "TextBlock", "text": subtitle, "wrap": True, "spacing": "small"},
                    {"type": "Input.ChoiceSet", "id": "ticket", "label": "Jouw tickets", "choices": choices, "isMultiSelect": False, "style": "compact"},
                    {"type": "TextBlock", "text": "Of voer handmatig een ticketnummer in:", "wrap": True, "spacing": "small"},
                    {"type": "Input.Text", "id": "ticket_manual", "placeholder": "Bijv. 12345"},
                    {"type": "TextBlock", "text": "Bericht (wordt als publieke note geplaatst):", "wrap": True, "spacing": "small"},
                    {"type": "Input.Text", "id": "message", "isMultiline": True, "placeholder": "Je bericht...", "maxLength": 4000, "label": "Bericht", "required": True}
                ],
                "actions": [
                    {"type": "Action.Submit", "title": "📨 Verstuur reactie", "data": {"formType": "reply", "sender": (sender_email or "").lower()}}
                ]
            }
        }]
    }
    try:
        requests.post("https://webexapis.com/v1/messages", headers=WEBEX_HEADERS, json=card, timeout=10)
        log.info(f"✅ Reply card verstuurd naar room {room_id}")
    except Exception as e:
        log.error(f"❌ Reply card versturen mislukt: {e}")

def send_my_tickets_card(room_id, sender_email):
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return
    user_lower = (sender_email or "").lower()
    tickets = ROOM_TICKETS_BY_USER.get(room_id, {}).get(user_lower, [])
    items = []
    for t in tickets:
        status = (TICKET_STATUS_TRACKER.get(str(t)) or {}).get("status", "onbekend")
        items.append({
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "auto", "items": [{"type": "TextBlock", "text": f"# {t}", "weight": "bolder"}]},
                {"type": "Column", "width": "stretch", "items": [{"type": "TextBlock", "text": f"Status: {status}", "wrap": True}]}
            ]
        })
    if not items:
        items = [{"type": "TextBlock", "text": "Je hebt nog geen gekoppelde tickets in deze ruimte.", "wrap": True}]

    card = {
        "roomId": room_id,
        "text": "Jouw tickets",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": "📋 Jouw tickets in deze ruimte", "weight": "bolder", "size": "medium"},
                    {"type": "Container", "items": items, "spacing": "small"}
                ],
                "actions": [
                    {"type": "Action.Submit", "title": "💬 Reageer op ticket", "data": {"formType": "openReply", "sender": user_lower}}
                ]
            }
        }]
    }
    try:
        requests.post("https://webexapis.com/v1/messages", headers=WEBEX_HEADERS, json=card, timeout=10)
        log.info(f"✅ My tickets card verstuurd naar room {room_id}")
    except Exception as e:
        log.error(f"❌ My tickets card versturen mislukt: {e}")

# --------------------------------------------------------------------------
# HALO TICKETS
# --------------------------------------------------------------------------
def create_halo_ticket(form, room_id):
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return
    h = get_halo_headers()
    user = get_user(form["email"])
    if not user:
        send_message(room_id, "❌ Geen gebruiker gevonden in Halo.")
        return
    log.info(f"✅ Gebruiker gevonden: {user.get('emailaddress')}")
    details = (
        "### 📝 Nieuwe melding details\n\n"
        f"- **Omschrijving:** {form['omschrijving']}\n"
        f"- **Sinds wanneer:** {form.get('sindswanneer', '-')}\n"
        f"- **Wat werkt niet:** {form.get('watwerktniet', '-')}\n"
        f"- **Zelf geprobeerd:** {form.get('zelfgeprobeerd', '-')}\n"
    )
    if form.get('impacttoelichting', '').strip():
        details += f"- **Impact toelichting:** {form['impacttoelichting']}\n"
    
    body = {
        "summary": form["omschrijving"][:100],
        "details": details,
        "tickettype_id": HALO_TICKET_TYPE_ID,
        "impact": int(form.get("impact", "3")),
        "urgency": int(form.get("urgency", "3")),
        "client_id": int(user.get("client_id", HALO_CLIENT_ID_NUM)),
        "site_id": int(user.get("site_id", HALO_SITE_ID)),
        "user_id": int(user["id"])
    }
    url = f"{HALO_API_BASE}/api/Tickets"
    log.info(f"➡️ Creëer Halo ticket met body: {json.dumps(body, indent=2)}")
    
    r = halo_request(url, method='POST', headers=h, json=[body])
    
    if not r.ok:
        send_message(room_id, f"⚠️ Ticket aanmaken mislukt: {r.status_code}")
        log.error(f"❌ Halo ticket aanmaken mislukt: {r.status_code} - {r.text}")
        return
    response = r.json()
    if isinstance(response, list) and response:
        ticket = response[0]
    elif isinstance(response, dict):
        ticket = response.get("data") or response.get("tickets", [None])[0] or response
    else:
        ticket = response
    
    tid = str(ticket.get("TicketNumber") or ticket.get("id") or ticket.get("TicketID") or "")
    current_status = ticket.get("Status") or ticket.get("status") or ticket.get("StatusName") or "Unknown"
    
    # Converteer status ID naar naam als nodig
    if isinstance(current_status, int) or (isinstance(current_status, str) and current_status.isdigit()):
        current_status = get_status_name(current_status)
    
    if not tid:
        send_message(room_id, "❌ Ticket aangemaakt, maar geen ID gevonden")
        log.error("❌ Geen ticket ID gevonden in Halo response")
        return
    log.info(f"✅ Ticket aangemaakt: {tid}")
    
    if room_id not in USER_TICKET_MAP:
        USER_TICKET_MAP[room_id] = []
    USER_TICKET_MAP[room_id].append(tid)
    # Koppel ticket aan opgegeven eindgebruikers-e-mailadres in deze room
    owner_email = (form.get("email") or "").strip().lower()
    if owner_email:
        ROOM_TICKETS_BY_USER.setdefault(room_id, {}).setdefault(owner_email, []).append(tid)
    
    TICKET_STATUS_TRACKER[tid] = {"status": current_status, "last_checked": time.time()}
    # Initialiseer ACTION_TRACKER op huidige hoogste action-id zodat we geen oude acties posten
    try:
        latest_actions = get_actions_for_ticket(int(tid), None)
        if latest_actions:
            last = latest_actions[-1]
            ACTION_TRACKER[str(tid)] = int(last.get("_id_int", last.get("id", 0)) or 0)
    except Exception as e:
        log.warning(f"⚠️ Kon initiële Actions niet ophalen voor ticket {tid}: {e}")
    
    send_message(room_id, f"✅ Ticket aangemaakt: **{tid}**")
    log.info(f"✅ Ticket {tid} toegevoegd aan room {room_id}")
    return tid

# -------------------------------------------------------------------------- 
# HALO ACTIONS POLLING (statuswijzigingen + agent notes)
# -------------------------------------------------------------------------- 
def get_actions_for_ticket(ticket_id, last_seen_action_id):
    """Haalt Actions op voor één ticket. Filtert niets uit zodat systeem- en privé-acties zichtbaar zijn.
    Retourneert op id gesorteerde lijst en respecteert last_seen_action_id indien opgegeven.
    """
    h = get_halo_headers()
    params = {
        "ticket_id": int(ticket_id),
        "count": 50,
        "includehtmlnote": True,
        "includeattachments": True,
        # Belangrijk: GEEN excludesys / excludeprivate / agentonly / conversationonly / emailonly / slaonly
    }
    url = f"{HALO_API_BASE}/api/Actions"
    r = halo_request(url, headers=h, params=params)
    if r.status_code != 200:
        log.warning(f"⚠️ Actions ophalen voor ticket {ticket_id} gaf {r.status_code}: {r.text[:200]}")
        return []
    data = r.json()
    # Accepteer meerdere mogelijke containers
    actions = []
    if isinstance(data, list):
        actions = data
    elif isinstance(data, dict):
        actions = data.get("root") or data.get("actions") or data.get("items") or []
    
    # Normaliseer id naar int en sorteer
    norm_actions = []
    for a in actions:
        try:
            a_id = int(a.get("id") or a.get("ActionId") or a.get("action_id") or 0)
        except Exception:
            a_id = 0
        a["_id_int"] = a_id
        norm_actions.append(a)
    norm_actions.sort(key=lambda x: x.get("_id_int", 0))
    
    if last_seen_action_id is not None:
        norm_actions = [a for a in norm_actions if a.get("_id_int", 0) > int(last_seen_action_id)]
    return norm_actions

def _extract_status_change(action: dict):
    # Zoek diverse veldnamen voor status van/naar
    old_s = action.get("statusfrom") or action.get("status_from") or action.get("oldstatus") or action.get("fromstatus")
    new_s = action.get("statusto") or action.get("status_to") or action.get("newstatus") or action.get("tostatus")
    # Soms zitten de namen in sub-objecten
    if isinstance(action.get("status"), dict):
        new_s = new_s or action["status"].get("name")
    return old_s, new_s

def classify_action_to_message(ticket_id, action):
    """Maakt een Webex-bericht o.b.v. een action. Geeft None terug als we het negeren."""
    a_type = (action.get("type") or action.get("actiontype") or action.get("ActionType") or "").lower()
    a_note_html = action.get("htmlbody") or action.get("notehtml") or action.get("notebody") or action.get("body") or action.get("outcome")
    is_private = bool(action.get("private") or action.get("isprivate") or action.get("private_note"))

    # 1) Statuswijziging (system action)
    old_s, new_s = _extract_status_change(action)
    if ("status" in a_type) or new_s:
        if isinstance(new_s, (int, str)) and str(new_s).isdigit():
            new_s = get_status_name(int(new_s))
        if isinstance(old_s, (int, str)) and str(old_s).isdigit():
            old_s = get_status_name(int(old_s))
        old_s = old_s or "onbekend"
        new_s = new_s or "onbekend"
        return f"⚠️ Statuswijziging voor ticket #{ticket_id}: {old_s} → {new_s}"

    # 2) Agent note (privé of publiek)
    if ("note" in a_type) or a_note_html:
        author = action.get("agentname") or action.get("createdby") or action.get("author") or "Agent"
        visibility = "(privé) " if is_private else ""
        content = (a_note_html or "").strip()
        if not content:
            return None
        return f"📝 {visibility}Nieuwe agent note van {author} op ticket #{ticket_id}:\n{content}"

    # Andere types (email, sla-hold, etc.) negeren voorlopig
    return None

def check_ticket_actions():
    # Verzamel unieke ticket-ids uit alle rooms
    all_ticket_ids = set()
    for tickets in USER_TICKET_MAP.values():
        for t in tickets:
            all_ticket_ids.add(str(t))
    if not all_ticket_ids:
        return

    for tid in list(all_ticket_ids):
        last_seen = ACTION_TRACKER.get(str(tid))
        try:
            actions = get_actions_for_ticket(int(tid), last_seen)
        except Exception as e:
            log.error(f"❌ Fout bij ophalen Actions voor ticket {tid}: {e}")
            continue

        if not actions:
            continue

        # Bepaal hoogste id en verwerk berichten
        max_id = last_seen or 0
        for a in actions:
            a_id = a.get("_id_int", 0)
            msg = classify_action_to_message(str(tid), a)
            if msg:
                # Zoek passende room
                room_id = None
                for rid, tickets in USER_TICKET_MAP.items():
                    if str(tid) in tickets:
                        room_id = rid
                        break
                if room_id:
                    send_message(room_id, msg)
            if a_id > max_id:
                max_id = a_id
        ACTION_TRACKER[str(tid)] = int(max_id)

# -------------------------------------------------------------------------- 
# PUBLIC NOTE FUNCTIE (CORRECTE IMPLEMENTATIE)
# -------------------------------------------------------------------------- 
def add_public_note(ticket_id, text):
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return False
    h = get_halo_headers()
    url = f"{HALO_API_BASE}/api/Actions"
    payload = [
        {
            "Ticket_Id": int(ticket_id),
            "ActionId": ACTION_ID_PUBLIC,
            "outcome": text
        }
    ]
    log.info(f"➡️ Sturen public note naar Halo voor ticket {ticket_id}: {text}")
    
    r = halo_request(url, method='POST', headers=h, json=payload)
    
    if r.status_code in [200, 201]:
        log.info(f"✅ Public note succesvol toegevoegd aan ticket {ticket_id}")
        return True
    else:
        log.error(f"❌ Public note mislukt: {r.status_code} - {r.text}")
        return False

# -------------------------------------------------------------------------- 
# STATUS WIJZIGINGEN
# -------------------------------------------------------------------------- 
def check_ticket_status_changes():
    h = get_halo_headers()
    for ticket_id, status_info in list(TICKET_STATUS_TRACKER.items()):
        try:
            # Haal volledige ticket details met includedetails=true
            url = f"{HALO_API_BASE}/api/Tickets/{ticket_id}"
            log.info(f"➡️ Controleer status van ticket {ticket_id}")
            r = halo_request(url, headers=h, params={"includedetails": True})
            
            if r.status_code == 200:
                ticket_data = r.json()
                
                # Check alle mogelijke statusvelden (top-level en nested)
                current_status = ticket_data.get("Status") or \
                                 ticket_data.get("status") or \
                                 ticket_data.get("StatusName") or \
                                 ticket_data.get("status_name") or \
                                 ticket_data.get("StatusID") or \
                                 ticket_data.get("ticket_status", {}).get("name") or \
                                 ticket_data.get("status", {}).get("name") or \
                                 ticket_data.get("status", {}).get("status") or \
                                 ticket_data.get("status", {}).get("Status") or \
                                 ticket_data.get("status", {}).get("StatusName") or \
                                 "Unknown"
                
                # Converteer status ID naar naam als nodig
                if isinstance(current_status, int) or (isinstance(current_status, str) and current_status.isdigit()):
                    current_status = get_status_name(current_status)
                
                if current_status != status_info["status"]:
                    TICKET_STATUS_TRACKER[ticket_id]["status"] = current_status
                    room_id = None
                    for rid, tickets in USER_TICKET_MAP.items():
                        if ticket_id in tickets:
                            room_id = rid
                            break
                    
                    if room_id:
                        send_message(room_id, f"⚠️ **Statuswijziging voor ticket #{ticket_id}**\n- Oude status: {status_info['status']}\n- Nieuwe status: {current_status}")
                        log.info(f"✅ Statuswijziging gestuurd naar room {room_id}")
                    else:
                        log.warning(f"⚠️ Geen Webex-room gevonden voor ticket {ticket_id}")
                    log.info(f"✅ Statuswijziging gedetecteerd voor ticket {ticket_id}: {status_info['status']} → {current_status}")
            else:
                log.warning(f"⚠️ Ticket status check mislukt voor {ticket_id}: {r.status_code}")
        except Exception as e:
            log.error(f"💥 Fout bij statuscheck voor ticket {ticket_id}: {e}")

# -------------------------------------------------------------------------- 
# WEBEX EVENTS
# -------------------------------------------------------------------------- 
def process_webex_event(payload):
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return
    res = payload.get("resource")
    log.info(f"📩 Verwerken Webex event: resource={res}")
    if res == "messages":
        mid = payload["data"]["id"]
        log.info(f"📩 Verwerken bericht: id={mid}")
        msg = requests.get(f"https://webexapis.com/v1/messages/{mid}", headers=WEBEX_HEADERS).json()
        text = msg.get("text", "")
        room_id = msg.get("roomId")
        sender = msg.get("personEmail", "")
        log.info(f"📩 Bericht ontvangen van {sender} in room {room_id}: '{text}'")
        if sender and sender.endswith("@webex.bot"):
            log.info("❌ Bericht is van de bot zelf, negeren")
            return
        
        # Trigger voor keuze-kaart om een ticket te selecteren
        triggers = ["reageer", "reactie", "reply", "note", "notitie", "kies ticket"]
        if any(t in text.lower() for t in triggers):
            send_reply_card(room_id, sender)
            return

        # Toon kaart met eigen tickets
        if any(t in text.lower() for t in ["mijn tickets", "tickets", "lijst", "overzicht"]):
            send_my_tickets_card(room_id, sender)
            return

        # Help / menu
        if any(t == text.lower().strip() for t in ["help", "?", "menu"]):
            send_message(
                room_id,
                """
                **🚀 Webex Halo bot — snelle hulp**
                - Typ `nieuwe melding` om een ticket aan te maken
                - Typ `reageer` om een ticket te kiezen en een reactie te sturen
                - Typ `mijn tickets` voor een overzicht van jouw tickets
                - Typ `link #12345` om een bestaand ticket aan jezelf te koppelen
                - Typ `Ticket #12345 jouw bericht` om direct te reageren op dat ticket
                """
            )
            return

        # Link bestaand ticket aan afzender: "link #12345" of "koppel #12345"
        m_link = re.search(r"(?:link|koppel)\s*#?(\d+)", text.lower())
        if m_link:
            link_tid = m_link.group(1)
            link_ticket_to_user(room_id, sender, link_tid)
            return

        if "nieuwe melding" in text.lower():
            log.info("ℹ️ Bericht bevat 'nieuwe melding', stuur adaptive card")
            send_message(room_id, "👋 Hi! Je hebt 'nieuwe melding' gestuurd. Klik op de knop hieronder om een ticket aan te maken:\n\n"
                                 "Je kunt ook een bericht sturen met 'Ticket #<nummer>' om een reactie te geven aan een specifiek ticket.")
            send_adaptive_card(room_id)
            return
        
        ticket_match = re.search(r'Ticket #(\d+)', text)
        if ticket_match:
            requested_tid = ticket_match.group(1)
            log.info(f"ℹ️ Bericht bevat ticket #{requested_tid}")
            # Alleen toestaan als dit ticket aan afzender gekoppeld is binnen deze room
            sender_email = (sender or "").strip().lower()
            allowed = False
            if room_id in ROOM_TICKETS_BY_USER and sender_email in ROOM_TICKETS_BY_USER[room_id]:
                allowed = requested_tid in ROOM_TICKETS_BY_USER[room_id][sender_email]
            if not allowed and room_id in USER_TICKET_MAP:
                # Fallback: als we nog geen per-gebruiker mapping hebben, val terug op oude lijst
                allowed = requested_tid in USER_TICKET_MAP[room_id]

            if allowed:
                log.info(f"✅ Ticket #{requested_tid} toegestaan voor afzender {sender_email} in room {room_id}")
                success = add_public_note(requested_tid, text)
                if success:
                    send_message(room_id, f"📝 Bericht toegevoegd aan jouw ticket #{requested_tid}.")
                else:
                    send_message(room_id, f"❌ Kan geen notitie toevoegen aan ticket #{requested_tid}. Probeer het opnieuw.")
            else:
                log.info(f"❌ Ticket #{requested_tid} is niet gekoppeld aan afzender {sender_email} in deze room")
                send_message(room_id, f"❌ Ticket #{requested_tid} is niet gekoppeld aan jou in deze room.")
        else:
            # Geen ticketnummer: voeg alleen toe aan tickets van de afzender in deze room
            sender_email = (sender or "").strip().lower()
            user_tickets = ROOM_TICKETS_BY_USER.get(room_id, {}).get(sender_email, [])
            if user_tickets:
                errors = 0
                for tid in user_tickets:
                    success = add_public_note(tid, text)
                    if not success:
                        errors += 1
                if errors:
                    send_message(room_id, f"⚠️ Bericht toegevoegd aan {len(user_tickets)-errors} van jouw tickets; {errors} mislukt.")
                else:
                    send_message(room_id, f"📝 Bericht toegevoegd aan jouw {len(user_tickets)} ticket(s) in deze room.")
            else:
                send_message(room_id, "ℹ️ Ik vond geen tickets die aan jou gekoppeld zijn in deze room. Voeg 'Ticket #<nummer>' toe of maak eerst een ticket aan.")
    elif res == "attachmentActions":
        a_id = payload["data"]["id"]
        log.info(f"📩 Verwerken attachmentActions: id={a_id}")
        action_payload = requests.get(f"https://webexapis.com/v1/attachment/actions/{a_id}",
                              headers=WEBEX_HEADERS).json()
        inputs = action_payload.get("inputs", {})
        room_id = payload["data"]["roomId"]
        log.info(f"📩 attachmentActions in room {room_id} met inputs: {json.dumps(inputs, indent=2)}")
        form_type = (inputs.get("formType") or inputs.get("form_type") or "").lower()
        if form_type == "reply":
            # Reply form afhandelen
            sender = (inputs.get("sender") or "").lower()
            selected = (inputs.get("ticket") or "").strip()
            manual = (inputs.get("ticket_manual") or "").strip()
            message = (inputs.get("message") or "").strip()
            tid = manual if manual else selected
            if not (tid and tid.isdigit()):
                send_message(room_id, "❌ Geen geldig ticketnummer opgegeven.")
                return
            # Controleer of ticket aan afzender gekoppeld is
            allowed = tid in ROOM_TICKETS_BY_USER.get(room_id, {}).get(sender, [])
            if not allowed:
                send_message(room_id, f"❌ Ticket #{tid} is niet aan jou gekoppeld in deze room.")
                return
            ok = add_public_note(tid, message)
            if ok:
                send_message(room_id, f"📝 Reactie geplaatst op ticket #{tid}.")
            else:
                send_message(room_id, f"❌ Reactie plaatsen op ticket #{tid} mislukt.")
        elif form_type == "openReply":
            sender = (inputs.get("sender") or "").lower()
            send_reply_card(room_id, sender)
        else:
            # Default: oude kaart voor nieuw ticket
            create_halo_ticket(inputs, room_id)
    else:
        log.info(f"ℹ️ Onbekende resource type: {res}")

# -------------------------------------------------------------------------- 
# HALO ACTION BUTTON WEBHOOK - GEREDUCEERDE LOGGING
# -------------------------------------------------------------------------- 
@app.route("/halo-action", methods=["POST"])
def halo_action():
    # --- CRUCIALE AUTHENTICATIE CHECK ---
    auth = request.authorization
    if not auth or auth.username != "Webexbot" or auth.password != "Webexbot2025":
        log.error("❌ Ongeldige credentials voor Halo webhook")
        return {"status": "unauthorized"}, 401
    
    # --- GEREDUCEERDE LOGGING VAN WEBHOOK DATA ---
    data = request.json if request.is_json else request.form.to_dict()
    
    # Log alleen relevante velden in plaats van volledige JSON
    relevant_data = {
        "ticket_id": data.get("ticket_id") or data.get("TicketId") or data.get("TicketNumber"),
        "status": data.get("status") or data.get("Status"),
        "action_id": data.get("actionid") or data.get("ActionId"),
        "note": data.get("outcome") or data.get("note") or data.get("text"),
        "assigned_agent": data.get("assigned_to") or data.get("assignedTo"),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    log.info(f"📥 HALO WEBHOOK DATA: {json.dumps(relevant_data, indent=2)}")
    
    # Controleer alle mogelijke veldnamen voor ticket ID
    ticket_id = None
    for f in ["ticket_id", "TicketId", "TicketID", "TicketNumber", "id", "Ticket_Id", "ticketnumber", "Ticket_ID", "ticketid", "TicketID", "TicketID", "ticket_id", "TicketID"]:
        if f in data:
            ticket_id = data[f]
            break
    
    # Controleer alle mogelijke veldnamen voor notitietekst
    note_text = None
    for f in ["outcome", "note", "text", "comment", "description", "public_note", "note_text", "comment_text", "action_description", "notecontent", "NoteContent", "note_body", "NoteBody", "note_text", "NoteText", "action_note", "ActionNote"]:
        if f in data:
            note_text = data[f]
            break
    
    # Controleer alle mogelijke veldnamen voor status
    status_change = None
    for f in ["status", "Status", "status_name", "statusName", "status_id", "StatusID", "ticketstatus", "TicketStatus", "current_status", "NewStatus", "NewStatusName", "status_value", "StatusValue", "status_name", "StatusName", "status_text", "StatusText", "newstatus", "NewStatus", "status_id", "StatusID"]:
        if f in data:
            status_change = data[f]
            break
    
    # Controleer alle mogelijke veldnamen voor toegewezen agent
    assigned_agent = None
    for f in ["assigned_to", "assignedTo", "assignedagent", "agent", "AssignedAgent", "assigned_by", "assignedBy", "assignedby", "AssignedBy", "assigned_to_name", "assignedToName", "agent_name", "AgentName", "assigned_to_id", "AssignedToID", "assigned_by_id", "AssignedByID"]:
        if f in data:
            assigned_agent = data[f]
            break
    
    # Controleer alle mogelijke veldnamen voor actie ID
    action_id = None
    for f in ["actionid", "ActionId", "action_id", "action", "Action", "action_id", "action_id", "ActionID", "action_id", "ActionType", "action_type", "action_type_id", "ActionTypeID"]:
        if f in data:
            action_id = data[f]
            break
    
    # Als we geen ticket_id hebben, log dit en stopt
    if not ticket_id:
        log.warning("❌ Geen ticket_id gevonden in webhook data")
        return {"status": "ignore"}
    
    # Zorg dat ticket_id een string is voor consistentie
    ticket_id = str(ticket_id)
    
    # Zoek de room waar dit ticket in zit
    room_id = None
    for rid, tickets in USER_TICKET_MAP.items():
        if ticket_id in tickets:
            room_id = rid
            break
    
    if not room_id:
        log.warning(f"❌ Geen Webex-room gevonden voor ticket {ticket_id}")
        return {"status": "ignore"}
    
    # Verwerk public notes
    if note_text and action_id and str(action_id) == str(ACTION_ID_PUBLIC):
        log.info(f"✅ Public note ontvangen voor ticket {ticket_id}")
        send_message(room_id, f"📥 **Nieuwe public note vanuit Halo:**\n{note_text}")
        return {"status": "ok"}
    
    # Verwerk statuswijzigingen (converteer ID naar naam)
    if status_change:
        # Converteer status ID naar naam als nodig
        if isinstance(status_change, int) or (isinstance(status_change, str) and status_change.isdigit()):
            status_name = get_status_name(status_change)
            log.info(f"✅ Status ID {status_change} geconverteerd naar naam: {status_name}")
            status_change = status_name
        else:
            status_name = status_change
        
        log.info(f"✅ Statuswijziging ontvangen voor ticket {ticket_id}: {status_change}")
        send_message(room_id, f"⚠️ **Statuswijziging voor ticket #{ticket_id}**\n- Nieuwe status: {status_change}")
        return {"status": "ok"}
    
    # Verwerk toewijzingen
    if assigned_agent:
        log.info(f"✅ Toewijzing ontvangen voor ticket {ticket_id}: {assigned_agent}")
        send_message(room_id, f"✅ **Ticket #{ticket_id} is toegewezen aan {assigned_agent}**")
        return {"status": "ok"}
    
    # Als geen van de bovenstaande gevalen, log en stopt
    log.warning("❌ Geen herkenbare actie in webhook data")
    return {"status": "ignore"}

# -------------------------------------------------------------------------- 
# ROUTES
# -------------------------------------------------------------------------- 
@app.route("/webex", methods=["POST"])
def webex_hook():
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return {"status": "ignore"}
    threading.Thread(target=process_webex_event, args=(request.json,)).start()
    return {"status": "ok"}

@app.route("/initialize", methods=["GET"])
def initialize():
    if not WEBEX_HEADERS:
        log.error("❌ WEBEX_HEADERS is niet ingesteld")
        return {"status": "error", "message": "WEBEX_BOT_TOKEN is niet ingesteld"}
    get_users()
    # Gebruik Actions-polling als primaire updatebron
    threading.Thread(target=action_check_loop, daemon=True).start()
    return {
        "status": "initialized",
        "source": f"client{HALO_CLIENT_ID_NUM}_site{HALO_SITE_ID}",
        "cached_users": len(USER_CACHE["users"])
    }

def status_check_loop():
    while True:
        try:
            check_ticket_status_changes()
            time.sleep(60)
        except Exception as e:
            log.error(f"💥 Fout bij status check loop: {e}")
            time.sleep(60)

def action_check_loop():
    while True:
        try:
            check_ticket_actions()
            # Iets sneller dan status-check zodat notes vlot doorkomen
            time.sleep(30)
        except Exception as e:
            log.error(f"💥 Fout bij actions check loop: {e}")
            time.sleep(30)

# -------------------------------------------------------------------------- 
# LINK BESTAAND TICKET AAN GEBRUIKER IN ROOM
# -------------------------------------------------------------------------- 
def fetch_ticket_details(ticket_id):
    h = get_halo_headers()
    url = f"{HALO_API_BASE}/api/Tickets/{ticket_id}"
    r = halo_request(url, headers=h, params={"includedetails": True})
    if r.status_code != 200:
        raise RuntimeError(f"Ticket {ticket_id} ophalen mislukt: {r.status_code}")
    return r.json()

def _extract_ticket_status(ticket_data):
    current_status = ticket_data.get("Status") or \
                     ticket_data.get("status") or \
                     ticket_data.get("StatusName") or \
                     ticket_data.get("status_name") or \
                     ticket_data.get("StatusID") or \
                     (ticket_data.get("ticket_status") or {}).get("name") or \
                     (ticket_data.get("status") or {}).get("name") or \
                     (ticket_data.get("status") or {}).get("status") or \
                     "Unknown"
    if isinstance(current_status, int) or (isinstance(current_status, str) and str(current_status).isdigit()):
        current_status = get_status_name(current_status)
    return current_status

def _ticket_belongs_to_user(ticket_data, user_obj, sender_email_lower):
    # 1) Matching user id
    t_uid = ticket_data.get("user_id") or ticket_data.get("UserID") or ticket_data.get("userid")
    if t_uid and user_obj and int(t_uid) == int(user_obj.get("id")):
        return True
    # 2) Matching email
    possible_emails = [
        ticket_data.get("user", {}).get("emailaddress") if isinstance(ticket_data.get("user"), dict) else None,
        ticket_data.get("UserEmail"), ticket_data.get("EmailAddress"), ticket_data.get("emailaddress"),
        ticket_data.get("useremail"), ticket_data.get("user_email"),
    ]
    possible_emails = [e.lower() for e in possible_emails if isinstance(e, str)]
    return sender_email_lower in possible_emails

def link_ticket_to_user(room_id, sender_email, ticket_id):
    sender_lower = (sender_email or "").strip().lower()
    try:
        user_obj = get_user(sender_lower)
        if not user_obj:
            send_message(room_id, "❌ Ik vond jouw gebruiker niet in Halo op basis van je e-mailadres.")
            return
        tdata = fetch_ticket_details(ticket_id)
        if not _ticket_belongs_to_user(tdata, user_obj, sender_lower):
            send_message(room_id, f"❌ Ticket #{ticket_id} lijkt niet van jou te zijn.")
            return
        # Registreer mapping
        ROOM_TICKETS_BY_USER.setdefault(room_id, {}).setdefault(sender_lower, [])
        if str(ticket_id) not in ROOM_TICKETS_BY_USER[room_id][sender_lower]:
            ROOM_TICKETS_BY_USER[room_id][sender_lower].append(str(ticket_id))
        USER_TICKET_MAP.setdefault(room_id, [])
        if str(ticket_id) not in USER_TICKET_MAP[room_id]:
            USER_TICKET_MAP[room_id].append(str(ticket_id))
        # Status tracker
        TICKET_STATUS_TRACKER[str(ticket_id)] = {"status": _extract_ticket_status(tdata), "last_checked": time.time()}
        # Action tracker op laatst bekende actie zetten
        try:
            latest = get_actions_for_ticket(int(ticket_id), None)
            if latest:
                ACTION_TRACKER[str(ticket_id)] = int(latest[-1].get("_id_int", latest[-1].get("id", 0)) or 0)
        except Exception as e:
            log.warning(f"⚠️ Kon Actions voor ticket {ticket_id} niet initialiseren: {e}")
        send_message(room_id, f"🔗 Ticket #{ticket_id} is gekoppeld aan jou in deze ruimte. Je ontvangt updates en kunt nu reageren.")
    except Exception as e:
        log.error(f"❌ Linken van ticket {ticket_id} mislukt: {e}")
        send_message(room_id, f"❌ Linken van ticket #{ticket_id} is mislukt.")

# -------------------------------------------------------------------------- 
# START
# -------------------------------------------------------------------------- 
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"🚀 Start server op poort {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
