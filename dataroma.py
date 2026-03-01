"""
DataRoma Portfolio Tracker — Render.com Deploy
===============================================
Render otomatik başlatır:  gunicorn dataroma:app
"""

# ── İMPORT'LAR ────────────────────────────────────────────────
import os, time, re, traceback
from io import StringIO
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests as req
from bs4 import BeautifulSoup
import pandas as pd

# ── FLASK UYGULAMASI ──────────────────────────────────────────
app = Flask(__name__)
CORS(app)

PORT          = int(os.environ.get("PORT", 5000))   # Render PORT env'i inject eder
DATAROMA_BASE = "https://www.dataroma.com"
MANAGERS_URL  = f"{DATAROMA_BASE}/m/managers.php"
CACHE_TTL     = 3600   # 60 dk  — yönetici listesi
PORTFOLIO_TTL = 1800   # 30 dk  — portföy detayı

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer":         "https://www.dataroma.com/",
}

COL_REMAP = {
    "stock":            "Stock",
    "%ofportfolio":     "pct",
    "portfolioweight":  "pct",
    "recentactivity":   "activity",
    "reportedprice*":   "reportedPrice",
    "reportedprice":    "reportedPrice",
    "currentprice":     "currentPrice",
    "+/-reportedprice": "change",
    "+/-price":         "change",
    "change":           "change",
}

# ── 3. CACHE ──────────────────────────────────────────────────
_cache: dict = {}

def _ttl(key):
    return PORTFOLIO_TTL if key.startswith("pf:") else CACHE_TTL

def cache_get(key):
    e = _cache.get(key)
    if e and time.time() - e[0] < _ttl(key):
        return e[1]
    if e: del _cache[key]
    return None

def cache_set(key, val):
    _cache[key] = (time.time(), val)

def cache_age(key):
    e = _cache.get(key)
    return int(time.time() - e[0]) if e else -1

