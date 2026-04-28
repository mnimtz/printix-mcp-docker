# Printix MCP — Brukerhåndbok

> **Versjon:** 6.8.10 · **Verktøykatalog:** 127 verktøy · **Per:** April 2026
> **Målgruppe:** Administratorer, helpdesk og superbrukere som bruker Printix MCP-serveren via en AI-assistent (claude.ai, ChatGPT, Claude Desktop, Cursor osv.).
> **Språk:** Norsk (bokmål) · Engelsk versjon: `MCP_MANUAL_EN.pdf` · Tysk: `MCP_MANUAL_DE.pdf`

---

## ⚠️ Viktig: hold AI-assistentens verktøyliste oppdatert

Når MCP-serveren får en ny versjon, kommer det som regel **nye verktøy** med. For at AI-assistenten din faktisk skal bruke dem, må verktøylisten lastes på nytt. **En oppgradering av serveren alene er ikke nok** — klienten cacher verktøydefinisjonene.

| Klient | Slik oppdaterer du verktøylisten |
|--------|----------------------------------|
| **claude.ai (web)** | *Settings → Connectors → Printix MCP → koble fra → koble til på nytt*. Eller bare start en **ny samtale** — verktøylisten hentes på første melding. |
| **ChatGPT (custom connector)** | I *Custom GPT-editor* trykk *Disconnect* på MCP-serveren, deretter *Connect*. Lukk og åpne fanen — det fungerer som regel også. |
| **Claude Desktop** | **Full restart** av appen (`Cmd+Q`, så start på nytt — *ikke* bare lukk vinduet). Verktøy lastes ved oppstart. |
| **Cursor / Continue / andre** | Slå connector av og på, eller bruk klientens `/mcp reload` (varierer per klient). |

**Rask test** for å se om et nytt verktøy har kommet: spør assistenten *«Hvilke Printix-verktøy har du?»*. Ser du f.eks. `printix_print_self` eller `printix_welcome_user` i listen, er du oppdatert. Hvis ikke: gjør oppdateringen over.

---

## Hva er Printix MCP?

Printix MCP-serveren er broen mellom moderne AI-assistenter og Printix Cloud Print API. Den eksponerer **127 verktøy** som lar deg styre Printix på naturlig språk — fra det enkle *«Hvilke skrivere har vi i Oslo?»* til komplekse arbeidsflyter som *«Send meg ukerapporten som PDF til skriveren min, og arkiver en kopi i Paperless med taggen ‚Q1-rapport'.»*

Du **trenger ikke å huske verktøynavnene**. Assistenten velger riktig verktøy basert på spørsmålet ditt. Denne håndboken viser deg *hva som er mulig*, slik at du kan stille fokuserte spørsmål.

---

## Hvordan lese denne håndboken

Hver kategori har en kort introduksjon, en verktøytabell med formålsbeskrivelser og flere **eksempeldialoger** med konkrete prompts og verktøykall. Du kan bruke promptene ordrett eller som inspirasjon.

🆕 markerer verktøy som er lagt til i v6.8.0–v6.8.10 (April 2026).

---

## Innholdsfortegnelse

