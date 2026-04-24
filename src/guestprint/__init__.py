"""Guest-Print (v7.1.0).

Mail-basierter Secure-Print-Flow fuer Gaeste: ein ueberwachtes Outlook-/
Exchange-Postfach empfaengt Anhaenge von gelisteten Gast-Absendern, die
dann automatisch via Printix Secure Print gedruckt werden (change_job_owner
auf den Gast). Gast-User werden in Printix als GUEST_USER mit
expirationTimestamp angelegt.

Die Entra-App-Credentials fuer den Graph-Zugriff liegen separat von der
SSO-App (separates Client-ID/Secret), damit der Kunde pro Use-Case eine
eigene App registrieren kann — z.B. eine zweite App mit minimal-scope
Mail.ReadWrite nur fuer das Guest-Postfach.
"""
