"""
Demo Data Generator — Lokale SQLite Demo-Daten (v4.4.0)
=======================================================
Generiert realistische Demo-Daten und speichert sie in der lokalen SQLite-DB
(/data/demo_data.db). Kein Azure SQL Schreibzugriff mehr nötig!

Features:
  - Kulturell passende Namen in 8 Sprachen (DE, EN, FR, IT, ES, NL, SV, NO)
  - Herstellerbasierte Drucker-Namenskonvention (HP, Xerox, Canon, Ricoh, KM, Kyocera)
  - Realistische Druckvolumen-Verteilung (Saisonalität, Tageszeiten, Duplex/Farbe)
  - Print-, Scan- und Kopieraufträge mit Dateinamen und Capture-Workflows
  - Komplettes Rollback via demo_session_id — alle Demo-Daten löschbar
  - Daten lokal in SQLite, Reports mergen automatisch mit Azure SQL (read-only)

Schema: Lokale SQLite via local_demo_db.py, Azure SQL nur noch lesend.
"""

import uuid
import json
import random
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Konstanten: Namen ──────────────────────────────────────────────────────────

NAMES: dict[str, dict[str, list[str]]] = {
    "de": {
        "first": ["Hans","Klaus","Petra","Sabine","Michael","Andrea","Thomas","Maria",
                  "Günther","Ursula","Wolfgang","Monika","Dieter","Brigitte","Frank",
                  "Christine","Bernd","Karin","Jörg","Ute","Rainer","Ingrid","Holger","Silke",
                  "Stefan","Barbara","Martin","Claudia","Andreas","Susanne","Jürgen","Gabriele",
                  "Peter","Birgit","Matthias","Kerstin","Rolf","Heike","Gerhard","Angelika",
                  "Uwe","Elke","Ralf","Marion","Harald","Manuela","Norbert","Nicole","Helmut",
                  "Martina","Volker","Beate","Dirk","Anja","Axel","Tanja","Bernhard","Daniela",
                  "Karl","Simone","Fabian","Julia","Lukas","Lena","Sebastian","Nina","Tobias",
                  "Katharina","Alexander","Sandra","Markus","Stefanie"],
        "last":  ["Müller","Schmidt","Schneider","Fischer","Weber","Meyer","Wagner","Becker",
                  "Schulz","Hoffmann","Schäfer","Koch","Bauer","Richter","Klein","Wolf",
                  "Schröder","Neumann","Schwarz","Zimmermann","Braun","Krüger","Hofmann","Lange",
                  "Schmitt","Werner","Schmitz","Krause","Meier","Lehmann","Schmid","Schulze",
                  "Maier","Köhler","Herrmann","König","Walter","Mayer","Huber","Kaiser","Fuchs",
                  "Peters","Lang","Scholz","Möller","Weiß","Jung","Hahn","Schubert","Vogel",
                  "Friedrich","Keller","Günther","Frank","Berger","Winkler","Roth","Beck",
                  "Lorenz","Baumann","Franke","Albrecht","Schuster","Simon","Ludwig","Böhm",
                  "Winter","Kraus","Martin","Schumacher","Krämer","Vogt","Stein","Jäger","Otto"],
    },
    "en": {
        "first": ["John","Sarah","Michael","Emma","David","Lisa","James","Jennifer","Robert",
                  "Mary","William","Patricia","Richard","Linda","Joseph","Barbara","Thomas",
                  "Elizabeth","Charles","Susan","Daniel","Jessica","Matthew","Ashley",
                  "Christopher","Amanda","Andrew","Melissa","Joshua","Deborah","Kenneth",
                  "Stephanie","Paul","Rebecca","Mark","Laura","Donald","Helen","Steven","Sharon",
                  "Kevin","Cynthia","Brian","Kathleen","George","Amy","Edward","Shirley","Ronald",
                  "Angela","Timothy","Anna","Jason","Brenda","Jeffrey","Pamela","Ryan","Nicole",
                  "Jacob","Samantha","Gary","Katherine","Nicholas","Christine","Eric","Debra",
                  "Jonathan","Rachel","Stephen","Catherine","Larry","Carolyn","Justin","Janet"],
        "last":  ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
                  "Wilson","Taylor","Anderson","Jackson","White","Harris","Martin",
                  "Thompson","Young","Walker","Robinson","Lewis","Clark","Hall","Allen",
                  "Wright","King","Scott","Green","Baker","Adams","Nelson","Carter","Mitchell",
                  "Perez","Roberts","Turner","Phillips","Campbell","Parker","Evans","Edwards",
                  "Collins","Stewart","Morris","Rogers","Reed","Cook","Morgan","Bell","Murphy",
                  "Bailey","Rivera","Cooper","Richardson","Cox","Howard","Ward","Torres",
                  "Peterson","Gray","Ramirez","James","Watson","Brooks","Kelly","Sanders",
                  "Price","Bennett","Wood","Barnes","Ross","Henderson","Coleman","Jenkins"],
    },
    "fr": {
        "first": ["Jean","Marie","Pierre","Sophie","Luc","Isabelle","François","Nathalie",
                  "Philippe","Sylvie","Michel","Catherine","Christophe","Valérie","Nicolas",
                  "Sandrine","Stéphane","Laurence","Patrick","Anne","Julien","Céline",
                  "Olivier","Véronique","Alain","Brigitte","Laurent","Corinne","Éric",
                  "Martine","Thierry","Nicole","Bruno","Chantal","Pascal","Dominique",
                  "Frédéric","Florence","Didier","Christine","Hervé","Monique","Sébastien",
                  "Caroline","Vincent","Hélène","Xavier","Karine","Emmanuel","Emilie","Fabien",
                  "Camille","Antoine","Julie","Alexandre","Delphine","Mathieu","Aurélie",
                  "Guillaume","Stéphanie","Romain","Sarah","Arnaud","Marion","Damien","Audrey",
                  "Cédric","Élodie","Gilles","Charlotte","Thomas","Amélie","Rémy","Manon"],
        "last":  ["Dupont","Martin","Bernard","Dubois","Thomas","Robert","Richard","Petit",
                  "Durand","Leroy","Moreau","Simon","Laurent","Lefebvre","Michel","Garcia",
                  "David","Bertrand","Roux","Vincent","Fournier","Morel","Girard","André",
                  "Mercier","Blanc","Guérin","Boyer","Garnier","Chevalier","Francois","Legrand",
                  "Gauthier","Perrin","Robin","Clément","Morin","Nicolas","Henry","Roussel",
                  "Mathieu","Gautier","Masson","Marchand","Duval","Denis","Dumont","Marie",
                  "Lemaire","Noël","Meyer","Dufour","Meunier","Brun","Blanchard","Giraud",
                  "Joly","Rivière","Lucas","Brunet","Gaillard","Barbier","Arnaud","Martinez",
                  "Gerard","Roche","Renard","Schmitt","Roy","Leroux","Colin","Vidal","Caron"],
    },
    "it": {
        "first": ["Marco","Giulia","Luca","Sara","Andrea","Francesca","Matteo","Chiara",
                  "Davide","Valentina","Alessandro","Laura","Simone","Elena","Federico",
                  "Martina","Riccardo","Paola","Stefano","Giorgia","Antonio","Roberta",
                  "Giuseppe","Silvia","Francesco","Anna","Giovanni","Alessia","Paolo",
                  "Barbara","Roberto","Cristina","Luigi","Claudia","Salvatore","Monica",
                  "Mario","Patrizia","Vincenzo","Rossella","Alberto","Daniela","Fabio","Raffaella",
                  "Massimo","Lucia","Enrico","Manuela","Claudio","Michela","Giorgio","Elisa",
                  "Pietro","Tiziana","Carlo","Sabrina","Lorenzo","Angela","Nicola","Rita",
                  "Emanuele","Letizia","Gianluca","Serena","Alessio","Eleonora","Filippo","Ilaria"],
        "last":  ["Rossi","Ferrari","Russo","Bianchi","Romano","Gallo","Costa","Fontana",
                  "Conti","Esposito","Ricci","Bruno","De Luca","Moretti","Lombardi",
                  "Barbieri","Testa","Serra","Fabbri","Villa","Pellegrini","Marini",
                  "Greco","Mancini","Marino","Rizzo","Lombardo","Giordano","Galli","Leone",
                  "Longo","Gentile","Martinelli","Cattaneo","Morelli","Ferrara","Santoro",
                  "Mariani","Rinaldi","Caruso","Ferri","Sala","Monti","De Santis","Marchetti",
                  "D'Amico","Colombo","Gatti","Parisi","Bellini","Grassi","Benedetti","Giuliani",
                  "Amato","Battaglia","Sanna","Farina","Palumbo","Coppola","Basile","Riva",
                  "Donati","Orlando","Bianco","Valentini","Pagano","Piras","Messina","Cattivelli"],
    },
    "es": {
        "first": ["Carlos","Ana","Miguel","Carmen","José","María","Antonio","Isabel",
                  "Francisco","Laura","Manuel","Marta","Juan","Cristina","David","Elena",
                  "Pedro","Lucía","Alejandro","Patricia","Diego","Sofía","Javier","Raquel",
                  "Jorge","Rosa","Luis","Pilar","Rafael","Dolores","Ángel","Teresa","Fernando",
                  "Nuria","Ramón","Mónica","Jesús","Beatriz","Rubén","Ángela","Sergio","Silvia",
                  "Alberto","Rocío","Óscar","Sonia","Iván","Julia","Álvaro","Alicia","Mario",
                  "Eva","Adrián","Clara","Pablo","Inés","Daniel","Andrea","Víctor","Natalia",
                  "Roberto","Sara","Enrique","Claudia","Gabriel","Paula","Emilio","Victoria","Marcos"],
        "last":  ["García","Martínez","López","Sánchez","González","Pérez","Rodríguez",
                  "Fernández","Torres","Ramírez","Flores","Morales","Ortiz","Vargas","Díaz",
                  "Reyes","Gómez","Molina","Herrera","Silva","Castro","Romero","Navarro",
                  "Jiménez","Álvarez","Moreno","Muñoz","Alonso","Gutiérrez","Ruiz","Hernández",
                  "Serrano","Blanco","Suárez","Castillo","Ortega","Rubio","Sanz","Iglesias",
                  "Nuñez","Medina","Garrido","Santos","Cortés","Lozano","Guerrero","Cano",
                  "Prieto","Méndez","Cruz","Calvo","Gallego","Vidal","León","Márquez","Herrero",
                  "Peña","Cabrera","Campos","Vega","Fuentes","Carrasco","Diez","Caballero","Reyes"],
    },
    "nl": {
        "first": ["Jan","Emma","Pieter","Sophie","Dirk","Anneke","Thomas","Lisa","Joost",
                  "Marieke","Bas","Inge","Tim","Claudia","Martijn","Evelien","Ruben",
                  "Nathalie","Sander","Iris","Lars","Roos","Jeroen","Fleur",
                  "Mark","Linda","Michiel","Esther","Wouter","Annemarie","Erik","Yvonne",
                  "Rick","Saskia","Kees","Monique","Johan","Petra","Bram","Marloes",
                  "Maarten","Wendy","Vincent","Karin","Daan","Femke","Stijn","Hanneke",
                  "Niels","Suzanne","Koen","Judith","Robin","Mirjam","Jeroen","Astrid",
                  "Jasper","Caroline","Joost","Lieke","Tom","Sanne","Freek","Mariska"],
        "last":  ["de Vries","Janssen","van den Berg","Bakker","Peters","Visser","Meijer",
                  "Bos","Mulder","de Boer","Smit","Dekker","van Leeuwen","Dijkstra","van Dijk",
                  "Vermeulen","Kok","Jacobs","Brouwer","de Groot","Willems","van der Meer",
                  "van Beek","Schouten","Hoekstra","van Dam","Verhoeven","de Wit","Prins","Bosch",
                  "Huisman","Peeters","van der Velde","Kuipers","van der Linden","Koster",
                  "Gerritsen","van Veen","van den Broek","Willemsen","Timmermans","Martens",
                  "van Loon","Hendriks","Wolters","de Lange","Koning","van Zanten","Scholten"],
    },
    "sv": {
        "first": ["Erik","Anna","Lars","Maja","Björn","Linnea","Johan","Emma","Mikael",
                  "Lena","Anders","Sofia","Per","Maria","Henrik","Sara","Jonas","Karin",
                  "Stefan","Ingrid","Oskar","Frida","Viktor","Johanna",
                  "Peter","Kerstin","Daniel","Helena","Magnus","Eva","Thomas","Birgitta",
                  "Jan","Ulla","Bengt","Margareta","Kalle","Monika","Axel","Linda",
                  "Fredrik","Cecilia","Gustav","Elsa","Ludvig","Astrid","Rasmus","Alma",
                  "Oliver","Wilma","Isak","Nora","Alexander","Ida","Simon","Alice"],
        "last":  ["Eriksson","Johansson","Andersson","Lindqvist","Nilsson","Larsson",
                  "Svensson","Gustafsson","Pettersson","Persson","Olsson","Bergström",
                  "Holm","Björk","Lindberg","Magnusson","Carlsson","Jakobsson","Hansson","Karlsson",
                  "Jonsson","Lindström","Axelsson","Berglund","Fredriksson","Sandberg","Henriksson",
                  "Forsberg","Sjöberg","Lundberg","Wallin","Engström","Danielsson","Håkansson",
                  "Lund","Bengtsson","Jönsson","Lindgren","Berg","Fransson","Holmberg","Nyström"],
    },
    "no": {
        "first": ["Erik","Ingrid","Lars","Astrid","Ole","Kari","Bjørn","Elin","Tor","Silje",
                  "Per","Anne","Gunnar","Kristin","Svein","Hanne","Trond","Randi","Dag","Marit",
                  "Jan","Berit","Arne","Liv","Rolf","Eli","Knut","Turid","Odd","Ragnhild",
                  "Geir","Sissel","Morten","Trine","Håkon","Linda","Kjell","Grete","Tore","Unni",
                  "Magnus","Mari","Eirik","Nora","Henrik","Ida","Jonas","Emma","Sindre","Ingeborg"],
        "last":  ["Hansen","Johansen","Olsen","Larsen","Andersen","Pedersen","Nilsen",
                  "Kristiansen","Jensen","Karlsen","Johnsen","Haugen","Pettersen","Eriksen",
                  "Berg","Dahl","Halvorsen","Iversen","Moen","Jacobsen","Strand","Lund",
                  "Solberg","Bakken","Svendsen","Martinsen","Rasmussen","Kristoffersen","Jørgensen",
                  "Nygård","Paulsen","Gundersen","Ellingsen","Lie","Mathisen","Knutsen","Aas",
                  "Sæther","Hagen","Antonsen","Ruud","Christensen","Thomassen","Hauge"],
    },
}

