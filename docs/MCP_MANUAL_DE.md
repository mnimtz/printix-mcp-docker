# Printix MCP — Handbuch für Anwender

> **Version:** 6.8.8 · **Tool-Inventar:** 127 Tools · **Stand:** April 2026
> **Zielgruppe:** Administratoren, Helpdesk und Power-User, die den Printix MCP Server über einen AI-Assistenten (claude.ai, ChatGPT, Claude Desktop, Cursor o. ä.) ansprechen.
> **Sprache:** Deutsch · Englische Version siehe `MCP_MANUAL_EN.pdf`

---

## ⚠️ Wichtig: AI-Tool-Liste regelmäßig aktualisieren

Wenn der MCP-Server eine neue Version bekommt, kommen oft **neue Tools** dazu. Damit dein AI-Assistent die auch wirklich nutzt, muss er die Tool-Liste neu laden. **Server-Update allein reicht nicht** — der Client cached die Tool-Definitionen.

| Client | So aktualisierst du die Tool-Liste |
|--------|-----------------------------------|
| **claude.ai (Web)** | *Settings → Connectors → Printix MCP → trennen → erneut verbinden*. Oder einfach eine **neue Conversation** starten — beim ersten Nachrichtenaufruf wird die Tool-Liste neu gepullt. |
| **ChatGPT (Custom Connector)** | Im *Custom GPT Editor* den MCP-Server einmal auf *„Disconnect"* klicken, dann *„Connect"*. Tab schließen + neu öffnen reicht meist auch. |
| **Claude Desktop** | App **kompletter Restart** (`Cmd+Q` + neu starten — *nicht* nur Fenster schließen). Tools werden beim Start neu eingelesen. |
| **Cursor / Continue / andere** | Connector-Toggle aus + an, oder den jeweiligen Befehl `/mcp reload` (je nach Client). |

**Schnelltest**, ob ein neues Tool da ist: frage den Assistenten *„Welche Tools hast du für Printix?"*. Erscheinen z. B. `printix_print_self` oder `printix_welcome_user` in der Liste, läufst du auf einer aktuellen Version. Falls nicht: oben den Refresh ausführen.

---

## Was ist Printix MCP?

Der Printix MCP Server ist die Brücke zwischen modernen AI-Assistenten und der Printix Cloud Print API. Er stellt **127 Tools** bereit, mit denen du Printix in natürlicher Sprache steuern kannst — vom einfachen *„Welche Drucker haben wir in Düsseldorf?"* bis zu komplexen Workflows wie *„Sende mir diesen Wochenbericht als PDF an meinen Drucker, archiviere parallel eine Kopie in Paperless mit Tags ‚Q1-Bericht'."*

Du musst die Tool-Namen **nicht auswendig lernen**. Der Assistent wählt das passende Werkzeug anhand deiner Frage. Dieses Handbuch zeigt dir, *was* möglich ist, damit du gezieltere Fragen stellen kannst.

---

## Wie man dieses Handbuch liest

Jede Kategorie enthält eine kurze Einleitung, eine Tool-Tabelle mit Zweck-Beschreibung und mehrere **Beispiel-Dialoge** mit konkretem Prompt und Tool-Aufruf. Du kannst die Prompts 1:1 verwenden oder als Inspiration nutzen.

🆕 **Neu** markiert Tools, die mit v6.8.0–v6.8.8 (April 2026) hinzugekommen sind.

---

## Inhaltsverzeichnis

