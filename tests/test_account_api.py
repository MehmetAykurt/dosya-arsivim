import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


KOK = Path(__file__).resolve().parents[1]
MODUL_YOLU = KOK / "globalPlugins" / "arsivim" / "account_api.py"

addon_handler = types.ModuleType("addonHandler")
addon_handler.initTranslation = lambda: None
sys.modules.setdefault("addonHandler", addon_handler)

log_handler = types.ModuleType("logHandler")
log_handler.log = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
sys.modules.setdefault("logHandler", log_handler)

paket = types.ModuleType("arsivim")
paket.__path__ = [str(MODUL_YOLU.parent)]
sys.modules.setdefault("arsivim", paket)

oturum_deposu = types.ModuleType("arsivim.oturum_deposu")
oturum_deposu.OturumDeposu = object
oturum_deposu._sifre_coz = lambda veri: veri
oturum_deposu._sifrele = lambda veri: veri
sys.modules.setdefault("arsivim.oturum_deposu", oturum_deposu)

global_vars = types.ModuleType("globalVars")
global_vars.appArgs = types.SimpleNamespace(configPath=None)
sys.modules.setdefault("globalVars", global_vars)

spec = importlib.util.spec_from_file_location("arsivim.account_api", MODUL_YOLU)
account_api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = account_api
spec.loader.exec_module(account_api)

kuyruk_spec = importlib.util.spec_from_file_location(
	"arsivim.yukleme_kuyrugu",
	MODUL_YOLU.parent / "yukleme_kuyrugu.py",
)
yukleme_kuyrugu = importlib.util.module_from_spec(kuyruk_spec)
sys.modules[kuyruk_spec.name] = yukleme_kuyrugu
kuyruk_spec.loader.exec_module(yukleme_kuyrugu)


class _Yanit:
	status = 200

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc, traceback):
		return False


class _JsonYanit(_Yanit):
	def __init__(self, veri):
		self.veri = veri

	def read(self, boyut=-1):
		veri = json.dumps(self.veri).encode("utf-8")
		return veri if boyut < 0 else veri[:boyut]


class HesapGirisTesti(unittest.TestCase):
	def test_csrf_belirteci_guncel_servisten_alinir(self):
		istem = object.__new__(account_api.HesapIstemi)
		istekler = []

		def istek(yol, method="GET", veri=None):
			istekler.append((yol, method, veri))
			return {"token": "guvenlik-belirteci"}

		istem._istek = istek

		self.assertEqual(istem.csrf_belirteci_al(), "guvenlik-belirteci")
		self.assertEqual(istekler, [(account_api.CSRF_PATH, "GET", None)])

	def test_eposta_kodu_istegi_csrf_basligini_gonderir(self):
		istem = object.__new__(account_api.HesapIstemi)
		istem.csrf_belirteci_al = lambda: "guvenlik-belirteci"
		istekler = []

		def istek(yol, method="GET", veri=None, ek_basliklar=None):
			istekler.append((yol, method, veri, ek_basliklar))

		istem._istek = istek
		istem.eposta_kodu_gonder("kullanici@example.com", False)

		self.assertEqual(istekler[0][0], account_api.OTP_PATH)
		self.assertEqual(istekler[0][1], "POST")
		self.assertEqual(istekler[0][3], {"X-CSRF-Token": "guvenlik-belirteci"})


