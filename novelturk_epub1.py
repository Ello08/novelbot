# -*- coding: utf-8 -*-
"""
novelturk.com -> EPUB   (Google Colab, GitHub Actions veya yerel makine)

Yalnizca asagidaki SERIES listesini degistirmeniz yeterli (ya da NT_SERIES ortam degiskeni):
  - Seri sayfasinin linkini ekleyin (https://novelturk.com/novel/<seri-adi>/)
  - Ya da sadece '<seri-adi>' yazin.

Nasil calisir
  1. curl_cffi ile tarayici TLS/HTTP2 parmak izi taklit edilir; her istek turu (sayfa / JSON / resim)
     icin gercekci basliklar gonderilir, oturum once ana sayfayi ziyaret ederek isinir.
  2. Seri bilgisi once HTML'den, alinamazsa WordPress REST API'sinden (wp-json) okunur.
  3. Bolum metni REST API'den alinir; sonraki bolum HTML "Sonraki" baglantisindan ya da
     (HTML engelliyse) REST'ten olusturulan bolum listesinden bulunur.
  4. EbookLib ile EPUB uretilir. Yarim kalirsa tekrar calistirin: onbellekten devam eder.

Ortam degiskenleri (hepsi istege bagli)
  NT_SERIES         "link1,link2"   SERIES listesini ezer
  NT_MAX_CHAPTERS   "5" / "0"|"all" (0 = tum seri)
  NT_OUT            cikti klasoru (varsayilan: Colab'da /content, aksi halde .)
  NOVELTURK_PROXY   http://kullanici:sifre@host:port  (veri merkezi IP'si engelliyse)
  NT_PROBE=1        (veya --probe) indirme yapmadan erisim tanisi yazdirir
"""
import sys, subprocess, importlib, os


def _pip(*pkgs):
    cmd = [sys.executable, "-m", "pip", "install", "-q", *pkgs]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        subprocess.check_call(cmd + ["--break-system-packages"])


