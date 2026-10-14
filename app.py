"""Berber Randevu Takip - yerel web uygulaması (Flask + SQLite).

Çalıştırma:
    python app.py            -> sadece bu bilgisayardan (http://127.0.0.1:5050)
    python app.py --ag       -> aynı ağdaki telefonlardan da erişilir
"""
import argparse
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.serving import make_server

if getattr(sys, "frozen", False):  # PyInstaller exe
    TEMEL = os.path.dirname(sys.executable)
    STATIK = os.path.join(sys._MEIPASS, "static")
else:
    TEMEL = os.path.dirname(os.path.abspath(__file__))
    STATIK = os.path.join(TEMEL, "static")

sys.path.insert(0, TEMEL if not getattr(sys, "frozen", False) else sys._MEIPASS)
from version import VERSION as SURUM  # noqa: E402

# Tüm veriler exe'nin yanındaki tek bir klasörde durur (masaüstü dağınık kalmasın):
#   BerberRandevu_Veri/berber.db, BerberRandevu_Veri/Yedekler/..., BerberRandevu_Veri/pencere_profil
DB_YOLU = os.environ.get("BERBER_DB", os.path.join(TEMEL, "BerberRandevu_Veri", "berber.db"))
DURUMLAR = ("bekliyor", "geldi", "gelmedi", "iptal")
YEDEK_DESEN = re.compile(r"^berber_\d{4}-\d{2}-\d{2}_\d{6}\.db$")

app = Flask(__name__, static_folder=STATIK, static_url_path="/static")


# ---------------------------------------------------------------- veritabanı
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_YOLU)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def kapat(_exc):
    c = g.pop("db", None)
    if c is not None:
        c.close()


SEMA = """
CREATE TABLE IF NOT EXISTS berberler (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ad TEXT NOT NULL,
    aktif INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS hizmetler (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ad TEXT NOT NULL,
    sure_dk INTEGER NOT NULL DEFAULT 30,
    fiyat REAL NOT NULL DEFAULT 0,
    aktif INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS randevular (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    berber_id INTEGER NOT NULL REFERENCES berberler(id),
    musteri_ad TEXT NOT NULL,
    telefon TEXT NOT NULL DEFAULT '',
    tarih TEXT NOT NULL,          -- YYYY-MM-DD
    baslangic TEXT NOT NULL,      -- HH:MM
    sure_dk INTEGER NOT NULL,
    hizmetler TEXT NOT NULL DEFAULT '',   -- "Saç;Sakal" (o günkü ad)
    ucret REAL NOT NULL DEFAULT 0,
    durum TEXT NOT NULL DEFAULT 'bekliyor',
    notu TEXT NOT NULL DEFAULT '',
    olusturma TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rand_tarih ON randevular(tarih, berber_id);
CREATE TABLE IF NOT EXISTS ayarlar (
    anahtar TEXT PRIMARY KEY,
    deger TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kasa_cikis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tarih TEXT NOT NULL,          -- YYYY-MM-DD
    saat TEXT NOT NULL,           -- HH:MM
    tur TEXT NOT NULL,            -- 'gider' (dükkan gideri) | 'avans' (personel avansı)
    berber_id INTEGER REFERENCES berberler(id),   -- parayı kim aldı
    kategori TEXT NOT NULL DEFAULT '',
    aciklama TEXT NOT NULL DEFAULT '',
    tutar REAL NOT NULL,
    olusturma TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kasa_tarih ON kasa_cikis(tarih);
"""

VARSAYILAN_AYAR = {"acilis": "09:00", "kapanis": "21:00", "adim_dk": "30", "dukkan_adi": "Berber Randevu",
                   "yedek_adet": "14"}


def veri_dizini():
    return os.path.dirname(DB_YOLU)


def yedek_dizini():
    return os.path.join(veri_dizini(), "Yedekler")


def veri_hazirla():
    """Veri klasörünü açar; eski yerleşimi (db exe'nin yanında) yeni klasöre taşır."""
    os.makedirs(veri_dizini(), exist_ok=True)
    if "BERBER_DB" not in os.environ:
        eski = os.path.join(TEMEL, "berber.db")
        if os.path.exists(eski) and not os.path.exists(DB_YOLU):
            shutil.move(eski, DB_YOLU)
        shutil.rmtree(os.path.join(TEMEL, "pencere_profil"), ignore_errors=True)   # eski konum


def baslat_db():
    veri_hazirla()
    c = sqlite3.connect(DB_YOLU)
    c.executescript(SEMA)
    if "hakedis_oran" not in [r[1] for r in c.execute("PRAGMA table_info(berberler)")]:
        c.execute("ALTER TABLE berberler ADD COLUMN hakedis_oran REAL NOT NULL DEFAULT 50")
    for k, v in VARSAYILAN_AYAR.items():
        c.execute("INSERT OR IGNORE INTO ayarlar(anahtar, deger) VALUES (?, ?)", (k, v))
    if c.execute("SELECT COUNT(*) FROM berberler").fetchone()[0] == 0:
        c.execute("INSERT INTO berberler(ad) VALUES ('Berber 1')")
    if c.execute("SELECT COUNT(*) FROM hizmetler").fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO hizmetler(ad, sure_dk, fiyat) VALUES (?, ?, ?)",
            [("Saç Kesimi", 30, 300), ("Sakal", 15, 150), ("Saç + Sakal", 45, 400)],
        )
    c.commit()
    c.close()


# ------------------------------------------------------------------- yedekleme
EXE_YEDEK_ADET = 3     # güncellemede kenara alınan eski exe'lerden en fazla bu kadarı tutulur