# ── Konstanten: Drucker ────────────────────────────────────────────────────────

# (vendor, model_full, code_prefix, is_color)
PRINTER_MODELS: list[tuple[str, str, str, bool]] = [
    ("HP",              "Color LaserJet Pro M479fdw",       "HP-CLJ",  True),
    ("HP",              "LaserJet Enterprise M507dn",        "HP-LJE",  False),
    ("HP",              "LaserJet Pro M404dn",               "HP-LJP",  False),
    ("HP",              "Color LaserJet Enterprise M554dn",  "HP-CLE",  True),
    ("Xerox",           "VersaLink C505",                    "XRX-VLC", True),
    ("Xerox",           "WorkCentre 7845",                   "XRX-WC",  True),
    ("Xerox",           "AltaLink C8170",                    "XRX-ALC", True),
    ("Xerox",           "Phaser 6510",                       "XRX-PH",  True),
    ("Canon",           "imageRUNNER ADVANCE C5560i",        "CNX-iR",  True),
    ("Canon",           "i-SENSYS MF543x",                   "CNX-MF",  True),
    ("Canon",           "MAXIFY GX7050",                     "CNX-MX",  True),
    ("Ricoh",           "MP C3004",                          "RCH-MPC", True),
    ("Ricoh",           "IM C2000",                          "RCH-IMC", True),
    ("Ricoh",           "SP 5310DN",                         "RCH-SP",  False),
    ("Konica Minolta",  "bizhub C450i",                      "KM-BHC",  True),
    ("Konica Minolta",  "bizhub 4702P",                      "KM-BH",   False),
    ("Kyocera",         "TASKalfa 3553ci",                   "KYO-TA",  True),
    ("Kyocera",         "ECOSYS P3145dn",                    "KYO-EC",  False),
    ("Lexmark",         "CX625ade",                          "LXM-CX",  True),
    ("Lexmark",         "MS622de",                           "LXM-MS",  False),
    ("Brother",         "MFC-L9570CDW",                      "BTH-MFC", True),
    ("Sharp",           "MX-3070N",                          "SHP-MX",  True),
]

