# -*- coding: utf-8 -*-
"""Dosya Arşivim için erişilebilir wxPython pencereleri."""

import re
import threading
import weakref
import os
import time
from datetime import datetime

import addonHandler
addonHandler.initTranslation()

import ui
import wx
import logHandler
from speech import speech
from speech.commands import CallbackCommand

from .account_api import HesapHatasi, HesapIstemi, YuklemeDuraklatildi
from .ayarlar import Ayarlar
from .sqlite_compat import sqlite3
from .yukleme_kuyrugu import YuklemeKuyrugu

try:
	_
except NameError:
	_ = lambda metin: metin


class HesapDurumu:
	"""Hesap oturumunu yalnızca geçerli NVDA çalışmasında saklar."""

	def __init__(self):
		self.istem = HesapIstemi()
		try:
			self.eposta = self.istem.kalici_oturumu_yukle()
		except OSError:
			self.eposta = None

	@property
	def bagli_mi(self):
		return bool(self.eposta)

	def baglan(self, eposta):
		try:
			self.istem.oturumu_kaydet(eposta)
		except (OSError, ValueError):
			raise HesapHatasi(_("Oturum bilgisi kaydedilemedi."))
		self.eposta = eposta

	def baglantiyi_kes(self):
		try:
			self.istem.kalici_oturumu_sil()
		except OSError:
			raise HesapHatasi(_("Oturum bilgisi silinemedi."))
		self.istem = HesapIstemi()
		self.eposta = None


HESAP_DURUMU = HesapDurumu()
AYARLAR = Ayarlar()
BAGLANTI_BILDIRIM_GECIKMESI_MS = 150
SUNUCU_ISLEMLERI_ETKIN_YENILEME_MS = 10000
SUNUCU_ISLEMLERI_BOSTA_YENILEME_MS = 30000
SUNUCU_ISLEMLERI_HATA_GECIKMELERI_MS = (5000, 15000, 30000)
EKLENTI_SURUMU = "26.8.15"


def arka_planda(calistir, tamamla):
	"""Ağ isteğini NVDA arayüzünü bekletmeden çalıştırır."""
	def is_parcacigi():
		try:
			sonuc = calistir()
		except HesapHatasi as hata:
			wx.CallAfter(tamamla, None, str(hata))
		except Exception:
			logHandler.log.exception("Dosya Arşivim arka plan işlemi başarısız oldu.")
			wx.CallAfter(tamamla, None, _("İşlem sırasında beklenmeyen bir hata oluştu."))
		else:
			wx.CallAfter(tamamla, sonuc, None)
	threading.Thread(target=is_parcacigi, daemon=True).start()


def baglanti_bildir_ve_kapat(pencere):
	"""Konuşma bitince ana pencereye dönmek için NVDA konuşma geri çağırımını kullanır."""
	def ana_pencereye_don():
		def pencereyi_kapat():
			if getattr(pencere, "iptal_edildi", False):
				return
			try:
				if pencere.IsModal():
					pencere.EndModal(wx.ID_OK)
			except RuntimeError:
				pass
		wx.CallLater(BAGLANTI_BILDIRIM_GECIKMESI_MS, pencereyi_kapat)

	speech.speak([
		_("Hesabınıza bağlanıldı."),
		CallbackCommand(ana_pencereye_don, name="Dosya Arşivim bağlantı bildirimi tamamlandı"),
	])


