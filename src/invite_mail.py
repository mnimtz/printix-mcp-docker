"""
Localized invitation email rendering for the Printix Management Console.
"""

from __future__ import annotations

from typing import Dict, List


INVITE_CONTENT: Dict[str, Dict[str, object]] = {
    "de": {
        "subject": "Einladung zur Printix Management Console",
        "headline": "Du wurdest zur Printix Management Console eingeladen",
        "intro": "Ein Administrator hat für dich einen Benutzerzugang eingerichtet.",
        "cta": "Anmelden",
        "credentials_title": "Deine Zugangsdaten",
        "username_label": "Benutzername",
        "password_label": "Temporäres Passwort",
        "login_label": "Login",
        "security_note": "Bitte melde dich an und ändere dein Passwort direkt beim ersten Login.",
        "highlights_title": "Top 5 Highlights",
        "highlights": [
            "Drucker, Queues und Workstations zentral im Browser verwalten",
            "Benutzer und Karten übersichtlich pflegen",
            "Karten & Codes für fortgeschrittene Kartenwerte und Profile nutzen",
            "Reports, Demo-Daten und Logs direkt in der Oberfläche abrufen",
            "Clientless- und Zero-Trust-Pakete vorbereitet herunterladen",
        ],
    },
    "en": {
        "subject": "Invitation to the Printix Management Console",
        "headline": "You have been invited to the Printix Management Console",
        "intro": "An administrator created an account for you.",
        "cta": "Sign in",
        "credentials_title": "Your access details",
        "username_label": "Username",
        "password_label": "Temporary password",
        "login_label": "Login",
        "security_note": "Please sign in and change your password immediately on first login.",
        "highlights_title": "Top 5 highlights",
        "highlights": [
            "Manage printers, queues and workstations in one browser interface",
            "Maintain users and cards in a structured workflow",
            "Use Cards & Codes for advanced card values and profiles",
            "Open reports, demo data and logs directly in the console",
            "Prepare clientless and zero-trust packages for download",
        ],
    },
    "fr": {
        "subject": "Invitation a la Printix Management Console",
        "headline": "Vous avez ete invite a la Printix Management Console",
        "intro": "Un administrateur a cree un compte pour vous.",
        "cta": "Se connecter",
        "credentials_title": "Vos acces",
        "username_label": "Nom d'utilisateur",
        "password_label": "Mot de passe temporaire",
        "login_label": "Connexion",
        "security_note": "Connectez-vous puis changez votre mot de passe des la premiere connexion.",
        "highlights_title": "Top 5 des points forts",
        "highlights": [
            "Gerer imprimantes, files et postes depuis une seule interface web",
            "Maintenir utilisateurs et cartes avec plus de clarte",
            "Utiliser Cartes & Codes pour les valeurs et profils avances",
            "Consulter rapports, donnees de demo et journaux directement dans la console",
            "Preparer des paquets clientless et zero trust pour le telechargement",
        ],
    },
    "it": {
        "subject": "Invito alla Printix Management Console",
        "headline": "Sei stato invitato alla Printix Management Console",
        "intro": "Un amministratore ha creato un account per te.",
        "cta": "Accedi",
        "credentials_title": "Le tue credenziali",
        "username_label": "Nome utente",
        "password_label": "Password temporanea",
        "login_label": "Accesso",
        "security_note": "Accedi e cambia subito la password al primo login.",
        "highlights_title": "Top 5 funzioni principali",
        "highlights": [
            "Gestire stampanti, code e workstation da un'unica interfaccia web",
            "Mantenere utenti e carte in un flusso ordinato",
            "Usare Carte & Codici per valori carta e profili avanzati",
            "Aprire report, dati demo e log direttamente nella console",
            "Preparare pacchetti clientless e zero trust per il download",
        ],
    },
    "es": {
        "subject": "Invitacion a la Printix Management Console",
        "headline": "Has sido invitado a la Printix Management Console",
        "intro": "Un administrador ha creado una cuenta para ti.",
        "cta": "Iniciar sesion",
        "credentials_title": "Tus datos de acceso",
        "username_label": "Nombre de usuario",
        "password_label": "Contrasena temporal",
        "login_label": "Acceso",
        "security_note": "Inicia sesion y cambia tu contrasena inmediatamente en el primer acceso.",
        "highlights_title": "Top 5 funciones destacadas",
        "highlights": [
            "Gestionar impresoras, colas y workstations desde una sola interfaz web",
            "Mantener usuarios y tarjetas de forma mas clara",
            "Usar Tarjetas y Codigos para valores y perfiles avanzados",
            "Abrir reportes, datos demo y logs directamente en la consola",
            "Preparar paquetes clientless y zero trust para descarga",
        ],
    },
    "nl": {
        "subject": "Uitnodiging voor de Printix Management Console",
        "headline": "Je bent uitgenodigd voor de Printix Management Console",
        "intro": "Een beheerder heeft een account voor je aangemaakt.",
        "cta": "Aanmelden",
        "credentials_title": "Jouw toegangsgegevens",
        "username_label": "Gebruikersnaam",
        "password_label": "Tijdelijk wachtwoord",
        "login_label": "Login",
        "security_note": "Meld je aan en wijzig je wachtwoord direct bij de eerste login.",
        "highlights_title": "Top 5 highlights",
        "highlights": [
            "Printers, wachtrijen en workstations beheren vanuit een webinterface",
            "Gebruikers en kaarten overzichtelijk onderhouden",
            "Kaarten & Codes gebruiken voor geavanceerde kaartwaarden en profielen",
            "Rapporten, demodata en logs direct in de console openen",
            "Clientless- en zero-trust-pakketten voorbereiden voor download",
        ],
    },
    "no": {
        "subject": "Invitasjon til Printix Management Console",
        "headline": "Du er invitert til Printix Management Console",
        "intro": "En administrator har opprettet en konto for deg.",
        "cta": "Logg inn",
        "credentials_title": "Dine tilgangsdetaljer",
        "username_label": "Brukernavn",
        "password_label": "Midlertidig passord",
        "login_label": "Innlogging",
        "security_note": "Logg inn og bytt passord med en gang ved forste innlogging.",
        "highlights_title": "Topp 5 hoydepunkter",
        "highlights": [
            "Administrer skrivere, koer og workstations i ett nettgrensesnitt",
            "Vedlikehold brukere og kort pa en ryddig mate",
            "Bruk Kort og koder for avanserte kortverdier og profiler",
            "Apne rapporter, demodata og logger direkte i konsollen",
            "Forbered clientless- og zero-trust-pakker for nedlasting",
        ],
    },
    "sv": {
        "subject": "Inbjudan till Printix Management Console",
        "headline": "Du har blivit inbjuden till Printix Management Console",
        "intro": "En administratör har skapat ett konto för dig.",
        "cta": "Logga in",
        "credentials_title": "Dina inloggningsuppgifter",
        "username_label": "Användarnamn",
        "password_label": "Tillfalligt losenord",
        "login_label": "Inloggning",
        "security_note": "Logga in och byt losenord direkt vid forsta inloggningen.",
        "highlights_title": "Topp 5 hojdpunkter",
        "highlights": [
            "Hantera skrivare, koer och workstations i ett webbgranssnitt",
            "Underhall anvandare och kort pa ett tydligare satt",
            "Anvand Kort och koder for avancerade kortvarden och profiler",
            "Oppna rapporter, demodata och loggar direkt i konsolen",
            "Forbered clientless- och zero-trust-paket for nedladdning",
        ],
    },
}


