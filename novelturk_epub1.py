# -*- coding: utf-8 -*-
"""
novelturk.com -> EPUB   (HER SERI ICIN, Google Colab, tek hucre)

Yalnizca asagidaki SERIES listesini degistirmeniz yeterli:
  - Seri sayfasinin linkini ekleyin (https://novelturk.com/novel/<seri-adi>/)
  - Ya da sadece '<seri-adi>' yazin.
Baslik, yazar, kapak, ilk ve son bolum otomatik bulunur; dosya adi seri adindan uretilir.

Nasil calisir: curl_cffi ile Cloudflare'i asar, bolumleri "Sonraki" baglantisiyla gezer,
metni sitenin acik WordPress REST API'sinden alir (bildirim penceresi/reklam/filigran
sorunu olmaz), EbookLib ile EPUB uretir. Yarim kalirsa tekrar calistirin: kaldigi yerden
devam eder.
"""
import sys, subprocess, importlib

def _pip(*pkgs):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *pkgs])

for _m, _p in [("curl_cffi", "curl_cffi"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"),
               ("ebooklib", "EbookLib"), ("tqdm", "tqdm"), ("PIL", "pillow")]:
    try:
        importlib.import_module(_m)
    except ImportError:
        _pip(_p)

import io, re, json, time, random, html as html_lib
from pathlib import Path
from curl_cffi import requests as cffi
from bs4 import BeautifulSoup, NavigableString, Comment
from ebooklib import epub
from tqdm.auto import tqdm

# ===================== AYARLAR =====================
SERIES = [
    "https://novelturk.com/novel/solo-farming-in-the-tower/",
    # "https://novelturk.com/novel/solo-farming-in-the-tower/",
    # "Solo Farming Tower",
]
MAX_CHAPTERS = 5        # None = serinin tamami. Yeni seride once 3 ile deneyin.
DELAY = (1.0, 2.2)      # bolumler arasi rastgele bekleme (sn)
# ===================================================

BASE = "https://novelturk.com"
WORK = Path("/content") if Path("/content").exists() else Path(".")
IMPERSONATE = ["chrome124", "chrome120", "chrome110"]


class FetchError(Exception):
    pass


class Http:
    def __init__(self):
        self.i = 0
        self.s = self._new()

    def _new(self):
        for _ in IMPERSONATE:
            try:
                return cffi.Session(impersonate=IMPERSONATE[self.i % len(IMPERSONATE)])
            except Exception:
                self.i += 1
        return cffi.Session(impersonate="chrome")

    def get(self, url, referer=None, binary=False, retries=4):
        last = "?"
        for n in range(1, retries + 1):
            try:
                h = {"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7"}
                if referer:
                    h["Referer"] = referer
                r = self.s.get(url, headers=h, timeout=30, allow_redirects=True)
                if r.status_code == 200:
                    if binary:
                        return r.content
                    if "Just a moment..." not in r.text[:4000]:
                        return r.text
                    last = "Cloudflare challenge"
                else:
                    last = f"HTTP {r.status_code}"
            except Exception as e:
                last = repr(e)
            self.i += 1
            self.s = self._new()
            time.sleep(min(2 ** n, 20) + random.random())
        raise FetchError(f"{url} alinamadi ({last})")


http = Http()

# ---------------- HTML -> temiz XHTML bloklari ----------------
BLOCK = {"p", "div", "section", "article", "main", "center", "blockquote", "ul", "ol", "li",
         "table", "thead", "tbody", "tr", "td", "th", "pre", "hr",
         "h1", "h2", "h3", "h4", "h5", "h6"}
KEEP = {"b", "strong", "i", "em", "u"}
NOISE = re.compile(r"(novel\s*t[üu]rk|novelturk\.com|bu bölümü paylaş|bu bölümde hata bildir|"
                   r"bildirimlere izin ver|bölüm yorumları)", re.I)


def plain(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def inline_html(node):
    out = []
    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            out.append(html_lib.escape(str(ch), quote=False))
        elif ch.name == "br":
            out.append("<br/>")
        elif ch.name in ("img", "script", "style", "svg", "ins"):
            continue
        elif ch.name in KEEP:
            t = inline_html(ch)
            if t.strip():
                out.append(f"<{ch.name}>{t}</{ch.name}>")
        else:
            out.append(inline_html(ch))
    return "".join(out)


def blocks_from(node, out):
    buf = []

    def flush():
        txt = "".join(buf).strip()
        buf.clear()
        for line in re.split(r"(?:<br/>\s*)+", txt):
            if plain(line):
                out.append(f"<p>{line.strip()}</p>")

    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            buf.append(html_lib.escape(str(ch), quote=False))
            continue
        n = ch.name
        if n in ("script", "style", "img", "svg", "noscript", "ins"):
            continue
        if n == "br":
            buf.append("<br/>")
            continue
        if n not in BLOCK:
            t = inline_html(ch) if n not in KEEP else f"<{n}>{inline_html(ch)}</{n}>"
            buf.append(t)
            continue
        flush()
        if n == "hr":
            out.append("<hr/>")
        elif re.fullmatch(r"h[1-6]", n):
            t = inline_html(ch).strip()
            if plain(t):
                out.append(f"<h3>{t}</h3>")
        elif n == "p":
            t = inline_html(ch).strip()
            if plain(t):
                out.append(f"<p>{t}</p>")
        elif n == "blockquote":
            inner = []
            blocks_from(ch, inner)
            if inner:
                out.append("<blockquote>" + "".join(inner) + "</blockquote>")
        elif n in ("ul", "ol"):
            for li in ch.find_all("li", recursive=False):
                t = inline_html(li).strip()
                if plain(t):
                    out.append(f"<p>• {t}</p>")
        elif n == "table":
            for tr in ch.find_all("tr"):
                cells = [inline_html(td).strip() for td in tr.find_all(["td", "th"])]
                cells = [c for c in cells if plain(c)]
                if cells:
                    out.append("<p>" + " | ".join(cells) + "</p>")
        elif n == "pre":
            out.append("<pre>" + html_lib.escape(ch.get_text()) + "</pre>")
        else:
            blocks_from(ch, out)
    flush()


def filigran_mi(t):
    """Unicode 'susleme' harfleriyle yazilmis site filigranlarini yakalar."""
    n = sum(1 for c in t if 0x1D400 <= ord(c) <= 0x1D7FF or 0x1F100 <= ord(c) <= 0x1F1FF
            or 0x2100 <= ord(c) <= 0x214F or 0x20A0 <= ord(c) <= 0x20CF
            or 0x2980 <= ord(c) <= 0x29FF or 0x3000 <= ord(c) <= 0x303F)
    return n >= 3


# ---------------- Site islemleri ----------------
def absolute(u):
    return u if u.startswith("http") else BASE + (u if u.startswith("/") else "/" + u)


def novel_url_of(x):
    x = x.strip()
    return x if x.startswith("http") else f"{BASE}/novel/{x.strip('/')}/"


def novel_info(url):
    soup = BeautifulSoup(http.get(url), "lxml")
    meta = lambda p: (soup.find("meta", property=p) or {}).get("content", "")
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else url.rstrip("/").split("/")[-1]
    a = soup.select_one('a[href*="nauthor="]')
    links = [(x.get_text(" ", strip=True), absolute(x["href"]))
             for x in soup.find_all("a", href=True) if "/bolum/" in x["href"]]
    first = next((h for t, h in links if re.search(r"[İI]LK", t)), None)
    last = next((h for t, h in links if re.search(r"\bSON\b", t)), None)
    if not first and links:   # yedek: numarasi en kucuk bolum
        num = lambda h: int((re.search(r"bolum-(\d+)", h) or [0, 10**9])[1])
        first = min((h for _, h in links), key=num)
    return {"title": title, "author": a.get_text(strip=True) if a else "Bilinmiyor",
            "desc": html_lib.unescape(meta("og:description")), "cover": html_lib.unescape(meta("og:image")),
            "first": first, "last": last, "url": url}


def chapter_page(url, referer):
    soup = BeautifulSoup(http.get(url, referer=referer), "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    nxt = None
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() == "sonraki" and "/bolum/" in a["href"]:
            nxt = absolute(a["href"])
            break
    return title, nxt


def rest_chapter(slug):
    api = f"{BASE}/wp-json/wp/v2/chapter?slug={slug}&_fields=id,slug,title,content"
    arr = json.loads(http.get(api, referer=BASE))
    if not arr:
        return None
    raw = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", arr[0]["content"]["rendered"])
    soup = BeautifulSoup("<div>" + raw + "</div>", "lxml")
    for t in soup.find_all(["script", "ins", "style", "iframe"]):
        t.extract()
    for t in soup.select("div.ad-slot"):
        t.extract()
    blocks = []
    blocks_from(soup.find("div"), blocks)
    blocks = [b for b in blocks if not filigran_mi(plain(b))]
    blocks = [b for b in blocks if not (len(plain(b)) <= 150 and NOISE.search(plain(b)))]
    return blocks if len(plain(" ".join(blocks))) >= 100 else None


# ---------------- EPUB ----------------
CSS = ("body{font-family:serif;line-height:1.6;margin:5%}h1{text-align:center;margin:1.5em 0 1em;"
       "font-size:1.4em}h3{margin:1.2em 0 .6em}p{margin:0 0 .9em;text-align:justify}"
       "blockquote{margin:1em 1.2em;padding-left:.8em;border-left:3px solid #999}hr{margin:1.5em 0}")


def cover_jpeg(url):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(http.get(url, referer=BASE, binary=True))).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        print(f"(Kapak eklenemedi: {e!r})")
        return None


def build_epub(info, chapters, path):
    slug = info["url"].rstrip("/").split("/")[-1]
    book = epub.EpubBook()
    book.set_identifier(f"novelturk-{slug}")
    book.set_title(info["title"])
    book.set_language("tr")
    book.add_author(info["author"])
    if info["desc"]:
        book.add_metadata("DC", "description", info["desc"])
    style = epub.EpubItem(uid="style_main", file_name="style/main.css", media_type="text/css", content=CSS)
    book.add_item(style)
    cover = cover_jpeg(info["cover"]) if info["cover"] else None
    if cover:
        book.set_cover("cover.jpg", cover)
    items, toc = [], []
    for i, ch in enumerate(chapters, 1):
        it = epub.EpubHtml(title=ch["title"], file_name=f"chap_{i:04d}.xhtml", lang="tr")
        it.content = f"<h1>{html_lib.escape(ch['title'])}</h1>" + "".join(ch["blocks"])
        it.add_item(style)
        book.add_item(it)
        items.append(it)
        toc.append(epub.Link(it.file_name, ch["title"], f"chap{i:04d}"))
    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = (["cover"] if cover else []) + ["nav"] + items
    epub.write_epub(str(path), book)


# ---------------- Ana akis ----------------
def run_series(entry):
    url = novel_url_of(entry)
    slug = url.rstrip("/").split("/")[-1]
    print(f"\n=== {slug} ===")
    info = novel_info(url)
    if not info["first"]:
        print("✗ Ilk bolum bulunamadi, seri atlandi.")
        return
    print(f"{info['title']} | Yazar: {info['author']} | Ilk: {info['first'].split('/')[-2]} | "
          f"Son: {(info['last'] or '?').rstrip('/').split('/')[-1]}")

    cache = WORK / "novelturk_cache" / slug
    cache.mkdir(parents=True, exist_ok=True)
    chapters, failed, seen = [], [], set()
    cur, ref = info["first"], url
    bar = tqdm(total=MAX_CHAPTERS, desc=slug[:25])

    while cur and cur.rstrip("/") not in seen:
        if MAX_CHAPTERS is not None and len(chapters) + len(failed) >= MAX_CHAPTERS:
            break
        seen.add(cur.rstrip("/"))
        cp = cache / (cur.rstrip("/").split("/")[-1] + ".json")
        if cp.exists():
            d = json.loads(cp.read_text(encoding="utf-8"))
        else:
            try:
                title, nxt = chapter_page(cur, ref)
                blocks = rest_chapter(cur.rstrip("/").split("/")[-1])
            except (FetchError, ValueError) as e:
                print(f"\n✗ {e}")
                break
            title = title.replace(info["title"], "", 1).strip(" –-") or title
            d = {"title": title, "blocks": blocks, "next": nxt}
            if blocks:
                cp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            time.sleep(random.uniform(*DELAY))
        if d["blocks"]:
            chapters.append(d)
        else:
            print(f"\n✗ Icerik alinamadi: {cur}")
            failed.append(cur)
        bar.update(1)
        if info["last"] and cur.rstrip("/") == info["last"].rstrip("/"):
            break
        ref, cur = cur, d["next"]
    bar.close()

    if not chapters:
        print("Hic bolum alinamadi.")
        return
    out = WORK / (re.sub(r"[^\w\-]+", "_", info["title"]).strip("_") + ".epub")
    build_epub(info, chapters, out)
    print(f"✓ {len(chapters)} bolum -> {out}  ({out.stat().st_size // 1024} KB)")
    for f in failed:
        print("  ⚠ alinamayan:", f)
    try:
        from google.colab import files
        files.download(str(out))
    except Exception:
        pass


for _s in SERIES:
    try:
        run_series(_s)
    except Exception as _e:
        print(f"✗ {_s} atlandi: {_e!r}")
