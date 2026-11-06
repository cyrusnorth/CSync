"""Crée l'application CSync.app dans ~/Applications (macOS)."""

from __future__ import annotations

import os

from .paths import Paths, system_name
from .util import CSyncError, atomic_write_text, ensure_dir

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>CSync</string>
	<key>CFBundleDisplayName</key>
	<string>CSync</string>
	<key>CFBundleIdentifier</key>
	<string>com.csync.app</string>
	<key>CFBundleExecutable</key>
	<string>CSync</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0</string>
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
</dict>
</plist>
"""


def install_app(paths: Paths) -> str:
	if system_name() != "darwin":
		raise CSyncError("L'application n'existe que sur macOS ; ailleurs, utilisez la commande « csync ».")
	app = os.path.join(paths.home, "Applications", "CSync.app")
	contents = os.path.join(app, "Contents")
	ensure_dir(os.path.join(contents, "MacOS"))
	ensure_dir(os.path.join(contents, "Resources"))
	atomic_write_text(os.path.join(contents, "Info.plist"), INFO_PLIST, 0o644)
	# Une application ouverte depuis le Finder reçoit un PATH minimal : on le complète pour que
	# git, ssh et claude soient trouvés comme dans le terminal.
	launcher = ("#!/bin/sh\n"
				"PATH=\"$HOME/.local/bin:$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:$PATH\"\n"
				"export PATH\n"
				"exec \"%s\" gui\n" % paths.bin_path)
	atomic_write_text(os.path.join(contents, "MacOS", "CSync"), launcher, 0o755)
	return "Application installée : %s (ouvrable depuis le Finder ou Spotlight)." % app
