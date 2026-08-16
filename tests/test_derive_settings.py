import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest


KOK = Path(__file__).resolve().parents[1]
AYARLAR_YOLU = KOK / "globalPlugins" / "arsivim" / "ayarlar.py"
DIALOGS_YOLU = KOK / "globalPlugins" / "arsivim" / "dialogs.py"


class TurevAyarlariTesti(unittest.TestCase):
	def test_turev_uretimi_ayari_varsayilan_olarak_acik_ve_kalicidir(self):
		with tempfile.TemporaryDirectory() as ayar_klasoru:
			global_vars = sys.modules.get("globalVars") or types.ModuleType("globalVars")
			eski_app_args = getattr(global_vars, "appArgs", None)
			global_vars.appArgs = types.SimpleNamespace(configPath=ayar_klasoru)
			sys.modules["globalVars"] = global_vars
			try:
				spec = importlib.util.spec_from_file_location("arsivim.ayarlar_turev_testi", AYARLAR_YOLU)
				modul = importlib.util.module_from_spec(spec)
				spec.loader.exec_module(modul)
				ayarlar = modul.Ayarlar()
				self.assertFalse(ayarlar.turev_uretimini_kapat)
				ayarlar.turev_uretimini_kapat = True
				ayarlar.kaydet()
				yeniden_yuklenen = modul.Ayarlar()
				self.assertTrue(yeniden_yuklenen.turev_uretimini_kapat)
				with open(yeniden_yuklenen.dosya_yolu, "r", encoding="utf-8") as dosya:
					self.assertTrue(json.load(dosya)["turev_uretimini_kapat"])
			finally:
				global_vars.appArgs = eski_app_args

	def test_menu_turev_uretimi_kapatilinca_gosterme_secenenegini_kaldirir(self):
		kaynak = DIALOGS_YOLU.read_text(encoding="utf-8")
		self.assertIn("KIMLIK_TUREV_URETIMINI_KAPAT", kaynak)
		self.assertIn('_("Türev üretimini kapat")', kaynak)
		self.assertIn("self.ayarlar_menu.Delete", kaynak)
		self.assertIn("self.ayarlar_menu.AppendCheckItem", kaynak)


if __name__ == "__main__":
	unittest.main()
