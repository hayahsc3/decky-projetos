"""CheatDeck Plus - backend (Decky Loader).

Executa trainers (FLiNG e similares, .exe) dentro do mesmo prefixo Proton do jogo
que está rodando. Dois modos de lançamento:

* container: usa o steam-runtime-launch-client para rodar o trainer DENTRO do
  container (Steam Linux Runtime) do jogo, compartilhando o mesmo wineserver.
* direct: roda "proton run trainer.exe" no host com o mesmo STEAM_COMPAT_DATA_PATH.

No modo "auto" tenta container e cai para direct se falhar.
"""

import asyncio
import html
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import decky

HOME = Path(decky.DECKY_USER_HOME)
DATA_DIR = HOME / ".local" / "share" / "cheatdeck-plus"
TRAINERS_DIR = DATA_DIR / "trainers"
TMP_DIR = DATA_DIR / "tmp"
SETTINGS_FILE = Path(decky.DECKY_PLUGIN_SETTINGS_DIR) / "settings.json"
LOG_FILE = Path(decky.DECKY_PLUGIN_LOG_DIR) / "trainer.log"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
ARCHIVE_EXTS = (".zip", ".rar", ".7z")
IGNORED_EXE = re.compile(
    r"(unins|uninstall|setup|vc_?redist|dotnet|ndp\d|dxsetup|crashpad|updater|installer)",
    re.I,
)
PASS_ENV_PREFIXES = ("STEAM_COMPAT_", "SteamAppId", "SteamGameId", "PROTON_", "WINEDLLOVERRIDES")
HOST_ENV_PREFIXES = (
    "STEAM", "Steam", "PROTON_", "WINE", "DXVK", "VKD3D", "SDL_", "DISPLAY", "WAYLAND",
    "XDG_RUNTIME_DIR", "XAUTHORITY", "DBUS_SESSION", "GAMESCOPE", "ENABLE_GAMESCOPE",
    "XDG_SESSION_TYPE", "LANG", "HOME", "USER",
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "launch_mode": "auto",   # auto | container | direct
    "launch_delay": 0,       # segundos antes de abrir o trainer
    "auto_close": True,      # fechar trainer quando o jogo fechar
}


# --------------------------------------------------------------------------- util

def _log(msg: str) -> None:
    decky.logger.info(msg)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _load_settings() -> Dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    try:
        data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return data