class YonlendirmeTesti(unittest.TestCase):
	def test_put_307_yonlendirmesinde_govdeyi_ve_yetkiyi_korur(self):
		govdeler = []
		yetkiler = []

		class IstekIsleyicisi(BaseHTTPRequestHandler):
			def do_PUT(self):
				uzunluk = int(self.headers.get("Content-Length", "0"))
				govdeler.append(self.rfile.read(uzunluk))
				yetkiler.append(self.headers.get("Authorization"))
				if self.path == "/baslangic":
					self.send_response(307)
					self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/hedef")
					self.end_headers()
					return
				self.send_response(200)
				self.end_headers()

			def log_message(self, format, *args):
				pass

		sunucu = ThreadingHTTPServer(("127.0.0.1", 0), IstekIsleyicisi)
		is_parcacigi = threading.Thread(target=sunucu.serve_forever, daemon=True)
		is_parcacigi.start()
		isleyici = account_api._ArsivYonlendirmeIsleyicisi()
		isleyici._guvenilir_archive_hostu = lambda host: True
		acici = urllib.request.build_opener(isleyici)
		govde = account_api._IlerlemeliDosya(io.BytesIO(b"deneme"), 6, None)
		istek = urllib.request.Request(
			f"http://127.0.0.1:{sunucu.server_port}/baslangic",
			data=govde,
			headers={"Authorization": "LOW deneme:gizli", "Content-Length": "6"},
			method="PUT",
		)
		try:
			with acici.open(istek, timeout=5) as yanit:
				self.assertEqual(yanit.status, 200)
		finally:
			sunucu.shutdown()
			sunucu.server_close()
		self.assertEqual(govdeler, [b"deneme", b"deneme"])
		self.assertEqual(yetkiler, ["LOW deneme:gizli", "LOW deneme:gizli"])

	def test_delete_307_yonlendirmesinde_yontemi_ve_basliklari_korur(self):
		istekler = []

		class IstekIsleyicisi(BaseHTTPRequestHandler):
			def do_DELETE(self):
				istekler.append((self.path, self.headers.get("Authorization"), self.headers.get("X-Archive-Cascade-Delete")))
				if self.path == "/baslangic":
					self.send_response(307)
					self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/hedef")
					self.end_headers()
					return
				self.send_response(204)
				self.end_headers()

			def log_message(self, format, *args):
				pass

		sunucu = ThreadingHTTPServer(("127.0.0.1", 0), IstekIsleyicisi)
		is_parcacigi = threading.Thread(target=sunucu.serve_forever, daemon=True)
		is_parcacigi.start()
		isleyici = account_api._ArsivYonlendirmeIsleyicisi()
		isleyici._guvenilir_archive_hostu = lambda host: True
		acici = urllib.request.build_opener(isleyici)
		istek = urllib.request.Request(
			f"http://127.0.0.1:{sunucu.server_port}/baslangic",
			headers={"Authorization": "LOW deneme:gizli", "X-Archive-Cascade-Delete": "1"},
			method="DELETE",
		)
		try:
			with acici.open(istek, timeout=5) as yanit:
				self.assertEqual(yanit.status, 204)
		finally:
			sunucu.shutdown()
			sunucu.server_close()
		self.assertEqual(
			istekler,
			[
				("/baslangic", "LOW deneme:gizli", "1"),
				("/hedef", "LOW deneme:gizli", "1"),
			],
		)


class YenidenDenemeTesti(unittest.TestCase):
	def test_gecici_503_sonrasinda_put_govdesini_bastan_gonderir(self):
		class SahteAcici:
			def __init__(self):
				self.govdeler = []

			def open(self, istek, timeout):
				if "check_limit=1" in istek.full_url:
					return _JsonYanit({"over_limit": 0})
				self.govdeler.append(istek.data.read())
				if len(self.govdeler) == 1:
					xml = io.BytesIO(b"<Error><Code>SlowDown</Code><Message>Busy</Message></Error>")
					raise urllib.error.HTTPError(istek.full_url, 503, "Busy", {}, xml)
				return _Yanit()

		istem = object.__new__(account_api.HesapIstemi)
		istem.s3_anahtari = "anahtar"
		istem.s3_gizli_anahtari = "gizli"
		istem.acici = SahteAcici()
		govde = account_api._IlerlemeliDosya(io.BytesIO(b"yeniden"), 7, None)
		eski_gecikmeler = account_api.S3_YENIDEN_DENEME_GECIKMELERI
		account_api.S3_YENIDEN_DENEME_GECIKMELERI = (0,)
		try:
			sonuc = istem._s3_istegi(
				"oge",
				"Belgeler/deneme.txt",
				"PUT",
				govde,
				ek_basliklar={"Content-Length": "7"},
				hata_metni="Dosya yüklenemedi.",
			)
		finally:
			account_api.S3_YENIDEN_DENEME_GECIKMELERI = eski_gecikmeler
		self.assertEqual(sonuc, 200)
		self.assertEqual(istem.acici.govdeler, [b"yeniden", b"yeniden"])

	def test_put_ve_delete_tutarlı_etkilesimli_oncelik_kullanir(self):
		class SahteAcici:
			def __init__(self):
				self.yazma_istekleri = []

			def open(self, istek, timeout):
				if "check_limit=1" in istek.full_url:
					return _JsonYanit({"over_limit": 0})
				self.yazma_istekleri.append(istek)
				return _Yanit()

		istem = object.__new__(account_api.HesapIstemi)
		istem.s3_anahtari = "anahtar"
		istem.s3_gizli_anahtari = "gizli"
		istem.acici = SahteAcici()
		istem._s3_istegi("oge", "Belgeler/a.txt", "PUT", b"a")
		istem._s3_istegi("oge", "Belgeler/a.txt", "DELETE")

		self.assertEqual(len(istem.acici.yazma_istekleri), 2)
		for istek in istem.acici.yazma_istekleri:
			self.assertEqual(istek.get_header("X-archive-interactive-priority"), "1")

	def test_sunucu_yogunken_yazma_istegi_gonderilmez(self):
		class SahteAcici:
			def __init__(self):
				self.yazma_sayisi = 0

			def open(self, istek, timeout):
				if "check_limit=1" in istek.full_url:
					return _JsonYanit({"over_limit": 1})
				self.yazma_sayisi += 1
				return _Yanit()

		istem = object.__new__(account_api.HesapIstemi)
		istem.s3_anahtari = "anahtar"
		istem.s3_gizli_anahtari = "gizli"
		istem.acici = SahteAcici()
		eski_gecikmeler = account_api.S3_YENIDEN_DENEME_GECIKMELERI
		account_api.S3_YENIDEN_DENEME_GECIKMELERI = (0,)
		try:
			with self.assertRaises(account_api.HesapHatasi):
				istem._s3_istegi("oge", "Belgeler/a.txt", "PUT", b"a")
		finally:
			account_api.S3_YENIDEN_DENEME_GECIKMELERI = eski_gecikmeler
		self.assertEqual(istem.acici.yazma_sayisi, 0)


