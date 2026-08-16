from pathlib import Path
import unittest


KOK = Path(__file__).resolve().parents[1]
DIALOGS_YOLU = KOK / "globalPlugins" / "arsivim" / "dialogs.py"


class SunucuIslemleriArayuzuTesti(unittest.TestCase):
	def test_erisilebilir_standart_denetime_ve_sinirli_yenilemeye_sahiptir(self):
		kaynak = DIALOGS_YOLU.read_text(encoding="utf-8")
		self.assertIn("class SunucuIslemleriPenceresi(wx.Dialog):", kaynak)
		self.assertIn("self.gorev_listesi = wx.ListBox", kaynak)
		self.assertIn("self.ozet_metni = wx.StaticText", kaynak)
		self.assertIn("SUNUCU_ISLEMLERI_HATA_GECIKMELERI_MS = (5000, 15000, 30000)", kaynak)
		self.assertIn("self.istek_devam_ediyor", kaynak)

	def test_yalnizca_hata_gorevi_bir_kez_yeniden_calistirilabilir(self):
		kaynak = DIALOGS_YOLU.read_text(encoding="utf-8")
		self.assertIn('gorev.get("durum") == "error"', kaynak)
		self.assertIn("self.yeniden_calistirilan_gorevler", kaynak)
		self.assertIn("HESAP_DURUMU.istem.gorevi_yeniden_calistir", kaynak)


if __name__ == "__main__":
	unittest.main()
