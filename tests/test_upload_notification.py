import ast
from pathlib import Path
import unittest


KOK = Path(__file__).resolve().parents[1]


class YuklemeBildirimiTesti(unittest.TestCase):
	def test_arayuz_yenilemesi_konusma_geri_cagrisina_bagli_degil(self):
		yol = KOK / "globalPlugins" / "arsivim" / "dialogs.py"
		agac = ast.parse(yol.read_text(encoding="utf-8"), filename=str(yol))
		sinif = next(
			dugum for dugum in agac.body
			if isinstance(dugum, ast.ClassDef) and dugum.name == "YuklemeYoneticisi"
		)
		metot = next(
			dugum for dugum in sinif.body
			if isinstance(dugum, ast.FunctionDef) and dugum.name == "_basari_bildir"
		)

		ic_fonksiyonlar = [
			dugum for dugum in ast.walk(metot)
			if isinstance(dugum, (ast.FunctionDef, ast.AsyncFunctionDef)) and dugum is not metot
		]
		self.assertEqual(
			ic_fonksiyonlar,
			[],
			"Yükleme tamamlanınca arayüz yenilemesi konuşma geri çağrısına bağlanmamalı.",
		)
		self.assertTrue(
			any(
				isinstance(dugum, ast.Call)
				and isinstance(dugum.func, ast.Attribute)
				and dugum.func.attr == "yukleme_tamamlandi"
				for dugum in ast.walk(metot)
			),
			"Başarılı yükleme dinleyicilere hemen bildirilmelidir.",
		)

	def test_arsiv_islemesi_baslayinca_aciklayici_iletisim_kutusu_gosterilir(self):
		yol = KOK / "globalPlugins" / "arsivim" / "dialogs.py"
		agac = ast.parse(yol.read_text(encoding="utf-8"), filename=str(yol))
		sinif = next(
			dugum for dugum in agac.body
			if isinstance(dugum, ast.ClassDef) and dugum.name == "YuklemeYoneticisi"
		)
		metot = next(
			dugum for dugum in sinif.body
			if isinstance(dugum, ast.FunctionDef) and dugum.name == "_arsiv_isleme_bildir"
		)
		self.assertTrue(
			any(
				isinstance(dugum, ast.Call)
				and isinstance(dugum.func, ast.Attribute)
				and isinstance(dugum.func.value, ast.Name)
				and dugum.func.value.id == "wx"
				and dugum.func.attr == "MessageBox"
				for dugum in ast.walk(metot)
			),
			"Aktarım bitip arşiv işlemesi başladığında Tamam düğmeli bir iletişim kutusu gösterilmelidir.",
		)

		basari_metodu = next(
			dugum for dugum in sinif.body
			if isinstance(dugum, ast.FunctionDef) and dugum.name == "_basari_bildir"
		)
		self.assertFalse(
			any(
				isinstance(dugum, ast.Call)
				and isinstance(dugum.func, ast.Attribute)
				and isinstance(dugum.func.value, ast.Name)
				and dugum.func.value.id == "wx"
				and dugum.func.attr == "MessageBox"
				for dugum in ast.walk(basari_metodu)
			),
			"Arşiv işlemesi tamamlandığında ikinci bir iletişim kutusu açılmamalıdır.",
		)


if __name__ == "__main__":
	unittest.main()