for _m, _p in [("curl_cffi", "curl_cffi"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"),
               ("ebooklib", "EbookLib"), ("tqdm", "tqdm"), ("PIL", "pillow")]:
    try:
        importlib.import_module(_m)
    except ImportError:
        _pip(_p)

import io, re, json, time, random, html as html_lib
from pathlib import Path
from urllib.parse import urlparse, urlencode, quote
from curl_cffi import requests as cffi
from bs4 import BeautifulSoup, NavigableString, Comment
from ebooklib import epub
from tqdm.auto import tqdm

# ===================== AYARLAR =====================
SERIES = [
    "https://novelturk.com/novel/solo-farming-in-the-tower/",
    # "Solo Farming Tower",
]
MAX_CHAPTERS = 5        # None = serinin tamami. Yeni seride once 3 ile deneyin.
DELAY = (1.0, 2.2)      # bolumler arasi rastgele bekleme (sn)
# ===================================================

if os.getenv("NT_SERIES", "").strip():
    SERIES = [s.strip() for s in re.split(r"[,\n]", os.environ["NT_SERIES"]) if s.strip()]
if os.getenv("NT_MAX_CHAPTERS", "").strip():
    MAX_CHAPTERS = (None if os.environ["NT_MAX_CHAPTERS"].strip().lower() in ("0", "all", "none")
                    else int(os.environ["NT_MAX_CHAPTERS"]))
PROXY = os.getenv("NOVELTURK_PROXY") or None
PROBE = os.getenv("NT_PROBE") == "1" or "--probe" in sys.argv

BASE = "https://novelturk.com"
WORK = Path(os.getenv("NT_OUT") or ("/content" if Path("/content").exists() else "."))
WORK.mkdir(parents=True, exist_ok=True)

STATE = {"html": True}   # HTML sayfalari engelliyse False olur; bir daha denenmez


def warn(msg):
    print(f"\n⚠ {msg}", flush=True)
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::warning::{msg}", flush=True)


# ---------------- HTTP katmani ----------------
class FetchError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


# En yeni tarayici parmak izlerinden eskiye; kurulu curl_cffi'de olmayanlar elenir.
_PREF = ["chrome136", "chrome133a", "chrome131", "chrome124", "chrome120", "chrome119",
         "chrome116", "chrome110"]


def _targets():
    try:
        from curl_cffi.requests import BrowserType
        have = {b.value for b in BrowserType}
    except Exception:
        have = set()
    return ([t for t in _PREF if t in have] or ["chrome120", "chrome110"]) + ["chrome"]


IMPERSONATE = _targets()
LANG = "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
CHALLENGE = ("Just a moment", "cf-chl", "challenge-platform", "Attention Required", "cf_chl_opt")


def _site(url, referer):
    """Sec-Fetch-Site degeri: adres cubuguna yazma / ayni site / baska site."""
    if not referer:
        return "none"
    a, b = urlparse(url).netloc, urlparse(referer).netloc
    if a == b:
        return "same-origin"
    return "same-site" if a.split(".")[-2:] == b.split(".")[-2:] else "cross-site"


class Http:
    def __init__(self):
        self.i = 0
        self.s = None
        self.name = "?"
        self.warm = False
        self._new()

    def _new(self):
        proxies = {"http": PROXY, "https": PROXY} if PROXY else None
        for _ in range(len(IMPERSONATE)):
            name = IMPERSONATE[self.i % len(IMPERSONATE)]
            try:
                self.s, self.name, self.warm = cffi.Session(impersonate=name, proxies=proxies), name, False
                return
            except Exception:
                self.i += 1
        self.s, self.name, self.warm = cffi.Session(impersonate="chrome", proxies=proxies), "chrome", False

    def _headers(self, url, kind, referer):
        # User-Agent / sec-ch-ua'yi bilerek elle vermiyoruz: impersonate, TLS parmak iziyle
        # uyumlu setini kendisi ekler. Uyumsuz elle yazilmis UA, engel ihtimalini arttirir.
        h = {"Accept-Language": LANG}
        if kind == "json":
            h.update({"Accept": "application/json, text/plain, */*", "Sec-Fetch-Dest": "empty",
                      "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": _site(url, referer or BASE)})
        elif kind == "image":
            h.update({"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                      "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors",
                      "Sec-Fetch-Site": _site(url, referer or BASE)})
        else:
            h.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                "image/avif,image/webp,image/apng,*/*;q=0.8",
                      "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document",
                      "Sec-Fetch-Mode": "navigate", "Sec-Fetch-User": "?1",
                      "Sec-Fetch-Site": _site(url, referer)})
        if referer:
            h["Referer"] = referer
        return h

    def _once(self, url, kind, referer):
        return self.s.get(url, headers=self._headers(url, kind, referer), timeout=30,
                          allow_redirects=True)

    def _warm_up(self):
        """Gercek kullanici gibi once ana sayfaya girip cerezleri al."""
        self.warm = True
        try:
            self._once(BASE + "/", "html", None)
            time.sleep(random.uniform(0.6, 1.4))
        except Exception:
            pass

    @staticmethod
    def _challenged(r, kind):
        if r.headers.get("cf-mitigated") == "challenge":
            return True
        if kind == "image":
            return False
        head = r.text[:4000]
        if kind == "json" and not head.lstrip().startswith("<"):
            return False    # gecerli JSON; icerikte "Just a moment" gecebilir
        return any(m in head for m in CHALLENGE)

    def request(self, url, kind="html", referer=None, retries=3):
        last, status = "?", None
        for n in range(1, retries + 1):
            wait = None
            try:
                if not self.warm and url.rstrip("/") != BASE:
                    self._warm_up()
                r = self._once(url, kind, referer)
                status = r.status_code
                challenged = self._challenged(r, kind)
                if status == 200 and not challenged:
                    return r
                if status in (400, 401, 404, 410) and not challenged:
                    raise FetchError(f"{url} alinamadi (HTTP {status})", status)
                last = f"HTTP {status}" + (" / Cloudflare challenge" if challenged else "") \
                       + f" [{self.name}]"
                ra = r.headers.get("retry-after")
                if status == 429 and ra and ra.isdigit():
                    wait = min(int(ra), 60)
            except FetchError:
                raise
            except Exception as e:
                last = repr(e)
            self.i += 1
            self._new()          # farkli parmak izi + temiz oturum ile tekrar dene
            if n < retries:
                time.sleep(wait or min(2 ** n, 20) + random.random())
        raise FetchError(f"{url} alinamadi ({last})", status)

    def get(self, url, referer=None, binary=False, retries=3):
        r = self.request(url, "image" if binary else "html", referer, retries)
        return r.content if binary else r.text

    def get_json(self, url, referer=None, retries=3):
        r = self.request(url, "json", referer or BASE + "/", retries)
        try:
            return json.loads(r.text)
        except ValueError:
            raise FetchError(f"{url} gecerli JSON degil", r.status_code)