class TurevGorunurluguTesti(unittest.TestCase):
	def test_turevler_ayara_gore_listelenir_ve_torrent_gizlenir(self):
		class MetadataYaniti:
			def __enter__(self):
				return self

			def __exit__(self, exc_type, exc, traceback):
				return False

			def read(self):
				return json.dumps({
					"files": [
						{
							"name": "Ses ve Müzik/ses.wav",
							"source": "original",
							"format": "WAVE",
						},
						{
							"name": "Ses ve Müzik/ses_vbr.mp3",
							"source": "derivative",
							"original": "Ses ve Müzik/ses.wav",
							"format": "VBR MP3",
						},
						{
							"name": "oge_archive.torrent",
							"source": "metadata",
							"format": "Archive BitTorrent",
						},
					],
				}).encode("utf-8")

		class SahteAcici:
			def open(self, istek, timeout):
				return MetadataYaniti()

		istem = object.__new__(account_api.HesapIstemi)
		istem.acici = SahteAcici()
		gizli = istem.tum_dosyalari_al("deneme@example.com", False)
		gorunur = istem.tum_dosyalari_al("deneme@example.com", True)

		self.assertEqual([dosya["ad"] for dosya in gizli["Ses ve Müzik"]], ["ses.wav"])
		self.assertEqual(
			[dosya["ad"] for dosya in gorunur["Ses ve Müzik"]],
			["ses.wav", "ses_vbr.mp3"],
		)
		self.assertNotIn("oge_archive.torrent", [dosya["ad"] for dosya in gorunur["Ses ve Müzik"]])


