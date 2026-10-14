"""Berber Randevu Takip - yerel web uygulaması (Flask + SQLite).

Çalıştırma:
    python app.py            -> sadece bu bilgisayardan (http://127.0.0.1:5050)
    python app.py --ag       -> aynı ağdaki telefonlardan da erişilir
"""
import argparse
import os
import socket
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timedelta

from flask import Flask, g, jsonify, request, send_from_directory

if getattr(sys, "frozen", False):  # PyInstaller exe
    TEMEL = os.path.dirname(sys.executable)
    STATIK = os.path.join(sys._MEIPASS, "static")
else:
    TEMEL = os.path.dirname(os.path.abspath(__file__))
    STATIK = os.path.join(TEMEL, "static")

sys.path.insert(0, TEMEL if not getattr(sys, "frozen", False) else sys._MEIPASS)
from version import VERSION as SURUM  # noqa: E402

DB_YOLU = os.environ.get("BERBER_DB", os.path.join(TEMEL, "berber.db"))
DURUMLAR = ("bekliyor", "geldi", "gelmedi", "iptal")

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
"""

VARSAYILAN_AYAR = {"acilis": "09:00", "kapanis": "21:00", "adim_dk": "30", "dukkan_adi": "Berber Randevu"}


def baslat_db():
    c = sqlite3.connect(DB_YOLU)
    c.executescript(SEMA)
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


def ayarlar():
    return {r["anahtar"]: r["deger"] for r in db().execute("SELECT * FROM ayarlar")}


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


# ------------------------------------------------------------------- ayarlar
@app.get("/api/ayarlar")
def ayar_getir():
    return jsonify(ayarlar())


@app.put("/api/ayarlar")
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
@app.get("/api/berberler")
def berber_liste():
    return jsonify(satirlar(db().execute("SELECT * FROM berberler WHERE aktif=1 ORDER BY id")))


@app.post("/api/berberler")
def berber_ekle():
    ad = ((request.get_json(force=True, silent=True) or {}).get("ad") or "").strip()
    if not ad:
        return hata("Ad gerekli.")
    cur = db().execute("INSERT INTO berberler(ad) VALUES (?)", (ad,))
    db().commit()
    return jsonify({"id": cur.lastrowid, "ad": ad, "aktif": 1}), 201


@app.put("/api/berberler/<int:bid>")
def berber_guncelle(bid):
    ad = ((request.get_json(force=True, silent=True) or {}).get("ad") or "").strip()
    if not ad:
        return hata("Ad gerekli.")
    db().execute("UPDATE berberler SET ad=? WHERE id=?", (ad, bid))
    db().commit()
    return jsonify({"id": bid, "ad": ad})


@app.delete("/api/berberler/<int:bid>")
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
def hizmet_ekle():
    t, e = hizmet_dogrula(request.get_json(force=True, silent=True) or {})
    if e:
        return hata(e)
    cur = db().execute("INSERT INTO hizmetler(ad, sure_dk, fiyat) VALUES (?,?,?)", t)
    db().commit()
    return jsonify({"id": cur.lastrowid, "ad": t[0], "sure_dk": t[1], "fiyat": t[2], "aktif": 1}), 201


@app.put("/api/hizmetler/<int:hid>")
def hizmet_guncelle(hid):
    t, e = hizmet_dogrula(request.get_json(force=True, silent=True) or {})
    if e:
        return hata(e)
    db().execute("UPDATE hizmetler SET ad=?, sure_dk=?, fiyat=? WHERE id=?", (*t, hid))
    db().commit()
    return jsonify({"id": hid, "ad": t[0], "sure_dk": t[1], "fiyat": t[2]})


@app.delete("/api/hizmetler/<int:hid>")
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
    return jsonify({"berberler": satir, "toplam": toplam})


# ------------------------------------------------------------------ çalıştır
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ag", action="store_true", help="Aynı ağdaki cihazlardan erişime aç (telefon)")
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--tarayici-acma", action="store_true")
    a = p.parse_args()
    # Program zaten açıksa ikinci kopya başlatma, sadece tarayıcıyı aç.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", a.port)) == 0:
            if not a.tarayici_acma:
                webbrowser.open(f"http://127.0.0.1:{a.port}")
            return
    baslat_db()
    host = "0.0.0.0" if a.ag else "127.0.0.1"
    if not a.tarayici_acma:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{a.port}")).start()
    print(f"Berber Randevu v{SURUM} çalışıyor: http://127.0.0.1:{a.port}")
    print("Programı kullanırken bu pencereyi KAPATMAYIN. Kapatmak için pencereyi kapatın.")
    if a.ag:
        print("Ağ erişimi AÇIK: telefondan http://<bu-bilgisayarın-IP'si>:%d adresini açın." % a.port)
    app.run(host=host, port=a.port, debug=False)


if __name__ == "__main__":
    main()
