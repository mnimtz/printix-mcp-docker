# Changelog

Dieses Projekt folgt [Semantic Versioning](https://semver.org/lang/de/).

## 7.0.0 — 2026-04-24

Erstes Release als Standalone-Docker-Image (`ghcr.io/mnimtz/printix-mcp-docker`).
Bis v6.7.118 lief der MCP-Server ausschließlich als Home-Assistant-Addon.

### Breaking Changes

- **Single-Tenant-Modell.** Bisher hat jeder eingeladene User automatisch einen
  eigenen, isolierten Tenant bekommen — das hat bei selbst gehosteten
  Deployments für eine Organisation nie Sinn ergeben. Ab v7.0.0 gibt es pro
  Installation genau **einen** Tenant; alle User teilen ihn sich. Migration
  läuft automatisch beim ersten Start (siehe unten).
- **2-Rollen-Modell.** Rollen reduziert auf `admin` und `employee`.
  Die Legacy-Rolle `user` wird beim Startup auf `employee` migriert.
- **HA-Addon-Pfad entfernt.** Kein `run.sh`, keine `config.yaml`-Ports, keine
  Ingress-Integration. Wer noch auf dem HA-Addon sitzt, bleibt beim
  v6.7.x-Zweig.
- **Config-Bereinigung.** Die `HOST_*_PORT`-Env-Variablen sind weg —
  Port-Mapping erfolgt ausschließlich in `docker-compose.yml`.
  `CAPTURE_PUBLIC_URL` entfällt ebenfalls; Capture leitet die URL aus
  `capture_public_url` (DB) oder der Haupt-URL ab.

### Neue Features

- **Mehrere gleichberechtigte Admins pro Tenant** — jeder Admin kann User
  verwalten, Credentials rotieren, Printix-Integration konfigurieren.
- **CSV-Bulk-Import** unter `/admin/users/bulk-import`. Pflichtfeld: `email`.
  Optional: `full_name`, `username`, `company`, `local_role`, `printix_role`.
  Checkbox-Optionen: Einladungs-Mail mit Temp-Passwort versenden und/oder
  den User in Printix anlegen (USER oder GUEST_USER).
- **Safeguard für letzten Admin.** Weder Löschen noch Herunterstufen noch
  Deaktivieren des letzten verbliebenen Admins ist möglich — neue
  `LastAdminError`-Exception wird als UI-Banner surface gezeigt
  (`/admin/users?err=...`).
- **Tenant-Owner-Schutz.** Der erste Admin (Tenant-Owner) kann nicht ohne
  expliziten Transfer gelöscht werden.

### Verbesserungen

- **Vereinheitlichte Port-Config.** `docker-compose.yml` ist die einzige
  Stelle für Host-Port-Mappings; `.env` enthält nur noch Runtime-Settings.
- **Vereinfachte URL-Auflösung (2-stufig).** `public_url` (DB, Admin-UI)
  überschreibt `MCP_PUBLIC_URL` (Env). Fallback auf Request-Host für LAN-
  Betrieb.
- **Admin-Settings-Seite** zeigt die effektive Public URL + Quelle
  ("DB-Setting" / "Env" / "Request-Host") — kein rätselraten mehr.
- **Capture-URL-Auflösung** von 5 auf 3 Stufen reduziert
  (DB-Override ➜ Haupt-URL ➜ Request).

### Migration (automatisch beim ersten Start)

1. `role_type='user'` → `role_type='employee'`
2. `parent_user_id` wird auf den ältesten Admin (Tenant-Owner) gesetzt,
   falls leer
3. Leere/verwaiste Tenant-Datensätze (die durch das alte
   „pro-User-Tenant"-Modell entstanden sind) werden entfernt

Bestehende Daten bleiben erhalten. Ein Backup vor dem Upgrade ist trotzdem
empfohlen (`/admin/settings` → Backup).

### Intern

- Deprecations entfernt: `_create_empty_tenant()`-Aufrufe aus
  `create_user_admin`, `create_invited_user`, `get_or_create_entra_user`
- `get_parent_user_id` löst jetzt für **alle** User den Tenant-Owner auf,
  nicht mehr nur für Employees
- `<HA-IP>`-Hardcoded-Fallbacks entfernt aus `app.py`, `server.py`,
  `capture_server.py`, `employee_routes.py`
