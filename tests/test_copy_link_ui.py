import ast
from pathlib import Path
import unittest


KOK = Path(__file__).resolve().parents[1]
DIALOGS_YOLU = KOK / "globalPlugins" / "arsivim" / "dialogs.py"


class BaglantiKopyalamaArayuzuTesti(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		agac = ast.parse(DIALOGS_YOLU.read_text(encoding="utf-8"), filename=str(DIALOGS_YOLU))
		cls.sinif = next(
			dugum for dugum in agac.body
			if isinstance(dugum, ast.ClassDef) and dugum.name == "DosyaArsivimPenceresi"
		)

	@classmethod
	def _metot(cls, ad):
		return next(
			dugum for dugum in cls.sinif.body
			if isinstance(dugum, ast.FunctionDef) and dugum.name == ad
		)

	def test_icerik_menusu_baglanti_alt_menusu_olusturmaz(self):
		metot = self._metot("icerik_menusu_ac")
		cagrilar = [
			dugum.func.attr
			for dugum in ast.walk(metot)
			if isinstance(dugum, ast.Call) and isinstance(dugum.func, ast.Attribute)
		]
		self.assertNotIn("AppendSubMenu", cagrilar)
		self.assertNotIn("_dosyanin_baglanti_ogeleri", cagrilar)

	def test_kopyalama_yalnizca_secili_ozgun_dosyanin_baglantisini_alir(self):
		metot = self._metot("baglantiyi_panoya_kopyala_secildi")
		cagrilar = [
			dugum.func.attr
			for dugum in ast.walk(metot)
			if isinstance(dugum, ast.Call) and isinstance(dugum.func, ast.Attribute)
		]
		self.assertIn("dosya_baglantisi", cagrilar)
		self.assertNotIn("_dosyanin_baglanti_ogeleri", cagrilar)

	def test_dosya_bilgileri_yalnizca_ozgun_dosya_baglantisini_alir(self):
		metot = self._metot("dosya_bilgileri_secildi")
		cagrilar = [
			dugum.func.attr
			for dugum in ast.walk(metot)
			if isinstance(dugum, ast.Call) and isinstance(dugum.func, ast.Attribute)
		]
		self.assertIn("dosya_baglantisi", cagrilar)
		self.assertNotIn("arsiv_zip_baglantisi", cagrilar)
		self.assertNotIn("uzak_dosya_baglantisi", cagrilar)
		self.assertFalse(any(
			isinstance(dugum, ast.FunctionDef) and dugum.name == "_dosyanin_baglanti_ogeleri"
			for dugum in self.sinif.body
		))


if __name__ == "__main__":
	unittest.main()
