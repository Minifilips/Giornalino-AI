#!/usr/bin/env python3
"""Il Giornalino AI: ogni mattina un PDF "giornale" con i nuovi video YouTube dei canali AI
e le notizie dei siti AI, allegato a una mail. Pensato per GitHub Actions."""
import base64, html, json, os, re, smtplib, ssl, sys, time
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# Canali YouTube: (nome, handle, channel ID oppure None)
CANALI = [
    ("Simone Rizzo", "simone_rizzo98", "UCbMlkb79E12CwveGAtdFj-A"),
    ("Fireship", "Fireship", "UCsBjURrPoezykLs9EqgamOA"),
    ("AI Explained", "AIExplained-official", None),
    ("Matthew Berman", "matthew_berman", None),
    ("Matt Wolfe", "mreflow", None),
    ("Two Minute Papers", "TwoMinutePapers", None),
]
# Siti con feed RSS/Atom: (nome, url del feed). Se un feed non risponde finisce in "Problemi oggi".
SITI = [
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
]
ARTICOLI_PER_SITO = 2
ARTICOLI_MAX = 10

ORE = int(os.environ.get("ORE_FINESTRA", "26"))
DEST = os.environ.get("DEST_EMAIL", "corraof05@gmail.com")
MITTENTE = os.environ["GMAIL_USER"]
PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
MODELLO = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
# Modelli Gemini da provare in ordine (il primo che risponde vince)
GEMINI_MODELLI = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash-lite",
                              "gemini-2.5-flash", "gemini-3.1-flash-lite"] if m]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
NS = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/",
      "y": "http://www.youtube.com/xml/schemas/2015"}
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]


# ---------------------------------------------------------------- rete

def get_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def get(url):
    return get_bytes(url).decode("utf-8", "replace")


def immagine_data_uri(url):
    """Scarica un'immagine e la incorpora nella pagina; None se non riesce."""
    if not url:
        return None
    try:
        dati = get_bytes(url, timeout=20)
        if not dati or len(dati) > 3_000_000:
            return None
        tipo = "image/png" if dati[:4] == b"\x89PNG" else "image/webp" if dati[8:12] == b"WEBP" else "image/jpeg"
        return f"data:{tipo};base64," + base64.b64encode(dati).decode()
    except Exception as ex:
        print(f"[immagine] {url}: {ex}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- testo

def pulisci_html(raw):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def descrizione_breve(testo, n=220):
    """Fallback senza AI: toglie link e hashtag e tiene l'inizio."""
    t = re.sub(r"https?://\S+", "", testo or "")
    t = re.sub(r"#\w+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n] + ("…" if len(t) > n else "")


def data_italiana(d):
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]} {d.year}"


# ---------------------------------------------------------------- YouTube

def channel_id(handle):
    pagina = get(f"https://www.youtube.com/@{handle}")
    m = re.search(r'"channelId":"(UC[\w-]{22})"', pagina) or re.search(r'channel_id=(UC[\w-]{22})', pagina)
    return m.group(1) if m else None


def nuovi_video(cid, limite):
    root = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
    out = []
    for e in root.findall("a:entry", NS):
        pub = datetime.fromisoformat(e.find("a:published", NS).text.replace("Z", "+00:00"))
        link = e.find("a:link", NS).attrib["href"]
        if pub < limite or "/shorts/" in link:
            continue
        desc = e.find("m:group/m:description", NS)
        vid = e.findtext("y:videoId", default="", namespaces=NS)
        out.append({
            "titolo": e.find("a:title", NS).text,
            "link": link,
            "desc": (desc.text or "")[:1500] if desc is not None else "",
            "pub": pub,
            "img_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None,
        })
    return out


# ---------------------------------------------------------------- feed dei siti

def _nome(el):
    return el.tag.split("}")[-1]


def _figli(el, nome):
    return [c for c in el if _nome(c) == nome]


def _testo(el, nome):
    for c in _figli(el, nome):
        if c.text and c.text.strip():
            return c.text.strip()
    return ""


