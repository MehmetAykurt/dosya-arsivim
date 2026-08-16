# -*- coding: utf-8 -*-
"""Sunucudaki hesap hizmetleri için standart Python istemcisi."""

import http.cookiejar
from html.parser import HTMLParser
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import addonHandler
addonHandler.initTranslation()

import logHandler

from .oturum_deposu import OturumDeposu


VENDOR_DIZINI = os.path.join(os.path.dirname(__file__), "vendor")
if VENDOR_DIZINI not in sys.path:
	sys.path.insert(0, VENDOR_DIZINI)

try:
	import truststore
except ImportError:
	truststore = None

try:
	_
except NameError:
	_ = lambda metin: metin


SUNUCU_ADRESI = "https://archive.org"
LOGIN_PATH = "/services/account/login/"
OTP_PATH = "/services/account/otp/"
KAYIT_PATH = "/services/account/signup/"
S3_ANAHTAR_SAYFASI = "/account/s3.php"
S3_ADRESI = "https://s3.us.archive.org"
GOREVLER_PATH = "/services/tasks.php"
ANA_OGE_ON_EKI = "dosya-arsivim-"
KLASOR_DIZINI_DOSYASI = ".dosya_arsivim_klasorler.json"
VARSAYILAN_KLASORLER = (
	"Resimler",
	"Ses ve Müzik",
	"Videolar",
	"Belgeler",
	"Uygulamalar",
	"Sıkıştırılmış Dosyalar",
	"Yedekler",
	"Diğer",
)
S3_GECICI_HTTP_KODLARI = frozenset((429, 500, 502, 503, 504))
S3_YENIDEN_DENEME_GECIKMELERI = (2, 5)
S3_YOGUNLUK_KONTROL_ZAMAN_ASIMI = 15
GOREV_API_YANIT_SINIRI = 1024 * 1024
GOREV_API_GECICI_HTTP_KODLARI = frozenset((429, 500, 502, 503, 504))
GOREV_API_YENIDEN_DENEME_GECIKMELERI = (2, 5)
GOREV_DURUM_KODLARI = {
	0: "queued",
	1: "running",
	2: "error",
	9: "paused",
}
GOREV_DURUM_ADLARI = {
	"queued": "queued",
	"green": "queued",
	"running": "running",
	"blue": "running",
	"error": "error",
	"red": "error",
	"paused": "paused",
	"brown": "paused",
}


class HesapHatasi(Exception):
	"""Hesap hizmetinin kullanıcıya gösterilebilecek hatası."""


class YuklemeDuraklatildi(Exception):
	"""Yükleme yöneticisinin isteğiyle kesilen aktarımı belirtir."""


class _IlerlemeliDosya:
	"""Dosya okunurken gönderilen bayt miktarını yükleme yöneticisine bildirir."""

	def __init__(self, dosya, toplam_boyut, bildir, durdurma_olayi=None):
		self.dosya = dosya
		self.toplam_boyut = toplam_boyut
		self.bildir = bildir
		self.durdurma_olayi = durdurma_olayi
		self.gonderilen = 0

	def read(self, boyut=-1):
		if self.durdurma_olayi and self.durdurma_olayi.is_set():
			raise YuklemeDuraklatildi()
		if boyut < 0:
			boyut = 1024 * 128
		veri = self.dosya.read(boyut)
		if self.durdurma_olayi and self.durdurma_olayi.is_set():
			raise YuklemeDuraklatildi()
		self.gonderilen += len(veri)
		if veri and self.bildir:
			self.bildir(self.gonderilen, self.toplam_boyut)
		return veri

	def seek(self, konum, nereden=os.SEEK_SET):
		"""Yönlendirme veya yeniden denemede dosyayı tekrar okunabilir kılar."""
		sonuc = self.dosya.seek(konum, nereden)
		if nereden == os.SEEK_SET and konum == 0:
			self.gonderilen = 0
		return sonuc

	def tell(self):
		return self.dosya.tell()