FLOOR_CODES  = ["EG", "OG1", "OG2", "OG3", "KG", "DG"]
DEPARTMENTS  = ["IT", "HR", "FIN", "MKT", "VTR", "LOG", "MGT", "PRD", "QM", "EKF", "RD"]
PAPER_SIZES  = ["A4"] * 80 + ["A3"] * 10 + ["Letter"] * 8 + ["A5"] * 2   # weighted
MONTH_FACTORS = {1:0.95, 2:1.00, 3:1.05, 4:1.05, 5:1.00, 6:0.90,
                 7:0.80, 8:0.55, 9:1.05, 10:1.10, 11:1.00, 12:0.65}

# ── Konstanten: Dateinamen ─────────────────────────────────────────────────────

_PRINT_TEMPLATES = [
    "Rechnung_{nr:04d}.pdf",
    "Angebot_Kunde_{nr:03d}.pdf",
    "Lieferschein_{nr:05d}.pdf",
    "Bestellung_{nr:04d}.pdf",
    "Protokoll_Meeting_{date}.pdf",
    "Vertrag_{nr:03d}.pdf",
    "KV_Projekt_{nr:03d}.pdf",
    "Mahnschreiben_{nr:03d}.pdf",
    "Praesentation_Produkt.pptx",
    "Jahresbericht_{year}.pdf",
    "Budget_{year}_Q{q}.xlsx",
    "Report_Q{q}_{year}.xlsx",
    "Vertriebsbericht_KW{kw:02d}.pdf",
    "Handbuch_v{maj}.{min}.pdf",
    "Schulungsunterlage_{nr:02d}.pdf",
    "Zertifikat_{nr:03d}.pdf",
    "Reisekostenabrechnung_{date}.xlsx",
    "Projektplan_{nr:03d}.pdf",
]

