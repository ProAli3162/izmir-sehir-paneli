from flask import Flask, render_template, jsonify, request, send_from_directory
import requests, json, csv, os
from datetime import datetime

app = Flask(__name__)

BASE_ESHOT = "https://appapi.eshot.gov.tr/api"
BASE_IBB   = "https://openapi.izmir.bel.tr/api"
BASE_GDZ   = "https://www.gdzelektrik.com.tr"
BASE_ACIK  = "https://acikveri.bizizmir.com/api/3/action/datastore_search"
HEADERS    = {"Content-Type": "application/json; charset=UTF-8"}
DIR        = os.path.dirname(__file__)

# ─── CSV Cache ────────────────────────────────────────────────────────────────
_cache = {}

def load_csv(filename, delimiter=None):
    if filename in _cache:
        return _cache[filename]
    rows = []
    try:
        path = os.path.join(DIR, filename)
        with open(path, 'r', encoding='utf-8-sig') as f:
            if not delimiter:
                head = f.readline()
                delimiter = ';' if ';' in head else ','
                f.seek(0)
            reader = csv.DictReader(f, delimiter=delimiter)
            for row in reader:
                # Pre-index search text for performance
                if 'eshot' in filename:
                    # Exclude coordinates from search text to avoid false positives (e.g. searching ID matching lat/lng)
                    search_vals = [str(v) for k, v in row.items() if v and k not in ('ENLEM', 'BOYLAM', 'X', 'Y', 'Latitude', 'Longitude', 'KoorX', 'KoorY')]
                    row['_search_text'] = turkish_norm(" ".join(search_vals))
                rows.append(row)
    except Exception as e:
        print(f"CSV load error ({filename}): {e}")
    _cache[filename] = rows
    return rows

def haversine(lat1, lon1, lat2, lon2):
    import math
    R = 6371  # Earth radius in km
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = (math.sin(dLat / 2) * math.sin(dLat / 2) +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dLon / 2) * math.sin(dLon / 2))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def turkish_norm(s):
    if not s: return ''
    return (s.lower()
            .replace('ı','i').replace('İ','i').replace('I','i')
            .replace('ğ','g').replace('Ğ','g')
            .replace('ü','u').replace('Ü','u')
            .replace('ş','s').replace('Ş','s')
            .replace('ö','o').replace('Ö','o')
            .replace('ç','c').replace('Ç','c'))

def cbs_paginated(endpoint):
    """Fetch all pages from an openapi.izmir CBS endpoint."""
    all_items = []
    seen_ids = set()
    page = 1
    while True:
        try:
            r = requests.get(f"{BASE_IBB}/{endpoint}?page={page}", timeout=25)
            data = r.json()
            
            # Municipal API can return data in various keys
            items = []
            if isinstance(data, list):
                items = data
            else:
                # Try common keys
                items = data.get("onemliyer") or data.get("onemli_yerler") or data.get("onemliyerler") or data.get("records")
                if items is None:
                    # Fallback: find the first list in the dict
                    for val in data.values():
                        if isinstance(val, list):
                            items = val
                            break
                if items is None: items = []
            
            if not items:
                break
            
            added_any = False
            for item in items:
                if not isinstance(item, dict): continue
                item_id = item.get("Id") or item.get("ID") or item.get("Adi") or item.get("ADI") or str(item)
                if item_id not in seen_ids:
                    seen_ids.add(item_id)
                    all_items.append(item)
                    added_any = True
                    
            if not added_any:
                break
                
            toplam_sayfa = data.get("toplam_sayfa_sayisi") or data.get("total_pages")
            if toplam_sayfa and page >= int(toplam_sayfa):
                break
            page += 1
        except Exception:
            break
    return all_items

def acik_veri(resource_id, limit=9999):
    """Fetch from Bizİzmir açık veri API."""
    try:
        r = requests.get(BASE_ACIK, params={"resource_id": resource_id, "limit": limit}, timeout=12)
        data = r.json()
        return data.get("result", {}).get("records", [])
    except Exception:
        return []