http = Http()


def wp_get(route, retries=3, **params):
    """WordPress REST GET. Once /wp-json/, olmazsa ?rest_route= bicimi denenir."""
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    urls = [f"{BASE}/wp-json{route}?{qs}", f"{BASE}/?rest_route={quote(route)}&{qs}"]
    err = None
    for u in urls:
        try:
            return http.get_json(u, referer=BASE + "/", retries=retries)
        except FetchError as e:
            err = e
            if e.status in (400, 404):    # rota yok / sayfa sonu: ikinci bicim de ayni sonucu verir
                raise
    raise err


# ---------------- HTML -> temiz XHTML bloklari ----------------
BLOCK = {"p", "div", "section", "article", "main", "center", "blockquote", "ul", "ol", "li",
         "table", "thead", "tbody", "tr", "td", "th", "pre", "hr",
         "h1", "h2", "h3", "h4", "h5", "h6"}
KEEP = {"b", "strong", "i", "em", "u"}
NOISE = re.compile(r"(novel\s*t[üu]rk|novelturk\.com|bu bölümü paylaş|bu bölümde hata bildir|"
                   r"bildirimlere izin ver|bölüm yorumları)", re.I)
ZW = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


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


def to_blocks(node):
    """Bir HTML dugumunden temiz <p> bloklari cikarir; metin cok kisaysa None."""
    for t in node.find_all(["script", "ins", "style", "iframe", "noscript"]):
        t.extract()
    for t in node.select("div.ad-slot"):
        t.extract()
    blocks = []
    blocks_from(node, blocks)
    blocks = [ZW.sub("", b) for b in blocks]
    blocks = [b for b in blocks if not filigran_mi(plain(b))]
    blocks = [b for b in blocks if not (len(plain(b)) <= 150 and NOISE.search(plain(b)))]
    return blocks if len(plain(" ".join(blocks))) >= 100 else None


# ---------------- Site islemleri ----------------
def absolute(u):
    return u if u.startswith("http") else BASE + (u if u.startswith("/") else "/" + u)


def novel_url_of(x):
    x = x.strip()
    return x if x.startswith("http") else f"{BASE}/novel/{x.strip('/')}/"


def last_seg(u):
    return u.rstrip("/").split("/")[-1]


def chapter_num(s):
    m = re.search(r"bolum-(\d+)", s)
    return int(m.group(1)) if m else None


def novel_info_html(url):
    soup = BeautifulSoup(http.get(url, referer=BASE + "/"), "lxml")
    meta = lambda p: (soup.find("meta", property=p) or {}).get("content", "")
    h1 = soup.find("h1")
    a = soup.select_one('a[href*="nauthor="]')
    links = [(x.get_text(" ", strip=True), absolute(x["href"]))
             for x in soup.find_all("a", href=True) if "/bolum/" in x["href"]]
    first = next((h for t, h in links if re.search(r"[İI]LK", t)), None)
    last = next((h for t, h in links if re.search(r"\bSON\b", t)), None)
    if not first and links:   # yedek: numarasi en kucuk bolum
        num = lambda h: chapter_num(h) if chapter_num(h) is not None else 10 ** 9
        first = min((h for _, h in links), key=num)
    return {"title": h1.get_text(" ", strip=True) if h1 else "",
            "author": a.get_text(strip=True) if a else "",
            "desc": html_lib.unescape(meta("og:description")),
            "cover": html_lib.unescape(meta("og:image")),
            "first": first, "last": last, "url": url}