class YuklemeYoneticisi:
	"""Kalıcı kuyruktaki dosyaları sırayla arka planda yükler."""

	def __init__(self):
		self.depo = YuklemeKuyrugu()
		self.parcacik = None
		self.kilit = threading.RLock()
		self.dinleyiciler = weakref.WeakSet()
		self.durduruldu = False
		self.duraklatildi = False
		self.aktif_kayit_id = None
		self.aktif_durdurma_olayi = None
		self.iptal_edilen_kayitlar = set()
		self.bildirilen_arsiv_islemleri = set()
		self.son_yuzdeler = {}
		self.uyandirma_olayi = threading.Event()

	def dinleyici_ekle(self, dinleyici):
		self.dinleyiciler.add(dinleyici)

	def dinleyici_cikar(self, dinleyici):
		self.dinleyiciler.discard(dinleyici)

	def ekle(self, eposta, klasor, yerel_yollar):
		sonuc = self.depo.ekle(eposta, klasor, yerel_yollar)
		if not sonuc["eklenenler"]:
			return sonuc
		self.uyandirma_olayi.set()
		self.baslat()
		return sonuc

	def baslat(self):
		with self.kilit:
			if self.durduruldu or self.duraklatildi or not HESAP_DURUMU.bagli_mi:
				return
			if self.parcacik and self.parcacik.is_alive():
				self.uyandirma_olayi.set()
				return
			self.uyandirma_olayi.clear()
			self.parcacik = threading.Thread(target=self._calistir, daemon=True)
			self.parcacik.start()

	def devam_et(self):
		"""Eklentiler yeniden yüklendiğinde kalıcı kuyruğu yeniden başlatır."""
		with self.kilit:
			self.durduruldu = False
		self.baslat()

	def durdur(self):
		with self.kilit:
			self.durduruldu = True
			if self.aktif_durdurma_olayi:
				self.aktif_durdurma_olayi.set()

	def yuklemeleri_duraklat(self):
		"""Etkin aktarımı keser; kuyruk kayıtlarını korur."""
		with self.kilit:
			self.duraklatildi = True
			if self.aktif_durdurma_olayi:
				self.aktif_durdurma_olayi.set()
		wx.CallAfter(self._duraklatma_durumu_bildir)

	def yuklemeleri_baslat(self):
		"""Duraklatılmış kalıcı kuyruğu yeniden çalıştırır."""
		with self.kilit:
			self.duraklatildi = False
		self.baslat()
		wx.CallAfter(self._duraklatma_durumu_bildir)

	def yuklemeleri_iptal_et(self, eposta):
		"""Bekleyenleri kaldırır; sunucuya ulaşanları doğrulanana kadar korur."""
		with self.kilit:
			iptal_edilenler = self.depo.epostadakileri_iptal_et(eposta)
			iptal_edilen_kayit_idleri = {kayit["id"] for kayit in iptal_edilenler}
			uzaktan_kaldirilacak_sayi = sum(
				kayit["durum"] in ("yükleniyor", "arşivleniyor", "iptal_ediliyor", "iptal_dogrulaniyor")
				for kayit in iptal_edilenler
			)
			if self.aktif_kayit_id in iptal_edilen_kayit_idleri and self.aktif_durdurma_olayi:
				self.iptal_edilen_kayitlar.add(self.aktif_kayit_id)
				self.aktif_durdurma_olayi.set()
			if uzaktan_kaldirilacak_sayi:
				self.duraklatildi = False
			self.uyandirma_olayi.set()
		self.baslat()
		wx.CallAfter(self._duraklatma_durumu_bildir)
		return len(iptal_edilenler), uzaktan_kaldirilacak_sayi

	def hatali_yuklemeleri_yeniden_dene(self, eposta):
		"""Hatalı yükleme kayıtlarını yeniden kuyruğa alır."""
		sayi = self.depo.hatalilari_beklemeye_al(eposta)
		if sayi:
			self.baslat()
			wx.CallAfter(self._duraklatma_durumu_bildir)
		return sayi

	def etkin_yuklemeyi_duraklat(self):
		"""O anki aktarımı keser; kalıcı kuyruk kaydını korur."""
		with self.kilit:
			if self.aktif_durdurma_olayi:
				self.aktif_durdurma_olayi.set()

	def yukleme_sayisi(self, eposta):
		return self.depo.epostadaki_sayi(eposta)

	def klasordeki_durumler(self, eposta, klasor):
		return self.depo.klasordekileri_al(eposta, klasor)

	def sunucuda_gorunen_yuklemeleri_tamamla(self, eposta, klasor_dosyalari):
		"""Başarılı eşitlemede sunucuda görünen eski işleniyor kayıtlarını uzlaştırır."""
		try:
			tamamlananlar = self.depo.sunucuda_gorunen_yuklemeleri_tamamla(eposta, klasor_dosyalari)
		except Exception:
			logHandler.log.exception("Dosya Arşivim yükleme kuyruğu sunucu listesiyle uzlaştırılamadı.")
			return []
		if not tamamlananlar:
			self.baslat()
			return []
		tamamlanan_kimlikleri = {kayit["id"] for kayit in tamamlananlar}
		with self.kilit:
			if self.aktif_kayit_id in tamamlanan_kimlikleri and self.aktif_durdurma_olayi:
				self.aktif_durdurma_olayi.set()
			for kayit_id in tamamlanan_kimlikleri:
				self.bildirilen_arsiv_islemleri.discard(kayit_id)
				self.son_yuzdeler.pop(kayit_id, None)
		self.uyandirma_olayi.set()
		self.baslat()
		return tamamlananlar

	def _aktif_kaydi_temizle(self, kayit_id):
		with self.kilit:
			if self.aktif_kayit_id == kayit_id:
				self.aktif_kayit_id = None
				self.aktif_durdurma_olayi = None

	def _calistir(self):
		"""Kuyruk işçisini beklenmedik hatalardan sonra güvenli biçimde yeniden başlatır."""
		yeniden_baslat = False
		aktif_kayit_id = None
		try:
			self._calistir_dongusu()
		except Exception:
			logHandler.log.exception("Dosya Arşivim yükleme yöneticisi beklenmedik biçimde durdu; yeniden başlatılacak.")
			with self.kilit:
				aktif_kayit_id = self.aktif_kayit_id
			if aktif_kayit_id is not None:
				try:
					self.depo.beklenmedik_kesintiyi_kurtar(aktif_kayit_id)
				except Exception:
					logHandler.log.exception("Yarım kalan Dosya Arşivim yükleme kaydı kurtarılamadı.")
			yeniden_baslat = True
		finally:
			if aktif_kayit_id is not None:
				self._aktif_kaydi_temizle(aktif_kayit_id)
			with self.kilit:
				if self.parcacik is threading.current_thread():
					self.parcacik = None
		if yeniden_baslat and not self.durduruldu and not self.duraklatildi and HESAP_DURUMU.bagli_mi:
			self.uyandirma_olayi.clear()
			self.uyandirma_olayi.wait(5)
			self.baslat()

	def _calistir_dongusu(self):
		while not self.durduruldu and not self.duraklatildi and HESAP_DURUMU.bagli_mi:
			eposta = HESAP_DURUMU.eposta
			istem = HESAP_DURUMU.istem
			self.uyandirma_olayi.clear()
			kayit = self.depo.siradakini_al(eposta)
			if not kayit:
				with self.kilit:
					if self.uyandirma_olayi.is_set():
						continue
					self.parcacik = None
				return
			if kayit["durum"] in ("iptal_ediliyor", "iptal_dogrulaniyor"):
				with self.kilit:
					self.aktif_kayit_id = kayit["id"]
					self.aktif_durdurma_olayi = threading.Event()
				dosya_adi = os.path.basename(kayit["yerel_yol"])
				if kayit["durum"] == "iptal_ediliyor":
					try:
						istem.dosya_sil(eposta, kayit["klasor"], dosya_adi)
					except HesapHatasi as hata:
						try:
							gorunuyor_mu = istem.dosya_arsivde_mi(eposta, kayit["klasor"], dosya_adi)
						except HesapHatasi:
							gorunuyor_mu = None
						if gorunuyor_mu is None:
							self.aktif_durdurma_olayi.wait(5)
						elif gorunuyor_mu:
							self.depo.tamamlandi(kayit["id"])
							wx.CallAfter(self._iptal_hatasi_bildir, kayit, str(hata))
						else:
							self.depo.tamamlandi(kayit["id"])
							wx.CallAfter(self._iptal_tamamlandi_bildir, kayit)
						self._aktif_kaydi_temizle(kayit["id"])
						continue
					except Exception:
						logHandler.log.exception("İptal edilen yükleme sunucudan kaldırılırken beklenmeyen hata oluştu.")
						self.aktif_durdurma_olayi.wait(5)
						self._aktif_kaydi_temizle(kayit["id"])
						continue
					self.depo.iptal_dogrulaniyor(kayit["id"])
					kayit["durum"] = "iptal_dogrulaniyor"
					wx.CallAfter(self._durum_bildir, kayit)
				try:
					gorunuyor_mu = istem.dosya_arsivde_mi(eposta, kayit["klasor"], dosya_adi)
				except HesapHatasi:
					gorunuyor_mu = True
				if not gorunuyor_mu:
					self.depo.tamamlandi(kayit["id"])
					wx.CallAfter(self._iptal_tamamlandi_bildir, kayit)
				elif not self.aktif_durdurma_olayi.wait(5):
					pass
				self._aktif_kaydi_temizle(kayit["id"])
				continue
			if kayit["durum"] == "arşivleniyor":
				with self.kilit:
					self.aktif_kayit_id = kayit["id"]
					self.aktif_durdurma_olayi = threading.Event()
				if kayit["id"] not in self.bildirilen_arsiv_islemleri:
					self.bildirilen_arsiv_islemleri.add(kayit["id"])
					wx.CallAfter(self._arsiv_isleme_bildir, kayit)
				try:
					gorunuyor_mu = not self.aktif_durdurma_olayi.is_set() and HESAP_DURUMU.istem.dosya_arsivde_mi(
						eposta, kayit["klasor"], os.path.basename(kayit["yerel_yol"])
					)
				except HesapHatasi:
					gorunuyor_mu = False
				if kayit["id"] in self.iptal_edilen_kayitlar:
					pass
				elif gorunuyor_mu:
					self.depo.tamamlandi(kayit["id"])
					self.bildirilen_arsiv_islemleri.discard(kayit["id"])
					wx.CallAfter(self._basari_bildir, kayit, os.path.basename(kayit["yerel_yol"]))
				elif not self.aktif_durdurma_olayi.wait(5):
					pass
				self._aktif_kaydi_temizle(kayit["id"])
				self.iptal_edilen_kayitlar.discard(kayit["id"])
				continue
			wx.CallAfter(
				self._durum_mesaji_bildir,
				_("Dosya yükleniyor: {dosya}").format(dosya=os.path.basename(kayit["yerel_yol"])),
			)
			durdurma_olayi = threading.Event()
			with self.kilit:
				self.aktif_kayit_id = kayit["id"]
				self.aktif_durdurma_olayi = durdurma_olayi
			try:
				istem.dosya_yukle(
					eposta,
					kayit["klasor"],
					kayit["yerel_yol"],
					lambda gonderilen, toplam: self._ilerlemeyi_bildir(kayit, gonderilen, toplam),
					durdurma_olayi,
					turev_uret=not AYARLAR.turev_uretimini_kapat,
				)
			except YuklemeDuraklatildi:
				self.son_yuzdeler.pop(kayit["id"], None)
				if kayit["id"] not in self.iptal_edilen_kayitlar:
					self.depo.beklemeye_al(kayit["id"])
					kayit["durum"] = "bekliyor"
					kayit.pop("yuzde", None)
					wx.CallAfter(self._durum_bildir, kayit)
				self._aktif_kaydi_temizle(kayit["id"])
				self.iptal_edilen_kayitlar.discard(kayit["id"])
				continue
			except HesapHatasi as hata:
				if durdurma_olayi.is_set():
					self.son_yuzdeler.pop(kayit["id"], None)
					if kayit["id"] not in self.iptal_edilen_kayitlar:
						self.depo.beklemeye_al(kayit["id"])
						kayit["durum"] = "bekliyor"
						wx.CallAfter(self._durum_bildir, kayit)
					self._aktif_kaydi_temizle(kayit["id"])
					self.iptal_edilen_kayitlar.discard(kayit["id"])
					continue
				self.son_yuzdeler.pop(kayit["id"], None)
				self.depo.hatali(kayit["id"], str(hata))
				wx.CallAfter(self._hata_bildir, kayit, str(hata))
				self._aktif_kaydi_temizle(kayit["id"])
				continue
			except Exception:
				logHandler.log.exception("Dosya Arşivim yükleme kuyruğunda beklenmeyen hata oluştu.")
				self.son_yuzdeler.pop(kayit["id"], None)
				hata = _("Dosya yüklenemedi.")
				self.depo.hatali(kayit["id"], hata)
				wx.CallAfter(self._hata_bildir, kayit, hata)
				self._aktif_kaydi_temizle(kayit["id"])
				continue
			iptal_edildi = kayit["id"] in self.iptal_edilen_kayitlar
			if iptal_edildi:
				self._aktif_kaydi_temizle(kayit["id"])
				self.iptal_edilen_kayitlar.discard(kayit["id"])
				continue
			if self.duraklatildi:
				self.son_yuzdeler.pop(kayit["id"], None)
				self.depo.beklemeye_al(kayit["id"])
				self._aktif_kaydi_temizle(kayit["id"])
				continue
			self.depo.arsivleniyor(kayit["id"])
			self.son_yuzdeler.pop(kayit["id"], None)
			kayit["durum"] = "arşivleniyor"
			wx.CallAfter(self._durum_bildir, kayit)
			self._aktif_kaydi_temizle(kayit["id"])

	def _durum_bildir(self, kayit):
		for dinleyici in list(self.dinleyiciler):
			dinleyici.yukleme_durumu_degisti(kayit)

	def _duraklatma_durumu_bildir(self):
		for dinleyici in list(self.dinleyiciler):
			dinleyici.yukleme_durumu_degisti(None)

	def _durum_mesaji_bildir(self, mesaj):
		"""Pencere açıkken veya bildirimler etkinse yükleme durumunu duyurur."""
		if self.dinleyiciler or AYARLAR.bildirimleri_goster:
			ui.message(mesaj)

	def _arsiv_isleme_bildir(self, kayit):
		"""Aktarım bittiğinde arşiv işlemesinin arka planda süreceğini bildirir."""
		self._durum_bildir(kayit)
		if self.dinleyiciler or AYARLAR.bildirimleri_goster:
			dosya_adi = os.path.basename(kayit["yerel_yol"])
			wx.MessageBox(
				_(
					"{dosya} dosyasının yüklemesi tamamlandı.\n\n"
					"Dosyanın arşivde işlenmesi, sunucu yoğunluğuna bağlı olarak zaman alabilir. "
					"Bu sırada çalışmalarınıza devam edebilirsiniz."
				).format(dosya=dosya_adi),
				_("Yükleme tamamlandı"),
				wx.OK | wx.ICON_INFORMATION,
			)

	def _ilerlemeyi_bildir(self, kayit, gonderilen, toplam):
		if not toplam:
			return
		yuzde = min(100, int(gonderilen * 100 / toplam))
		son_yuzde = self.son_yuzdeler.get(kayit["id"], 0)
		if yuzde < son_yuzde + 10 and yuzde != 100:
			return
		self.son_yuzdeler[kayit["id"]] = yuzde
		self.depo.ilerlemeyi_guncelle(kayit["id"], yuzde)
		kayit["yuzde"] = yuzde
		wx.CallAfter(self._durum_bildir, kayit)
		wx.CallAfter(
			self._durum_mesaji_bildir,
			_("Dosya yükleniyor: {dosya}, yüzde {oran}").format(
				dosya=os.path.basename(kayit["yerel_yol"]), oran=yuzde
			),
		)

	def _basari_bildir(self, kayit, dosya_adi):
		dinleyiciler = list(self.dinleyiciler)
		for dinleyici in dinleyiciler:
			dinleyici.yukleme_tamamlandi(kayit, dosya_adi)

	def _hata_bildir(self, kayit, hata):
		wx.MessageBox(hata, _("Dosya yükle"), wx.OK | wx.ICON_ERROR)
		for dinleyici in list(self.dinleyiciler):
			dinleyici.yukleme_hatali(kayit, hata)

	def _iptal_tamamlandi_bildir(self, kayit):
		for dinleyici in list(self.dinleyiciler):
			dinleyici.yukleme_iptali_tamamlandi(kayit)

	def _iptal_hatasi_bildir(self, kayit, hata):
		dosya_adi = os.path.basename(kayit["yerel_yol"])
		ui.message(_("{dosya} yüklemesi iptal edilemedi. Dosya sunucuda bırakıldı.").format(dosya=dosya_adi))
		for dinleyici in list(self.dinleyiciler):
			dinleyici.yukleme_iptali_hatali(kayit, hata)


YUKLEME_YONETICISI = YuklemeYoneticisi()


