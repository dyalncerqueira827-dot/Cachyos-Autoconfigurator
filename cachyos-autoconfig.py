#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CachyOS Auto Configurator
=========================

Assistente de configuração pós-instalação para o CachyOS.

Princípios de projeto
---------------------
* Nada é alterado sem mostrar o plano e pedir confirmação (Sim / Não / Pular).
* Nunca assume que pacotes ou units existem: tudo é verificado antes.
* Arquivos importantes sempre recebem backup em
  /var/backups/cachyos-autoconfigurator/<timestamp>/ antes de serem alterados.
* Tudo é registrado em /var/log/cachyos-autoconfigurator.log (sem segredos).
* Nada de `curl | bash`, nada de repositórios desconhecidos, nada de `rm -rf`.
* Em sistemas que não sejam CachyOS o programa não faz alterações.

Uso:
    sudo python3 cachyos_autoconfigurator.py            # modo interativo
    sudo python3 cachyos_autoconfigurator.py --dry-run  # simula, sem alterar nada
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import pwd
import grp
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

APP_NAME = "CachyOS Auto Configurator"
APP_VERSION = "1.0.0"
LOG_FILE = Path("/var/log/cachyos-autoconfigurator.log")
BACKUP_ROOT = Path("/var/backups/cachyos-autoconfigurator")
PACMAN_LOCK = Path("/var/lib/pacman/db.lck")
FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"  # URL oficial do Flathub

# ---------------------------------------------------------------------------
# Listas de pacotes
# ---------------------------------------------------------------------------
BASIC_PKGS = ["base-devel", "git", "curl", "wget", "unzip", "zip", "tar",
              "rsync", "man-db", "man-pages"]
TERMINAL_PKGS = ["htop", "btop", "fastfetch", "eza", "bat", "fd", "ripgrep",
                 "fzf", "zoxide", "jq", "tree"]
DIAG_PKGS = ["htop", "btop", "fastfetch", "pciutils", "usbutils", "lsof",
             "pacman-contrib"]


# ===========================================================================
# Exceções de controle de fluxo
# ===========================================================================
class UserAbort(Exception):
    """O usuário interrompeu o programa (Ctrl+C / EOF)."""


class SkipModule(Exception):
    """O usuário escolheu 'Pular' - abandona o restante da seção atual."""


class Answer(Enum):
    YES = "sim"
    NO = "não"
    SKIP = "pular"


# ===========================================================================
# Log
# ===========================================================================
class Logger:
    """Grava no arquivo de log, removendo qualquer coisa que pareça segredo."""

    SECRET_RE = re.compile(
        r"(?i)\b(pass(?:word|wd)?|senha|token|secret|api[_-]?key)\b(\s*[=:]\s*|\s+)\S+")

    def __init__(self, path: Path):
        self.fh = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            self.fh = os.fdopen(fd, "a", encoding="utf-8")
        except OSError as exc:
            print(f"[WARN] Não foi possível abrir o log {path}: {exc}", file=sys.stderr)

    def write(self, level: str, msg: str) -> None:
        if not self.fh:
            return
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in str(msg).splitlines() or [""]:
            clean = self.SECRET_RE.sub(lambda m: f"{m.group(1)}=***", line)
            try:
                self.fh.write(f"{ts} [{level}] {clean}\n")
            except OSError:
                return
        try:
            self.fh.flush()
        except OSError:
            pass


LOG: Optional[Logger] = None  # definido em main()


def _log(level: str, msg: str) -> None:
    if LOG:
        LOG.write(level, msg)


# ===========================================================================
# Console (saída bonita + log)
# ===========================================================================
class Console:
    COLORS = {"INFO": "36", "OK": "32", "WARN": "33", "ERROR": "31", "SKIP": "35"}

    def __init__(self, color: bool):
        self.color = color

    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def tag(self, kind: str, msg: str) -> None:
        print(f"{self.c(self.COLORS[kind], f'[{kind}]')} {msg}")
        _log(kind, msg)

    def info(self, m: str) -> None: self.tag("INFO", m)
    def ok(self, m: str) -> None: self.tag("OK", m)
    def warn(self, m: str) -> None: self.tag("WARN", m)
    def error(self, m: str) -> None: self.tag("ERROR", m)
    def skip(self, m: str) -> None: self.tag("SKIP", m)

    def line(self, text: str = "") -> None:
        print(text)

    def dim(self, text: str) -> None:
        print(self.c("2", text))

    def bullet(self, text: str) -> None:
        print(f"   • {text}")

    def section(self, title: str) -> None:
        print()
        print(self.c("1;34", f"── {title} " + "─" * max(3, 52 - len(title))))
        _log("SECTION", title)

    def banner(self, title: str) -> None:
        bar = "=" * 40
        print()
        print(self.c("1;36", bar))
        print(self.c("1;36", title.center(40)))
        print(self.c("1;36", bar))
        _log("BANNER", title)


# ===========================================================================
# Execução de comandos
# ===========================================================================
@dataclass
class CmdResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def query(cmd: list[str], timeout: int = 30, env: Optional[dict] = None) -> CmdResult:
    """Executa um comando SOMENTE-LEITURA (também roda em --dry-run).

    Usa LC_ALL=C para que a saída seja previsível e fácil de interpretar.
    """
    full_env = os.environ.copy()
    full_env.update({"LC_ALL": "C", "LANG": "C"})
    if env:
        full_env.update(env)
    _log("QUERY", shlex.join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=full_env, errors="replace")
        return CmdResult(p.returncode, p.stdout, p.stderr)
    except FileNotFoundError:
        return CmdResult(127, "", f"comando não encontrado: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return CmdResult(124, "", "tempo esgotado")
    except OSError as exc:
        return CmdResult(126, "", str(exc))


def run_stream(cmd: list[str], console: Console) -> CmdResult:
    """Executa um comando que ALTERA o sistema, mostrando a saída em tempo real."""
    lines: list[str] = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, errors="replace")
    except FileNotFoundError:
        return CmdResult(127, "", f"comando não encontrado: {cmd[0]}")
    except OSError as exc:
        return CmdResult(126, "", str(exc))
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            console.dim(f"    {line}")
            _log("OUT", line)
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        raise
    return CmdResult(rc, "\n".join(lines), "")


