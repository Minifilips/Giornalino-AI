#!/usr/bin/env python3
"""Giornalino AI: ogni mattina manda per mail i nuovi video dei canali YouTube scelti,
con un recap in italiano (se c'e' ANTHROPIC_API_KEY). Solo libreria standard."""

import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- Configurazione -------------------------------------------------------
# (nome, @handle oppure channel id che inizia per "UC"). Modifica pure la lista.
CANALI = [
    ("Two Minute Papers", "@TwoMinutePapers"),
    ("AI Explained", "@aiexplained-official"),
    ("Matt Wolfe", "@mreflow"),
    ("Matthew Berman", "@matthew_berman"),
    ("Wes Roth", "@WesRoth"),
    ("Fireship", "@Fireship"),
    ("Andrej Karpathy", "@AndrejKarpathy"),
    ("Yannic Kilcher", "@YannicKilcher"),
    ("Anthropic", "@anthropic-ai"),
]
ORE_FINESTRA = 30          # video pubblicati nelle ultime N ore
MODELLO = "claude-haiku-4-5-20251001"
MAX_DESC = 1500            # caratteri di descrizione passati al modello / mostrati in mail
UA = {"User-Agent": "Mozilla/5.0 (compatible; GiornalinoAI/1.0)"}
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "m": "http://search.yahoo.com/mrss/",
    "y": "http://www.youtube.com/xml/schemas/2015",
}


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def channel_id(ref):
    if re.fullmatch(r"UC[\w-]{22}", ref):
        return ref
    page = http_get("https://www.youtube.com/" + ref.lstrip("/"))
    m = re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', page) or re.search(
        r'channel_id=(UC[\w-]{22})', page
    )
    if not m:
        raise RuntimeError("channel id non trovato per " + ref)
    return m.group(1)


def video_recenti(ref, da):
    cid = channel_id(ref)
    root = ET.fromstring(http_get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid))
    out = []
    for e in root.findall("a:entry", NS):
        pub = datetime.fromisoformat(e.findtext("a:published", namespaces=NS).replace("Z", "+00:00"))
        if pub < da:
            continue
        link = e.find("a:link", NS).get("href")
        # niente Shorts
        if "/shorts/" in link:
            continue
        out.append({
            "titolo": e.findtext("a:title", namespaces=NS),
            "link": link,
            "data": pub,
            "descrizione": (e.findtext("m:group/m:description", namespaces=NS) or "").strip(),
        })
    return out


def recap_ai(video, api_key):
    prompt = (
        "Scrivi in italiano un recap di 2-3 frasi di questo video YouTube sull'AI, "
        "basandoti solo su titolo e descrizione. Vai dritto al punto, niente premesse, "
        "niente promozioni/sponsor/link. Se la descrizione non basta, dillo in poche parole.\n\n"
        f"Titolo: {video['titolo']}\nDescrizione:\n{video['descrizione'][:MAX_DESC]}"
    )
    body = json.dumps({
        "model": MODELLO,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text").strip()


def costruisci_mail(per_canale, errori, ha_ai):
    oggi = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y")
    n = 0
    righe = []
    testo = []
    for nome, videos in per_canale:
        if not videos:
            continue
        righe.append(f"<h3 style='margin:18px 0 6px'>{html.escape(nome)}</h3>")
        testo.append(f"\n== {nome} ==")
        for v in videos:
            n += 1
            riassunto = v["recap"] or (v["descrizione"][:300] + ("…" if len(v["descrizione"]) > 300 else ""))
            righe.append(
                f"<p style='margin:0 0 12px'><b>{n}. <a href='{html.escape(v['link'])}'>"
                f"{html.escape(v['titolo'])}</a></b><br>{html.escape(riassunto).replace(chr(10), '<br>')}</p>"
            )
            testo.append(f"{n}. {v['titolo']}\n{v['link']}\n{riassunto}\n")
    if n == 0:
        righe.append("<p>Nessun nuovo video oggi.</p>")
        testo.append("Nessun nuovo video oggi.")
    righe.append("<p style='color:#666'>Rispondi con i numeri dei video da mandare a NotebookLM.</p>")
    testo.append("Rispondi con i numeri dei video da mandare a NotebookLM.")
    if not ha_ai:
        righe.append("<p style='color:#999'>(Recap AI non attivo: manca ANTHROPIC_API_KEY.)</p>")
    if errori:
        elenco = ", ".join(errori)
        righe.append(f"<p style='color:#b00'>Canali in errore: {html.escape(elenco)}</p>")
        testo.append(f"Canali in errore: {elenco}")
    oggetto = f"Giornalino AI {oggi}: {n} nuovi video"
    return oggetto, "<html><body style='font-family:sans-serif'>" + "".join(righe) + "</body></html>", "\n".join(testo)


def invia(oggetto, corpo_html, corpo_testo, utente, password):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = oggetto
    msg["From"] = utente
    msg["To"] = utente
    msg.attach(MIMEText(corpo_testo, "plain", "utf-8"))
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(utente, password)
        s.send_message(msg)


def main():
    utente = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    da = datetime.now(timezone.utc) - timedelta(hours=ORE_FINESTRA)

    per_canale, errori = [], []
    for nome, ref in CANALI:
        try:
            videos = video_recenti(ref, da)
        except Exception as e:  # un canale rotto non deve fermare gli altri
            print(f"[errore] {nome}: {e}", file=sys.stderr)
            errori.append(nome)
            continue
        for v in videos:
            v["recap"] = ""
            if api_key:
                try:
                    v["recap"] = recap_ai(v, api_key)
                except Exception as e:
                    print(f"[recap errore] {v['titolo']}: {e}", file=sys.stderr)
        per_canale.append((nome, sorted(videos, key=lambda v: v["data"])))

    oggetto, h, t = costruisci_mail(per_canale, errori, bool(api_key))
    invia(oggetto, h, t, utente, password)
    print("Mail inviata:", oggetto)


if __name__ == "__main__":
    main()
