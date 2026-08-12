# -*- coding: utf-8 -*-
"""Dosya Arşivim NVDA genel eklentisi."""

import addonHandler
addonHandler.initTranslation()

import globalPluginHandler
import gui
import logHandler
import ui
import wx

from .dialogs import DosyaArsivimPenceresi, YUKLEME_YONETICISI
from .onbellek import DosyaOnbellegi
from .sqlite_compat import sqlite3


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	"""Dosya Arşivim penceresini NVDA içinden açar."""

	scriptCategory = _("Dosya Arşivim")

	def __init__(self):
		super().__init__()
		self.pencere = None
		try:
			self.onbellek = DosyaOnbellegi()
		except (OSError, sqlite3.Error):
			self.onbellek = None
			logHandler.log.exception("Dosya Arşivim önbelleği hazırlanamadı.")
		YUKLEME_YONETICISI.devam_et()

	def terminate(self):
		YUKLEME_YONETICISI.durdur()
		if self.pencere and self.pencere.IsShown():
			self.pencere.kapanisa_hazirla()
			self.pencere.Destroy()
		self.pencere = None
		super().terminate()

	def script_dosya_arsivim_ac(self, gesture):
		"""Dosya Arşivim."""
		wx.CallAfter(self._pencereyi_ac)

	def _pencereyi_ac(self):
		if self.pencere and self.pencere.IsShown():
			self.pencere.Raise()
			self.pencere.SetFocus()
			return
		try:
			self.pencere = DosyaArsivimPenceresi(gui.mainFrame, self._pencere_kapandi, self.onbellek)
			self.pencere.Show()
			self.pencere.Raise()
		except Exception:
			self.pencere = None
			logHandler.log.exception("Dosya Arşivim ana penceresi açılamadı.")
			ui.message(_("Dosya Arşivim açılamadı. Ayrıntılar için NVDA günlüğünü kontrol edin."))

	def _pencere_kapandi(self):
		self.pencere = None

	__gestures = {
		"kb:nvda+alt+a": "dosya_arsivim_ac",
	}