def yedek_adet_oku():
    try:
        c = sqlite3.connect(DB_YOLU)
        r = c.execute("SELECT deger FROM ayarlar WHERE anahtar='yedek_adet'").fetchone()
        c.close()
        return max(3, min(60, int(r[0]))) if r else 14
    except Exception:
        return 14


def yedek_dosyalari():
    """Veritabanı yedeklerinin adları, en yeniden eskiye."""
    d = yedek_dizini()
    if not os.path.isdir(d):
        return []
    return sorted((f for f in os.listdir(d) if YEDEK_DESEN.match(f)), reverse=True)


def exe_yedek_dosyalari():
    d = yedek_dizini()
    if not os.path.isdir(d):
        return []
    adlar = [f for f in os.listdir(d) if f.startswith("BerberRandevu_") and f.endswith(".exe")]
    return sorted(adlar, key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)


def yedek_temizle():
    """Yeni yedekten sonra: ayarlardaki adedi aşan EN ESKİ yedekler silinir (exe yedekleri için son 3)."""
    d = yedek_dizini()
    for f in yedek_dosyalari()[yedek_adet_oku():]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    for f in exe_yedek_dosyalari()[EXE_YEDEK_ADET:]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass


def yedek_al():
    """Çalışan veritabanının tutarlı bir kopyasını Yedekler klasörüne alır, sonra eskileri temizler."""
    os.makedirs(yedek_dizini(), exist_ok=True)
    ad = datetime.now().strftime("berber_%Y-%m-%d_%H%M%S.db")
    kaynak = sqlite3.connect(DB_YOLU)
    hedef = sqlite3.connect(os.path.join(yedek_dizini(), ad))
    try:
        kaynak.backup(hedef)
    finally:
        hedef.close()
        kaynak.close()
    yedek_temizle()
    return ad


def bugun_yedek_var():
    onek = datetime.now().strftime("berber_%Y-%m-%d_")
    return any(f.startswith(onek) for f in yedek_dosyalari())


def otomatik_yedek_dongusu():
    """Program açık kaldıkça günde en az bir yedek (açılışta + gün değişince)."""
    while True:
        try:
            if not bugun_yedek_var():
                yedek_al()
        except Exception:
            pass
        time.sleep(1800)


def yedek_geri_yukle(ad):
    if not YEDEK_DESEN.match(ad) or not os.path.exists(os.path.join(yedek_dizini(), ad)):
        raise ValueError("Yedek dosyası bulunamadı.")
    gecici = os.path.join(veri_dizini(), "geri_yukle_gecici.db")
    shutil.copy2(os.path.join(yedek_dizini(), ad), gecici)   # temizlik seçilen yedeği silmesin diye kopya
    try:
        yedek_al()                                           # geri yüklemeden önce mevcut hali de yedekle
        kaynak = sqlite3.connect(gecici)
        hedef = sqlite3.connect(DB_YOLU)
        try:
            kaynak.backup(hedef)
        finally:
            kaynak.close()
            hedef.close()
    finally:
        try:
            os.remove(gecici)
        except OSError:
            pass
    baslat_db()


# ------------------------------------------------------------------ yardımcı
def satirlar(cur):
    return [dict(r) for r in cur.fetchall()]


def saat_dk(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def dk_saat(d):
    return f"{d // 60:02d}:{d % 60:02d}"


def hata(mesaj, kod=400):
    return jsonify({"hata": mesaj}), kod


def ayar_ham():
    return {r["anahtar"]: r["deger"] for r in db().execute("SELECT * FROM ayarlar")}


def ayarlar():
    """Dışarı verilen ayarlar: PIN bilgileri asla dahil edilmez."""
    return {k: v for k, v in ayar_ham().items() if not k.startswith("pin_")}


def ayar_yaz(k, v):
    db().execute("INSERT OR REPLACE INTO ayarlar(anahtar, deger) VALUES (?, ?)", (k, v))
    db().commit()


# ------------------------------------------------------------ yetkili (PIN) girişi
TOKEN_SURE = 30 * 60              # 30 dk işlem yapılmazsa oturum kapanır
_tokenlar = {}                    # jeton -> bitiş zamanı
_kilit = {"hata": 0, "bitis": 0.0}


def pin_hash(pin, tuz_hex):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(tuz_hex), 120_000).hex()


def pin_var():
    return bool(ayar_ham().get("pin_hash"))


def pin_belirle(pin):
    tuz = secrets.token_hex(16)
    ayar_yaz("pin_salt", tuz)
    ayar_yaz("pin_hash", pin_hash(pin, tuz))


def pin_dogru(pin):
    a = ayar_ham()
    return bool(a.get("pin_hash")) and hmac.compare_digest(pin_hash(pin, a["pin_salt"]), a["pin_hash"])


def token_uret():
    t = secrets.token_urlsafe(24)
    _tokenlar[t] = time.time() + TOKEN_SURE
    return t


def yetkili_mi():
    t = request.headers.get("X-Yetkili", "")
    bit = _tokenlar.get(t)
    if bit and bit > time.time():
        _tokenlar[t] = time.time() + TOKEN_SURE
        return True
    _tokenlar.pop(t, None)
    return False


def yetkili_gerekli(f):
    @functools.wraps(f)
    def sar(*a, **k):
        if not yetkili_mi():
            return jsonify({"hata": "Bu işlem için yetkili girişi gerekli.", "kod": "yetki"}), 401
        return f(*a, **k)
    return sar


def pin_gecerli_mi(pin):
    return pin.isdigit() and 4 <= len(pin) <= 12