def process_running(name: str) -> bool:
    """Procura um processo pelo nome (/proc/*/comm) - sem depender de pgrep."""
    try:
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    if Path(entry.path, "comm").read_text().strip() == name:
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def read_text(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return default


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def dir_size(path: Path) -> int:
    """Tamanho total (somente leitura) de um diretório, ignorando symlinks."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


# ===========================================================================
# Informações do sistema
# ===========================================================================
@dataclass
class SystemInfo:
    distro_name: str = "desconhecida"
    distro_id: str = ""
    distro_version: str = "rolling"
    is_cachyos: bool = False
    kernel: str = ""
    arch: str = ""
    cpu: str = "desconhecida"
    ram_gib: float = 0.0
    swap_gib: float = 0.0
    gpus: list[str] = field(default_factory=list)
    disks: list[str] = field(default_factory=list)
    root_fs: str = ""
    disk_free: str = ""
    desktop: str = "desconhecido"
    session: str = "desconhecida"
    has_systemd: bool = False
    network_manager: str = "nenhum detectado"
    pipewire: bool = False
    bluetooth_hw: bool = False
    is_laptop: bool = False
    enabled_services: list[str] = field(default_factory=list)
    cachy_kernels: list[str] = field(default_factory=list)
    free_bytes: int = 0
    total_bytes: int = 0


def parse_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in read_text("/etc/os-release").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def detect_gpus() -> list[str]:
    gpus: list[str] = []
    r = query(["lspci", "-nnk"])
    if r.ok:
        for block in re.split(r"\n\s*\n", r.stdout.strip()):
            lines = block.splitlines()
            if lines and re.search(r"VGA compatible|3D controller|Display controller", lines[0]):
                desc = lines[0].split(": ", 1)[1] if ": " in lines[0] else lines[0]
                drv = next((l.split(":", 1)[1].strip() for l in lines
                            if "Kernel driver in use" in l), "")
                gpus.append(desc + (f"  [driver: {drv}]" if drv else ""))
    if not gpus:  # fallback sem pciutils
        vendors = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}
        for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
            v = read_text(card / "device" / "vendor").strip()
            if v:
                gpus.append(f"{vendors.get(v, 'GPU ' + v)} ({card.name})")
    return gpus


def detect_disks() -> list[str]:
    out: list[str] = []
    r = query(["lsblk", "-J", "-d", "-b", "-o", "NAME,SIZE,TYPE,ROTA,MODEL,TRAN"])
    if not r.ok:
        return out
    try:
        for d in json.loads(r.stdout).get("blockdevices", []):
            name = d.get("name", "")
            if d.get("type") != "disk" or name.startswith(("zram", "loop", "ram")):
                continue
            rota = str(d.get("rota")).lower() in ("1", "true")
            kind = "NVMe" if name.startswith("nvme") else ("HDD" if rota else "SSD")
            model = (d.get("model") or "").strip() or "?"
            out.append(f"{name}: {model} - {fmt_bytes(int(d.get('size') or 0))} ({kind})")
    except (ValueError, TypeError):
        pass
    return out


DESKTOP_PROCS = {
    "plasmashell": "KDE Plasma", "gnome-shell": "GNOME", "xfce4-session": "Xfce",
    "cosmic-comp": "COSMIC", "Hyprland": "Hyprland", "sway": "Sway", "niri": "niri",
    "i3": "i3", "bspwm": "bspwm", "awesome": "awesome", "cinnamon": "Cinnamon",
    "mate-session": "MATE", "lxqt-session": "LXQt", "budgie-wm": "Budgie",
    "labwc": "labwc", "openbox": "Openbox", "qtile": "Qtile", "wayfire": "Wayfire",
}


def detect_desktop_session(user: Optional[str]) -> tuple[str, str]:
    """Descobre desktop e tipo de sessão (Wayland/X11) do usuário logado.

    Sob `sudo` as variáveis XDG_* costumam ser descartadas, então consultamos
    o logind e, como último recurso, os processos em execução.
    """
    desktop, session = "", ""
    r = query(["loginctl", "list-sessions", "--no-legend", "--no-pager"])
    for line in r.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        s = query(["loginctl", "show-session", parts[0], "-p", "Type", "-p",
                   "Desktop", "-p", "Active", "-p", "Name"])
        props = dict(l.split("=", 1) for l in s.stdout.splitlines() if "=" in l)
        if props.get("Type") in ("wayland", "x11", "mir") and props.get("Active") == "yes":
            if user is None or props.get("Name") == user:
                session = props["Type"]
                desktop = props.get("Desktop", "")
                break
    session = session or os.environ.get("XDG_SESSION_TYPE", "")
    desktop = desktop or os.environ.get("XDG_CURRENT_DESKTOP", "")
    if not desktop:
        try:
            for entry in os.scandir("/proc"):
                if entry.name.isdigit():
                    comm = read_text(Path(entry.path, "comm")).strip()
                    if comm in DESKTOP_PROCS:
                        desktop = DESKTOP_PROCS[comm]
                        break
        except OSError:
            pass
    return desktop or "não detectado", session or "não detectada (TTY/SSH?)"


def detect_laptop() -> bool:
    if any(Path("/sys/class/power_supply").glob("BAT*")):
        return True
    chassis = read_text("/sys/class/dmi/id/chassis_type").strip()
    return chassis in {"8", "9", "10", "11", "14", "30", "31", "32"}


def detect_bluetooth_hw() -> bool:
    if any(Path("/sys/class/bluetooth").glob("hci*")):
        return True
    r = query(["rfkill", "list", "bluetooth"])
    return r.ok and "Bluetooth" in r.stdout


# ===========================================================================
# Relatório (resumo final)
# ===========================================================================
@dataclass
class Report:
    changes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    reboot_needed: bool = False


# ===========================================================================
# Contexto: concentra helpers usados por todos os módulos
# ===========================================================================
class Ctx:
    def __init__(self, console: Console, dry_run: bool):
        self.console = console
        self.dry_run = dry_run
        self.auto_mode = False       # True = aceita automaticamente o que é "seguro"
        self.report = Report()
        self.info = SystemInfo()
        self._backup_dir: Optional[Path] = None
        self._target_user: Optional[pwd.struct_passwd] = None

    # ----- registro -------------------------------------------------------
    def changed(self, msg: str) -> None:
        msg = ("[simulado] " if self.dry_run else "") + msg
        self.report.changes.append(msg)
        _log("CHANGE", msg)

    def skipped(self, msg: str) -> None:
        self.report.skipped.append(msg)
        _log("SKIPPED", msg)

    def failed(self, msg: str) -> None:
        self.report.errors.append(msg)
        self.console.error(msg)

    # ----- interação ------------------------------------------------------
    def read(self, prompt: str) -> str:
        try:
            ans = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            raise UserAbort()
        return ans

    def ask(self, question: str, default: Answer = Answer.NO, safe: bool = True) -> Answer:
        """Pergunta Sim/Não/Pular.

        `safe=True` indica uma ação de baixo risco: no modo "aceitar todas as
        recomendações" ela é aprovada automaticamente. Ações com `safe=False`
        SEMPRE perguntam.
        """
        if self.auto_mode and safe:
            self.console.info(f"[AUTO] {question} → sim")
            _log("ANSWER", f"{question} => auto-sim")
            return Answer.YES
        hint = "[S/n/p]" if default is Answer.YES else "[s/N/p]"
        while True:
            raw = self.read(f"{self.console.c('1;36', '?')} {question} {hint} ").strip().lower()
            if raw == "":
                ans = default
            elif raw in ("s", "sim", "y", "yes"):
                ans = Answer.YES
            elif raw in ("n", "nao", "não", "no"):
                ans = Answer.NO
            elif raw in ("p", "pular", "skip"):
                ans = Answer.SKIP
            else:
                self.console.warn("Resposta inválida. Use s (sim), n (não) ou p (pular).")
                continue
            _log("ANSWER", f"{question} => {ans.value}")
            return ans

    def yes(self, question: str, default: Answer = Answer.NO, safe: bool = True) -> bool:
        """Retorna True/False; 'Pular' abandona o restante da seção atual."""
        ans = self.ask(question, default, safe)
        if ans is Answer.SKIP:
            raise SkipModule()
        return ans is Answer.YES

    def choose(self, title: str, options: list[str], allow_cancel: bool = True) -> Optional[int]:
        """Menu numerado simples. Retorna o índice escolhido ou None."""
        self.console.line(title)
        for i, opt in enumerate(options, 1):
            self.console.line(f"  [{i}] {opt}")
        if allow_cancel:
            self.console.line("  [0] Cancelar")
        while True:
            raw = self.read("Escolha: ").strip()
            _log("ANSWER", f"{title} => {raw}")
            if raw == "0" and allow_cancel:
                return None
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            self.console.warn("Opção inválida.")

    def prompt_text(self, label: str, validator: Callable[[str], bool],
                    current: str = "") -> str:
        """Lê texto livre com validação. Enter mantém o valor atual (se houver)."""
        suffix = f" [{current}]" if current else ""
        for _ in range(3):
            raw = self.read(f"{label}{suffix}: ").strip()
            val = raw or current
            _log("ANSWER", f"{label} => {val}")
            if not val:
                return ""
            if validator(val):
                return val
            self.console.warn("Valor inválido, tente novamente.")
        return ""

    # ----- comandos que alteram o sistema ---------------------------------
    def run(self, cmd: list[str]) -> CmdResult:
        printable = shlex.join(cmd)
        if self.dry_run:
            self.console.info(f"[DRY-RUN] {printable}")
            return CmdResult(0)
        self.console.info(f"Executando: {printable}")
        _log("CMD", printable)
        res = run_stream(cmd, self.console)
        _log("CMD", f"código de saída: {res.returncode}")
        return res

    def run_pacman(self, args: list[str]) -> CmdResult:
        """Executa pacman respeitando o lock do banco de dados."""
        if PACMAN_LOCK.exists():
            if process_running("pacman"):
                return CmdResult(1, "", "outro pacman está em execução")
            return CmdResult(1, "", f"lock obsoleto encontrado em {PACMAN_LOCK}; "
                                    "verifique manualmente antes de continuar")
        return self.run(["pacman", *args])

    # ----- backups e escrita de arquivos ----------------------------------
    def backup_dir(self) -> Path:
        if self._backup_dir is None:
            stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
            self._backup_dir = BACKUP_ROOT / stamp
            if not self.dry_run:
                BACKUP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._backup_dir

    def backup(self, path: str | Path) -> Optional[Path]:
        """Copia arquivo/diretório para o diretório de backup da execução."""
        src = Path(path)
        if not src.exists() and not src.is_symlink():
            return None
        dest = self.backup_dir() / str(src).lstrip("/")
        if self.dry_run:
            self.console.info(f"[DRY-RUN] backup de {src} → {dest}")
            return dest
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir() and not src.is_symlink():
                shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
            self.console.ok(f"Backup criado: {dest}")
            _log("BACKUP", f"{src} -> {dest}")
            self.report.backups.append(f"{src} → {dest}")
            return dest
        except OSError as exc:
            self.failed(f"Falha ao criar backup de {src}: {exc}")
            return None

    def write_file(self, path: str | Path, content: str, mode: int = 0o644) -> bool:
        """Escreve um arquivo de forma atômica, com backup do anterior."""
        p = Path(path)
        if p.exists() and read_text(p) == content:
            self.console.ok(f"{p} já possui o conteúdo desejado.")
            return True
        self.console.info(f"Arquivo a ser gravado: {p}")
        for l in content.rstrip().splitlines():
            self.console.dim(f"    | {l}")
        if self.dry_run:
            self.console.info("[DRY-RUN] arquivo não gravado.")
            return True
        if p.exists() and self.backup(p) is None:
            self.failed(f"Backup de {p} falhou; arquivo NÃO foi alterado.")
            return False
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".cachyos-ac-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.chmod(tmp, mode)
            os.replace(tmp, p)
            _log("WRITE", str(p))
            return True
        except OSError as exc:
            self.failed(f"Falha ao gravar {p}: {exc}")
            return False

    # ----- usuário alvo ---------------------------------------------------
    def target_user(self) -> Optional[pwd.struct_passwd]:
        """Usuário "real" (quem chamou sudo). Pergunta se não for possível saber."""
        if self._target_user:
            return self._target_user
        name = os.environ.get("SUDO_USER", "")
        if not name or name == "root":
            self.console.warn("Não foi possível identificar o usuário que chamou o sudo.")

            def valid(n: str) -> bool:
                try:
                    u = pwd.getpwnam(n)
                    return u.pw_uid >= 1000 and Path(u.pw_dir).is_dir()
                except KeyError:
                    return False
            name = self.prompt_text("Nome do usuário (não-root) a configurar", valid)
        try:
            u = pwd.getpwnam(name)
        except KeyError:
            return None
        if u.pw_uid == 0:
            return None
        self._target_user = u
        return u

    def as_user(self, user: pwd.struct_passwd, cmd: list[str]) -> list[str]:
        """Prefixa o comando para rodar como o usuário (nunca como root)."""
        return ["runuser", "-u", user.pw_name, "--", "env", f"HOME={user.pw_dir}", *cmd]

    # ----- pacotes --------------------------------------------------------
    def pkg_installed(self, name: str) -> bool:
        """`pacman -T` considera também pacotes "provides"."""
        return query(["pacman", "-T", name]).returncode == 0

    def repo_has(self, name: str) -> bool:
        return query(["pacman", "-Si", name]).ok

    def group_members(self, name: str) -> list[str]:
        r = query(["pacman", "-Sgq", name])
        return r.stdout.split() if r.ok else []

    def pkg_version(self, name: str) -> str:
        r = query(["pacman", "-Si", name])
        m = re.search(r"^Version\s*:\s*(.+)$", r.stdout, re.M)
        return m.group(1).strip() if m else "?"

    def sync_db_present(self) -> bool:
        return any(Path("/var/lib/pacman/sync").glob("*.db"))

    def install_packages(self, label: str, pkgs: Iterable[str], safe: bool = True) -> bool:
        """Fluxo padrão de instalação: verifica → mostra plano → confirma → instala."""
        self.console.info(f"{label}: verificando pacotes...")
        installed: list[str] = []
        to_install: list[str] = []
        missing: list[str] = []

        def classify(p: str, allow_group: bool = True) -> None:
            if self.pkg_installed(p):
                installed.append(p)
            elif self.repo_has(p):
                to_install.append(p)
            elif allow_group and (members := self.group_members(p)):
                for m in members:
                    classify(m, allow_group=False)
            else:
                missing.append(p)

        for p in dict.fromkeys(pkgs):
            classify(p)

        if installed:
            self.console.ok(f"Já instalados: {', '.join(installed)}")
        if missing:
            self.console.warn(f"Não encontrados nos repositórios (ignorados): {', '.join(missing)}")
            if not self.sync_db_present():
                self.console.warn("Bases do pacman não sincronizadas - execute a opção [1] (atualização).")
            self.skipped(f"{label}: indisponíveis: {', '.join(missing)}")
        if not to_install:
            if not missing:
                self.console.ok(f"{label}: nada a instalar.")
            return not missing

        self.console.info(f"Os seguintes pacotes serão instalados: {', '.join(to_install)}")
        prev = query(["pacman", "-S", "--needed", "--print", "--print-format",
                      "%n|%v|%s", *to_install], timeout=90)
        if prev.ok:
            total, rows = 0, []
            for l in prev.stdout.splitlines():
                parts = l.split("|")
                if len(parts) == 3 and parts[2].isdigit():
                    rows.append(f"{parts[0]} {parts[1]}")
                    total += int(parts[2])
            if rows:
                self.console.dim(f"    Total (com dependências): {len(rows)} pacote(s), "
                                 f"download ≈ {fmt_bytes(total)}")
                if len(rows) > len(to_install):
                    extra = [r for r in rows if r.split()[0] not in to_install]
                    self.console.dim(f"    Dependências adicionais: {', '.join(extra)}")
        else:
            self.console.warn("Não foi possível simular a instalação: "
                              + (prev.stderr.strip().splitlines() or ["erro desconhecido"])[0])

        if not self.yes("Continuar?", Answer.NO, safe=safe):
            self.skipped(f"{label}: instalação recusada pelo usuário")
            self.console.skip(f"{label}: não instalado.")
            return False

        res = self.run_pacman(["-S", "--needed", "--noconfirm", *to_install])
        if not res.ok:
            self.failed(f"{label}: falha ao instalar ({res.stderr or 'veja o log'}). "
                        "Se for erro 404, atualize o sistema (opção 1) e tente de novo.")
            return False
        self.console.ok(f"{label}: instalado com sucesso.")
        self.changed(f"Pacotes instalados ({label}): {', '.join(to_install)}")
        return True

    # ----- systemd --------------------------------------------------------
    def unit_exists(self, unit: str) -> bool:
        if not self.info.has_systemd:
            return False
        r = query(["systemctl", "list-unit-files", "--no-pager", "--no-legend", unit])
        return any(l.split() and l.split()[0] == unit for l in r.stdout.splitlines())

    def unit_state(self, unit: str) -> str:
        r = query(["systemctl", "is-enabled", unit])
        return (r.stdout.strip() or r.stderr.strip() or "unknown").splitlines()[0]

    def unit_enabled(self, unit: str) -> bool:
        return self.unit_state(unit) in ("enabled", "enabled-runtime")

    def unit_active(self, unit: str) -> bool:
        return query(["systemctl", "is-active", "--quiet", unit]).ok

    def enable_unit(self, unit: str, label: str = "", safe: bool = True) -> bool:
        """Habilita (e inicia) uma unit após todas as verificações."""
        label = label or unit
        if not self.unit_exists(unit):
            self.console.skip(f"{unit} não existe neste sistema.")
            self.skipped(f"{label}: unit {unit} inexistente")
            return False
        state = self.unit_state(unit)
        if state in ("masked", "masked-runtime"):
            self.console.warn(f"{unit} está mascarada (masked); não será alterada automaticamente.")
            self.skipped(f"{label}: {unit} mascarada")
            return False
        if self.unit_enabled(unit) and self.unit_active(unit):
            self.console.ok(f"{unit} já está habilitado e ativo.")
            return True
        self.console.info(f"Plano: systemctl enable --now {unit}")
        if not self.yes(f"Habilitar e iniciar {unit}?", Answer.YES, safe=safe):
            self.skipped(f"{label}: habilitação recusada")
            return False
        res = self.run(["systemctl", "enable", "--now", unit])
        if not res.ok:
            self.failed(f"Falha ao habilitar {unit}.")
            return False
        self.console.ok(f"{unit} habilitado.")
        self.changed(f"Serviço habilitado: {unit}")
        return True

    def disable_unit(self, unit: str) -> bool:
        res = self.run(["systemctl", "disable", "--now", unit])
        if res.ok:
            self.changed(f"Serviço desativado: {unit}")
            return True
        self.failed(f"Falha ao desativar {unit}.")
        return False


# ===========================================================================
# Detecção inicial
# ===========================================================================
def detect_system(ctx: Ctx) -> SystemInfo:
    i = SystemInfo()
    osr = parse_os_release()
    i.distro_id = osr.get("ID", "").lower()
    i.distro_name = osr.get("PRETTY_NAME") or osr.get("NAME") or "desconhecida"
    i.distro_version = osr.get("VERSION_ID") or osr.get("BUILD_ID") or "rolling"
    i.is_cachyos = (i.distro_id == "cachyos" or "cachyos" in osr.get("NAME", "").lower()
                    or "cachyos" in osr.get("ID_LIKE", "").lower())
    i.kernel = platform.release()
    i.arch = platform.machine()

    for line in read_text("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            i.cpu = f"{line.split(':', 1)[1].strip()} ({os.cpu_count()} threads)"
            break

    mem = read_text("/proc/meminfo")
    for key, attr in (("MemTotal", "ram_gib"), ("SwapTotal", "swap_gib")):
        m = re.search(rf"^{key}:\s+(\d+)\s+kB", mem, re.M)
        if m:
            setattr(i, attr, int(m.group(1)) / 1024 / 1024)

    i.gpus = detect_gpus()
    i.disks = detect_disks()
    i.root_fs = query(["findmnt", "-no", "FSTYPE", "/"]).stdout.strip()
    try:
        du = shutil.disk_usage("/")
        i.free_bytes, i.total_bytes = du.free, du.total
        i.disk_free = f"{fmt_bytes(du.free)} livres de {fmt_bytes(du.total)} em /"
    except OSError:
        pass

    i.has_systemd = Path("/run/systemd/system").is_dir()
    user = os.environ.get("SUDO_USER") or None
    i.desktop, i.session = detect_desktop_session(user)

    # Gerenciador de rede
    if i.has_systemd:
        for unit, label in (("NetworkManager.service", "NetworkManager"),
                            ("systemd-networkd.service", "systemd-networkd"),
                            ("iwd.service", "iwd")):
            if query(["systemctl", "is-active", "--quiet", unit]).ok:
                i.network_manager = label
                break

    i.pipewire = ctx.pkg_installed("pipewire") or process_running("pipewire")
    i.bluetooth_hw = detect_bluetooth_hw()
    i.is_laptop = detect_laptop()

    r = query(["systemctl", "list-unit-files", "--type=service", "--state=enabled",
               "--no-legend", "--no-pager"])
    i.enabled_services = sorted(l.split()[0] for l in r.stdout.splitlines() if l.strip())

    r = query(["pacman", "-Qq"])
    i.cachy_kernels = [p for p in r.stdout.split()
                       if re.fullmatch(r"linux-cachyos[a-z0-9-]*", p) and "headers" not in p]
    return i


def print_detection(ctx: Ctx) -> None:
    c, i = ctx.console, ctx.info
    c.section("Detecção do sistema")
    if i.is_cachyos:
        c.ok(f"CachyOS detectado ({i.distro_name}, versão {i.distro_version})")
    else:
        c.error(f"Este sistema NÃO parece ser CachyOS ({i.distro_name}).")
    c.ok(f"Kernel {i.kernel} / arquitetura {i.arch}")
    if i.cachy_kernels:
        c.ok(f"Kernel(s) CachyOS instalado(s): {', '.join(i.cachy_kernels)}")
    c.ok(f"CPU: {i.cpu}")
    c.ok(f"RAM: {i.ram_gib:.1f} GiB (swap/zram: {i.swap_gib:.1f} GiB)")
    for g in i.gpus or ["não detectada"]:
        c.ok(f"GPU: {g}")
    for d in i.disks:
        c.ok(f"Disco: {d}")
    if i.disk_free:
        (c.warn if i.total_bytes and i.free_bytes / i.total_bytes < 0.10 else c.ok)(
            f"Espaço: {i.disk_free} (fs: {i.root_fs or '?'})")
    c.ok(f"Desktop: {i.desktop} | Sessão: {i.session}")
    (c.ok if i.has_systemd else c.warn)("systemd detectado" if i.has_systemd else "systemd NÃO detectado")
    if i.network_manager == "NetworkManager":
        c.ok("NetworkManager detectado")
    else:
        c.info(f"Gerenciador de rede: {i.network_manager}")
    (c.ok if i.pipewire else c.skip)("PipeWire detectado" if i.pipewire else "PipeWire não encontrado")
    (c.ok if i.bluetooth_hw else c.skip)(
        "Hardware Bluetooth detectado" if i.bluetooth_hw else "Bluetooth não encontrado")
    c.info(f"Tipo de máquina: {'notebook' if i.is_laptop else 'desktop'}")
    c.info(f"Serviços habilitados: {len(i.enabled_services)}")
    if ctx.unit_exists("fstrim.timer") and ctx.unit_enabled("fstrim.timer"):
        c.ok("fstrim.timer já está habilitado")


def print_header(ctx: Ctx) -> None:
    c, i = ctx.console, ctx.info
    c.banner("CACHYOS AUTO CONFIGURATOR")
    c.line(f"Sistema: {i.distro_name}")
    c.line(f"Kernel:  {i.kernel}")
    c.line(f"Desktop: {i.desktop}")
    c.line(f"Sessão:  {i.session}")
    c.line(f"CPU:     {i.cpu}")
    c.line(f"RAM:     {i.ram_gib:.1f} GiB")
    c.line(f"GPU:     {'; '.join(g.split('  [')[0] for g in i.gpus) or 'não detectada'}")
    if ctx.dry_run:
        c.warn("MODO SIMULAÇÃO (--dry-run): nada será alterado.")


# ===========================================================================
# MÓDULO 1 - Atualização
# ===========================================================================
def scan_pacnew() -> list[str]:
    found: list[str] = []
    for root, _d, files in os.walk("/etc", onerror=lambda e: None):
        for f in files:
            if f.endswith((".pacnew", ".pacsave")):
                found.append(os.path.join(root, f))
    return found


def mod_update(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Atualização do sistema")
    if shutil.which("checkupdates"):
        r = query(["checkupdates"], timeout=120)
        pend = [l for l in r.stdout.splitlines() if l.strip()]
        if pend:
            c.info(f"{len(pend)} atualização(ões) pendente(s):")
            for l in pend[:25]:
                c.bullet(l)
            if len(pend) > 25:
                c.dim(f"   ... e mais {len(pend) - 25}")
        elif r.returncode == 2:
            c.ok("Nenhuma atualização pendente segundo o checkupdates.")
        else:
            c.warn("Não foi possível consultar atualizações pendentes.")
    else:
        c.info("(Instale 'pacman-contrib' para ver a lista de pendências antes de atualizar.)")

    c.info("Plano: pacman -Syu --noconfirm  (atualização completa; sem atualizações parciais)")
    if not ctx.yes("Atualizar o sistema antes de continuar?", Answer.YES):
        ctx.skipped("Atualização do sistema")
        return

    # Otimização opcional dos espelhos (ferramenta própria do CachyOS)
    if shutil.which("cachyos-rate-mirrors"):
        c.info("A ferramenta cachyos-rate-mirrors reordena as listas de espelhos.")
        if ctx.yes("Classificar espelhos agora? (opcional)", Answer.NO, safe=False):
            for f in ("/etc/pacman.d/mirrorlist", "/etc/pacman.d/cachyos-mirrorlist",
                      "/etc/pacman.d/cachyos-v3-mirrorlist", "/etc/pacman.d/cachyos-v4-mirrorlist"):
                ctx.backup(f)
            if ctx.run(["cachyos-rate-mirrors"]).ok:
                ctx.changed("Espelhos reordenados (cachyos-rate-mirrors)")
            else:
                ctx.failed("cachyos-rate-mirrors falhou (seguindo com os espelhos atuais).")

    res = ctx.run_pacman(["-Syu", "--noconfirm"])
    if not res.ok:
        ctx.failed(f"Atualização falhou: {res.stderr or 'veja o log'}")
        return
    c.ok("Sistema atualizado.")
    ctx.changed("Sistema atualizado (pacman -Syu)")
    # Heurística: se o diretório de módulos do kernel em uso sumiu, é preciso reiniciar.
    if not ctx.dry_run and not Path("/usr/lib/modules", platform.release()).exists():
        ctx.report.reboot_needed = True
        c.warn("O kernel em execução foi substituído: reinício recomendado.")
    pn = scan_pacnew()
    if pn:
        c.warn(f"{len(pn)} arquivo(s) .pacnew/.pacsave em /etc - revise (ex.: pacdiff):")
        for p in pn[:10]:
            c.bullet(p)


# ===========================================================================
# MÓDULOS 2 e 3 - Dependências e ferramentas de terminal
# ===========================================================================
def mod_basic_deps(ctx: Ctx) -> None:
    ctx.console.section("Dependências básicas")
    ctx.install_packages("Dependências básicas", BASIC_PKGS)


def mod_terminal_tools(ctx: Ctx) -> None:
    ctx.console.section("Ferramentas de terminal")
    ctx.install_packages("Ferramentas de terminal", TERMINAL_PKGS)
    ctx.console.dim("   Obs.: fzf/zoxide precisam ser ativados no seu shell; "
                    "os arquivos de configuração do shell NÃO são alterados.")


def mod_diagnostics(ctx: Ctx) -> None:
    ctx.console.section("Ferramentas de diagnóstico")
    ctx.install_packages("Ferramentas de diagnóstico", DIAG_PKGS)


# ===========================================================================
# MÓDULO 4 e 6-10 - Desenvolvimento
# ===========================================================================
def _dev_simple(ctx: Ctx, title: str, pkgs: list[str], ask: bool) -> None:
    ctx.console.section(title)
    if ask and not ctx.yes(f"{title}?", Answer.NO, safe=False):
        ctx.skipped(f"{title}: não solicitado")
        return
    ctx.install_packages(title, pkgs, safe=False)


def dev_python(ctx: Ctx, ask: bool = False) -> None:
    _dev_simple(ctx, "Python", ["python", "python-pip", "python-virtualenv"], ask)


def dev_node(ctx: Ctx, ask: bool = False) -> None:
    _dev_simple(ctx, "Node.js", ["nodejs", "npm"], ask)


def dev_go(ctx: Ctx, ask: bool = False) -> None:
    _dev_simple(ctx, "Go", ["go"], ask)


def dev_cpp(ctx: Ctx, ask: bool = False) -> None:
    _dev_simple(ctx, "GCC/Clang", ["gcc", "clang", "gdb"], ask)


def dev_build_tools(ctx: Ctx, ask: bool = True) -> None:
    for title, pkg in (("CMake", "cmake"), ("Ninja", "ninja"), ("Meson", "meson")):
        _dev_simple(ctx, title, [pkg], ask)


def dev_rust(ctx: Ctx, ask: bool = False) -> None:
    c = ctx.console
    c.section("Rust")
    if ask and not ctx.yes("Rust?", Answer.NO, safe=False):
        ctx.skipped("Rust: não solicitado")
        return
    for pkg in ("rustup", "rust"):  # os dois pacotes conflitam entre si
        if ctx.pkg_installed(pkg):
            c.ok(f"Rust já instalado via pacote '{pkg}'.")
            if pkg == "rustup":
                _rustup_toolchain(ctx)
            return
    idx = ctx.choose("Como instalar o Rust?",
                     ["rustup (recomendado: gerencia toolchains por usuário)",
                      "rust (pacote do sistema, versão estável única)"])
    if idx is None:
        ctx.skipped("Rust: cancelado")
        return
    if idx == 0:
        if ctx.install_packages("rustup", ["rustup"], safe=False):
            _rustup_toolchain(ctx)
    else:
        ctx.install_packages("rust", ["rust"], safe=False)


def _rustup_toolchain(ctx: Ctx) -> None:
    """Instala a toolchain stable para o usuário (nunca como root)."""
    user = ctx.target_user()
    if not user:
        ctx.skipped("Rust: toolchain não instalada (usuário alvo desconhecido)")
        return
    r = query(ctx.as_user(user, ["rustup", "toolchain", "list"]))
    if r.ok and "stable" in r.stdout:
        ctx.console.ok(f"Toolchain stable já configurada para {user.pw_name}.")
        return
    ctx.console.info("Plano: rustup default stable (baixa a toolchain dos servidores oficiais da Rust).")
    if ctx.yes(f"Instalar a toolchain stable para o usuário {user.pw_name}?", Answer.YES, safe=False):
        if ctx.run(ctx.as_user(user, ["rustup", "default", "stable"])).ok:
            ctx.changed(f"Toolchain Rust stable instalada para {user.pw_name}")
        else:
            ctx.failed("Falha ao instalar a toolchain stable do Rust.")


def dev_java(ctx: Ctx, ask: bool = False) -> None:
    c = ctx.console
    c.section("Java")
    if ask and not ctx.yes("Java?", Answer.NO, safe=False):
        ctx.skipped("Java: não solicitado")
        return
    inst = [p for p in query(["pacman", "-Qq"]).stdout.split()
            if re.fullmatch(r"(jdk|jre)[0-9]*-openjdk(-headless)?", p)]
    if inst:
        c.ok(f"Java já instalado: {', '.join(inst)}")
        if not ctx.yes("Instalar outra versão do Java?", Answer.NO, safe=False):
            return
    def jdk_order(n: str) -> int:
        m = re.search(r"\d+", n)
        return int(m.group()) if m else 999  # "jdk-openjdk" (sem número) = mais recente

    cands = sorted(set(query(["pacman", "-Ssq", r"^jdk[0-9]*-openjdk$"]).stdout.split()), key=jdk_order)
    if not cands:
        c.skip("Nenhum JDK OpenJDK encontrado nos repositórios.")
        ctx.skipped("Java: nenhum JDK disponível")
        return
    opts = [f"{n}  (versão {ctx.pkg_version(n)})" for n in cands]
    idx = ctx.choose("Versões de JDK disponíveis:", opts)
    if idx is None:
        ctx.skipped("Java: cancelado")
        return
    if ctx.install_packages(f"Java ({cands[idx]})", [cands[idx]], safe=False):
        c.dim("   Para alternar versões: archlinux-java status / archlinux-java set <ambiente>")


def dev_containers(ctx: Ctx, ask: bool = False) -> None:
    c = ctx.console
    c.section("Docker / Podman")
    if ask and not ctx.yes("Docker/Podman?", Answer.NO, safe=False):
        ctx.skipped("Containers: não solicitado")
        return
    have = [p for p in ("docker", "podman") if ctx.pkg_installed(p)]
    if have:
        c.ok(f"Já instalado: {', '.join(have)}")
    idx = ctx.choose("Qual engine de containers?",
                     ["Podman (rootless, sem daemon)", "Docker (daemon root + grupo docker)"])
    if idx is None:
        ctx.skipped("Containers: cancelado")
        return
    user = ctx.target_user()
    if idx == 0:
        if not ctx.install_packages("Podman", ["podman"], safe=False) and not ctx.pkg_installed("podman"):
            return
        _podman_subids(ctx, user)
    else:
        if not ctx.install_packages("Docker", ["docker", "docker-compose"], safe=False) \
                and not ctx.pkg_installed("docker"):
            return
        ctx.enable_unit("docker.service", "Docker", safe=False)
        in_group = _group_exists("docker") and bool(user) and user.pw_name in grp.getgrnam("docker").gr_mem
        if user and _group_exists("docker") and not in_group:
            c.warn("Estar no grupo 'docker' equivale a ter acesso root ao sistema.")
            if ctx.yes(f"Adicionar {user.pw_name} ao grupo docker?", Answer.NO, safe=False):
                if ctx.run(["usermod", "-aG", "docker", user.pw_name]).ok:
                    ctx.changed(f"{user.pw_name} adicionado ao grupo docker (relogin necessário)")
                    ctx.report.reboot_needed = True


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _podman_subids(ctx: Ctx, user: Optional[pwd.struct_passwd]) -> None:
    """Podman rootless precisa de intervalos subuid/subgid para o usuário."""
    if not user:
        return
    entries = read_text("/etc/subuid").splitlines()
    if any(l.split(":")[0] == user.pw_name for l in entries):
        ctx.console.ok(f"{user.pw_name} já possui intervalo subuid/subgid.")
        return
    start = 100000
    for l in entries:
        p = l.split(":")
        if len(p) == 3 and p[1].isdigit() and p[2].isdigit():
            start = max(start, int(p[1]) + int(p[2]))
    rng = f"{start}-{start + 65535}"
    ctx.console.info(f"Plano: usermod --add-subuids {rng} --add-subgids {rng} {user.pw_name}")
    if ctx.yes("Adicionar intervalo subuid/subgid (necessário ao Podman rootless)?", Answer.YES, safe=False):
        ctx.backup("/etc/subuid")
        ctx.backup("/etc/subgid")
        if ctx.run(["usermod", "--add-subuids", rng, "--add-subgids", rng, user.pw_name]).ok:
            ctx.changed(f"Intervalo subuid/subgid {rng} adicionado a {user.pw_name}")
        else:
            ctx.failed("Falha ao configurar subuid/subgid.")


def mod_dev(ctx: Ctx) -> None:
    """Categoria completa: pergunta individualmente cada ferramenta."""
    ctx.console.section("Ambiente de desenvolvimento")
    ctx.console.dim("   (p = pular o restante desta categoria)")
    dev_python(ctx, True)
    dev_rust(ctx, True)
    dev_node(ctx, True)
    dev_java(ctx, True)
    dev_go(ctx, True)
    dev_cpp(ctx, True)
    dev_build_tools(ctx, True)
    dev_containers(ctx, True)


def mod_compilers(ctx: Ctx) -> None:
    dev_cpp(ctx, False)
    dev_build_tools(ctx, True)


# ===========================================================================
# MÓDULO 5 - Git
# ===========================================================================
def mod_git(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Git")
    if not ctx.yes("Configurar Git?", Answer.NO, safe=False):
        ctx.skipped("Git: não solicitado")
        return
    if not ctx.pkg_installed("git") and not ctx.install_packages("Git", ["git"], safe=False):
        return
    user = ctx.target_user()
    if not user:
        ctx.skipped("Git: usuário alvo desconhecido")
        return

    def get(key: str) -> str:
        return query(ctx.as_user(user, ["git", "config", "--global", "--get", key])).stdout.strip()

    cur_name, cur_mail = get("user.name"), get("user.email")
    name = ctx.prompt_text("Nome", lambda s: 0 < len(s) <= 100 and not any(ord(ch) < 32 for ch in s), cur_name)
    mail = ctx.prompt_text("Email", lambda s: bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s)), cur_mail)
    todo = [(k, v, cur) for k, v, cur in (("user.name", name, cur_name), ("user.email", mail, cur_mail))
            if v and v != cur]
    if not todo:
        c.ok("Nenhuma alteração de Git necessária.")
        return
    c.info("Serão definidos (git config --global) para o usuário " + user.pw_name + ":")
    for k, v, _ in todo:
        c.bullet(f"{k} = {v}")
    if not ctx.yes("Continuar?", Answer.NO, safe=False):
        ctx.skipped("Git: recusado")
        return
    ctx.backup(Path(user.pw_dir) / ".gitconfig")
    for k, v, _ in todo:
        if ctx.run(ctx.as_user(user, ["git", "config", "--global", k, v])).ok:
            ctx.changed(f"Git: {k} definido para {user.pw_name}")
        else:
            ctx.failed(f"Falha ao definir {k}")


# ===========================================================================
# MÓDULO 11 - Flatpak
# ===========================================================================
def mod_flatpak(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Flatpak")
    if not ctx.pkg_installed("flatpak"):
        c.info("Flatpak não está instalado.")
        if not ctx.yes("Flatpak não está instalado. Deseja instalar?", Answer.YES):
            ctx.skipped("Flatpak: não instalado")
            return
        if not ctx.install_packages("Flatpak", ["flatpak"]):
            return
    else:
        c.ok("Flatpak já instalado.")
    if ctx.dry_run and not shutil.which("flatpak"):
        c.info("[DRY-RUN] configuraria o remoto Flathub após instalar o flatpak.")
        return
    have = query(["flatpak", "remotes", "--system", "--columns=name"]).stdout.split()
    user_have = query(["flatpak", "remotes", "--user", "--columns=name"]).stdout.split()
    if "flathub" in have:
        c.ok("Flathub já está configurado (sistema).")
        return
    if "flathub" in user_have:
        c.ok("Flathub já está configurado (somente nível do usuário).")
    c.info(f"Plano: flatpak remote-add --if-not-exists --system flathub {FLATHUB_URL}")
    c.dim("   (somente o repositório oficial do Flathub; nenhum outro será adicionado)")
    if not ctx.yes("Configurar o Flathub no sistema?", Answer.YES):
        ctx.skipped("Flathub: recusado")
        return
    if ctx.run(["flatpak", "remote-add", "--if-not-exists", "--system", "flathub", FLATHUB_URL]).ok:
        ctx.changed("Remoto Flathub configurado")
    else:
        ctx.failed("Falha ao configurar o Flathub.")


# ===========================================================================
# MÓDULOS 12-15 - Serviços (Bluetooth, impressão, TRIM, horário)
# ===========================================================================
def mod_bluetooth(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Bluetooth")
    if not ctx.info.bluetooth_hw:
        c.skip("Bluetooth não detectado. Pulando.")
        ctx.skipped("Bluetooth: hardware não detectado")
        return
    c.ok("Hardware Bluetooth detectado.")
    if not ctx.pkg_installed("bluez"):
        if not ctx.install_packages("Bluetooth (bluez)", ["bluez", "bluez-utils"]):
            return
    if not ctx.unit_exists("bluetooth.service"):
        c.skip("bluetooth.service não existe.")
        ctx.skipped("Bluetooth: unit inexistente")
        return
    ctx.enable_unit("bluetooth.service", "Bluetooth")


def mod_printing(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Impressão (CUPS)")
    installed = ctx.pkg_installed("cups")
    if installed and (ctx.unit_enabled("cups.socket") or ctx.unit_enabled("cups.service")):
        c.ok("CUPS já está instalado e habilitado.")
        return
    if not installed:
        c.skip("Impressão não configurada.")
    if not ctx.yes("Você utiliza impressora?", Answer.NO, safe=False):
        ctx.skipped("Impressão: não utilizada")
        return
    if not installed and not ctx.install_packages("CUPS", ["cups", "cups-filters", "ghostscript"], safe=False):
        return
    # cups.socket = inicia o CUPS sob demanda (preferível a deixar o daemon sempre ativo)
    unit = "cups.socket" if ctx.unit_exists("cups.socket") else "cups.service"
    ctx.enable_unit(unit, "CUPS", safe=False)


def disk_supports_trim() -> bool:
    r = query(["lsblk", "-D", "-d", "-n", "-b", "-o", "NAME,DISC-MAX"])
    for l in r.stdout.splitlines():
        p = l.split()
        if len(p) == 2 and p[1].isdigit() and int(p[1]) > 0:
            return True
    return False


def mod_trim(ctx: Ctx) -> None:
    c = ctx.console
    c.section("TRIM (fstrim.timer)")
    if not ctx.unit_exists("fstrim.timer"):
        c.skip("fstrim.timer não existe (util-linux ausente?).")
        ctx.skipped("TRIM: unit inexistente")
        return
    if ctx.unit_enabled("fstrim.timer"):
        c.ok("TRIM já está configurado.")
        if not ctx.unit_active("fstrim.timer"):
            ctx.enable_unit("fstrim.timer", "TRIM")
        return
    if not disk_supports_trim():
        c.warn("Nenhum disco anuncia suporte a TRIM (HDD, VM ou controlador sem discard).")
        if not ctx.yes("Habilitar mesmo assim?", Answer.NO, safe=False):
            ctx.skipped("TRIM: sem suporte detectado")
            return
    c.dim("   Obs.: em volumes LUKS o TRIM só alcança o disco se 'allow_discards' estiver ativo.")
    ctx.enable_unit("fstrim.timer", "TRIM")


def mod_timesync(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Sincronização de horário")
    others = [u for u in ("chronyd.service", "ntpd.service", "openntpd.service", "ntpd-rs.service")
              if ctx.unit_exists(u) and (ctx.unit_enabled(u) or ctx.unit_active(u))]
    if others:
        c.ok(f"Outro serviço de horário em uso: {', '.join(others)}. Nenhuma alteração "
             "(evita conflito).")
        ctx.skipped(f"Horário: já gerenciado por {', '.join(others)}")
        return
    unit = "systemd-timesyncd.service"
    if not ctx.unit_exists(unit):
        c.skip(f"{unit} não existe.")
        ctx.skipped("Horário: systemd-timesyncd inexistente")
        return
    ntp = query(["timedatectl", "show", "-p", "NTP", "--value"]).stdout.strip()
    if ctx.unit_enabled(unit) and ctx.unit_active(unit) and ntp == "yes":
        c.ok("Sincronização de horário já está configurada (systemd-timesyncd).")
        return
    if ctx.enable_unit(unit, "Horário"):
        ctx.run(["timedatectl", "set-ntp", "true"])


# ===========================================================================
# MÓDULO 16 - Otimizações do systemd
# ===========================================================================
def find_setting(key: str, paths: list[str]) -> Optional[tuple[str, str]]:
    rx = re.compile(rf"^\s*{key}\s*=\s*(.+?)\s*$")
    for pattern in paths:
        p = Path(pattern)
        files = sorted(p.parent.glob(p.name)) if "*" in p.name else [p]
        for f in files:
            for line in read_text(f).splitlines():
                m = rx.match(line)
                if m:
                    return str(f), m.group(1)
    return None


SYSTEMD_TWEAKS = [
    dict(key="DefaultTimeoutStopSec", value="15s", section="Manager",
         file="/etc/systemd/system.conf.d/50-cachyos-autoconfigurator.conf",
         search=["/etc/systemd/system.conf", "/etc/systemd/system.conf.d/*.conf",
                 "/usr/lib/systemd/system.conf.d/*.conf"],
         desc="Reduz o tempo máximo de espera ao parar serviços (padrão 90s) → desligamento mais rápido."),
    dict(key="SystemMaxUse", value="500M", section="Journal",
         file="/etc/systemd/journald.conf.d/50-cachyos-autoconfigurator.conf",
         search=["/etc/systemd/journald.conf", "/etc/systemd/journald.conf.d/*.conf",
                 "/usr/lib/systemd/journald.conf.d/*.conf"],
         desc="Limita o journal persistente a 500 MiB para não crescer indefinidamente."),
]


def mod_systemd_opt(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Otimizações do systemd")
    if not ctx.info.has_systemd:
        c.skip("systemd não detectado.")
        return
    r = query(["systemd-analyze"])
    if r.ok and r.stdout.strip():
        c.info(r.stdout.strip().splitlines()[0])
    failed = [l for l in query(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"]).stdout.splitlines() if l.strip()]
    if failed:
        c.warn("Units com falha:")
        for l in failed:
            c.bullet(l.split()[0])
    else:
        c.ok("Nenhuma unit com falha.")
    for t in SYSTEMD_TWEAKS:
        found = find_setting(t["key"], t["search"])
        if found and not (found[0] == t["file"] and found[1] == t["value"]):
            c.ok(f"{t['key']} já definido em {found[0]} ({found[1]}). Nenhuma alteração.")
            continue
        if found:
            c.ok(f"{t['key']}={t['value']} já configurado.")
            continue
        c.info(f"{t['key']}={t['value']}: {t['desc']}")
        if ctx.yes(f"Aplicar {t['key']}={t['value']}?", Answer.YES):
            content = f"# Criado por {APP_NAME}\n[{t['section']}]\n{t['key']}={t['value']}\n"
            if ctx.write_file(t["file"], content):
                ctx.changed(f"systemd: {t['key']}={t['value']} ({t['file']})")
                ctx.report.reboot_needed = True
        else:
            ctx.skipped(f"systemd: {t['key']}")


# ===========================================================================
# MÓDULO 17 - Serviços do usuário
# ===========================================================================
def mod_user_services(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Serviços do usuário")
    user = ctx.target_user()
    if user:
        uid = user.pw_uid
        r = query(ctx.as_user(user, ["env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                                     f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                                     "systemctl", "--user", "list-unit-files", "--state=enabled",
                                     "--no-legend", "--no-pager"]))
        names = [l.split()[0] for l in r.stdout.splitlines() if l.strip()]
        if names:
            c.info(f"Units de usuário habilitadas ({user.pw_name}): {', '.join(names[:12])}"
                   + (" ..." if len(names) > 12 else ""))
    # --- Pilha de áudio PipeWire ---
    if ctx.pkg_installed("pulseaudio") and ctx.pkg_installed("pipewire-pulse"):
        c.warn("PulseAudio e pipewire-pulse estão ambos instalados (possível conflito).")
    stack = [u for u in ("pipewire.socket", "pipewire-pulse.socket", "wireplumber.service")
             if query(["systemctl", "--global", "cat", u]).ok]
    if not ctx.pkg_installed("pipewire"):
        c.skip("PipeWire não instalado; nada a fazer em serviços de áudio do usuário.")
        return
    pending = [u for u in stack if query(["systemctl", "--global", "is-enabled", u]).stdout.strip()
               not in ("enabled", "static", "indirect")]
    if not pending:
        c.ok("PipeWire detectado. Nenhuma alteração necessária.")
        return
    c.info(f"Serviços PipeWire não habilitados globalmente: {', '.join(pending)}")
    c.info(f"Plano: systemctl --global enable {' '.join(pending)}")
    if ctx.yes("Habilitar para todos os usuários?", Answer.YES):
        if ctx.run(["systemctl", "--global", "enable", *pending]).ok:
            ctx.changed(f"Units de usuário habilitadas globalmente: {', '.join(pending)}")
            ctx.report.reboot_needed = True
        else:
            ctx.failed("Falha ao habilitar serviços de usuário.")


# ===========================================================================
# MÓDULO 18 - Energia
# ===========================================================================
POWER_UNITS = ["power-profiles-daemon.service", "tlp.service", "tuned.service",
               "tuned-ppd.service", "auto-cpufreq.service"]


def mod_power(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Configuração de energia")
    if not ctx.info.is_laptop:
        c.skip("Desktop detectado: gerenciamento de energia de notebook não é necessário.")
        ctx.skipped("Energia: não é notebook")
        return
    c.ok("Notebook detectado.")
    active = [u for u in POWER_UNITS if ctx.unit_exists(u) and (ctx.unit_active(u) or ctx.unit_enabled(u))]
    c.info("Gerenciador atual: " + (", ".join(u.removesuffix(".service") for u in active) or "nenhum"))
    if len(active) > 1:
        c.warn("Mais de um gerenciador de energia ativo - isso pode causar conflitos!")
    idx = ctx.choose("O que deseja fazer?",
                     ["Manter atual", "Configurar alternativa", "Não alterar"], allow_cancel=False)
    if idx in (0, 2):
        ctx.skipped("Energia: mantido/sem alterações")
        return
    options = [("power-profiles-daemon", "power-profiles-daemon.service"),
               ("tlp", "tlp.service")]
    options = [o for o in options if o[1] not in active]
    if not options:
        c.ok("Os gerenciadores suportados já estão ativos.")
        return
    sel = ctx.choose("Alternativa:", [o[0] for o in options])
    if sel is None:
        return
    pkg, unit = options[sel]
    plan = [f"instalar {pkg} (se necessário)"]
    plan += [f"desativar {u}" for u in active]
    plan.append(f"habilitar {unit}")
    if pkg == "tlp":
        plan.append("mascarar systemd-rfkill.service/.socket (recomendado pelo TLP)")
    c.info("Plano: " + "; ".join(plan))
    if not ctx.yes("Continuar?", Answer.NO, safe=False):
        ctx.skipped("Energia: recusado")
        return
    if not ctx.install_packages(pkg, [pkg], safe=False) and not ctx.pkg_installed(pkg):
        return
    disabled: list[str] = []
    for u in active:  # nunca deixar dois gerenciadores ativos ao mesmo tempo
        if ctx.disable_unit(u):
            disabled.append(u)
    if pkg == "tlp":
        ctx.run(["systemctl", "mask", "systemd-rfkill.service", "systemd-rfkill.socket"])
    if not ctx.enable_unit(unit, pkg, safe=False):
        c.warn("Falha ao ativar o novo gerenciador; restaurando o anterior.")
        for u in disabled:
            ctx.run(["systemctl", "enable", "--now", u])
    else:
        c.dim("   Pacotes antigos permanecem instalados, apenas desativados.")


# ===========================================================================
# MÓDULO 19 - Limpeza
# ===========================================================================
def _paccache(ctx: Ctx, args_dry: list[str], args_run: list[str], title: str, safe: bool) -> None:
    c = ctx.console
    dry = query(["paccache", *args_dry], timeout=120)
    lines = [l for l in dry.stdout.splitlines() if l.strip()]
    c.info(f"{title} - simulação:")
    for l in lines[:25]:
        c.dim(f"    {l}")
    if len(lines) > 25:
        c.dim(f"    ... (+{len(lines) - 25} linhas)")
    if not lines or "no candidate" in dry.stdout.lower():
        c.ok("Nada a remover.")
        return
    if ctx.yes(f"Executar: paccache {' '.join(args_run)} ?", Answer.YES, safe=safe):
        if ctx.run(["paccache", *args_run]).ok:
            ctx.changed(f"Limpeza do cache do pacman: {title}")
        else:
            ctx.failed(f"paccache falhou ({title}).")


def cleanup_pacman_cache(ctx: Ctx) -> None:
    c = ctx.console
    cache = Path("/var/cache/pacman/pkg")
    c.info(f"Cache do pacman: {fmt_bytes(dir_size(cache))} em {cache}")
    if not shutil.which("paccache"):
        if not ctx.install_packages("pacman-contrib (paccache)", ["pacman-contrib"]):
            return
        if not shutil.which("paccache") and not ctx.dry_run:
            return
    _paccache(ctx, ["-d", "-k3"], ["-r", "-k3"], "versões antigas (mantém as 3 mais recentes)", True)
    _paccache(ctx, ["-d", "-u", "-k0"], ["-r", "-u", "-k0"], "pacotes já desinstalados", True)


def cleanup_orphans(ctx: Ctx) -> None:
    c = ctx.console
    r = query(["pacman", "-Qdtq"])
    orphans = r.stdout.split()
    if not orphans:
        c.ok("Nenhum pacote órfão encontrado.")
        return
    c.warn(f"{len(orphans)} pacote(s) órfão(s) (instalados como dependência e sem uso):")
    prev = query(["pacman", "-Rns", "--print", "--print-format", "%n %v", *orphans], timeout=90)
    targets = prev.stdout.splitlines() if prev.ok else orphans
    for t in targets:
        c.bullet(t)
    c.dim("   Revise a lista: ela mostra EXATAMENTE o que será removido.")
    if ctx.yes("Remover estes pacotes? (pacman -Rns)", Answer.NO, safe=False):
        if ctx.run_pacman(["-Rns", "--noconfirm", *orphans]).ok:
            ctx.changed(f"Pacotes órfãos removidos: {', '.join(orphans)}")
        else:
            ctx.failed("Falha ao remover órfãos.")
    else:
        ctx.skipped("Remoção de órfãos recusada")


def cleanup_old_caches(ctx: Ctx) -> None:
    c = ctx.console
    # 1) Journal: apenas entradas com mais de 30 dias
    du = query(["journalctl", "--disk-usage"]).stdout.strip()
    if du:
        c.info(du)
    if ctx.yes("Remover entradas do journal com mais de 30 dias?", Answer.NO, safe=False):
        if ctx.run(["journalctl", "--vacuum-time=30d"]).ok:
            ctx.changed("Journal: entradas > 30 dias removidas")
    # 2) Coredumps do sistema com mais de 30 dias (arquivos individuais)
    cd = Path("/var/lib/systemd/coredump")
    old = []
    if cd.is_dir():
        limit = dt.datetime.now().timestamp() - 30 * 86400
        for f in cd.iterdir():
            try:
                st = f.lstat()
                if f.is_file() and not f.is_symlink() and st.st_mtime < limit:
                    old.append((f, st.st_size))
            except OSError:
                pass
    if old:
        c.info(f"{len(old)} coredump(s) antigo(s) ({fmt_bytes(sum(s for _, s in old))}):")
        for f, s in old[:10]:
            c.bullet(f"{f.name} ({fmt_bytes(s)})")
        if ctx.yes("Remover estes coredumps?", Answer.NO, safe=False):
            n = 0
            for f, _ in old:
                if ctx.dry_run:
                    n += 1
                    continue
                try:
                    f.unlink()
                    n += 1
                except OSError as exc:
                    ctx.failed(f"Não foi possível remover {f}: {exc}")
            ctx.changed(f"{n} coredump(s) antigo(s) removido(s)")
    # 3) Downloads parciais do pacman (*.part) - apenas arquivos
    parts = [f for f in Path("/var/cache/pacman/pkg").glob("*.part") if f.is_file() and not f.is_symlink()]
    if parts:
        c.info(f"{len(parts)} download(s) parcial(is) do pacman (*.part):")
        for f in parts[:10]:
            c.bullet(f.name)
        if ctx.yes("Remover estes arquivos parciais?", Answer.NO, safe=False):
            for f in parts:
                if not ctx.dry_run:
                    try:
                        f.unlink()
                    except OSError as exc:
                        ctx.failed(f"Falha ao remover {f}: {exc}")
            ctx.changed(f"{len(parts)} download(s) parcial(is) removido(s)")
    # 4) Caches do usuário: SOMENTE informativo
    user = ctx.target_user() if not ctx.auto_mode else None
    if user:
        cache = Path(user.pw_dir) / ".cache"
        if cache.is_dir():
            sizes = []
            for d in cache.iterdir():
                if d.is_dir() and not d.is_symlink():
                    sizes.append((dir_size(d), d.name))
            sizes.sort(reverse=True)
            c.info(f"Maiores itens em {cache} (NÃO removidos automaticamente):")
            for s, n in sizes[:5]:
                c.bullet(f"{n}: {fmt_bytes(s)}")


def cleanup_disk_check(ctx: Ctx) -> None:
    c = ctx.console
    real = {"ext4", "btrfs", "xfs", "f2fs", "vfat", "ntfs3", "ntfs", "exfat", "zfs"}
    seen = set()
    for line in read_text("/proc/mounts").splitlines():
        p = line.split()
        if len(p) >= 3 and p[2] in real and p[1] not in seen:
            seen.add(p[1])
            try:
                du = shutil.disk_usage(p[1])
            except OSError:
                continue
            pct = du.used / du.total * 100 if du.total else 0
            msg = f"{p[1]}: {fmt_bytes(du.free)} livres de {fmt_bytes(du.total)} ({pct:.0f}% usado) [{p[2]}]"
            (c.warn if pct >= 90 else c.ok)(msg)


def mod_cleanup(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Limpeza do sistema")
    c.dim("   Arquivos pessoais NUNCA são removidos.")
    cleanup_disk_check(ctx)
    if ctx.yes("Limpar o cache do pacman?", Answer.YES):
        cleanup_pacman_cache(ctx)
    if ctx.yes("Verificar pacotes órfãos?", Answer.YES, safe=False):
        cleanup_orphans(ctx)
    if ctx.yes("Verificar caches antigos (journal, coredumps, downloads parciais)?", Answer.NO, safe=False):
        cleanup_old_caches(ctx)


def mod_maintenance_basic(ctx: Ctx) -> None:
    """Manutenção segura usada pelo modo recomendado (sem remoções arriscadas)."""
    c = ctx.console
    c.section("Manutenção básica")
    cleanup_disk_check(ctx)
    if ctx.yes("Limpar o cache do pacman (mantém 3 versões)?", Answer.YES):
        cleanup_pacman_cache(ctx)
    orphans = query(["pacman", "-Qdtq"]).stdout.split()
    if orphans:
        c.info(f"{len(orphans)} pacote(s) órfão(s) encontrado(s). Use a opção [19] para revisá-los.")


# ===========================================================================
# MÓDULO 20 - Segurança
# ===========================================================================
RISKY_PORTS = {21: "FTP", 23: "Telnet", 139: "SMB", 445: "SMB", 2375: "Docker API sem TLS",
               3306: "MySQL", 3389: "RDP", 5432: "PostgreSQL", 5900: "VNC",
               6379: "Redis", 27017: "MongoDB"}


def sec_firewall(ctx: Ctx) -> None:
    c = ctx.console
    found: list[str] = []
    if shutil.which("ufw") and "Status: active" in query(["ufw", "status"]).stdout:
        found.append("ufw")
    if ctx.unit_exists("firewalld.service") and ctx.unit_active("firewalld.service"):
        found.append("firewalld")
    if shutil.which("nft"):
        rules = [l for l in query(["nft", "list", "ruleset"]).stdout.splitlines() if l.strip()]
        if len(rules) > 3:
            found.append("nftables (regras presentes)")
    if found:
        c.ok(f"Firewall ativo: {', '.join(found)}")
        return
    c.warn("Nenhum firewall ativo detectado.")
    if not ctx.yes("Instalar e ativar o UFW (bloqueia conexões de entrada, permite saída)?",
                   Answer.NO, safe=False):
        ctx.skipped("Firewall: não configurado")
        return
    ssh_port = None
    if ctx.unit_exists("sshd.service") and (ctx.unit_active("sshd.service") or ctx.unit_enabled("sshd.service")):
        port = _ssh_effective().get("port", "22")
        c.warn("O servidor SSH está ativo; sem uma regra, você perderia o acesso remoto.")
        if ctx.yes(f"Permitir a porta SSH {port}/tcp?", Answer.YES, safe=False):
            ssh_port = port
    if not ctx.install_packages("UFW", ["ufw"], safe=False) and not ctx.pkg_installed("ufw"):
        return
    ctx.backup("/etc/ufw")
    cmds = [["ufw", "default", "deny", "incoming"], ["ufw", "default", "allow", "outgoing"]]
    if ssh_port:
        cmds.append(["ufw", "allow", f"{ssh_port}/tcp"])
    cmds.append(["ufw", "--force", "enable"])
    for cmd in cmds:
        if not ctx.run(cmd).ok:
            ctx.failed(f"UFW: falha em: {shlex.join(cmd)}")
            return
    ctx.run(["systemctl", "enable", "ufw.service"])
    ctx.changed("UFW configurado e ativado (entrada negada por padrão)")


def sec_ports(ctx: Ctx) -> None:
    c = ctx.console
    r = query(["ss", "-H", "-tulnp"])
    exposed, risky = [], []
    for l in r.stdout.splitlines():
        p = l.split()
        if len(p) < 5:
            continue
        host, _, port = p[4].rpartition(":")
        if host.startswith(("127.", "[::1]", "::1")) or host.startswith("127.0.0."):
            continue
        proc = re.search(r'\("([^"]+)"', l)
        exposed.append(f"{p[0]:4} {p[4]:24} {proc.group(1) if proc else '-'}")
        if port.isdigit() and int(port) in RISKY_PORTS:
            risky.append(f"{port} ({RISKY_PORTS[int(port)]})")
    if not exposed:
        c.ok("Nenhum serviço escutando em interfaces externas.")
        return
    c.info(f"{len(exposed)} socket(s) escutando em interfaces não-locais:")
    for e in sorted(set(exposed))[:25]:
        c.bullet(e)
    if risky:
        c.warn(f"Portas sensíveis expostas: {', '.join(risky)}")


def _ssh_effective() -> dict[str, str]:
    r = query(["sshd", "-T"])
    cfg = {}
    for l in r.stdout.splitlines():
        if " " in l:
            k, v = l.split(" ", 1)
            cfg.setdefault(k.lower(), v.strip())
    return cfg


def sec_ssh(ctx: Ctx) -> None:
    c = ctx.console
    if not shutil.which("sshd"):
        c.ok("Servidor SSH (sshd) não instalado.")
        return
    active = [u for u in ("sshd.service", "sshd.socket")
              if ctx.unit_exists(u) and (ctx.unit_active(u) or ctx.unit_enabled(u))]
    if not active:
        c.ok("Servidor SSH instalado, porém inativo.")
        return
    c.info(f"SSH ativo via: {', '.join(active)}")
    cfg = _ssh_effective()
    if not cfg:
        c.warn("Não foi possível ler a configuração efetiva do sshd (sshd -T).")
        return
    root = cfg.get("permitrootlogin", "?")
    (c.warn if root == "yes" else c.ok)(f"PermitRootLogin: {root}")
    pw = cfg.get("passwordauthentication", "?")
    (c.warn if pw == "yes" else c.ok)(f"PasswordAuthentication: {pw}"
                                      + ("  (prefira chaves públicas)" if pw == "yes" else ""))
    c.info(f"Porta: {cfg.get('port', '?')} | PubkeyAuthentication: {cfg.get('pubkeyauthentication', '?')}")
    if root != "yes":
        return
    main = read_text("/etc/ssh/sshd_config")
    if not re.search(r"^\s*Include\s+.*sshd_config\.d", main, re.M):
        c.warn("sshd_config não inclui sshd_config.d; ajuste manualmente (não alterado).")
        return
    c.info("Proposta: criar /etc/ssh/sshd_config.d/99-cachyos-autoconfigurator.conf com 'PermitRootLogin no'.")
    if not ctx.yes("Desabilitar login SSH direto como root?", Answer.NO, safe=False):
        ctx.skipped("SSH: PermitRootLogin mantido")
        return
    path = Path("/etc/ssh/sshd_config.d/99-cachyos-autoconfigurator.conf")
    ctx.backup("/etc/ssh/sshd_config")
    if not ctx.write_file(path, f"# Criado por {APP_NAME}\nPermitRootLogin no\n"):
        return
    if ctx.dry_run:
        return
    # Valida ANTES de recarregar; reverte se algo estiver errado.
    if not query(["sshd", "-t"]).ok or _ssh_effective().get("permitrootlogin") != "no":
        ctx.failed("A validação do sshd falhou ou a diretiva não ficou efetiva; revertendo.")
        try:
            path.unlink()
        except OSError:
            pass
        return
    ctx.run(["systemctl", "reload", "sshd.service"])
    ctx.changed("SSH: PermitRootLogin no")


def sec_updates(ctx: Ctx) -> None:
    c = ctx.console
    if shutil.which("checkupdates"):
        r = query(["checkupdates"], timeout=120)
        n = len([l for l in r.stdout.splitlines() if l.strip()])
        (c.warn if n else c.ok)(f"{n} atualização(ões) pendente(s)." if n else "Sistema em dia.")
    else:
        n = len([l for l in query(["pacman", "-Qu"]).stdout.splitlines() if l.strip()])
        c.info(f"{n} atualização(ões) conhecida(s) (base local; instale pacman-contrib p/ checkupdates).")


def sec_admins(ctx: Ctx) -> None:
    c = ctx.console
    extra_root = [l.split(":")[0] for l in read_text("/etc/passwd").splitlines()
                  if l.split(":")[2:3] == ["0"] and not l.startswith("root:")]
    if extra_root:
        c.warn(f"Contas extras com UID 0: {', '.join(extra_root)}")
    else:
        c.ok("Somente 'root' possui UID 0.")
    for g in ("wheel", "sudo", "adm"):
        try:
            gr = grp.getgrnam(g)
        except KeyError:
            continue
        prim = [u.pw_name for u in pwd.getpwall() if u.pw_gid == gr.gr_gid]
        members = sorted(set(gr.gr_mem) | set(prim))
        c.info(f"Grupo {g}: {', '.join(members) or '(vazio)'}")
    sudoers = [Path("/etc/sudoers")] + sorted(Path("/etc/sudoers.d").glob("*"))
    for f in sudoers:
        for l in read_text(f).splitlines():
            s = l.strip()
            if s and not s.startswith("#") and "NOPASSWD" in s:
                c.warn(f"sudo sem senha em {f}: {s}")


def sec_permissions(ctx: Ctx) -> None:
    c = ctx.console
    problems = 0

    def chk(path: str, bad_mask: int, label: str) -> None:
        nonlocal problems
        try:
            st = os.stat(path)
        except OSError:
            return
        if st.st_mode & bad_mask:
            problems += 1
            c.warn(f"{label}: {path} com permissões {oct(st.st_mode & 0o7777)}")

    chk("/etc/shadow", 0o077, "Hashes de senha")
    chk("/etc/gshadow", 0o077, "Hashes de grupo")
    chk("/etc/sudoers", 0o037, "sudoers")
    for k in Path("/etc/ssh").glob("ssh_host_*_key"):
        chk(str(k), 0o077, "Chave privada do host SSH")
    user = ctx.target_user() if not ctx.auto_mode else None
    if user:
        ssh = Path(user.pw_dir) / ".ssh"
        if ssh.is_dir():
            chk(str(ssh), 0o077, "~/.ssh")
            for k in ssh.glob("id_*"):
                if not k.name.endswith(".pub"):
                    chk(str(k), 0o077, "Chave SSH privada")
        chk(user.pw_dir, 0o002, "Diretório home gravável por todos")
    # Arquivos graváveis por todos em /etc (ignora symlinks)
    ww = []
    for root, dirs, files in os.walk("/etc", onerror=lambda e: None):
        for n in files + dirs:
            p = os.path.join(root, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o002 and not (stat.S_ISDIR(st.st_mode) and st.st_mode & 0o1000):
                ww.append(p)
    for p in ww[:15]:
        problems += 1
        c.warn(f"Gravável por todos: {p}")
    # SUID/SGID fora do controle do pacman
    sus = []
    for base in ("/usr/bin", "/usr/sbin", "/usr/local", "/opt"):
        for root, _d, files in os.walk(base, onerror=lambda e: None):
            for n in files:
                p = os.path.join(root, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_mode & 0o6000:
                    sus.append(p)
    for p in sus:
        if not query(["pacman", "-Qoq", p]).ok:
            problems += 1
            c.warn(f"SUID/SGID que não pertence a nenhum pacote: {p}")
    if problems == 0:
        c.ok("Nenhuma permissão suspeita encontrada nas verificações básicas.")
    else:
        c.dim("   Estas verificações são apenas informativas; nada foi alterado.")


def mod_security(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Segurança básica")
    for title, fn in (("Firewall", sec_firewall), ("Serviços expostos", sec_ports),
                      ("SSH", sec_ssh), ("Atualizações", sec_updates),
                      ("Usuários administrativos", sec_admins),
                      ("Permissões suspeitas", sec_permissions)):
        c.info(f"Verificando: {title}")
        try:
            fn(ctx)
        except (SkipModule, UserAbort):
            raise
        except Exception as exc:  # uma verificação não deve derrubar as demais
            ctx.failed(f"Verificação '{title}' falhou: {exc}")
            _log("TRACE", traceback.format_exc())


# ===========================================================================
# Modo recomendado
# ===========================================================================
RECOMMENDED_STEPS: list[tuple[str, Callable[[Ctx], None]]] = [
    ("Atualizar o sistema", mod_update),
    ("Ferramentas básicas", mod_basic_deps),
    ("Ferramentas de diagnóstico", mod_diagnostics),
    ("TRIM (fstrim.timer)", mod_trim),
    ("Sincronização de horário", mod_timesync),
    ("Flatpak + Flathub", mod_flatpak),
    ("Bluetooth (se disponível)", mod_bluetooth),
    ("Manutenção básica (cache do pacman)", mod_maintenance_basic),
]


def mod_recommended(ctx: Ctx) -> None:
    c = ctx.console
    c.section("Configuração completa recomendada")
    c.info("Itens considerados seguros para um desktop CachyOS:")
    for label, _ in RECOMMENDED_STEPS:
        c.bullet(label)
    c.dim("   Nada de dezenas de programas: apenas o essencial. Remoções e SSH/firewall NÃO fazem parte.")
    idx = ctx.choose("Como deseja prosseguir?",
                     ["Aceitar todas as recomendações automaticamente",
                      "Revisar cada passo (Sim / Não / Pular)"])
    if idx is None:
        ctx.skipped("Modo recomendado: cancelado")
        return
    ctx.auto_mode = idx == 0
    try:
        for label, fn in RECOMMENDED_STEPS:
            run_module(ctx, label, fn)
    finally:
        ctx.auto_mode = False


# ===========================================================================
# Resumo / menu
# ===========================================================================
def print_summary(ctx: Ctx, final: bool = False) -> None:
    c, r = ctx.console, ctx.report
    c.banner("CONFIGURAÇÃO CONCLUÍDA" if final else "RESUMO ATUAL")
    c.line(c.c("1", "Alterações realizadas:"))
    for x in r.changes or ["(nenhuma)"]:
        c.bullet(x)
    c.line(c.c("1", "\nIgnorados:"))
    for x in r.skipped or ["(nenhum)"]:
        c.bullet(x)
    c.line(c.c("1", "\nErros:"))
    for x in r.errors or ["(nenhum)"]:
        c.bullet(x)
    if r.backups:
        c.line(c.c("1", "\nBackups:"))
        for x in r.backups:
            c.bullet(x)
    c.line(f"\nLog: {LOG_FILE}")


def mod_summary_view(ctx: Ctx) -> None:
    print_detection(ctx)
    if ctx.info.enabled_services:
        ctx.console.section("Serviços habilitados")
        names = ctx.info.enabled_services
        for i in range(0, len(names), 3):
            ctx.console.line("  " + "".join(n.ljust(34) for n in names[i:i + 3]))
    print_summary(ctx)


MENU: list[tuple[int, str, Callable[[Ctx], None]]] = [
    (1, "Atualização do sistema", mod_update),
    (2, "Dependências básicas", mod_basic_deps),
    (3, "Ferramentas de terminal", mod_terminal_tools),
    (4, "Desenvolvimento", mod_dev),
    (5, "Git", mod_git),
    (6, "Python", lambda c: dev_python(c, False)),
    (7, "Rust", lambda c: dev_rust(c, False)),
    (8, "Node.js", lambda c: dev_node(c, False)),
    (9, "Java", lambda c: dev_java(c, False)),
    (10, "Compiladores", mod_compilers),
    (11, "Flatpak", mod_flatpak),
    (12, "Bluetooth", mod_bluetooth),
    (13, "Impressão", mod_printing),
    (14, "TRIM", mod_trim),
    (15, "Sincronização de horário", mod_timesync),
    (16, "Otimizações do systemd", mod_systemd_opt),
    (17, "Serviços do usuário", mod_user_services),
    (18, "Configuração de energia", mod_power),
    (19, "Limpeza do sistema", mod_cleanup),
    (20, "Segurança básica", mod_security),
    (21, "Configuração completa recomendada", mod_recommended),
    (22, "Mostrar resumo", mod_summary_view),
]
MENU_MAP = {n: (label, fn) for n, label, fn in MENU}


def run_module(ctx: Ctx, label: str, fn: Callable[[Ctx], None]) -> None:
    """Executa um módulo isolando falhas: erros são registrados e o programa segue."""
    _log("MODULE", f"início: {label}")
    try:
        fn(ctx)
    except SkipModule:
        ctx.console.skip(f"{label}: pulado pelo usuário.")
        ctx.skipped(f"{label}: pulado pelo usuário")
    except (UserAbort, KeyboardInterrupt):
        raise UserAbort()
    except Exception as exc:
        ctx.failed(f"{label}: erro inesperado: {exc}")
        _log("TRACE", traceback.format_exc())
    _log("MODULE", f"fim: {label}")


def parse_selection(raw: str) -> Optional[list[int]]:
    """Aceita '3', '1 3 5', '1,3,5' e intervalos '2-4'. None = entrada inválida."""
    nums: list[int] = []
    for tok in re.split(r"[\s,;]+", raw.strip()):
        if not tok:
            continue
        if re.fullmatch(r"\d+-\d+", tok):
            a, b = map(int, tok.split("-"))
            if a > b:
                return None
            nums.extend(range(a, b + 1))
        elif tok.isdigit():
            nums.append(int(tok))
        else:
            return None
    if not nums or any(n != 0 and n not in MENU_MAP for n in nums):
        return None
    return list(dict.fromkeys(nums))


def main_menu(ctx: Ctx) -> None:
    c = ctx.console
    while True:
        print_header(ctx)
        c.line("\nO que deseja configurar?\n")
        for n, label, _ in MENU:
            c.line(f"  [{n:>2}] {label}")
        c.line("  [ 0] Sair")
        c.dim("\n  Dica: escolha vários itens, ex.: '2 3 14' ou '14-15' (executa somente o que escolher).")
        c.dim("  Em cada pergunta: s = Sim, n = Não, p = Pular o restante da seção.")
        sel = parse_selection(ctx.read("\nOpção(ões): "))
        if sel is None:
            c.warn("Seleção inválida.")
            continue
        _log("MENU", f"seleção: {sel}")
        if 0 in sel:
            return
        if 21 in sel:
            sel = [21]  # o modo recomendado já cobre as demais etapas seguras
        if len(sel) > 1:
            c.info("Serão executados, nesta ordem:")
            for n in sorted(sel):
                c.bullet(f"[{n}] {MENU_MAP[n][0]}")
            if ctx.ask("Continuar?", Answer.YES, safe=False) is not Answer.YES:
                continue
        for n in sorted(sel):
            run_module(ctx, MENU_MAP[n][0], MENU_MAP[n][1])
        ctx.read("\nPressione Enter para voltar ao menu...")


def finish(ctx: Ctx) -> None:
    print_summary(ctx, final=True)
    if ctx.dry_run:
        return
    if ctx.report.reboot_needed:
        ctx.console.warn("Algumas alterações exigem reinício para serem aplicadas.")
        try:
            if ctx.ask("Reiniciar agora?", Answer.NO, safe=False) is Answer.YES:
                ctx.console.info("Reiniciando...")
                _log("CMD", "systemctl reboot")
                subprocess.run(["systemctl", "reboot"], check=False)
        except UserAbort:
            pass
    else:
        ctx.console.ok("Nenhuma das alterações exige reinício.")


# ===========================================================================
# Programa principal
# ===========================================================================
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    ap.add_argument("--dry-run", action="store_true",
                    help="simula todas as ações sem alterar o sistema")
    ap.add_argument("--no-color", action="store_true", help="desativa cores ANSI")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    global LOG
    args = parse_args(argv)

    # Regra 1 e 2: precisa ser root.
    if os.geteuid() != 0:
        print(f"Este programa precisa ser executado como root.\n"
              f"Use: sudo python3 {Path(sys.argv[0]).name}")
        return 1
    if sys.version_info < (3, 9):
        print("Python 3.9 ou superior é necessário.")
        return 1

    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    console = Console(color)
    LOG = Logger(LOG_FILE)
    _log("START", f"{APP_NAME} {APP_VERSION} | dry_run={args.dry_run} | user={os.environ.get('SUDO_USER', 'root')}")
    ctx = Ctx(console, args.dry_run)

    if not sys.stdin.isatty():
        console.error("É necessário um terminal interativo (stdin não é um TTY).")
        return 1
    if not shutil.which("pacman"):
        console.error("pacman não encontrado: este não é um sistema baseado em Arch.")
        return 2

    # Regra 3-5: detectar tudo ANTES de modificar qualquer coisa.
    console.info("Detectando o sistema (nenhuma alteração será feita nesta etapa)...")
    ctx.info = detect_system(ctx)
    print_detection(ctx)

    if not ctx.info.is_cachyos:
        if not args.dry_run:
            console.error("Sistema diferente de CachyOS: por segurança, nenhuma alteração será feita.")
            _log("EXIT", "sistema não é CachyOS")
            return 2
        console.warn("Sistema diferente de CachyOS - seguindo apenas porque --dry-run está ativo.")
    if not ctx.info.has_systemd:
        console.warn("systemd ausente: módulos de serviços serão ignorados.")
    if not ctx.sync_db_present():
        console.warn("Bases do pacman não sincronizadas. Execute primeiro a opção [1] (atualização).")
    if ctx.info.total_bytes and ctx.info.free_bytes < 2 * 1024 ** 3:
        console.warn("Menos de 2 GiB livres em /: instalações e atualizações podem falhar.")

    try:
        main_menu(ctx)
    except UserAbort:
        console.warn("Interrompido pelo usuário.")
    except Exception as exc:  # rede de segurança final
        ctx.failed(f"Erro fatal: {exc}")
        _log("TRACE", traceback.format_exc())
    try:
        finish(ctx)
    except UserAbort:
        pass
    _log("EXIT", "encerrado")
    return 0 if not ctx.report.errors else 3


if __name__ == "__main__":
    sys.exit(main())
