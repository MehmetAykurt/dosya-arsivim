import ast
import gettext
from pathlib import Path
import unittest


KOK = Path(__file__).resolve().parents[1]


class IngilizceDilDosyasiTesti(unittest.TestCase):
	def test_koddaki_tum_metinler_derlenmis_katalogda_bulunur(self):
		metinler = set()
		for yol in (KOK / "globalPlugins" / "arsivim").glob("*.py"):
			agac = ast.parse(yol.read_text(encoding="utf-8"), filename=str(yol))
			for dugum in ast.walk(agac):
				if (
					isinstance(dugum, ast.Call)
					and isinstance(dugum.func, ast.Name)
					and dugum.func.id == "_"
					and dugum.args
					and isinstance(dugum.args[0], ast.Constant)
					and isinstance(dugum.args[0].value, str)
				):
					metinler.add(dugum.args[0].value)

		with (KOK / "locale" / "en" / "LC_MESSAGES" / "nvda.mo").open("rb") as dosya:
			katalog = gettext.GNUTranslations(dosya)._catalog

		eksikler = sorted(metin for metin in metinler if metin not in katalog)
		self.assertEqual(eksikler, [])


if __name__ == "__main__":
	unittest.main()
