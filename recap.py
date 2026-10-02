#!/usr/bin/env python3
"""Recap mattutino dei nuovi video YouTube dei canali AI. Pensato per GitHub Actions."""
import json, os, re, smtplib, ssl, sys
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

# Handle YouTube (se hai il channel ID mettilo come secondo valore, altrimenti None)
CANALI = [
    ("Simone Rizzo", "simone_rizzo98", "UCbMlkb79E12CwveGAtdFj-A"),
    ("Fireship", "Fireship", "UCsBjURrPoezykLs9EqgamOA"),
    ("AI Explained", "AIExplained-official", None),
    ("Matthew Berman", "matthew_berman", None),
    ("Matt Wolfe", "mreflow", None),
    ("Two Minute Papers", "TwoMinutePapers", None),
]
ORE = int(os.environ.get("ORE_FINESTRA", "36"))
DEST = os.environ.get("DEST_EMAIL", "corraof05@gmail.com")
MITTENTE = os.environ["GMAIL_USER"]
PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
# Modelli Gemini da provare in ordine (il primo che risponde vince)
GEMINI_MODELLI = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash-lite",
                              "gemini-2.5-flash", "gemini-3.1-flash-lite"] if m]
MODELLO = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
NS = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/",
      "y": "http://www.youtube.com/xml/schemas/2015"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def channel_id(handle):
    html = get(f"https://www.youtube.com/@{handle}")
    m = re.search(r'"channelId":"(UC[\w-]{22})"', html) or re.search(r'channel_id=(UC[\w-]{22})', html)
    return m.group(1) if m else None


def nuovi_video(cid):
    xml = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    root = ET.fromstring(xml)
    limite = datetime.now(timezone.utc) - timedelta(hours=ORE)
    out = []
    for e in root.findall("a:entry", NS):
        pub = datetime.fromisoformat(e.find("a:published", NS).text.replace("Z", "+00:00"))
        if pub < limite:
            continue
        if "/shorts/" in e.find("a:link", NS).attrib["href"]:
            continue
        desc = e.find("m:group/m:description", NS)
        out.append({
            "titolo": e.find("a:title", NS).text,
            "link": e.find("a:link", NS).attrib["href"],
            "desc": (desc.text or "")[:1500] if desc is not None else "",
            "pub": pub,
        })
    return out


PROMPT = ("Scrivi in italiano, in 2-3 righe, senza markdown, gli argomenti di questo video YouTube "
          "usando SOLO titolo e descrizione. Ignora sponsor, link e promozioni. Non inventare dettagli.\n\n"
          "Titolo: {titolo}\nDescrizione: {desc}")


def recap_gemini(v):
    corpo = json.dumps({"contents": [{"parts": [{"text": PROMPT.format(**v)}]}],
                        "generationConfig": {"maxOutputTokens": 300}}).encode()
    ultimo = None
    for modello in GEMINI_MODELLI:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
        req = urllib.request.Request(url, data=corpo, headers={
            "Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            testo = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
            if testo:
                return testo + " (recap basato sulla descrizione)"
        except urllib.error.HTTPError as ex:
            ultimo = f"{modello}: HTTP {ex.code} {ex.read()[:200].decode('utf-8', 'replace')}"
        except Exception as ex:
            ultimo = f"{modello}: {ex}"
    raise RuntimeError(ultimo or "risposta vuota")


def recap_claude(v):
    import anthropic
    c = anthropic.Anthropic(api_key=API_KEY)
    r = c.messages.create(model=MODELLO, max_tokens=300,
                          messages=[{"role": "user", "content": PROMPT.format(**v)}])
    return r.content[0].text.strip() + " (recap basato sulla descrizione)"


def recap(v):
    fallback = "Recap non generato. Descrizione: " + v["desc"][:200]
    try:
        if GEMINI_KEY:
            return recap_gemini(v)
        if API_KEY:
            return recap_claude(v)
        return "Recap non generato (manca GEMINI_API_KEY). Descrizione: " + v["desc"][:200]
    except Exception as ex:  # un recap fallito non deve bloccare la mail
        print(f"[recap errore] {v['titolo']}: {ex}", file=sys.stderr)
        return fallback + f" [errore recap: {str(ex)[:150]}]"


def main():
    oggi = datetime.now().strftime("%d/%m/%Y")
    righe, n, errori = [], 0, []
    for nome, handle, cid in CANALI:
        try:
            cid = cid or channel_id(handle)
            if not cid:
                raise RuntimeError("channel ID non trovato")
            vids = nuovi_video(cid)
        except Exception as ex:
            errori.append(f"{nome}: errore ({ex})")
            continue
        if not vids:
            righe.append(f"{nome}: nessun nuovo video\n")
        for v in vids:
            n += 1
            righe.append(f"{n}. {nome} - {v['titolo']}\n{recap(v)}\n{v['link']}\n")
    corpo = "\n".join(righe)
    if errori:
        corpo += "\nProblemi oggi:\n" + "\n".join(errori) + "\n"
    if n:
        corpo += "\nRispondi con i numeri che vuoi ingerire in NotebookLM e il formato (podcast, presentazione, mappa mentale, ecc.).\n"
    elif not errori:
        corpo = "Nessun nuovo video oggi dai canali monitorati."
    msg = MIMEText(corpo, "plain", "utf-8")
    msg["Subject"] = f"Recap AI {oggi}"
    msg["From"] = MITTENTE
    msg["To"] = DEST
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(MITTENTE, PASSWORD)
        s.send_message(msg)
    print(corpo)
    if errori and n == 0 and len(errori) == len(CANALI):
        sys.exit(1)


if __name__ == "__main__":
    main()