1. [System & egendiagnose](#1-system--egendiagnose)
2. [Skrivere, sites & nettverk](#2-skrivere-sites--nettverk)
3. [Utskriftsjobber & Cloud Print](#3-utskriftsjobber--cloud-print)
4. [Brukere, grupper & arbeidsstasjoner](#4-brukere-grupper--arbeidsstasjoner)
5. [Kort & kortprofiler](#5-kort--kortprofiler)
6. [Rapporter & analyser](#6-rapporter--analyser)
7. [Rapportmaler & planlegging](#7-rapportmaler--planlegging)
8. [Capture & dokumentarbeidsflyt](#8-capture--dokumentarbeidsflyt)
9. [Onboarding, Time-Bombs & Entra-synk 🆕](#9-onboarding-time-bombs--entra-synk-)
10. [Drift, vedlikehold & revisjon](#10-drift-vedlikehold--revisjon)
11. [Tips for produktive AI-dialoger](#11-tips-for-produktive-ai-dialoger)

---

## 1. System & egendiagnose

Metaspørsmål: *Hvem er jeg? Fungerer alt? Hvilken rolle har jeg? Hva bør jeg gjøre nå?* Perfekt som åpning av en ny økt — eller når noe ikke fungerer og du må vite **hvorfor**.

| Verktøy | Formål |
|---------|--------|
| `printix_status` | Helsesjekk: server oppe, tenant tilgjengelig, hvilke credential-områder er konfigurert. |
| `printix_whoami` | Aktuell tenant + egen Printix-bruker + admin-status. |
| `printix_tenant_summary` | Kompakt oversikt: skrivere, brukere, sites, kort, åpne jobber. |
| `printix_explain_error` | Oversetter en Printix-feilkode eller -melding til klartekst + løsningstips. |
| `printix_suggest_next_action` | Foreslår et fornuftig neste steg ut fra en kontekststreng. |
| `printix_natural_query` | Tar imot et naturlig-språklig spørsmål og foreslår passende rapport-verktøy. |

### Eksempeldialoger

**Prompt:** *«Fungerer Printix?»*
→ `printix_status` rapporterer API-tilkobling, tenant-ID og konfigurerte credential-områder.

**Prompt:** *«Hvem er jeg innlogget som i Printix?»*
→ `printix_whoami` returnerer tenant, e-post og admin-flagg.

**Prompt:** *«Gi meg en oversikt over tenanten min.»*
→ `printix_tenant_summary` returnerer alle nøkkeltall i én blokk.

**Prompt:** *«Hva betyr feilen 'AADSTS700025'?»*
→ `printix_explain_error("AADSTS700025")` forklarer det (public client / ingen client_secret med PKCE) og foreslår løsninger.

**Prompt:** *«Hva bør jeg gjøre nå? Jeg har akkurat installert en ny skriver.»*
→ `printix_suggest_next_action("ny skriver installert")` foreslår SNMP-konfig-sjekk, testutskrift, helsetest.

---

## 2. Skrivere, sites & nettverk

Fysisk og logisk infrastruktur: skrivere, køer, sites, nettverk, SNMP-konfigurasjoner. Lese- og skriveoperasjoner. `*_context`-verktøyene gir aggregerte visninger (kø + skriver + nyeste jobber i ett kall).

| Verktøy | Formål |
|---------|--------|
| `printix_list_printers` | Alle skrivere (valgfritt søk). |
| `printix_get_printer` | Detaljer + funksjoner for en spesifikk skriver. |
| `printix_resolve_printer` | Fuzzy-match (navn + plassering + modell + site). |
| `printix_network_printers` | Alle skrivere i et nettverk eller på en site. |
| `printix_get_queue_context` | Kø + skriverobjekt + nyeste jobber i ett kall. |
| `printix_printer_health_report` | Status gruppert: online / offline / feiltilstander. |
| `printix_top_printers` | Topp-N skrivere etter volum. |
| `printix_list_sites` / `printix_get_site` | Site-liste / -detaljer. |
| `printix_create_site` / `printix_update_site` / `printix_delete_site` | Site-administrasjon. |
| `printix_site_summary` | Site + nettverk + skrivere i én aggregert blokk. |
| `printix_list_networks` / `printix_get_network` | Nettverksliste / -detaljer. |
| `printix_create_network` / `printix_update_network` / `printix_delete_network` | Nettverksadministrasjon. |
| `printix_get_network_context` | Nettverk + site + skrivere i én blokk. |
| `printix_list_snmp_configs` / `printix_get_snmp_config` | SNMP-konfigurasjoner. |
| `printix_create_snmp_config` / `printix_delete_snmp_config` | SNMP-konfig opprett/slett. |
| `printix_get_snmp_context` | SNMP-konfig + berørte skrivere + nettverk. |

### Eksempeldialoger

**Prompt:** *«Hvilke Brother-skrivere står i Oslo?»*
→ `printix_resolve_printer("Brother Oslo")` token-fuzzy match på navn/plassering/leverandør/site.

**Prompt:** *«Vis meg alle skrivere i nettverk 9cfa4bf0.»*
→ `printix_network_printers(network_id="9cfa4bf0")` løser site (når direkte network→printer-mapping ikke finnes) og returnerer relevante skrivere.

**Prompt:** *«Gi meg en komplett oversikt over site DACH.»*
→ `printix_site_summary(site_id=…)` — site-meta + nettverk + alle skrivere.

**Prompt:** *«Hvilke skrivere er offline akkurat nå?»*
→ `printix_printer_health_report` grupperer etter status, problemer øverst.

**Prompt:** *«Topp 5 skrivere etter sideantall forrige uke?»*
→ `printix_top_printers(days=7, limit=5, metric="pages")`.

**Prompt:** *«Opprett en ny site 'Bergen' på Bryggen 5.»*
→ `printix_create_site(name="Bergen", address="Bryggen 5", ...)`.

---

## 3. Utskriftsjobber & Cloud Print

Vis, send og deleger utskriftsjobber. **🆕 v6.8.x**: tre nye høynivå-verktøy (`print_self`, `print_to_recipients`, `session_print`) for AI-arbeidsflyter med innebygd PDF/PCL-konvertering.

> 🆕 **Auto PDL-konvertering (v6.8.8+)**: Alle utskriftsverktøyene konverterer automatisk PDF/PostScript/tekst til PCL XL via Ghostscript før innsending til skriverkøen. Uten dette ville skrivere uten PDF-RIP skrive ut hieroglyfer (rå PDF-kildekode som ASCII). Standard `pdl="auto"` (= PCL XL farge). Bruk `pdl="passthrough"` for å sende filen uendret.

| Verktøy | Formål |
|---------|--------|
| `printix_list_jobs` | Alle jobber, valgfritt køfilter. |
| `printix_get_job` | Jobbdetaljer. |
| `printix_submit_job` | Lavnivå jobb-submit (steg 1 av 5-stegs flow). |
| `printix_complete_upload` | Fullfør opplasting. |
| `printix_delete_job` | Avbryt jobb. |
| `printix_change_job_owner` | Deleger jobb til annen bruker. |
| `printix_jobs_stuck` | Jobber som henger mer enn N minutter. |
| `printix_quick_print` | Kort-form: URL + mottaker → ferdig. |
| `printix_send_to_user` | Send dokument (URL eller base64) til bruker X. v6.8.8+: med auto-konvertering. |
| 🆕 `printix_print_self` | Skriv ut til **egen** secure-print-kø (AI genererer PDF inline). |
| 🆕 `printix_print_to_recipients` | Multi-mottaker: én PDF til mange (også via `group:Navn` eller `entra:OID`). |
| 🆕 `printix_resolve_recipients` | Diagnose: viser hvilke mottakere som ville blitt løst før `print_to_recipients`. |
| 🆕 `printix_session_print` | Jobb med time-bomb (auto-utløper etter N timer). |

### Eksempeldialoger

**Prompt:** *«Send denne PDF-en som Secure Print til marcus@firma.no.»*
→ `printix_send_to_user(user_email="marcus@firma.no", file_content_b64=..., filename="kontrakt.pdf")` — serveren konverterer PDF→PCL XL og legger jobben i køen.

**Prompt:** *«Hvilke jobber har hengt i mer enn 30 minutter?»*
→ `printix_jobs_stuck(minutes=30)`.

**Prompt:** *«Overfør jobb 4711 til marcus@firma.no — jeg drar på ferie.»*
→ `printix_change_job_owner(job_id="4711", new_owner_email="marcus@firma.no")`.

🆕 **Prompt:** *«Lag en A4-utskriftsmal med kvartalstallene og legg den i køen min.»*
→ AI genererer PDF inline → `printix_print_self(file_b64=..., filename="Q1.pdf")` legger jobben i din egen kø. Hent på skriveren med kort/kode.

🆕 **Prompt:** *«Send dette notatet til alle i markedsgruppen som Secure Print.»*
→ `printix_print_to_recipients(recipients_csv="group:Marketing", file_b64=..., filename="notat.pdf")` — én jobb per medlem.

🆕 **Prompt:** *«Før jeg sender: hvem er på listen?»*
→ `printix_resolve_recipients("group:Marketing, alice@firma.no, entra:abc-uuid")` viser oppløste mottakere uten å skrive ut. Tvetydigheter + ikke-funne inputs listes separat.

🆕 **Prompt:** *«Send denne kontraktutkastet til externer-gjest@partner.no — skal auto-utløpe etter 4 timer.»*
→ `printix_session_print(user_email="externer-gjest@partner.no", file_b64=..., filename="kontrakt.pdf", expires_in_hours=4)` — sender jobben og setter en cleanup-time-bomb.

🆕 **Prompt:** *«Skriv kun monokromt — vi trenger ikke farge her.»*
→ Sett `color=False` på ethvert utskriftsverktøy (standard `True`).

🆕 **Prompt:** *«Skriveren min gjør PDF-RIP selv — bare send filen som den er.»*
→ Sett `pdl="passthrough"` for å deaktivere serverside-konvertering.

---

## 4. Brukere, grupper & arbeidsstasjoner

Full livssyklus: opprett, rediger, deaktiver, diagnostiser. `user_360`- og `diagnose_user`-verktøyene er allroundere for helpdesk. **🆕 v6.8.x**: detaljert gruppemedlemskap + utskriftsmønsteranalyse + quota-vakt.

| Verktøy | Formål |
|---------|--------|
| `printix_list_users` | Alle brukere, med paginering + rolle-filter. |
| `printix_get_user` | Brukerdetaljer. |
| `printix_find_user` | Fuzzy-søk på e-post eller navn. |
| `printix_user_360` | 360°-visning: bruker + kort + grupper + arbeidsstasjoner + nyeste jobber. |
| `printix_diagnose_user` | Helpdesk-diagnose: hva fungerer, hva ikke, hvorfor. |
| `printix_create_user` / `printix_delete_user` | Brukeradministrasjon. |
| `printix_generate_id_code` | Ny ID-kode for en bruker. |
| `printix_onboard_user` / `printix_offboard_user` | Veiledet on-/offboarding (flere steg i ett kall). |
| `printix_list_admins` | Alle admins. |
| `printix_permission_matrix` | Matrise: bruker × tilganger. |
| `printix_inactive_users` | Brukere som har vært inaktive i N dager. |
| `printix_sso_status` | Sjekk SSO-mapping. |
| `printix_list_groups` / `printix_get_group` | Gruppeliste / -detaljer. |
| `printix_create_group` / `printix_delete_group` | Gruppeadministrasjon. |
| 🆕 `printix_get_group_members` | Medlemmer av en gruppe per UUID eller navn (sikkert mot tvetydighet). |
| 🆕 `printix_get_user_groups` | Omvendt oppslag: hvilke grupper er bruker X i? |
| 🆕 `printix_describe_user_print_pattern` | Utskriftsprofil: foretrukne skrivere, fargeandel, sideantall. |
| 🆕 `printix_quota_guard` | Pre-flight-burst-sjekk før innsending (verdict allow/throttle/block). |
| 🆕 `printix_print_history_natural` | Historikk med naturlig-språklige tidsvinduer (`heute`, `last week`, `Q1`, `7d`). |
| `printix_list_workstations` / `printix_get_workstation` | Arbeidsstasjoner. |

### Eksempeldialoger

**Prompt:** *«Fortell meg alt du vet om marcus@firma.no.»*
→ `printix_user_360(query="marcus@firma.no")` returnerer full 360°-visning.

**Prompt:** *«Hvorfor kan ikke Anna skrive ut lenger?»*
→ `printix_diagnose_user(email="anna@firma.no")` sjekker status, SSO, kort, grupper, blokkere — og foreslår løsninger.

**Prompt:** *«Hvilke brukere har vært inaktive i 180 dager?»*
→ `printix_inactive_users(days=180)` — kandidatliste for offboarding.

**Prompt:** *«Onboard ny ansatt: peter@firma.no, Peter Hansen, gruppe 'Finance'.»*
→ `printix_onboard_user(...)` kjører alle steg i riktig rekkefølge.

🆕 **Prompt:** *«Hvem er i markedsgruppen?»*
→ `printix_get_group_members("Marketing")` — for tvetydige navn får du en kandidatliste med UUID-er.

🆕 **Prompt:** *«Hvilke grupper er Anna i?»*
→ `printix_get_user_groups("anna@firma.no")` — sjekker brukerobjektet først, faller tilbake til gruppeskanning.

🆕 **Prompt:** *«Hva har Marcus skrevet ut i dag?»*
→ `printix_print_history_natural(user_email="marcus@firma.no", when="today")`.

🆕 **Prompt:** *«Marcus' utskriftsmønster siste 30 dager?»*
→ `printix_describe_user_print_pattern(user_email="marcus@firma.no", days=30)` — toppskrivere, fargeandel, gj.snittlig sideantall.

🆕 **Prompt:** *«Før jeg sender enda en PDF: har brukeren ikke sendt for mange de siste 5 minuttene?»*
→ `printix_quota_guard(user_email="marcus@firma.no", window_minutes=5, max_jobs=10)` — verdict `allow`/`throttle`/`block`.

---

## 5. Kort & kortprofiler

Alt rundt RFID/Mifare/HID-kort: registrering, mapping, profiler, masseimport. **🆕 v6.8.x**: AI-wrapper `card_enrol_assist` som gjør UID + profile-transform + register i ett verktøy.

| Verktøy | Formål |
|---------|--------|
| `printix_list_cards` | Kort til en bruker. |
| `printix_list_cards_by_tenant` | Alle kort i tenant (filter: `all`/`registered`/`orphaned`). |
| `printix_search_card` | Søk kort etter ID/nummer. |
| `printix_register_card` | Tilordne kort til bruker (lavnivå). |
| `printix_delete_card` | Fjern korttilordning. |
| `printix_get_card_details` | Kort + lokal mapping + eier i én blokk. |
| `printix_decode_card_value` | Dekod rå kortverdi (Base64/Hex/YSoft/Konica-varianter). |
| `printix_transform_card_value` | Kjør verdi gjennom transformasjonspipeline (hex↔dec, reverse, prefix/suffix …). |
| `printix_get_user_card_context` | Bruker + alle kort + profiler i én blokk. |
| `printix_list_card_profiles` / `printix_get_card_profile` | Profilliste/-detaljer. |
| `printix_search_card_mappings` | Søk lokal mapping-DB. |
| `printix_bulk_import_cards` | CSV-masseimport (med profil + dry-run). |
| `printix_suggest_profile` | Foreslå profil ut fra eksempel-UID (topp-10). |
| `printix_card_audit` | Revisjonslogg over alle kortendringer for en bruker. |
| `printix_find_orphaned_mappings` | Lokale mappinger uten matchende Printix-bruker. |
| 🆕 `printix_card_enrol_assist` | AI-onboarding: UID + profile-transform + register i ett kall. |

### Eksempeldialoger

**Prompt:** *«Hvilke kort har Marcus?»*
→ `printix_get_user_card_context(email="marcus@firma.no")` — bruker + alle kort + profiler.

**Prompt:** *«Hva er kort UID `04:5F:F0:02:AB:3C`?»*
→ `printix_decode_card_value(card_value="04:5F:F0:02:AB:3C")` gjenkjenner hex med skilletegn, returnerer dekodede bytes + profile-hint.

**Prompt:** *«Importer 500 kort fra denne CSV-en — først som dry-run.»*
→ `printix_bulk_import_cards(csv=..., profile=..., dry_run=True)` validerer hver rad og viser forhåndsvisning uten å skrive.

**Prompt:** *«For UID `045FF002` — hvilken profil passer?»*
→ `printix_suggest_profile(sample_uid="045FF002")` topp-10 med score + `best_match`.

🆕 **Prompt:** *«Marcus tappet ID-kortet på iPhone. UID er `04A1B2C3D4E5F6`. Vennligst registrer det for ham.»*
→ `printix_card_enrol_assist(user_email="marcus@firma.no", card_uid_raw="04A1B2C3D4E5F6")` — kjøres gjennom standard kortprofil og registreres via `register_card`.

---

## 6. Rapporter & analyser

Rapporter kjører mot SQL Server-warehouse. Du får nøkkeltall, trender, anomalier og ad-hoc-spørringer via et enhetlig grensesnitt. `query_any` er det universelle inngangspunktet; spesialiserte verktøy er raskere snarveier for vanlige spørsmål.

| Verktøy | Formål |
|---------|--------|
| `printix_reporting_status` | Status av rapportmotoren (DB-tilkobling, siste nightlies, preset-antall). |
| `printix_query_any` | Universelt: preset + filtre → tabell. |
| `printix_query_print_stats` | Utskriftsvolum etter dimensjon. |
| `printix_query_cost_report` | Kostnader, valgfritt etter avdeling/bruker. |
| `printix_query_top_users` / `printix_query_top_printers` | Topp-N med tidsvindu. |
| `printix_query_anomalies` | Anomali-deteksjon. |
| `printix_query_trend` | Trendlinjer over tid. |
| `printix_query_audit_log` | Strukturert revisjonsspor av MCP-serveren (handlinger, objekter, aktør). |
| `printix_top_printers` / `printix_top_users` | Kort-form-wrappers. |
| `printix_print_trends` | Trend etter dag/uke/måned. |
| `printix_cost_by_department` | Kostnader aggregert per avdeling. |
| `printix_compare_periods` | Periode A vs periode B. |

### Eksempeldialoger

**Prompt:** *«Hvem skrev ut mest forrige måned?»*
→ `printix_top_users(days=30, limit=10, metric="pages")`.

**Prompt:** *«Vis utskriftstrenden de siste 90 dagene, månedlig.»*
→ `printix_print_trends(group_by="month", days=90)`.

**Prompt:** *«Sammenlign de siste 30 dagene med de 30 dagene før — hva har endret seg?»*
→ `printix_compare_periods(days_a=30, days_b=30)` returnerer delta-KPI-er.

**Prompt:** *«Hvilken avdeling har høyeste utskriftskostnader?»*
→ `printix_cost_by_department(days=30)`.

**Prompt:** *«Hva gjorde bruker X i MCP den 15. april?»*
→ `printix_query_audit_log(start_date="2026-04-15", end_date="2026-04-15", actor_email="x@firma.no")`.

**Prompt:** *«Er det noen anomalier i utskriftsadferden de siste 14 dagene?»*
→ `printix_query_anomalies(days=14)` — f.eks. plutselige volumtopper eller uvanlige skrivermønstre.

---

## 7. Rapportmaler & planlegging

For analyser du trenger regelmessig: lagre som mal, planlegg gjentakende levering, send på e-post. Designvalg via `list_design_options`; `preview_report` rendrer en PDF-forhåndsvisning uten å sende.

| Verktøy | Formål |
|---------|--------|
| `printix_save_report_template` | Lagre query + design som mal. |
| `printix_list_report_templates` | Alle lagrede maler. |
| `printix_get_report_template` | Maldetaljer. |
| `printix_delete_report_template` | Slett mal. |
| `printix_run_report_now` | Kjør mal én gang, lever. |
| `printix_send_test_email` | Test-e-post (SMTP-sjekk). |
| `printix_schedule_report` | Planlegg mal som cron-jobb. |
| `printix_list_schedules` | Alle aktive planer. |
| `printix_update_schedule` / `printix_delete_schedule` | Plan modifiser/slett. |
| `printix_list_design_options` | Tilgjengelige fargeskjemaer, logoer, layouter. |
| `printix_preview_report` | PDF-forhåndsvisning av en rapport uten å sende. |

### Eksempeldialoger

**Prompt:** *«Lagre nåværende topp-10-bruker-rapport som mal 'Månedlig utskrift Top10'.»*
→ `printix_save_report_template(...)`.

**Prompt:** *«Send denne malen den 1. virkedagen hver måned til ledelse@firma.no.»*
→ `printix_schedule_report(report_id=…, cron="0 8 1 * *", recipients=["ledelse@firma.no"])`.

**Prompt:** *«Vis meg en PDF-forhåndsvisning av mal XY.»*
→ `printix_preview_report(report_id=…)`.

**Prompt:** *«Hvilke fargeskjemaer kan jeg bruke for rapporter?»*
→ `printix_list_design_options()`.

---

## 8. Capture & dokumentarbeidsflyt

Capture kobler skannede eller AI-genererte dokumenter til målsystemer (Paperless-ngx, SharePoint, DMS …) via plugins. **🆕 v6.8.x**: to nye verktøy som lar AI mate filer direkte inn i en capture-arbeidsflyt — ikke bare via klassisk webhook.

| Verktøy | Formål |
|---------|--------|
| `printix_list_capture_profiles` | Alle capture-profiler i tenant. |
| `printix_capture_status` | Status: server-port, webhook-base-URL, tilgjengelige plugins, konfigurerte profiler. |
| 🆕 `printix_describe_capture_profile` | Plugin-skjema for en profil: hvilke metadata-felt godtas, pluss aktuell konfig (secrets maskert). |
| 🆕 `printix_send_to_capture` | Mat fil direkte inn i capture-arbeidsflyt — samme kodevei som webhook, men uten skriver/blob-omvei. |

### Eksempeldialoger

**Prompt:** *«Er capture aktiv og hvilke plugins er installert?»*
→ `printix_capture_status` — pluginliste (f.eks. paperless_ngx) + antall konfigurerte profiler.

**Prompt:** *«Hvilke capture-profiler har jeg?»*
→ `printix_list_capture_profiles` — liste med målsystem, webhook-URL og siste kjøringer.

🆕 **Prompt:** *«Hvilke metadata godtar Paperless-profilen?»*
→ `printix_describe_capture_profile("Paperless (Marcus)")` — plugin-skjema (`tags`, `correspondent`, `document_type` osv.) pluss aktuelle standardverdier.

🆕 **Prompt:** *«Lagre denne AI-genererte kontrakten i Paperless med tagger 'Q1', 'Kontrakt' og korrespondent 'Acme Corp'.»*
→ `printix_send_to_capture(profile="Paperless (Marcus)", file_b64=..., filename="kontrakt_acme.pdf", metadata_json='{"tags":["Q1","Kontrakt"], "correspondent":"Acme Corp", "document_type":"Kontrakt"}')`.

🆕 **Prompt:** *«Ukerapporten min er klar — legg den i Paperless og send en utskriftskopi til skriveren min også.»*
→ To kall: `printix_send_to_capture(...)` + `printix_print_self(...)` — assistenten kjeder begge automatisk.

---

## 9. Onboarding, Time-Bombs & Entra-synk 🆕

Alt i denne seksjonen er **nytt i v6.8.x**. Det handler om automatiserte brukerlivsyklus-arbeidsflyter: en nyopprettet bruker får en velkomst-PDF, påminnelses-time-bombs planlegges, og 7 dager senere følger vi opp automatisk hvis ingen handling er gjort. Pluss: synkronisering av Entra/AD-grupper med Printix-grupper.

| Verktøy | Formål |
|---------|--------|
| 🆕 `printix_welcome_user` | Onboarding-følgesvenn: velkomst-PDF + time-bombs for `card_enrol`, `first_print_reminder`. |
| 🆕 `printix_list_timebombs` | Vis aktive/historiske time-bombs i tenant. |
| 🆕 `printix_defuse_timebomb` | Manuelt deaktivere en time-bomb (med revisjonsbegrunnelse). |
| 🆕 `printix_sync_entra_group_to_printix` | Diff Entra/AD-gruppemedlemmer vs Printix-gruppe (standard `report_only`). |

### Eksempeldialoger

🆕 **Prompt:** *«Onboard ny ansatt peter@firma.no og kjør velkomst-arbeidsflyten med påminnelser.»*
→ `printix_onboard_user(...)` (for DB-/kontooppretting) + `printix_welcome_user(user_email="peter@firma.no")` legger en velkomst-PDF i hans secure-print-kø OG setter to time-bombs: 3-dagers påminnelse hvis ingen første utskrift, 7-dagers påminnelse hvis ingen kort registrert.

🆕 **Prompt:** *«Hvilke onboarding-påminnelser er aktive nå?»*
→ `printix_list_timebombs(status="pending")`.

🆕 **Prompt:** *«Peter sa han er på ferie til neste uke. Deaktiver påminnelsene hans.»*
→ `printix_list_timebombs(user_email="peter@firma.no")` viser bombene med ID-er, deretter `printix_defuse_timebomb(bomb_id=42, reason="på ferie til 2026-05-12")`.

🆕 **Prompt:** *«Synk Entra-gruppen `Marketing-DACH` (OID `abc-123`) med Printix-gruppen vår — men bare rapport, ingen skriving.»*
→ `printix_sync_entra_group_to_printix(entra_group_oid="abc-123", printix_group_id="def-456", sync_mode="report_only")` returnerer diff: `to_add` (i Entra men ikke Printix) og `to_remove` (i Printix men ikke Entra).

> ℹ️ **Forutsetning for `sync_entra_group_to_printix`**: MCP-serverens Entra-app-registrering trenger applikasjonstilgangen `Group.Read.All` (Application, ikke Delegated) + admin-samtykke. Legg til i Azure-portalen.

### Time-bomb-konseptet

Time-bombs er **betingede, utsatte handlinger**. En cron-jobb kjører hver time (`minute=7`) og sjekker for hver pending bomb:

1. Er betingelsen fortsatt oppfylt? (f.eks. *«bruker har ikke registrert kort ennå»*)
2. Hvis ja → kjør handlingen (legg en påminnelses-PDF i kø, loggoppføring osv.).
3. Hvis nei → bomb auto-merkes `defused`.

Så `first_print_reminder_3d`-bomben forblir pending etter 3 dager — hvis brukeren faktisk har skrevet ut i mellomtiden, defuses den automatisk. Bare hvis brukeren virkelig ikke har gjort noe, fyrer påminnelsen.

---

## 10. Drift, vedlikehold & revisjon

Backups, demo-data, feature-tracking. Mix av drift og meta.

| Verktøy | Formål |
|---------|--------|
| `printix_list_backups` | Alle eksisterende backups. |
| `printix_create_backup` | Ny backup (DB + konfig + metadata). |
| `printix_demo_setup_schema` | Opprett demo-skjema i rapport-DB. |
| `printix_demo_generate` | Generer syntetiske demo-data. |
| `printix_demo_rollback` | Fjern demo-data (per demo-tag). |
| `printix_demo_status` | Hvilke demo-sett er aktive? |
| `printix_list_feature_requests` / `printix_get_feature_request` | Ticket-system for feature-ønsker. |

### Eksempeldialoger

**Prompt:** *«Lag en backup før jeg endrer noe.»*
→ `printix_create_backup`.

**Prompt:** *«Sett opp et demo-miljø med 50 brukere og 500 jobber.»*
→ `printix_demo_setup_schema` (én gang) + `printix_demo_generate(users=50, jobs=500)`.

**Prompt:** *«Vis meg alle åpne feature-ønsker.»*
→ `printix_list_feature_requests(status="open")`.

---

## 11. Tips for produktive AI-dialoger

1. **Tenk i mål, ikke verktøy.** *«Hvem skriver ut for mye?»* slår *«kall query_top_users med days=30»*. Assistenten velger riktig verktøy.
2. **Gi kontekst.** *«Marcus fra finans»* er klarere enn bare *«Marcus»*.
3. **Bruk 360°-verktøyene.** `printix_user_360`, `printix_get_queue_context`, `printix_site_summary` sparer oppfølgingsspørsmål.
4. **Spør «hvorfor» ved feil.** *«Hvorfor feilet dette?»* trigger `printix_explain_error` eller `printix_diagnose_user`.
5. **Dry-run før masseoperasjoner.** `printix_bulk_import_cards(dry_run=True)`, `printix_resolve_recipients` (før `print_to_recipients`), `printix_sync_entra_group_to_printix(sync_mode="report_only")`.
6. **🆕 Multi-step-arbeidsflyter i én prompt.** Assistenten kan kjede verktøykall: *«Generer en ukerapport-PDF, arkiver i Paperless, og skriv ut en kopi til meg.»*
7. **🆕 Stol på auto-konvertering i `print_*`-verktøy.** Standard `pdl="auto"` gjør det rette i 99% av tilfellene (PCL XL farge). Override bare når skriverkøen din konverterer serverside (`passthrough`) eller du eksplisitt trenger PostScript.
8. **🆕 Oppfrisk verktøylisten regelmessig** (se varsel øverst). Ellers bruker assistenten utdaterte verktøydefinisjoner.

---

*Dokument generert fra Printix MCP Server v6.8.10 · April 2026 · 127 verktøy · [Repository](https://github.com/mnimtz/Printix-MCP)*