# ── 4. HTTP ───────────────────────────────────────────────────
def fetch(url, timeout=20):
    r = req.get(url, headers=HTTP_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text

# ── 5. PARSE: YÖNETİCİLER ────────────────────────────────────
def parse_managers(html):
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.find("table", id="grid")
    if not grid:
        grid = next((t for t in soup.find_all("table") if "Portfolio" in t.get_text()), None)
    if not grid:
        raise ValueError("Yönetici tablosu bulunamadı.")

    managers = []
    for row in grid.find_all("tr"):
        tds = row.find_all("td")
        if not tds: continue
        a = tds[0].find("a")
        if not a or not a.get("href"): continue

        href = a["href"]
        url  = href if href.startswith("http") else DATAROMA_BASE + (href if href.startswith("/") else "/" + href)

        updated = ""
        for td in tds[1:]:
            txt = td.get_text(strip=True)
            if re.search(r"(20\d{2}|Q[1-4]\s*20\d{2})", txt):
                updated = txt; break

        managers.append({
            "name":            a.get_text(strip=True),
            "url":             url,
            "portfolio_value": tds[1].get_text(strip=True) if len(tds) > 1 else "",
            "updated":         updated,
        })

    return sorted(managers, key=lambda x: x["name"].lower())

# ── 6. PARSE: PORTFÖY ────────────────────────────────────────
def _norm(c):
    return str(c).strip().lower().replace(" ", "").replace("*", "")

def norm_df(df):
    return df.rename(columns={c: COL_REMAP.get(_norm(c), c) for c in df.columns})

def parse_portfolio(html, name):
    df = None
    try:
        for t in pd.read_html(StringIO(html), flavor="lxml"):
            n = norm_df(t)
            if "Stock" in n.columns:
                df = n; break
    except Exception:
        pass

    if df is None:
        df = _bs4_table(html)
        if df is not None:
            df = norm_df(df)

    if df is None or df.empty:
        raise ValueError(f"'{name}' için portföy tablosu bulunamadı.")
    if "Stock" not in df.columns:
        raise ValueError(f"Stock sütunu yok. Mevcut: {list(df.columns)}")

    out = []
    for _, row in df.iterrows():
        stock = str(row.get("Stock", "")).strip()
        if not stock or stock.lower() in ("nan", "stock", "ticker", ""): continue

        raw = str(row.get("pct", "0")).replace("%","").replace(",",".").strip()
        try:   pct = float(raw)
        except: pct = 0.0

        act = str(row.get("activity", "None")).strip()
        if act.lower() in ("nan", "none", ""): act = "None"

        def fp(v):
            s = str(v).strip()
            return "—" if s.lower() in ("nan","none","","-") else s

        out.append({
            "Stock":         stock,
            "pct":           round(pct, 2),
            "activity":      act,
            "reportedPrice": fp(row.get("reportedPrice","—")),
            "currentPrice":  fp(row.get("currentPrice","—")),
            "change":        fp(row.get("change","—")),
        })
    return out

def _bs4_table(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup.find_all("table"):
        txt = t.get_text().lower()
        if "stock" in txt and ("portfolio" in txt or "activity" in txt):
            rows, headers, data = t.find_all("tr"), [], []
            for i, tr in enumerate(rows):
                cells = [td.get_text(strip=True) for td in tr.find_all(["th","td"])]
                if not cells: continue
                if i == 0 or cells[0].lower() in ("","stock","no"):
                    headers = cells
                else:
                    data.append(cells)
            if headers and data:
                data = [r[:len(headers)] + [""]*max(0,len(headers)-len(r)) for r in data]
                return pd.DataFrame(data, columns=headers)
    return None

def api_err(e):
    if isinstance(e, req.HTTPError):
        return jsonify({"error": f"dataroma.com HTTP {e.response.status_code}"}), 502
    if isinstance(e, req.ConnectionError):
        return jsonify({"error": "İnternet bağlantısı yok veya dataroma.com erişilemiyor."}), 503
    if isinstance(e, req.Timeout):
        return jsonify({"error": "dataroma.com zaman aşımı."}), 504
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

# ── 7. API ENDPOINT'LERİ ─────────────────────────────────────
@app.route("/api/status")
def api_status():
    return jsonify({"status": "ok", "cache_count": len(_cache),
                    "cache_ttl": {"managers_min": CACHE_TTL//60, "portfolio_min": PORTFOLIO_TTL//60}})

@app.route("/api/managers")
def api_managers():
    force = request.args.get("refresh") == "1"
    if not force:
        hit = cache_get("managers")
        if hit:
            return jsonify({"managers": hit, "cached": True, "age_s": cache_age("managers"), "count": len(hit)})
    try:
        html = fetch(MANAGERS_URL)
        mgrs = parse_managers(html)
        if not mgrs: return jsonify({"error": "Yönetici listesi boş."}), 502
        cache_set("managers", mgrs)
        return jsonify({"managers": mgrs, "cached": False, "count": len(mgrs)})
    except Exception as e:
        return api_err(e)

@app.route("/api/portfolio")
def api_portfolio():
    url   = request.args.get("url","").strip()
    name  = request.args.get("name","Bilinmeyen").strip()
    force = request.args.get("refresh") == "1"

    if not url: return jsonify({"error": "url parametresi zorunlu."}), 400
    if not url.startswith(DATAROMA_BASE): return jsonify({"error": "Sadece dataroma.com URL."}), 403

    ck = f"pf:{url}"
    if not force:
        hit = cache_get(ck)
        if hit:
            return jsonify({"name": name, "portfolio": hit, "cached": True, "age_s": cache_age(ck), "count": len(hit)})
    try:
        html = fetch(url)
        pf   = parse_portfolio(html, name)
        if not pf: return jsonify({"error": "Portföy verisi yok."}), 404
        cache_set(ck, pf)
        return jsonify({"name": name, "portfolio": pf, "cached": False, "count": len(pf)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        return api_err(e)

@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    n = len(_cache); _cache.clear()
    return jsonify({"cleared": n})

# ── 8. FRONTEND (HTML gömülü) ─────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DataRoma Portfolio Tracker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Fira+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#08080d;--s1:#111118;--s2:#181824;--s3:#20202e;--bd:#272736;--ac:#00e5b0;--a2:#7c5cbf;--tx:#e4e4f0;--mu:#60607a;--up:#00e5b0;--dn:#ff4060;--wn:#ffc840;--r:12px}
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--tx);font-family:'Syne',sans-serif;min-height:100vh;overflow-x:hidden;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;background:radial-gradient(ellipse 60% 40% at 10% 0%,rgba(0,229,176,.04),transparent 60%),radial-gradient(ellipse 50% 60% at 90% 100%,rgba(124,92,191,.04),transparent 60%)}
.wrap{position:relative;z-index:1;max-width:1320px;margin:0 auto;padding:0 20px 60px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:var(--bd);border-radius:2px}

/* HEADER */
header{padding:30px 0 18px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;border-bottom:1px solid var(--bd);margin-bottom:18px}
.logo{display:flex;align-items:center;gap:11px}
.logo-mark{width:38px;height:38px;border-radius:9px;background:var(--ac);display:flex;align-items:center;justify-content:center;font-size:19px;box-shadow:0 0 20px rgba(0,229,176,.25)}
.logo h1{font-size:clamp(16px,3.5vw,22px);font-weight:800;letter-spacing:-.4px}
.logo h1 em{color:var(--ac);font-style:normal}
.hdr-right{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.pill{display:flex;align-items:center;gap:6px;background:var(--s1);border:1px solid var(--bd);border-radius:20px;padding:4px 11px;font-family:'Fira Mono',monospace;font-size:11px;color:var(--mu);white-space:nowrap}
.dot{width:6px;height:6px;border-radius:50%;background:var(--mu);flex-shrink:0}
.dot.live{background:var(--ac);animation:blink 2s infinite}
.dot.err{background:var(--dn)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.btn{padding:5px 11px;border:1px solid var(--bd);border-radius:8px;background:var(--s1);color:var(--mu);cursor:pointer;font-family:'Fira Mono',monospace;font-size:11px;transition:border-color .15s,color .15s,background .15s;white-space:nowrap}
.btn:hover{border-color:var(--ac);color:var(--ac);background:rgba(0,229,176,.05)}

/* BANNER */
#banner{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);padding:10px 15px;margin-bottom:14px;font-size:12.5px;transition:all .3s}
#banner.ok{border-color:rgba(0,229,176,.3);background:rgba(0,229,176,.04)}
#banner.fail{border-color:rgba(255,64,96,.3);background:rgba(255,64,96,.04);color:var(--dn)}
#banner.wait{color:var(--mu)}
.bico{font-size:15px}
.btxt{flex:1;line-height:1.55}
.btxt code{font-family:'Fira Mono',monospace;font-size:11px;opacity:.75}

/* CONTROLS */
.controls{display:grid;grid-template-columns:1fr 210px;gap:10px;margin-bottom:9px;align-items:center}
@media(max-width:680px){.controls{grid-template-columns:1fr}}
.inp-wrap{position:relative}
.inp-ico{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--mu);font-size:13px;pointer-events:none}
input[type=text],select{width:100%;padding:11px 11px 11px 38px;background:var(--s1);border:1px solid var(--bd);border-radius:10px;color:var(--tx);font-family:'Syne',sans-serif;font-size:13.5px;outline:none;transition:border-color .2s}
input:focus,select:focus{border-color:var(--ac)}
input::placeholder{color:var(--mu)}
.sel-wrap{position:relative}
.sel-wrap select{padding:11px 28px 11px 13px;font-family:'Fira Mono',monospace;font-size:12px;cursor:pointer;appearance:none}
.sel-wrap::after{content:'▾';position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--mu);pointer-events:none;font-size:11px}
select option{background:#1a1a26}

/* FILTER BAR */
.fbar{display:flex;align-items:center;gap:7px;margin-bottom:13px;flex-wrap:wrap;min-height:22px}
.badge{display:inline-flex;align-items:center;gap:5px;background:rgba(0,229,176,.08);border:1px solid rgba(0,229,176,.22);border-radius:20px;padding:2px 9px;font-family:'Fira Mono',monospace;font-size:11px;color:var(--ac);animation:pop .2s ease}
@keyframes pop{from{transform:scale(.85);opacity:0}to{transform:scale(1);opacity:1}}
.badge button{background:none;border:none;color:var(--ac);cursor:pointer;font-size:12px;padding:0;line-height:1;opacity:.6}
.badge button:hover{opacity:1}
.rcount{font-family:'Fira Mono',monospace;font-size:11px;color:var(--mu);margin-left:auto}

/* LAYOUT */
.main-grid{display:grid;grid-template-columns:300px 1fr;gap:16px;align-items:start}
@media(max-width:760px){.main-grid{grid-template-columns:1fr}}

/* MANAGER LIST */
.mgr-panel{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);overflow:hidden;max-height:720px;display:flex;flex-direction:column;position:sticky;top:14px}
.mgr-head{padding:10px 13px;border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between}
.mgr-head span{font-family:'Fira Mono',monospace;font-size:10px;color:var(--mu);text-transform:uppercase;letter-spacing:1px}
#mgCount{color:var(--ac)!important;font-weight:600}
#mgList{overflow-y:auto;flex:1}
.mgr-item{padding:9px 12px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.025);display:flex;align-items:flex-start;gap:8px;transition:background .1s}
.mgr-item:hover{background:var(--s2)}
.mgr-item.active{background:rgba(0,229,176,.06);border-left:2px solid var(--ac)}
.av{width:26px;height:26px;border-radius:6px;background:var(--s3);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;color:var(--a2);margin-top:2px}
.mi{flex:1;min-width:0}
.mname{font-size:12.5px;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mgr-item.active .mname{color:var(--ac)}
.mdate{font-family:'Fira Mono',monospace;font-size:10px;color:var(--mu);margin-top:2px;display:flex;align-items:center;gap:4px}
.dd{width:5px;height:5px;border-radius:50%;display:inline-block;flex-shrink:0}
.d-f{background:var(--up)}.d-r{background:var(--wn)}.d-o{background:var(--mu)}
.list-empty{padding:36px 12px;text-align:center;color:var(--mu);font-size:13px}
.list-loading{padding:36px 12px;display:flex;flex-direction:column;align-items:center;gap:12px;color:var(--mu);font-size:12px}

/* PORTFOLIO PANEL */
.pf-panel{min-height:400px}
.ph-ph{height:440px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--mu);background:var(--s1);border:1px dashed var(--bd);border-radius:var(--r)}
.ph-ph .bi{font-size:42px;opacity:.18}
.ph-ld{height:440px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;background:var(--s1);border:1px solid var(--bd);border-radius:var(--r)}
.spin{width:28px;height:28px;border:2px solid var(--bd);border-top-color:var(--ac);border-radius:50%;animation:rot .7s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}
.err-card{background:rgba(255,64,96,.05);border:1px solid rgba(255,64,96,.2);border-radius:var(--r);padding:22px 24px;color:var(--dn);line-height:1.7}
.err-card code{font-family:'Fira Mono',monospace;font-size:12px;opacity:.8}
.retry{margin-top:14px;padding:7px 16px;background:rgba(255,64,96,.1);border:1px solid rgba(255,64,96,.28);color:var(--dn);border-radius:7px;cursor:pointer;font-family:'Syne',sans-serif;font-size:13px;display:inline-block;transition:background .15s}
.retry:hover{background:rgba(255,64,96,.18)}

/* STATS */
.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(115px,1fr));gap:10px;margin-bottom:12px}
.sc{background:var(--s1);border:1px solid var(--bd);border-radius:10px;padding:13px 15px;animation:fu .35s ease both}
@keyframes fu{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}
.sl{font-size:9.5px;font-family:'Fira Mono',monospace;color:var(--mu);text-transform:uppercase;letter-spacing:1.1px;margin-bottom:5px}
.sv{font-size:22px;font-weight:700;line-height:1}
.sv.g{color:var(--up)}.sv.r{color:var(--dn)}.sv.y{color:var(--wn)}

/* PORTFOLIO HEADER */
.pf-head{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r) var(--r) 0 0;padding:15px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;border-bottom:none}
.pf-head h2{font-size:clamp(14px,2.8vw,18px);font-weight:700}
.pf-head h2 em{color:var(--ac);font-style:normal}
.chips{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.chip{background:var(--s2);border:1px solid var(--bd);border-radius:6px;padding:3px 9px;font-family:'Fira Mono',monospace;font-size:10.5px;color:var(--mu)}
.chip strong{color:var(--tx)}
.chip.live{color:var(--ac);border-color:rgba(0,229,176,.28);background:rgba(0,229,176,.05)}
.chip.cache{color:var(--wn);border-color:rgba(255,200,64,.28);background:rgba(255,200,64,.05)}
.chip.rb{cursor:pointer;transition:background .15s,border-color .15s}
.chip.rb:hover{border-color:var(--ac);color:var(--ac);background:rgba(0,229,176,.08)}

/* TABLE */
.tbl-wrap{overflow-x:auto;background:var(--s1);border:1px solid var(--bd);border-radius:0 0 var(--r) var(--r);border-top:1px solid var(--bd)}
table{width:100%;border-collapse:collapse;font-size:13px}
thead tr{background:var(--s2)}
th{padding:10px 13px;text-align:left;font-family:'Fira Mono',monospace;font-size:9.5px;letter-spacing:1px;text-transform:uppercase;color:var(--mu);font-weight:500;white-space:nowrap}
tbody tr{border-top:1px solid rgba(255,255,255,.025);transition:background .1s}
tbody tr:hover{background:var(--s2)}
td{padding:10px 13px;white-space:nowrap}
.tk{font-family:'Fira Mono',monospace;font-weight:600;color:var(--ac);font-size:13px}
.bar-cell{min-width:145px}
.bar-wrap{display:flex;align-items:center;gap:7px}
.bar-bg{flex:1;height:3px;background:var(--s3);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;background:var(--ac);transition:width .5s}
.bar-pct{font-family:'Fira Mono',monospace;font-size:11.5px;min-width:38px;text-align:right}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-family:'Fira Mono',monospace;font-size:10.5px}
.t-buy{background:rgba(0,229,176,.1);color:var(--up)}
.t-sell{background:rgba(255,64,96,.1);color:var(--dn)}
.t-new{background:rgba(124,92,191,.18);color:#b08dff}
.t-none{background:var(--s3);color:var(--mu)}
.pc{font-family:'Fira Mono',monospace;font-size:12px}
.pc.up{color:var(--up)}.pc.dn{color:var(--dn)}.pc.nt{color:var(--mu)}
.pv{font-family:'Fira Mono',monospace;font-size:12.5px}

/* MOBILE CARDS */
.mob-cards{display:none}
@media(max-width:600px){.tbl-wrap{display:none}.mob-cards{display:block}}
.mc{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin-bottom:8px;animation:fu .3s ease both}
.mc-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px}
.mc-bd{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.mf{font-size:9.5px;color:var(--mu);font-family:'Fira Mono',monospace;margin-bottom:2px}
.mv{font-size:13px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="logo">
    <div class="logo-mark">📊</div>
    <h1>Data<em>Roma</em> Tracker</h1>
  </div>
  <div class="hdr-right">
    <div class="pill"><span class="dot" id="sdot"></span><span id="stxt">Bağlanıyor…</span></div>
    <button class="btn" onclick="refreshManagers()">↻ Güncelle</button>
    <button class="btn" onclick="clearCache()">🗑 Cache</button>
  </div>
</header>

<div id="banner" class="wait">
  <span class="bico">🔌</span>
  <div class="btxt">Backend kontrol ediliyor…</div>
</div>

<div class="controls">
  <div class="inp-wrap">
    <span class="inp-ico">🔍</span>
    <input type="text" id="search" placeholder="Yönetici ara… (Buffett, Ackman, Tepper…)" />
  </div>
  <div class="sel-wrap">
    <select id="dateFilter">
      <option value="all">📅 Tüm tarihler</option>
      <option value="7">Son 7 gün</option>
      <option value="14">Son 14 gün</option>
      <option value="30">Son 30 gün</option>
      <option value="60">Son 60 gün</option>
      <option value="90">Son 90 gün</option>
    </select>
  </div>
</div>

<div class="fbar" id="fbar"><span class="rcount" id="rcount"></span></div>

<div class="main-grid">
  <div class="mgr-panel">
    <div class="mgr-head">
      <span>YÖNETİCİLER</span>
      <span id="mgCount">—</span>
    </div>
    <div id="mgList">
      <div class="list-loading"><div class="spin"></div><span>Yükleniyor…</span></div>
    </div>
  </div>
  <div class="pf-panel" id="pfPanel">
    <div class="ph-ph"><div class="bi">👈</div><p style="font-size:13px;color:var(--mu)">Bir yönetici seçin</p></div>
  </div>
</div>

</div>
<script>
const API = window.location.origin;
let allManagers=[], filteredManagers=[], activeMgr=null, activeDateFilter='all';
const $=id=>document.getElementById(id);

/* ─ utils ─ */
function daysSince(s){
  if(!s)return 9999;
  const q=s.match(/Q(\d)\s*(20\d{2})/);
  if(q)return Math.floor((Date.now()-new Date(+q[2],(+q[1]-1)*3,1))/864e5);
  const d=new Date(s);
  return isNaN(d)?9999:Math.floor((Date.now()-d)/864e5);
}
function fmtDate(s){if(!s)return'—';const d=new Date(s);return isNaN(d)?s:d.toLocaleDateString('tr-TR',{day:'2-digit',month:'short',year:'numeric'})}
function dotCls(s){const d=daysSince(s);return d<=14?'d-f':d<=60?'d-r':'d-o'}
function tagCls(a){const v=(a||'').toLowerCase();if(v==='add'||v.includes('buy'))return't-buy';if(v==='reduce'||v.includes('sell'))return't-sell';if(v==='new')return't-new';return't-none'}
function chgCls(v){if(!v||v==='—')return'nt';if(v.startsWith('+'))return'up';if(v.startsWith('-'))return'dn';return'nt'}
function initials(n){return n.replace(/[^a-zA-Z\s]/g,'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0].toUpperCase()).join('')}

/* ─ api ─ */
async function api(path,opts={}){
  const c=new AbortController(),t=setTimeout(()=>c.abort(),opts.timeout||20000);
  try{
    const r=await fetch(API+path,{signal:c.signal,...opts});
    clearTimeout(t);
    if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||`HTTP ${r.status}`)}
    return r.json();
  }catch(e){clearTimeout(t);throw e}
}

/* ─ backend check ─ */
async function checkBackend(){
  setBanner('wait','🔌','Backend kontrol ediliyor…');
  try{
    const d=await api('/api/status',{timeout:5000});
    $('sdot').className='dot live';$('stxt').textContent='Bağlı · Canlı';
    setBanner('ok','✅',`Backend aktif &nbsp;·&nbsp; Cache: ${d.cache_count} giriş &nbsp;·&nbsp; TTL: Yönetici ${d.cache_ttl.managers_min}dk / Portföy ${d.cache_ttl.portfolio_min}dk`);
    loadManagers();
  }catch(e){
    $('sdot').className='dot err';$('stxt').textContent='Bağlantı yok';
    setBanner('fail','❌',`Backend yanıt vermiyor. <br><small>Terminal çıktısını kontrol edin.</small>`);
    $('mgList').innerHTML='<div class="list-empty">⚠ Backend çevrimdışı</div>';
  }
}
function setBanner(cls,icon,html){
  $('banner').className=cls;
  $('banner').innerHTML=`<span class="bico">${icon}</span><div class="btxt">${html}</div><button class="btn" onclick="checkBackend()">↻ Yeniden dene</button>`;
}

/* ─ managers ─ */
async function loadManagers(force=false){
  $('mgList').innerHTML='<div class="list-loading"><div class="spin"></div><span>Yükleniyor…</span></div>';
  try{
    const d=await api('/api/managers'+(force?'?refresh=1':''));
    allManagers=d.managers||[];
    $('stxt').textContent=`${allManagers.length} yönetici · ${d.cached?`cache (${Math.round((d.age_s||0)/60)}dk)`:'canlı'}`;
    applyFilters();
  }catch(e){$('mgList').innerHTML=`<div class="list-empty">❌ ${e.message}</div>`}
}
async function refreshManagers(){await loadManagers(true)}
async function clearCache(){
  try{await api('/api/cache/clear',{method:'POST',timeout:5000});$('stxt').textContent='Cache temizlendi';setTimeout(()=>refreshManagers(),300)}
  catch(e){alert('Cache temizlenemedi: '+e.message)}
}

/* ─ filters ─ */
function applyFilters(){
  const q=$('search').value.toLowerCase().trim();
  const d=activeDateFilter==='all'?Infinity:+activeDateFilter;
  filteredManagers=allManagers.filter(m=>m.name.toLowerCase().includes(q)&&daysSince(m.updated||'')<=d);
  renderMgrList();renderFbar();
}
function renderFbar(){
  const labels={'7':'Son 7 gün','14':'Son 14 gün','30':'Son 30 gün','60':'Son 60 gün','90':'Son 90 gün'};
  let h='';
  if(activeDateFilter!=='all')h+=`<span class="badge">📅 ${labels[activeDateFilter]} <button onclick="clearDateFilter()">✕</button></span>`;
  h+=`<span class="rcount">${filteredManagers.length} / ${allManagers.length} yönetici</span>`;
  $('fbar').innerHTML=h;
}
function clearDateFilter(){activeDateFilter='all';$('dateFilter').value='all';applyFilters()}

/* ─ list render ─ */
function renderMgrList(){
  $('mgCount').textContent=filteredManagers.length;
  const el=$('mgList');
  if(!filteredManagers.length){el.innerHTML='<div class="list-empty">🔍 Sonuç yok</div>';return}
  el.innerHTML='';
  filteredManagers.forEach(m=>{
    const d=document.createElement('div');
    d.className='mgr-item'+(activeMgr?.name===m.name?' active':'');
    d.innerHTML=`<div class="av">${initials(m.name)}</div><div class="mi"><div class="mname">${m.name}</div><div class="mdate"><span class="dd ${dotCls(m.updated)}"></span>${m.updated?fmtDate(m.updated):'—'}</div></div>`;
    d.onclick=()=>selectMgr(m);el.appendChild(d);
  });
}

/* ─ portfolio ─ */
function selectMgr(m){activeMgr=m;renderMgrList();loadPortfolio(m)}

async function loadPortfolio(m,force=false){
  $('pfPanel').innerHTML=`<div class="ph-ld"><div class="spin"></div><p style="color:var(--mu);font-size:13px">${m.name} yükleniyor…</p></div>`;
  try{
    const p=new URLSearchParams({url:m.url,name:m.name});
    if(force)p.set('refresh','1');
    const d=await api('/api/portfolio?'+p);
    renderPortfolio(m,d.portfolio,d.cached,d.age_s||0);
  }catch(e){
    $('pfPanel').innerHTML=`<div class="err-card"><strong>⚠ Yüklenemedi</strong><br><code>${e.message}</code><br><button class="retry" onclick="loadPortfolio(activeMgr,true)">↻ Tekrar dene</button></div>`;
  }
}

function renderPortfolio(m,pf,cached,ageS){
  if(!pf?.length){$('pfPanel').innerHTML='<div class="err-card">⚠ Portföy verisi yok.</div>';return}

  const buys=pf.filter(r=>['add','new'].includes((r.activity||'').toLowerCase())).length;
  const sells=pf.filter(r=>(r.activity||'').toLowerCase()==='reduce').length;
  const gain=pf.filter(r=>(r.change||'').startsWith('+')).length;
  const loss=pf.filter(r=>(r.change||'').startsWith('-')).length;
  const dAgo=daysSince(m.updated);
  const fl=dAgo<=7?'🟢 Bu hafta':dAgo<=30?'🟡 Bu ay':dAgo<=90?'🟠 3 ay':'⚪ Eski';
  const ageLbl=cached?`<span class="chip cache">⚡ ${Math.round(ageS/60)}dk önce</span>`:`<span class="chip live">🔴 Canlı</span>`;

  const stats=`<div class="stats-row">
    <div class="sc" style="animation-delay:0s"><div class="sl">Pozisyon</div><div class="sv">${pf.length}</div></div>
    <div class="sc" style="animation-delay:.04s"><div class="sl">Alım/Yeni</div><div class="sv g">${buys}</div></div>
    <div class="sc" style="animation-delay:.08s"><div class="sl">Azaltma</div><div class="sv r">${sells}</div></div>
    <div class="sc" style="animation-delay:.12s"><div class="sl">Kazanan</div><div class="sv g">${gain}</div></div>
    <div class="sc" style="animation-delay:.16s"><div class="sl">Kaybeden</div><div class="sv r">${loss}</div></div>
  </div>`;

  const mx=Math.max(...pf.map(r=>r.pct||0),1);
  const rows=pf.map((r,i)=>`<tr style="animation:fu .28s ease ${i*.02}s both">
    <td><span class="tk">${r.Stock||'—'}</span></td>
    <td class="bar-cell"><div class="bar-wrap"><div class="bar-bg"><div class="bar-fill" style="width:${((r.pct||0)/mx*100).toFixed(1)}%"></div></div><span class="bar-pct">${(r.pct||0).toFixed(1)}%</span></div></td>
    <td><span class="tag ${tagCls(r.activity)}">${r.activity||'None'}</span></td>
    <td><span class="pv">${r.reportedPrice||'—'}</span></td>
    <td><span class="pv">${r.currentPrice||'—'}</span></td>
    <td><span class="pc ${chgCls(r.change)}">${r.change||'—'}</span></td>
  </tr>`).join('');

  const mob=pf.map((r,i)=>`<div class="mc" style="animation-delay:${i*.03}s">
    <div class="mc-hd"><span class="tk" style="font-size:14px">${r.Stock||'—'}</span><span class="tag ${tagCls(r.activity)}">${r.activity||'None'}</span></div>
    <div class="mc-bd">
      <div><div class="mf">Portföy %</div><div class="mv">${(r.pct||0).toFixed(1)}%</div></div>
      <div><div class="mf">Değişim</div><div class="mv pc ${chgCls(r.change)}">${r.change||'—'}</div></div>
      <div><div class="mf">Raporlanan</div><div class="mv">${r.reportedPrice||'—'}</div></div>
      <div><div class="mf">Güncel</div><div class="mv">${r.currentPrice||'—'}</div></div>
    </div>
  </div>`).join('');

  const sn=m.name.includes(' - ')?m.name.split(' - ')[0]:m.name.split(' ').slice(0,3).join(' ');
  $('pfPanel').innerHTML=`${stats}
  <div class="pf-head">
    <h2><em>${sn}</em> Portföyü</h2>
    <div class="chips">
      <span class="chip"><strong>${pf.length}</strong> pozisyon</span>
      <span class="chip live">📅 ${m.updated?fmtDate(m.updated):'—'}</span>
      <span class="chip">${fl}</span>${ageLbl}
      <span class="chip rb" onclick="loadPortfolio(activeMgr,true)">↻ Yenile</span>
    </div>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Hisse</th><th>Portföy %</th><th>Aktivite</th><th>Raporlanan</th><th>Güncel</th><th>+/- Değişim</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>
  <div class="mob-cards">${mob}</div>`;
}

$('search').addEventListener('input',applyFilters);
$('dateFilter').addEventListener('change',e=>{activeDateFilter=e.target.value;applyFilters()});
checkBackend();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

# ── SUNUCU BAŞLATMA ───────────────────────────────────────────
# Render gunicorn ile başlatır: gunicorn dataroma:app
# Lokal test için: python dataroma.py
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
