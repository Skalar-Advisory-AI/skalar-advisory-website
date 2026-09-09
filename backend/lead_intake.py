"""
lead_intake.py - Endpoint-VORSCHLAG fuer POST /lead-intake (skalar AI Kontaktformular).

STATUS: Vorschlag von Ada. Scotty reviewt, haertet und betreibt diesen Service auf
dem VPS hinter dem Cloudflare-Tunnel. NICHT von Ada deployt.

Zweck: nimmt den JSON-POST des Formulars (kontakt-ki.html) entgegen, validiert
serverseitig, schreibt genau EINE append-only JSONL-Zeile pro Lead (atomic:
Lock + fsync), benachrichtigt JARVIS/Marc ueber einen Platzhalter-Hook und
sendet die Emma-Clarke-Empfangsmail NUR wenn das Flag SEND_ACK_EMAIL an ist
(Default False, Freigabe durch Marc steht aus).

DSGVO-Leitplanken (hart):
- opt_in muss serverseitig true sein, sonst kein Persistieren (HTTP 422).
- Datensparsamkeit: NUR die Schema-Felder werden persistiert (Allowlist).
- KEIN IP-Logging, KEIN User-Agent-Logging, KEIN Referer, KEIN Tracking.
- Einwilligungs-Nachweis: nur consent_text_version + timestamp_utc.

Stdlib + Flask. Keine weiteren Abhaengigkeiten.
"""

import os
import re
import sys
import json
import uuid
import fcntl
import datetime

from flask import Flask, request, jsonify

app = Flask(__name__)

# --------------------------------------------------------------------------
# Konfiguration (Scotty verdrahtet die echten Werte am Deploy)
# --------------------------------------------------------------------------

# Ziel-Datei fuer die Leads. Platzhalter-Default; Scotty setzt den echten
# Cross-Container-Pfad (z. B. per ENV LEAD_FILE). Append-only JSONL.
LEAD_FILE = os.environ.get(
    "LEAD_FILE", "/opt/data/profiles/magnet/leads/inbound.jsonl"
)

# Auto-Empfangsmail. DEFAULT FALSE. Marcs Freigabe zum automatischen
# Mailversand steht noch aus. Ada aktiviert das NICHT.
SEND_ACK_EMAIL = os.environ.get("SEND_ACK_EMAIL", "false").lower() == "true"

# Erwarteter Wert der Einwilligungs-Version (Formular sendet ihn mit; wir
# akzeptieren nur den bekannten Stand, sonst setzen wir den Server-Wert).
CONSENT_TEXT_VERSION = "v1-2026-09"

# Feld-Allowlist: NUR diese Felder aus dem Request werden uebernommen.
REQUIRED_FIELDS = ("name", "firma", "email", "ziel_mit_ki")
OPTIONAL_FIELDS = ("rolle", "firmengroesse", "telefon")
CLIENT_META_FIELDS = ("consent_text_version", "seite_sprache", "quelle")

ALLOWED_FIRMENGROESSE = {"unter 10", "10 bis 49", "50 bis 249", "250 und mehr"}
ALLOWED_SPRACHE = {"de", "en"}

# Finale Feld-Reihenfolge im gespeicherten Lead (von Magnet vorgegeben).
LEAD_FIELD_ORDER = (
    "lead_id", "timestamp_utc", "status", "name", "firma", "email", "rolle",
    "firmengroesse", "ziel_mit_ki", "telefon", "opt_in", "consent_text_version",
    "seite_sprache", "quelle",
)

# Honeypot-Feldname (verstecktes Formularfeld). Wenn ausgefuellt -> Bot -> Drop.
HONEYPOT_FIELD = "website"

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Defensive Laengen-Limits (kein DoS ueber Riesenfelder).
MAX_LEN = {
    "name": 200, "firma": 200, "email": 320, "rolle": 200,
    "firmengroesse": 40, "ziel_mit_ki": 4000, "telefon": 60,
    "seite_sprache": 5, "quelle": 60, "consent_text_version": 40,
}


def _log(msg):
    print(f"[lead_intake] {msg}", file=sys.stderr, flush=True)


def _now_utc_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def notify_new_lead(lead):
    """Platzhalter-Hook: benachrichtigt JARVIS/Marc ueber einen neuen Lead.

    Scotty verdrahtet den echten Kanal (Peer-Bus / Cockpit / Telegram).
    Bewusst KEINE personenbezogenen Details ins Log; nur lead_id.
    """
    try:
        _log(f"neuer Lead {lead['lead_id']} (Benachrichtigung: Hook noch nicht verdrahtet)")
    except Exception as e:  # niemals den Request wegen Logging brechen
        _log(f"notify_new_lead Fehler: {type(e).__name__}")


def send_ack_email(lead):
    """Emma-Clarke-Empfangsmail. Wird NUR aufgerufen wenn SEND_ACK_EMAIL True ist.

    Scotty verdrahtet den echten Mailversand. Template unten 1:1 aus Pens Copy.
    """
    sender = "Emma Clarke, Skalar Advisory"
    subject = "Ihre Anfrage bei Skalar Advisory ist angekommen"
    body = (
        "Guten Tag, vielen Dank für Ihre Anfrage zu skalar AI. Sie ist bei uns "
        "eingegangen, und wir melden uns persönlich bei Ihnen. Dies ist eine "
        "automatische Eingangsbestätigung. Ihre Angaben verarbeiten wir "
        "ausschließlich, um Ihre Anfrage zu bearbeiten und mit Ihnen Kontakt "
        "aufzunehmen. Wir geben sie nicht an Dritte weiter. Wie wir mit Ihren "
        "Daten umgehen, steht in unserer Datenschutzerklärung: "
        "https://skalar-advisory.de/datenschutz . Bis in Kürze. "
        "Emma Clarke, Skalar Advisory, https://skalar-advisory.de"
    )
    # Platzhalter: echten SMTP/Mailer-Versand verdrahtet Scotty.
    _log(f"send_ack_email waere gesendet an Lead {lead['lead_id']} "
         f"(Absender={sender!r}, Betreff={subject!r}) [Versand-Hook noch nicht verdrahtet]")
    return {"sender": sender, "subject": subject, "body": body}


