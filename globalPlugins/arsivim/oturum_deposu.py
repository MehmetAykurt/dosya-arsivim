# -*- coding: utf-8 -*-
"""Oturum bilgisini NVDA ayar klasöründe şifreli olarak saklar."""

import base64
import ctypes
from ctypes import wintypes
import json
import os

import globalVars


CRYPTPROTECT_UI_FORBIDDEN = 0x1


class VeriBlogu(ctypes.Structure):
	_fields_ = [
		("boyut", wintypes.DWORD),
		("veri", ctypes.POINTER(ctypes.c_byte)),
	]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_crypt32.CryptProtectData.argtypes = (
	ctypes.POINTER(VeriBlogu), wintypes.LPCWSTR, ctypes.POINTER(VeriBlogu),
	ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(VeriBlogu),
)
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = (
	ctypes.POINTER(VeriBlogu), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(VeriBlogu),
	ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(VeriBlogu),
)
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
_kernel32.LocalFree.restype = wintypes.HLOCAL


def _veri_blogu(veri):
	"""Bayt verisini Windows API'sinin kullanacağı yapıya dönüştürür."""
	tampon = ctypes.create_string_buffer(veri)
	return VeriBlogu(len(veri), ctypes.cast(tampon, ctypes.POINTER(ctypes.c_byte))), tampon


def _sifrele(veri):
	girdi, tampon = _veri_blogu(veri)
	cikti = VeriBlogu()
	if not _crypt32.CryptProtectData(
		ctypes.byref(girdi), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(cikti)
	):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(cikti.veri, cikti.boyut)
	finally:
		_kernel32.LocalFree(cikti.veri)


def _sifre_coz(veri):
	girdi, tampon = _veri_blogu(veri)
	cikti = VeriBlogu()
	if not _crypt32.CryptUnprotectData(
		ctypes.byref(girdi), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(cikti)
	):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(cikti.veri, cikti.boyut)
	finally:
		_kernel32.LocalFree(cikti.veri)


class OturumDeposu:
	"""Yalnızca geçerli Windows kullanıcısının açabileceği oturum deposu."""

	def __init__(self):
		ayar_klasoru = globalVars.appArgs.configPath
		if not ayar_klasoru:
			raise OSError("NVDA ayar klasörü belirlenemedi.")
		self.klasor = os.path.join(os.fspath(ayar_klasoru), "dosya_arsivim")
		self.dosya_yolu = os.path.join(self.klasor, "oturum.dat")

	def yukle(self):
		"""Şifreli oturumu okur; dosya yoksa veya geçersizse None döndürür."""
		try:
			with open(self.dosya_yolu, "r", encoding="utf-8") as dosya:
				sifreli_veri = base64.b64decode(json.load(dosya)["veri"], validate=True)
			veri = _sifre_coz(sifreli_veri)
			sonuc = json.loads(veri.decode("utf-8"))
			if not isinstance(sonuc.get("eposta"), str) or not isinstance(sonuc.get("cerezler"), list):
				return None
			return sonuc
		except (OSError, KeyError, TypeError, ValueError, UnicodeError):
			return None

	def kaydet(self, eposta, cerezler, s3_anahtari=None, s3_gizli_anahtari=None):
		"""Oturum verisini Windows DPAPI ile şifreleyerek atomik biçimde yazar."""
		if not cerezler:
			raise ValueError("Kaydedilecek oturum çerezi yok.")
		icerik = {"surum": 2, "eposta": eposta, "cerezler": cerezler}
		if s3_anahtari and s3_gizli_anahtari:
			icerik["s3_anahtari"] = s3_anahtari
			icerik["s3_gizli_anahtari"] = s3_gizli_anahtari
		veri = json.dumps(
			icerik,
			ensure_ascii=False,
			separators=(",", ":"),
		).encode("utf-8")
		kayit = json.dumps(
			{"veri": base64.b64encode(_sifrele(veri)).decode("ascii")},
			separators=(",", ":"),
		)
		os.makedirs(self.klasor, exist_ok=True)
		gecici_yol = self.dosya_yolu + ".tmp"
		try:
			with open(gecici_yol, "w", encoding="utf-8") as dosya:
				dosya.write(kayit)
			os.replace(gecici_yol, self.dosya_yolu)
		finally:
			if os.path.exists(gecici_yol):
				os.remove(gecici_yol)

	def sil(self):
		"""Kalıcı oturumu siler."""
		try:
			os.remove(self.dosya_yolu)
		except FileNotFoundError:
			pass