class GorevDurumlariTesti(unittest.TestCase):
	def test_hata_veren_gorev_resmi_api_ile_yeniden_calistirilir(self):
		class SahteAcici:
			def __init__(self):
				self.istekler = []

			def open(self, istek, timeout):
				self.istekler.append(istek)
				return _JsonYanit({"success": True, "value": {"123": "dosya-arsivim-deneme"}})

		istem = object.__new__(account_api.HesapIstemi)
		istem.s3_anahtari = "anahtar"
		istem.s3_gizli_anahtari = "gizli"
		istem._s3_anahtarlarini_al_veya_olustur = lambda: None
		istem.acici = SahteAcici()

		self.assertTrue(istem.gorevi_yeniden_calistir(123))
		istek = istem.acici.istekler[0]
		self.assertEqual(istek.get_method(), "PUT")
		self.assertEqual(json.loads(istek.data.decode("utf-8")), {"op": "rerun", "task_id": 123})
		self.assertEqual(istek.get_header("X-accept-reduced-priority"), "1")

	def test_gorev_yeniden_calistirma_gecici_429_sonrasinda_sinirli_yeniden_denir(self):
		class SahteAcici:
			def __init__(self):
				self.sayi = 0

			def open(self, istek, timeout):
				self.sayi += 1
				if self.sayi == 1:
					raise urllib.error.HTTPError(istek.full_url, 429, "gizli", {}, io.BytesIO(b"gizli"))
				return _JsonYanit({"success": True, "value": {"123": "dosya-arsivim-deneme"}})

		istem = object.__new__(account_api.HesapIstemi)
		istem.s3_anahtari = "anahtar"
		istem.s3_gizli_anahtari = "gizli"
		istem._s3_anahtarlarini_al_veya_olustur = lambda: None
		istem.acici = SahteAcici()
		eski_gecikmeler = account_api.GOREV_API_YENIDEN_DENEME_GECIKMELERI
		account_api.GOREV_API_YENIDEN_DENEME_GECIKMELERI = (0,)
		try:
			self.assertTrue(istem.gorevi_yeniden_calistir(123))
		finally:
			account_api.GOREV_API_YENIDEN_DENEME_GECIKMELERI = eski_gecikmeler
		self.assertEqual(istem.acici.sayi, 2)

	def test_s3_hata_ayrintisi_yalnizca_guvenli_servis_kodunu_gosterir(self):
		guvenli = account_api.HesapIstemi._s3_kullanici_hatasi("Yüklenemedi.", 503, "SlowDown")
		guvensiz = account_api.HesapIstemi._s3_kullanici_hatasi(
			"Yüklenemedi.", 500, "Authorization LOW anahtar:cok-gizli"
		)

		self.assertIn("SlowDown", guvenli)
		self.assertIn("HTTP 500", guvensiz)
		self.assertNotIn("cok-gizli", guvensiz)

	def test_resmi_durumlar_eslenir_ve_hassas_alanlar_atilir(self):
		class SahteAcici:
			def __init__(self):
				self.istek = None

			def open(self, istek, timeout):
				self.istek = istek
				return _JsonYanit({
					"success": True,
					"value": {
						"summary": {"queued": 1, "running": "1", "error": 1, "paused": 1},
						"catalog": [
							{"task_id": 11, "cmd": "archive.php", "wait_admin": 0, "submitter": "gizli@example.com"},
							{"task_id": "12", "cmd": "derive.php", "wait_admin": "1", "args": {"token": "gizli"}},
							{"task_id": 13, "cmd": "modify_xml.php", "wait_admin": 2, "server": "gizli-sunucu"},
							{"task_id": 14, "cmd": "archive.php", "wait_admin": 9, "secret": "gizli"},
						],
					},
				})

		istem = object.__new__(account_api.HesapIstemi)
		istem.s3_anahtari = "anahtar"
		istem.s3_gizli_anahtari = "gizli-anahtar"
		istem._s3_anahtarlarini_al_veya_olustur = lambda: None
		istem.acici = SahteAcici()

		sonuc = istem.gorev_durumlarini_al("deneme@example.com")

		self.assertEqual([gorev["durum"] for gorev in sonuc["gorevler"]], ["queued", "running", "error", "paused"])
		self.assertTrue(sonuc["gorevler"][2]["yeniden_calistirilabilir"])
		self.assertFalse(sonuc["gorevler"][3]["yeniden_calistirilabilir"])
		self.assertEqual(sonuc["ozet"], {"queued": 1, "running": 1, "error": 1, "paused": 1})
		metin = repr(sonuc)
		self.assertNotIn("gizli@example.com", metin)
		self.assertNotIn("gizli-sunucu", metin)
		self.assertNotIn("token", metin)
		self.assertEqual(istem.acici.istek.get_header("Authorization"), "LOW anahtar:gizli-anahtar")

	def test_gorev_api_hatasi_sunucu_govdesini_kullaniciya_sizdirmaz(self):
		class SahteAcici:
			def open(self, istek, timeout):
				govde = io.BytesIO(b'{"error":"Authorization LOW anahtar:cok-gizli"}')
				raise urllib.error.HTTPError(istek.full_url, 500, "cok-gizli", {}, govde)

		istem = object.__new__(account_api.HesapIstemi)
		istem.s3_anahtari = "anahtar"
		istem.s3_gizli_anahtari = "cok-gizli"
		istem._s3_anahtarlarini_al_veya_olustur = lambda: None
		istem.acici = SahteAcici()

		with self.assertRaises(account_api.HesapHatasi) as hata:
			istem.gorev_durumlarini_al("deneme@example.com")
		self.assertIn("HTTP 500", str(hata.exception))
		self.assertNotIn("cok-gizli", str(hata.exception))