def _clean_str(value, max_len):
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]


def _validate_and_build(data):
    """Validiert den Payload und baut den zu speichernden Lead.

    Rueckgabe: (lead_dict, None) bei Erfolg, oder (None, (fehler_dict, status)).
    """
    if not isinstance(data, dict):
        return None, ({"error": "invalid_payload"}, 400)

    # opt_in MUSS true sein (harte DSGVO-Pflicht).
    if data.get("opt_in") is not True:
        return None, ({"error": "opt_in_required",
                       "message": "Einwilligung erforderlich."}, 422)

    errors = {}
    cleaned = {}

    # Pflichtfelder
    for field in REQUIRED_FIELDS:
        val = _clean_str(data.get(field), MAX_LEN.get(field, 500))
        if not val:
            errors[field] = "required"
        cleaned[field] = val

    # E-Mail-Format
    if cleaned.get("email") and not _EMAIL_RE.match(cleaned["email"]):
        errors["email"] = "invalid_email"

    if errors:
        return None, ({"error": "validation_failed", "fields": errors}, 422)

    # Optionale Felder
    for field in OPTIONAL_FIELDS:
        cleaned[field] = _clean_str(data.get(field), MAX_LEN.get(field, 200))

    # Firmengroesse: nur erlaubte Werte, sonst leer
    if cleaned["firmengroesse"] and cleaned["firmengroesse"] not in ALLOWED_FIRMENGROESSE:
        cleaned["firmengroesse"] = ""

    # Sprache: nur de/en, sonst Default de
    sprache = _clean_str(data.get("seite_sprache"), MAX_LEN["seite_sprache"]).lower()
    if sprache not in ALLOWED_SPRACHE:
        sprache = "de"

    # consent_text_version: Server bleibt Herr des Werts
    cversion = _clean_str(data.get("consent_text_version"), MAX_LEN["consent_text_version"])
    if cversion != CONSENT_TEXT_VERSION:
        cversion = CONSENT_TEXT_VERSION

    lead = {
        "lead_id": str(uuid.uuid4()),          # SERVERSEITIG
        "timestamp_utc": _now_utc_iso(),       # SERVERSEITIG (UTC ISO)
        "status": "neu",                       # immer "neu" bei Anlage
        "name": cleaned["name"],
        "firma": cleaned["firma"],
        "email": cleaned["email"],
        "rolle": cleaned["rolle"],
        "firmengroesse": cleaned["firmengroesse"],
        "ziel_mit_ki": cleaned["ziel_mit_ki"],
        "telefon": cleaned["telefon"],
        "opt_in": True,
        "consent_text_version": cversion,
        "seite_sprache": sprache,
        "quelle": "skalar-ai-formular",        # serverseitig fixiert
    }
    # Feld-Reihenfolge exakt nach Schema (Magnet).
    lead = {k: lead[k] for k in LEAD_FIELD_ORDER}
    return lead, None


def _append_lead(lead):
    """Schreibt genau eine JSONL-Zeile append-only, atomic via Lock + fsync."""
    os.makedirs(os.path.dirname(LEAD_FILE), exist_ok=True)
    line = json.dumps(lead, ensure_ascii=False) + "\n"
    # Open im Append-Modus; exklusiver Lock deckt konkurrierende Writer ab.
    fd = os.open(LEAD_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@app.route("/lead-intake", methods=["POST"])
def lead_intake():
    # Bewusst KEIN Zugriff auf request.remote_addr / User-Agent / Referer.
    data = request.get_json(silent=True)

    # Honeypot: verstecktes Feld. Bots fuellen es aus -> stiller Drop.
    # Wir taeuschen Erfolg vor (200), damit der Bot kein Feedback bekommt,
    # persistieren aber NICHTS.
    if isinstance(data, dict) and str(data.get(HONEYPOT_FIELD, "")).strip():
        _log("Honeypot getriggert, stiller Drop")
        return jsonify({"ok": True}), 200

    lead, err = _validate_and_build(data)
    if err is not None:
        body, status = err
        return jsonify(body), status

    try:
        _append_lead(lead)
    except Exception as e:
        _log(f"Persistenz-Fehler: {type(e).__name__}: {str(e)[:120]}")
        return jsonify({"error": "server_error"}), 500

    notify_new_lead(lead)

    if SEND_ACK_EMAIL:
        try:
            send_ack_email(lead)
        except Exception as e:
            _log(f"Ack-Mail-Fehler: {type(e).__name__}: {str(e)[:120]}")

    return jsonify({"ok": True, "lead_id": lead["lead_id"]}), 201


@app.route("/lead-intake/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "send_ack_email": SEND_ACK_EMAIL}), 200


if __name__ == "__main__":
    # Nur fuer lokalen Test. Produktiv betreibt Scotty den Service (WSGI/Tunnel).
    app.run(host="127.0.0.1", port=8090)