# ─── ESHOT Token ──────────────────────────────────────────────────────────────
def eshot_token():
    try:
        r1 = requests.post(f"{BASE_ESHOT}/Transportation/Login",
                           json={"userName":"tur","password":"t@r!"},
                           headers=HEADERS, timeout=8)
        t1 = r1.json().get("data",{}).get("Item1")
        if not t1: return None
        r2 = requests.get(f"{BASE_ESHOT}/TransportationUser/getAnonymousUser",
                          headers={"Authorization":f"Bearer {t1}"}, timeout=8)
        return r2.json().get("data",{}).get("Item1")
    except Exception:
        return None

# ─── Main Route ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

# ─── Static GeoJSON ───────────────────────────────────────────────────────────
@app.route("/api/ilceler.geojson")
def ilceler_geojson():
    return send_from_directory(DIR, 'ilceler.geojson', mimetype='application/json')

@app.route("/api/metro.geojson")
def metro_geojson():
    return send_from_directory(DIR, 'metro.geojson', mimetype='application/json')

@app.route("/api/tramvay.geojson")
def tramvay_geojson():
    return send_from_directory(DIR, 'tramvay.geojson', mimetype='application/json')

@app.route("/api/bisiklet.geojson")
def bisiklet_geojson():
    return send_from_directory(DIR, 'bisikletyollari.geojson', mimetype='application/json')

@app.route("/api/izban.geojson")
def izban_geojson():
    return send_from_directory(DIR, 'izban.geojson', mimetype='application/json')

# ─── ESHOT Hatlar (CSV) ───────────────────────────────────────────────────────
@app.route("/api/eshot/hatlar")
def eshot_hatlar():
    q = request.args.get("q","").strip()
    hatlar = load_csv('eshot-otobus-hatlari.csv')
    if q:
        qn = turkish_norm(q)
        # Priority: Exact match on HAT_NO
        exact = [h for h in hatlar if h.get('HAT_NO') == q]
        if exact: return jsonify({"ok":True, "data": exact})

        words = qn.split()
        def match(row):
            text = row.get('_search_text', '')
            return all(w in text for w in words)
        hatlar = [h for h in hatlar if match(h)]
    return jsonify({"ok":True,"data":hatlar})

# ─── ESHOT Duraklar (CSV) ────────────────────────────────────────────────────
@app.route("/api/eshot/duraklar")
def eshot_duraklar():
    q   = request.args.get("q","").strip()
    hat = request.args.get("hat","").strip()
    yon = request.args.get("yon","").strip()
    limit = int(request.args.get("limit","60"))
    duraklar = load_csv('eshot-otobus-duraklari.csv')
    if hat:
        duraklar = [d for d in duraklar if hat in (d.get('DURAKTAN_GECEN_HATLAR','') or '').split('-')]
    elif q:
        qn = turkish_norm(q)
        # Priority: Exact match on DURAK_ID
        exact = [d for d in duraklar if d.get('DURAK_ID') == q]
        if exact: return jsonify({"ok":True, "data": exact})

        words = qn.split()
        def match(row):
            text = row.get('_search_text', '')
            return all(w in text for w in words)
        duraklar = [d for d in duraklar if match(d)]
    return jsonify({"ok":True,"data":duraklar[:limit]})

# ─── ESHOT Hareket Saatleri (CSV) ─────────────────────────────────────────────
@app.route("/api/eshot/hareketsaatleri")
def eshot_hareket():
    hat_no = request.args.get("hat","").strip()
    tarife = request.args.get("tarife","").strip()  # 1=hafta içi, 2=cumartesi, 3=pazar
    rows = load_csv('eshot-otobus-hareketsaatleri.csv')
    if hat_no:
        rows = [r for r in rows if r.get('HAT_NO','') == hat_no]
    if tarife:
        rows = [r for r in rows if r.get('TARIFE_ID','') == tarife]
    # Deduplicate by SIRA+GIDIS_SAATI
    seen = set()
    unique = []
    for r in rows:
        key = (r.get('TARIFE_ID',''), r.get('SIRA',''), r.get('GIDIS_SAATI',''))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return jsonify({"ok":True,"data":unique})