_SCAN_TEMPLATES = [
    "SCAN_{date}_{time}.pdf",
    "Eingang_Rechnung_{date}.pdf",
    "Posteingang_{date}.pdf",
    "Lieferschein_Eingang_{nr:04d}.pdf",
    "Beleg_{date}.pdf",
    "Vertrag_Scan_{date}.pdf",
    "Personalakte_Eingang.pdf",
    "Brief_Eingang_{date}.pdf",
    "Zertifikat_Eingang_{date}.pdf",
]

# ── Sensible Dateinamen (v3.8.0) ──────────────────────────────────────────────
# Diese Templates enthalten bewusst Schlüsselwörter aus den 6 Keyword-Sets des
# "Sensible Dokumente"-Reports (HR, Finanzen, Vertraulich, Gesundheit, Recht, PII),
# damit Demo-Datasets Treffer für den Compliance-Scan liefern. Anteil in den
# Print-/Scan-Jobs: ~8 % (siehe _filename_print/_filename_scan).
_SENSITIVE_PRINT_TEMPLATES = [
    # HR
    "Gehaltsabrechnung_{year}_{mo:02d}.pdf",
    "Lohnabrechnung_{user}_{mo:02d}_{year}.pdf",
    "Arbeitsvertrag_{user}.pdf",
    "Kuendigung_Entwurf_{nr:03d}.pdf",
    "Personalakte_{user}.pdf",
    "Bewerbung_{user}_CV.pdf",
    # Finanzen
    "Kreditkartenabrechnung_{year}_{mo:02d}.pdf",
    "IBAN_Liste_Kunden_{year}.xlsx",
    "Kontoauszug_{year}_{mo:02d}.pdf",
    "Steuererklaerung_{year}.pdf",
    "Bilanz_Entwurf_{year}.xlsx",
    # Vertraulich / Confidential
    "VERTRAULICH_Strategie_{year}.pdf",
    "Confidential_Board_Meeting_{date}.pdf",
    "NDA_{kunde}_{nr:03d}.pdf",
    "Geheim_MA_Deal_{nr:03d}.pdf",
    # Gesundheit / Health
    "Krankmeldung_{user}_{date}.pdf",
    "Arztbrief_{user}.pdf",
    "AU_Bescheinigung_{nr:04d}.pdf",
    # Recht / Legal
    "Klageschrift_{nr:03d}.pdf",
    "Anwaltsschreiben_{kunde}.pdf",
    "Gerichtsbeschluss_{nr:04d}.pdf",
    "Mahnbescheid_{nr:04d}.pdf",
    # PII
    "Personalausweis_Kopie_{user}.pdf",
    "Reisepass_Scan_{user}.pdf",
    "SVN_Liste_{year}.xlsx",
]

_SENSITIVE_SCAN_TEMPLATES = [
    "SCAN_Personalausweis_{date}.pdf",
    "SCAN_Reisepass_{date}.pdf",
    "SCAN_Gehaltsabrechnung_{date}.pdf",
    "SCAN_Arbeitsvertrag_{date}.pdf",
    "SCAN_Krankmeldung_{date}.pdf",
    "SCAN_Arztbrief_{date}.pdf",
    "SCAN_Kontoauszug_{date}.pdf",
    "SCAN_NDA_Vertraulich_{date}.pdf",
    "SCAN_Anwaltsschreiben_{date}.pdf",
    "SCAN_Personalakte_{date}.pdf",
    "SCAN_Kreditkarte_Beleg_{date}.pdf",
    "SCAN_VERTRAULICH_{date}.pdf",
]

# Wahrscheinlichkeit, mit der ein Dateiname aus dem sensiblen Pool gezogen wird.
_SENSITIVE_RATIO = 0.08

CAPTURE_WORKFLOWS = [
    "Posteingang digitalisieren",
    "Rechnungen Buchhaltung",
    "HR Personalakte",
    "Verträge Archiv",
    "Lieferscheine Lager",
    "Eingangspost Büro",
    "Qualitätsdoku QM",
    "Kundenkorrespondenz",
    "Behördenpost",
    "Projektdokumentation",
    "Finanzdokumente",
    "Einkauf Bestellungen",
]


# v4.4.7: Azure SQL SCHEMA_STATEMENTS und _create_v_jobs_view() entfernt.
# Demo-Daten liegen seit v4.4.0 komplett auf lokaler SQLite (/data/demo_data.db).


