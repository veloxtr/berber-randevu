/* Sesli komut / yazılı komut çözücü (Türkçe).
   Örnek: "Hasan ustanın müşterisi gelecek cuma günü saat beş buçukta"
   -> { berber_id, tarih:"2026-10-16", baslangic:"17:30", ad:"", hizmetler:[...], eksik:[] }
   Hiçbir sunucuya gitmez; tamamen bu dosyada çalışır. */
(function (kok) {
  "use strict";

  function norm(s) {
    return String(s || "")
      .toLocaleLowerCase("tr")
      .replace(/ç/g, "c").replace(/ğ/g, "g").replace(/ı/g, "i").replace(/i̇/g, "i")
      .replace(/ö/g, "o").replace(/ş/g, "s").replace(/ü/g, "u").replace(/â/g, "a").replace(/î/g, "i").replace(/û/g, "u")
      .replace(/['’`´]/g, "")
      .replace(/[^a-z0-9:./]/g, " ")
      .replace(/[.:/]+$/g, "")      // cümle sonu noktası
      .trim();
  }
  function kelimeNorm(w) { return norm(w).replace(/\s+/g, ""); }

  const EK = new Set(["", "i", "u", "e", "a", "de", "da", "te", "ta", "ye", "ya", "yi", "yu", "in", "un", "nin", "nun", "dan", "den", "ten", "tan", "ki", "si", "su", "ndan", "nden", "nda", "nde"]);
  const YER = /(de|da|te|ta)$/;

  const BIRLER = [["bir", 1], ["iki", 2], ["uc", 3], ["dort", 4], ["dor", 4], ["bes", 5], ["alti", 6], ["yedi", 7], ["sekiz", 8], ["dokuz", 9]];
  const ONLAR = [["yirmi", 20], ["on", 10]];

  function kelimeSayi(tok, liste) {
    for (const [kok_, v] of liste) {
      if (tok === kok_) return v;
      if (tok.startsWith(kok_) && EK.has(tok.slice(kok_.length))) return v;
    }
    return null;
  }

  /* i. konumdan sayı oku: {val,len} | null */
  function sayiOku(t, i) {
    const a = t[i]; if (a === undefined) return null;
    let m = /^(\d{1,2})(te|ta|de|da|e|a|i|u|ye|ya|yi)?$/.exec(a);
    if (m) return { val: +m[1], len: 1 };
    const on = kelimeSayi(a, ONLAR);
    if (on !== null) {
      const b = t[i + 1] !== undefined ? kelimeSayi(t[i + 1], BIRLER) : null;
      if (b !== null && !(on === 10 && false)) return { val: on + b, len: 2 };
      return { val: on, len: 1 };
    }
    const b = kelimeSayi(a, BIRLER);
    if (b !== null) return { val: b, len: 1 };
    return null;
  }

  const GUNLER = [["pazartesi", 1], ["cumartesi", 6], ["carsamba", 3], ["persembe", 4], ["sali", 2], ["cuma", 5], ["pazar", 0]];
  const GUN_EK = new Set(["", "ya", "ye", "da", "de", "ki", "dan", "den", "yi", "i", "gunu", "nin"]);
  function gunNo(tok) {
    for (const [k, v] of GUNLER) if (tok.startsWith(k) && GUN_EK.has(tok.slice(k.length))) return v;
    return null;
  }
  const AYLAR = ["ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul", "ekim", "kasim", "aralik"];
  function ayNo(tok) {
    for (let i = 0; i < 12; i++) if (tok.startsWith(AYLAR[i]) && tok.length - AYLAR[i].length <= 3) return i;
    return null;
  }

  function p2(n) { return String(n).padStart(2, "0"); }
  function tarihStr(d) { return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate()); }

  const DURAK = new Set(["saat", "gunu", "gun", "gelecek", "haftaya", "bugun", "yarin", "usta", "ustanin", "ustaya", "ustadan", "randevu", "musteri", "musterisi", "icin", "ve", "adina", "adi", "bir", "bu", "var", "ekle", "olustur", "yaz", "yap", "sabah", "aksam", "ogleden", "sonra", "ikindi", "gece", "ogle"]);

  /* metin: söylenen/yazılan cümle
     ctx: {berberler:[{id,ad}], hizmetler:[{id,ad}], simdi:Date, acilis:"09:00", kapanis:"21:00"} */
  function coz(metin, ctx) {
    const asil = String(metin || "").trim().split(/\s+/).filter(Boolean);
    const t = asil.map(kelimeNorm);
    const simdi = ctx.simdi || new Date();
    const bugun = new Date(simdi.getFullYear(), simdi.getMonth(), simdi.getDate());
    const sonuc = { berber_id: null, berber: "", tarih: "", baslangic: "", ad: "", hizmetler: [], eksik: [] };
    const kullanilan = new Set();

    /* ---- berber ---- */
    let enIyi = null;
    (ctx.berberler || []).forEach(b => {
      const ilk = kelimeNorm(String(b.ad).trim().split(/\s+/)[0] || "");
      if (ilk.length < 2) return;
      for (let i = 0; i < t.length; i++) {
        if (t[i] === ilk || (t[i].startsWith(ilk) && t[i].length - ilk.length <= 4 && gunNo(t[i]) === null)) {
          if (!enIyi || i < enIyi.i) enIyi = { b, i };
          break;
        }
      }
    });
    if (enIyi) { sonuc.berber_id = enIyi.b.id; sonuc.berber = enIyi.b.ad; kullanilan.add(enIyi.i); }

    /* ---- tarih ---- */
    let tarih = null;
    const topla = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
    for (let i = 0; i < t.length && !tarih; i++) {
      const w = t[i];
      if (w === "bugun") { tarih = bugun; kullanilan.add(i); }
      else if (w === "yarin") { tarih = topla(bugun, 1); kullanilan.add(i); }
      else if (w === "obur" || w === "ertesi") { if (t[i + 1] === "gun" || t[i + 1] === "gunu") tarih = topla(bugun, 2); }
      else if (w === "sonra" && (t[i - 1] === "gun" || t[i - 1] === "hafta")) {
        const n = sayiOku(t, i - 2);
        const adet = n ? n.val : (i - 2 >= 0 && /^(bir|bi)$/.test(t[i - 2]) ? 1 : null);
        if (adet !== null) tarih = topla(bugun, adet * (t[i - 1] === "hafta" ? 7 : 1));
      }
    }
    if (!tarih) {
      // "15 ekim"
      for (let i = 1; i < t.length && !tarih; i++) {
        const ay = ayNo(t[i]);
        if (ay !== null) {
          // sayı bir veya iki kelime önce olabilir
          let g = null;
          for (const j of [i - 2, i - 1]) { if (j < 0) continue; const s = sayiOku(t, j); if (s && j + s.len === i) { g = s.val; break; } }
          if (g && g >= 1 && g <= 31) {
            let d = new Date(bugun.getFullYear(), ay, g);
            if (d < bugun) d = new Date(bugun.getFullYear() + 1, ay, g);
            tarih = d;
          }
        }
      }
    }
    if (!tarih) {
      // "20/10" veya "20/10/2026"
      for (let i = 0; i < t.length && !tarih; i++) {
        const m = /^(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?$/.exec(t[i]);
        if (m) {
          let y = m[3] ? +m[3] : bugun.getFullYear(); if (y < 100) y += 2000;
          let d = new Date(y, +m[2] - 1, +m[1]);
          if (!m[3] && d < bugun) d = new Date(y + 1, +m[2] - 1, +m[1]);
          if (d.getMonth() === +m[2] - 1) tarih = d;
        }
      }
    }
    if (!tarih) {
      // hafta günü: "cuma", "gelecek cuma", "haftaya cuma"
      const gelecek = t.includes("gelecek") || t.includes("haftaya") || t.includes("onumuzdeki");
      const haftaya = t.includes("haftaya");
      for (let i = 0; i < t.length; i++) {
        const g = gunNo(t[i]);
        if (g === null) continue;
        const bg = bugun.getDay();
        if (haftaya) {
          const pzt = topla(bugun, 7 - ((bg + 6) % 7));          // gelecek haftanın pazartesisi
          tarih = topla(pzt, (g + 6) % 7);
        } else {
          let fark = (g - bg + 7) % 7;
          if (fark === 0 && gelecek) fark = 7;
          tarih = topla(bugun, fark);
        }
        kullanilan.add(i);
        break;
      }
      if (!tarih && t.includes("haftaya")) tarih = topla(bugun, 7);
    }
    if (tarih) sonuc.tarih = tarihStr(tarih);

    /* ---- saat ---- */
    let h = null, m = 0;
    for (let i = 0; i < t.length && h === null; i++) {
      let r = /^(\d{1,2})[:.](\d{2})(?:te|ta|de|da)?$/.exec(t[i]);
      if (r && +r[1] < 24 && +r[2] < 60) { h = +r[1]; m = +r[2]; continue; }
      if (t[i] === "saat") {
        const n = sayiOku(t, i + 1);
        if (n && n.val <= 24) {
          h = n.val;
          const sonra = t[i + 1 + n.len];
          if (sonra && sonra.startsWith("bucuk")) m = 30;
          else if (sonra && /^(\d{1,2})$/.test(sonra) && +sonra < 60 && n.len === 1) m = +sonra;
          else if (sonra && sonra.startsWith("ceyrek")) m = 15;
        }
        continue;
      }
      // "beş buçukta", "dokuzda", "5te"
      const n = sayiOku(t, i);
      if (n && n.val <= 24) {
        const son = t[i + n.len - 1], sonra = t[i + n.len];
        if (sonra && sonra.startsWith("bucuk")) { h = n.val; m = 30; }
        else if (YER.test(son) && son !== "iki" && ayNo(sonra || "") === null && !/^\d+$/.test(son)) { h = n.val; }
        else if (/^\d{1,2}(te|ta|de|da)$/.test(son) && ayNo(sonra || "") === null) { h = n.val; }
      }
    }
    if (h !== null) {
      const pm = t.some(w => w.startsWith("aksam") || w.startsWith("ikindi")) || (t.includes("ogleden") && t.includes("sonra"));
      const am = t.some(w => w.startsWith("sabah"));
      const ac = parseInt((ctx.acilis || "09:00").split(":")[0], 10), kap = parseInt((ctx.kapanis || "21:00").split(":")[0], 10);
      if (h < 12) {
        if (pm) h += 12;
        else if (!am && h < ac && h + 12 < kap + 1) h += 12;
      }
      if (h > 23) h = 23;
      sonuc.baslangic = p2(h) + ":" + p2(m);
    }

    /* ---- hizmet ---- */
    const kume = new Set(t);
    let tam = [], kismi = [];
    (ctx.hizmetler || []).forEach(hz => {
      const kel = norm(hz.ad).split(/\s+/).filter(x => x.length >= 3);
      if (!kel.length) return;
      const esle = kel.filter(k => t.some(w => w === k || (w.startsWith(k.slice(0, Math.max(3, k.length - 2))) && Math.abs(w.length - k.length) <= 3)));
      if (esle.length === kel.length) tam.push({ hz, puan: kel.length });
      else if (esle.length) kismi.push({ hz, puan: esle.length / kel.length });
    });
    if (tam.length) {
      const mx = Math.max(...tam.map(x => x.puan));
      sonuc.hizmetler = tam.filter(x => x.puan === mx || tam.length > 1 && x.puan >= 1).map(x => x.hz);
      // aynı kelimeyi paylaşan iki hizmetten uzun olanı tercih et (örn. "Saç" ile "Saç Boyama")
      if (sonuc.hizmetler.length > 1) {
        const en = Math.max(...sonuc.hizmetler.map(x => norm(x.ad).split(/\s+/).length));
        const uzun = sonuc.hizmetler.filter(x => norm(x.ad).split(/\s+/).length === en);
        sonuc.hizmetler = uzun.length === 1 && sonuc.hizmetler.some(x => !uzun.includes(x) && norm(x.ad).split(/\s+/).every(k => norm(uzun[0].ad).includes(k))) ? uzun : sonuc.hizmetler;
      }
    } else if (kismi.length) {
      kismi.sort((a, b) => b.puan - a.puan);   // kararlı sıralama: eşitlikte liste sırası korunur
      sonuc.hizmetler = [kismi[0].hz];   // eşitlikte listedeki ilki; randevu formunda zaten görünür, değiştirilebilir
    }

    /* ---- müşteri adı: "Ahmet Yılmaz adına" / "müşteri adı Ahmet" ---- */
    const durakMi = i => {
      const w = t[i];
      return !w || DURAK.has(w) || /^\d/.test(w) || gunNo(w) !== null || ayNo(w) !== null || kullanilan.has(i) || sayiOku(t, i) !== null && /^(bir|iki|uc|dort|bes|alti|yedi|sekiz|dokuz|on|yirmi)/.test(w) || w.startsWith("bucuk");
    };
    const adIdx = t.indexOf("adina");
    if (adIdx > 0) {
      const p = [];
      for (let i = adIdx - 1; i >= 0 && p.length < 2; i--) { if (durakMi(i)) break; p.unshift(asil[i]); }
      sonuc.ad = p.join(" ");
    } else {
      const j = t.indexOf("adi");
      if (j >= 0 && t[j - 1] && t[j - 1].startsWith("musteri")) {
        const p = [];
        for (let i = j + 1; i < t.length && p.length < 2; i++) { if (durakMi(i)) break; p.push(asil[i]); }
        sonuc.ad = p.join(" ");
      }
    }
    sonuc.ad = sonuc.ad.split(" ").map(w => w ? w.charAt(0).toLocaleUpperCase("tr") + w.slice(1).toLocaleLowerCase("tr") : w).join(" ").trim();

    if (sonuc.berber_id === null) sonuc.eksik.push("berber");
    if (!sonuc.tarih) sonuc.eksik.push("gün");
    if (!sonuc.baslangic) sonuc.eksik.push("saat");
    return sonuc;
  }

  const disa = { coz, norm };
  if (typeof module !== "undefined" && module.exports) module.exports = disa;
  else kok.SesCoz = disa;
})(typeof window !== "undefined" ? window : globalThis);
