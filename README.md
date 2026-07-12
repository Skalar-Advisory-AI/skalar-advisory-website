# Skalar Advisory Website

Statische Website, gehostet bei Netlify (Projekt "skalar-advisory").

## Struktur

- `index.html` – Startseite Deutsch
- `en/` – englische Version (Startseite, Terminseite)
- `termin.html` – Terminbuchung (Google Kalender, 60 Min.)
- `impressum.html`, `datenschutz.html` – Rechtstexte
- `portal/` – Kundenportal (wird auf skalar-advisory.pro ausgeliefert)
- `_redirects` – Domain-Routing (.com zu /en/, .pro zum Portal, Kunden-Kurzlinks)
- `netlify.toml` – Netlify-Konfiguration (kein Build-Schritt)

## Domains

- skalar-advisory.de – Hauptdomain (DE)
- skalar-advisory.com – Weiterleitung auf /en/
- skalar-advisory.pro – Kundenportal

## Aenderungen veroeffentlichen

Nach Verknuepfung mit Netlify deployt jeder Commit auf `main` automatisch.
Texte lassen sich direkt im GitHub-Webeditor anpassen (Datei oeffnen, Stift-Symbol, Commit).

## Kunden-Kurzlink anlegen

In `_redirects` eine Zeile ergaenzen:

    https://skalar-advisory.pro/kunde/KUERZEL DRIVE-ORDNER-URL 302!

Offen: USt-IdNr. im Impressum nachtragen, sobald vergeben.