# ---- WordPress REST yedegi ----
def _txt(s):
    return html_lib.unescape(plain(s or ""))


def rest_types():
    """{rest_base: tur_slug} - sitenin REST'e acik icerik turleri."""
    try:
        d = wp_get("/wp/v2/types", retries=2)
        return {(v.get("rest_base") or k): k for k, v in d.items()}
    except FetchError as e:
        if e.status in (400, 404):
            return {}
        raise


def find_novel_post(slug):
    """Seri yazisini REST'te bulur: /types ile kesfedilen turler, sonra posts/pages, sonra arama."""
    types = rest_types()
    is_novel = lambda b: (re.search(r"novel|seri|roman", b + types[b], re.I)
                          and not re.search(r"chapter|bolum|b[öo]l[üu]m", b + types[b], re.I))
    bases = [b for b in types if is_novel(b)]
    bases += [b for b in ("novel", "novels", "series", "seri", "posts", "pages")
              if b not in bases and (not types or b in types)]
    for b in bases:
        try:
            arr = wp_get(f"/wp/v2/{b}", retries=2, slug=slug, _embed=1)
        except FetchError as e:
            if e.status in (400, 404):
                continue
            raise
        if arr:
            return arr[0]
    # Son care: genel arama (tum acik turlerde)
    try:
        res = wp_get("/wp/v2/search", retries=2, search=slug.replace("-", " "), per_page=10,
                     _fields="id,url,subtype")
    except FetchError:
        res = []
    for r in res:
        if slug in r.get("url", ""):
            base = next((k for k, v in types.items() if v == r.get("subtype")), r.get("subtype"))
            try:
                return wp_get(f"/wp/v2/{base}/{r['id']}", retries=2, _embed=1)
            except FetchError:
                continue
    raise FetchError(f"REST API'de '{slug}' serisi bulunamadi", 404)


def novel_info_rest(slug, url):
    p = find_novel_post(slug)
    y = p.get("yoast_head_json") or {}
    emb = p.get("_embedded") or {}
    title = _txt((p.get("title") or {}).get("rendered"))
    desc = (y.get("og_description") or y.get("description")
            or _txt((p.get("excerpt") or {}).get("rendered"))
            or _txt((p.get("content") or {}).get("rendered"))[:600])
    cover = (next((i.get("url") for i in (y.get("og_image") or []) if i.get("url")), "")
             or ((emb.get("wp:featuredmedia") or [{}])[0] or {}).get("source_url", "")
             or p.get("jetpack_featured_media_url", ""))
    author = ""
    for group in emb.get("wp:term") or []:
        for t in group or []:
            if "author" in (t.get("taxonomy") or "") and t.get("name"):
                author = t["name"]
    return {"title": title, "author": author, "desc": desc, "cover": cover,
            "first": None, "last": None, "url": url}


def rest_chapter_index(info):
    """[(slug, link), ...] bolum numarasina gore sirali. Bolumler novel adiyla aranir."""
    slug = last_seg(info["url"])
    found = {}
    for term in dict.fromkeys([info["title"], slug.replace("-", " ")]):
        if not term:
            continue
        for page in range(1, 101):
            try:
                arr = wp_get("/wp/v2/chapter", retries=2, search=term, per_page=100, page=page,
                             orderby="date", order="asc", _fields="id,slug,link,title")
            except FetchError as e:
                if e.status in (400, 404):    # sayfa sonu
                    break
                raise
            for a in arr:
                found.setdefault(a["slug"], a)
            if len(arr) < 100:
                break
        if found:
            break
    mine = {k: v for k, v in found.items() if slug in k or slug in v.get("link", "")}
    if found and not mine:
        warn("Bolum listesi seri adina gore suzulemedi; arama sonuclari oldugu gibi kullaniliyor.")
    found = mine or found
    order = sorted(enumerate(found.values()),
                   key=lambda t: (chapter_num(t[1]["slug"]) if chapter_num(t[1]["slug"]) is not None
                                  else 10 ** 9, t[0]))
    return [(v["slug"], v["link"]) for _, v in order]


def index_of(info):
    if info.get("_idx") is None:
        try:
            info["_idx"] = rest_chapter_index(info)
        except FetchError as e:
            warn(f"Bolum listesi REST'ten alinamadi: {e}")
            info["_idx"] = []
    return info["_idx"]


