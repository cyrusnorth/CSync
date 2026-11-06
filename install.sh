#!/bin/sh
# Installation (ou mise à jour) de CSync sur cette machine.
#
#   ./install.sh            installe puis lance la configuration guidée
#   ./install.sh --no-setup installe ou met à jour sans relancer la configuration
#
# Ce script ne modifie rien en dehors de ~/.csync (et d'un lien ~/.local/bin/csync).

set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
CSYNC_HOME="${CSYNC_DIR:-$HOME/.csync}"
RUN_SETUP=1
for arg in "$@"; do
	case "$arg" in
		--no-setup) RUN_SETUP=0 ;;
		*) echo "Option inconnue : $arg" >&2; exit 2 ;;
	esac
done

say() { printf '%s\n' "$*"; }
fail() { printf 'Erreur : %s\n' "$*" >&2; exit 1; }

# 1. Python 3.9 ou plus récent
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
	if command -v "$candidate" >/dev/null 2>&1; then
		if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
			PYTHON=$(command -v "$candidate")
			break
		fi
	fi
done
[ -n "$PYTHON" ] || fail "Python 3.9 ou plus récent est requis (sur macOS : « xcode-select --install »)."
command -v git >/dev/null 2>&1 || fail "git est requis."
say "Python utilisé : $PYTHON ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

# 2. Environnement Python isolé (recréé s'il est incomplet ou cassé, par exemple après une
#    mise à jour de Python)
mkdir -p "$CSYNC_HOME"
chmod 700 "$CSYNC_HOME"
CSYNC_HOME=$(cd "$CSYNC_HOME" && pwd)
VPY="$CSYNC_HOME/venv/bin/python"
venv_ok() {
	[ -x "$VPY" ] && "$VPY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 \
		&& "$VPY" -m pip --version >/dev/null 2>&1
}
if ! venv_ok; then
	say "Création de l'environnement Python…"
	rm -rf "$CSYNC_HOME/venv"
	"$PYTHON" -m venv "$CSYNC_HOME/venv" >/dev/null 2>&1 || true
	venv_ok || fail "Impossible de créer l'environnement Python avec pip (sur Debian/Ubuntu : « sudo apt install python3-venv », puis relancez ce script)."
fi
say "Installation des dépendances…"
"$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
"$VPY" -m pip install --quiet --disable-pip-version-check "cryptography>=41" || fail "Installation de « cryptography » impossible (réseau ?)."
if [ "$(uname -s)" = "Darwin" ]; then
	"$VPY" -m pip install --quiet --disable-pip-version-check "PySide6-Essentials>=6.5" \
		|| say "Attention : la bibliothèque de la fenêtre (PySide6) n'a pas pu être installée. La ligne de commande fonctionne quand même."
fi

# 3. Code de CSync
rm -rf "$CSYNC_HOME/app.new"
mkdir -p "$CSYNC_HOME/app.new"
cp -R "$SRC_DIR/csync" "$CSYNC_HOME/app.new/csync"
find "$CSYNC_HOME/app.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$CSYNC_HOME/app.old"
[ -d "$CSYNC_HOME/app" ] && mv "$CSYNC_HOME/app" "$CSYNC_HOME/app.old"
mv "$CSYNC_HOME/app.new" "$CSYNC_HOME/app"
rm -rf "$CSYNC_HOME/app.old"

# 4. Lanceur. Le dossier d'installation y est inscrit en dur : les hooks, launchd et systemd
#    retrouvent donc toujours la bonne installation, quel que soit leur environnement.
mkdir -p "$CSYNC_HOME/bin"
QUOTED_HOME=$(printf '%s' "$CSYNC_HOME" | sed "s/'/'\\\\''/g")
{
	printf '%s\n' '#!/bin/sh'
	printf '%s\n' '# Lanceur de CSync (généré par install.sh). -I isole Python des variables et paquets de l'"'"'utilisateur.'
	printf "CSYNC_DIR='%s'\n" "$QUOTED_HOME"
	printf '%s\n' 'export CSYNC_DIR'
	printf '%s\n' 'exec "$CSYNC_DIR/venv/bin/python" -I -c '"'"'import sys; sys.path.insert(0, sys.argv.pop(1)); from csync.cli import main; sys.exit(main(sys.argv[1:]))'"'"' "$CSYNC_DIR/app" "$@"'
} > "$CSYNC_HOME/bin/csync.new"
chmod 755 "$CSYNC_HOME/bin/csync.new"
mv "$CSYNC_HOME/bin/csync.new" "$CSYNC_HOME/bin/csync"
"$CSYNC_HOME/bin/csync" --version >/dev/null || fail "Le lanceur ne fonctionne pas."

# 5. Commande « csync » dans le terminal
mkdir -p "$HOME/.local/bin"
ln -sf "$CSYNC_HOME/bin/csync" "$HOME/.local/bin/csync"
case ":$PATH:" in
	*":$HOME/.local/bin:"*) ;;
	*) say "Ajoutez ~/.local/bin à votre PATH pour taper « csync » directement, par exemple :"
	   say "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc   (ou ~/.bashrc sur Linux)" ;;
esac

say "CSync $("$CSYNC_HOME/bin/csync" --version | awk '{print $2}') installé dans $CSYNC_HOME."

# Mise à jour : l'application macOS et les hooks pointent déjà vers le lanceur, rien d'autre à faire.
if [ "$RUN_SETUP" = "1" ]; then
	if [ -t 0 ]; then
		exec "$CSYNC_HOME/bin/csync" setup
	else
		say "Lancez maintenant : csync setup"
	fi
fi
