import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest


KOK = Path(__file__).resolve().parents[1]
MODUL_YOLU = KOK / "globalPlugins" / "arsivim" / "onbellek.py"

paket = sys.modules.setdefault("arsivim", types.ModuleType("arsivim"))
paket.__path__ = [str(MODUL_YOLU.parent)]

global_vars = sys.modules.setdefault("globalVars", types.ModuleType("globalVars"))
global_vars.appArgs = types.SimpleNamespace(configPath=None)

sqlite_compat = types.ModuleType("arsivim.sqlite_compat")
sqlite_compat.sqlite3 = sqlite3
sys.modules["arsivim.sqlite_compat"] = sqlite_compat

spec = importlib.util.spec_from_file_location("arsivim.onbellek", MODUL_YOLU)
onbellek = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = onbellek
spec.loader.exec_module(onbellek)


class SilmeGorunurluguTesti(unittest.TestCase):
	def test_silme_dogrulanana_kadar_dosya_siliniyor_durumuyla_listelenir(self):
		with tempfile.TemporaryDirectory() as ayar_klasoru:
			depo = onbellek.DosyaOnbellegi(ayar_klasoru)
			eposta = "deneme@example.com"
			depo.tum_dosyalari_esitle(
				eposta,
				["Belgeler"],
				{"Belgeler": [{"ad": "deneme.txt", "kaynak": "original"}]},
			)

			islem_id = depo.silmeyi_baslat(eposta, "Belgeler", "deneme.txt")
			dosyalar = depo.klasordeki_dosyalari_al(eposta, "Belgeler")

			self.assertEqual(len(dosyalar), 1)
			self.assertEqual(dosyalar[0]["ad"], "deneme.txt")
			self.assertEqual(dosyalar[0]["durum"], "siliniyor")

			depo.silme_tamamlandi(eposta, islem_id, "Belgeler", "deneme.txt")
			self.assertEqual(depo.klasordeki_dosyalari_al(eposta, "Belgeler"), [])

	def test_silme_hatasi_dosyayi_normal_duruma_dondurur(self):
		with tempfile.TemporaryDirectory() as ayar_klasoru:
			depo = onbellek.DosyaOnbellegi(ayar_klasoru)
			eposta = "deneme@example.com"
			depo.tum_dosyalari_esitle(
				eposta,
				["Belgeler"],
				{"Belgeler": [{"ad": "deneme.txt", "kaynak": "original"}]},
			)

			islem_id = depo.silmeyi_baslat(eposta, "Belgeler", "deneme.txt")
			depo.silme_hatali(eposta, islem_id, "Belgeler", "deneme.txt", "hata")
			dosyalar = depo.klasordeki_dosyalari_al(eposta, "Belgeler")

			self.assertEqual(len(dosyalar), 1)
			self.assertEqual(dosyalar[0]["durum"], "yuklendi")

	def test_kabul_edilen_silme_yeniden_acilinca_yalnizca_dogrulanir(self):
		with tempfile.TemporaryDirectory() as ayar_klasoru:
			eposta = "deneme@example.com"
			depo = onbellek.DosyaOnbellegi(ayar_klasoru)
			depo.tum_dosyalari_esitle(
				eposta,
				["Belgeler"],
				{"Belgeler": [{"ad": "deneme.txt", "kaynak": "original"}]},
			)
			islem_id = depo.silmeyi_baslat(eposta, "Belgeler", "deneme.txt")
			depo.silme_dogrulaniyor(islem_id)

			yeniden_acilan_depo = onbellek.DosyaOnbellegi(ayar_klasoru)
			islemler = yeniden_acilan_depo.bekleyen_silmeleri_al(eposta)

			self.assertEqual(len(islemler), 1)
			self.assertEqual(islemler[0]["durum"], "dogrulaniyor")

	def test_eski_siliniyor_kaydi_yeniden_gonderilmek_yerine_dogrulamaya_gecer(self):
		with tempfile.TemporaryDirectory() as ayar_klasoru:
			eposta = "deneme@example.com"
			depo = onbellek.DosyaOnbellegi(ayar_klasoru)
			depo.tum_dosyalari_esitle(
				eposta,
				["Belgeler"],
				{"Belgeler": [{"ad": "deneme.txt", "kaynak": "original"}]},
			)
			depo.silmeyi_baslat(eposta, "Belgeler", "deneme.txt")

			yeniden_acilan_depo = onbellek.DosyaOnbellegi(ayar_klasoru)
			islemler = yeniden_acilan_depo.bekleyen_silmeleri_al(eposta)

			self.assertEqual(len(islemler), 1)
			self.assertEqual(islemler[0]["durum"], "dogrulaniyor")


if __name__ == "__main__":
	unittest.main()