def cakisma_var(berber_id, tarih, baslangic, sure_dk, haric_id=None):
    bas = saat_dk(baslangic)
    bit = bas + sure_dk
    q = "SELECT id, baslangic, sure_dk, musteri_ad FROM randevular WHERE berber_id=? AND tarih=? AND durum NOT IN ('iptal')"
    for r in db().execute(q, (berber_id, tarih)):
        if haric_id and r["id"] == haric_id:
            continue
        b2 = saat_dk(r["baslangic"])
        if bas < b2 + r["sure_dk"] and b2 < bit:
            return r
    return None


def dogrula_randevu(v, haric_id=None):
    """Gelen JSON'u doğrular; (temiz_dict, hata_metni) döner."""
    try:
        berber_id = int(v.get("berber_id"))
    except (TypeError, ValueError):
        return None, "Berber seçilmedi."
    if not db().execute("SELECT 1 FROM berberler WHERE id=? AND aktif=1", (berber_id,)).fetchone():
        return None, "Berber bulunamadı."
    musteri = (v.get("musteri_ad") or "").strip()
    if not musteri:
        return None, "Müşteri adı gerekli."
    try:
        datetime.strptime(v.get("tarih", ""), "%Y-%m-%d")
        datetime.strptime(v.get("baslangic", ""), "%H:%M")
    except ValueError:
        return None, "Tarih veya saat geçersiz."
    try:
        sure = int(v.get("sure_dk"))
    except (TypeError, ValueError):
        return None, "Süre geçersiz."
    if sure < 5 or sure > 600:
        return None, "Süre 5-600 dakika arasında olmalı."
    try:
        ucret = float(v.get("ucret") or 0)
    except (TypeError, ValueError):
        return None, "Ücret geçersiz."
    durum = v.get("durum") or "bekliyor"
    if durum not in DURUMLAR:
        return None, "Durum geçersiz."
    if saat_dk(v["baslangic"]) + sure > 24 * 60:
        return None, "Randevu gece yarısını aşıyor."
    if durum != "iptal":
        c = cakisma_var(berber_id, v["tarih"], v["baslangic"], sure, haric_id)
        if c:
            return None, f"Çakışma: {c['baslangic']} saatinde {c['musteri_ad']} ile randevu var."
    return {
        "berber_id": berber_id,
        "musteri_ad": musteri,
        "telefon": (v.get("telefon") or "").strip(),
        "tarih": v["tarih"],
        "baslangic": v["baslangic"],
        "sure_dk": sure,
        "hizmetler": (v.get("hizmetler") or "").strip(),
        "ucret": ucret,
        "durum": durum,
        "notu": (v.get("notu") or "").strip(),
    }, None


# --------------------------------------------------------------------- sayfa
@app.get("/")
def anasayfa():
    return send_from_directory(STATIK, "index.html")


@app.get("/api/yetkili/durum")
def y_durum():
    return jsonify({"pin_var": pin_var(), "acik": yetkili_mi()})


@app.post("/api/yetkili/kur")
def y_kur():
    """İlk kurulum: PIN henüz yoksa belirlenir. PIN varken çalışmaz."""
    if pin_var():
        return hata("PIN zaten belirlenmiş.", 403)
    pin = str((request.get_json(force=True, silent=True) or {}).get("pin", "")).strip()
    if not pin_gecerli_mi(pin):
        return hata("PIN 4-12 haneli rakam olmalı.")
    pin_belirle(pin)
    return jsonify({"token": token_uret()})


@app.post("/api/yetkili/giris")
def y_giris():
    kalan = _kilit["bitis"] - time.time()
    if kalan > 0:
        return hata(f"Çok fazla hatalı deneme. {int(kalan) + 1} sn sonra tekrar deneyin.", 429)
    if not pin_var():
        return hata("Önce PIN belirlenmeli.", 409)
    pin = str((request.get_json(force=True, silent=True) or {}).get("pin", "")).strip()
    if pin_dogru(pin):
        _kilit["hata"] = 0
        return jsonify({"token": token_uret()})
    _kilit["hata"] += 1
    if _kilit["hata"] >= 5:
        _kilit.update(hata=0, bitis=time.time() + 30)
    return hata("PIN yanlış.", 403)


@app.post("/api/yetkili/cikis")
def y_cikis():
    _tokenlar.pop(request.headers.get("X-Yetkili", ""), None)
    return jsonify({"tamam": True})


@app.post("/api/yetkili/pin")
@yetkili_gerekli
def y_pin_degistir():
    v = request.get_json(force=True, silent=True) or {}
    if not pin_dogru(str(v.get("eski", "")).strip()):
        return hata("Eski PIN yanlış.", 403)
    yeni = str(v.get("yeni", "")).strip()
    if not pin_gecerli_mi(yeni):
        return hata("Yeni PIN 4-12 haneli rakam olmalı.")
    pin_belirle(yeni)
    gecerli = request.headers.get("X-Yetkili", "")
    for t in [t for t in _tokenlar if t != gecerli]:   # diğer oturumları kapat
        _tokenlar.pop(t, None)
    return jsonify({"tamam": True})


# ------------------------------------------------------------------- ayarlar
@app.get("/api/ayarlar")
def ayar_getir():
    return jsonify(ayarlar())