def _c(lang: str) -> Dict[str, object]:
    aliases = {
        "cockney": "en",
        "us_south": "en",
        "bar": "de",
        "hessisch": "de",
        "oesterreichisch": "de",
        "schwiizerdütsch": "de",
    }
    normalized = aliases.get(lang, lang)
    return INVITE_CONTENT.get(normalized, INVITE_CONTENT["en"])


def render_invitation_email(
    *,
    lang: str,
    full_name: str,
    username: str,
    password: str,
    login_url: str,
) -> tuple[str, str]:
    content = _c(lang)
    recipient = full_name.strip() or username
    highlights = "\n".join(
        f"<li style=\"margin:0 0 6px;\">{item}</li>" for item in content["highlights"]  # type: ignore[index]
    )
    subject = str(content["subject"])
    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,Helvetica,sans-serif;background:#f4f7fb;margin:0;padding:24px;color:#172033;">
  <div style="max-width:720px;margin:0 auto;background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 18px 44px rgba(15,23,42,.10);">
    <div style="padding:28px 30px;background:linear-gradient(135deg,#173a63 0%,#0b4f6c 100%);color:#ffffff;">
      <div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;opacity:.8;">Printix Management Console</div>
      <h1 style="margin:10px 0 0;font-size:28px;line-height:1.1;">{content["headline"]}</h1>
      <p style="margin:14px 0 0;font-size:15px;line-height:1.6;opacity:.92;">{content["intro"]}</p>
    </div>
    <div style="padding:28px 30px;">
      <p style="margin:0 0 18px;font-size:15px;line-height:1.6;">{recipient},</p>
      <div style="background:#f8fbff;border:1px solid #dbeafe;border-radius:16px;padding:18px 20px;">
        <div style="font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#49627f;margin-bottom:12px;">{content["credentials_title"]}</div>
        <table style="width:100%;border-collapse:collapse;font-size:15px;">
          <tr><td style="padding:6px 0;color:#5b6b80;">{content["username_label"]}</td><td style="padding:6px 0;font-weight:700;">{username}</td></tr>
          <tr><td style="padding:6px 0;color:#5b6b80;">{content["password_label"]}</td><td style="padding:6px 0;font-weight:700;">{password}</td></tr>
          <tr><td style="padding:6px 0;color:#5b6b80;">{content["login_label"]}</td><td style="padding:6px 0;"><a href="{login_url}" style="color:#0f5bd8;text-decoration:none;font-weight:700;">{login_url}</a></td></tr>
        </table>
      </div>
      <p style="margin:18px 0 0;color:#9a3412;background:#fff7ed;border:1px solid #fed7aa;border-radius:14px;padding:14px 16px;font-size:14px;line-height:1.6;">{content["security_note"]}</p>
      <div style="margin-top:26px;">
        <div style="font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#49627f;margin-bottom:12px;">{content["highlights_title"]}</div>
        <ol style="padding-left:20px;margin:0;line-height:1.65;">{highlights}</ol>
      </div>
      <div style="margin-top:28px;">
        <a href="{login_url}" style="display:inline-block;background:#0f5bd8;color:#ffffff;text-decoration:none;padding:13px 20px;border-radius:12px;font-weight:700;">{content["cta"]}</a>
      </div>
    </div>
  </div>
</body>
</html>"""
    return subject, html