def _data(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _immagine(item, grezzo):
    for c in item.iter():
        n = _nome(c)
        if n in ("content", "thumbnail") and c.get("url") and (
                n == "thumbnail" or "image" in (c.get("type") or "image") or c.get("medium") == "image"):
            return c.get("url")
        if n == "enclosure" and "image" in (c.get("type") or "") and c.get("url"):
            return c.get("url")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', grezzo or "")
    return html.unescape(m.group(1)) if m else None


def articoli_feed(url, limite):
    root = ET.fromstring(get_bytes(url))
    voci = [e for e in root.iter() if _nome(e) in ("item", "entry")]
    out = []
    for e in voci:
        pub = _data(_testo(e, "pubDate") or _testo(e, "published") or _testo(e, "updated") or _testo(e, "date"))
        if not pub or pub < limite:
            continue
        link = _testo(e, "link")
        if not link:
            for c in _figli(e, "link"):
                if c.get("href") and c.get("rel", "alternate") == "alternate":
                    link = c.get("href")
                    break
        grezzo = _testo(e, "encoded") or _testo(e, "content") or _testo(e, "description") or _testo(e, "summary")
        out.append({
            "titolo": pulisci_html(_testo(e, "title")),
            "link": link,
            "desc": pulisci_html(_testo(e, "description") or _testo(e, "summary") or grezzo)[:1500],
            "pub": pub,
            "img_url": _immagine(e, grezzo),
        })
    out.sort(key=lambda a: a["pub"], reverse=True)
    return out[:ARTICOLI_PER_SITO]


# ---------------------------------------------------------------- recap AI

PROMPT_VIDEO = ("Scrivi in italiano, in 2-3 righe, senza markdown, gli argomenti di questo video YouTube "
                "usando SOLO titolo e descrizione. Ignora sponsor, link e promozioni. Non inventare dettagli.\n\n"
                "Titolo: {titolo}\nDescrizione: {desc}")
PROMPT_ARTICOLO = ("Scrivi in italiano, in 2 righe, senza markdown, di cosa parla questa notizia sull'AI "
                   "usando SOLO titolo e testo. Non inventare dettagli.\n\n"
                   "Titolo: {titolo}\nTesto: {desc}")


def recap_gemini(prompt):
    corpo = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"maxOutputTokens": 300}}).encode()
    ultimo = None
    for modello in GEMINI_MODELLI:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
        for tentativo in range(2):
            req = urllib.request.Request(url, data=corpo, headers={
                "Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.load(r)
                testo = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
                if testo:
                    time.sleep(4.5)  # il piano gratuito concede ~15 richieste al minuto
                    return testo
                ultimo = f"{modello}: risposta vuota"
                break
            except urllib.error.HTTPError as ex:
                ultimo = f"{modello}: HTTP {ex.code} {ex.read()[:150].decode('utf-8', 'replace')}"
                if ex.code == 429 and tentativo == 0:
                    time.sleep(25)
                    continue
                break
            except Exception as ex:
                ultimo = f"{modello}: {ex}"
                break
    raise RuntimeError(ultimo or "risposta vuota")


def recap_claude(prompt):
    import anthropic
    c = anthropic.Anthropic(api_key=API_KEY)
    r = c.messages.create(model=MODELLO, max_tokens=300, messages=[{"role": "user", "content": prompt}])
    return r.content[0].text.strip()


def recap(v, prompt):
    base = descrizione_breve(v["desc"])
    try:
        if GEMINI_KEY:
            return recap_gemini(prompt.format(**v))
        if API_KEY:
            return recap_claude(prompt.format(**v))
        return base
    except Exception as ex:  # un recap fallito non deve bloccare il giornale
        print(f"[recap errore] {v['titolo']}: {ex}", file=sys.stderr)
        return base + f" [errore recap: {str(ex)[:120]}]"


# ---------------------------------------------------------------- giornale (HTML -> PDF)

CSS = """
@page { size: A4; margin: 14mm 13mm 16mm 13mm;
        @bottom-center { content: "Il Giornalino AI · pagina " counter(page); font: 8pt 'DejaVu Sans', sans-serif; color: #777; } }
body { font-family: Georgia, 'DejaVu Serif', serif; color: #1a1a1a; font-size: 10pt; line-height: 1.35; }
.testata { text-align: center; border-top: 3px double #1a1a1a; border-bottom: 3px double #1a1a1a; padding: 6px 0 8px; margin-bottom: 12px; }
.testata .sopra, .testata .sotto { font: 8.5pt 'DejaVu Sans', sans-serif; letter-spacing: 1px; text-transform: uppercase; color: #444; }
.testata h1 { font-size: 40pt; margin: 4px 0; font-weight: bold; letter-spacing: -1px; }
.apertura { border-bottom: 1px solid #1a1a1a; padding-bottom: 12px; margin-bottom: 12px; }
.apertura img { width: 100%; height: 78mm; object-fit: cover; }
.apertura h2 { font-size: 24pt; line-height: 1.12; margin: 8px 0 6px; }
.apertura p.recap { font-size: 12pt; }
.sez { font: bold 10pt 'DejaVu Sans', sans-serif; text-transform: uppercase; letter-spacing: 2px; border-bottom: 2px solid #1a1a1a;
       margin: 14px 0 8px; padding-bottom: 3px; break-after: avoid; }
.colonne { column-count: 2; column-gap: 8mm; column-rule: 1px solid #ccc; }
.pezzo { break-inside: avoid; margin-bottom: 11px; padding-bottom: 9px; border-bottom: 1px solid #ddd; }
.pezzo img { width: 100%; height: 32mm; object-fit: cover; margin-bottom: 4px; }
.pezzo h2 { font-size: 12.5pt; line-height: 1.18; margin: 2px 0 4px; }
.kicker { font: bold 7.5pt 'DejaVu Sans', sans-serif; text-transform: uppercase; letter-spacing: 1px; color: #b3261e; }
p { margin: 3px 0; }
a { color: inherit; text-decoration: none; }
.link { font: 7.5pt 'DejaVu Sans', sans-serif; color: #555; word-break: break-all; }
.vuoto { text-align: center; font-size: 13pt; margin: 40px 0; color: #555; }
.problemi { font: 8pt 'DejaVu Sans', sans-serif; color: #888; margin-top: 12px; border-top: 1px solid #ccc; padding-top: 5px; }
"""


def pezzo_html(p, grande=False):
    e = html.escape
    img = f'<img src="{p["img"]}">' if p.get("img") else ""
    dominio = urlparse(p["link"]).netloc.replace("www.", "")
    azione = "▶ Guarda il video" if p["tipo"] == "video" else "Leggi l'articolo"
    return (f'<div class="{"apertura" if grande else "pezzo"}">{img}'
            f'<div class="kicker">{p["n"]}. {e(p["fonte"])}</div>'
            f'<h2><a href="{e(p["link"])}">{e(p["titolo"])}</a></h2>'
            f'<p class="recap">{e(p["recap"])}</p>'
            f'<p class="link"><a href="{e(p["link"])}">{azione} · {e(dominio)}</a></p></div>')


def costruisci_html(video, articoli, errori, adesso):
    tutti = video + articoli
    sotto = f'{len(video)} video · {len(articoli)} notizie dai siti'
    corpo = [f'<div class="testata"><div class="sopra">{data_italiana(adesso)}</div>'
             f'<h1>Il Giornalino AI</h1><div class="sotto">{sotto}</div></div>']
    if not tutti:
        corpo.append('<p class="vuoto">Nessuna novità oggi dai canali e dai siti monitorati.</p>')
    else:
        corpo.append(pezzo_html(tutti[0], grande=True))
        resto_video = [p for p in video if p is not tutti[0]]
        if resto_video:
            corpo.append('<div class="sez">Dai canali YouTube</div><div class="colonne">'
                         + "".join(pezzo_html(p) for p in resto_video) + "</div>")
        resto_art = [p for p in articoli if p is not tutti[0]]
        if resto_art:
            corpo.append('<div class="sez">Dai siti</div><div class="colonne">'
                         + "".join(pezzo_html(p) for p in resto_art) + "</div>")
    if errori:
        corpo.append('<div class="problemi">Problemi oggi: ' + html.escape(" · ".join(errori)) + "</div>")
    return f'<html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{"".join(corpo)}</body></html>'


def crea_pdf(pagina_html):
    from weasyprint import HTML
    return HTML(string=pagina_html).write_pdf()


# ---------------------------------------------------------------- mail

def invia(oggetto, testo, pdf, nome_pdf):
    msg = MIMEMultipart()
    msg["Subject"] = oggetto
    msg["From"] = MITTENTE
    msg["To"] = DEST
    msg.attach(MIMEText(testo, "plain", "utf-8"))
    if pdf:
        allegato = MIMEApplication(pdf, _subtype="pdf")
        allegato.add_header("Content-Disposition", "attachment", filename=nome_pdf)
        msg.attach(allegato)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(MITTENTE, PASSWORD)
        s.send_message(msg)


def main():
    adesso = datetime.now(ZoneInfo("Europe/Rome"))
    limite = datetime.now(timezone.utc) - timedelta(hours=ORE)
    video, articoli, errori = [], [], []

    for nome, handle, cid in CANALI:
        try:
            cid = cid or channel_id(handle)
            if not cid:
                raise RuntimeError("channel ID non trovato")
            for v in nuovi_video(cid, limite):
                v.update(tipo="video", fonte=nome)
                video.append(v)
        except Exception as ex:
            errori.append(f"{nome}: errore ({ex})")
    video.sort(key=lambda v: v["pub"], reverse=True)

    for nome, url in SITI:
        try:
            for a in articoli_feed(url, limite):
                a.update(tipo="articolo", fonte=nome)
                articoli.append(a)
        except Exception as ex:
            errori.append(f"{nome}: errore ({ex})")
    articoli.sort(key=lambda a: a["pub"], reverse=True)
    articoli = articoli[:ARTICOLI_MAX]

    for n, p in enumerate(video + articoli, 1):
        p["n"] = n
        p["recap"] = recap(p, PROMPT_VIDEO if p["tipo"] == "video" else PROMPT_ARTICOLO)
        p["img"] = immagine_data_uri(p.get("img_url"))

    oggetto = f"Il Giornalino AI - {adesso.strftime('%d/%m/%Y')}"
    pdf = None
    try:
        pdf = crea_pdf(costruisci_html(video, articoli, errori, adesso))
    except Exception as ex:
        print(f"[pdf errore] {ex}", file=sys.stderr)
        errori.append(f"PDF non generato ({ex})")

    righe = [f"Il Giornalino AI del {data_italiana(adesso)}: {len(video)} video e {len(articoli)} notizie dai siti."]
    if pdf:
        righe.append("Il giornale completo, con le immagini, è nel PDF allegato.")
    righe.append("")
    for p in video + articoli:
        righe.append(f"{p['n']}. {p['fonte']} - {p['titolo']}\n{p['recap']}\n{p['link']}\n")
    if not (video or articoli):
        righe.append("Nessuna novità oggi.")
    if errori:
        righe.append("\nProblemi oggi:\n" + "\n".join(errori))
    if video or articoli:
        righe.append("\nRispondi con i numeri che vuoi ingerire in NotebookLM e il formato "
                     "(podcast, presentazione, mappa mentale, ecc.).")
    testo = "\n".join(righe)

    invia(oggetto, testo, pdf, f"giornalino-ai-{adesso.strftime('%Y-%m-%d')}.pdf")
    print(testo)
    if errori and not (video or articoli) and len(errori) >= len(CANALI) + len(SITI):
        sys.exit(1)


if __name__ == "__main__":
    main()