class TurevUretimiTesti(unittest.TestCase):
	def _yukleme_basliklarini_al(self, turev_uret=True):
		istekler = []
		istem = object.__new__(account_api.HesapIstemi)
		istem.s3_anahtari = "anahtar"
		istem.s3_gizli_anahtari = "gizli"
		istem._s3_anahtarlarini_al_veya_olustur = lambda: None
		istem._s3_istegi = lambda *args, **kwargs: istekler.append(kwargs["ek_basliklar"])
		with tempfile.TemporaryDirectory() as gecici_klasor:
			dosya_yolu = Path(gecici_klasor) / "deneme.txt"
			dosya_yolu.write_bytes(b"deneme")
			istem.dosya_yukle(
				"deneme@example.com",
				"Belgeler",
				dosya_yolu,
				turev_uret=turev_uret,
			)
		return istekler[0]

	def test_turev_uretimi_kapatilinca_sunucu_basligi_gonderilir(self):
		basliklar = self._yukleme_basliklarini_al(False)
		self.assertEqual(basliklar["x-archive-queue-derive"], "0")

	def test_turev_uretimi_acikken_sunucunun_varsayilani_kullanilir(self):
		basliklar = self._yukleme_basliklarini_al(True)
		self.assertNotIn("x-archive-queue-derive", basliklar)