def setup_schema() -> dict:
    """
    v4.4.0: Initialisiert die lokale Demo-SQLite-DB (idempotent).
    Azure SQL Schema/Views werden NICHT mehr erstellt — Demo-Daten
    liegen jetzt komplett lokal. Azure SQL bleibt rein lesend.
    """
    from .local_demo_db import init_demo_db
    result = init_demo_db()
    return {
        "success":  True,
        "executed": 1,
        "errors":   [],
        "message":  "Demo-DB (lokal SQLite) bereit.",
    }


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())


def _pick_name(languages: list[str], rng: random.Random) -> tuple[str, str, str]:
    """Gibt (first, last, lang) aus einer zufälligen der gewählten Sprachen zurück."""
    lang = rng.choice(languages)
    if lang not in NAMES:
        lang = "de"
    bank = NAMES[lang]
    return rng.choice(bank["first"]), rng.choice(bank["last"]), lang


def _ascii_slug(s: str) -> str:
    """
    Wandelt Diakritika um und entfernt alles außer Buchstaben/Ziffern.
      'Günther' -> 'guenther'
      "D'Amico" -> 'damico'
      'de Vries' -> 'devries'
    """
    replacements = {"ä":"ae","ö":"oe","ü":"ue","ß":"ss","á":"a","à":"a","â":"a",
                    "é":"e","è":"e","ê":"e","ë":"e","í":"i","ì":"i","î":"i","ï":"i",
                    "ó":"o","ò":"o","ô":"o","ú":"u","ù":"u","û":"u","ñ":"n","ç":"c",
                    "ø":"o","å":"a","æ":"ae","Ä":"ae","Ö":"oe","Ü":"ue","É":"e","È":"e",
                    "Á":"a","À":"a","Í":"i","Ó":"o","Ú":"u","Ñ":"n","Ç":"c"}
    for k, v in replacements.items():
        s = s.replace(k, v)
    return "".join(c for c in s.lower() if c.isalnum())


def _email(first: str, last: str, domain: str) -> str:
    """
    Erzeugt eine saubere E-Mail-Adresse:
      'Günther Schröder' -> 'guenther.schroeder@domain'
      'Jean-Luc' 'de Vries' -> 'jeanluc.devries@domain'
    """
    return f"{_ascii_slug(first)}.{_ascii_slug(last)}@{domain}"


def _working_days(start: datetime, end: datetime) -> list[datetime]:
    """Gibt alle Werktage (Mo-Fr) zwischen start und end zurück."""
    days, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _random_time(day: datetime, rng: random.Random) -> datetime:
    """
    Zufällige Uhrzeit mit realistischer Verteilung:
    Spitzen 9-11 Uhr (35 %) und 13-15 Uhr (30 %).
    """
    segments = [(570, 660, 35), (780, 900, 30), (450, 570, 10),
                (660, 780, 10), (900, 1110, 15)]
    total = sum(w for _, _, w in segments)
    pick  = rng.randint(1, total)
    cumul = 0
    for s_min, e_min, w in segments:
        cumul += w
        if pick <= cumul:
            minute = rng.randint(s_min, e_min - 1)
            break
    else:
        minute = 540
    return day.replace(hour=minute // 60, minute=minute % 60,
                       second=rng.randint(0, 59), microsecond=0)


def _page_count(rng: random.Random) -> int:
    """Log-normalverteilte Seitenanzahl: Median ~3, max ~200."""
    pages = int(math.exp(rng.gauss(1.1, 0.85)))
    return max(1, min(200, pages))