def index_next(info, cur):
    idx = index_of(info)
    slugs = [s for s, _ in idx]
    cs = last_seg(cur)
    if cs in slugs:
        i = slugs.index(cs)
        return idx[i + 1][1] if i + 1 < len(idx) else None
    return None


def novel_info(url):
    slug = last_seg(url)
    h = {}
    if STATE["html"]:
        try:
            h = novel_info_html(url)
        except FetchError as e:
            STATE["html"] = False
            warn(f"Ana sayfa HTML'i alinamadi ({e}). WordPress REST API yedegine geciliyor.")
    r = {}
    if not h.get("first") or not h.get("title"):
        try:
            r = novel_info_rest(slug, url)
        except FetchError as e:
            warn(f"REST API'den seri bilgisi alinamadi: {e}")
    info = {**r, **{k: v for k, v in h.items() if v}}
    info.update(url=url, title=info.get("title") or slug.replace("-", " ").title(),
                author=info.get("author") or "Bilinmiyor", desc=info.get("desc", ""),
                cover=info.get("cover", ""), first=info.get("first"), last=info.get("last"))
    if not info["first"]:
        idx = index_of(info)
        if idx:
            info["first"], info["last"] = idx[0][1], info["last"] or idx[-1][1]
    return info


def chapter_page(url, referer):
    soup = BeautifulSoup(http.get(url, referer=referer), "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    nxt = None
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() == "sonraki" and "/bolum/" in a["href"]:
            nxt = absolute(a["href"])
            break
    return title, nxt, soup


def rest_chapter(slug):
    arr = wp_get("/wp/v2/chapter", slug=slug, _fields="id,slug,title,content")
    if not arr:
        return "", None
    raw = ZW.sub("", arr[0]["content"]["rendered"])
    soup = BeautifulSoup("<div>" + raw + "</div>", "lxml")
    return _txt((arr[0].get("title") or {}).get("rendered")), to_blocks(soup.find("div"))


def html_blocks(soup):
    """Son care: REST calismazsa bolum metnini HTML sayfasindan oku."""
    for sel in ("div.entry-content", "div.chapter-content", "#chapter-content",
                "div.reading-content", "article"):
        node = soup.select_one(sel)
        b = to_blocks(node) if node else None
        if b:
            return b
    return None


def fetch_chapter(info, cur, ref):
    """{'title','blocks','next'} dondurur. HTML engelliyse REST'e duser."""
    cs = last_seg(cur)
    title = nxt = soup = None
    if STATE["html"]:
        try:
            title, nxt, soup = chapter_page(cur, ref)
        except FetchError as e:
            STATE["html"] = False
            warn(f"Bolum HTML'i alinamadi ({e}); bundan sonra yalnizca REST kullanilacak.")
    rtitle, blocks = "", None
    try:
        rtitle, blocks = rest_chapter(cs)
    except FetchError as e:
        if soup is None:
            raise
        warn(f"REST bolum icerigi alinamadi ({e}); metin HTML'den okunacak.")
    if not blocks and soup is not None:
        blocks = html_blocks(soup)
    is_last = bool(info["last"]) and cur.rstrip("/") == info["last"].rstrip("/")
    if not nxt and not is_last:
        nxt = index_next(info, cur)
    title = title or rtitle or cs
    title = title.replace(info["title"], "", 1).strip(" –—-") or title
    return {"title": title, "blocks": blocks, "next": nxt}


# ---------------- EPUB ----------------
CSS = ("body{font-family:serif;line-height:1.6;margin:5%}h1{text-align:center;margin:1.5em 0 1em;"
       "font-size:1.4em}h3{margin:1.2em 0 .6em}p{margin:0 0 .9em;text-align:justify}"
       "blockquote{margin:1em 1.2em;padding-left:.8em;border-left:3px solid #999}hr{margin:1.5em 0}")


def cover_jpeg(url):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(http.get(url, referer=BASE + "/", binary=True))).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        print(f"(Kapak eklenemedi: {e!r})")
        return None