class _ArsivYonlendirmeIsleyicisi(urllib.request.HTTPRedirectHandler):
	"""IA-S3 tarafından PUT isteklerine verilen 307/308 yönlendirmelerini izler."""

	@staticmethod
	def _guvenilir_archive_hostu(host):
		host = (host or "").lower().rstrip(".")
		return host == "archive.org" or host.endswith(".archive.org")

	def redirect_request(self, req, fp, code, msg, headers, newurl):
		yontem = req.get_method()
		if yontem not in ("PUT", "DELETE") or code not in (307, 308):
			return super().redirect_request(req, fp, code, msg, headers, newurl)
		hedef = urllib.parse.urlparse(newurl)
		if hedef.scheme not in ("http", "https") or not self._guvenilir_archive_hostu(hedef.hostname):
			raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
		if yontem == "PUT" and not hasattr(req.data, "seek"):
			raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
		if req.data is not None and hasattr(req.data, "seek"):
			req.data.seek(0)
		yeni_basliklar = dict(req.headers)
		yeni_basliklar.update(req.unredirected_hdrs)
		return urllib.request.Request(
			newurl,
			data=req.data,
			headers=yeni_basliklar,
			origin_req_host=req.origin_req_host,
			unverifiable=True,
			method=yontem,
		)


class _S3AnahtarAyraci(HTMLParser):
	"""S3 anahtar sayfasındaki form alanlarını güvenli biçimde okur."""

	def __init__(self):
		super().__init__()
		self.alanlar = {}
		self.etiketler = {}
		self._etiket_hedefi = None

	def handle_starttag(self, etiket, nitelikler):
		nitelik = {ad.lower(): deger for ad, deger in nitelikler}
		if etiket.lower() == "label":
			self._etiket_hedefi = nitelik.get("for")
			return
		if etiket.lower() not in ("input", "textarea"):
			return
		ad = nitelik.get("name") or nitelik.get("id")
		deger = nitelik.get("value")
		if ad and deger:
			self.alanlar[ad.lower()] = deger
			etiket_metni = self.etiketler.get(nitelik.get("id"), "").lower()
			if "access key" in etiket_metni:
				self.alanlar["access_key"] = deger
			elif "secret key" in etiket_metni:
				self.alanlar["secret_key"] = deger

	def handle_data(self, veri):
		if self._etiket_hedefi:
			self.etiketler[self._etiket_hedefi] = veri.strip()

	def handle_endtag(self, etiket):
		if etiket.lower() == "label":
			self._etiket_hedefi = None


def hata_mesaji(sonuc, varsayilan):
	"""Sunucu hata kodlarını kullanıcıya uygun metne dönüştürür."""
	mesaj = (sonuc.get("error") or sonuc.get("value")) if isinstance(sonuc, dict) else None
	if mesaj == "rate_exception":
		return _("Kısa süre içinde çok fazla kod istendi. Lütfen bir süre bekleyip yeniden deneyin.")
	if mesaj == "account_not_found":
		return _("Bu e-posta adresiyle kayıtlı bir hesap bulunamadı.")
	return varsayilan


