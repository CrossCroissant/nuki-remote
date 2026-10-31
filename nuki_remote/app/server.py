"""Nuki Remote – kleine Webseite, die die Nuki-Haustür über geschützte HA-Skripte steuert.

Der Browser spricht nur mit diesem Server. Das HA-Token bleibt hier; erlaubt sind
ausschließlich Status lesen, PIN prüfen, Sitzung beenden, Tür öffnen und Lock 'n' Go.
Freigabe und PIN-Sitzung werden zusätzlich in den HA-Skripten selbst geprüft.
"""
import asyncio
import hmac
import json
import logging
import os
import ssl
import time
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession, ClientTimeout, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nuki_remote")

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
HA_URL = os.environ.get("HA_URL", "http://supervisor/core/api")
S6_ENV_DIR = "/run/s6/container_environment"


def env(name):
    """Umgebungsvariable lesen; s6-overlay reicht sie nicht immer an CMD weiter, daher auch aus S6_ENV_DIR."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        with open(os.path.join(S6_ENV_DIR, name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


HA_TOKEN = os.environ.get("HA_TOKEN") or env("SUPERVISOR_TOKEN")
PORT = int(os.environ.get("PORT", "8099"))

FREIGABE = "input_boolean.nuki_remote_erlaubt"
SITZUNG = "input_datetime.nuki_pin_sitzung_bis"
SPERRE = "input_datetime.nuki_pin_sperre_bis"
ZAEHLER = "counter.nuki_pin_fehlversuche"
STATUS_ENTITIES = {
    "lock": "lock.nuki",
    "lock_state": "sensor.nuki_lock_state",
    "door_state": "sensor.nuki_door_state",
    "battery": "sensor.nuki_battery",
    "battery_critical": "binary_sensor.nuki_battery_critical",
    "last_action": "sensor.nuki_last_lock_action",
    "last_trigger": "sensor.nuki_last_action_trigger",
    "last_user": "sensor.nuki_last_action_user_name",
    "command_status": "sensor.nuki_last_nuki_command_status",
    "freigabe": FREIGABE,
    "sitzung": SITZUNG,
    "sperre": SPERRE,
    "fehlversuche": ZAEHLER,
}
VIPER = {
    "weg": "input_boolean.viper_ist_weg",
    "seit": "input_datetime.viper_draussen_seit",
    "ausfluege": "counter.viper_ausfluge_heute",
    "letzter_min": "input_number.viper_letzter_ausflug_minuten",
}
MIN_PIN_INTERVAL = 2.0  # Sekunden zwischen PIN-Versuchen pro IP


def load_options():
    with open(OPTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def current_options(app):
    """Optionen bei jeder PIN-Eingabe neu lesen, damit (De-)Aktivieren ohne Neustart greift."""
    try:
        app["options"] = load_options()
    except (OSError, ValueError) as e:
        log.warning("Optionen nicht lesbar, nutze letzte bekannte: %s", e)
    return app["options"]


def match_pin(eingabe, pins):
    """Passenden PIN-Eintrag finden; vergleicht immer alle Einträge (konstante Laufzeit)."""
    treffer = None
    for eintrag in pins:
        if hmac.compare_digest(eingabe.encode(), str(eintrag.get("pin", "")).encode()) and treffer is None:
            treffer = eintrag
    return treffer


class HA:
    def __init__(self, session: ClientSession):
        self.s = session
        self.headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

    async def states(self):
        async with self.s.get(f"{HA_URL}/states", headers=self.headers) as r:
            r.raise_for_status()
            return {e["entity_id"]: e for e in await r.json()}

    async def script(self, name, data):
        url = f"{HA_URL}/services/script/{name}?return_response"
        async with self.s.post(url, headers=self.headers, json=data) as r:
            r.raise_for_status()
            body = await r.json()
            return body.get("service_response") or {}

    async def logbook(self, hours=24):
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        url = f"{HA_URL}/logbook/{start}"
        async with self.s.get(url, headers=self.headers, params={"entity": "lock.nuki"}) as r:
            r.raise_for_status()
            return await r.json()


def client_ip(request: web.Request) -> str:
    return request.remote or "?"


def quelle(request: web.Request, name: str | None = None) -> str:
    name = name if name is not None else request.app.get("sitzung_von")
    return f"{name} – Webseite ({client_ip(request)})" if name else f"Webseite ({client_ip(request)})"


def viper_status(st):
    """Viper-Status; wird nur während einer gültigen PIN-Sitzung ausgeliefert."""
    weg = st.get(VIPER["weg"])
    if not weg:
        return None

    def zahl(key):
        try:
            return int(float(st.get(VIPER[key], {}).get("state")))
        except (TypeError, ValueError):
            return None

    seit = ts_attr(st.get(VIPER["seit"]))
    unterwegs = weg.get("state") == "on"
    return {
        "unterwegs": unterwegs,
        "seit": seit if unterwegs and seit else None,
        "dauer_s": max(0, int(time.time() - seit)) if unterwegs and seit else None,
        "ausfluege_heute": zahl("ausfluege"),
        "letzter_ausflug_min": zahl("letzter_min"),
    }


def ts_attr(state):
    try:
        return float((state or {}).get("attributes", {}).get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0


@web.middleware
async def security_headers(request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return resp


def json_error(status, text, **extra):
    return web.json_response({"ok": False, "text": text, **extra}, status=status)


async def index(request):
    return web.FileResponse(os.path.join(HERE, "static", "index.html"))


async def status(request):
    ha: HA = request.app["ha"]
    try:
        st = await ha.states()
    except Exception as e:  # noqa: BLE001
        log.warning("HA nicht erreichbar: %s", e)
        return json_error(502, "Home Assistant nicht erreichbar")
    now = time.time()
    get = lambda key: (st.get(STATUS_ENTITIES[key]) or {}).get("state", "unavailable")  # noqa: E731
    sitzung_rest = max(0, int(ts_attr(st.get(SITZUNG)) - now))
    sperre_rest = max(0, int(ts_attr(st.get(SPERRE)) - now))
    freigabe = get("freigabe") == "on"
    return web.json_response({
        "ok": True,
        "lock": get("lock"),
        "lock_state": get("lock_state"),
        "door_state": get("door_state"),
        "battery": get("battery"),
        "battery_critical": get("battery_critical") == "on",
        "last_action": get("last_action"),
        "last_trigger": get("last_trigger"),
        "last_user": get("last_user"),
        "command_status": get("command_status"),
        "last_changed": (st.get("lock.nuki") or {}).get("last_changed"),
        "freigabe": freigabe,
        "sitzung_rest": sitzung_rest if freigabe else 0,
        "sitzung_von": request.app.get("sitzung_von") if freigabe and sitzung_rest > 0 else None,
        "viper": viper_status(st) if freigabe and sitzung_rest > 0 else None,
        "sperre_rest": sperre_rest,
        "fehlversuche": int(float(get("fehlversuche") or 0)) if get("fehlversuche") not in ("unknown", "unavailable") else 0,
        "max_fehlversuche": request.app["options"]["max_fehlversuche"],
    })


async def verlauf(request):
    ha: HA = request.app["ha"]
    try:
        entries = await ha.logbook()
    except Exception as e:  # noqa: BLE001
        log.warning("Logbuch nicht abrufbar: %s", e)
        return json_error(502, "Verlauf nicht abrufbar")
    entries = sorted(entries, key=lambda x: x.get("when", ""), reverse=True)[:25]
    return web.json_response({"ok": True, "eintraege": [
        {"when": e.get("when"), "name": e.get("name"), "message": e.get("message") or e.get("state")}
        for e in entries
    ]})


async def read_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def pin(request):
    ha: HA = request.app["ha"]
    opts = request.app["options"]
    ip = client_ip(request)

    # Einfache Drosselung pro IP, zusätzlich zur zentralen Sperre in HA.
    last = request.app["pin_last"].get(ip, 0)
    if time.monotonic() - last < MIN_PIN_INTERVAL:
        return json_error(429, "Bitte kurz warten")
    request.app["pin_last"][ip] = time.monotonic()

    data = await read_json(request)
    eingabe = str(data.get("pin", ""))[:32]
    opts = current_options(request.app)

    async with request.app["pin_lock"]:
        st = await ha.states()
        if st.get(FREIGABE, {}).get("state") != "on":
            return json_error(403, "Fernsteuerung ist ausgeschaltet", grund="remote_aus")
        sperre_rest = int(ts_attr(st.get(SPERRE)) - time.time())
        if sperre_rest > 0:
            return json_error(423, "PIN-Eingabe gesperrt", grund="gesperrt", sperre_rest=sperre_rest)

        treffer = match_pin(eingabe, opts.get("pins") or [])
        if treffer and treffer.get("aktiv"):
            name = treffer.get("name") or "Unbenannt"
            res = await ha.script("nuki_fern_sitzung_starten",
                                  {"minuten": opts["sitzung_minuten"], "quelle": quelle(request, name)})
            log.info("PIN von %s korrekt (%s) -> %s", name, ip, res)
            if not res.get("ok"):
                return json_error(403, res.get("text", "Abgelehnt"), grund=res.get("grund"))
            request.app["sitzung_von"] = name
            return web.json_response({"ok": True, "name": name})

        # Deaktivierte PINs verhalten sich nach außen wie falsche, im Logbuch steht aber, wessen PIN es war.
        hinweis = f"deaktivierte PIN von {treffer.get('name') or 'Unbenannt'}" if treffer else "falsche PIN"
        res = await ha.script("nuki_fern_pin_fehlversuch", {
            "max_versuche": opts["max_fehlversuche"], "sperre_minuten": opts["sperre_minuten"],
            "quelle": f"{quelle(request, '')} – {hinweis}"})
        log.warning("%s von %s -> %s", hinweis, ip, res)
        if res.get("gesperrt"):
            return json_error(423, "Zu viele Fehlversuche – PIN-Eingabe gesperrt", grund="gesperrt",
                              sperre_rest=opts["sperre_minuten"] * 60)
        return json_error(401, "Falsche PIN", grund="falsch", rest=res.get("rest"))


async def beenden(request):
    res = await request.app["ha"].script("nuki_fern_sitzung_beenden", {"quelle": quelle(request)})
    request.app["sitzung_von"] = None
    log.info("Sitzung beendet von %s", client_ip(request))
    return web.json_response({"ok": bool(res.get("ok"))})


async def aktion(request):
    data = await read_json(request)
    name = data.get("aktion")
    if name not in ("unlock", "lock", "lock_n_go"):
        return json_error(400, "Unbekannte Aktion")
    log.info("Aktion %s angefordert von %s", name, client_ip(request))
    try:
        res = await request.app["ha"].script("nuki_fern_oeffnen", {"aktion": name, "quelle": quelle(request)})
    except Exception as e:  # noqa: BLE001
        log.error("Aktion fehlgeschlagen: %s", e)
        return json_error(502, "Home Assistant hat den Befehl nicht angenommen")
    log.info("Aktion %s -> %s", name, res)
    if not res.get("ok"):
        return json_error(403, res.get("text", "Abgelehnt"), grund=res.get("grund"))
    return web.json_response({"ok": True, "empfangen": True, **res})


async def on_startup(app):
    app["http"] = ClientSession(timeout=ClientTimeout(total=40))
    app["ha"] = HA(app["http"])


async def on_cleanup(app):
    await app["http"].close()


def build_app(options):
    app = web.Application(middlewares=[security_headers], client_max_size=4096)
    app["options"] = options
    app["pin_last"] = {}
    app["sitzung_von"] = None
    app["pin_lock"] = asyncio.Lock()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/", index),
        web.get("/api/status", status),
        web.get("/api/verlauf", verlauf),
        web.post("/api/pin", pin),
        web.post("/api/beenden", beenden),
        web.post("/api/aktion", aktion),
    ])
    return app


def main():
    options = load_options()
    if options.get("pin"):
        log.warning("Option 'pin' ist veraltet und wird ignoriert – PINs bitte unter 'pins' pflegen")
    aktive = [p.get("name") for p in options.get("pins") or [] if p.get("aktiv")]
    log.info("%d PIN(s) konfiguriert, aktiv: %s", len(options.get("pins") or []), ", ".join(aktive) or "keine")
    if not HA_TOKEN:
        raise SystemExit("Kein Zugriffstoken (SUPERVISOR_TOKEN) vorhanden – homeassistant_api aktiv?")
    ssl_ctx = None
    if options.get("ssl"):
        cert = os.path.join("/ssl", options["certfile"])
        key = os.path.join("/ssl", options["keyfile"])
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(cert, key)
    log.info("Nuki Remote startet auf Port %s (%s)", PORT, "HTTPS" if ssl_ctx else "HTTP")
    web.run_app(build_app(options), port=PORT, ssl_context=ssl_ctx, access_log=None, print=None)


if __name__ == "__main__":
    main()
