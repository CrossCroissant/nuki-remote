# Nuki Remote

Eigenständige Webseite, um die Nuki-Haustür von unterwegs zu sehen und geschützt zu öffnen,
ohne Home-Assistant-Login. Der Browser spricht nur mit diesem Add-on. Das HA-Zugriffstoken
verlässt das Add-on nie.

## Voraussetzungen in Home Assistant

Diese Helfer und Skripte müssen existieren. Sie sind bereits angelegt.

| Entität | Zweck |
|---|---|
| `input_boolean.nuki_remote_erlaubt` | Zentrale Freigabe. AUS = keine Fernöffnung möglich |
| `input_datetime.nuki_pin_sitzung_bis` | Ende der aktuellen PIN-Sitzung (zentral für alle Geräte) |
| `input_datetime.nuki_pin_sperre_bis` | Ende der Sperre nach zu vielen Fehlversuchen |
| `counter.nuki_pin_fehlversuche` | Zähler für falsche PINs |
| `script.nuki_fern_oeffnen` | Öffnen / Lock 'n' Go, prüft Freigabe + Sitzung selbst |
| `script.nuki_fern_sitzung_starten` / `_beenden` | PIN-Sitzung steuern |
| `script.nuki_fern_pin_fehlversuch` | Fehlversuch zählen, ggf. sperren und Push senden |

## Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| `pin` | `1337` | PIN für die Webseite (wird nur im Add-on geprüft) |
| `sitzung_minuten` | `15` | Dauer der Freigabe nach korrekter PIN |
| `max_fehlversuche` | `5` | Falsche PINs bis zur Sperre |
| `sperre_minuten` | `15` | Dauer der Sperre |
| `ssl` | `true` | HTTPS mit dem Zertifikat aus `/ssl` (z. B. vom DuckDNS-Add-on) |
| `certfile` / `keyfile` | `fullchain.pem` / `privkey.pem` | Dateinamen in `/ssl` |

## Erreichbarkeit von außen

Das Add-on lauscht auf Port **8443** (im Reiter „Netzwerk“ änderbar). Am Router eine
Portfreigabe **TCP 8443 → IP des Home-Assistant-Geräts : 8443** anlegen. Danach ist die Seite
unter `https://<deine-domain>.duckdns.org:8443` erreichbar.

## Ablauf

1. Freigabe `input_boolean.nuki_remote_erlaubt` in HA einschalten.
2. Auf der Webseite PIN eingeben. Die Tür ist dann 15 Minuten lang **für alle Geräte** freigegeben.
3. „Tür öffnen“ oder „Lock & Go“ gedrückt halten. Die Seite zeigt, ob HA den Befehl empfangen hat
   und ob Nuki reagiert hat.
4. Die Freigabe endet automatisch, über „Freigabe jetzt beenden“ oder beim Ausschalten des Schalters.

Jeder Versuch steht im HA-Logbuch unter „Nuki Fernsteuerung“ und ist auf der Seite im Verlauf zu sehen.