@app.put("/api/ayarlar")
@yetkili_gerekli
def ayar_kaydet():
    v = request.get_json(force=True, silent=True) or {}
    yeni = ayarlar()
    for k in ("acilis", "kapanis", "dukkan_adi"):
        if k in v:
            yeni[k] = str(v[k]).strip()
    if "adim_dk" in v:
        try:
            a = int(v["adim_dk"])
        except (TypeError, ValueError):
            return hata("Adım geçersiz.")
        if a not in (10, 15, 20, 30, 60):
            return hata("Adım 10, 15, 20, 30 veya 60 olabilir.")
        yeni["adim_dk"] = str(a)
    if "yedek_adet" in v:
        try:
            n = int(v["yedek_adet"])
        except (TypeError, ValueError):
            return hata("Yedek adedi sayı olmalı.")
        if not 3 <= n <= 60:
            return hata("Yedek adedi 3 ile 60 arasında olmalı.")
        yeni["yedek_adet"] = str(n)
    try:
        if saat_dk(yeni["acilis"]) >= saat_dk(yeni["kapanis"]):
            return hata("Açılış saati kapanıştan önce olmalı.")
    except ValueError:
        return hata("Saat biçimi SS:DD olmalı.")
    for k, val in yeni.items():
        db().execute("INSERT OR REPLACE INTO ayarlar(anahtar, deger) VALUES (?, ?)", (k, val))
    db().commit()
    return jsonify(yeni)


# ------------------------------------------------------------------ berberler
def oran_oku(v, varsayilan=None):
    """Hakediş oranı 0-100 arası sayı olmalı."""
    ham = v.get("hakedis_oran", varsayilan)
    try:
        o = float(ham)
    except (TypeError, ValueError):
        return None, "Hakediş oranı sayı olmalı."
    if not 0 <= o <= 100:
        return None, "Hakediş oranı 0 ile 100 arasında olmalı."
    return o, None


@app.get("/api/berberler")
def berber_liste():
    # Hakediş oranı sadece yetkili girişinde görünür.
    kolon = "id, ad, aktif, hakedis_oran" if yetkili_mi() else "id, ad, aktif"
    return jsonify(satirlar(db().execute(f"SELECT {kolon} FROM berberler WHERE aktif=1 ORDER BY id")))


@app.post("/api/berberler")
@yetkili_gerekli
def berber_ekle():
    v = request.get_json(force=True, silent=True) or {}
    ad = (v.get("ad") or "").strip()
    if not ad:
        return hata("Ad gerekli.")
    oran, e = oran_oku(v, 50)
    if e:
        return hata(e)
    cur = db().execute("INSERT INTO berberler(ad, hakedis_oran) VALUES (?, ?)", (ad, oran))
    db().commit()
    return jsonify({"id": cur.lastrowid, "ad": ad, "aktif": 1, "hakedis_oran": oran}), 201


@app.put("/api/berberler/<int:bid>")
@yetkili_gerekli
def berber_guncelle(bid):
    v = request.get_json(force=True, silent=True) or {}
    ad = (v.get("ad") or "").strip()
    if not ad:
        return hata("Ad gerekli.")
    oran, e = oran_oku(v, 50)
    if e:
        return hata(e)
    db().execute("UPDATE berberler SET ad=?, hakedis_oran=? WHERE id=?", (ad, oran, bid))
    db().commit()
    return jsonify({"id": bid, "ad": ad, "hakedis_oran": oran})


@app.delete("/api/berberler/<int:bid>")
@yetkili_gerekli
def berber_sil(bid):
    # Geçmiş randevular bozulmasın diye silmek yerine pasife alınır.
    if db().execute("SELECT COUNT(*) FROM berberler WHERE aktif=1").fetchone()[0] <= 1:
        return hata("En az bir berber kalmalı.")
    gel = db().execute(
        "SELECT COUNT(*) FROM randevular WHERE berber_id=? AND tarih>=? AND durum='bekliyor'",
        (bid, datetime.now().strftime("%Y-%m-%d")),
    ).fetchone()[0]
    if gel:
        return hata(f"Bu berberin ileri tarihli {gel} bekleyen randevusu var. Önce iptal edin veya taşıyın.")
    db().execute("UPDATE berberler SET aktif=0 WHERE id=?", (bid,))
    db().commit()
    return jsonify({"tamam": True})


# ------------------------------------------------------------------ hizmetler
@app.get("/api/hizmetler")
def hizmet_liste():
    return jsonify(satirlar(db().execute("SELECT * FROM hizmetler WHERE aktif=1 ORDER BY ad")))


def hizmet_dogrula(v):
    ad = (v.get("ad") or "").strip()
    if not ad:
        return None, "Hizmet adı gerekli."
    try:
        sure = int(v.get("sure_dk"))
        fiyat = float(v.get("fiyat"))
    except (TypeError, ValueError):
        return None, "Süre ve fiyat sayı olmalı."
    if sure < 5 or sure > 600 or fiyat < 0:
        return None, "Süre 5-600 dk, fiyat 0 veya üstü olmalı."
    return (ad, sure, fiyat), None


@app.post("/api/hizmetler")
@yetkili_gerekli
def hizmet_ekle():
    t, e = hizmet_dogrula(request.get_json(force=True, silent=True) or {})
    if e:
        return hata(e)
    cur = db().execute("INSERT INTO hizmetler(ad, sure_dk, fiyat) VALUES (?,?,?)", t)
    db().commit()
    return jsonify({"id": cur.lastrowid, "ad": t[0], "sure_dk": t[1], "fiyat": t[2], "aktif": 1}), 201


@app.put("/api/hizmetler/<int:hid>")
@yetkili_gerekli
def hizmet_guncelle(hid):
    t, e = hizmet_dogrula(request.get_json(force=True, silent=True) or {})
    if e:
        return hata(e)
    db().execute("UPDATE hizmetler SET ad=?, sure_dk=?, fiyat=? WHERE id=?", (*t, hid))
    db().commit()
    return jsonify({"id": hid, "ad": t[0], "sure_dk": t[1], "fiyat": t[2]})


@app.delete("/api/hizmetler/<int:hid>")
@yetkili_gerekli
def hizmet_sil(hid):
    db().execute("UPDATE hizmetler SET aktif=0 WHERE id=?", (hid,))
    db().commit()
    return jsonify({"tamam": True})


