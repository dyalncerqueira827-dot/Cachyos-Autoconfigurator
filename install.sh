#!/bin/sh
# CachyOS Auto Configurator - bootstrap
#
# Uso (um comando só):
#   curl -fsSL https://raw.githubusercontent.com/dyalncerqueira827-dot/Cachyos-Autoconfigurator/main/install.sh | sh
#
# Com argumentos (ex.: simulação, sem alterar nada):
#   curl -fsSL <URL> | sh -s -- --dry-run
#
# O que este script faz (e SÓ isso):
#   1. baixa cachyos-autoconfig.py do repositório oficial para um diretório temporário;
#   2. mostra o SHA-256 do arquivo (e confere, se você passou CACHYOS_AC_SHA256);
#   3. executa o programa como root (via sudo), ligando o teclado (/dev/tty)
#      para que as perguntas Sim/Não/Pular funcionem mesmo com "curl | sh";
#   4. apaga o arquivo temporário ao terminar.
#
# Variáveis opcionais:
#   CACHYOS_AC_REF     branch ou tag a baixar (padrão: main). Ex.: v1.0.0
#   CACHYOS_AC_SHA256  hash esperado; se não bater, nada é executado.

set -eu

REPO_RAW="https://raw.githubusercontent.com/dyalncerqueira827-dot/Cachyos-Autoconfigurator"
REF="${CACHYOS_AC_REF:-main}"
SCRIPT="cachyos-autoconfig.py"
EXPECTED_SHA256="${CACHYOS_AC_SHA256:-}"

say() { printf '%s\n' "$*"; }
die() { printf 'Erro: %s\n' "$*" >&2; exit 1; }

# --- pré-requisitos ---------------------------------------------------------
command -v curl >/dev/null 2>&1 || die "curl não encontrado (instale com: sudo pacman -S curl)."
command -v python3 >/dev/null 2>&1 || die "python3 não encontrado (instale com: sudo pacman -S python)."
[ -r /dev/tty ] || die "é necessário um terminal interativo (/dev/tty)."

if [ -r /etc/os-release ] && ! grep -qi 'cachyos' /etc/os-release; then
    say "Aviso: este sistema não parece ser CachyOS; o programa se recusará a alterar algo."
fi

# --- download para diretório temporário privado -----------------------------
TMPDIR_AC="$(mktemp -d)"
FILE="$TMPDIR_AC/$SCRIPT"
cleanup() { rm -f "$FILE"; rmdir "$TMPDIR_AC" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

URL="$REPO_RAW/$REF/$SCRIPT"
say "Baixando $URL ..."
curl -fsSL "$URL" -o "$FILE" || die "falha ao baixar o programa."
[ -s "$FILE" ] || die "arquivo baixado está vazio."

# --- verificação de integridade ---------------------------------------------
if command -v sha256sum >/dev/null 2>&1; then
    HASH="$(sha256sum "$FILE" | cut -d' ' -f1)"
    say "SHA-256: $HASH"
    if [ -n "$EXPECTED_SHA256" ] && [ "$HASH" != "$EXPECTED_SHA256" ]; then
        die "o hash não confere com CACHYOS_AC_SHA256. Nada foi executado."
    fi
fi

# --- execução como root, com o teclado ligado --------------------------------
if [ "$(id -u)" -eq 0 ]; then
    python3 "$FILE" "$@" </dev/tty
else
    command -v sudo >/dev/null 2>&1 || die "sudo não encontrado; execute como root."
    say "O programa precisa de root: pedindo sudo..."
    sudo python3 "$FILE" "$@" </dev/tty
fi