def _filename_print(rng: random.Random, ts: datetime,
                    user: Optional[dict] = None) -> str:
    # v3.8.0 — mit Wahrscheinlichkeit _SENSITIVE_RATIO aus dem sensiblen Pool
    if rng.random() < _SENSITIVE_RATIO:
        tpl = rng.choice(_SENSITIVE_PRINT_TEMPLATES)
    else:
        tpl = rng.choice(_PRINT_TEMPLATES)
    user_slug = _ascii_slug(user["name"]) if user and user.get("name") else "mitarbeiter"
    return tpl.format(
        nr=rng.randint(1, 9999), date=ts.strftime("%Y-%m-%d"),
        year=ts.year, q=((ts.month - 1) // 3) + 1, mo=ts.month,
        kw=ts.isocalendar()[1], maj=rng.randint(1, 5), min=rng.randint(0, 9),
        kunde=rng.choice(["ABC","XYZ","Mustermann","Musterfrau","Omega","Alpha","Beta"]),
        thema=rng.choice(["Produkt","Service","Vertrieb","Marketing","IT","HR"]),
        user=user_slug,
    )


def _filename_scan(rng: random.Random, ts: datetime) -> str:
    # v3.8.0 — mit Wahrscheinlichkeit _SENSITIVE_RATIO aus dem sensiblen Pool
    if rng.random() < _SENSITIVE_RATIO:
        tpl = rng.choice(_SENSITIVE_SCAN_TEMPLATES)
    else:
        tpl = rng.choice(_SCAN_TEMPLATES)
    return tpl.format(
        nr=rng.randint(1, 9999), date=ts.strftime("%Y%m%d"),
        time=ts.strftime("%H%M%S"),
    )


# ── Daten-Generierung ─────────────────────────────────────────────────────────

def _gen_users(
    tenant_id: str, user_count: int, languages: list[str],
    session_id: str, rng: random.Random, email_domain: str,
) -> list[dict]:
    """
    Generiert Benutzer nach dem Schema 'Vorname Nachname'.

    Kollisionsbehandlung: Wenn derselbe 'Vorname Nachname' doppelt auftritt,
    wird ein Mittelinitial (A./B./C./...) eingefügt — so bleiben Anzeige-Name
    UND E-Mail-Adresse eindeutig und lesbar, statt dass nur die Mail-Adresse
    mit einer zufälligen Zahl ergänzt wird (was vorher 'komische Schreibweisen'
    erzeugt hat).
    """
    users: list[dict] = []
    seen_names_exact: set[str] = set()   # vollständiger Anzeigename (inkl. Initial)
    seen_base_counts: dict[str, int] = {}  # "Vorname Nachname" -> Anzahl bisher
    seen_emails: set[str] = set()

    attempts = 0
    max_attempts = max(user_count * 15, 100)
    while len(users) < user_count and attempts < max_attempts:
        attempts += 1
        first, last, _ = _pick_name(languages, rng)

        base_name = f"{first} {last}"
        count = seen_base_counts.get(base_name, 0)

        if count == 0:
            display_name = base_name
            email = _email(first, last, email_domain)
        else:
            # Mittelinitial einfügen: 'Hans A. Müller', 'Hans B. Müller', ...
            # count=1 -> A, count=2 -> B, ... Email: 'hans.a.mueller@...'
            initial = chr(ord('A') + ((count - 1) % 26))
            display_name = f"{first} {initial}. {last}"
            email = (
                f"{_ascii_slug(first)}.{initial.lower()}."
                f"{_ascii_slug(last)}@{email_domain}"
            )

        # Fallback: sehr unwahrscheinliche Kollision -> skippen, neu ziehen
        if display_name in seen_names_exact or email in seen_emails:
            continue

        seen_base_counts[base_name] = count + 1
        seen_names_exact.add(display_name)
        seen_emails.add(email)
        users.append({
            "id":              _uid(),
            "tenant_id":       tenant_id,
            "email":           email,
            "name":            display_name,
            "department":      rng.choice(DEPARTMENTS),
            "demo_session_id": session_id,
        })
    return users


def _gen_networks(
    tenant_id: str, sites: list[str], session_id: str,
) -> list[dict]:
    return [
        {"id": _uid(), "tenant_id": tenant_id, "name": s, "demo_session_id": session_id}
        for s in sites
    ]


def _gen_printers(
    tenant_id: str, printer_count: int, networks: list[dict],
    session_id: str, rng: random.Random,
) -> list[dict]:
    printers = []
    used_names: set[str] = set()
    models = rng.choices(PRINTER_MODELS, k=printer_count)
    for i, (vendor, model, prefix, _is_color) in enumerate(models):
        net = networks[i % len(networks)]
        floor = rng.choice(FLOOR_CODES)
        seq   = i + 1
        name  = f"[DEMO] {prefix}-{floor}-{seq:02d}"
        if name in used_names:
            name = f"[DEMO] {prefix}-{floor}-{seq:02d}b"
        used_names.add(name)
        printers.append({
            "id":              _uid(),
            "tenant_id":       tenant_id,
            "name":            name,
            "model_name":      model,
            "vendor_name":     vendor,
            "network_id":      net["id"],
            "location":        f"{net['name']} / {floor}",
            "demo_session_id": session_id,
        })
    return printers


def _gen_print_jobs(
    tenant_id: str, users: list[dict], printers: list[dict],
    working_days: list[datetime], jobs_per_user_day: float,
    session_id: str, rng: random.Random,
) -> tuple[list[tuple], list[tuple]]:
    jobs_rows: list[tuple] = []
    tracking_rows: list[tuple] = []

    # v4.4.15: Realistische Fehlerquoten für Service-Desk-Report
    ERROR_STATUSES = [
        "PRINT_FAILED",       # Allgemeiner Druckfehler
        "PRINT_CANCELLED",    # Vom Benutzer abgebrochen
        "PRINTER_OFFLINE",    # Drucker nicht erreichbar
        "PAPER_JAM",          # Papierstau
        "TONER_EMPTY",        # Toner leer
    ]
    ERROR_RATE = 0.03  # 3% aller Jobs schlagen fehl

    user_weights = [rng.uniform(0.3, 2.5) for _ in users]

    for day in working_days:
        month_factor = MONTH_FACTORS.get(day.month, 1.0)
        for user, weight in zip(users, user_weights):
            n_jobs = max(0, int(rng.gauss(jobs_per_user_day * weight * month_factor, 1.0)))
            for _ in range(n_jobs):
                ts      = _random_time(day, rng)
                pages   = _page_count(rng)
                color   = 1 if rng.random() < 0.30 else 0
                duplex  = 1 if rng.random() < 0.60 else 0
                paper   = rng.choice(PAPER_SIZES)
                printer = rng.choice(printers)
                fname   = _filename_print(rng, ts, user)
                job_id  = _uid()

                jobs_rows.append((
                    job_id, tenant_id, color, duplex, pages, paper,
                    printer["id"], ts, user["id"], fname, session_id,
                ))
                status = rng.choice(ERROR_STATUSES) if rng.random() < ERROR_RATE else "PRINT_OK"
                tracking_rows.append((
                    job_id, tenant_id, pages, color, duplex, ts,
                    printer["id"], status, session_id,
                ))

    return jobs_rows, tracking_rows


def _gen_scan_jobs(
    tenant_id: str, users: list[dict], printers: list[dict],
    working_days: list[datetime], session_id: str, rng: random.Random,
) -> list[tuple]:
    rows: list[tuple] = []
    for day in working_days:
        month_factor = MONTH_FACTORS.get(day.month, 1.0)
        for user in users:
            if rng.random() > (0.33 * month_factor):
                continue
            ts    = _random_time(day, rng)
            pages = rng.randint(1, 20)
            color = 1 if rng.random() < 0.15 else 0
            rows.append((
                _uid(), tenant_id, rng.choice(printers)["id"], user["id"],
                ts, pages, color,
                rng.choice(CAPTURE_WORKFLOWS),
                _filename_scan(rng, ts),
                session_id,
            ))
    return rows


def _gen_copy_jobs(
    tenant_id: str, users: list[dict], printers: list[dict],
    working_days: list[datetime], session_id: str, rng: random.Random,
) -> tuple[list[tuple], list[tuple]]:
    copy_rows: list[tuple]   = []
    detail_rows: list[tuple] = []
    for day in working_days:
        month_factor = MONTH_FACTORS.get(day.month, 1.0)
        for user in users:
            if rng.random() > (0.25 * month_factor):
                continue
            ts      = _random_time(day, rng)
            job_id  = _uid()
            pages   = rng.randint(1, 30)
            color   = 1 if rng.random() < 0.20 else 0
            duplex  = 1 if rng.random() < 0.50 else 0
            paper   = rng.choice(PAPER_SIZES)
            copy_rows.append((
                job_id, tenant_id, rng.choice(printers)["id"],
                user["id"], ts, session_id,
            ))
            detail_rows.append((
                _uid(), job_id, pages, paper, duplex, color, session_id,
            ))
    return copy_rows, detail_rows


# ── Bulk-Insert Helfer (v4.4.0: lokal SQLite statt Azure SQL) ────────────────

def _bulk_insert_local(table: str, columns: list[str], rows: list[tuple]) -> int:
    """Bulk-Insert in die lokale Demo-SQLite-DB."""
    from .local_demo_db import demo_bulk_insert
    return demo_bulk_insert(table, columns, rows)


# ── Öffentliche API ───────────────────────────────────────────────────────────

def generate_demo_dataset(
    tenant_id: str,
    user_count: int = 15,
    printer_count: int = 6,
    queue_count: int = 2,
    months: int = 12,
    languages: Optional[list[str]] = None,
    sites: Optional[list[str]] = None,
    demo_tag: str = "",
    jobs_per_user_day: float = 3.0,
    seed: Optional[int] = None,
    preset: str = "custom",
) -> dict:
    """
    Generiert ein vollständiges Demo-Dataset und schreibt es in die lokale SQLite-DB.
    v4.4.0: Kein Azure SQL Schreibzugriff mehr nötig!
    """
    from .local_demo_db import demo_bulk_insert, demo_execute

    user_count       = max(1, min(200, user_count))
    printer_count    = max(1, min(50, printer_count))
    months           = max(1, min(36, months))
    languages        = [l for l in (languages or ["de"]) if l in NAMES] or ["de"]
    sites            = sites or ["Hauptsitz", "Niederlassung"]
    demo_tag         = demo_tag.strip() or f"DEMO_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    email_domain     = "printix-demo.example"

    rng        = random.Random(seed)
    session_id = f"{demo_tag}_{_uid()[:8]}"
    now        = datetime.now()
    end_dt     = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start_dt   = (now - timedelta(days=months * 30)).replace(
                     hour=0, minute=0, second=0, microsecond=0)

    logger.info("Demo-Generator gestartet (lokal SQLite): session=%s tenant=%s user=%d printer=%d months=%d",
                session_id, tenant_id, user_count, printer_count, months)

    users    = _gen_users(tenant_id, user_count, languages, session_id, rng, email_domain)
    networks = _gen_networks(tenant_id, sites, session_id)
    printers = _gen_printers(tenant_id, printer_count, networks, session_id, rng)
    wdays    = _working_days(start_dt, end_dt)

    logger.info("Werktage im Zeitraum: %d", len(wdays))

    jobs_rows, tracking_rows = _gen_print_jobs(
        tenant_id, users, printers, wdays, jobs_per_user_day, session_id, rng)
    scan_rows = _gen_scan_jobs(tenant_id, users, printers, wdays, session_id, rng)
    copy_rows, copy_detail_rows = _gen_copy_jobs(
        tenant_id, users, printers, wdays, session_id, rng)

    logger.info("Datenmenge: %d Druckjobs | %d Scans | %d Kopien",
                len(jobs_rows), len(scan_rows), len(copy_rows))

    # ── Alle Daten in lokale SQLite schreiben ──────────────────────────────
    _bulk_insert_local(
        "demo_networks",
        ["id", "tenant_id", "name", "demo_session_id"],
        [(n["id"], n["tenant_id"], n["name"], n["demo_session_id"]) for n in networks],
    )
    _bulk_insert_local(
        "demo_users",
        ["id", "tenant_id", "email", "name", "department", "demo_session_id"],
        [(u["id"], u["tenant_id"], u["email"], u["name"], u["department"], u["demo_session_id"])
         for u in users],
    )
    _bulk_insert_local(
        "demo_printers",
        ["id", "tenant_id", "name", "model_name", "vendor_name", "network_id", "location", "demo_session_id"],
        [(p["id"], p["tenant_id"], p["name"], p["model_name"], p["vendor_name"],
          p["network_id"], p["location"], p["demo_session_id"]) for p in printers],
    )
    # datetime → ISO string für SQLite
    jobs_rows_sqlite = [
        (r[0], r[1], r[2], r[3], r[4], r[5], r[6],
         r[7].isoformat() if hasattr(r[7], 'isoformat') else str(r[7]),
         r[8], r[9], r[10])
        for r in jobs_rows
    ]
    _bulk_insert_local(
        "demo_jobs",
        ["id", "tenant_id", "color", "duplex", "page_count", "paper_size",
         "printer_id", "submit_time", "tenant_user_id", "filename", "demo_session_id"],
        jobs_rows_sqlite,
    )
    tracking_rows_sqlite = [
        (r[0], r[1], r[2], r[3], r[4],
         r[5].isoformat() if hasattr(r[5], 'isoformat') else str(r[5]),
         r[6], r[7], r[8])
        for r in tracking_rows
    ]
    _bulk_insert_local(
        "demo_tracking_data",
        ["job_id", "tenant_id", "page_count", "color", "duplex",
         "print_time", "printer_id", "print_job_status", "demo_session_id"],
        tracking_rows_sqlite,
    )
    if scan_rows:
        scan_rows_sqlite = [
            (r[0], r[1], r[2], r[3],
             r[4].isoformat() if hasattr(r[4], 'isoformat') else str(r[4]),
             r[5], r[6], r[7], r[8], r[9])
            for r in scan_rows
        ]
        _bulk_insert_local(
            "demo_jobs_scan",
            ["id", "tenant_id", "printer_id", "tenant_user_id", "scan_time",
             "page_count", "color", "workflow_name", "filename", "demo_session_id"],
            scan_rows_sqlite,
        )
    if copy_rows:
        copy_rows_sqlite = [
            (r[0], r[1], r[2], r[3],
             r[4].isoformat() if hasattr(r[4], 'isoformat') else str(r[4]),
             r[5])
            for r in copy_rows
        ]
        _bulk_insert_local(
            "demo_jobs_copy",
            ["id", "tenant_id", "printer_id", "tenant_user_id", "copy_time", "demo_session_id"],
            copy_rows_sqlite,
        )
        _bulk_insert_local(
            "demo_jobs_copy_details",
            ["id", "job_id", "page_count", "paper_size", "duplex", "color", "demo_session_id"],
            copy_detail_rows,
        )

    params_json = json.dumps({
        "user_count": user_count, "printer_count": printer_count,
        "queue_count": queue_count,
        "months": months, "languages": languages, "sites": sites,
        "jobs_per_user_day": jobs_per_user_day, "seed": seed,
        "start": start_dt.isoformat(), "end": end_dt.isoformat(),
        "preset": preset,
    })
    demo_execute(
        "INSERT INTO demo_sessions "
        "(session_id,tenant_id,demo_tag,created_at,params_json,status,"
        "user_count,printer_count,network_count,print_job_count,scan_job_count,copy_job_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, tenant_id, demo_tag, now.isoformat(), params_json, "active",
         len(users), len(printers), len(networks),
         len(jobs_rows), len(scan_rows), len(copy_rows)),
    )

    logger.info("Demo-Datensatz fertig (lokal SQLite): session=%s", session_id)

    return {
        "session_id":    session_id,
        "demo_tag":      demo_tag,
        "period":        f"{start_dt.date()} – {end_dt.date()}",
        "working_days":  len(wdays),
        "users":         len(users),
        "printers":      len(printers),
        "networks":      len(networks),
        "print_jobs":    len(jobs_rows),
        "scan_jobs":     len(scan_rows),
        "copy_jobs":     len(copy_rows),
        "errors":        [],
        "status":        "ok",
        "rollback_cmd":  f'printix_demo_rollback(demo_tag="{demo_tag}")',
    }