def build_epub(info, chapters, path):
    slug = last_seg(info["url"])
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
    slug = last_seg(url)
    print(f"\n=== {slug} ===", flush=True)
    info = novel_info(url)
    if not info["first"]:
        print("✗ Ilk bolum bulunamadi (HTML ve REST denendi), seri atlandi.")
        return False
    print(f"{info['title']} | Yazar: {info['author']} | Ilk: {last_seg(info['first'])} | "
          f"Son: {last_seg(info['last']) if info['last'] else '?'}", flush=True)

    cache = WORK / "novelturk_cache" / slug
    cache.mkdir(parents=True, exist_ok=True)
    chapters, failed, seen = [], [], set()
    cur, ref = info["first"], url
    bar = tqdm(total=MAX_CHAPTERS, desc=slug[:25], disable=None)   # TTY yoksa (Actions) kapali

    while cur and cur.rstrip("/") not in seen:
        if MAX_CHAPTERS is not None and len(chapters) + len(failed) >= MAX_CHAPTERS:
            break
        seen.add(cur.rstrip("/"))
        cp = cache / (last_seg(cur) + ".json")
        if cp.exists():
            d = json.loads(cp.read_text(encoding="utf-8"))
            if not d.get("next") and not (info["last"] and cur.rstrip("/") == info["last"].rstrip("/")):
                d["next"] = index_next(info, cur)
        else:
            try:
                d = fetch_chapter(info, cur, ref)
            except FetchError as e:
                warn(f"{last_seg(cur)} alinamadi: {e}")
                break
            if d["blocks"]:
                cp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            time.sleep(random.uniform(*DELAY))
        if d["blocks"]:
            chapters.append(d)
        else:
            print(f"\n✗ Icerik alinamadi: {cur}")
            failed.append(cur)
        bar.update(1)
        n = len(chapters) + len(failed)
        if n % 10 == 0:
            print(f"  {n} bolum islendi...", flush=True)
        if info["last"] and cur.rstrip("/") == info["last"].rstrip("/"):
            break
        ref, cur = cur, d["next"]
    bar.close()

    if not chapters:
        print("Hic bolum alinamadi.")
        return False
    out = WORK / (re.sub(r"[^\w\-]+", "_", info["title"]).strip("_") + ".epub")
    build_epub(info, chapters, out)
    print(f"✓ {len(chapters)} bolum -> {out}  ({out.stat().st_size // 1024} KB)", flush=True)
    for f in failed:
        warn(f"alinamayan: {f}")
    try:
        from google.colab import files
        files.download(str(out))
    except Exception:
        pass
    return True


def probe(entry):
    """Indirme yapmadan hangi yolun acik oldugunu gosterir (sonucu paylasarak tani konulabilir)."""
    url = novel_url_of(entry)
    print(f"Parmak izi adaylari: {IMPERSONATE} | proxy: {'var' if PROXY else 'yok'}")
    tests = [("HTML ana sayfa", BASE + "/", "html"), ("HTML seri", url, "html"),
             ("REST kok", f"{BASE}/wp-json/", "json"),
             ("REST types", f"{BASE}/wp-json/wp/v2/types", "json"),
             ("REST chapter", f"{BASE}/wp-json/wp/v2/chapter?per_page=1&_fields=id,slug", "json"),
             ("REST rest_route", f"{BASE}/?rest_route=/wp/v2/types", "json")]
    for name, u, kind in tests:
        try:
            r = http._once(u, kind, None if kind == "html" else BASE + "/")
            print(f"{name:16} HTTP {r.status_code} | server={r.headers.get('server')} | "
                  f"cf-mitigated={r.headers.get('cf-mitigated')} | {r.text[:70]!r}")
        except Exception as e:
            print(f"{name:16} HATA {e!r}")


def main():
    if PROBE:
        probe(SERIES[0])
        return
    ok = True
    for s in SERIES:
        try:
            res = run_series(s)
        except Exception as e:
            print(f"✗ {s} atlandi: {e!r}")
            res = False
        ok = ok and res
    if not ok and "google.colab" not in sys.modules:
        sys.exit(1)     # Actions isi kirmizi bitsin


if __name__ == "__main__":
    main()
