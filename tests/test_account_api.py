import io
import importlib.util
from pathlib import Path
import sys
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


class KuyrukSirasiTesti(unittest.TestCase):
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