1. [System & Selbstdiagnose](#1-system--selbstdiagnose)
2. [Drucker, Sites & Netzwerke](#2-drucker-sites--netzwerke)
3. [Druckjobs & Cloud-Print](#3-druckjobs--cloud-print)
4. [Benutzer, Gruppen & Workstations](#4-benutzer-gruppen--workstations)
5. [Karten & Kartenprofile](#5-karten--kartenprofile)
6. [Reports & Analysen](#6-reports--analysen)
7. [Report-Templates & Scheduling](#7-report-templates--scheduling)
8. [Capture & Document-Workflow](#8-capture--document-workflow)
9. [Onboarding, Time-Bombs & Entra-Sync 🆕](#9-onboarding-time-bombs--entra-sync-)
10. [Betrieb, Wartung & Audit](#10-betrieb-wartung--audit)
11. [Tipps für produktive AI-Dialoge](#11-tipps-für-produktive-ai-dialoge)

---

## 1. System & Selbstdiagnose

Meta-Fragen: *Wer bin ich? Läuft alles? Welche Rolle habe ich? Was soll ich als Nächstes tun?* Ideal als Einstieg in eine neue Session — oder wenn etwas nicht funktioniert und du wissen willst **warum**.

| Tool | Zweck |
|------|-------|
| `printix_status` | Health-Check: läuft der Server, ist der Tenant erreichbar, welche Credential-Bereiche sind konfiguriert. |
| `printix_whoami` | Aktueller Tenant + eigener Printix-User + Admin-Status. |
| `printix_tenant_summary` | Kompakter Überblick: Anzahl Drucker, User, Sites, Cards, offene Jobs. |
| `printix_explain_error` | Übersetzt Printix-Fehlercode oder Error-Message in Klartext + Lösungsvorschlag. |
| `printix_suggest_next_action` | Schlägt einen sinnvollen nächsten Schritt anhand eines Kontext-Strings vor. |
| `printix_natural_query` | Nimmt natürlich-sprachige Frage entgegen, schlägt das passende Reports-Tool vor. |

### Beispiel-Dialoge

**Prompt:** *„Läuft alles bei Printix?"*
→ `printix_status` meldet API-Verbindung, Tenant-ID und konfigurierte Credential-Bereiche.

**Prompt:** *„Wer bin ich gerade bei Printix?"*
→ `printix_whoami` liefert Tenant-Name, eigene E-Mail und Admin-Status.

**Prompt:** *„Gib mir einen Überblick über meinen Tenant."*
→ `printix_tenant_summary` zeigt alle Kennzahlen in einem Block.

**Prompt:** *„Was bedeutet der Fehler 'AADSTS700025'?"*
→ `printix_explain_error("AADSTS700025")` erklärt den Code (Public Client / kein client_secret bei PKCE) und nennt typische Lösungen.

**Prompt:** *„Was sollte ich als Nächstes tun, ich habe gerade einen neuen Drucker installiert?"*
→ `printix_suggest_next_action("neuer Drucker installiert")` schlägt z. B. SNMP-Konfig prüfen, Test-Druck machen, Health-Report ziehen.

---

## 2. Drucker, Sites & Netzwerke

Physische und logische Infrastruktur: Drucker, Queues, Standorte (Sites), Netzwerke, SNMP-Konfigurationen. Lesende und schreibende Operationen. Die `*_context`-Tools liefern aggregierte Sichten (z. B. Queue + Printer + letzte Jobs in einem Aufruf).

| Tool | Zweck |
|------|-------|
| `printix_list_printers` | Listet alle Drucker (mit optionalem Suchbegriff). |
| `printix_get_printer` | Details + Fähigkeiten eines konkreten Druckers. |
| `printix_resolve_printer` | Fuzzy-Match (Name + Location + Modell + Site). |
| `printix_network_printers` | Alle Drucker eines Netzwerks oder einer Site. |
| `printix_get_queue_context` | Queue + Printer-Objekt + letzte Jobs in einem Aufruf. |
| `printix_printer_health_report` | Drucker-Status: online, offline, Fehlerzustände. |
| `printix_top_printers` | Top-N Drucker nach Druckvolumen. |
| `printix_list_sites` / `printix_get_site` | Site-Listing / -Details. |
| `printix_create_site` / `printix_update_site` / `printix_delete_site` | Site-Verwaltung. |
| `printix_site_summary` | Site + Networks + Drucker in einem aggregierten Block. |
| `printix_list_networks` / `printix_get_network` | Netzwerk-Listing / -Details. |
| `printix_create_network` / `printix_update_network` / `printix_delete_network` | Netzwerk-Verwaltung. |
| `printix_get_network_context` | Network + Site + Drucker in einem Block. |
| `printix_list_snmp_configs` / `printix_get_snmp_config` | SNMP-Konfigurationen. |
| `printix_create_snmp_config` / `printix_delete_snmp_config` | SNMP-Config anlegen/entfernen. |
| `printix_get_snmp_context` | SNMP-Config + betroffene Drucker + Netzwerk in einem Block. |

### Beispiel-Dialoge

**Prompt:** *„Welche Drucker stehen in Düsseldorf und sind von Brother?"*
→ `printix_resolve_printer("Brother Düsseldorf")` liefert Token-Fuzzy-Match aller Geräte mit beiden Tokens in Name/Location/Vendor/Site.

**Prompt:** *„Zeig mir alle Drucker im Netzwerk 9cfa4bf0."*
→ `printix_network_printers(network_id="9cfa4bf0")` löst die Site auf (falls keine direkte Network→Printer-Zuordnung existiert) und liefert die zugehörigen Drucker.

**Prompt:** *„Mach eine komplette Zusammenfassung der Site DACH."*
→ `printix_site_summary(site_id=…)` — Site-Meta + Networks + alle Drucker.

**Prompt:** *„Welche Drucker sind gerade offline?"*
→ `printix_printer_health_report` gruppiert nach Status, Problem-Geräte oben.

**Prompt:** *„Top 5 Drucker der letzten 7 Tage nach Seitenzahl?"*
→ `printix_top_printers(days=7, limit=5, metric="pages")`.

**Prompt:** *„Lege einen neuen Standort 'Hamburg' an mit Adresse Mönckebergstraße 7."*
→ `printix_create_site(name="Hamburg", address="Mönckebergstraße 7", ...)`.

---

## 3. Druckjobs & Cloud-Print

Druckjobs einsehen, einreichen und delegieren. **🆕 v6.8.x**: drei neue High-Level-Tools (`print_self`, `print_to_recipients`, `session_print`) für KI-Workflows mit nativer PDF/PCL-Konvertierung.

> 🆕 **Auto-PDL-Conversion (v6.8.8+)**: Alle Print-Tools konvertieren PDF/PostScript/Text automatisch zu PCL XL via Ghostscript, bevor sie zur Drucker-Queue geschickt werden. Drucker ohne eingebauten PDF-RIP würden sonst Hieroglyphen drucken. Default-Parameter `pdl="auto"` (= PCL XL Color). Mit `pdl="passthrough"` schickst du die Datei unverändert.

| Tool | Zweck |
|------|-------|
| `printix_list_jobs` | Alle Jobs, optional nach Queue gefiltert. |
| `printix_get_job` | Details zu einem Job. |
| `printix_submit_job` | Druckjob low-level einreichen (Schritt 1 des 5-stage-Submits). |
| `printix_complete_upload` | Upload abschließen. |
| `printix_delete_job` | Job stornieren. |
| `printix_change_job_owner` | Job an anderen User delegieren. |
| `printix_jobs_stuck` | Jobs, die länger als N Minuten hängen. |
| `printix_quick_print` | Single-shot: URL + Empfänger → fertig. |
| `printix_send_to_user` | Dokument an User X (URL oder Base64). v6.8.8+: mit Auto-Conversion. |
| 🆕 `printix_print_self` | Druckt Datei in **eigene** Secure-Print-Queue (KI generiert PDF inline). |
| 🆕 `printix_print_to_recipients` | Multi-Recipient: ein PDF an mehrere Empfänger gleichzeitig (auch via `group:Name` oder `entra:OID`). |
| 🆕 `printix_resolve_recipients` | Diagnose-Tool: zeigt vor `print_to_recipients` welche Empfänger aufgelöst werden. |
| 🆕 `printix_session_print` | Druckjob mit Time-Bomb (auto-expire nach N Stunden). |

### Beispiel-Dialoge

**Prompt:** *„Schick mir dieses PDF als Secure Print an marcus@firma.de."*
→ `printix_send_to_user(user_email="marcus@firma.de", file_content_b64=..., filename="vertrag.pdf")` — der Server konvertiert PDF→PCL XL und legt den Job in die Queue.

**Prompt:** *„Welche Druckjobs hängen seit mehr als 30 Minuten?"*
→ `printix_jobs_stuck(minutes=30)`.

**Prompt:** *„Gib den Job 4711 an marcus@firma.de ab, weil ich in den Urlaub fahre."*
→ `printix_change_job_owner(job_id="4711", new_owner_email="marcus@firma.de")`.

🆕 **Prompt:** *„Erstelle eine A4-Druckvorlage mit den Quartalszahlen und sende sie zur Abholung an meinen Drucker."*
→ AI generiert PDF inline → `printix_print_self(file_b64=..., filename="Q1.pdf")` legt Job in deine eigene Queue. Du holst ihn am Drucker mit Karte/Code ab.

🆕 **Prompt:** *„Sende dieses Memo an alle in der Marketing-Gruppe als Secure Print."*
→ `printix_print_to_recipients(recipients_csv="group:Marketing", file_b64=..., filename="memo.pdf")` — ein Job pro Mitglied.

🆕 **Prompt:** *„Bevor ich verschicke: an wen genau geht das?"*
→ `printix_resolve_recipients("group:Marketing, alice@firma.de, entra:abc-uuid")` zeigt die exakte Empfängerliste, ohne zu drucken. Mehrdeutigkeiten + nicht-gefundene Eingaben werden separat aufgelistet.

🆕 **Prompt:** *„Schick diesen Vertragsentwurf an externer-gast@partner.de — soll aber nach 4 Stunden auto-expiren."*
→ `printix_session_print(user_email="externer-gast@partner.de", file_b64=..., filename="vertrag.pdf", expires_in_hours=4)` — Job läuft, plus Time-Bomb für automatische Bereinigung.

🆕 **Prompt:** *„Druck nur monochrom — Farbe brauchen wir hier nicht."*
→ Bei jedem Print-Tool: Parameter `color=False` setzen (Default `True`).

🆕 **Prompt:** *„Mein Drucker macht den PDF-RIP selbst — schick die Datei einfach so durch."*
→ Parameter `pdl="passthrough"` deaktiviert die Server-seitige Konvertierung.

---

## 4. Benutzer, Gruppen & Workstations

Kompletter Lifecycle: anlegen, bearbeiten, deaktivieren, diagnostizieren. Die `user_360`- und `diagnose_user`-Tools sind Helpdesk-Allrounder. **🆕 v6.8.x**: detaillierte Group-Membership + Print-Pattern-Analyse + Quota-Guard.

| Tool | Zweck |
|------|-------|
| `printix_list_users` | Alle User, mit Pagination + Rollen-Filter. |
| `printix_get_user` | User-Details. |
| `printix_find_user` | Sucht nach E-Mail-Fragment oder Name. |
| `printix_user_360` | 360°-Sicht: User + Karten + Gruppen + Workstations + letzte Jobs. |
| `printix_diagnose_user` | Helpdesk-Diagnose: was funktioniert, was nicht, warum. |
| `printix_create_user` / `printix_delete_user` | User-Verwaltung. |
| `printix_generate_id_code` | Neuer ID-Code für einen User. |
| `printix_onboard_user` / `printix_offboard_user` | Geführtes On-/Offboarding (mehrere Schritte in einem Aufruf). |
| `printix_list_admins` | Alle Admins. |
| `printix_permission_matrix` | Matrix: User × Berechtigungen. |
| `printix_inactive_users` | User, die seit N Tagen nicht mehr gedruckt haben. |
| `printix_sso_status` | Prüft SSO-Mapping. |
| `printix_list_groups` / `printix_get_group` | Gruppen-Listing / -Details. |
| `printix_create_group` / `printix_delete_group` | Gruppen-Verwaltung. |
| 🆕 `printix_get_group_members` | Mitglieder einer Gruppe per UUID oder Name (ambiguity-safe). |
| 🆕 `printix_get_user_groups` | Reverse-Lookup: in welchen Gruppen ist User X? |
| 🆕 `printix_describe_user_print_pattern` | Druck-Profil eines Users: bevorzugte Drucker, Farb-Quote, Seitenzahl. |
| 🆕 `printix_quota_guard` | Pre-flight-Burst-Check vor Submit (verdict allow/throttle/block). |
| 🆕 `printix_print_history_natural` | Druckhistorie mit natürlich-sprachigen Zeitangaben („heute", „letzte Woche", „Q1", „7d"). |
| `printix_list_workstations` / `printix_get_workstation` | Workstations-Listing / -Details. |

### Beispiel-Dialoge

**Prompt:** *„Gib mir alles, was du über marcus@firma.de weißt."*
→ `printix_user_360(query="marcus@firma.de")` liefert die komplette 360°-Sicht.

**Prompt:** *„Warum kann Anna nicht mehr drucken?"*
→ `printix_diagnose_user(email="anna@firma.de")` prüft Status, SSO, Karten, Gruppen, aktive Blockaden — und schlägt Lösungen vor.

**Prompt:** *„Welche User sind seit 180 Tagen inaktiv?"*
→ `printix_inactive_users(days=180)` — Kandidatenliste fürs Offboarding.

**Prompt:** *„Leg einen neuen Mitarbeiter an: peter@firma.de, Peter Meier, Gruppe 'Finance'."*
→ `printix_onboard_user(...)` führt alle Schritte in der richtigen Reihenfolge aus.

🆕 **Prompt:** *„Wer ist alles in der Marketing-Gruppe drin?"*
→ `printix_get_group_members("Marketing")` — Bei mehreren gleichnamigen Gruppen gibt's eine Kandidatenliste mit UUIDs.

🆕 **Prompt:** *„In welchen Gruppen ist Anna?"*
→ `printix_get_user_groups("anna@firma.de")` — sucht zuerst im User-Object, fällt auf Gruppen-Scan zurück.

🆕 **Prompt:** *„Was hat Marcus heute gedruckt?"*
→ `printix_print_history_natural(user_email="marcus@firma.de", when="today")`.

🆕 **Prompt:** *„Druckmuster von Marcus über die letzten 30 Tage?"*
→ `printix_describe_user_print_pattern(user_email="marcus@firma.de", days=30)` — zeigt Top-Drucker, Farb-Quote, Ø-Seitenzahl.

🆕 **Prompt:** *„Bevor ich noch ein PDF schicke: hat der User in den letzten 5 Min nicht zu viele Jobs gesendet?"*
→ `printix_quota_guard(user_email="marcus@firma.de", window_minutes=5, max_jobs=10)` — verdict `allow`/`throttle`/`block`.

---

## 5. Karten & Kartenprofile

Alles rund um RFID/Mifare/HID-Karten: Registrierung, Mapping, Profile, Bulk-Import. **🆕 v6.8.x**: AI-Wrapper `card_enrol_assist` der UID + Profile-Transform + Register in einem Tool macht.

| Tool | Zweck |
|------|-------|
| `printix_list_cards` | Karten eines Users. |
| `printix_list_cards_by_tenant` | Alle Karten des Tenants (Filter: `all`/`registered`/`orphaned`). |
| `printix_search_card` | Karte per ID/Nummer suchen. |
| `printix_register_card` | Karte einem User zuordnen (low-level). |
| `printix_delete_card` | Karten-Zuordnung entfernen. |
| `printix_get_card_details` | Karte + lokales Mapping + Owner in einem Block. |
| `printix_decode_card_value` | Raw-Kartenwert dekodieren (Base64/Hex/YSoft/Konica-Varianten). |
| `printix_transform_card_value` | Wert durch Transformations-Pipeline schicken (Hex↔Dezimal, Reverse, Prefix/Suffix …). |
| `printix_get_user_card_context` | User + alle Karten + Profile in einem Block. |
| `printix_list_card_profiles` / `printix_get_card_profile` | Profile-Listing/Details. |
| `printix_search_card_mappings` | Lokale Mapping-DB durchsuchen. |
| `printix_bulk_import_cards` | CSV-Massenimport (mit Profil + Dry-Run). |
| `printix_suggest_profile` | Schlägt anhand einer Beispiel-UID das passende Profil vor (Top-10). |
| `printix_card_audit` | Audit-Trail aller Karten-Änderungen für einen User. |
| `printix_find_orphaned_mappings` | Lokale Mappings ohne zugehörigen Printix-User. |
| 🆕 `printix_card_enrol_assist` | AI-Onboarding: UID + Profile-Transform + Register in einem Aufruf. |

### Beispiel-Dialoge

**Prompt:** *„Welche Karten hat Marcus?"*
→ `printix_get_user_card_context(email="marcus@firma.de")` liefert User + alle Karten + verwendete Profile.

**Prompt:** *„Was ist die Karte mit der UID `04:5F:F0:02:AB:3C`?"*
→ `printix_decode_card_value(card_value="04:5F:F0:02:AB:3C")` erkennt Hex-UID mit Trennzeichen und liefert `decoded_bytes_hex` + Profil-Hint.

**Prompt:** *„Importier mir 500 Karten aus dieser CSV — erst mal als Dry-Run."*
→ `printix_bulk_import_cards(csv=..., profile=..., dry_run=True)` validiert jede Zeile und zeigt Vorschau-Werte ohne zu schreiben.

**Prompt:** *„Für UID `045FF002` — welches Profil passt?"*
→ `printix_suggest_profile(sample_uid="045FF002")` Top-10 mit Score + `best_match`.

🆕 **Prompt:** *„Marcus hat seine Firmenkarte ans iPhone getappt. UID ist `04A1B2C3D4E5F6`. Bitte für ihn registrieren."*
→ `printix_card_enrol_assist(user_email="marcus@firma.de", card_uid_raw="04A1B2C3D4E5F6")` — automatisch durchs Default-Card-Profile transformiert + via `register_card` zugeordnet.

---

## 6. Reports & Analysen

Reports laufen gegen das separate SQL Server-Warehouse. Du bekommst Kennzahlen, Trends, Anomalien und Ad-hoc-Queries über ein einheitliches Interface. `query_any` ist der Universal-Einstieg, die spezialisierten Tools sind schnellere Abkürzungen für gängige Fragen.

| Tool | Zweck |
|------|-------|
| `printix_reporting_status` | Status der Reports-Engine (DB-Verbindung, letzte Nightlies, Preset-Count). |
| `printix_query_any` | Universal: Preset + Filter → Tabelle. |
| `printix_query_print_stats` | Druckvolumen nach Dimension. |
| `printix_query_cost_report` | Druckkosten, optional nach Abteilung/User. |
| `printix_query_top_users` / `printix_query_top_printers` | Top-N mit Zeitfenster. |
| `printix_query_anomalies` | Anomalie-Erkennung (Ausreißer). |
| `printix_query_trend` | Trendlinien über Zeit. |
| `printix_query_audit_log` | Strukturierter Audit-Trail des MCP-Servers (Aktionen, Objekte, Actor). |
| `printix_top_printers` / `printix_top_users` | Kurzform-Wrapper. |
| `printix_print_trends` | Trend nach Tag/Woche/Monat. |
| `printix_cost_by_department` | Kosten aggregiert pro Abteilung. |
| `printix_compare_periods` | Periode A gegen Periode B stellen. |

### Beispiel-Dialoge

**Prompt:** *„Wer hat letzten Monat am meisten gedruckt?"*
→ `printix_top_users(days=30, limit=10, metric="pages")`.

**Prompt:** *„Wie sieht der Druck-Trend der letzten 90 Tage aus, monatlich?"*
→ `printix_print_trends(group_by="month", days=90)`.

**Prompt:** *„Vergleich die letzten 30 Tage mit den 30 Tagen davor — was hat sich geändert?"*
→ `printix_compare_periods(days_a=30, days_b=30)` liefert Delta-Kennzahlen.

**Prompt:** *„Welche Abteilung verursacht die höchsten Druckkosten?"*
→ `printix_cost_by_department(days=30)`.

**Prompt:** *„Welche Aktionen hat User X am 15. April im MCP ausgeführt?"*
→ `printix_query_audit_log(start_date="2026-04-15", end_date="2026-04-15", actor_email="x@firma.de")`.

**Prompt:** *„Gibt es Ausreißer im Druckverhalten der letzten 14 Tage?"*
→ `printix_query_anomalies(days=14)` — z. B. plötzliche Volumen-Spikes oder ungewöhnliche Drucker-Nutzungsmuster.

---

## 7. Report-Templates & Scheduling

Wenn du eine Analyse regelmäßig brauchst: speichern als Template, als wiederkehrenden Versand einplanen, per E-Mail zustellen lassen. Design-Optionen werden via `list_design_options` abgefragt; `preview_report` rendert eine Vorschau ohne Versand.

| Tool | Zweck |
|------|-------|
| `printix_save_report_template` | Query + Design als Template speichern. |
| `printix_list_report_templates` | Alle gespeicherten Templates. |
| `printix_get_report_template` | Template-Details. |
| `printix_delete_report_template` | Template löschen. |
| `printix_run_report_now` | Template einmalig ausführen. |
| `printix_send_test_email` | Test-Mail (SMTP-Check). |
| `printix_schedule_report` | Template als Cron-Job einplanen. |
| `printix_list_schedules` | Alle aktiven Schedules. |
| `printix_update_schedule` / `printix_delete_schedule` | Schedule ändern/entfernen. |
| `printix_list_design_options` | Verfügbare Farbschemata, Logos, Layouts. |
| `printix_preview_report` | Vorschau-PDF eines Reports ohne Versand. |

### Beispiel-Dialoge

**Prompt:** *„Speichere den aktuellen Top-10-User-Report als Template 'Monatlicher Druck-Top10'."*
→ `printix_save_report_template(...)`.

**Prompt:** *„Schicke dieses Template jeden ersten Werktag des Monats an management@firma.de."*
→ `printix_schedule_report(report_id=…, cron="0 8 1 * *", recipients=["management@firma.de"])`.

**Prompt:** *„Zeig mir die Vorschau von Template XY als PDF."*
→ `printix_preview_report(report_id=…)`.

**Prompt:** *„Welche Farbschemata kann ich für Reports verwenden?"*
→ `printix_list_design_options()`.

---

## 8. Capture & Document-Workflow

Capture verknüpft eingescannte oder KI-generierte Dokumente mit Ziel-Systemen (Paperless-ngx, SharePoint, DMS …) über Plugins. **🆕 v6.8.x**: zwei neue Tools, mit denen das KI-Modell Dateien direkt in einen Capture-Workflow einspeisen kann — nicht nur über den klassischen Webhook-Pfad.

| Tool | Zweck |
|------|-------|
| `printix_list_capture_profiles` | Alle Capture-Profile des Tenants. |
| `printix_capture_status` | Status: Server-Port, Webhook-Base-URL, verfügbare Plugins, konfigurierte Profile. |
| 🆕 `printix_describe_capture_profile` | Plugin-Schema eines Profils: welche metadata-Felder erwartet/erlaubt sind, plus aktuelle Konfig (Secrets maskiert). |
| 🆕 `printix_send_to_capture` | Datei direkt in Capture-Workflow einspeisen — gleicher Code-Pfad wie ein Webhook, aber ohne Drucker-/Blob-Umweg. |

### Beispiel-Dialoge

**Prompt:** *„Ist Capture aktiv und welche Plugins habe ich?"*
→ `printix_capture_status` — Plugin-Liste (z. B. paperless_ngx) + Anzahl konfigurierter Profile.

**Prompt:** *„Welche Capture-Profile habe ich?"*
→ `printix_list_capture_profiles` — Liste mit Ziel-System, Webhook-URL und letzten Ausführungen.

🆕 **Prompt:** *„Was nimmt das Paperless-Profil an Metadaten an?"*
→ `printix_describe_capture_profile("Paperless (Marcus)")` — zeigt Plugin-Schema (`tags`, `correspondent`, `document_type` etc.) plus aktuelle Default-Werte.

🆕 **Prompt:** *„Speicher diesen KI-generierten Vertrag in Paperless mit Tags 'Q1', 'Vertrag' und Correspondent 'Acme Corp'."*
→ `printix_send_to_capture(profile="Paperless (Marcus)", file_b64=..., filename="vertrag_acme.pdf", metadata_json='{"tags":["Q1","Vertrag"], "correspondent":"Acme Corp", "document_type":"Vertrag"}')`.

🆕 **Prompt:** *„Mein Wochenbericht ist fertig — leg ihn parallel ins Paperless und schick mir eine Druck-Version an meinen Drucker."*
→ Zwei Aufrufe: `printix_send_to_capture(...)` + `printix_print_self(...)` — der Assistent macht beides automatisch.

---

## 9. Onboarding, Time-Bombs & Entra-Sync 🆕

Alles in dieser Sektion ist **neu mit v6.8.x**. Es geht um automatisierte User-Lifecycle-Workflows: ein neu angelegter User bekommt ein Welcome-PDF, Erinnerungs-Time-Bombs, und nach 7 Tagen wird automatisch nachgefasst, falls keine Aktion stattgefunden hat. Plus: Entra-AD-Gruppen-Sync mit Printix-Gruppen.

| Tool | Zweck |
|------|-------|
| 🆕 `printix_welcome_user` | Onboarding-Begleiter: Welcome-PDF + Time-Bombs für `card_enrol`, `first_print_reminder`. |
| 🆕 `printix_list_timebombs` | Aktive/vergangene Time-Bombs des Tenants anzeigen. |
| 🆕 `printix_defuse_timebomb` | Time-Bomb manuell deaktivieren (mit Audit-Reason). |
| 🆕 `printix_sync_entra_group_to_printix` | Mitglieder einer Entra-/AD-Gruppe vs. Printix-Gruppe abgleichen (default `report_only`). |

### Beispiel-Dialoge

🆕 **Prompt:** *„Lege einen neuen Mitarbeiter peter@firma.de an und mach ihm den Welcome-Workflow mit Erinnerungen."*
→ `printix_onboard_user(...)` (für die DB-/Account-Anlage) + `printix_welcome_user(user_email="peter@firma.de")` legt Welcome-PDF in seine Secure-Print-Queue UND scharft zwei Time-Bombs: nach 3 Tagen Reminder falls noch nicht gedruckt, nach 7 Tagen Reminder falls noch keine Karte enrolled.

🆕 **Prompt:** *„Welche Onboarding-Erinnerungen sind gerade aktiv?"*
→ `printix_list_timebombs(status="pending")`.

🆕 **Prompt:** *„Peter hat sich gemeldet — er ist im Urlaub bis nächste Woche. Stell die Erinnerungen für ihn ab."*
→ `printix_list_timebombs(user_email="peter@firma.de")` zeigt seine Bomben mit ID, dann `printix_defuse_timebomb(bomb_id=42, reason="im Urlaub bis 2026-05-12")`.

🆕 **Prompt:** *„Sync die Entra-Gruppe `Marketing-DACH` (OID `abc-123`) mit unserer Printix-Gruppe — aber erst nur Report, kein Schreiben."*
→ `printix_sync_entra_group_to_printix(entra_group_oid="abc-123", printix_group_id="def-456", sync_mode="report_only")` zeigt Diff: `to_add` (in Entra, nicht Printix) und `to_remove` (in Printix, nicht Entra).

> ℹ️ **Voraussetzung für `sync_entra_group_to_printix`**: die Entra-App-Registrierung des MCP-Servers braucht die App-Permission `Group.Read.All` (Application, nicht Delegated) + Admin-Consent. Im Azure-Portal nachpflegen.

### Time-Bomb-Konzept

Time-Bombs sind **bedingte, verzögerte Aktionen**. Ein Cron-Job läuft stündlich (`minute=7`) und checkt für jede pending Bombe:

1. Ist die Bedingung noch erfüllt? (z. B. *„User hat noch keine Karte enrolled"*)
2. Wenn ja → Action ausführen (Reminder-PDF in die Queue senden, Log-Eintrag, etc.)
3. Wenn nein → Bombe automatisch als `defused` markiert.

So bleibt z. B. die `first_print_reminder_3d`-Bombe nach 3 Tagen pending — wenn der User in der Zwischenzeit echt gedruckt hat, defuses sie sich automatisch. Erst wenn der User wirklich noch nichts getan hat, fällt der Reminder.

---

## 10. Betrieb, Wartung & Audit

Backups, Demo-Daten, Feature-Tracking. Mix aus Operations und Meta.

| Tool | Zweck |
|------|-------|
| `printix_list_backups` | Alle vorhandenen Backups. |
| `printix_create_backup` | Neues Backup erzeugen (DB + Konfig + Metadaten). |
| `printix_demo_setup_schema` | Demo-Schema in der Reports-DB anlegen. |
| `printix_demo_generate` | Synthetische Demo-Daten erzeugen. |
| `printix_demo_rollback` | Demo-Daten entfernen (per Demo-Tag). |
| `printix_demo_status` | Welche Demo-Sets sind aktiv? |
| `printix_list_feature_requests` / `printix_get_feature_request` | Ticketsystem für Feature-Wünsche. |

### Beispiel-Dialoge

**Prompt:** *„Mach ein Backup, bevor ich was ändere."*
→ `printix_create_backup`.

**Prompt:** *„Setze mir eine Demo-Umgebung mit 50 Usern und 500 Jobs auf."*
→ `printix_demo_setup_schema` (einmalig) + `printix_demo_generate(users=50, jobs=500)`.

**Prompt:** *„Zeig mir alle offenen Feature-Requests."*
→ `printix_list_feature_requests(status="open")`.

---

## 11. Tipps für produktive AI-Dialoge

1. **In Zielen denken, nicht in Tools.** *„Wer druckt zu viel?"* ist besser als *„ruf query_top_users mit days=30 auf"*. Der Assistent wählt das richtige Werkzeug.
2. **Kontext mitgeben.** *„Marcus aus der Finance-Abteilung"* ist eindeutiger als *„Marcus"*.
3. **360°-Tools nutzen.** `printix_user_360`, `printix_get_queue_context`, `printix_site_summary` sparen Nachfragen.
4. **Bei Fehlern nachfragen.** *„Warum ist das schiefgegangen?"* triggert `printix_explain_error` oder `printix_diagnose_user`.
5. **Dry-Run vor Bulk-Operationen.** `printix_bulk_import_cards(dry_run=True)`, `printix_resolve_recipients` (vor `print_to_recipients`), `printix_sync_entra_group_to_printix(sync_mode="report_only")`.
6. **🆕 Multi-Step-Workflows in einem Prompt.** Der Assistent kann mehrere Tools in Folge aufrufen: *„Erstelle einen Wochenbericht als PDF, archiviere ihn in Paperless und drucke eine Kopie für mich."*
7. **🆕 Bei `print_*`-Tools: Auto-Conversion vertrauen.** Default `pdl="auto"` macht in 99% der Fälle das Richtige (PCL XL Color). Nur abweichen wenn deine Drucker-Queue serverseitig konvertiert (`passthrough`) oder explizit PostScript braucht.
8. **🆕 Tool-Liste regelmäßig auffrischen** (siehe Hinweis am Anfang). Sonst nutzt der Assistent veraltete Definitionen.

---

*Dokument generiert aus Printix MCP Server v6.8.8 · April 2026 · 127 Tools · [Repository](https://github.com/mnimtz/Printix-MCP)*
