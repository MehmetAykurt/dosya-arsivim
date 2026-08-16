# -*- coding: utf-8 -*-
"""Dosya yüklemelerinin NVDA yeniden başlatılsa da korunmasını sağlar."""

import base64
import json
import os
import threading
import uuid

import globalVars

from .oturum_deposu import _sifre_coz, _sifrele


class YuklemeKuyrugu:
	"""Windows DPAPI ile şifrelenen, atomik yazılan sıralı yükleme kuyruğu."""

	def __init__(self):
		ayar_klasoru = globalVars.appArgs.configPath
		if not ayar_klasoru:
			raise OSError("NVDA ayar klasörü belirlenemedi.")
		self.klasor = os.path.join(os.fspath(ayar_klasoru), "dosya_arsivim")
		self.dosya_yolu = os.path.join(self.klasor, "yukleme_kuyrugu.dat")
		self.kilit = threading.RLock()
		self.kayitlar = self._yukle()
		self._yarim_kalanlari_beklemeye_al()

	def _yukle(self):
		try:
			with open(self.dosya_yolu, "r", encoding="utf-8") as dosya:
				sifreli = base64.b64decode(json.load(dosya)["veri"], validate=True)
			veri = json.loads(_sifre_coz(sifreli).decode("utf-8"))
			kayitlar = veri.get("kayitlar", [])
			if not isinstance(kayitlar, list):
				return []
			return [kayit for kayit in kayitlar if self._kayit_gecerli_mi(kayit)]
		except (OSError, KeyError, TypeError, ValueError, UnicodeError):
			return []

	@staticmethod
	def _kayit_gecerli_mi(kayit):
		return (
			isinstance(kayit, dict)
			and all(isinstance(kayit.get(alan), str) for alan in ("id", "eposta", "klasor", "yerel_yol", "durum"))
		)

	def _kaydet(self):
		veri = json.dumps({"surum": 1, "kayitlar": self.kayitlar}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
		kayit = json.dumps({"veri": base64.b64encode(_sifrele(veri)).decode("ascii")}, separators=(",", ":"))
		os.makedirs(self.klasor, exist_ok=True)
		gecici_yol = self.dosya_yolu + ".tmp"
		try:
			with open(gecici_yol, "w", encoding="utf-8") as dosya:
				dosya.write(kayit)
			os.replace(gecici_yol, self.dosya_yolu)
		finally:
			if os.path.exists(gecici_yol):
				os.remove(gecici_yol)

	def _yarim_kalanlari_beklemeye_al(self):
		degisti = False
		for kayit in self.kayitlar:
			if kayit["durum"] == "yükleniyor":
				kayit["durum"] = "bekliyor"
				degisti = True
		if degisti:
			self._kaydet()

	def ekle(self, eposta, klasor, yerel_yollar):
		with self.kilit:
			mevcut_hedefler = {
				self._uzak_hedef_anahtari(kayit["eposta"], kayit["klasor"], kayit["yerel_yol"])
				for kayit in self.kayitlar
			}
			eklenenler = []
			yinelenenler = []
			for yerel_yol in yerel_yollar:
				yerel_yol = os.fspath(yerel_yol)
				hedef = self._uzak_hedef_anahtari(eposta, klasor, yerel_yol)
				if hedef in mevcut_hedefler:
					yinelenenler.append(yerel_yol)
					continue
				self.kayitlar.append({
					"id": uuid.uuid4().hex,
					"eposta": eposta,
					"klasor": klasor,
					"yerel_yol": yerel_yol,
					"durum": "bekliyor",
				})
				mevcut_hedefler.add(hedef)
				eklenenler.append(yerel_yol)
			if eklenenler:
				self._kaydet()
		return {"eklenenler": eklenenler, "yinelenenler": yinelenenler}

	@staticmethod
	def _uzak_hedef_anahtari(eposta, klasor, yerel_yol):
		"""Windows'ta aynı uzak dosyaya karşılık gelen yollar için kararlı anahtar üretir."""
		return (
			eposta.strip().casefold(),
			klasor.casefold(),
			os.path.basename(os.fspath(yerel_yol)).casefold(),
		)

	def siradakini_al(self, eposta):
		with self.kilit:
			for kayit in self.kayitlar:
				if kayit["eposta"] == eposta and kayit["durum"] == "bekliyor":
					kayit["durum"] = "yükleniyor"
					self._kaydet()
					return dict(kayit)
			for kayit in self.kayitlar:
				if kayit["eposta"] == eposta and kayit["durum"] in ("iptal_ediliyor", "iptal_dogrulaniyor"):
					return dict(kayit)
			for kayit in self.kayitlar:
				if kayit["eposta"] == eposta and kayit["durum"] == "arşivleniyor":
					return dict(kayit)
		return None

	def arsivleniyor(self, kayit_id):
		with self.kilit:
			for kayit in self.kayitlar:
				if kayit["id"] == kayit_id:
					kayit["durum"] = "arşivleniyor"
					break
			self._kaydet()

	def ilerlemeyi_guncelle(self, kayit_id, yuzde):
		with self.kilit:
			for kayit in self.kayitlar:
				if kayit["id"] == kayit_id:
					kayit["yuzde"] = yuzde
					break
			self._kaydet()

	def tamamlandi(self, kayit_id):
		with self.kilit:
			self.kayitlar = [kayit for kayit in self.kayitlar if kayit["id"] != kayit_id]
			self._kaydet()

	def sunucuda_gorunen_yuklemeleri_tamamla(self, eposta, klasor_dosyalari):
		"""Sunucuda özgün dosyası görünen arşivleniyor kayıtlarını güvenle tamamlar."""
		sunucudaki_ozgun_dosyalar = {
			(klasor, dosya.get("ad"))
			for klasor, dosyalar in klasor_dosyalari.items()
			for dosya in dosyalar
			if dosya.get("kaynak", "original") == "original" and isinstance(dosya.get("ad"), str)
		}
		with self.kilit:
			tamamlananlar = [
				dict(kayit)
				for kayit in self.kayitlar
				if (
					kayit["eposta"] == eposta
					and kayit["durum"] == "arşivleniyor"
					and (kayit["klasor"], os.path.basename(kayit["yerel_yol"])) in sunucudaki_ozgun_dosyalar
				)
			]
			if not tamamlananlar:
				return []
			tamamlanan_kimlikleri = {kayit["id"] for kayit in tamamlananlar}
			eski_kayitlar = self.kayitlar
			self.kayitlar = [kayit for kayit in self.kayitlar if kayit["id"] not in tamamlanan_kimlikleri]
			try:
				self._kaydet()
			except Exception:
				self.kayitlar = eski_kayitlar
				raise
		return tamamlananlar

	def iptal_dogrulaniyor(self, kayit_id):
		"""Sunucu silme isteği kabul edilen kaydı doğrulama aşamasına geçirir."""
		with self.kilit:
			for kayit in self.kayitlar:
				if kayit["id"] == kayit_id:
					kayit["durum"] = "iptal_dogrulaniyor"
					break
			self._kaydet()

	def hatali(self, kayit_id, hata):
		with self.kilit:
			for kayit in self.kayitlar:
				if kayit["id"] == kayit_id:
					kayit["durum"] = "hata"
					kayit["hata"] = hata
					break
			self._kaydet()

	def hatalilari_beklemeye_al(self, eposta):
		"""Belirtilen hesaptaki hatalı kayıtları yeniden denemeye hazırlar."""
		with self.kilit:
			sayi = 0
			for kayit in self.kayitlar:
				if kayit["eposta"] == eposta and kayit["durum"] == "hata":
					kayit["durum"] = "bekliyor"
					kayit.pop("hata", None)
					kayit.pop("yuzde", None)
					sayi += 1
			if sayi:
				self._kaydet()
		return sayi

	def beklemeye_al(self, kayit_id):
		"""Duraklatılan aktarımı kuyrukta yeniden başlatılmaya hazır tutar."""
		with self.kilit:
			for kayit in self.kayitlar:
				if kayit["id"] == kayit_id:
					kayit["durum"] = "bekliyor"
					kayit.pop("yuzde", None)
					break
			self._kaydet()

	def beklenmedik_kesintiyi_kurtar(self, kayit_id):
		"""Çöken işçinin yarım bıraktığı etkin aktarımı güvenli biçimde yeniden sıraya alır."""
		with self.kilit:
			for kayit in self.kayitlar:
				if kayit["id"] != kayit_id or kayit["durum"] != "yükleniyor":
					continue
				eski_kayit = dict(kayit)
				kayit["durum"] = "bekliyor"
				kayit.pop("yuzde", None)
				try:
					self._kaydet()
				except Exception:
					kayit.clear()
					kayit.update(eski_kayit)
					raise
				return True
		return False

	def epostadakileri_iptal_et(self, eposta):
		"""Bekleyenleri kaldırır; sunucuya ulaşanları kalıcı iptal durumunda tutar."""
		with self.kilit:
			iptal_edilenler = [dict(kayit) for kayit in self.kayitlar if kayit["eposta"] == eposta]
			korunanlar = []
			for kayit in self.kayitlar:
				if kayit["eposta"] != eposta:
					korunanlar.append(kayit)
					continue
				if kayit["durum"] in ("yükleniyor", "arşivleniyor"):
					kayit["durum"] = "iptal_ediliyor"
					kayit.pop("hata", None)
					kayit.pop("yuzde", None)
					korunanlar.append(kayit)
				elif kayit["durum"] in ("iptal_ediliyor", "iptal_dogrulaniyor"):
					korunanlar.append(kayit)
			self.kayitlar = korunanlar
			self._kaydet()
		return iptal_edilenler

	def epostadaki_sayi(self, eposta):
		with self.kilit:
			return sum(1 for kayit in self.kayitlar if kayit["eposta"] == eposta)

	def klasordekileri_al(self, eposta, klasor):
		with self.kilit:
			return [
				dict(kayit)
				for kayit in self.kayitlar
				if kayit["eposta"] == eposta and kayit["klasor"] == klasor
			]