def rollback_demo(tenant_id: str, demo_tag: str) -> dict:
    """
    Löscht alle Demo-Daten mit dem angegebenen demo_tag.
    v4.4.0: Arbeitet auf lokaler SQLite statt Azure SQL.
    """
    from .local_demo_db import demo_query, demo_execute

    sessions = demo_query(
        "SELECT session_id FROM demo_sessions WHERE tenant_id=? AND demo_tag=?",
        (tenant_id, demo_tag),
    )
    session_ids = [s["session_id"] for s in sessions]
    if not session_ids:
        return {"deleted": {}, "sessions_found": 0, "message": f"Keine Sessions für Tag '{demo_tag}' gefunden."}

    deleted: dict[str, int] = {}
    tables_ordered = [
        "demo_jobs_copy_details",
        "demo_jobs_copy",
        "demo_jobs_scan",
        "demo_tracking_data",
        "demo_jobs",
        "demo_printers",
        "demo_users",
        "demo_networks",
        "demo_sessions",
    ]
    for sid in session_ids:
        for tbl in tables_ordered:
            col = "session_id" if tbl == "demo_sessions" else "demo_session_id"
            try:
                n = demo_execute(f"DELETE FROM {tbl} WHERE {col}=?", (sid,))
                deleted[tbl] = deleted.get(tbl, 0) + n
            except Exception as e:
                logger.warning("Rollback-Fehler %s session %s: %s", tbl, sid, e)

    total = sum(deleted.values())
    logger.info("Rollback abgeschlossen: %d Zeilen gelöscht für tag=%s", total, demo_tag)
    return {
        "deleted":        deleted,
        "sessions_found": len(session_ids),
        "total_deleted":  total,
        "demo_tag":       demo_tag,
        "status":         "ok",
    }