# ─── ESHOT Güzergah (CSV - sampled) ──────────────────────────────────────────
@app.route("/api/eshot/guzergah")
def eshot_guzergah():
    hat_no = request.args.get("hat","").strip()
    yon    = request.args.get("yon","1").strip()
    if not hat_no:
        return jsonify({"ok":False,"error":"hat parametresi gerekli"})
    rows = load_csv('eshot-otobus-hat-guzergahlari.csv')
    filtered = [r for r in rows if r.get('HAT_NO','') == hat_no and r.get('YON','') == yon]
    # Her 3. noktayı al - çok fazla nokta var
    sampled = filtered[::3]
    coords = [[float(r['BOYLAM']), float(r['ENLEM'])] for r in sampled if r.get('BOYLAM') and r.get('ENLEM')]
    return jsonify({"ok":True,"data":coords,"total":len(filtered)})

# ─── ESHOT Bağlantılar (CSV) ──────────────────────────────────────────────────
@app.route("/api/eshot/baglantitipleri")
def eshot_baglanti_tipleri():
    return jsonify({"ok":True,"data":load_csv('eshot-otobus-baglanti-tipleri.csv')})

@app.route("/api/eshot/baglantilihatlar")
def eshot_baglantili_hatlar():
    tip = request.args.get("tip","").strip()
    rows = load_csv('eshot-otobus-baglantili-hatlar.csv')
    if tip:
        rows = [r for r in rows if r.get('BAGLANTI_TIP_ID','') == tip]
    return jsonify({"ok":True,"data":rows})

