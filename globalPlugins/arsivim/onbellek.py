# -*- coding: utf-8 -*-
"""Dosya listesinin yerel SQLite önbelleği."""

import hashlib
import os
import time
from contextlib import contextmanager

import globalVars

from .sqlite_compat import sqlite3


class DosyaOnbellegi:
	"""Dosya bilgilerini ve daha sonraki eşitleme işlemlerini yerelde tutar."""

	def __init__(self, ayar_klasoru=None):
		if ayar_klasoru is None:
			ayar_klasoru = globalVars.appArgs.configPath
		if not ayar_klasoru:
			raise OSError("NVDA ayar klasörü belirlenemedi.")
		self.klasor = os.path.join(os.fspath(ayar_klasoru), "dosya_arsivim")
		self.dosya_yolu = os.path.join(self.klasor, "dosya_arsivim.db")
		self._hazirla()

	@staticmethod
	def hesap_kimligini_al(eposta):
		"""E-posta adresini veritabanında açıkça saklamayan kararlı kimlik üretir."""
		return hashlib.sha256(eposta.strip().lower().encode("utf-8")).hexdigest()

	@contextmanager
	def _baglan(self):
		baglanti = sqlite3.connect(self.dosya_yolu, timeout=15)
		baglanti.execute("PRAGMA foreign_keys = ON")
		try:
			yield baglanti
		except Exception:
			baglanti.rollback()
			raise
		else:
			baglanti.commit()
		finally:
			baglanti.close()

	def _hazirla(self):
		"""Önbellek tablolarını kurar; bozuk dosyayı güvenli yedekle değiştirir."""
		os.makedirs(self.klasor, exist_ok=True)
		try:
			self._tablolari_olustur()
		except sqlite3.DatabaseError:
			self._bozuk_veritabanini_yedekle()
			self._tablolari_olustur()

	def _bozuk_veritabanini_yedekle(self):
		"""Bozuk veritabanını silmeden ayırır; sonraki eşitleme yeni dosyayı doldurur."""
		yedek_on_eki = self.dosya_yolu + ".bozuk-" + str(time.time_ns())
		if os.path.exists(self.dosya_yolu):
			os.replace(self.dosya_yolu, yedek_on_eki)
		for uzanti in ("-wal", "-shm"):
			yol = self.dosya_yolu + uzanti
			if os.path.exists(yol):
				os.replace(yol, yedek_on_eki + uzanti)

	def _tablolari_olustur(self):
		"""Geçerli SQLite dosyasında tabloları oluşturur."""
		with self._baglan() as baglanti:
			baglanti.executescript("""
				CREATE TABLE IF NOT EXISTS hesaplar (
					hesap_kimligi TEXT PRIMARY KEY,
					son_esitleme INTEGER,
					ilk_esitleme_tamamlandi INTEGER NOT NULL DEFAULT 0
				);

				CREATE TABLE IF NOT EXISTS klasorler (
					hesap_kimligi TEXT NOT NULL,
					ad TEXT NOT NULL,
					sira INTEGER NOT NULL,
					PRIMARY KEY (hesap_kimligi, ad),
					FOREIGN KEY (hesap_kimligi) REFERENCES hesaplar(hesap_kimligi) ON DELETE CASCADE
				);

				CREATE TABLE IF NOT EXISTS dosyalar (
					hesap_kimligi TEXT NOT NULL,
					klasor TEXT NOT NULL,
					ad TEXT NOT NULL,
					boyut INTEGER,
					yukleme_zamani INTEGER,
					bicim TEXT,
					kaynak TEXT NOT NULL DEFAULT 'original',
					durum TEXT NOT NULL DEFAULT 'yuklendi',
					guncelleme_zamani INTEGER,
					PRIMARY KEY (hesap_kimligi, klasor, ad),
					FOREIGN KEY (hesap_kimligi) REFERENCES hesaplar(hesap_kimligi) ON DELETE CASCADE
				);

				CREATE TABLE IF NOT EXISTS islemler (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					hesap_kimligi TEXT NOT NULL,
					tur TEXT NOT NULL,
					klasor TEXT NOT NULL,
					dosya_adi TEXT NOT NULL,
					durum TEXT NOT NULL DEFAULT 'bekliyor',
					hata TEXT,
					olusturma_zamani INTEGER NOT NULL,
					FOREIGN KEY (hesap_kimligi) REFERENCES hesaplar(hesap_kimligi) ON DELETE CASCADE
				);

				CREATE INDEX IF NOT EXISTS dosyalar_klasor_indeksi
				ON dosyalar (hesap_kimligi, klasor, ad);

				CREATE INDEX IF NOT EXISTS islemler_durum_indeksi
				ON islemler (hesap_kimligi, durum, id);
			""")
			sutunlar = {satir[1] for satir in baglanti.execute("PRAGMA table_info(dosyalar)")}
			if "kaynak" not in sutunlar:
				baglanti.execute("ALTER TABLE dosyalar ADD COLUMN kaynak TEXT NOT NULL DEFAULT 'original'")
			baglanti.execute(
				"""UPDATE islemler SET durum = 'dogrulaniyor'
				WHERE tur = 'sil' AND durum = 'siliniyor'"""
			)

	def ilk_esitleme_tamamlandi_mi(self, eposta):
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		with self._baglan() as baglanti:
			kayit = baglanti.execute(
				"SELECT ilk_esitleme_tamamlandi FROM hesaplar WHERE hesap_kimligi = ?",
				(hesap_kimligi,),
			).fetchone()
		return bool(kayit and kayit[0])

	def hesabi_hazirla(self, eposta):
		"""Hesap için boş bir yerel kayıt açar; uzaktaki veriye işlem yapmaz."""
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		with self._baglan() as baglanti:
			baglanti.execute(
				"INSERT OR IGNORE INTO hesaplar (hesap_kimligi) VALUES (?)",
				(hesap_kimligi,),
			)

	def klasordeki_dosyalari_al(self, eposta, klasor, turetilmisleri_goster=False):
		"""Seçili klasörün son eşitlenmiş dosya listesini döndürür."""
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		with self._baglan() as baglanti:
			kayitlar = baglanti.execute(
				"""SELECT ad, boyut, yukleme_zamani, bicim, kaynak, durum
				FROM dosyalar
				WHERE hesap_kimligi = ? AND klasor = ? AND durum IN ('yuklendi', 'siliniyor')
				AND (? OR kaynak = 'original')
				ORDER BY ad COLLATE NOCASE""",
				(hesap_kimligi, klasor, int(turetilmisleri_goster)),
			).fetchall()
		return [
			{
				"ad": ad,
				"boyut": boyut,
				"yukleme_zamani": yukleme_zamani,
				"bicim": bicim,
				"kaynak": kaynak,
				"durum": durum,
			}
			for ad, boyut, yukleme_zamani, bicim, kaynak, durum in kayitlar
		]

	def tum_dosyalari_esitle(self, eposta, klasorler, klasor_dosyalari):
		"""Sunucudan alınan listeyi yerelde günceller; uzak sisteme istek göndermez."""
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		simdi = int(time.time())
		with self._baglan() as baglanti:
			baglanti.execute(
				"INSERT OR IGNORE INTO hesaplar (hesap_kimligi) VALUES (?)",
				(hesap_kimligi,),
			)
			bekleyen_silme_islemleri = list(baglanti.execute(
				"""SELECT id, klasor, dosya_adi FROM islemler
				WHERE hesap_kimligi = ? AND tur = 'sil'
				AND durum IN ('bekliyor', 'siliniyor', 'dogrulaniyor')""",
				(hesap_kimligi,),
			))
			sunucudaki_dosyalar = {
				(klasor, dosya["ad"])
				for klasor, dosyalar in klasor_dosyalari.items()
				for dosya in dosyalar
				if dosya.get("kaynak", "original") == "original"
			}
			bekleyen_silmeler = {
				(klasor, dosya_adi)
				for _, klasor, dosya_adi in bekleyen_silme_islemleri
				if (klasor, dosya_adi) in sunucudaki_dosyalar
			}
			for islem_id, klasor, dosya_adi in bekleyen_silme_islemleri:
				if (klasor, dosya_adi) not in sunucudaki_dosyalar:
					baglanti.execute(
						"DELETE FROM islemler WHERE id = ? AND hesap_kimligi = ?",
						(islem_id, hesap_kimligi),
					)
			baglanti.execute("DELETE FROM dosyalar WHERE hesap_kimligi = ?", (hesap_kimligi,))
			baglanti.execute("DELETE FROM klasorler WHERE hesap_kimligi = ?", (hesap_kimligi,))
			baglanti.executemany(
				"INSERT INTO klasorler (hesap_kimligi, ad, sira) VALUES (?, ?, ?)",
				[(hesap_kimligi, klasor, sira) for sira, klasor in enumerate(klasorler)],
			)
			for klasor, dosyalar in klasor_dosyalari.items():
				baglanti.executemany(
					"""INSERT INTO dosyalar
					(hesap_kimligi, klasor, ad, boyut, yukleme_zamani, bicim, kaynak,
					durum, guncelleme_zamani)
					VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
					[
						(
							hesap_kimligi,
							klasor,
							dosya["ad"],
							dosya.get("boyut"),
							dosya.get("yukleme_zamani"),
							dosya.get("bicim"),
							dosya.get("kaynak", "original"),
							"siliniyor" if (klasor, dosya["ad"]) in bekleyen_silmeler else "yuklendi",
							simdi,
						)
						for dosya in dosyalar
					],
				)
			baglanti.execute(
				"""UPDATE hesaplar
				SET son_esitleme = ?, ilk_esitleme_tamamlandi = 1
				WHERE hesap_kimligi = ?""",
				(simdi, hesap_kimligi),
			)

	def dosya_yuklendi(self, eposta, klasor, dosya):
		"""Sunucuda doğrulanan yüklemeyi SQL önbelleğine işler."""
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		with self._baglan() as baglanti:
			baglanti.execute(
				"INSERT OR IGNORE INTO hesaplar (hesap_kimligi) VALUES (?)",
				(hesap_kimligi,),
			)
			baglanti.execute(
				"""INSERT INTO dosyalar
				(hesap_kimligi, klasor, ad, boyut, yukleme_zamani, bicim, kaynak,
				durum, guncelleme_zamani)
				VALUES (?, ?, ?, ?, ?, ?, ?, 'yuklendi', ?)
				ON CONFLICT(hesap_kimligi, klasor, ad) DO UPDATE SET
					boyut = excluded.boyut,
					yukleme_zamani = excluded.yukleme_zamani,
					bicim = excluded.bicim,
					kaynak = excluded.kaynak,
				durum = 'yuklendi',
				guncelleme_zamani = excluded.guncelleme_zamani""",
				(
					hesap_kimligi, klasor, dosya["ad"], dosya.get("boyut"),
					dosya.get("yukleme_zamani"), dosya.get("bicim"), dosya.get("kaynak", "original"),
					int(time.time()),
				),
			)

	def silmeyi_baslat(self, eposta, klasor, dosya_adi):
		"""Açık kullanıcı onayıyla silme durumunu yerelde başlatır."""
		islem_id, _ = self.silmeyi_baslat_ve_durumu_al(eposta, klasor, dosya_adi)
		return islem_id

	def silmeyi_baslat_ve_durumu_al(self, eposta, klasor, dosya_adi):
		"""Aynı dosyanın etkin silmesini çoğaltmadan işlem kimliğini ve yenilik durumunu döndürür."""
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		simdi = int(time.time())
		with self._baglan() as baglanti:
			baglanti.execute(
				"INSERT OR IGNORE INTO hesaplar (hesap_kimligi) VALUES (?)",
				(hesap_kimligi,),
			)
			mevcut = baglanti.execute(
				"""SELECT id FROM islemler
				WHERE hesap_kimligi = ? AND tur = 'sil' AND klasor = ? AND dosya_adi = ?
				AND durum IN ('bekliyor', 'siliniyor', 'dogrulaniyor')
				ORDER BY id LIMIT 1""",
				(hesap_kimligi, klasor, dosya_adi),
			).fetchone()
			if mevcut:
				return mevcut[0], False
			sonuc = baglanti.execute(
				"""UPDATE dosyalar SET durum = 'siliniyor', guncelleme_zamani = ?
				WHERE hesap_kimligi = ? AND klasor = ? AND ad = ?""",
				(simdi, hesap_kimligi, klasor, dosya_adi),
			)
			if sonuc.rowcount != 1:
				raise sqlite3.IntegrityError("Silinecek dosya yerel önbellekte bulunamadı.")
			kayit = baglanti.execute(
				"""INSERT INTO islemler
				(hesap_kimligi, tur, klasor, dosya_adi, durum, olusturma_zamani)
				VALUES (?, 'sil', ?, ?, 'siliniyor', ?)""",
				(hesap_kimligi, klasor, dosya_adi, simdi),
			)
		return kayit.lastrowid, True

	def bekleyen_silmeleri_al(self, eposta):
		"""Önceki çalışmadan kalan silmeleri durumlarını değiştirmeden döndürür."""
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		with self._baglan() as baglanti:
			kayitlar = baglanti.execute(
				"""SELECT id, klasor, dosya_adi, durum FROM islemler
				WHERE hesap_kimligi = ? AND tur = 'sil'
				AND durum IN ('bekliyor', 'siliniyor', 'dogrulaniyor')
				ORDER BY id""",
				(hesap_kimligi,),
			).fetchall()
		return [
			{"id": islem_id, "klasor": klasor, "dosya_adi": dosya_adi, "durum": durum}
			for islem_id, klasor, dosya_adi, durum in kayitlar
		]

	def silme_dogrulaniyor(self, islem_id):
		"""Kabul edilen silme isteğini yalnızca sunucu doğrulaması bekleyen duruma getirir."""
		with self._baglan() as baglanti:
			baglanti.execute(
				"UPDATE islemler SET durum = 'dogrulaniyor' WHERE id = ? AND tur = 'sil'",
				(islem_id,),
			)

	def silme_tamamlandi(self, eposta, islem_id, klasor, dosya_adi):
		"""Sunucuda tamamlanan silme işleminin yerel kaydını kaldırır."""
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		with self._baglan() as baglanti:
			baglanti.execute(
				"DELETE FROM dosyalar WHERE hesap_kimligi = ? AND klasor = ? AND ad = ?",
				(hesap_kimligi, klasor, dosya_adi),
			)
			baglanti.execute("DELETE FROM islemler WHERE id = ?", (islem_id,))

	def silme_hatali(self, eposta, islem_id, klasor, dosya_adi, hata):
		"""Başarısız silmeden sonra dosyayı yeniden görünür yapar."""
		hesap_kimligi = self.hesap_kimligini_al(eposta)
		with self._baglan() as baglanti:
			baglanti.execute(
				"""UPDATE dosyalar SET durum = 'yuklendi', guncelleme_zamani = ?
				WHERE hesap_kimligi = ? AND klasor = ? AND ad = ?""",
				(int(time.time()), hesap_kimligi, klasor, dosya_adi),
			)
			baglanti.execute("DELETE FROM islemler WHERE id = ?", (islem_id,))