class HesapIstemi:
	"""Sunucu oturumunu yönetir ve gerekirse şifreli olarak kalıcılaştırır."""

	def __init__(self):
		self.cerezler = http.cookiejar.CookieJar()
		self.ssl_baglami = self._ssl_baglamini_olustur()
		self.acici = urllib.request.build_opener(
			urllib.request.HTTPCookieProcessor(self.cerezler),
			urllib.request.HTTPSHandler(context=self.ssl_baglami),
			_ArsivYonlendirmeIsleyicisi(),
		)
		self.bekleyen_kod_belirteci = None
		self.bekleyen_kod_epostasi = None
		self.bekleyen_kod_kayit_icin = None
		self.s3_anahtari = None
		self.s3_gizli_anahtari = None

	@staticmethod
	def _ssl_baglamini_olustur():
		"""Sertifika denetimini Windows güvenilen sertifika deposuyla yapar."""
		if truststore is not None:
			return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
		return ssl.create_default_context()

	def cerezleri_al(self):
		"""Çerezleri güvenli depolama için JSON uyumlu biçime dönüştürür."""
		return [
			{
				"version": cerez.version,
				"name": cerez.name,
				"value": cerez.value,
				"port": cerez.port,
				"port_specified": cerez.port_specified,
				"domain": cerez.domain,
				"domain_specified": cerez.domain_specified,
				"domain_initial_dot": cerez.domain_initial_dot,
				"path": cerez.path,
				"path_specified": cerez.path_specified,
				"secure": cerez.secure,
				"expires": cerez.expires,
				"discard": cerez.discard,
				"comment": cerez.comment,
				"comment_url": cerez.comment_url,
				"rest": dict(cerez._rest),
				"rfc2109": cerez.rfc2109,
			}
			for cerez in self.cerezler
		]

	def cerezleri_yukle(self, cerezler):
		"""Şifreli depodan gelen çerezleri bellekteki çerez kavanozuna yükler."""
		for cerez in cerezler:
			self.cerezler.set_cookie(http.cookiejar.Cookie(**cerez))

	def oturumu_kaydet(self, eposta):
		OturumDeposu().kaydet(
			eposta,
			self.cerezleri_al(),
			self.s3_anahtari,
			self.s3_gizli_anahtari,
		)

	def kalici_oturumu_yukle(self):
		"""Kalıcı oturum varsa çerezleri geri yükler ve e-posta adresini döndürür."""
		veri = OturumDeposu().yukle()
		if not veri:
			return None
		try:
			self.cerezleri_yukle(veri["cerezler"])
		except (KeyError, TypeError, ValueError):
			return None
		self.s3_anahtari = veri.get("s3_anahtari")
		self.s3_gizli_anahtari = veri.get("s3_gizli_anahtari")
		return veri["eposta"]

	def kalici_oturumu_sil(self):
		OturumDeposu().sil()

	def _istek(self, yol, method="GET", veri=None):
		govde = None
		basliklar = {
			"Accept": "application/json",
			"User-Agent": "DosyaArsivimNVDA/26.8.15",
		}
		if veri is not None:
			govde = json.dumps(veri).encode("utf-8")
			basliklar["Content-Type"] = "application/json"
		istek = urllib.request.Request(
			SUNUCU_ADRESI + yol,
			data=govde,
			headers=basliklar,
			method=method,
		)
		try:
			with self.acici.open(istek, timeout=30) as yanit:
				metin = yanit.read().decode("utf-8")
		except urllib.error.HTTPError as hata:
			try:
				veri = json.loads(hata.read().decode("utf-8"))
				mesaj = hata_mesaji(veri, _("Sunucu isteği tamamlanamadı."))
			except Exception:
				mesaj = None
			raise HesapHatasi(mesaj or _("Sunucu isteği tamamlanamadı."))
		except urllib.error.URLError:
			raise HesapHatasi(_("Sunucuya bağlantı kurulamadı."))
		try:
			sonuc = json.loads(metin)
		except ValueError:
			raise HesapHatasi(_("Sunucu beklenmeyen bir yanıt gönderdi."))
		if not sonuc.get("success"):
			raise HesapHatasi(hata_mesaji(sonuc, _("İşlem tamamlanamadı.")))
		return sonuc.get("value")

	def csrf_belirteci_al(self, yol=LOGIN_PATH):
		"""Oturuma bağlı, kısa ömürlü CSRF belirtecini alır."""
		sonuc = self._istek(yol)
		belirtec = sonuc.get("token") if isinstance(sonuc, dict) else None
		if not belirtec:
			raise HesapHatasi(_("Güvenlik belirteci alınamadı."))
		return belirtec

	def eposta_kodu_gonder(self, eposta, kayit_icin):
		"""E-posta ile giriş veya kayıt için altı haneli kod ister."""
		belirtec = self.csrf_belirteci_al(KAYIT_PATH if kayit_icin else LOGIN_PATH)
		self._istek(
			OTP_PATH,
			method="POST",
			veri={
				"email": eposta,
				"token": belirtec,
				"sender_page": "sign up" if kayit_icin else "log in",
			},
		)
		self.bekleyen_kod_belirteci = belirtec
		self.bekleyen_kod_epostasi = eposta
		self.bekleyen_kod_kayit_icin = kayit_icin

	def eposta_kodunu_dogrula(self, eposta, kod, kayit_icin, ekran_adi=None):
		"""E-posta kodunu doğrular; kayıt işleminde ekran adı da iletilir."""
		if (
			self.bekleyen_kod_belirteci
			and self.bekleyen_kod_epostasi == eposta
			and self.bekleyen_kod_kayit_icin == kayit_icin
		):
			belirtec = self.bekleyen_kod_belirteci
		else:
			belirtec = self.csrf_belirteci_al(KAYIT_PATH if kayit_icin else LOGIN_PATH)
		veri = {
			"email": eposta,
			"passcode": kod,
			"token": belirtec,
			"sender_page": "sign up" if kayit_icin else "log in",
		}
		if kayit_icin:
			veri["screenname"] = ekran_adi
		return self._istek(OTP_PATH, method="POST", veri=veri)

	@staticmethod
	def ana_oge_kimligi(eposta):
		"""E-posta adresini açığa çıkarmayan, kararlı ana öge kimliğini üretir."""
		ozet = hashlib.sha256(eposta.strip().lower().encode("utf-8")).hexdigest()[:24]
		return ANA_OGE_ON_EKI + ozet

	@staticmethod
	def _s3_anahtarlarini_htmlden_al(html):
		ayrac = _S3AnahtarAyraci()
		ayrac.feed(html)
		alanlar = ayrac.alanlar
		anahtar = next((deger for ad, deger in alanlar.items() if "access" in ad), None)
		gizli = next((deger for ad, deger in alanlar.items() if "secret" in ad), None)
		if anahtar and gizli:
			return anahtar, gizli
		ra = re.compile(
			r"(?:access|secret)[ _-]*key[^A-Za-z0-9]+(?:<[^>]+>)*\s*([A-Za-z0-9+/=._-]+)",
			re.IGNORECASE,
		)
		eslesmeler = ra.findall(html)
		if len(eslesmeler) >= 2:
			return eslesmeler[0], eslesmeler[1]
		return None, None

	def _s3_anahtarlarini_al_veya_olustur(self):
		"""Gerekirse hesabın S3 anahtarlarını üretir ve yalnızca bellekte tutar."""
		if self.s3_anahtari and self.s3_gizli_anahtari:
			return
		url = SUNUCU_ADRESI + S3_ANAHTAR_SAYFASI
		try:
			with self.acici.open(urllib.request.Request(url), timeout=30) as yanit:
				html = yanit.read().decode("utf-8", errors="replace")
		except (urllib.error.URLError, OSError):
			raise HesapHatasi(_("Arşiv erişim anahtarı oluşturulamadı."))
		anahtar, gizli = self._s3_anahtarlarini_htmlden_al(html)
		if not (anahtar and gizli):
			veri = urllib.parse.urlencode({"confirm": "on", "generateNewKeys": "Generate New Keys"}).encode("utf-8")
			istek = urllib.request.Request(url, data=veri, method="POST")
			try:
				with self.acici.open(istek, timeout=30) as yanit:
					html = yanit.read().decode("utf-8", errors="replace")
			except (urllib.error.URLError, OSError):
				raise HesapHatasi(_("Arşiv erişim anahtarı oluşturulamadı."))
			anahtar, gizli = self._s3_anahtarlarini_htmlden_al(html)
		if not (anahtar and gizli):
			raise HesapHatasi(_("Arşiv erişim anahtarı oluşturulamadı."))
		self.s3_anahtari = anahtar
		self.s3_gizli_anahtari = gizli

	@staticmethod
	def _s3_hata_ayrintisi(hata):
		"""IA-S3 XML yanıtından kimlik bilgisi içermeyen hata ayrıntısını alır."""
		kod = None
		mesaj = None
		try:
			govde = hata.read(64 * 1024)
			kok = ET.fromstring(govde)
			kod = kok.findtext("Code")
			mesaj = kok.findtext("Message")
		except (ET.ParseError, OSError, TypeError, ValueError):
			pass
		return kod, mesaj

	@staticmethod
	def _s3_kullanici_hatasi(hata_metni, http_kodu, servis_kodu=None):
		ayrinti = f"HTTP {http_kodu}"
		if isinstance(servis_kodu, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", servis_kodu):
			ayrinti += f", {servis_kodu}"
		return _("{mesaj} ({ayrinti})").format(mesaj=hata_metni, ayrinti=ayrinti)

	def _s3_kullanim_siniri_asildi_mi(self, oge_kimligi):
		"""IA'nın belgelenmiş sınır denetimini yapar; belirsizlikte gerçek isteği engellemez."""
		sorgu = urllib.parse.urlencode({
			"check_limit": "1",
			"accesskey": self.s3_anahtari,
			"bucket": oge_kimligi,
		})
		try:
			with self.acici.open(
				urllib.request.Request(f"{S3_ADRESI}/?{sorgu}"),
				timeout=S3_YOGUNLUK_KONTROL_ZAMAN_ASIMI,
			) as yanit:
				sonuc = json.loads(yanit.read().decode("utf-8"))
		except (urllib.error.URLError, OSError, TypeError, ValueError):
			logHandler.log.warning("Dosya Arşivim IA-S3 yoğunluk denetimi alınamadı; gerçek istek denenecek.")
			return None
		if not isinstance(sonuc, dict):
			return None
		deger = sonuc.get("over_limit")
		if deger in (0, "0", False):
			return False
		if deger in (1, "1", True):
			return True
		return None

	def _s3_istegi(self, oge_kimligi, dosya_adi, method, veri=None, yeni_oge=False, ek_basliklar=None, hata_metni=None):
		"""IA-S3 ile ana ögedeki tek bir dosya üzerinde işlem yapar."""
		url = f"{S3_ADRESI}/{urllib.parse.quote(oge_kimligi)}/{urllib.parse.quote(dosya_adi)}"
		basliklar = {
			"Authorization": f"LOW {self.s3_anahtari}:{self.s3_gizli_anahtari}",
		}
		if yeni_oge:
			basliklar.update({
				"x-archive-auto-make-bucket": "1",
				"x-archive-meta-mediatype": "data",
				"x-archive-meta-collection": "opensource_media",
				"x-archive-meta-title": "Dosya Arsivim",
			})
		if ek_basliklar:
			basliklar.update(ek_basliklar)
		yazma_istegi = method in ("PUT", "DELETE")
		if yazma_istegi:
			# IA aynı öğede öncelikli ve önceliksiz işlemlerin karıştırılmamasını ister.
			basliklar["x-archive-interactive-priority"] = "1"
		varsayilan_hata = hata_metni or _("Varsayılan klasörler oluşturulamadı.")
		gecikmeler = (0,) + S3_YENIDEN_DENEME_GECIKMELERI
		for deneme, gecikme in enumerate(gecikmeler):
			if gecikme:
				time.sleep(gecikme)
			if yazma_istegi and self._s3_kullanim_siniri_asildi_mi(oge_kimligi):
				logHandler.log.warning(
					"Dosya Arşivim IA-S3 yazma isteği sunucu yoğunluğu nedeniyle ertelendi: yöntem=%s, deneme=%s",
					method,
					deneme + 1,
				)
				if deneme + 1 < len(gecikmeler):
					continue
				raise HesapHatasi(
					_("Archive.org sunucusu şu anda yoğun. Lütfen işlemi daha sonra yeniden deneyin.")
				)
			if veri is not None and hasattr(veri, "seek"):
				veri.seek(0)
			istek = urllib.request.Request(url, data=veri, headers=basliklar, method=method)
			try:
				with self.acici.open(istek, timeout=45) as yanit:
					return yanit.status
			except urllib.error.HTTPError as hata:
				if hata.code == 404 and method in ("HEAD", "DELETE"):
					return None
				servis_kodu, _servis_mesaji = self._s3_hata_ayrintisi(hata)
				logHandler.log.warning(
					"Dosya Arşivim IA-S3 isteği başarısız: yöntem=%s, HTTP=%s, kod=%s, deneme=%s",
					method,
					hata.code,
					servis_kodu if isinstance(servis_kodu, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", servis_kodu) else "bilinmiyor",
					deneme + 1,
				)
				if hata.code in S3_GECICI_HTTP_KODLARI and deneme + 1 < len(gecikmeler):
					continue
				raise HesapHatasi(self._s3_kullanici_hatasi(varsayilan_hata, hata.code, servis_kodu))
			except (urllib.error.URLError, OSError) as hata:
				logHandler.log.warning(
					"Dosya Arşivim IA-S3 bağlantı hatası: yöntem=%s, tür=%s, deneme=%s",
					method,
					type(hata).__name__,
					deneme + 1,
				)
				if deneme + 1 < len(gecikmeler):
					continue
				raise HesapHatasi(varsayilan_hata)

	def varsayilan_klasorleri_olustur(self, eposta):
		"""Ana öge ve boş klasörlerin kalıcı dizin kaydını ilk kez oluşturur."""
		self._s3_anahtarlarini_al_veya_olustur()
		oge_kimligi = self.ana_oge_kimligi(eposta)
		var_mi = self._s3_istegi(oge_kimligi, KLASOR_DIZINI_DOSYASI, "HEAD")
		if var_mi:
			return
		veri = json.dumps(
			{"surum": 1, "klasorler": VARSAYILAN_KLASORLER},
			ensure_ascii=False,
			separators=(",", ":"),
		).encode("utf-8")
		self._s3_istegi(oge_kimligi, KLASOR_DIZINI_DOSYASI, "PUT", veri, yeni_oge=True)

	def dosya_yukle(
		self,
		eposta,
		klasor,
		yerel_yol,
		ilerleme_bildir=None,
		durdurma_olayi=None,
		turev_uret=True,
	):
		"""Yerel dosyayı seçili klasör yoluyla ana arşiv ögesine yükler."""
		if not os.path.isfile(yerel_yol):
			raise HesapHatasi(_("Seçilen dosya bulunamadı."))
		self._s3_anahtarlarini_al_veya_olustur()
		dosya_adi = os.path.basename(yerel_yol)
		uzak_yol = f"{klasor}/{dosya_adi}"
		oge_kimligi = self.ana_oge_kimligi(eposta)
		try:
			with open(yerel_yol, "rb") as dosya:
				boyut = os.path.getsize(yerel_yol)
				veri = _IlerlemeliDosya(dosya, boyut, ilerleme_bildir, durdurma_olayi)
				basliklar = {"Content-Length": str(boyut)}
				if not turev_uret:
					basliklar["x-archive-queue-derive"] = "0"
				self._s3_istegi(
					oge_kimligi,
					uzak_yol,
					"PUT",
					veri,
					ek_basliklar=basliklar,
					hata_metni=_("Dosya yüklenemedi."),
				)
		except OSError:
			raise HesapHatasi(_("Dosya yüklenemedi."))
		return dosya_adi

	def dosya_sil(self, eposta, klasor, dosya_adi):
		"""Seçili dosyayı bağlı hesabın depolama alanından siler."""
		self._s3_anahtarlarini_al_veya_olustur()
		oge_kimligi = self.ana_oge_kimligi(eposta)
		self._s3_istegi(
			oge_kimligi,
			f"{klasor}/{dosya_adi}",
			"DELETE",
			ek_basliklar={"x-archive-cascade-delete": "1"},
			hata_metni=_("Dosya silinemedi."),
		)

	@staticmethod
	def _gorev_durumunu_coz(gorev):
		"""Tasks API durumunu yalnızca belgelenmiş dört sabit duruma indirger."""
		try:
			kod = int(gorev.get("wait_admin"))
		except (TypeError, ValueError):
			kod = None
		if kod in GOREV_DURUM_KODLARI:
			return GOREV_DURUM_KODLARI[kod]
		for alan in ("status", "color"):
			deger = gorev.get(alan)
			if isinstance(deger, str):
				durum = GOREV_DURUM_ADLARI.get(deger.strip().casefold())
				if durum:
					return durum
		return "unknown"

	@staticmethod
	def _guvenli_gorev_sayisi(deger):
		try:
			deger = int(deger)
		except (TypeError, ValueError):
			return 0
		return min(max(deger, 0), 1000000)

	@classmethod
	def _gorevi_guvenli_hale_getir(cls, gorev):
		"""Sunucu, kullanıcı ve istek ayrıntılarını atarak yalnızca arayüz için gereken alanları bırakır."""
		if not isinstance(gorev, dict):
			return None
		gorev_id = gorev.get("task_id", gorev.get("id"))
		try:
			gorev_id = int(gorev_id)
		except (TypeError, ValueError):
			return None
		if gorev_id < 1:
			return None
		komut = gorev.get("cmd")
		if not isinstance(komut, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", komut):
			komut = "unknown"
		durum = cls._gorev_durumunu_coz(gorev)
		try:
			oncelik = int(gorev.get("priority"))
		except (TypeError, ValueError):
			oncelik = None
		if oncelik is not None and not -10 <= oncelik <= 10:
			oncelik = None
		return {
			"id": gorev_id,
			"komut": komut,
			"durum": durum,
			"oncelik": oncelik,
			"yeniden_calistirilabilir": durum == "error",
		}

	def gorev_durumlarini_al(self, eposta):
		"""Ana arşiv öğesinin etkin görevlerini güvenli ve kararlı bir veri yapısında döndürür."""
		self._s3_anahtarlarini_al_veya_olustur()
		sorgu = urllib.parse.urlencode({
			"identifier": self.ana_oge_kimligi(eposta),
			"catalog": "1",
			"summary": "1",
			"history": "0",
			"limit": "100",
		})
		istek = urllib.request.Request(
			f"{SUNUCU_ADRESI}{GOREVLER_PATH}?{sorgu}",
			headers={"Authorization": f"LOW {self.s3_anahtari}:{self.s3_gizli_anahtari}"},
		)
		sonuc = self._gorev_api_json_istegi(
			istek,
			_("Sunucu işlem durumları alınamadı."),
			yeniden_dene=False,
		)
		deger = sonuc.get("value")
		if not isinstance(deger, dict):
			raise HesapHatasi(_("Sunucu işlem durumları alınamadı."))
		ozet_girdisi = deger.get("summary") if isinstance(deger.get("summary"), dict) else {}
		ozet = {
			durum: self._guvenli_gorev_sayisi(ozet_girdisi.get(durum))
			for durum in ("queued", "running", "error", "paused")
		}
		katalog = deger.get("catalog")
		if isinstance(katalog, dict):
			katalog = list(katalog.values())
		if not isinstance(katalog, list):
			katalog = []
		gorevler = []
		for gorev in katalog:
			guvenli_gorev = self._gorevi_guvenli_hale_getir(gorev)
			if guvenli_gorev is not None:
				gorevler.append(guvenli_gorev)
		return {"ozet": ozet, "gorevler": gorevler}

	def _gorev_api_json_istegi(self, istek, hata_metni, yeniden_dene=True):
		"""Tasks API isteğini geçici hatalarda sınırlı sayıda dener ve yalnızca doğrulanmış JSON döndürür."""
		gecikmeler = (0,) + GOREV_API_YENIDEN_DENEME_GECIKMELERI if yeniden_dene else (0,)
		for deneme, gecikme in enumerate(gecikmeler):
			if gecikme:
				time.sleep(gecikme)
			try:
				with self.acici.open(istek, timeout=30) as yanit:
					govde = yanit.read(GOREV_API_YANIT_SINIRI + 1)
			except urllib.error.HTTPError as hata:
				logHandler.log.warning(
					"Dosya Arşivim görev API isteği başarısız: yöntem=%s, HTTP=%s, deneme=%s",
					istek.get_method(), hata.code, deneme + 1,
				)
				if hata.code in GOREV_API_GECICI_HTTP_KODLARI and deneme + 1 < len(gecikmeler):
					continue
				raise HesapHatasi(_("{mesaj} (HTTP {kod})").format(mesaj=hata_metni, kod=hata.code))
			except (urllib.error.URLError, OSError):
				logHandler.log.warning(
					"Dosya Arşivim görev API bağlantı hatası: yöntem=%s, deneme=%s",
					istek.get_method(), deneme + 1,
				)
				if deneme + 1 < len(gecikmeler):
					continue
				raise HesapHatasi(hata_metni)
			if len(govde) > GOREV_API_YANIT_SINIRI:
				raise HesapHatasi(hata_metni)
			try:
				sonuc = json.loads(govde.decode("utf-8"))
			except (UnicodeError, ValueError):
				raise HesapHatasi(hata_metni)
			if not isinstance(sonuc, dict) or sonuc.get("success") is not True:
				raise HesapHatasi(hata_metni)
			return sonuc
		raise HesapHatasi(hata_metni)

	def gorevi_yeniden_calistir(self, gorev_id):
		"""Hata durumundaki görevin resmî rerun işlemini sınırlı yeniden denemeyle gönderir."""
		if isinstance(gorev_id, bool) or not isinstance(gorev_id, int) or gorev_id < 1:
			raise HesapHatasi(_("Sunucu görevi yeniden çalıştırılamadı."))
		self._s3_anahtarlarini_al_veya_olustur()
		govde = json.dumps({"op": "rerun", "task_id": gorev_id}, separators=(",", ":")).encode("utf-8")
		istek = urllib.request.Request(
			f"{SUNUCU_ADRESI}{GOREVLER_PATH}",
			data=govde,
			headers={
				"Authorization": f"LOW {self.s3_anahtari}:{self.s3_gizli_anahtari}",
				"Content-Type": "application/json",
				"Content-Length": str(len(govde)),
				"X-Accept-Reduced-Priority": "1",
			},
			method="PUT",
		)
		sonuc = self._gorev_api_json_istegi(istek, _("Sunucu görevi yeniden çalıştırılamadı."))
		deger = sonuc.get("value")
		if not isinstance(deger, dict) or str(gorev_id) not in deger:
			raise HesapHatasi(_("Sunucu görevi yeniden çalıştırılamadı."))
		return True

	def dosya_baglantisi(self, eposta, klasor, dosya_adi):
		"""Dosyanın paylaşılabilir doğrudan indirme bağlantısını üretir."""
		return self.uzak_dosya_baglantisi(eposta, f"{klasor}/{dosya_adi}")

	def uzak_dosya_baglantisi(self, eposta, uzak_yol):
		"""Arşiv öğesindeki tam dosya yolunun doğrudan indirme bağlantısını üretir."""
		oge_kimligi = self.ana_oge_kimligi(eposta)
		return f"{SUNUCU_ADRESI}/download/{urllib.parse.quote(oge_kimligi)}/{urllib.parse.quote(uzak_yol, safe='/')}"

	def dosya_indir(self, eposta, klasor, dosya_adi, hedef_yol, iptal_olayi):
		"""Dosyayı geçici bir dosyaya indirir, tamamlanınca hedefe taşır."""
		url = self.dosya_baglantisi(eposta, klasor, dosya_adi)
		gecici_yol = None
		try:
			dosya_tanimi, gecici_yol = tempfile.mkstemp(
				prefix=".dosya_arsivim_", suffix=".part", dir=os.path.dirname(hedef_yol) or None
			)
			with os.fdopen(dosya_tanimi, "wb") as hedef:
				with self.acici.open(urllib.request.Request(url), timeout=30) as yanit:
					while True:
						if iptal_olayi.is_set():
							raise HesapHatasi(_("İndirme iptal edildi."))
						parca = yanit.read(1024 * 128)
						if not parca:
							break
						hedef.write(parca)
			if iptal_olayi.is_set():
				raise HesapHatasi(_("İndirme iptal edildi."))
			os.replace(gecici_yol, hedef_yol)
			gecici_yol = None
		except (urllib.error.URLError, OSError):
			raise HesapHatasi(_("Dosya indirilemedi."))
		finally:
			if gecici_yol and os.path.exists(gecici_yol):
				try:
					os.remove(gecici_yol)
				except OSError:
					pass

	def tum_dosyalari_al(self, eposta, turetilmisleri_goster=False):
		"""Ana ögedeki doğrudan klasör dosyalarını, isteğe göre türevlerle döndürür."""
		oge_kimligi = self.ana_oge_kimligi(eposta)
		url = f"{SUNUCU_ADRESI}/metadata/{urllib.parse.quote(oge_kimligi)}?reCache=1"
		try:
			with self.acici.open(urllib.request.Request(url), timeout=30) as yanit:
				sonuc = json.loads(yanit.read().decode("utf-8"))
		except (urllib.error.URLError, OSError, ValueError):
			raise HesapHatasi(_("Dosya listesi alınamadı."))
		dosyalar = {}
		for bilgi in sonuc.get("files", []):
			ad = bilgi.get("name") if isinstance(bilgi, dict) else None
			kaynak = bilgi.get("source") if isinstance(bilgi, dict) else None
			bicim = bilgi.get("format") if isinstance(bilgi, dict) else None
			if (
				isinstance(ad, str)
				and (
					bicim == "Archive BitTorrent"
					or (kaynak == "metadata" and ad.casefold().endswith("_archive.torrent"))
				)
			):
				continue
			if (
				not isinstance(ad, str)
				or kaynak not in ("original", "derivative")
				or ad.startswith(".")
				or "/" not in ad
			):
				continue
			klasor, goreli_ad = ad.split("/", 1)
			if not goreli_ad:
				continue
			boyut = bilgi.get("size")
			zaman = bilgi.get("mtime")
			try:
				boyut = int(boyut)
			except (TypeError, ValueError):
				boyut = None
			try:
				zaman = int(zaman)
			except (TypeError, ValueError):
				zaman = None
			dosya = {
				"ad": goreli_ad,
				"boyut": boyut,
				"yukleme_zamani": zaman,
				"bicim": bicim if isinstance(bicim, str) else None,
				"kaynak": kaynak,
			}
			if kaynak == "original" or turetilmisleri_goster:
				dosyalar.setdefault(klasor, []).append(dosya)
		for klasor in dosyalar:
			dosyalar[klasor].sort(key=lambda dosya: dosya["ad"].casefold())
		return dosyalar

	def klasordeki_dosyalari_al(self, eposta, klasor, turetilmisleri_goster=False):
		"""Seçili klasördeki dosyaların ad ve temel ayrıntılarını döndürür."""
		return self.tum_dosyalari_al(eposta, turetilmisleri_goster).get(klasor, [])

	def dosya_arsivde_mi(self, eposta, klasor, dosya_adi):
		"""Yüklenen dosyanın genel arşiv listesindeki görünürlüğünü denetler."""
		return any(dosya["ad"] == dosya_adi for dosya in self.klasordeki_dosyalari_al(eposta, klasor))

	@property
	def varsayilan_klasorler(self):
		"""Arşivde kullanılan, dile bağlı olmayan varsayılan klasör kimlikleri."""
		return VARSAYILAN_KLASORLER

	def klasor_gorunen_adi(self, klasor):
		"""Sabit arşiv klasörü kimliğinin kullanıcıya gösterilecek adını döndürür."""
		return _(klasor)
