# -*- coding: utf-8 -*-
"""Eklentinin kalıcı kullanıcı ayarları."""

import json
import os

import globalVars


class Ayarlar:
	def __init__(self):
		klasor = os.path.join(os.fspath(globalVars.appArgs.configPath), "dosya_arsivim")
		self.dosya_yolu = os.path.join(klasor, "ayarlar.json")
		self.bildirimleri_goster = True
		self.turetilmis_dosyalari_goster = False
		try:
			with open(self.dosya_yolu, "r", encoding="utf-8") as dosya:
				ayarlar = json.load(dosya)
				self.bildirimleri_goster = bool(ayarlar.get("bildirimleri_goster", True))
				self.turetilmis_dosyalari_goster = bool(ayarlar.get("turetilmis_dosyalari_goster", False))
		except (OSError, ValueError, TypeError):
			pass

	def kaydet(self):
		os.makedirs(os.path.dirname(self.dosya_yolu), exist_ok=True)
		gecici_yol = self.dosya_yolu + ".tmp"
		try:
			with open(gecici_yol, "w", encoding="utf-8") as dosya:
				json.dump({
					"bildirimleri_goster": self.bildirimleri_goster,
					"turetilmis_dosyalari_goster": self.turetilmis_dosyalari_goster,
				}, dosya)
			os.replace(gecici_yol, self.dosya_yolu)
		finally:
			if os.path.exists(gecici_yol):
				os.remove(gecici_yol)
