from pathlib import Path
import unittest


KOK = Path(__file__).resolve().parents[1]


class KilavuzTesti(unittest.TestCase):
	def _oku(self, dil):
		return (KOK / "doc" / dil / "readme.html").read_text(encoding="utf-8")

	def test_turkce_ve_ingilizce_kilavuzlar_yeni_islevleri_aciklar(self):
		tr = self._oku("tr")
		en = self._oku("en")
		for metin in (
			"Yüklemeler &gt; Sunucu işlemleri",
			"Türev üretimini kapat",
			"yalnızca seçili olarak yüklediğiniz özgün dosyanın",
			"ZIP veya torrent bağlantıları bu menüde gösterilmez",
			"Bağlantı alanında yalnızca yüklediğiniz özgün dosyanın",
			"5, 15 ve 30 saniye",
			"Virüs taraması",
		):
			self.assertIn(metin, tr)
		for metin in (
			"Uploads &gt; Server operations",
			"Disable derivative generation",
			"only the direct access link of the selected original file",
			"ZIP and torrent links are not shown in this menu",
			"The link field shows only the direct access link of the original file",
			"5, 15 and 30 seconds",
			"Virus scanning",
		):
			self.assertIn(metin, en)

	def test_duzenlenen_html_bloklari_dengeli(self):
		for dil in ("tr", "en"):
			metin = self._oku(dil)
			for etiket in ("p", "div", "table", "thead", "tbody", "tr", "td", "ul", "li"):
				self.assertEqual(
					metin.count(f"<{etiket}"),
					metin.count(f"</{etiket}>"),
					f"{dil} kılavuzunda {etiket} etiketi dengesiz",
				)


if __name__ == "__main__":
	unittest.main()
