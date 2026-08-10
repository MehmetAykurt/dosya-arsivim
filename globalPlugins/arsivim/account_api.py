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
import urllib.error
import urllib.parse
import urllib.request

import addonHandler
addonHandler.initTranslation()

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
			"User-Agent": "DosyaArsivimNVDA/26.8.2",
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
		istek = urllib.request.Request(url, data=veri, headers=basliklar, method=method)
		try:
			with self.acici.open(istek, timeout=45) as yanit:
				return yanit.status
		except urllib.error.HTTPError as hata:
			if hata.code == 404 and method in ("HEAD", "DELETE"):
				return None
			raise HesapHatasi(hata_metni or _("Varsayılan klasörler oluşturulamadı."))
		except (urllib.error.URLError, OSError):
			raise HesapHatasi(hata_metni or _("Varsayılan klasörler oluşturulamadı."))

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

	def dosya_yukle(self, eposta, klasor, yerel_yol, ilerleme_bildir=None, durdurma_olayi=None):
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
				self._s3_istegi(
					oge_kimligi,
					uzak_yol,
					"PUT",
					veri,
					ek_basliklar={"Content-Length": str(boyut)},
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

	def dosya_baglantisi(self, eposta, klasor, dosya_adi):
		"""Dosyanın paylaşılabilir doğrudan indirme bağlantısını üretir."""
		oge_kimligi = self.ana_oge_kimligi(eposta)
		uzak_yol = f"{klasor}/{dosya_adi}"
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
		url = f"{SUNUCU_ADRESI}/metadata/{urllib.parse.quote(oge_kimligi)}"
		try:
			with self.acici.open(urllib.request.Request(url), timeout=30) as yanit:
				sonuc = json.loads(yanit.read().decode("utf-8"))
		except (urllib.error.URLError, OSError, ValueError):
			raise HesapHatasi(_("Dosya listesi alınamadı."))
		dosyalar = {}
		for bilgi in sonuc.get("files", []):
			ad = bilgi.get("name") if isinstance(bilgi, dict) else None
			kaynak = bilgi.get("source") if isinstance(bilgi, dict) else None
			if (
				not isinstance(ad, str)
				or kaynak not in ("original", "derivative")
				or (kaynak == "derivative" and not turetilmisleri_goster)
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
			dosyalar.setdefault(klasor, []).append({
				"ad": goreli_ad,
				"boyut": boyut,
				"yukleme_zamani": zaman,
				"bicim": bilgi.get("format") if isinstance(bilgi.get("format"), str) else None,
				"kaynak": kaynak,
			})
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