def _save_settings(data: Dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _valid_appid(appid: Any) -> str:
    s = str(appid).strip()
    if not re.fullmatch(r"\d{1,20}", s):
        raise ValueError("appid inválido")
    return s


def _trainer_dir(appid: str) -> Path:
    return TRAINERS_DIR / appid


def _safe_join(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    base_r = base.resolve()
    if target != base_r and base_r not in target.parents:
        raise ValueError("caminho inválido")
    return target


def _sanitize_name(name: str) -> str:
    name = re.sub(r"[^\w.\-+()\[\] ]", "_", name, flags=re.UNICODE).strip(" .")
    return name[:120] or "trainer"


def _host_env(game_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Ambiente limpo para processos no host (Decky roda via PyInstaller e
    polui LD_LIBRARY_PATH)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH")
    }
    if "LD_LIBRARY_PATH_ORIG" in os.environ:
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH_ORIG"]
    if game_env:
        for k, v in game_env.items():
            if k.startswith(HOST_ENV_PREFIXES) and k not in ("LD_LIBRARY_PATH",):
                env[k] = v
    return env


def _same_game(gid: Optional[str], target: int) -> bool:
    try:
        n = int(gid or "")
    except ValueError:
        return False
    if n == target:
        return True
    # atalhos (não-Steam): SteamGameId = (appid << 32) | 0x02000000
    return target > 0 and n > 0xFFFFFFFF and (n >> 32) == target


def _read_env(pid: str) -> Optional[Dict[str, str]]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    env: Dict[str, str] = {}
    for kv in raw.decode("utf-8", "replace").split("\0"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k] = v
    return env


def _read_cmdline(pid: str) -> List[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
    except OSError:
        return []


def _scan_game(appid: str) -> Optional[Dict[str, Any]]:
    """Procura os processos do jogo em execução e extrai ambiente Proton."""
    target = int(appid)
    game_env: Optional[Dict[str, str]] = None
    game_pid: Optional[int] = None
    proton: Optional[str] = None

    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        if b"SteamGameId=" not in raw and b"SteamAppId=" not in raw:
            continue
        env = _read_env(pid)
        if not env:
            continue
        if not _same_game(env.get("SteamGameId") or env.get("SteamAppId"), target):
            continue
        if game_env is None and "STEAM_COMPAT_DATA_PATH" in env:
            game_env, game_pid = env, int(pid)
        if proton is None:
            for arg in _read_cmdline(pid):
                if arg.endswith("/proton") and os.path.isfile(arg):
                    proton = arg
                    break

    if not game_env:
        return None
    if proton is None:
        for p in game_env.get("STEAM_COMPAT_TOOL_PATHS", "").split(":"):
            cand = os.path.join(p, "proton")
            if p and os.path.isfile(cand):
                proton = cand
                break
    if proton is None:
        return None
    return {
        "pid": game_pid,
        "env": game_env,
        "proton": proton,
        "prefix": game_env.get("STEAM_COMPAT_DATA_PATH", ""),
    }


def _trainer_pids(appid: str) -> List[int]:
    """PIDs de processos Wine cujo comando aponta para a pasta de trainers do plugin."""
    marker_a = "cheatdeck-plus/trainers"
    marker_b = "cheatdeck-plus\\trainers"
    found: List[int] = []
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        cmd = " ".join(_read_cmdline(pid)).lower()
        if marker_a in cmd or marker_b in cmd:
            env = _read_env(pid)
            if env and _same_game(env.get("SteamGameId") or env.get("SteamAppId"), int(appid)):
                found.append(int(pid))
    return found


def _kill_trainers(appid: str) -> int:
    killed = 0
    for pid in _trainer_pids(appid):
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            pass
    return killed


# ------------------------------------------------------------ trainers (arquivos)

def _list_trainers(appid: str) -> List[Dict[str, Any]]:
    base = _trainer_dir(appid)
    items: List[Dict[str, Any]] = []
    if not base.is_dir():
        return items
    base_depth = len(base.parts)
    for root, dirs, files in os.walk(base):
        if len(Path(root).parts) - base_depth >= 4:
            dirs[:] = []
        for f in files:
            if not f.lower().endswith(".exe") or IGNORED_EXE.search(f):
                continue
            p = Path(root) / f
            rel = p.relative_to(base)
            try:
                st = p.stat()
            except OSError:
                continue
            items.append(
                {
                    "rel": str(rel),
                    "name": f,
                    "group": rel.parts[0] if len(rel.parts) > 1 else f,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items


def _strip_symlinks(folder: Path) -> None:
    for root, dirs, files in os.walk(folder):
        for n in list(dirs) + list(files):
            p = Path(root) / n
            if p.is_symlink():
                try:
                    p.unlink()
                except OSError:
                    pass


def _extract(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(src) as zf:
            dest_r = dest.resolve()
            for m in zf.infolist():
                t = (dest / m.filename).resolve()
                if t != dest_r and dest_r not in t.parents:
                    raise ValueError("zip com caminho malicioso")
            zf.extractall(dest)
        return
    tools = [
        ("7zz", ["x", "-y", f"-o{dest}", str(src)]),
        ("7z", ["x", "-y", f"-o{dest}", str(src)]),
        ("7za", ["x", "-y", f"-o{dest}", str(src)]),
        ("bsdtar", ["-xf", str(src), "-C", str(dest)]),
        ("unrar", ["x", "-y", str(src), str(dest) + "/"]),
    ]
    for tool, args in tools:
        exe = shutil.which(tool)
        if not exe:
            continue
        res = subprocess.run([exe, *args], capture_output=True, text=True, env=_host_env(), timeout=300)
        if res.returncode == 0:
            return
        _log(f"{tool} falhou ({res.returncode}): {res.stderr.strip()[:300]}")
    raise RuntimeError(
        "Não consegui extrair o arquivo. Instale 7z/bsdtar ou extraia manualmente "
        f"em {dest}"
    )


def _import_file(appid: str, src: Path) -> Dict[str, Any]:
    if not src.is_file():
        return {"ok": False, "message": "Arquivo não encontrado."}
    suffix = src.suffix.lower()
    group = _sanitize_name(src.stem)
    dest = _trainer_dir(appid) / group
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if suffix == ".exe":
            shutil.copy2(src, dest / _sanitize_name(src.name))
        elif suffix in ARCHIVE_EXTS:
            _extract(src, dest)
            _strip_symlinks(dest)
        else:
            shutil.rmtree(dest, ignore_errors=True)
            return {"ok": False, "message": "Formato não suportado (use .exe, .zip, .rar ou .7z)."}
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(dest, ignore_errors=True)
        _log(f"import falhou: {exc}")
        return {"ok": False, "message": f"Falha ao importar: {exc}"}
    exes = [t for t in _list_trainers(appid) if t["group"] == group or t["rel"].startswith(group)]
    if not exes:
        return {"ok": False, "message": "Importado, mas nenhum .exe de trainer foi encontrado dentro."}
    _log(f"importado {src.name} -> {dest}")
    return {"ok": True, "message": f"Trainer importado: {exes[0]['name']}"}


# ------------------------------------------------------------------------- rede

def _http_get(url: str, timeout: int = 25) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Use um link http(s).")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read(5_000_000)


def _download(url: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Use um link http(s).")
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=40) as resp:  # noqa: S310
        name = ""
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        if m:
            name = urllib.parse.unquote(m.group(1))
        if not name:
            name = urllib.parse.unquote(os.path.basename(parsed.path)) or "trainer.zip"
        name = _sanitize_name(name)
        dest = TMP_DIR / name
        total = 0
        with dest.open("wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise ValueError("Arquivo grande demais.")
                fh.write(chunk)
    return dest


def _fling_search(query: str) -> List[Dict[str, str]]:
    url = "https://flingtrainer.com/?s=" + urllib.parse.quote(query)
    page = _http_get(url).decode("utf-8", "replace")
    results: List[Dict[str, str]] = []
    seen = set()
    for href, title in re.findall(
        r'<a[^>]+href="(https://flingtrainer\.com/trainer/[^"#?]+)"[^>]*>\s*([^<]{3,})\s*</a>', page
    ):
        if href in seen:
            continue
        seen.add(href)
        results.append({"url": href, "title": html.unescape(title.strip())})
    return results[:10]


def _fling_files(page_url: str) -> List[Dict[str, str]]:
    host = urllib.parse.urlparse(page_url).hostname or ""
    if not host.endswith("flingtrainer.com"):
        raise ValueError("A página precisa ser do flingtrainer.com")
    page = _http_get(page_url).decode("utf-8", "replace")
    files: List[Dict[str, str]] = []
    seen = set()
    for href in re.findall(r'href=["\']([^"\']+\.(?:zip|rar|7z|exe))["\']', page, flags=re.I):
        full = urllib.parse.urljoin(page_url, html.unescape(href))
        h = urllib.parse.urlparse(full).hostname or ""
        if not h.endswith("flingtrainer.com") or full in seen:
            continue
        seen.add(full)
        files.append({"url": full, "label": urllib.parse.unquote(os.path.basename(full))})
    return files[:8]


NOISE_LINE = re.compile(
    r"(ATTENTION: default value|ProtonFixes|\[Gamescope WSI\]|pApplicationName|applicationVersion|"
    r"pEngineName|engineVersion|apiVersion|unit test)",
    re.I,
)


def _attempt_output(start: int, limit: int = 12) -> str:
    """Últimas linhas úteis que a tentativa de lançamento escreveu no log."""
    try:
        with LOG_FILE.open("rb") as fh:
            fh.seek(start)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not NOISE_LINE.search(ln)]
    return "\n".join(lines[-limit:])


# ------------------------------------------------------------------ lançamento

def _find_launch_client(env: Dict[str, str]) -> Optional[str]:
    root = Path(env.get("STEAM_COMPAT_CLIENT_INSTALL_PATH") or HOME / ".local/share/Steam")
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
            libs.append(Path(p))
    except OSError:
        pass
    for lib in libs:
        hits = sorted(
            lib.glob("steamapps/common/SteamLinuxRuntime*/pressure-vessel/bin/steam-runtime-launch-client"),
            reverse=True,
        )
        for h in hits:
            if os.access(h, os.X_OK):
                return str(h)
    return shutil.which("steam-runtime-launch-client")


def _bus_available(client: str, bus_id: str, env: Dict[str, str]) -> bool:
    try:
        res = subprocess.run(
            [client, "--list"], capture_output=True, text=True, timeout=6, env=_host_env(env)
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return f"com.steampowered.App{bus_id}" in (res.stdout + res.stderr)


def _python3(env: Dict[str, str]) -> str:
    return shutil.which("python3", path=env.get("PATH")) or "/usr/bin/python3"


def _build_commands(info: Dict[str, Any], exe: Path, appid: str, mode: str) -> List[Dict[str, Any]]:
    """Retorna lista ordenada de tentativas de lançamento."""
    env: Dict[str, str] = info["env"]
    proton: str = info["proton"]
    attempts: List[Dict[str, Any]] = []
    client = _find_launch_client(env)
    bus_id = env.get("SteamAppId") or appid

    if mode in ("auto", "container") and client and _bus_available(client, bus_id, env):
        pass_env = {k: v for k, v in env.items() if k.startswith(PASS_ENV_PREFIXES)}
        cmd = [
            client,
            f"--bus-name=com.steampowered.App{bus_id}",
            "--directory", str(exe.parent),
            "--",
            "/usr/bin/env",
            *[f"{k}={v}" for k, v in pass_env.items()],
            "python3", proton, "run", str(exe),
        ]
        attempts.append({"label": "container", "cmd": cmd, "env": _host_env(env), "cwd": str(exe.parent)})
    elif mode == "container":
        attempts.append({"label": "container-indisponivel", "cmd": None})

    if mode in ("auto", "direct"):
        denv = _host_env(env)
        for k, v in env.items():
            if k.startswith(PASS_ENV_PREFIXES):
                denv[k] = v
        attempts.append(
            {
                "label": "direct",
                "cmd": [_python3(env), proton, "run", str(exe)],
                "env": denv,
                "cwd": str(exe.parent),
            }
        )
    return attempts


# -------------------------------------------------------------------- Plugin

class Plugin:
    procs: Dict[str, List[subprocess.Popen]]
    watchdog: Optional["asyncio.Task[None]"]

    async def _main(self):
        self.procs = {}
        TRAINERS_DIR.mkdir(parents=True, exist_ok=True)
        _log("CheatDeck Plus iniciado")
        self.watchdog = asyncio.get_event_loop().create_task(self._watchdog())

    async def _unload(self):
        if getattr(self, "watchdog", None):
            self.watchdog.cancel()
        _log("CheatDeck Plus descarregado")

    async def _uninstall(self):
        _log("CheatDeck Plus desinstalado (trainers mantidos em ~/.local/share/cheatdeck-plus)")

    async def _watchdog(self):
        """Fecha trainers quando o jogo correspondente termina."""
        while True:
            await asyncio.sleep(6)
            try:
                if not _load_settings().get("auto_close", True):
                    continue
                for appid in list(self.procs.keys()):
                    self.procs[appid] = [p for p in self.procs[appid] if p.poll() is None]
                    pids = await asyncio.to_thread(_trainer_pids, appid)
                    if not self.procs[appid] and not pids:
                        self.procs.pop(appid, None)
                        continue
                    if not await asyncio.to_thread(_scan_game, appid):
                        _log(f"jogo {appid} encerrou; fechando trainer")
                        await self.stop_trainer(appid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                decky.logger.error(f"watchdog: {exc}")

    # ---- chamadas do frontend

    async def get_runtime(self, appid: str) -> Dict[str, Any]:
        try:
            appid = _valid_appid(appid)
        except ValueError:
            return {"running": False}
        info = await asyncio.to_thread(_scan_game, appid)
        pids = await asyncio.to_thread(_trainer_pids, appid)
        return {
            "running": bool(info),
            "proton": os.path.basename(os.path.dirname(info["proton"])) if info else "",
            "prefix": info["prefix"] if info else "",
            "trainer_running": len(pids) > 0,
        }

    async def list_trainers(self, appid: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(_list_trainers, _valid_appid(appid))

    async def list_downloads(self) -> List[Dict[str, Any]]:
        def work() -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for d in (HOME / "Downloads", HOME / "Desktop"):
                if not d.is_dir():
                    continue
                for p in d.iterdir():
                    if p.is_file() and p.suffix.lower() in (".exe",) + ARCHIVE_EXTS:
                        low = p.name.lower()
                        out.append(
                            {
                                "path": str(p),
                                "name": p.name,
                                "size": p.stat().st_size,
                                "mtime": int(p.stat().st_mtime),
                                "likely": "trainer" in low or "fling" in low,
                            }
                        )
            out.sort(key=lambda i: (not i["likely"], -i["mtime"]))
            return out[:12]

        return await asyncio.to_thread(work)

    async def import_download(self, appid: str, path: str) -> Dict[str, Any]:
        appid = _valid_appid(appid)
        src = Path(path).resolve()
        allowed = [(HOME / "Downloads").resolve(), (HOME / "Desktop").resolve()]
        if not any(a in src.parents for a in allowed):
            return {"ok": False, "message": "Só é possível importar de Downloads ou Desktop."}
        return await asyncio.to_thread(_import_file, appid, src)

    async def download_url(self, appid: str, url: str) -> Dict[str, Any]:
        appid = _valid_appid(appid)

        def work() -> Dict[str, Any]:
            try:
                tmp = _download(url.strip())
            except (ValueError, urllib.error.URLError, OSError) as exc:
                return {"ok": False, "message": f"Falha no download: {exc}"}
            try:
                return _import_file(appid, tmp)
            finally:
                tmp.unlink(missing_ok=True)

        return await asyncio.to_thread(work)

    async def fling_search(self, query: str) -> Dict[str, Any]:
        try:
            return {"ok": True, "results": await asyncio.to_thread(_fling_search, query.strip())}
        except Exception as exc:  # noqa: BLE001
            _log(f"fling_search: {exc}")
            return {"ok": False, "results": [], "message": f"Busca falhou ({exc}). Baixe pelo navegador e importe."}

    async def fling_files(self, page_url: str) -> Dict[str, Any]:
        try:
            return {"ok": True, "files": await asyncio.to_thread(_fling_files, page_url)}
        except Exception as exc:  # noqa: BLE001
            _log(f"fling_files: {exc}")
            return {"ok": False, "files": [], "message": f"Falha ao ler a página ({exc})."}

    async def delete_trainer(self, appid: str, rel: str) -> Dict[str, Any]:
        appid = _valid_appid(appid)
        base = _trainer_dir(appid)
        try:
            target = _safe_join(base, rel)
        except ValueError:
            return {"ok": False, "message": "Caminho inválido."}
        rel_path = target.relative_to(base.resolve())
        top = base / rel_path.parts[0] if rel_path.parts else None
        if not top or not top.exists():
            return {"ok": False, "message": "Trainer não encontrado."}
        if top.is_dir():
            shutil.rmtree(top, ignore_errors=True)
        else:
            top.unlink(missing_ok=True)
        return {"ok": True, "message": "Trainer removido."}

    async def launch_trainer(self, appid: str, rel: str) -> Dict[str, Any]:
        try:
            appid = _valid_appid(appid)
            exe = _safe_join(_trainer_dir(appid), rel)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        if not exe.is_file():
            return {"ok": False, "message": "Arquivo do trainer não existe mais."}

        info = await asyncio.to_thread(_scan_game, appid)
        if not info:
            return {
                "ok": False,
                "message": "Jogo não detectado. Abra o jogo primeiro: o trainer precisa rodar no prefixo Proton dele.",
            }
        if await asyncio.to_thread(_trainer_pids, appid):
            return {"ok": False, "message": "Já há um trainer rodando para este jogo. Pare-o antes."}

        settings = _load_settings()
        delay = int(settings.get("launch_delay", 0) or 0)
        if delay > 0:
            await asyncio.sleep(min(delay, 60))

        attempts = await asyncio.to_thread(_build_commands, info, exe, appid, settings.get("launch_mode", "auto"))
        last_error = "Nenhum modo de lançamento disponível."
        for att in attempts:
            if not att.get("cmd"):
                last_error = "Modo container indisponível (jogo sem servidor de container ativo)."
                continue
            _log(f"lançando [{att['label']}]: {' '.join(att['cmd'])}")
            try:
                log_start = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
            except OSError:
                log_start = 0
            try:
                with LOG_FILE.open("a", encoding="utf-8") as logfh:
                    proc = subprocess.Popen(
                        att["cmd"],
                        cwd=att["cwd"],
                        env=att["env"],
                        stdout=logfh,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
            except OSError as exc:
                last_error = f"Erro ao executar ({att['label']}): {exc}"
                _log(last_error)
                continue
            await asyncio.sleep(4)
            code = proc.poll()
            if code not in (None, 0):
                detail = _attempt_output(log_start)
                last_error = f"Modo {att['label']} terminou com código {code}."
                if detail:
                    last_error += "\n" + detail
                _log(last_error)
                continue
            self.procs.setdefault(appid, []).append(proc)
            note = "" if code is None else " (processo de lançamento já terminou; verifique a janela do trainer)"
            return {
                "ok": True,
                "message": f"Trainer iniciado ({att['label']}).{note} Abra a janela dele pelo menu Steam > Trocar janela.",
            }
        return {"ok": False, "message": last_error}

    async def stop_trainer(self, appid: str) -> Dict[str, Any]:
        appid = _valid_appid(appid)
        for p in self.procs.pop(appid, []):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        killed = await asyncio.to_thread(_kill_trainers, appid)
        _log(f"trainer {appid} parado ({killed} processos)")
        return {"ok": True, "message": "Trainer encerrado."}

    async def diagnose(self, appid: str) -> str:
        try:
            appid = _valid_appid(appid)
        except ValueError:
            return "appid inválido"
        lines: List[str] = []
        info = await asyncio.to_thread(_scan_game, appid)
        if not info:
            lines.append("Jogo: NÃO detectado (SteamGameId/Proton não encontrados em /proc).")
        else:
            env = info["env"]
            lines.append(f"Jogo: detectado (pid {info['pid']})")
            lines.append(f"Proton: {info['proton']}")
            lines.append(f"Prefixo: {info['prefix']}")
            client = await asyncio.to_thread(_find_launch_client, env)
            lines.append(f"launch-client: {client or 'não encontrado'}")
            if client:
                ok = await asyncio.to_thread(_bus_available, client, env.get("SteamAppId") or appid, env)
                lines.append(f"Servidor de container do jogo: {'ativo' if ok else 'inativo'}")
        lines.append(f"Extratores: {', '.join(t for t in ('7zz', '7z', '7za', 'bsdtar', 'unrar') if shutil.which(t)) or 'só zip'}")
        lines.append(f"Pasta de trainers: {_trainer_dir(appid)}")
        lines.append("")
        lines.append("--- últimas linhas do log ---")
        try:
            raw = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = [ln for ln in raw if not NOISE_LINE.search(ln)][-40:]
            lines.extend(tail)
        except OSError:
            lines.append("(sem log)")
        return "\n".join(lines)

    async def get_settings(self) -> Dict[str, Any]:
        return _load_settings()

    async def set_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        cur = _load_settings()
        if settings.get("launch_mode") in ("auto", "container", "direct"):
            cur["launch_mode"] = settings["launch_mode"]
        if "launch_delay" in settings:
            cur["launch_delay"] = max(0, min(60, int(settings["launch_delay"])))
        if "auto_close" in settings:
            cur["auto_close"] = bool(settings["auto_close"])
        _save_settings(cur)
        return cur