def rollback_demo_all(tenant_id: str) -> dict:
    """
    Löscht ALLE Demo-Daten für den Tenant (alle Tags/Sessions).
    v4.4.0: Arbeitet auf lokaler SQLite statt Azure SQL.
    """
    from .local_demo_db import rollback_all_demos
    result = rollback_all_demos(tenant_id)
    total = sum(result.get("deleted", {}).values())
    logger.info("Rollback-All (lokal): %d Zeilen gelöscht für tenant_id=%s", total, tenant_id)
    return {
        "deleted":        result.get("deleted", {}),
        "sessions_found": 0,
        "total_deleted":  total,
        "status":         "ok",
    }


def get_demo_status(tenant_id: str) -> dict:
    """
    Gibt eine Übersicht aller Demo-Sessions für den Tenant zurück.
    v4.4.0: Liest aus lokaler SQLite statt Azure SQL.
    """
    from .local_demo_db import get_demo_sessions

    sessions = get_demo_sessions(tenant_id)
    # Maximal 20 Sessions zurückgeben (bereits nach created_at DESC sortiert)
    sessions = sessions[:20]

    total_jobs = sum((s.get("print_job_count") or 0) for s in sessions)
    total_rows = sum(
        (s.get("print_job_count") or 0) + (s.get("scan_job_count") or 0) +
        (s.get("copy_job_count") or 0) for s in sessions
    )
    return {
        "sessions":          sessions,
        "session_count":     len(sessions),
        "total_print_jobs":  total_jobs,
        "total_demo_rows":   total_rows,
        "hint":              "Rollback: printix_demo_rollback(demo_tag='TAG')",
    }