class DosyaArsivimPenceresi(wx.Frame):
	"""Ana pencere. Bağlı hesapta ileride dosya listesi gösterilecektir."""

	SAYFA_BASINA_DOSYA = 100

	KIMLIK_BAGLAN = wx.NewIdRef()
	KIMLIK_KES = wx.NewIdRef()
	KIMLIK_CIKIS = wx.NewIdRef()
	KIMLIK_DOSYA_YUKLE = wx.NewIdRef()
	KIMLIK_INDIR = wx.NewIdRef()
	KIMLIK_BAGLANTI_KOPYALA = wx.NewIdRef()
	KIMLIK_DOSYA_BILGILERI = wx.NewIdRef()
	KIMLIK_SIL = wx.NewIdRef()
	KIMLIK_BILDIRIMLER = wx.NewIdRef()
	KIMLIK_TUREV_URETIMINI_KAPAT = wx.NewIdRef()
	KIMLIK_TURETILMIS_DOSYALAR = wx.NewIdRef()
	KIMLIK_YUKLEMELERI_DURAKLAT = wx.NewIdRef()
	KIMLIK_YUKLEMELERI_IPTAL_ET = wx.NewIdRef()
	KIMLIK_HATALI_YUKLEMELERI_YENIDEN_DENE = wx.NewIdRef()
	KIMLIK_SUNUCU_ISLEMLERI = wx.NewIdRef()
	KIMLIK_HAKKINDA = wx.NewIdRef()
	KIMLIK_KULLANIM_KILAVUZU = wx.NewIdRef()

	def __init__(self, parent, kapanis_islevi, onbellek=None):
		super().__init__(parent, title=_("Dosya Arşivim"), style=wx.DEFAULT_FRAME_STYLE | wx.RESIZE_BORDER)
		self.kapanis_islevi = kapanis_islevi
		self.onbellek = onbellek
		self.aktif_klasor = None
		self.onceki_klasor = None
		self.klasor_dosyalari = {}
		self.gorunen_klasorler = {}
		self.klasor_yukleniyor = set()
		self.gorunen_dosyalar = {}
		self.dosya_ayrinti_indeksi = 0
		self.dosya_sayfasi = 0
		self.onceki_sayfa_ogesi = None
		self.sonraki_sayfa_ogesi = None
		self.esitleme_devam_ediyor = False
		self.esitleme_hata_gosterilsin = False
		self.dogrulanan_silmeler = set()
		self.kapatildi = False
		YUKLEME_YONETICISI.dinleyici_ekle(self)
		self._menu_olustur()
		self.icerigi_olustur()
		self.Bind(wx.EVT_CLOSE, self.kapat)
		self.SetSize((620, 420))
		self.CentreOnScreen()
		self.arayuzu_yenile()
		wx.CallAfter(self.hesap_yok_uyarisi_goster)
		wx.CallAfter(self._esitlemeyi_baslat)
		wx.CallAfter(self._bekleyen_silmeleri_surdur)

	def _menu_olustur(self):
		menu_cubugu = wx.MenuBar()
		hesap_menu = wx.Menu()
		hesap_menu.Append(self.KIMLIK_BAGLAN, _("&Bağlan\tAlt+B"))
		hesap_menu.Append(self.KIMLIK_KES, _("Bağlantıyı &kes\tAlt+K"))
		hesap_menu.AppendSeparator()
		hesap_menu.Append(self.KIMLIK_CIKIS, _("Çıkış\tAlt+F4"))
		menu_cubugu.Append(hesap_menu, _("&Hesap"))
		self.ayarlar_menu = wx.Menu()
		self.turetilmis_dosyalar_menu_ogesi = None
		self.ayarlar_menu.AppendCheckItem(self.KIMLIK_BILDIRIMLER, _("Bildirimleri göster"))
		self.ayarlar_menu.Check(self.KIMLIK_BILDIRIMLER, AYARLAR.bildirimleri_goster)
		self.ayarlar_menu.AppendCheckItem(self.KIMLIK_TUREV_URETIMINI_KAPAT, _("Türev üretimini kapat"))
		self.ayarlar_menu.Check(self.KIMLIK_TUREV_URETIMINI_KAPAT, AYARLAR.turev_uretimini_kapat)
		self._turetilmis_dosyalar_menu_ogesini_guncelle()
		menu_cubugu.Append(self.ayarlar_menu, _("&Ayarlar"))
		yuklemeler_menu = wx.Menu()
		self.yuklemeleri_duraklat_ogesi = yuklemeler_menu.Append(
			self.KIMLIK_YUKLEMELERI_DURAKLAT, _("Yüklemeleri &duraklat")
		)
		yuklemeler_menu.Append(self.KIMLIK_YUKLEMELERI_IPTAL_ET, _("Yüklemeleri &iptal et"))
		yuklemeler_menu.Append(
			self.KIMLIK_HATALI_YUKLEMELERI_YENIDEN_DENE,
			_("Hatalı yüklemeleri &yeniden dene"),
		)
		yuklemeler_menu.AppendSeparator()
		yuklemeler_menu.Append(self.KIMLIK_SUNUCU_ISLEMLERI, _("&Sunucu işlemleri"))
		menu_cubugu.Append(yuklemeler_menu, _("&Yüklemeler"))
		yardim_menu = wx.Menu()
		yardim_menu.Append(self.KIMLIK_KULLANIM_KILAVUZU, _("&Kullanım kılavuzu"))
		yardim_menu.Append(self.KIMLIK_HAKKINDA, _("&Hakkında"))
		menu_cubugu.Append(yardim_menu, _("&Yardım"))
		self.SetMenuBar(menu_cubugu)
		self.Bind(wx.EVT_MENU, self.baglan_secildi, id=self.KIMLIK_BAGLAN)
		self.Bind(wx.EVT_MENU, self.baglanti_kes_secildi, id=self.KIMLIK_KES)
		self.Bind(wx.EVT_MENU, self.cikis_secildi, id=self.KIMLIK_CIKIS)
		self.Bind(wx.EVT_MENU, self.dosya_yukle_secildi, id=self.KIMLIK_DOSYA_YUKLE)
		self.Bind(wx.EVT_MENU, self.dosya_indir_secildi, id=self.KIMLIK_INDIR)
		self.Bind(wx.EVT_MENU, self.baglantiyi_panoya_kopyala_secildi, id=self.KIMLIK_BAGLANTI_KOPYALA)
		self.Bind(wx.EVT_MENU, self.dosya_bilgileri_secildi, id=self.KIMLIK_DOSYA_BILGILERI)
		self.Bind(wx.EVT_MENU, self.dosya_sil_secildi, id=self.KIMLIK_SIL)
		self.Bind(wx.EVT_MENU, self.bildirimler_degisti, id=self.KIMLIK_BILDIRIMLER)
		self.Bind(wx.EVT_MENU, self.turev_uretimi_degisti, id=self.KIMLIK_TUREV_URETIMINI_KAPAT)
		self.Bind(wx.EVT_MENU, self.turetilmis_dosyalar_degisti, id=self.KIMLIK_TURETILMIS_DOSYALAR)
		self.Bind(wx.EVT_MENU, self.yuklemeleri_duraklat_baslat_secildi, id=self.KIMLIK_YUKLEMELERI_DURAKLAT)
		self.Bind(wx.EVT_MENU, self.yuklemeleri_iptal_et_secildi, id=self.KIMLIK_YUKLEMELERI_IPTAL_ET)
		self.Bind(
			wx.EVT_MENU,
			self.hatali_yuklemeleri_yeniden_dene_secildi,
			id=self.KIMLIK_HATALI_YUKLEMELERI_YENIDEN_DENE,
		)
		self.Bind(wx.EVT_MENU, self.sunucu_islemleri_secildi, id=self.KIMLIK_SUNUCU_ISLEMLERI)
		self.Bind(wx.EVT_MENU, self.kullanim_kilavuzu_secildi, id=self.KIMLIK_KULLANIM_KILAVUZU)
		self.Bind(wx.EVT_MENU, self.hakkinda_secildi, id=self.KIMLIK_HAKKINDA)

	def bildirimler_degisti(self, event):
		AYARLAR.bildirimleri_goster = event.IsChecked()
		try:
			AYARLAR.kaydet()
		except OSError:
			wx.MessageBox(_("Ayarlar kaydedilemedi."), _("Ayarlar"), wx.OK | wx.ICON_ERROR, self)

	def _turetilmis_dosyalar_menu_ogesini_guncelle(self):
		"""Türev üretimi kapalıyken ilgisiz görüntüleme seçeneğini menüden kaldırır."""
		if AYARLAR.turev_uretimini_kapat:
			if self.turetilmis_dosyalar_menu_ogesi is not None:
				self.ayarlar_menu.Delete(self.turetilmis_dosyalar_menu_ogesi)
				self.turetilmis_dosyalar_menu_ogesi = None
			return
		if self.turetilmis_dosyalar_menu_ogesi is None:
			self.turetilmis_dosyalar_menu_ogesi = self.ayarlar_menu.AppendCheckItem(
				self.KIMLIK_TURETILMIS_DOSYALAR,
				_("Türetilmiş dosyaları göster"),
			)
		self.ayarlar_menu.Check(self.KIMLIK_TURETILMIS_DOSYALAR, AYARLAR.turetilmis_dosyalari_goster)

	def turev_uretimi_degisti(self, event):
		AYARLAR.turev_uretimini_kapat = event.IsChecked()
		if AYARLAR.turev_uretimini_kapat:
			AYARLAR.turetilmis_dosyalari_goster = False
		try:
			AYARLAR.kaydet()
		except OSError:
			wx.MessageBox(_("Ayarlar kaydedilemedi."), _("Ayarlar"), wx.OK | wx.ICON_ERROR, self)
			return
		self._turetilmis_dosyalar_menu_ogesini_guncelle()
		self._turetilmis_dosya_gorunumunu_yenile()

	def turetilmis_dosyalar_degisti(self, event):
		AYARLAR.turetilmis_dosyalari_goster = event.IsChecked()
		try:
			AYARLAR.kaydet()
		except OSError:
			wx.MessageBox(_("Ayarlar kaydedilemedi."), _("Ayarlar"), wx.OK | wx.ICON_ERROR, self)
			return
		self._turetilmis_dosya_gorunumunu_yenile()

	def _turetilmis_dosya_gorunumunu_yenile(self):
		if self.aktif_klasor and self.onbellek:
			try:
				self.klasor_dosyalari[self.aktif_klasor] = self.onbellek.klasordeki_dosyalari_al(
					HESAP_DURUMU.eposta, self.aktif_klasor, AYARLAR.turetilmis_dosyalari_goster
				)
			except (OSError, sqlite3.Error):
				self.onbellek = None
		self.arayuzu_yenile()
		self._esitlemeyi_baslat()

	def yuklemeleri_duraklat_baslat_secildi(self, event):
		if not HESAP_DURUMU.bagli_mi:
			return
		if YUKLEME_YONETICISI.duraklatildi:
			YUKLEME_YONETICISI.yuklemeleri_baslat()
		else:
			YUKLEME_YONETICISI.yuklemeleri_duraklat()
		self.arayuzu_yenile()

	def yuklemeleri_iptal_et_secildi(self, event):
		if not HESAP_DURUMU.bagli_mi:
			return
		sayi = YUKLEME_YONETICISI.yukleme_sayisi(HESAP_DURUMU.eposta)
		if not sayi:
			wx.MessageBox(_("İptal edilecek yükleme bulunamadı."), _("Yüklemeleri iptal et"), wx.OK | wx.ICON_INFORMATION, self)
			return
		onay = wx.MessageBox(
			_("Yükleme işlemleri iptal edilecek.\nDevam etmek istiyor musunuz?"),
			_("Yüklemeleri iptal et"),
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
			self,
		)
		if onay != wx.YES:
			return
		_, uzaktan_kaldirilacak_sayi = YUKLEME_YONETICISI.yuklemeleri_iptal_et(HESAP_DURUMU.eposta)
		if uzaktan_kaldirilacak_sayi == 1:
			ui.message(_("Dosyanız sunucudan kaldırılıyor. Bu sırada çalışmalarınıza devam edebilirsiniz."))
		elif uzaktan_kaldirilacak_sayi > 1:
			ui.message(_("Dosyalarınız sunucudan kaldırılıyor. Bu sırada çalışmalarınıza devam edebilirsiniz."))
		self.arayuzu_yenile()

	def hatali_yuklemeleri_yeniden_dene_secildi(self, event):
		if not HESAP_DURUMU.bagli_mi:
			return
		sayi = YUKLEME_YONETICISI.hatali_yuklemeleri_yeniden_dene(HESAP_DURUMU.eposta)
		if not sayi:
			wx.MessageBox(
				_("Yeniden denenecek hatalı yükleme bulunamadı."),
				_("Hatalı yüklemeleri yeniden dene"),
				wx.OK | wx.ICON_INFORMATION,
				self,
			)
			return
		self.arayuzu_yenile()

	def sunucu_islemleri_secildi(self, event):
		if not HESAP_DURUMU.bagli_mi:
			return
		pencere = SunucuIslemleriPenceresi(self)
		try:
			pencere.ShowModal()
		finally:
			pencere.Destroy()

	def kullanim_kilavuzu_secildi(self, event):
		eklenti = addonHandler.getCodeAddon()
		kilavuz_yolu = eklenti.getDocFilePath() if eklenti else None
		if not kilavuz_yolu:
			wx.MessageBox(_("Kullanım kılavuzu dosyası bulunamadı."), _("Kullanım kılavuzu"), wx.OK | wx.ICON_ERROR, self)
			return
		try:
			os.startfile(kilavuz_yolu)
		except OSError:
			wx.MessageBox(_("Kullanım kılavuzu açılamadı."), _("Kullanım kılavuzu"), wx.OK | wx.ICON_ERROR, self)

	def hakkinda_secildi(self, event):
		metin = "\n".join((
			_("Dosya Arşivim"),
			_("Sürüm: {surum}").format(surum=EKLENTI_SURUMU),
			_("Geliştiren: Mehmet Aykurt"),
			_("E-posta: m.aykurt38@gmail.com"),
			_("Web sitesi: mehmetaykurt.com.tr"),
		))
		wx.MessageBox(metin, _("Hakkında"), wx.OK | wx.ICON_INFORMATION, self)

	def icerigi_olustur(self):
		self.panel = wx.Panel(self)
		self.icerik_sizer = wx.BoxSizer(wx.VERTICAL)
		self.panel.SetSizer(self.icerik_sizer)
		self.dosya_listesi = wx.ListBox(self.panel)
		self.icerik_sizer.Add(self.dosya_listesi, 1, wx.EXPAND | wx.ALL, 8)
		self.dosya_listesi.Bind(wx.EVT_CONTEXT_MENU, self.icerik_menusu_ac)
		self.dosya_listesi.Bind(wx.EVT_LISTBOX, self.liste_secimi_degisti)
		self.Bind(wx.EVT_CHAR_HOOK, self.liste_tusuna_basildi)

	def liste_secimi_degisti(self, event):
		"""Yeni dosyaya geçildiğinde ayrıntı dolaşımını dosya adına döndürür."""
		self.dosya_ayrinti_indeksi = 0
		event.Skip()

	def liste_tusuna_basildi(self, event):
		"""Liste klavye komutlarını işler."""
		if wx.Window.FindFocus() is not self.dosya_listesi:
			event.Skip()
			return
		tus = event.GetKeyCode()
		numpad_enter = getattr(wx, "WXK_NUMPAD_ENTER", wx.WXK_RETURN)
		if tus in (wx.WXK_RETURN, numpad_enter):
			secili_oge = self.dosya_listesi.GetStringSelection()
			if self.aktif_klasor and secili_oge == self.onceki_sayfa_ogesi:
				self.sayfayi_degistir(-1)
				return
			if self.aktif_klasor and secili_oge == self.sonraki_sayfa_ogesi:
				self.sayfayi_degistir(1)
				return
			self.klasore_gir()
			return
		if event.ControlDown() and tus in (ord("V"), ord("v")):
			self.panodan_dosya_yukle()
			return
		if event.AltDown() and tus in (ord("S"), ord("s")):
			self.dosya_sil_secildi(None)
			return
		if tus in (wx.WXK_LEFT, wx.WXK_RIGHT):
			self.dosya_ayrintisini_seslendir(tus == wx.WXK_RIGHT)
			return
		if tus == wx.WXK_ESCAPE and self.aktif_klasor:
			self.ana_dizine_don()
			return
		event.Skip()

	@staticmethod
	def _dosya_boyutunu_bicimlendir(boyut):
		if boyut is None or boyut < 0:
			return _("Bilinmiyor")
		if boyut < 1024:
			return _("{boyut} bayt").format(boyut=boyut)
		for birim in (_("KB"), _("MB"), _("GB"), _("TB")):
			boyut /= 1024
			if boyut < 1024:
				return _("{boyut:.1f} {birim}").format(boyut=boyut, birim=birim).replace(".", ",")
		return _("Bilinmiyor")

	@staticmethod
	def _dosya_turunu_belirle(bilgi):
		bicim = bilgi.get("bicim")
		if bicim:
			bilinen_bicimler = {
				"Text": _("Metin belgesi"),
				"WAVE": _("WAVE ses dosyası"),
			}
			return bilinen_bicimler.get(bicim, bicim)
		uzanti = os.path.splitext(bilgi["ad"])[1].lstrip(".").upper()
		if uzanti:
			return _("{uzanti} dosyası").format(uzanti=uzanti)
		return _("Bilinmiyor")

	def dosya_ayrintisini_seslendir(self, ileri):
		"""Sağ-sol okla seçili dosyanın temel ayrıntılarını seslendirir."""
		bilgi = self.gorunen_dosyalar.get(self.dosya_listesi.GetStringSelection())
		if not bilgi:
			return
		ayrintilar = [
			_("Dosya adı: {ad}").format(ad=bilgi["ad"]),
			_("Tür: {tur}").format(tur=self._dosya_turunu_belirle(bilgi)),
			_("Boyut: {boyut}").format(boyut=self._dosya_boyutunu_bicimlendir(bilgi.get("boyut"))),
			_("Yüklenme zamanı: {zaman}").format(
				zaman=datetime.fromtimestamp(bilgi["yukleme_zamani"]).strftime("%d.%m.%Y %H:%M")
				if bilgi.get("yukleme_zamani") else _("Bilinmiyor")
			),
			_("Durum: Yüklendi"),
		]
		yeni_indeks = self.dosya_ayrinti_indeksi + (1 if ileri else -1)
		if 0 <= yeni_indeks < len(ayrintilar):
			self.dosya_ayrinti_indeksi = yeni_indeks
			ui.message(ayrintilar[yeni_indeks])

	def klasore_gir(self):
		if not HESAP_DURUMU.bagli_mi or self.aktif_klasor:
			return
		secili_oge = self.dosya_listesi.GetStringSelection()
		klasor = self.gorunen_klasorler.get(secili_oge)
		if klasor not in HESAP_DURUMU.istem.varsayilan_klasorler:
			return
		self.onceki_klasor = klasor
		self.aktif_klasor = klasor
		self.dosya_sayfasi = 0
		self.klasor_yukleniyor.add(self.aktif_klasor)
		klasor = self.aktif_klasor
		if self.onbellek:
			try:
				self.klasor_dosyalari[klasor] = self.onbellek.klasordeki_dosyalari_al(
					HESAP_DURUMU.eposta, klasor, AYARLAR.turetilmis_dosyalari_goster
				)
				ilk_esitleme = not self.onbellek.ilk_esitleme_tamamlandi_mi(HESAP_DURUMU.eposta)
			except (OSError, sqlite3.Error):
				self.onbellek = None
			else:
				self.arayuzu_yenile()
				self.dosya_listesi.SetFocus()
				self._esitlemeyi_baslat(hata_goster=ilk_esitleme)
				return
		self.arayuzu_yenile()
		self.dosya_listesi.SetFocus()
		arka_planda(
			lambda: self._klasor_dosyalarini_al(klasor),
			lambda dosyalar, hata: self._klasor_dosyalari_alindi(klasor, dosyalar, hata),
		)

	def ana_dizine_don(self):
		self.aktif_klasor = None
		self.dosya_sayfasi = 0
		self.arayuzu_yenile()
		if self.onceki_klasor:
			indeks = self.dosya_listesi.FindString(
				HESAP_DURUMU.istem.klasor_gorunen_adi(self.onceki_klasor)
			)
			if indeks != wx.NOT_FOUND:
				self.dosya_listesi.SetSelection(indeks)
		self.dosya_listesi.SetFocus()

	def sayfayi_degistir(self, yon):
		"""Seçili klasördeki dosya sayfasını değiştirir."""
		if not self.aktif_klasor:
			return
		dosyalar = self.klasor_dosyalari.get(self.aktif_klasor, [])
		toplam_sayfa = max(1, (len(dosyalar) + self.SAYFA_BASINA_DOSYA - 1) // self.SAYFA_BASINA_DOSYA)
		yeni_sayfa = self.dosya_sayfasi + yon
		if not 0 <= yeni_sayfa < toplam_sayfa:
			return
		self.dosya_sayfasi = yeni_sayfa
		self.arayuzu_yenile()
		self.dosya_listesi.SetFocus()

	def _klasor_dosyalarini_al(self, klasor):
		return HESAP_DURUMU.istem.klasordeki_dosyalari_al(
			HESAP_DURUMU.eposta, klasor, AYARLAR.turetilmis_dosyalari_goster
		)

	def _klasor_dosyalari_alindi(self, klasor, dosyalar, hata):
		if self.kapatildi:
			return
		self.klasor_yukleniyor.discard(klasor)
		if hata:
			wx.MessageBox(hata, _("Dosya Arşivim"), wx.OK | wx.ICON_ERROR, self)
			return
		if self.aktif_klasor != klasor:
			return
		self.klasor_dosyalari[klasor] = dosyalar
		self.arayuzu_yenile()
		self.dosya_listesi.SetFocus()

	def _esitlemeyi_baslat(self, hata_goster=False):
		"""Sunucu listesini arka planda alır; arayüz SQL önbelleğini kullanmaya devam eder."""
		if not self.onbellek or not HESAP_DURUMU.bagli_mi:
			return
		self.esitleme_hata_gosterilsin = self.esitleme_hata_gosterilsin or hata_goster
		if self.esitleme_devam_ediyor:
			return
		eposta = HESAP_DURUMU.eposta
		istem = HESAP_DURUMU.istem
		turetilmisleri_goster = AYARLAR.turetilmis_dosyalari_goster
		self.esitleme_devam_ediyor = True
		arka_planda(
			lambda: istem.tum_dosyalari_al(eposta, turetilmisleri_goster),
			lambda dosyalar, hata: self._esitleme_tamamlandi(eposta, dosyalar, hata, turetilmisleri_goster),
		)

	def _esitleme_tamamlandi(self, eposta, klasor_dosyalari, hata, turetilmisleri_goster):
		if self.kapatildi:
			return
		self.esitleme_devam_ediyor = False
		if HESAP_DURUMU.eposta != eposta:
			self.esitleme_hata_gosterilsin = False
			return
		if hata:
			if self.aktif_klasor:
				self.klasor_yukleniyor.discard(self.aktif_klasor)
				self.arayuzu_yenile()
			if self.esitleme_hata_gosterilsin and self.aktif_klasor:
				wx.MessageBox(hata, _("Dosya Arşivim"), wx.OK | wx.ICON_ERROR, self)
			self.esitleme_hata_gosterilsin = False
			return
		try:
			self.onbellek.tum_dosyalari_esitle(
				eposta,
				HESAP_DURUMU.istem.varsayilan_klasorler,
				klasor_dosyalari,
			)
		except (OSError, sqlite3.Error):
			self.onbellek = None
			self.esitleme_hata_gosterilsin = False
			return
		YUKLEME_YONETICISI.sunucuda_gorunen_yuklemeleri_tamamla(eposta, klasor_dosyalari)
		self.esitleme_hata_gosterilsin = False
		if turetilmisleri_goster != AYARLAR.turetilmis_dosyalari_goster:
			self._esitlemeyi_baslat()
			return
		if self.aktif_klasor:
			try:
				self.klasor_dosyalari[self.aktif_klasor] = self.onbellek.klasordeki_dosyalari_al(
					eposta, self.aktif_klasor, AYARLAR.turetilmis_dosyalari_goster
				)
			except (OSError, sqlite3.Error):
				self.onbellek = None
				return
			self.klasor_yukleniyor.discard(self.aktif_klasor)
			self.arayuzu_yenile()
			self.dosya_listesi.SetFocus()

	def icerik_menusu_ac(self, event):
		"""Dosya listesinin sağ tık ve klavye içerik menüsünü açar."""
		if not self.aktif_klasor:
			return
		bilgi = self._secili_dosya_bilgisi()
		dosya_mi = bilgi is not None
		menu = wx.Menu()
		menu.Append(self.KIMLIK_DOSYA_YUKLE, _("Dosya &yükle\tAlt+Y"))
		menu.Append(self.KIMLIK_INDIR, _("&İndir\tAlt+İ"))
		menu.Append(self.KIMLIK_BAGLANTI_KOPYALA, _("&Bağlantıyı kopyala\tAlt+B"))
		menu.Append(self.KIMLIK_DOSYA_BILGILERI, _("&Dosya bilgileri\tAlt+D"))
		menu.Append(self.KIMLIK_SIL, _("&Sil\tAlt+S"))
		menu.Enable(self.KIMLIK_INDIR, dosya_mi)
		menu.Enable(self.KIMLIK_BAGLANTI_KOPYALA, dosya_mi)
		menu.Enable(self.KIMLIK_DOSYA_BILGILERI, dosya_mi)
		menu.Enable(self.KIMLIK_SIL, dosya_mi)
		self.dosya_listesi.PopupMenu(menu)
		menu.Destroy()

	def _secili_dosya_bilgisi(self):
		return self.gorunen_dosyalar.get(self.dosya_listesi.GetStringSelection())

	def dosya_indir_secildi(self, event):
		bilgi = self._secili_dosya_bilgisi()
		if not bilgi or not self.aktif_klasor:
			return
		with wx.FileDialog(
			self,
			_("İndir"),
			defaultFile=bilgi["ad"],
			style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
		) as pencere:
			if pencere.ShowModal() != wx.ID_OK:
				return
			hedef_yol = pencere.GetPath()
		indirme = IndirmePenceresi(self, self.aktif_klasor, bilgi["ad"], hedef_yol)
		wx.CallAfter(indirme.baslat)
		indirme.ShowModal()
		indirme.Destroy()

	def baglantiyi_panoya_kopyala_secildi(self, event):
		bilgi = self._secili_dosya_bilgisi()
		if not bilgi or not self.aktif_klasor:
			return
		baglanti = HESAP_DURUMU.istem.dosya_baglantisi(
			HESAP_DURUMU.eposta, self.aktif_klasor, bilgi["ad"]
		)
		self._baglantiyi_panoya_kopyala(baglanti)

	def _baglantiyi_panoya_kopyala(self, baglanti):
		if not wx.TheClipboard.Open():
			wx.MessageBox(_("Panoya erişilemedi."), _("Bağlantıyı panoya kopyala"), wx.OK | wx.ICON_ERROR, self)
			return
		try:
			wx.TheClipboard.SetData(wx.TextDataObject(baglanti))
		finally:
			wx.TheClipboard.Close()
		wx.CallLater(BAGLANTI_BILDIRIM_GECIKMESI_MS, self._baglanti_kopyalandi_bildir)

	def dosya_bilgileri_secildi(self, event):
		bilgi = self._secili_dosya_bilgisi()
		if not bilgi or not self.aktif_klasor:
			return
		baglanti = HESAP_DURUMU.istem.dosya_baglantisi(
			HESAP_DURUMU.eposta, self.aktif_klasor, bilgi["ad"]
		)
		metin = "\n".join([
			_("Ad: {ad}").format(ad=bilgi["ad"]),
			_("Tür: {tur}").format(tur=self._dosya_turunu_belirle(bilgi)),
			_("Boyut: {boyut}").format(boyut=self._dosya_boyutunu_bicimlendir(bilgi.get("boyut"))),
			_("Bağlantı: {bağlantı}").format(bağlantı=baglanti),
		])
		pencere = DosyaBilgileriPenceresi(self, metin)
		pencere.ShowModal()
		pencere.Destroy()

	def dosya_sil_secildi(self, event):
		bilgi = self._secili_dosya_bilgisi()
		if not bilgi or not self.aktif_klasor:
			return
		dosya_adi = bilgi["ad"]
		onay = wx.MessageBox(
			_(
				"{dosya} adlı dosyayı silmek istediğinizden emin misiniz?\n\n"
				"Bu işlem geri alınamaz. Silme isteği sunucuya iletilecektir. "
				"Dosyanın sunucudan kaldırılması, sunucu yoğunluğuna bağlı olarak zaman alabilir; "
				"bu sırada çalışmalarınıza devam edebilirsiniz."
			).format(dosya=dosya_adi),
			_("Dosyayı sil"),
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
			self,
		)
		if onay != wx.YES:
			return
		klasor = self.aktif_klasor
		eposta = HESAP_DURUMU.eposta
		istem = HESAP_DURUMU.istem
		islem_id = None
		if self.onbellek:
			try:
				islem_id, yeni_islem = self.onbellek.silmeyi_baslat_ve_durumu_al(
					eposta, klasor, dosya_adi
				)
			except (OSError, sqlite3.Error):
				self.onbellek = None
			else:
				self.klasor_dosyalari[klasor] = self.onbellek.klasordeki_dosyalari_al(
					eposta, klasor, AYARLAR.turetilmis_dosyalari_goster
				)
				self.arayuzu_yenile()
				self.dosya_listesi.SetFocus()
				if not yeni_islem:
					ui.message(_("{dosya} için silme işlemi zaten devam ediyor.").format(dosya=dosya_adi))
					return
		arka_planda(
			lambda: istem.dosya_sil(eposta, klasor, dosya_adi),
			lambda sonuc, hata: self._dosya_silindi(eposta, islem_id, klasor, dosya_adi, hata),
		)

	def _dosya_silindi(self, eposta, islem_id, klasor, dosya_adi, hata):
		if hata:
			if self.onbellek and islem_id is not None:
				try:
					self.onbellek.silme_hatali(eposta, islem_id, klasor, dosya_adi, hata)
					self.klasor_dosyalari[klasor] = self.onbellek.klasordeki_dosyalari_al(
						eposta, klasor, AYARLAR.turetilmis_dosyalari_goster
					)
				except (OSError, sqlite3.Error):
					self.onbellek = None
			if self.kapatildi:
				return
			if self.aktif_klasor == klasor:
				self.arayuzu_yenile()
				self.dosya_listesi.SetFocus()
			wx.MessageBox(hata, _("Dosyayı sil"), wx.OK | wx.ICON_ERROR, self)
			return
		if self.onbellek and islem_id is not None:
			try:
				self.onbellek.silme_dogrulaniyor(islem_id)
			except (OSError, sqlite3.Error):
				self.onbellek = None
		if self.onbellek and islem_id is not None:
			self._silme_dogrulamasini_baslat(eposta, islem_id, klasor, dosya_adi)
			return
		if self.kapatildi:
			return
		dosyalar = self.klasor_dosyalari.get(klasor, [])
		self.klasor_dosyalari[klasor] = [dosya for dosya in dosyalar if dosya["ad"] != dosya_adi]
		if self.aktif_klasor == klasor:
			self.arayuzu_yenile()
			self.dosya_listesi.SetFocus()

	def _silme_dogrulamasini_baslat(self, eposta, islem_id, klasor, dosya_adi):
		"""IA silmeyi gerçekten yansıtana kadar yerel silme kaydını korur."""
		if self.kapatildi or islem_id in self.dogrulanan_silmeler:
			return
		self.dogrulanan_silmeler.add(islem_id)
		arka_planda(
			lambda: not HESAP_DURUMU.istem.dosya_arsivde_mi(eposta, klasor, dosya_adi),
			lambda silindi, hata: self._silme_dogrulandi(
				eposta, islem_id, klasor, dosya_adi, silindi, hata
			),
		)

	def _silme_dogrulandi(self, eposta, islem_id, klasor, dosya_adi, silindi, hata):
		self.dogrulanan_silmeler.discard(islem_id)
		if self.kapatildi:
			return
		if hata or not silindi:
			wx.CallLater(
				10000,
				self._silme_dogrulamasini_baslat,
				eposta,
				islem_id,
				klasor,
				dosya_adi,
			)
			return
		if self.onbellek:
			try:
				self.onbellek.silme_tamamlandi(eposta, islem_id, klasor, dosya_adi)
			except (OSError, sqlite3.Error):
				self.onbellek = None
		dosyalar = self.klasor_dosyalari.get(klasor, [])
		self.klasor_dosyalari[klasor] = [dosya for dosya in dosyalar if dosya["ad"] != dosya_adi]
		if self.aktif_klasor == klasor:
			self.arayuzu_yenile()
			self.dosya_listesi.SetFocus()

	def _bekleyen_silmeleri_surdur(self):
		"""Önceki NVDA çalışmasından kalan silme işlemlerini arka planda sürdürür."""
		if not self.onbellek or not HESAP_DURUMU.bagli_mi:
			return
		eposta = HESAP_DURUMU.eposta
		istem = HESAP_DURUMU.istem
		try:
			islemler = self.onbellek.bekleyen_silmeleri_al(eposta)
		except (OSError, sqlite3.Error):
			self.onbellek = None
			return
		for islem in islemler:
			islem_id = islem["id"]
			klasor = islem["klasor"]
			dosya_adi = islem["dosya_adi"]
			if islem["durum"] == "dogrulaniyor":
				self._silme_dogrulamasini_baslat(eposta, islem_id, klasor, dosya_adi)
				continue
			arka_planda(
				lambda klasor=klasor, dosya_adi=dosya_adi: istem.dosya_sil(
					eposta, klasor, dosya_adi
				),
				lambda sonuc, hata, islem_id=islem_id, klasor=klasor, dosya_adi=dosya_adi:
				self._dosya_silindi(eposta, islem_id, klasor, dosya_adi, hata),
			)

	def _baglanti_kopyalandi_bildir(self):
		"""İçerik menüsü kapandıktan sonra kopyalama sonucunu seslendirir."""
		speech.speak([
			_("Bağlantı panoya kopyalandı."),
			CallbackCommand(lambda: None, name="Dosya Arşivim bağlantı kopyalama bildirimi tamamlandı"),
		])

	def dosya_yukle_secildi(self, event):
		if not self.aktif_klasor:
			wx.MessageBox(
				_("Dosya yüklemek için önce bir klasöre girin."),
				_("Dosya yükle"),
				wx.OK | wx.ICON_WARNING,
				self,
			)
			return
		with wx.FileDialog(
			self,
			_("Dosya yükle"),
			wildcard=_("Tüm dosyalar (*.*)|*.*"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
		) as pencere:
			if pencere.ShowModal() != wx.ID_OK:
				return
			self.dosya_yuklemeyi_baslat(pencere.GetPaths())

	def panodan_dosya_yukle(self):
		if not self.aktif_klasor:
			wx.MessageBox(
				_("Dosya yüklemek için önce bir klasöre girin."),
				_("Dosya yükle"),
				wx.OK | wx.ICON_WARNING,
				self,
			)
			return
		veri = wx.FileDataObject()
		if not wx.TheClipboard.Open():
			wx.MessageBox(_("Panoya erişilemedi."), _("Dosya yükle"), wx.OK | wx.ICON_ERROR, self)
			return
		try:
			dosyalar = veri.GetFilenames() if wx.TheClipboard.GetData(veri) else []
		finally:
			wx.TheClipboard.Close()
		if not dosyalar:
			wx.MessageBox(_("Panoda yüklenecek bir dosya bulunamadı."), _("Dosya yükle"), wx.OK | wx.ICON_WARNING, self)
			return
		self.dosya_yuklemeyi_baslat(dosyalar)

	def dosya_yuklemeyi_baslat(self, yerel_yollar):
		sonuc = YUKLEME_YONETICISI.ekle(HESAP_DURUMU.eposta, self.aktif_klasor, yerel_yollar)
		self.arayuzu_yenile()
		self.dosya_listesi.SetFocus()
		if sonuc["yinelenenler"]:
			dosya_adlari = ", ".join(os.path.basename(yol) for yol in sonuc["yinelenenler"])
			ui.message(
				_("Zaten yükleme kuyruğunda bulunan dosyalar yeniden eklenmedi: {dosyalar}").format(
					dosyalar=dosya_adlari
				)
			)

	def yukleme_tamamlandi(self, kayit, dosya_adi):
		klasor = kayit["klasor"]
		dosyalar = self.klasor_dosyalari.setdefault(klasor, [])
		try:
			boyut = os.path.getsize(kayit["yerel_yol"])
		except OSError:
			boyut = None
		bilgi = {
			"ad": dosya_adi,
			"boyut": boyut,
			"yukleme_zamani": int(time.time()),
			"bicim": None,
		}
		if self.onbellek:
			try:
				self.onbellek.dosya_yuklendi(kayit["eposta"], klasor, bilgi)
			except (OSError, sqlite3.Error):
				self.onbellek = None
		if not any(dosya["ad"] == dosya_adi for dosya in dosyalar):
			dosyalar.append(bilgi)
			dosyalar.sort(key=lambda dosya: dosya["ad"].casefold())
		if self.aktif_klasor == klasor:
			wx.CallLater(150, self._yuklenen_dosyayi_goster, dosya_adi)

	def yukleme_durumu_degisti(self, kayit):
		if kayit is None:
			self.arayuzu_yenile()
			return
		if self.aktif_klasor == kayit["klasor"]:
			self.arayuzu_yenile()

	def _yuklenen_dosyayi_goster(self, dosya_adi):
		if self.kapatildi:
			return
		dosyalar = self.klasor_dosyalari.get(self.aktif_klasor, [])
		for indeks, bilgi in enumerate(dosyalar):
			if bilgi["ad"] == dosya_adi:
				self.dosya_sayfasi = indeks // self.SAYFA_BASINA_DOSYA
				break
		self.arayuzu_yenile()
		indeks = self.dosya_listesi.FindString(dosya_adi)
		if indeks != wx.NOT_FOUND:
			self.dosya_listesi.SetSelection(indeks)
		self.dosya_listesi.SetFocus()

	def yukleme_hatali(self, kayit, hata):
		if self.aktif_klasor == kayit["klasor"]:
			self.arayuzu_yenile()

	def yukleme_iptali_tamamlandi(self, kayit):
		if self.aktif_klasor == kayit["klasor"]:
			self.arayuzu_yenile()
		self._esitlemeyi_baslat()

	def yukleme_iptali_hatali(self, kayit, hata):
		if self.aktif_klasor == kayit["klasor"]:
			self.arayuzu_yenile()
		self._esitlemeyi_baslat(hata_goster=True)

	def arayuzu_yenile(self):
		self.GetMenuBar().Enable(self.KIMLIK_BAGLAN, not HESAP_DURUMU.bagli_mi)
		self.GetMenuBar().Enable(self.KIMLIK_KES, HESAP_DURUMU.bagli_mi)
		self.yuklemeleri_duraklat_ogesi.SetItemLabel(
			_("Yüklemeleri &başlat") if YUKLEME_YONETICISI.duraklatildi else _("Yüklemeleri &duraklat")
		)
		self.GetMenuBar().Enable(self.KIMLIK_YUKLEMELERI_DURAKLAT, HESAP_DURUMU.bagli_mi)
		self.GetMenuBar().Enable(self.KIMLIK_YUKLEMELERI_IPTAL_ET, HESAP_DURUMU.bagli_mi)
		self.GetMenuBar().Enable(self.KIMLIK_HATALI_YUKLEMELERI_YENIDEN_DENE, HESAP_DURUMU.bagli_mi)
		self.GetMenuBar().Enable(self.KIMLIK_SUNUCU_ISLEMLERI, HESAP_DURUMU.bagli_mi)
		self.dosya_listesi.Clear()
		self.gorunen_dosyalar = {}
		self.dosya_ayrinti_indeksi = 0
		self.onceki_sayfa_ogesi = None
		self.sonraki_sayfa_ogesi = None
		if HESAP_DURUMU.bagli_mi:
			if self.aktif_klasor:
				dosyalar = self.klasor_dosyalari.get(self.aktif_klasor, [])
				kuyruktakiler = YUKLEME_YONETICISI.klasordeki_durumler(HESAP_DURUMU.eposta, self.aktif_klasor)
				if self.aktif_klasor in self.klasor_yukleniyor and not dosyalar:
					self.dosya_listesi.Append(_("Klasör içeriği yükleniyor."))
				elif dosyalar:
					toplam_sayfa = max(1, (len(dosyalar) + self.SAYFA_BASINA_DOSYA - 1) // self.SAYFA_BASINA_DOSYA)
					self.dosya_sayfasi = min(self.dosya_sayfasi, toplam_sayfa - 1)
					baslangic = self.dosya_sayfasi * self.SAYFA_BASINA_DOSYA
					bitis = baslangic + self.SAYFA_BASINA_DOSYA
					for bilgi in dosyalar[baslangic:bitis]:
						if bilgi.get("durum") == "siliniyor":
							self.dosya_listesi.Append(
								_("Siliniyor: {dosya}").format(dosya=bilgi["ad"])
							)
						else:
							self.dosya_listesi.Append(bilgi["ad"])
							self.gorunen_dosyalar[bilgi["ad"]] = bilgi
					if self.dosya_sayfasi > 0:
						self.onceki_sayfa_ogesi = _("Önceki sayfa")
						self.dosya_listesi.Append(self.onceki_sayfa_ogesi)
					if self.dosya_sayfasi < toplam_sayfa - 1:
						self.sonraki_sayfa_ogesi = _("Sonraki sayfa")
						self.dosya_listesi.Append(self.sonraki_sayfa_ogesi)
				elif not kuyruktakiler:
					self.dosya_listesi.Append(_("Bu klasör boş."))
				for kayit in kuyruktakiler:
					dosya_adi = os.path.basename(kayit["yerel_yol"])
					if kayit["durum"] in ("iptal_ediliyor", "iptal_dogrulaniyor"):
						self.dosya_listesi.Append(_("Siliniyor: {dosya}").format(dosya=dosya_adi))
					elif kayit["durum"] == "yükleniyor":
						self.dosya_listesi.Append(
							_("Dosya yükleniyor: {dosya}, yüzde {oran}").format(
								dosya=dosya_adi, oran=kayit.get("yuzde", 0)
							)
						)
					elif kayit["durum"] == "arşivleniyor":
						self.dosya_listesi.Append(_("Arşivde işleniyor: %100 {dosya}").format(dosya=dosya_adi))
					elif kayit["durum"] == "bekliyor":
						self.dosya_listesi.Append(_("Yükleme bekliyor: {dosya}").format(dosya=dosya_adi))
					elif kayit["durum"] == "hata":
						self.dosya_listesi.Append(_("Yükleme hatası: {dosya}").format(dosya=dosya_adi))
			else:
				self.gorunen_klasorler = {}
				for klasor in HESAP_DURUMU.istem.varsayilan_klasorler:
					gorunen_ad = HESAP_DURUMU.istem.klasor_gorunen_adi(klasor)
					self.dosya_listesi.Append(gorunen_ad)
					self.gorunen_klasorler[gorunen_ad] = klasor
		else:
			self.dosya_listesi.Append(_("Herhangi bir hesap tanımlı değil."))
		self.dosya_listesi.SetSelection(0)
		self.panel.Layout()

	def hesap_yok_uyarisi_goster(self):
		"""Açılışta etkin hesap yoksa kullanıcıya bağlantı seçimini sorar."""
		if HESAP_DURUMU.bagli_mi or not self.IsShown():
			return
		sonuc = wx.MessageBox(
			_("Etkin bir hesap bulunamadı. Giriş yapmak veya yeni bir hesap oluşturmak ister misiniz?"),
			_("Dosya Arşivim"),
			wx.YES_NO | wx.ICON_QUESTION,
			self,
		)
		if sonuc == wx.YES:
			self.baglan_secildi(None)
		else:
			self.dosya_listesi.SetFocus()

	def baglan_secildi(self, event):
		BaglantiSecimPenceresi(self).ShowModal()
		self.arayuzu_yenile()
		self._esitlemeyi_baslat()
		self._bekleyen_silmeleri_surdur()

	def baglanti_kes_secildi(self, event):
		onay = wx.MessageBox(
			_("Mevcut hesap bilgilerinizi kaldırmak istediğinizden emin misiniz? Bu işlem hesabınızı silmez. Dilediğiniz zaman tekrar giriş yapabilirsiniz."),
			_("Bağlantıyı kes"),
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
			self,
		)
		if onay != wx.YES:
			return
		try:
			HESAP_DURUMU.baglantiyi_kes()
		except HesapHatasi as hata:
			wx.MessageBox(str(hata), _("Bağlantıyı kes"), wx.OK | wx.ICON_ERROR, self)
			return
		YUKLEME_YONETICISI.etkin_yuklemeyi_duraklat()
		self._baglanti_kesildi_bildir()

	def _baglanti_kesildi_bildir(self):
		BaglantiKesildiPenceresi(self).ShowModal()
		self.arayuzu_yenile()
		self.dosya_listesi.SetFocus()

	def cikis_secildi(self, event):
		self.Close()

	def kapat(self, event):
		self.kapanisa_hazirla()
		self.kapanis_islevi()
		event.Skip()

	def kapanisa_hazirla(self):
		"""Kapanıştan önce zamanlanmış bildirimleri ve kuyruk dinleyicisini güvenle kapatır."""
		if self.kapatildi:
			return
		self.kapatildi = True
		YUKLEME_YONETICISI.dinleyici_cikar(self)


class SunucuIslemleriPenceresi(wx.Dialog):
	"""Archive.org görevlerini erişilebilir biçimde gösterir ve hata görevlerini güvenle yineler."""

	def __init__(self, parent):
		super().__init__(parent, title=_("Sunucu işlemleri"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.gorevler = []
		self.istek_devam_ediyor = False
		self.kapatildi = False
		self.ardisik_hata_sayisi = 0
		self.yenileme_zamanlayicisi = None
		self.yeniden_calistirilan_gorevler = set()

		sizer = wx.BoxSizer(wx.VERTICAL)
		self.ozet_metni = wx.StaticText(self, label=_("Sunucu işlem bilgileri alınıyor."))
		sizer.Add(self.ozet_metni, 0, wx.EXPAND | wx.ALL, 8)
		self.gorev_listesi = wx.ListBox(self)
		self.gorev_listesi.Append(_("Sunucu işlem bilgileri alınıyor."))
		self.gorev_listesi.SetSelection(0)
		sizer.Add(self.gorev_listesi, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

		dugmeler = wx.BoxSizer(wx.HORIZONTAL)
		self.yenile_dugmesi = wx.Button(self, label=_("&Yenile"))
		self.yeniden_dene_dugmesi = wx.Button(self, label=_("Hatalı görevi &yeniden çalıştır"))
		self.kapat_dugmesi = wx.Button(self, wx.ID_CLOSE, _("&Kapat"))
		for dugme in (self.yenile_dugmesi, self.yeniden_dene_dugmesi, self.kapat_dugmesi):
			dugmeler.Add(dugme, 0, wx.ALL, 5)
		sizer.Add(dugmeler, 0, wx.ALIGN_CENTER | wx.BOTTOM, 5)
		self.SetSizer(sizer)
		self.SetSize((620, 360))
		self.SetMinSize((520, 280))
		self.CentreOnParent()
		self.SetEscapeId(wx.ID_CLOSE)

		self.gorev_listesi.Bind(wx.EVT_LISTBOX, self._secim_degisti)
		self.yenile_dugmesi.Bind(wx.EVT_BUTTON, lambda event: self.yenilemeyi_baslat(manuel=True))
		self.yeniden_dene_dugmesi.Bind(wx.EVT_BUTTON, self._gorevi_yeniden_calistir)
		self.kapat_dugmesi.Bind(wx.EVT_BUTTON, self.kapat)
		self.Bind(wx.EVT_CLOSE, self.kapat)
		self._dugmeleri_guncelle()
		self.gorev_listesi.SetFocus()
		wx.CallAfter(self.yenilemeyi_baslat)

	def _zamanlayiciyi_durdur(self):
		zamanlayici = self.yenileme_zamanlayicisi
		self.yenileme_zamanlayicisi = None
		if zamanlayici is not None:
			try:
				zamanlayici.Stop()
			except RuntimeError:
				pass

	def _yenilemeyi_zamanla(self, gecikme_ms):
		if self.kapatildi:
			return
		self._zamanlayiciyi_durdur()
		self.yenileme_zamanlayicisi = wx.CallLater(gecikme_ms, self.yenilemeyi_baslat)

	def yenilemeyi_baslat(self, manuel=False):
		if self.kapatildi or self.istek_devam_ediyor or not HESAP_DURUMU.bagli_mi:
			return
		self._zamanlayiciyi_durdur()
		if manuel:
			self.ardisik_hata_sayisi = 0
		self.istek_devam_ediyor = True
		self.ozet_metni.SetLabel(_("Sunucu işlem bilgileri alınıyor."))
		self._dugmeleri_guncelle()
		arka_planda(
			lambda: HESAP_DURUMU.istem.gorev_durumlarini_al(HESAP_DURUMU.eposta),
			self._yenileme_tamamlandi,
		)

	def _yenileme_tamamlandi(self, sonuc, hata):
		if self.kapatildi:
			return
		self.istek_devam_ediyor = False
		if hata:
			self.ardisik_hata_sayisi += 1
			if self.ardisik_hata_sayisi <= len(SUNUCU_ISLEMLERI_HATA_GECIKMELERI_MS):
				gecikme = SUNUCU_ISLEMLERI_HATA_GECIKMELERI_MS[self.ardisik_hata_sayisi - 1]
				self.ozet_metni.SetLabel(
					_("Sunucu bilgileri alınamadı. Otomatik olarak yeniden denenecek.")
				)
				self._yenilemeyi_zamanla(gecikme)
			else:
				self.ozet_metni.SetLabel(
					_("Sunucu bilgileri alınamadı. Otomatik yenileme durduruldu; Yenile düğmesini kullanabilirsiniz.")
				)
			self._dugmeleri_guncelle()
			self.Layout()
			return
		self.ardisik_hata_sayisi = 0
		self._gorevleri_goster(sonuc)
		etkin_sayi = sum(sonuc["ozet"].values())
		gecikme = SUNUCU_ISLEMLERI_ETKIN_YENILEME_MS if etkin_sayi else SUNUCU_ISLEMLERI_BOSTA_YENILEME_MS
		self._yenilemeyi_zamanla(gecikme)

	@staticmethod
	def _durum_etiketi(durum):
		return {
			"queued": _("Kuyrukta"),
			"running": _("Çalışıyor"),
			"error": _("Hata"),
			"paused": _("Duraklatıldı"),
			"unknown": _("Bilinmeyen"),
		}.get(durum, _("Bilinmeyen"))

	@staticmethod
	def _komut_etiketi(komut):
		return {
			"archive.php": _("Arşivleme"),
			"derive.php": _("Türev üretimi"),
			"modify_xml.php": _("Arşiv bilgilerini güncelleme"),
			"bup.php": _("Yedekleme"),
		}.get(komut, _("Sunucu görevi: {komut}").format(komut=komut))

	def _gorevleri_goster(self, sonuc):
		secili_id = None
		secim = self.gorev_listesi.GetSelection()
		if 0 <= secim < len(self.gorevler):
			secili_id = self.gorevler[secim]["id"]
		self.gorevler = list(sonuc["gorevler"])
		ozet = sonuc["ozet"]
		self.ozet_metni.SetLabel(
			_("Kuyrukta: {kuyrukta}; Çalışıyor: {calisiyor}; Hata: {hata}; Duraklatıldı: {duraklatildi}").format(
				kuyrukta=ozet["queued"],
				calisiyor=ozet["running"],
				hata=ozet["error"],
				duraklatildi=ozet["paused"],
			)
		)
		self.gorev_listesi.Clear()
		secilecek_indeks = 0
		for indeks, gorev in enumerate(self.gorevler):
			self.gorev_listesi.Append(
				_("Görev {kimlik}: {islem}; durum: {durum}").format(
					kimlik=gorev["id"],
					islem=self._komut_etiketi(gorev["komut"]),
					durum=self._durum_etiketi(gorev["durum"]),
				)
			)
			if gorev["id"] == secili_id:
				secilecek_indeks = indeks
		if not self.gorevler:
			self.gorev_listesi.Append(_("Etkin sunucu işlemi bulunmuyor."))
		self.gorev_listesi.SetSelection(secilecek_indeks)
		self._dugmeleri_guncelle()
		self.Layout()

	def _secili_gorev(self):
		secim = self.gorev_listesi.GetSelection()
		if 0 <= secim < len(self.gorevler):
			return self.gorevler[secim]
		return None

	def _secim_degisti(self, event):
		self._dugmeleri_guncelle()
		event.Skip()

	def _dugmeleri_guncelle(self):
		gorev = self._secili_gorev()
		yeniden_calistirilabilir = bool(
			gorev
			and gorev.get("durum") == "error"
			and gorev["id"] not in self.yeniden_calistirilan_gorevler
		)
		self.yenile_dugmesi.Enable(not self.istek_devam_ediyor)
		self.yeniden_dene_dugmesi.Enable(not self.istek_devam_ediyor and yeniden_calistirilabilir)

	def _gorevi_yeniden_calistir(self, event):
		gorev = self._secili_gorev()
		if (
			not gorev
			or gorev.get("durum") != "error"
			or gorev["id"] in self.yeniden_calistirilan_gorevler
			or self.istek_devam_ediyor
		):
			return
		onay = wx.MessageBox(
			_("{kimlik} numaralı hata görevini yeniden çalıştırmak istiyor musunuz?").format(kimlik=gorev["id"]),
			_("Görevi yeniden çalıştır"),
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
			self,
		)
		if onay != wx.YES:
			return
		self._zamanlayiciyi_durdur()
		self.yeniden_calistirilan_gorevler.add(gorev["id"])
		self.istek_devam_ediyor = True
		self.ozet_metni.SetLabel(_("Sunucu görevi yeniden çalıştırılıyor."))
		self._dugmeleri_guncelle()
		arka_planda(
			lambda: HESAP_DURUMU.istem.gorevi_yeniden_calistir(gorev["id"]),
			lambda sonuc, hata: self._gorevi_yeniden_calistirma_tamamlandi(gorev["id"], hata),
		)

	def _gorevi_yeniden_calistirma_tamamlandi(self, gorev_id, hata):
		if self.kapatildi:
			return
		self.istek_devam_ediyor = False
		if hata:
			self.ozet_metni.SetLabel(hata)
			ui.message(hata)
			self._yenilemeyi_zamanla(SUNUCU_ISLEMLERI_ETKIN_YENILEME_MS)
			self._dugmeleri_guncelle()
			return
		ui.message(_("{kimlik} numaralı sunucu görevi yeniden çalıştırıldı.").format(kimlik=gorev_id))
		self.ozet_metni.SetLabel(_("Sunucu görevi yeniden çalıştırıldı; durum bilgisi yenilenecek."))
		self._yenilemeyi_zamanla(1000)
		self._dugmeleri_guncelle()

	def kapat(self, event):
		if self.kapatildi:
			return
		self.kapatildi = True
		self._zamanlayiciyi_durdur()
		if self.IsModal():
			self.EndModal(wx.ID_CLOSE)
		else:
			self.Destroy()


class BaglantiSecimPenceresi(wx.Dialog):
	"""Bağlan menüsünden açılan üç düğmeli seçim penceresi."""

	def __init__(self, parent):
		super().__init__(parent, title=_("Bağlan"))
		sizer = wx.BoxSizer(wx.VERTICAL)
		self.giris = wx.Button(self, label=_("&Giriş yap"))
		self.kayit = wx.Button(self, label=_("&Yeni hesap oluştur"))
		self.iptal = wx.Button(self, wx.ID_CANCEL, _("İ&ptal"))
		for dugme in (self.giris, self.kayit, self.iptal):
			sizer.Add(dugme, 0, wx.EXPAND | wx.ALL, 6)
		self.SetSizerAndFit(sizer)
		self.giris.Bind(wx.EVT_BUTTON, self.giris_yap)
		self.kayit.Bind(wx.EVT_BUTTON, self.yeni_hesap)
		self.giris.SetFocus()

	def giris_yap(self, event):
		self.Hide()
		GirisPenceresi(self.GetParent()).ShowModal()
		self.Destroy()

	def yeni_hesap(self, event):
		self.Hide()
		KayitPenceresi(self.GetParent()).ShowModal()
		self.Destroy()


class BaglantiKesildiPenceresi(wx.Dialog):
	"""Hesap bilgilerinin kaldırıldığını bildirir."""

	def __init__(self, parent):
		super().__init__(parent, title=_("Bağlantıyı kes"))
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(wx.StaticText(self, label=_("Hesap bilgileriniz kaldırıldı.")), 0, wx.ALL, 8)
		self.kapat = wx.Button(self, wx.ID_OK, _("&Kapat"))
		sizer.Add(self.kapat, 0, wx.ALIGN_CENTER | wx.ALL, 6)
		self.SetSizerAndFit(sizer)
		self.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_OK), id=wx.ID_OK)
		self.kapat.SetFocus()


class IndirmePenceresi(wx.Dialog):
	"""İndirme sürerken yalnızca iptal seçeneği sunar."""

	def __init__(self, parent, klasor, dosya_adi, hedef_yol):
		super().__init__(parent, title=_("İndiriliyor"))
		self.klasor = klasor
		self.dosya_adi = dosya_adi
		self.hedef_yol = hedef_yol
		self.iptal_olayi = threading.Event()
		self.iptal_edildi = False
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(wx.StaticText(self, label=_("Dosya indiriliyor.")), 0, wx.ALL, 8)
		self.iptal = wx.Button(self, wx.ID_CANCEL, _("İ&ptal"))
		sizer.Add(self.iptal, 0, wx.ALIGN_CENTER | wx.ALL, 6)
		self.SetSizerAndFit(sizer)
		self.iptal.Bind(wx.EVT_BUTTON, self.iptal_secildi)
		self.Bind(wx.EVT_CLOSE, self.iptal_secildi)
		self.iptal.SetFocus()

	def baslat(self):
		arka_planda(
			lambda: HESAP_DURUMU.istem.dosya_indir(
				HESAP_DURUMU.eposta, self.klasor, self.dosya_adi, self.hedef_yol, self.iptal_olayi
			),
			self.tamamlandi,
		)

	def iptal_secildi(self, event):
		self.iptal_edildi = True
		self.iptal_olayi.set()
		self.EndModal(wx.ID_CANCEL)

	def tamamlandi(self, sonuc, hata):
		if self.iptal_edildi:
			return
		if hata:
			self.EndModal(wx.ID_CANCEL)
			wx.CallAfter(wx.MessageBox, hata, _("İndir"), wx.OK | wx.ICON_ERROR, self.GetParent())
			return
		self.EndModal(wx.ID_OK)
		wx.CallAfter(
			wx.MessageBox,
			_("İndirme işlemi başarıyla tamamlandı."),
			_("İndir"),
			wx.OK | wx.ICON_INFORMATION,
			self.GetParent(),
		)


class DosyaBilgileriPenceresi(wx.Dialog):
	"""Seçili dosyanın paylaşım için gerekli bilgilerini gösterir."""

	def __init__(self, parent, bilgi_metni):
		super().__init__(parent, title=_("Dosya bilgileri"))
		self.bilgi_metni = bilgi_metni
		sizer = wx.BoxSizer(wx.VERTICAL)
		self.bilgiler = wx.TextCtrl(
			self,
			value=bilgi_metni,
			style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
		)
		sizer.Add(self.bilgiler, 1, wx.EXPAND | wx.ALL, 8)
		dugmeler = wx.BoxSizer(wx.HORIZONTAL)
		self.kopyala = wx.Button(self, label=_("&Bilgileri panoya kopyala"))
		self.kapat = wx.Button(self, wx.ID_CANCEL, _("&Kapat"))
		dugmeler.Add(self.kopyala, 0, wx.ALL, 5)
		dugmeler.Add(self.kapat, 0, wx.ALL, 5)
		sizer.Add(dugmeler, 0, wx.ALIGN_CENTER)
		self.SetSizerAndFit(sizer)
		self.SetMinSize((520, 220))
		self.kopyala.Bind(wx.EVT_BUTTON, self.bilgileri_kopyala)
		self.kapat.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_CANCEL))
		self.bilgiler.SetFocus()

	def bilgileri_kopyala(self, event):
		if not wx.TheClipboard.Open():
			wx.MessageBox(_("Panoya erişilemedi."), _("Dosya bilgileri"), wx.OK | wx.ICON_ERROR, self)
			return
		try:
			wx.TheClipboard.SetData(wx.TextDataObject(self.bilgi_metni))
		finally:
			wx.TheClipboard.Close()
		wx.CallLater(
			BAGLANTI_BILDIRIM_GECIKMESI_MS,
			lambda: speech.speak([
				_("Dosya bilgileri panoya kopyalandı."),
				CallbackCommand(lambda: None, name="Dosya Arşivim bilgi kopyalama bildirimi tamamlandı"),
			]),
		)

class GirisPenceresi(wx.Dialog):
	"""Tek kullanımlık e-posta kodu ile hesap girişi."""

	def __init__(self, parent):
		super().__init__(parent, title=_("Giriş yap"))
		self.iptal_edildi = False
		self._formu_olustur()

	def _formu_olustur(self):
		sizer = wx.BoxSizer(wx.VERTICAL)
		self.eposta_etiketi = wx.StaticText(self, label=_("E-posta adresi"))
		sizer.Add(self.eposta_etiketi, 0, wx.LEFT | wx.RIGHT | wx.TOP, 7)
		self.eposta = wx.TextCtrl(self)
		sizer.Add(self.eposta, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
		dugmeler = wx.BoxSizer(wx.HORIZONTAL)
		self.kod_giris = wx.Button(self, label=_("&Giriş kodu gönder"))
		self.iptal = wx.Button(self, wx.ID_CANCEL, _("İ&ptal"))
		for dugme in (self.kod_giris, self.iptal):
			dugmeler.Add(dugme, 0, wx.ALL, 5)
		sizer.Add(dugmeler, 0, wx.ALIGN_CENTER)
		self.SetSizerAndFit(sizer)
		self.kod_giris.Bind(wx.EVT_BUTTON, self.kod_iste)
		self.iptal.Bind(wx.EVT_BUTTON, self.iptal_secildi)
		self.Bind(wx.EVT_CLOSE, self.iptal_secildi)
		self.eposta.SetFocus()

	def iptal_secildi(self, event):
		self.iptal_edildi = True
		self.EndModal(wx.ID_CANCEL)

	def _eposta_al(self):
		eposta = self.eposta.GetValue().strip()
		if not eposta or "@" not in eposta:
			wx.MessageBox(_("Geçerli bir e-posta adresi girin."), _("Giriş yap"), wx.OK | wx.ICON_WARNING, self)
			return None
		return eposta

	def kod_iste(self, event):
		eposta = self._eposta_al()
		if not eposta:
			return
		self._kod_bekleme_gorunumunu_ayarla(True)
		arka_planda(
			lambda: HESAP_DURUMU.istem.eposta_kodu_gonder(eposta, False),
			lambda sonuc, hata: self._kod_istegi_tamamlandi(eposta, hata),
		)

	def _kod_istegi_tamamlandi(self, eposta, hata):
		if self.iptal_edildi:
			return
		if hata:
			self._kod_bekleme_gorunumunu_ayarla(False)
			wx.MessageBox(hata, _("Giriş yap"), wx.OK | wx.ICON_ERROR, self)
			return
		KodDogrulamaPenceresi(self.GetParent(), eposta, False).ShowModal()
		self.EndModal(wx.ID_OK)

	def _kod_bekleme_gorunumunu_ayarla(self, beklemede):
		"""Kod isteği sürerken yalnızca İptal düğmesini görünür bırakır."""
		gorunur = not beklemede
		self.eposta_etiketi.Show(gorunur)
		self.eposta.Show(gorunur)
		self.kod_giris.Show(gorunur)
		self.GetSizer().Layout()
		self.Fit()


class KayitPenceresi(wx.Dialog):
	"""E-posta kodu ile hesap oluşturur."""

	def __init__(self, parent):
		super().__init__(parent, title=_("Yeni hesap oluştur"))
		self.iptal_edildi = False
		sizer = wx.BoxSizer(wx.VERTICAL)
		self.ekran_adi_etiketi = wx.StaticText(self, label=_("Görünen ad"))
		sizer.Add(self.ekran_adi_etiketi, 0, wx.LEFT | wx.RIGHT | wx.TOP, 7)
		self.ekran_adi = wx.TextCtrl(self)
		sizer.Add(self.ekran_adi, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
		self.eposta_etiketi = wx.StaticText(self, label=_("E-posta adresi"))
		sizer.Add(self.eposta_etiketi, 0, wx.LEFT | wx.RIGHT | wx.TOP, 7)
		self.eposta = wx.TextCtrl(self)
		sizer.Add(self.eposta, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
		dugmeler = wx.BoxSizer(wx.HORIZONTAL)
		self.kod_gonder = wx.Button(self, label=_("Aktivasyon kodu &gönder"))
		self.iptal = wx.Button(self, wx.ID_CANCEL, _("İ&ptal"))
		dugmeler.Add(self.kod_gonder, 0, wx.ALL, 5)
		dugmeler.Add(self.iptal, 0, wx.ALL, 5)
		sizer.Add(dugmeler, 0, wx.ALIGN_CENTER)
		self.SetSizerAndFit(sizer)
		self.kod_gonder.Bind(wx.EVT_BUTTON, self.kod_iste)
		self.iptal.Bind(wx.EVT_BUTTON, self.iptal_secildi)
		self.Bind(wx.EVT_CLOSE, self.iptal_secildi)
		self.ekran_adi.SetFocus()

	def iptal_secildi(self, event):
		self.iptal_edildi = True
		self.EndModal(wx.ID_CANCEL)

	def kod_iste(self, event):
		eposta = self.eposta.GetValue().strip()
		ekran_adi = self.ekran_adi.GetValue().strip()
		if not eposta or "@" not in eposta:
			wx.MessageBox(_("Geçerli bir e-posta adresi girin."), _("Yeni hesap oluştur"), wx.OK | wx.ICON_WARNING, self)
			return
		if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,38}[A-Za-z0-9]", ekran_adi):
			wx.MessageBox(_("Görünen ad 3 ila 40 karakter olmalı; Türkçe karakter içermemeli, İngilizce harf veya rakamla başlayıp bitmelidir. Arada İngilizce harfler, rakamlar, nokta, alt çizgi ve kısa çizgi kullanılabilir."), _("Yeni hesap oluştur"), wx.OK | wx.ICON_WARNING, self)
			return
		self._kod_bekleme_gorunumunu_ayarla(True)
		arka_planda(
			lambda: HESAP_DURUMU.istem.eposta_kodu_gonder(eposta, True),
			lambda sonuc, hata: self._kod_istegi_tamamlandi(eposta, ekran_adi, hata),
		)

	def _kod_istegi_tamamlandi(self, eposta, ekran_adi, hata):
		if self.iptal_edildi:
			return
		if hata:
			self._kod_bekleme_gorunumunu_ayarla(False)
			wx.MessageBox(hata, _("Yeni hesap oluştur"), wx.OK | wx.ICON_ERROR, self)
			return
		KodDogrulamaPenceresi(self.GetParent(), eposta, True, ekran_adi).ShowModal()
		self.EndModal(wx.ID_OK)

	def _kod_bekleme_gorunumunu_ayarla(self, beklemede):
		"""Kod isteği sürerken yalnızca İptal düğmesini görünür bırakır."""
		gorunur = not beklemede
		self.ekran_adi_etiketi.Show(gorunur)
		self.ekran_adi.Show(gorunur)
		self.eposta_etiketi.Show(gorunur)
		self.eposta.Show(gorunur)
		self.kod_gonder.Show(gorunur)
		self.GetSizer().Layout()
		self.Fit()


class KodDogrulamaPenceresi(wx.Dialog):
	"""E-posta ile gönderilen altı haneli kodu doğrular."""

	def __init__(self, parent, eposta, kayit_icin, ekran_adi=None):
		baslik = _("Hesabı doğrula") if kayit_icin else _("Girişi doğrula")
		super().__init__(parent, title=baslik)
		self.eposta = eposta
		self.kayit_icin = kayit_icin
		self.ekran_adi = ekran_adi
		self.iptal_edildi = False
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(wx.StaticText(self, label=_("E-posta adresinize gönderilen altı haneli kodu girin.")), 0, wx.ALL, 7)
		self.kod = wx.TextCtrl(self)
		sizer.Add(self.kod, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
		dugmeler = wx.StdDialogButtonSizer()
		self.dogrula = wx.Button(self, wx.ID_OK, _("&Doğrula"))
		self.iptal = wx.Button(self, wx.ID_CANCEL, _("İ&ptal"))
		dugmeler.AddButton(self.dogrula)
		dugmeler.AddButton(self.iptal)
		dugmeler.Realize()
		sizer.Add(dugmeler, 0, wx.ALIGN_CENTER | wx.ALL, 5)
		self.SetSizerAndFit(sizer)
		self.Bind(wx.EVT_BUTTON, self.dogrula_secildi, id=wx.ID_OK)
		self.iptal.Bind(wx.EVT_BUTTON, self.iptal_secildi)
		self.Bind(wx.EVT_CLOSE, self.iptal_secildi)
		self.kod.SetFocus()

	def iptal_secildi(self, event):
		self.iptal_edildi = True
		self.EndModal(wx.ID_CANCEL)

	def dogrula_secildi(self, event):
		kod = self.kod.GetValue().strip()
		if not re.fullmatch(r"[0-9]{6}", kod):
			wx.MessageBox(_("Altı haneli kodu girin."), self.GetTitle(), wx.OK | wx.ICON_WARNING, self)
			return
		self.dogrula.Enable(False)
		self.kod.Enable(False)
		arka_planda(
			lambda: HESAP_DURUMU.istem.eposta_kodunu_dogrula(
				self.eposta, kod, self.kayit_icin, self.ekran_adi
			),
			self._dogrulama_tamamlandi,
		)

	def _dogrulama_tamamlandi(self, sonuc, hata):
		if self.iptal_edildi:
			return
		if hata:
			self.dogrula.Enable(True)
			self.kod.Enable(True)
			wx.MessageBox(hata, self.GetTitle(), wx.OK | wx.ICON_ERROR, self)
			self.kod.SetFocus()
			return
		arka_planda(self._baglantiyi_hazirla, self._baglanti_hazirlandi)

	def _baglantiyi_hazirla(self):
		HESAP_DURUMU.istem.varsayilan_klasorleri_olustur(self.eposta)

	def _baglanti_hazirlandi(self, sonuc, hata):
		if self.iptal_edildi:
			return
		if hata:
			self.dogrula.Enable(True)
			self.kod.Enable(True)
			wx.MessageBox(hata, self.GetTitle(), wx.OK | wx.ICON_ERROR, self)
			self.kod.SetFocus()
			return
		try:
			HESAP_DURUMU.baglan(self.eposta)
		except HesapHatasi as hata:
			wx.MessageBox(str(hata), self.GetTitle(), wx.OK | wx.ICON_ERROR, self)
			self.dogrula.Enable(True)
			self.kod.Enable(True)
			self.kod.SetFocus()
			return
		YUKLEME_YONETICISI.baslat()
		baglanti_bildir_ve_kapat(self)