# ------------------------------------------------------------------ randevular
@app.get("/api/randevular")
def randevu_liste():
    tarih = request.args.get("tarih")
    if not tarih:
        return hata("tarih gerekli.")
    cur = db().execute(
        "SELECT * FROM randevular WHERE tarih=? ORDER BY baslangic", (tarih,)
    )
    return jsonify(satirlar(cur))


@app.post("/api/randevular")
def randevu_ekle():
    t, e = dogrula_randevu(request.get_json(force=True, silent=True) or {})
    if e:
        return hata(e, 409 if e.startswith("Çakışma") else 400)
    t["olusturma"] = datetime.now().isoformat(timespec="seconds")
    kolon = ", ".join(t)
    cur = db().execute(f"INSERT INTO randevular({kolon}) VALUES ({', '.join('?' * len(t))})", tuple(t.values()))
    db().commit()
    return jsonify({"id": cur.lastrowid, **t}), 201


@app.put("/api/randevular/<int:rid>")
def randevu_guncelle(rid):
    if not db().execute("SELECT 1 FROM randevular WHERE id=?", (rid,)).fetchone():
        return hata("Randevu bulunamadı.", 404)
    t, e = dogrula_randevu(request.get_json(force=True, silent=True) or {}, haric_id=rid)
    if e:
        return hata(e, 409 if e.startswith("Çakışma") else 400)
    atama = ", ".join(f"{k}=?" for k in t)
    db().execute(f"UPDATE randevular SET {atama} WHERE id=?", (*t.values(), rid))
    db().commit()
    return jsonify({"id": rid, **t})


@app.patch("/api/randevular/<int:rid>/durum")
def randevu_durum(rid):
    d = (request.get_json(force=True, silent=True) or {}).get("durum")
    if d not in DURUMLAR:
        return hata("Durum geçersiz.")
    r = db().execute("SELECT * FROM randevular WHERE id=?", (rid,)).fetchone()
    if not r:
        return hata("Randevu bulunamadı.", 404)
    if r["durum"] == "iptal" and d != "iptal":
        c = cakisma_var(r["berber_id"], r["tarih"], r["baslangic"], r["sure_dk"], rid)
        if c:
            return hata(f"Çakışma: {c['baslangic']} saatinde {c['musteri_ad']} ile randevu var.", 409)
    db().execute("UPDATE randevular SET durum=? WHERE id=?", (d, rid))
    db().commit()
    return jsonify({"id": rid, "durum": d})


@app.delete("/api/randevular/<int:rid>")
def randevu_sil(rid):
    db().execute("DELETE FROM randevular WHERE id=?", (rid,))
    db().commit()
    return jsonify({"tamam": True})


