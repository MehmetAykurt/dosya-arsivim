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
			for yerel_yol in yerel_yollar:
				self.kayitlar.append({
					"id": uuid.uuid4().hex,
					"eposta": eposta,
					"klasor": klasor,
					"yerel_yol": os.fspath(yerel_yol),
					"durum": "bekliyor",
				})
			self._kaydet()

	def siradakini_al(self, eposta):
		with self.kilit:
			for kayit in self.kayitlar:
				if kayit["eposta"] == eposta and kayit["durum"] == "arşivleniyor":
					return dict(kayit)
			for kayit in self.kayitlar:
				if kayit["eposta"] == eposta and kayit["durum"] == "bekliyor":
					kayit["durum"] = "yükleniyor"
					self._kaydet()
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

	def epostadakileri_sil(self, eposta):
		"""Belirli hesaba ait kuyruk kayıtlarını siler ve silinenleri döndürür."""
		with self.kilit:
			silinenler = [dict(kayit) for kayit in self.kayitlar if kayit["eposta"] == eposta]
			self.kayitlar = [kayit for kayit in self.kayitlar if kayit["eposta"] != eposta]
			self._kaydet()
		return silinenler

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