class KuyrukSirasiTesti(unittest.TestCase):
	def test_ayni_uzak_dosya_ikinci_kez_kuyruga_eklenmez(self):
		kuyruk = object.__new__(yukleme_kuyrugu.YuklemeKuyrugu)
		kuyruk.kilit = threading.RLock()
		kuyruk._kaydet = lambda: None
		kuyruk.kayitlar = []

		ilk = kuyruk.ekle("a@example.com", "Belgeler", [r"C:\\Bir\\deneme.txt"])
		ikinci = kuyruk.ekle(
			"a@example.com",
			"Belgeler",
			[r"D:\\Iki\\DENEME.TXT", r"D:\\Iki\\baska.txt"],
		)

		self.assertEqual(len(kuyruk.kayitlar), 2)
		self.assertEqual(len(ilk["eklenenler"]), 1)
		self.assertEqual(ikinci["yinelenenler"], [r"D:\\Iki\\DENEME.TXT"])
		self.assertEqual(ikinci["eklenenler"], [r"D:\\Iki\\baska.txt"])

	def test_sunucuda_gorunen_arsivlenmis_yuklemeyi_kuyruktan_temizler(self):
		kuyruk = object.__new__(yukleme_kuyrugu.YuklemeKuyrugu)
		kuyruk.kilit = threading.RLock()
		kaydetme_sayisi = []
		kuyruk._kaydet = lambda: kaydetme_sayisi.append(1)
		kuyruk.kayitlar = [
			{"id": "1", "eposta": "a@example.com", "klasor": "Uygulamalar", "yerel_yol": r"C:\\Dosyalar\\vbrecorder.zip", "durum": "arşivleniyor"},
			{"id": "2", "eposta": "a@example.com", "klasor": "Uygulamalar", "yerel_yol": r"C:\\Dosyalar\\bekleyen.zip", "durum": "bekliyor"},
			{"id": "3", "eposta": "a@example.com", "klasor": "Uygulamalar", "yerel_yol": r"C:\\Dosyalar\\turev.zip", "durum": "arşivleniyor"},
			{"id": "4", "eposta": "b@example.com", "klasor": "Uygulamalar", "yerel_yol": r"C:\\Dosyalar\\vbrecorder.zip", "durum": "arşivleniyor"},
		]

		tamamlananlar = kuyruk.sunucuda_gorunen_yuklemeleri_tamamla(
			"a@example.com",
			{
				"Uygulamalar": [
					{"ad": "vbrecorder.zip", "kaynak": "original"},
					{"ad": "turev.zip", "kaynak": "derivative"},
				],
			},
		)

		self.assertEqual([kayit["id"] for kayit in tamamlananlar], ["1"])
		self.assertEqual([kayit["id"] for kayit in kuyruk.kayitlar], ["2", "3", "4"])
		self.assertEqual(len(kaydetme_sayisi), 1)

	def test_beklenmedik_kesintide_yalnizca_etkin_aktarimi_beklemeye_alir(self):
		kuyruk = object.__new__(yukleme_kuyrugu.YuklemeKuyrugu)
		kuyruk.kilit = threading.RLock()
		kuyruk._kaydet = lambda: None
		kuyruk.kayitlar = [
			{"id": "1", "eposta": "a@example.com", "klasor": "Uygulamalar", "yerel_yol": "etkin.zip", "durum": "yükleniyor", "yuzde": 70},
			{"id": "2", "eposta": "a@example.com", "klasor": "Uygulamalar", "yerel_yol": "sunucuda.zip", "durum": "arşivleniyor"},
		]

		self.assertTrue(kuyruk.beklenmedik_kesintiyi_kurtar("1"))
		self.assertEqual(kuyruk.kayitlar[0]["durum"], "bekliyor")
		self.assertNotIn("yuzde", kuyruk.kayitlar[0])
		self.assertFalse(kuyruk.beklenmedik_kesintiyi_kurtar("2"))
		self.assertEqual(kuyruk.kayitlar[1]["durum"], "arşivleniyor")

	def test_bekleyen_yukleme_arsivde_islenen_kaydi_beklemez(self):
		kuyruk = object.__new__(yukleme_kuyrugu.YuklemeKuyrugu)
		kuyruk.kilit = threading.RLock()
		kuyruk._kaydet = lambda: None
		kuyruk.kayitlar = [
			{"id": "1", "eposta": "a@example.com", "klasor": "Uygulamalar", "yerel_yol": "ilk.txt", "durum": "arşivleniyor"},
			{"id": "2", "eposta": "a@example.com", "klasor": "Resimler", "yerel_yol": "ikinci.txt", "durum": "bekliyor"},
		]
		kayit = kuyruk.siradakini_al("a@example.com")
		self.assertEqual(kayit["id"], "2")
		self.assertEqual(kayit["durum"], "yükleniyor")

	def test_toplu_iptal_sunucuya_ulasan_tum_kayitlari_korur(self):
		kuyruk = object.__new__(yukleme_kuyrugu.YuklemeKuyrugu)
		kuyruk.kilit = threading.RLock()
		kuyruk._kaydet = lambda: None
		kuyruk.kayitlar = [
			{"id": "1", "eposta": "a@example.com", "klasor": "Belgeler", "yerel_yol": "bekleyen.txt", "durum": "bekliyor"},
			{"id": "2", "eposta": "a@example.com", "klasor": "Uygulamalar", "yerel_yol": "etkin.txt", "durum": "yükleniyor"},
			{"id": "3", "eposta": "a@example.com", "klasor": "Resimler", "yerel_yol": "islenen.txt", "durum": "arşivleniyor"},
			{"id": "4", "eposta": "b@example.com", "klasor": "Belgeler", "yerel_yol": "diger.txt", "durum": "bekliyor"},
		]

		iptal_edilenler = kuyruk.epostadakileri_iptal_et("a@example.com")

		self.assertEqual({kayit["id"] for kayit in iptal_edilenler}, {"1", "2", "3"})
		self.assertEqual(
			[(kayit["id"], kayit["durum"]) for kayit in kuyruk.kayitlar],
			[("2", "iptal_ediliyor"), ("3", "iptal_ediliyor"), ("4", "bekliyor")],
		)

	def test_silme_istegi_kabul_edilince_kayit_dogrulama_asamasinda_kalir(self):
		kuyruk = object.__new__(yukleme_kuyrugu.YuklemeKuyrugu)
		kuyruk.kilit = threading.RLock()
		kuyruk._kaydet = lambda: None
		kuyruk.kayitlar = [
			{"id": "1", "eposta": "a@example.com", "klasor": "Belgeler", "yerel_yol": "deneme.txt", "durum": "iptal_ediliyor"},
		]

		kuyruk.iptal_dogrulaniyor("1")

		self.assertEqual(kuyruk.kayitlar[0]["durum"], "iptal_dogrulaniyor")
		self.assertEqual(kuyruk.siradakini_al("a@example.com")["id"], "1")


if __name__ == "__main__":
	unittest.main()