# ------------------------------------------------------------ müşteri arama
@app.get("/api/musteriler")
def musteri_ara():
    """Geçmiş randevulardan müşteri öner (ad veya telefona göre)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    cur = db().execute(
        """SELECT musteri_ad AS ad, telefon, COUNT(*) AS ziyaret, MAX(tarih) AS son
           FROM randevular
           WHERE (musteri_ad LIKE ? OR telefon LIKE ?) AND durum != 'iptal'
           GROUP BY musteri_ad, telefon
           ORDER BY ziyaret DESC LIMIT 8""",
        (f"%{q}%", f"%{q}%"),
    )
    return jsonify(satirlar(cur))


# ---------------------------------------------------------------------- özet
@app.get("/api/ozet")
def ozet():
    bas = request.args.get("bas")
    bit = request.args.get("bit") or bas
    if not bas:
        return hata("bas gerekli.")
    cur = db().execute(
        """SELECT b.id AS berber_id, b.ad AS berber,
                  SUM(CASE WHEN r.durum='geldi' THEN 1 ELSE 0 END) AS geldi,
                  SUM(CASE WHEN r.durum='bekliyor' THEN 1 ELSE 0 END) AS bekliyor,
                  SUM(CASE WHEN r.durum='gelmedi' THEN 1 ELSE 0 END) AS gelmedi,
                  SUM(CASE WHEN r.durum='iptal' THEN 1 ELSE 0 END) AS iptal,
                  COALESCE(SUM(CASE WHEN r.durum='geldi' THEN r.ucret END), 0) AS kazanc,
                  COALESCE(SUM(CASE WHEN r.durum='bekliyor' THEN r.ucret END), 0) AS beklenen
           FROM berberler b
           LEFT JOIN randevular r ON r.berber_id=b.id AND r.tarih BETWEEN ? AND ?
           WHERE b.aktif=1 OR r.id IS NOT NULL
           GROUP BY b.id ORDER BY b.id""",
        (bas, bit),
    )
    satir = satirlar(cur)
    toplam = {k: sum(s[k] for s in satir) for k in ("geldi", "bekliyor", "gelmedi", "iptal", "kazanc", "beklenen")}
    return jsonify({"toplam": toplam})   # berber bazlı kırılım hakediş ekranında (yetkili)


# ---------------------------------------------------------------------- kasa
KASA_TURLER = ("gider", "avans")


def kasa_satir(r):
    return dict(r)


@app.get("/api/kasa")
def kasa_liste():
    """Gün içi kasa çıkışları (PIN gerekmez: personel girer). tarih verilmezse bugün."""
    tarih = request.args.get("tarih") or datetime.now().strftime("%Y-%m-%d")
    cur = db().execute(
        """SELECT k.id, k.tarih, k.saat, k.tur, k.berber_id, b.ad AS berber, k.kategori, k.aciklama, k.tutar
           FROM kasa_cikis k LEFT JOIN berberler b ON b.id=k.berber_id
           WHERE k.tarih=? ORDER BY k.saat DESC, k.id DESC""",
        (tarih,),
    )
    liste = satirlar(cur)
    toplam = {
        "gider": round(sum(x["tutar"] for x in liste if x["tur"] == "gider"), 2),
        "avans": round(sum(x["tutar"] for x in liste if x["tur"] == "avans"), 2),
    }
    toplam["toplam"] = round(toplam["gider"] + toplam["avans"], 2)
    return jsonify({"tarih": tarih, "liste": liste, "toplam": toplam})


@app.post("/api/kasa")
def kasa_ekle():
    v = request.get_json(silent=True) or {}
    tur = v.get("tur")
    if tur not in KASA_TURLER:
        return hata("Tür seçin: dükkan gideri veya personel avansı.")
    try:
        tutar = round(float(v.get("tutar")), 2)
    except (TypeError, ValueError):
        return hata("Tutarı yazın.")
    if not (0 < tutar < 10_000_000):
        return hata("Tutar sıfırdan büyük olmalı.")
    berber_id = None
    if v.get("berber_id") not in (None, ""):
        try:
            berber_id = int(v.get("berber_id"))
        except (TypeError, ValueError):
            return hata("Kişi geçersiz.")
        if not db().execute("SELECT 1 FROM berberler WHERE id=?", (berber_id,)).fetchone():
            return hata("Kişi bulunamadı.")
    if tur == "avans" and berber_id is None:
        return hata("Parayı kimin aldığını seçin.")
    kategori = str(v.get("kategori") or "").strip()[:40]
    aciklama = str(v.get("aciklama") or "").strip()[:200]
    simdi = datetime.now()
    tarih, saat = simdi.strftime("%Y-%m-%d"), simdi.strftime("%H:%M")
    if (v.get("tarih") or v.get("saat")) and yetkili_mi():   # geçmişe kayıt sadece yetkili
        tarih = v.get("tarih") or tarih
        saat = v.get("saat") or saat
    cur = db().execute(
        "INSERT INTO kasa_cikis(tarih,saat,tur,berber_id,kategori,aciklama,tutar,olusturma) VALUES(?,?,?,?,?,?,?,?)",
        (tarih, saat, tur, berber_id, kategori, aciklama, tutar, simdi.isoformat(timespec="seconds")),
    )
    db().commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.delete("/api/kasa/<int:kid>")
def kasa_sil(kid):
    """Yanlış girişi düzeltmek için: bugün ve son 10 dk içinde girilen herkes silebilir; eskisi için PIN."""
    r = db().execute("SELECT olusturma, tarih FROM kasa_cikis WHERE id=?", (kid,)).fetchone()
    if not r:
        return hata("Kayıt yok.", 404)
    try:
        yeni = (datetime.now() - datetime.fromisoformat(r["olusturma"])).total_seconds() < 600
    except ValueError:
        yeni = False
    if not yeni and not yetkili_mi():
        return jsonify({"hata": "Eski kaydı silmek için yetkili girişi gerekli.", "kod": "yetki"}), 401
    db().execute("DELETE FROM kasa_cikis WHERE id=?", (kid,))
    db().commit()
    return jsonify({"ok": True})


# ------------------------------------------------------------------- hakediş
@app.get("/api/hakedis")
@yetkili_gerekli
def hakedis():
    """Sadece 'geldi' işaretli randevular sayılır. Hakediş = ciro x berberin oranı.
    Ödenecek = hakediş - dönem içindeki avanslar. Dükkan gideri ayrıca gösterilir."""
    bas = request.args.get("bas")
    bit = request.args.get("bit") or bas
    if not bas:
        return hata("bas gerekli.")
    cur = db().execute(
        """SELECT b.id AS berber_id, b.ad AS berber, b.hakedis_oran AS oran,
                  COUNT(r.id) AS adet, COALESCE(SUM(r.ucret), 0) AS ciro
           FROM berberler b
           LEFT JOIN randevular r ON r.berber_id=b.id AND r.durum='geldi' AND r.tarih BETWEEN ? AND ?
           WHERE b.aktif=1 OR r.id IS NOT NULL
           GROUP BY b.id ORDER BY b.id""",
        (bas, bit),
    )
    satir = satirlar(cur)
    avans = {r["berber_id"]: r["t"] for r in db().execute(
        "SELECT berber_id, SUM(tutar) AS t FROM kasa_cikis WHERE tur='avans' AND tarih BETWEEN ? AND ? GROUP BY berber_id",
        (bas, bit))}
    for s in satir:
        s["hakedis"] = round(s["ciro"] * s["oran"] / 100, 2)
        s["dukkan"] = round(s["ciro"] - s["hakedis"], 2)
        s["avans"] = round(avans.get(s["berber_id"], 0) or 0, 2)
        s["odenecek"] = round(s["hakedis"] - s["avans"], 2)
    toplam = {k: round(sum(s[k] for s in satir), 2) for k in ("adet", "ciro", "hakedis", "dukkan", "avans", "odenecek")}
    gider = db().execute(
        "SELECT COALESCE(SUM(tutar),0) FROM kasa_cikis WHERE tur='gider' AND tarih BETWEEN ? AND ?", (bas, bit)
    ).fetchone()[0]
    toplam["gider"] = round(gider, 2)
    toplam["dukkan_net"] = round(toplam["dukkan"] - toplam["gider"], 2)
    return jsonify({"berberler": satir, "toplam": toplam})


@app.get("/api/hakedis/detay")
@yetkili_gerekli
def hakedis_detay():
    bas = request.args.get("bas")
    bit = request.args.get("bit") or bas
    try:
        bid = int(request.args.get("berber_id"))
    except (TypeError, ValueError):
        return hata("berber_id gerekli.")
    if not bas:
        return hata("bas gerekli.")
    isler = satirlar(db().execute(
        """SELECT tarih, baslangic, musteri_ad, hizmetler, ucret FROM randevular
           WHERE berber_id=? AND durum='geldi' AND tarih BETWEEN ? AND ?
           ORDER BY tarih, baslangic""",
        (bid, bas, bit),
    ))
    avanslar = satirlar(db().execute(
        """SELECT tarih, saat, aciklama, tutar FROM kasa_cikis
           WHERE berber_id=? AND tur='avans' AND tarih BETWEEN ? AND ? ORDER BY tarih, saat""",
        (bid, bas, bit),
    ))
    return jsonify({"isler": isler, "avanslar": avanslar})


# --------------------------------------------------------------- program bilgisi
def yerel_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # paket gönderilmez, sadece hangi arayüz seçilir
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


@app.get("/api/bilgi")
def bilgi():
    port = app.config.get("PORT", 5050)
    return jsonify({
        "surum": SURUM,
        "ag": bool(app.config.get("AG")),
        "port": port,
        "telefon_adresi": f"http://{yerel_ip()}:{port}",
    })


# ------------------------------------------------------ online güncelleme (GitHub)
GUNCELLEME_REPO = os.environ.get("BERBER_GUNCELLEME_REPO", "veloxtr/berber-randevu")
EXE_ADI = "BerberRandevu.exe"
_gunc = {"son_kontrol": 0.0, "yeni": None, "url": None, "hata": None}


def surum_tuple(s):
    try:
        return tuple(int(x) for x in str(s).lstrip("vV").split("."))
    except ValueError:
        return (0,)


def guncelleme_kontrol(zorla=False):
    """GitHub'daki son sürümü sorar. Genel (public) depo gerekir; ağ yoksa sessizce geçer."""
    if not zorla and time.time() - _gunc["son_kontrol"] < 6 * 3600:
        return _gunc
    _gunc["son_kontrol"] = time.time()
    try:
        rq = urllib.request.Request(
            f"https://api.github.com/repos/{GUNCELLEME_REPO}/releases/latest",
            headers={"User-Agent": "BerberRandevu", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(rq, timeout=6) as r:
            veri = json.load(r)
        etiket = veri.get("tag_name", "")
        asset = next((a for a in veri.get("assets", []) if a.get("name") == EXE_ADI), None)
        if asset and surum_tuple(etiket) > surum_tuple(SURUM):
            _gunc.update(yeni=etiket.lstrip("vV"), url=asset["browser_download_url"], hata=None)
        else:
            _gunc.update(yeni=None, url=None, hata=None)
    except Exception as e:  # ağ yok, depo özel, limit vb.
        _gunc.update(yeni=None, url=None, hata=str(e)[:120])
    return _gunc


@app.get("/api/guncelleme")
def guncelleme_durum():
    g = guncelleme_kontrol(zorla=request.args.get("zorla") == "1")
    return jsonify({
        "mevcut": SURUM, "yeni": g["yeni"], "hata": g["hata"],
        "uygulanabilir": bool(getattr(sys, "frozen", False)),
    })


@app.post("/api/guncelleme/uygula")
@yetkili_gerekli
def guncelleme_uygula():
    if not getattr(sys, "frozen", False):
        return hata("Otomatik güncelleme sadece exe sürümünde çalışır.")
    g = guncelleme_kontrol(zorla=True)
    if not g["yeni"]:
        return hata("Yeni sürüm bulunamadı.")
    exe = sys.executable
    yeni = exe + ".yeni"
    # Eski exe masaüstüne/yanına dağılmasın: Yedekler klasörüne alınır (son 3 tutulur).
    eski = os.path.join(yedek_dizini(), "BerberRandevu_v%s_%s.exe" % (SURUM, datetime.now().strftime("%Y%m%d-%H%M%S")))
    try:
        rq = urllib.request.Request(g["url"], headers={"User-Agent": "BerberRandevu"})
        with urllib.request.urlopen(rq, timeout=90) as r, open(yeni, "wb") as f:
            shutil.copyfileobj(r, f)
        with open(yeni, "rb") as f:
            imza = f.read(2)
        if imza != b"MZ" or os.path.getsize(yeni) < 1_000_000:
            raise ValueError("İndirilen dosya geçerli bir exe değil.")
        yedek_al()                                   # güncellemeden önce veritabanı yedeği
        os.makedirs(yedek_dizini(), exist_ok=True)
        os.replace(exe, eski)      # çalışan exe Windows'ta taşınabilir/yeniden adlandırılabilir
        os.replace(yeni, exe)
        yedek_temizle()
    except Exception as e:
        if not os.path.exists(exe) and os.path.exists(eski):
            os.replace(eski, exe)  # yarım kaldıysa eskiye dön
        try:
            os.remove(yeni)
        except OSError:
            pass
        return hata("Güncelleme indirilemedi: " + str(e)[:150], 502)
    arg = " ".join(f'"{x}"' for x in sys.argv[1:])
    komut = f'ping -n 5 127.0.0.1 >nul & start "" "{exe}" {arg}'
    subprocess.Popen(["cmd", "/c", komut], creationflags=0x00000008 | 0x08000000, close_fds=True)
    threading.Timer(1.5, lambda: os._exit(0)).start()   # port boşalsın, yeni sürüm açılsın
    return jsonify({"tamam": True, "surum": g["yeni"]})


@app.get("/api/ping")
def ping():
    app.config["SON_PING"] = time.time()
    return jsonify({"tamam": True})


# -------------------------------------------------------------------- yedek uçları
@app.get("/api/yedek")
@yetkili_gerekli
def yedek_durum():
    d = yedek_dizini()

    def bilgi(f):
        yol = os.path.join(d, f)
        return {"ad": f, "boyut": os.path.getsize(yol),
                "zaman": datetime.fromtimestamp(os.path.getmtime(yol)).isoformat(timespec="seconds")}

    return jsonify({
        "klasor": d, "adet": yedek_adet_oku(),
        "liste": [bilgi(f) for f in yedek_dosyalari()],
        "exe_liste": [bilgi(f) for f in exe_yedek_dosyalari()],
    })


@app.post("/api/yedek")
@yetkili_gerekli
def yedek_manuel():
    try:
        return jsonify({"tamam": True, "ad": yedek_al()})
    except Exception as e:
        return hata("Yedek alınamadı: " + str(e)[:150], 500)


@app.post("/api/yedek/klasor-ac")
@yetkili_gerekli
def yedek_klasor_ac():
    d = yedek_dizini()
    os.makedirs(d, exist_ok=True)
    if os.name != "nt":
        return jsonify({"tamam": False, "klasor": d, "mesaj": "Klasörü sadece Windows'ta açabilirim."})
    os.startfile(d)  # noqa: S606 (Windows Gezgini)
    return jsonify({"tamam": True, "klasor": d})


@app.post("/api/yedek/geri-yukle")
@yetkili_gerekli
def yedek_geri_yukle_ucu():
    ad = str((request.get_json(force=True, silent=True) or {}).get("ad", ""))
    try:
        yedek_geri_yukle(ad)
    except ValueError as e:
        return hata(str(e), 404)
    except Exception as e:
        return hata("Geri yüklenemedi: " + str(e)[:150], 500)
    return jsonify({"tamam": True})


def kalp_atisi_bekle():
    """Edge penceresi hemen devrettiyse (kapanış izlenemiyorsa): sayfa belirli süre ping atmayınca çık."""
    bas = time.time()
    while True:
        time.sleep(5)
        son = app.config.get("SON_PING", 0)
        if (son and time.time() - son > 180) or (not son and time.time() - bas > 120):
            return


# ------------------------------------------------------------------ çalıştır
def pencere_ac(url, bekle=True):
    """Adres çubuğu ve sekmesi olmayan, kendi penceresinde uygulama gibi açar (Edge/Chrome 'app' modu).
    Pencere kapanana kadar bekler. Edge/Chrome yoksa False döner."""
    adaylar = [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ]
    for a in adaylar:
        yol = os.path.expandvars(a)
        if os.path.exists(yol):
            profil = os.path.join(veri_dizini(), "pencere_profil")  # ayrı profil: kendi süreci olur, kapanışı izlenebilir
            pr = subprocess.Popen([
                yol, f"--app={url}", f"--user-data-dir={profil}", "--window-size=1280,820",
                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
            ])
            if bekle:
                t0 = time.time()
                pr.wait()
                if time.time() - t0 < 3:   # süreç hemen devredildi, kapanışı ping ile izle
                    kalp_atisi_bekle()
            return True
    return False


def main():
    # Konsolsuz (windowed) exe'de stdout/stderr None olur; yazma hatası vermesin.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    p = argparse.ArgumentParser()
    p.add_argument("--ag", action="store_true", help="Aynı ağdaki cihazlardan erişime aç (telefon)")
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--tarayici-acma", action="store_true", help="Pencere açma, sadece sunucu çalışsın (test)")
    a = p.parse_args()
    url = f"http://127.0.0.1:{a.port}"

    # Program zaten açıksa ikinci sunucu başlatma, sadece yeni bir pencere aç.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", a.port)) == 0:
            if not a.tarayici_acma and not pencere_ac(url, bekle=False):
                webbrowser.open(url)
            return

    baslat_db()
    app.config["AG"] = a.ag
    app.config["PORT"] = a.port
    # PIN unutulursa: exe'nin yanına PIN_SIFIRLA.txt adlı boş bir dosya koyup programı açın.
    sifirla = os.path.join(TEMEL, "PIN_SIFIRLA.txt")
    if os.path.exists(sifirla):
        c = sqlite3.connect(DB_YOLU)
        c.execute("DELETE FROM ayarlar WHERE anahtar IN ('pin_hash', 'pin_salt')")
        c.commit()
        c.close()
        try:
            os.remove(sifirla)
        except OSError:
            pass
    try:  # eski sürümün güncellemesinden yanda kalan .eski exe'yi Yedekler klasörüne taşı
        if getattr(sys, "frozen", False) and os.path.exists(sys.executable + ".eski"):
            os.makedirs(yedek_dizini(), exist_ok=True)
            shutil.move(sys.executable + ".eski", os.path.join(
                yedek_dizini(), "BerberRandevu_onceki_%s.exe" % datetime.now().strftime("%Y%m%d-%H%M%S")))
            yedek_temizle()
    except OSError:
        pass
    threading.Thread(target=otomatik_yedek_dongusu, daemon=True).start()   # açılışta + günde en az bir yedek
    threading.Thread(target=guncelleme_kontrol, daemon=True).start()
    srv = make_server("0.0.0.0" if a.ag else "127.0.0.1", a.port, app, threaded=True)
    sunucu = threading.Thread(target=srv.serve_forever, daemon=True)
    sunucu.start()
    print(f"Berber Randevu v{SURUM}: {url}" + ("  (telefon erişimi AÇIK)" if a.ag else ""))

    if a.tarayici_acma:
        sunucu.join()          # sadece sunucu (test / sunucu modu)
        return
    if not pencere_ac(url):    # pencere kapanınca buradan devam eder ve program biter
        webbrowser.open(url)   # Edge/Chrome bulunamadı: varsayılan tarayıcıda aç, çalışmaya devam et
        sunucu.join()
    srv.shutdown()


if __name__ == "__main__":
    main()