# ─── Eczaneler ────────────────────────────────────────────────────────────────
@app.route("/api/nobetcieczaneler")
def nobetci_eczaneler():
    try:
        r = requests.get(f"{BASE_IBB}/ibb/nobetcieczaneler", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/eczaneler")
def eczaneler():
    try:
        r = requests.get(f"{BASE_IBB}/ibb/eczaneler", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── Su ───────────────────────────────────────────────────────────────────────
@app.route("/api/sukesintileri")
def sukesintileri():
    try:
        r = requests.get(f"{BASE_IBB}/izsu/arizakaynaklisukesintileri", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/barajdurum")
def baraj_durum():
    try:
        r = requests.get(f"{BASE_IBB}/izsu/barajdurum", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/gunluksuuretimi")
def gunluk_su():
    try:
        r = requests.get(f"{BASE_IBB}/izsu/gunluksuuretimi", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── Taksi / Tren ─────────────────────────────────────────────────────────────
@app.route("/api/taksiduraklari")
def taksi_duraklari():
    try:
        items = cbs_paginated("ibb/cbs/taksiduraklari")
        return jsonify({"ok":True,"data":items,"toplam":len(items)})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/trengarlari")
def tren_garlari():
    try:
        items = cbs_paginated("ibb/cbs/trengarlari")
        return jsonify({"ok":True,"data":items,"toplam":len(items)})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── Metro ────────────────────────────────────────────────────────────────────
@app.route("/api/metrosefersaatleri")
def metro_sefer():
    try:
        r = requests.get(f"{BASE_IBB}/metro/sefersaatleri", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/metroistasyonlar")
def metro_istasyon():
    try:
        r = requests.get(f"{BASE_IBB}/metro/istasyonlar", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/metro/mesafeler")
def metro_mesafeler():
    return jsonify({"ok":True,"data":load_csv('metro-durak-mesafeleri.csv')})

# ─── İZBAN ────────────────────────────────────────────────────────────────────
@app.route("/api/izbanistasyonlar")
def izban_istasyon():
    try:
        # Use provided Banliyö İstasyonları API
        r = requests.get("https://openapi.izmir.bel.tr/api/izban/istasyonlar", timeout=10)
        return jsonify({"ok": True, "data": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/izbansefersaatleri")
def izban_sefer():
    kalkis = request.args.get("kalkis","")
    varis  = request.args.get("varis","")
    try:
        r = requests.get(f"{BASE_IBB}/izban/sefersaatleri/{kalkis}/{varis}", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/izbanmesafe")
def izban_mesafe():
    try:
        records = acik_veri("53ff5f4b-c514-43aa-a4cd-4a12e03976e1")
        return jsonify({"ok":True,"data":records})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/izbanfiyat/<int:binis>/<int:inis>")
def izban_fiyat(binis, inis):
    try:
        r = requests.get(f"{BASE_IBB}/izban/tutarhesaplama/{binis}/{inis}/0/0", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── Tramvay ──────────────────────────────────────────────────────────────────
@app.route("/api/tramvayhatlar")
def tramvay_hatlar():
    try:
        r = requests.get(f"{BASE_IBB}/tramvay/hatlar", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/tramvay/istasyonlar/<int:sefer_id>")
def tramvay_istasyonlar_sefer(sefer_id):
    try:
        r = requests.get(f"{BASE_IBB}/tramvay/istasyonlar/{sefer_id}", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/tramvayseferler/<int:sefer_id>")
def tramvay_seferler(sefer_id):
    try:
        r = requests.get(f"{BASE_IBB}/tramvay/seferler/{sefer_id}", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/tramvayistasyonlar/konak")
def tramvay_konak():
    records = acik_veri("0385dc35-9b9f-4f95-9369-5e5af4cf83ed")
    return jsonify({"ok":True,"data":records})

@app.route("/api/tramvayistasyonlar/karsiyaka")
def tramvay_karsiyaka():
    records = acik_veri("0ed1ecba-8d75-46b5-8cd8-cdd6e0aa91cd")
    return jsonify({"ok":True,"data":records})

@app.route("/api/tramvayistasyonlar/cigli")
def tramvay_cigli():
    records = acik_veri("3bb87281-7aa2-403d-8ca9-c9a61a717316")
    return jsonify({"ok":True,"data":records})

@app.route("/api/tramvay/detay")
def tramvay_detay():
    hat = request.args.get("hat","konak").lower()
    # CSV data for distances and locations
    files = {
        "konak": ["tramvay-konak-durak-mesafeleri-sag.csv", "tramvay-konak-durak-mesafeleri-sol.csv", "tramvay-konak-konumlar.csv"],
        "karsiyaka": ["tramvay-karsiyaka-durak-mesafeleri.csv", "tramvay-karsiyaka-konumlar.csv"],
        "cigli": ["tramvay-cigili-durak-mesafeleri.csv", "tramvay-cigili-konumlar.csv"]
    }
    target = files.get(hat)
    if not target: return jsonify({"ok":False,"error":"Invalid hat"})
    
    data = {}
    if hat == "konak":
        data["mesafeler_sag"] = load_csv(target[0])
        data["mesafeler_sol"] = load_csv(target[1])
        data["konumlar"] = load_csv(target[2])
    else:
        data["mesafeler"] = load_csv(target[0])
        data["konumlar"] = load_csv(target[1])
    return jsonify({"ok":True,"data":data})

# ─── Vapur ────────────────────────────────────────────────────────────────────
@app.route("/api/vapuriskeleleri")
def vapur_iskeleler():
    try:
        r = requests.get(f"{BASE_IBB}/izdeniz/iskeleler", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/vapurgunleri")
def vapur_gunleri():
    try:
        r = requests.get(f"{BASE_IBB}/izdeniz/gunler", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/vapursaatleri")
def vapur_saatleri():
    kalkis = request.args.get("kalkis")
    varis = request.args.get("varis")
    gun_id = request.args.get("gunId", "1")
    if not kalkis or not varis: return jsonify({"ok":False, "error":"Eksik parametre"})
    url = f"{BASE_IBB}/izdeniz/vapursaatleri/{kalkis}/{varis}/{gun_id}/0"
    try:
        r = requests.get(url, timeout=12)
        return jsonify({"ok":True, "data": r.json()})
    except Exception as e:
        return jsonify({"ok":False, "error": str(e)})

@app.route("/api/iskelesefersaatleri")
def iskele_sefer():
    iskele_id = request.args.get("iskeleId","")
    gun_id    = request.args.get("gunId","1")
    try:
        r = requests.get(f"{BASE_IBB}/izdeniz/iskelesefersaatleri/{iskele_id}/{gun_id}", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── Otobüs Konum ─────────────────────────────────────────────────────────────
@app.route("/api/hatkonumlari/<int:hat_no>")
def hat_konumlari(hat_no):
    try:
        r = requests.get(f"{BASE_IBB}/iztek/hatotobuskonumlari/{hat_no}", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/duragayaklasan/<int:durak_id>")
def duraga_yaklasan(durak_id):
    try:
        r = requests.get(f"{BASE_IBB}/iztek/duragayaklasanotobusler/{durak_id}", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/hattinyaklasan/<int:hat_no>/<int:durak_id>")
def hattin_yaklasan(hat_no, durak_id):
    try:
        r = requests.get(f"{BASE_IBB}/iztek/hattinyaklasanotobusleri/{hat_no}/{durak_id}", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/yakinduraklar")
def yakin_duraklar():
    x = request.args.get("x") # Lng
    y = request.args.get("y") # Lat
    if not x or not y: return jsonify({"ok":False})
    try:
        y_lat = float(y)
        x_lng = float(x)
        duraklar = load_csv('eshot-otobus-duraklari.csv')
        valid_duraklar = []
        for d in duraklar:
            try:
                lat = float(d['ENLEM'])
                lng = float(d['BOYLAM'])
                dist = haversine(y_lat, x_lng, lat, lng)
                d['_dist'] = dist
                valid_duraklar.append(d)
            except Exception:
                pass
        valid_duraklar.sort(key=lambda d: d['_dist'])
        return jsonify({"ok":True, "data": valid_duraklar[:15]})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── Etkinlikler ──────────────────────────────────────────────────────────────
@app.route("/api/etkinlikler")
def etkinlikler():
    try:
        r = requests.get(f"{BASE_IBB}/ibb/kultursanat/etkinlikler", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── CBS Harita Noktaları (generic) ───────────────────────────────────────────
CBS_ENDPOINTS = {
    "pazaryerleri":      "ibb/cbs/pazaryerleri",
    "aciltoplanma":      "ibb/cbs/afetaciltoplanmaalani",
    "plajlar":           "ibb/cbs/plajlar",
    "terminaller":       "ibb/cbs/otobusterminalleri",
    "havaalani":         "ibb/cbs/havaalani",
    "meydanlar":         "ibb/cbs/meydanlar",
    "kaplicalar":        "ibb/cbs/kaplicalar",
    "hamamlar":          "ibb/cbs/hamamlar",
    "fuarlar":           "ibb/cbs/fuar",
    "kuleanit":          "ibb/cbs/kuleanitveheykeller",
    "tarihiyapilar":     "ibb/cbs/tarihiyapilar",
    "tarihicarsivehan":  "ibb/cbs/tarihicarsivehanlar",
    "antikkentler":      "ibb/cbs/antikkentler",
    "muzeler":           "ibb/cbs/muzeler",
    "koskvekonaklar":    "ibb/cbs/koskvekonaklar",
    "kutuphaneler":      "ibb/cbs/kutuphaneler",
    "hastaneler":        "ibb/cbs/hastaneler",
    "ailesagligi":       "ibb/cbs/ailesagligimerkezleri",
    "wizmirnet":         "ibb/cbs/wizmirnetnoktalari",
    "itfaiye":           "ibb/cbs/itfaiyegruplari",
    "noter":             "ibb/cbs/noterler",
    "ptt":               "ibb/cbs/ptt",
    "turizmdanisma":     "ibb/cbs/turizmdanisma",
    "korfezkoy":         "ibb/cbs/korfezvekoylar",
    "nehircay":          "ibb/cbs/nehirvecaylar",
    "goller":            "ibb/cbs/goller",
    "ormanlar":          "ibb/cbs/ormanlar",
    "dagtepe":           "ibb/cbs/dagtepe",
    "adayarimada":       "ibb/cbs/adayarimada",
    "kulturmerkez":      "ibb/cbs/kulturmerkezleri",
    "universiteler":     "ibb/cbs/universiteler",
    "ilkokullar":        "ibb/cbs/ilkokullar",
    "ortaokullar":       "ibb/cbs/ortaokullar",
    "liseler":           "ibb/cbs/liseler",
    "meslekliseleri":    "ibb/cbs/meslekliseleri",
    "anaokul":           "ibb/cbs/anaokullari",
    "etutmerkez":        "ibb/cbs/etutmerkezleri",
    "otoparklar":        "ibb/cbs/otoparklar"
}

# ─── IZTEK Real-time (Bus) ───────────────────────────────────────────────────
@app.route("/api/iztek/hatkonumları/<int:hat_no>")
def iztek_hat_konum(hat_no):
    try:
        r = requests.get(f"https://openapi.izmir.bel.tr/api/iztek/hatotobuskonumlari/{hat_no}", timeout=10)
        return jsonify({"ok": True, "data": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/iztek/durakvaris/<int:durak_id>")
def iztek_durak_varis(durak_id):
    try:
        r = requests.get(f"https://openapi.izmir.bel.tr/api/iztek/duragayaklasanotobusler/{durak_id}", timeout=10)
        return jsonify({"ok": True, "data": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/iztek/hatdurakvaris/<int:hat_no>/<int:durak_id>")
def iztek_hat_durak_varis(hat_no, durak_id):
    try:
        r = requests.get(f"https://openapi.izmir.bel.tr/api/iztek/hattinyaklasanotobusleri/{hat_no}/{durak_id}", timeout=10)
        return jsonify({"ok": True, "data": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/izban/tutar")
def izban_tutar():
    try:
        binis = request.args.get("binis")
        inis = request.args.get("inis")
        aktarma = request.args.get("aktarma", "0")
        htt = request.args.get("htt", "false")
        url = f"https://openapi.izmir.bel.tr/api/izban/tutarhesaplama/{binis}/{inis}/{aktarma}/{htt}"
        r = requests.get(url, timeout=10)
        return jsonify({"ok": True, "data": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/geojson/<name>")
def serve_geojson(name):
    # Only allow safe filenames
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+\.geojson$', name):
        return jsonify({"ok":False, "error":"Invalid file"}), 400
    
    path = os.path.join(DIR, name)
    if not os.path.exists(path):
        return jsonify({"ok":False, "error":"Not found"}), 404
        
    try:
        with open(path, 'r', encoding='utf-8') as f:
            import json
            data = json.load(f)
            return jsonify(data)
    except Exception as e:
        return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/cbs/<kategori>")
def cbs_kategori(kategori):
    endpoint = CBS_ENDPOINTS.get(kategori)
    if not endpoint:
        return jsonify({"ok":False,"error":f"Bilinmeyen kategori: {kategori}"})
    try:
        items = cbs_paginated(endpoint)
        if not items:
            # Try direct call (non-paginated)
            r = requests.get(f"{BASE_IBB}/{endpoint}", timeout=25)
            data = r.json()
            if isinstance(data, list):
                items = data
            else:
                items = data.get("onemliyer") or data.get("onemli_yerler") or data.get("onemliyerler") or data.get("records")
                if items is None:
                    for val in data.values():
                        if isinstance(val, list):
                            items = val
                            break
                if items is None: items = [data]
        
        return jsonify({"ok":True,"data":items,"toplam":len(items)})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── Çevre ────────────────────────────────────────────────────────────────────
@app.route("/api/havakabilitesi")
def hava_kalitesi():
    try:
        r = requests.get(f"{BASE_IBB}/ibb/cevre/havadegerleri", timeout=10)
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

# ─── Açık Veri (Bizİzmir) ─────────────────────────────────────────────────────
@app.route("/api/izmarurünleri")
def izmar_urunler():
    records = acik_veri("d243f67a-73bb-4915-a6a4-355c294fc38d")
    return jsonify({"ok":True,"data":records})

@app.route("/api/banliyobaglantihatlar")
def banliyo_baglanti():
    records = acik_veri("81138188-9e50-476d-a1d0-d069e3ec3878")
    return jsonify({"ok":True,"data":records})

@app.route("/api/iskeledetay")
def iskele_detay():
    records = acik_veri("0bacfaae-6ce7-4055-a48a-adcbc37cfaf8")
    return jsonify({"ok":True,"data":records})

@app.route("/api/engellisarj")
def engelli_sarj():
    records = acik_veri("028f2692-d930-481f-ab27-17a321bd1283")
    return jsonify({"ok":True,"data":records})

@app.route("/api/trafikkameralari")
def trafik_kameralari():
    records = acik_veri("b91cb15d-05c6-45b7-8a75-48e030aad368")
    return jsonify({"ok":True,"data":records})

# ─── GDZ Elektrik ─────────────────────────────────────────────────────────────
@app.route("/api/elektrik")
def elektrik_kesintileri():
    ilce = request.args.get("ilce","").strip()
    gdz_headers = {"Accept":"application/json","User-Agent":"Mozilla/5.0"}
    result = {"Enerjilendirildi":[],"Başladı":[],"Planlandı":[]}
    base_url = f"{BASE_GDZ}/outages_data3/İZMİR/{ilce}/" if ilce else f"{BASE_GDZ}/outages_data3/İZMİR/"
    for page_num in [1, 2]:
        try:
            r = requests.get(f"{base_url}?page={page_num}", timeout=15, headers=gdz_headers)
            if r.status_code != 200 or not r.text.strip(): continue
            page_data = r.json()
            data = page_data.get("data")
            if data is None: continue
            if isinstance(data, list):
                for item in data:
                    status = item.get("Durum","")
                    if "Enerjilend" in status or "enerji" in status.lower():
                        result["Enerjilendirildi"].append(item)
                    elif "Başla" in status or "başla" in status.lower():
                        result["Başladı"].append(item)
                    else:
                        result["Planlandı"].append(item)
            elif isinstance(data, dict):
                for key, items in data.items():
                    if not isinstance(items, list): continue
                    kl = key.lower()
                    if "enerji" in kl: result["Enerjilendirildi"].extend(items)
                    elif "başla" in kl or "basla" in kl: result["Başladı"].extend(items)
                    elif "planla" in kl: result["Planlandı"].extend(items)
                    else: result.setdefault(key, []).extend(items)
        except Exception as e:
            print(f"GDZ page {page_num} err: {e}")
    return jsonify({"ok":True,"data":result})

# ─── Kentkart ─────────────────────────────────────────────────────────────────
@app.route("/api/kentkart/bakiye", methods=["POST"])
def kentkart_bakiye():
    card_no = request.json.get("cardNo","")
    if len(card_no) < 10:
        return jsonify({"ok":False,"error":"Geçersiz kart numarası"})
    try:
        token = eshot_token()
        if not token:
            return jsonify({"ok":False,"error":"Token alınamadı"})
        r = requests.post(f"{BASE_ESHOT}/FavoriteCard/insertUpdate",
                          json={"name":card_no,"cardNo":card_no,"deleted":False,"active":True},
                          headers={**HEADERS,"Authorization":f"Bearer {token}"}, timeout=15)
        result = r.json()
        data_list = result.get("data",[])
        if data_list:
            card = next((c for c in data_list if c.get("name")==card_no or c.get("mifareId")==card_no), data_list[0])
            return jsonify({"ok":True,"data":card})
        return jsonify({"ok":False,"error":"Kart bilgisi bulunamadı"})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
