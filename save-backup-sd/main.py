"""SaveBackupSD - backend (Decky Loader).

Lista os jogos instalados (Steam e atalhos não-Steam), faz backup dos saves
para o microSD e restaura quando necessário.

Fontes de save copiadas por jogo:
  * prefixo do Proton: compatdata/<appid>/pfx/drive_c/users/steamuser (sem lixo/cache)
  * prefixo do Proton: compatdata/<appid>/pfx/drive_c/users/Public/Documents
  * cache da Steam Cloud: userdata/<userid>/<appid>/remote
  * pastas extras definidas em settings.json (custom_paths), úteis para jogos nativos

Destino: <microSD>/DeckSaveBackup/<appid> - <nome do jogo>/AAAA-MM-DD_HH-MM-SS.tar.gz
"""

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import decky

BACKUP_DIR_NAME = "DeckSaveBackup"
PRE_RESTORE_PREFIX = "pre-restore_"
PRE_RESTORE_KEEP = 3
TS_FORMAT = "%Y-%m-%d_%H-%M-%S"
BACKUP_FILE_RE = re.compile(r"^(?:pre-restore_)?(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.tar\.gz$")

# Entradas da Steam que não são jogos.
IGNORE_APPIDS = {228980}
IGNORE_NAME_PREFIXES = (
    "proton",
    "steam linux runtime",
    "steamworks common redistributables",
    "steam controller configs",
)

# Pastas descartadas dentro de users/steamuser (cache/lixo que não é save).
EXCLUDE_DIR_NAMES = {
    "temp", "tmp", "cache", "gpucache", "code cache", "shadercache", "d3dscache",
    "crashdumps", "crashpad", "crash reports", "webcache", "dawncache",
}
EXCLUDE_TOP_LEVEL = {"desktop", "downloads", "music", "pictures", "videos"}
EXCLUDE_REL_PATHS = {
    "appdata/local/microsoft",
    "appdata/roaming/microsoft",
    "appdata/locallow/microsoft",
    "appdata/local/nvidia",
    "appdata/locallow/nvidia",
    "appdata/local/packages",
}

DEFAULT_SETTINGS: Dict[str, Any] = {"max_backups": 5, "destination": "", "custom_paths": {}, "auto_backup": False, "auto_mount": True,
    "update_repo": "hayahsc3/decky-projetos", "github_token": "", "notified_version": "",
}

UPDATE_TAG_PREFIX = "save-backup-sd-v"
UPDATE_CACHE_TTL = 600.0

WATCH_INTERVAL = 5.0   # segundos entre verificações de jogos em execução
AUTO_DELAY = 6.0       # espera após o jogo fechar (deixa o jogo/Steam terminarem de gravar)

_lock: Optional[asyncio.Lock] = None
_owner_cache: Any = "unset"


# --------------------------------------------------------------------------
# utilidades gerais
# --------------------------------------------------------------------------

def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def _emit(event: str, *args: Any) -> None:
    try:
        await decky.emit(event, *args)
    except Exception:  # noqa: BLE001 - evento é só cosmético
        pass


def _res(ok: bool, message: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": ok, "message": message}
    out.update(extra)
    return out


def _home() -> Path:
    return Path(decky.DECKY_USER_HOME)


def _owner_ids() -> Optional[Tuple[int, int]]:
    """Se o backend rodar como root, os arquivos restaurados precisam voltar ao usuário."""
    global _owner_cache
    if _owner_cache != "unset":
        return _owner_cache
    ids = None
    try:
        if os.geteuid() == 0:
            import pwd

            pw = pwd.getpwnam(getattr(decky, "DECKY_USER", "deck") or "deck")
            ids = (pw.pw_uid, pw.pw_gid)
    except Exception:  # noqa: BLE001
        ids = None
    _owner_cache = ids
    return ids


def _chown(path: Path) -> None:
    ids = _owner_ids()
    if ids:
        try:
            os.chown(path, ids[0], ids[1], follow_symlinks=False)
        except OSError:
            pass


def _makedirs(path: Path, top: Path) -> None:
    """mkdir -p que ajusta o dono das pastas criadas (até `top`)."""
    missing: List[Path] = []
    p = path
    while not p.exists() and p != top and p != p.parent:
        missing.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True)
    for d in reversed(missing):
        _chown(d)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .")
    return cleaned[:60] or "jogo"


def _fmt_size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} B"


# --------------------------------------------------------------------------
# configurações
# --------------------------------------------------------------------------

def _settings_path() -> Path:
    return Path(decky.DECKY_PLUGIN_SETTINGS_DIR) / "settings.json"


def load_settings() -> Dict[str, Any]:
    s: Dict[str, Any] = {"max_backups": DEFAULT_SETTINGS["max_backups"], "destination": "", "custom_paths": {}, "auto_backup": False, "auto_mount": True,
         "update_repo": DEFAULT_SETTINGS["update_repo"], "github_token": "", "notified_version": ""}
    try:
        raw = json.loads(_settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return s
    except Exception as e:  # noqa: BLE001
        decky.logger.warning(f"settings.json inválido: {e}")
        return s
    if isinstance(raw, dict):
        mb = raw.get("max_backups")
        if isinstance(mb, int) and 1 <= mb <= 50:
            s["max_backups"] = mb
        for key in ("update_repo", "github_token", "notified_version"):
            val = raw.get(key)
            if isinstance(val, str) and (val.strip() or key != "update_repo"):
                s[key] = val.strip()
        am = raw.get("auto_mount")
        if isinstance(am, bool):
            s["auto_mount"] = am
        ab = raw.get("auto_backup")
        if isinstance(ab, bool):
            s["auto_backup"] = ab
        dst = raw.get("destination")
        if isinstance(dst, str):
            s["destination"] = dst
        cp = raw.get("custom_paths")
        if isinstance(cp, dict):
            s["custom_paths"] = {
                str(k): [str(x) for x in v] for k, v in cp.items() if isinstance(v, list)
            }
    return s


def save_settings(s: Dict[str, Any]) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# parsers de VDF
# --------------------------------------------------------------------------

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> Dict[str, Any]:
    """VDF em texto (libraryfolders.vdf, appmanifest_*.acf)."""
    tokens = _VDF_TOKEN.findall(text)
    pos = 0

    def unescape(s: str) -> str:
        return s.replace("\\\\", "\\").replace('\\"', '"')

    def parse_obj() -> Dict[str, Any]:
        nonlocal pos
        obj: Dict[str, Any] = {}
        while pos < len(tokens):
            s, brace = tokens[pos]
            if brace == "}":
                pos += 1
                return obj
            if brace == "{":  # malformado
                pos += 1
                continue
            key = unescape(s)
            pos += 1
            if pos >= len(tokens):
                break
            s2, b2 = tokens[pos]
            if b2 == "{":
                pos += 1
                obj[key] = parse_obj()
            elif b2 == "}":
                break
            else:
                obj[key] = unescape(s2)
                pos += 1
        return obj

    return parse_obj()


def parse_binary_vdf(data: bytes) -> Dict[str, Any]:
    """VDF binário (userdata/<id>/config/shortcuts.vdf)."""
    pos = 0

    def read_cstr() -> str:
        nonlocal pos
        end = data.index(b"\x00", pos)
        s = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    def read_obj() -> Dict[str, Any]:
        nonlocal pos
        obj: Dict[str, Any] = {}
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == 8:
                return obj
            key = read_cstr()
            if t == 0:
                obj[key] = read_obj()
            elif t == 1:
                obj[key] = read_cstr()
            elif t == 2:
                obj[key] = struct.unpack_from("<i", data, pos)[0]
                pos += 4
            elif t == 3:
                obj[key] = struct.unpack_from("<f", data, pos)[0]
                pos += 4
            elif t == 7:
                obj[key] = struct.unpack_from("<Q", data, pos)[0]
                pos += 8
            else:
                raise ValueError(f"tipo VDF desconhecido: {t}")
        return obj

    return read_obj()


# --------------------------------------------------------------------------
# Steam: pastas, bibliotecas, jogos
# --------------------------------------------------------------------------

def steam_roots() -> List[Path]:
    home = _home()
    candidates = [
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ]
    seen = set()
    roots: List[Path] = []
    for c in candidates:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r in seen or not (r / "steamapps").is_dir():
            continue
        seen.add(r)
        roots.append(r)
    return roots


def library_paths() -> List[Path]:
    libs: List[Path] = []
    for root in steam_roots():
        libs.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            data = parse_vdf(vdf.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        for k, v in folders.items():
            if isinstance(v, dict) and v.get("path"):
                libs.append(Path(v["path"]))
            elif isinstance(v, str) and k.isdigit():
                libs.append(Path(v))
    unique: List[Path] = []
    seen = set()
    for lib in libs:
        try:
            r = lib.resolve()
        except OSError:
            continue
        if r in seen or not (r / "steamapps").is_dir():
            continue
        seen.add(r)
        unique.append(r)
    return unique


def _numeric_dirs(path: Path) -> List[Path]:
    try:
        return sorted(e for e in path.iterdir() if e.is_dir() and e.name.isdigit() and e.name != "0")
    except OSError:
        return []


def userdata_dirs() -> List[Path]:
    out: List[Path] = []
    for root in steam_roots():
        out.extend(_numeric_dirs(root / "userdata"))
    return out


def installed_steam_games() -> Dict[int, Dict[str, Any]]:
    games: Dict[int, Dict[str, Any]] = {}
    for lib in library_paths():
        for mf in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                state = parse_vdf(mf.read_text(encoding="utf-8", errors="ignore")).get("AppState", {})
                appid = int(state.get("appid"))
            except (OSError, TypeError, ValueError):
                continue
            name = state.get("name") or f"App {appid}"
            if appid in IGNORE_APPIDS or name.lower().startswith(IGNORE_NAME_PREFIXES):
                continue
            games[appid] = {"appid": appid, "name": name, "kind": "steam"}
    return games


def nonsteam_games() -> Dict[int, Dict[str, Any]]:
    games: Dict[int, Dict[str, Any]] = {}
    for uid_dir in userdata_dirs():
        sc = uid_dir / "config" / "shortcuts.vdf"
        try:
            parsed = parse_binary_vdf(sc.read_bytes())
        except (OSError, ValueError, struct.error):
            continue
        entries = parsed.get("shortcuts") or parsed.get("Shortcuts") or {}
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            low = {str(k).lower(): v for k, v in entry.items()}
            name = low.get("appname") or low.get("name")
            if not name:
                continue
            appid_raw = low.get("appid")
            if isinstance(appid_raw, int):
                appid = appid_raw & 0xFFFFFFFF
            else:
                key = f"{low.get('exe', '')}{name}".encode("utf-8")
                appid = (zlib.crc32(key) & 0xFFFFFFFF) | 0x80000000
            games.setdefault(appid, {"appid": appid, "name": str(name), "kind": "nonsteam"})
    return games


def get_games() -> Dict[int, Dict[str, Any]]:
    games = nonsteam_games()
    games.update(installed_steam_games())
    return games


def find_compatdata(appid: int) -> Optional[Path]:
    for lib in library_paths():
        p = lib / "steamapps" / "compatdata" / str(appid)
        if p.is_dir():
            return p
    return None


def has_game_data(appid: int) -> bool:
    if find_compatdata(appid):
        return True
    return any((u / str(appid)).is_dir() for u in userdata_dirs())


def running_appids() -> set:
    """AppIds de jogos em execução (a Steam lança tudo via `reaper SteamLaunch AppId=<id>`)."""
    found = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\x00")
        except OSError:
            continue
        for a in args:
            if a.startswith(b"AppId="):
                try:
                    found.add(int(a[6:]))
                except ValueError:
                    pass
    return found


def is_game_running(appid: int) -> bool:
    return appid in running_appids()


# --------------------------------------------------------------------------
# microSD / destino
# --------------------------------------------------------------------------

def _unescape_mount(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_mounts(text: str) -> List[Dict[str, str]]:
    """Lê /proc/mounts e devolve os pontos de montagem de cartões (mmcblk*)."""
    out: List[Dict[str, str]] = []
    seen = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fs = parts[0], _unescape_mount(parts[1]), parts[2]
        if not dev.startswith("/dev/mmcblk") or mnt in seen:
            continue
        seen.add(mnt)
        out.append({"device": dev, "mount": mnt, "fs": fs})
    return out


def _describe_mount(mount: str, device: str = "", fs: str = "", custom: bool = False) -> Dict[str, Any]:
    try:
        du = shutil.disk_usage(mount)
        total, free = du.total, du.free
    except OSError:
        total = free = 0
    return {
        "mount": mount,
        "device": device,
        "fs": fs,
        "total": total,
        "free": free,
        "writable": os.access(mount, os.W_OK),
        "label": os.path.basename(mount.rstrip("/")) or mount,
        "custom": custom,
    }


SYSTEM_MOUNT_PREFIXES = ("/home", "/var", "/boot", "/efi", "/etc", "/usr", "/opt", "/srv", "/sysroot")
SKIP_FSTYPES = {"swap", "crypto_LUKS", "LVM2_member", "linux_raid_member", "iso9660", "squashfs", "vfat_esp"}
MOUNT_RETRY_COOLDOWN = 20.0
AUTOMOUNT_SCRIPT = "/usr/lib/hwsupport/steamos-automount.sh"

_mount_fail_ts = 0.0
_mount_fail_msg = ""


def _run_cmd(args: List[str], timeout: float = 25.0) -> Tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return 127, f"comando não encontrado: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "tempo esgotado"
    except OSError as e:
        return 126, str(e)


def _truthy(v: Any) -> bool:
    return v is True or str(v).lower() in ("1", "true")


def _is_system_mount(mp: str) -> bool:
    return mp == "/" or any(mp == p or mp.startswith(p + "/") for p in SYSTEM_MOUNT_PREFIXES)


def parse_lsblk(text: str) -> List[Dict[str, Any]]:
    """Interpreta `lsblk -J` e devolve sistemas de arquivos candidatos a cartão/mídia removível."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: List[Dict[str, Any]] = []

    def collect(node: Dict[str, Any], acc: List[Dict[str, Any]]) -> None:
        acc.append(node)
        for c in node.get("children") or []:
            collect(c, acc)

    for top in data.get("blockdevices") or []:
        nodes: List[Dict[str, Any]] = []
        collect(top, nodes)
        name = str(top.get("name", ""))
        removable = _truthy(top.get("rm")) or name.startswith("mmcblk")
        if not removable:
            continue
        mps = [str(n.get("mountpoint") or "") for n in nodes]
        if any(mp and _is_system_mount(mp) for mp in mps):
            continue  # disco do sistema (ex.: eMMC interna): nunca mexer
        for n in nodes:
            fs = str(n.get("fstype") or "")
            if not fs or fs in SKIP_FSTYPES or n.get("type") not in ("part", "disk"):
                continue
            if n.get("type") == "disk" and n.get("children"):
                continue
            mp = str(n.get("mountpoint") or "")
            if mp and mp != "/" and not mp.startswith(("/run/media", "/media", "/mnt")):
                continue
            try:
                size = int(n.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            out.append({
                "name": str(n.get("name", "")),
                "path": str(n.get("path") or f"/dev/{n.get('name', '')}"),
                "fstype": fs,
                "label": str(n.get("label") or ""),
                "mountpoint": mp,
                "size": size,
            })
    return out


def list_removable() -> List[Dict[str, Any]]:
    rc, out = _run_cmd(["lsblk", "-J", "-b", "-o", "NAME,PATH,FSTYPE,LABEL,MOUNTPOINT,RM,TYPE,SIZE"], 10)
    return parse_lsblk(out) if rc == 0 else []


def find_sd_mounts() -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8", errors="ignore")
        for m in parse_mounts(text):
            result.append(_describe_mount(m["mount"], m["device"], m["fs"]))
            seen.add(m["mount"])
    except OSError:
        pass
    # mídias removíveis montadas que não são mmcblk (ex.: leitor de cartão via USB)
    for c in list_removable():
        mp = c["mountpoint"]
        if mp and mp not in seen and mp != "/":
            result.append(_describe_mount(mp, c["path"], c["fstype"]))
            seen.add(mp)
    return result


def _mount_dir_for(c: Dict[str, Any]) -> Path:
    user = getattr(decky, "DECKY_USER", "deck") or "deck"
    name = re.sub(r"[^\w.-]+", "_", c["label"] or c["name"]) or c["name"]
    return Path("/run/media") / user / name


def _mount_one(c: Dict[str, Any]) -> Tuple[bool, str]:
    """Tenta montar uma partição; devolve (ok, detalhe)."""
    dev, name = c["path"], c["name"]
    errors: List[str] = []
    is_root = os.geteuid() == 0

    if is_root and os.path.exists(AUTOMOUNT_SCRIPT):
        rc, out = _run_cmd(["/bin/bash", AUTOMOUNT_SCRIPT, "add", name], 40)
        if rc == 0 and _find_mountpoint(dev):
            return True, "montado"
        errors.append(f"steamos-automount: {out or rc}")

    rc, out = _run_cmd(["udisksctl", "mount", "-b", dev, "--no-user-interaction"], 40)
    if rc == 0 or "already mounted" in out.lower():
        return True, "montado"
    errors.append(f"udisksctl: {out or rc}")

    if is_root:
        target = _mount_dir_for(c)
        opts = ["-o", "nosuid,nodev,noexec"]
        fs = c["fstype"]
        ids = _owner_ids()
        if fs in ("vfat", "exfat", "ntfs", "ntfs3") and ids:
            opts = ["-o", f"nosuid,nodev,noexec,uid={ids[0]},gid={ids[1]},umask=0022"]
        try:
            _makedirs(target, Path("/run/media"))
        except OSError as e:
            errors.append(f"mkdir: {e}")
        else:
            rc, out = _run_cmd(["mount", *opts, dev, str(target)], 40)
            if rc == 0:
                return True, "montado"
            errors.append(f"mount: {out or rc}")
            try:
                target.rmdir()
            except OSError:
                pass
    return False, "; ".join(errors)


def _find_mountpoint(dev: str) -> str:
    try:
        for line in Path("/proc/mounts").read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == dev:
                return _unescape_mount(parts[1])
    except OSError:
        pass
    return ""


def mount_sd(force: bool = False) -> Tuple[bool, str]:
    """Monta o(s) cartão(ões) removível(is) ainda não montados. Respeita um intervalo entre falhas."""
    global _mount_fail_ts, _mount_fail_msg
    if not force and _mount_fail_ts and time.time() - _mount_fail_ts < MOUNT_RETRY_COOLDOWN:
        return False, _mount_fail_msg
    pending = [c for c in list_removable() if not c["mountpoint"]]
    if not pending:
        return False, "Nenhum cartão desmontado foi encontrado."
    # prefere os mmcblk (microSD)
    pending.sort(key=lambda c: (not c["name"].startswith("mmcblk"), c["name"]))
    last = ""
    for c in pending:
        ok, detail = _mount_one(c)
        if ok:
            _mount_fail_ts, _mount_fail_msg = 0.0, ""
            decky.logger.info(f"microSD montado: {c['path']}")
            return True, f"Montado: {c['label'] or c['name']}"
        last = f"{c['path']}: {detail}"
        decky.logger.warning(f"falha ao montar {last}")
    _mount_fail_ts, _mount_fail_msg = time.time(), f"Não foi possível montar o cartão ({last})"
    return False, _mount_fail_msg


def get_destination(settings: Dict[str, Any]) -> Tuple[Optional[Path], str, Optional[Dict[str, Any]]]:
    custom = str(settings.get("destination", "")).strip()
    if custom:
        p = Path(custom)
        if p.is_dir() and os.access(p, os.W_OK):
            return p, "", _describe_mount(str(p), custom=True)
        return None, f"Destino personalizado indisponível: {custom}", None
    sds = find_sd_mounts()
    mount_err = ""
    if not any(sd["writable"] for sd in sds) and settings.get("auto_mount", True):
        ok, msg = mount_sd()
        if ok:
            sds = find_sd_mounts()
        elif list_removable_unmounted_hint(msg):
            mount_err = msg
    for sd in sds:
        if sd["writable"]:
            return Path(sd["mount"]), "", sd
    if sds:
        return None, "O microSD está montado, mas sem permissão de escrita.", sds[0]
    return None, mount_err or "microSD não encontrado. Insira o cartão e atualize.", None


def list_removable_unmounted_hint(msg: str) -> bool:
    """Só mostra a mensagem de falha de montagem quando havia mesmo um cartão para montar."""
    return msg.startswith("Não foi possível montar")


# --------------------------------------------------------------------------
# pastas de backup no destino
# --------------------------------------------------------------------------

def backups_root(dest: Path) -> Path:
    return dest / BACKUP_DIR_NAME


def find_game_dir(dest: Path, appid: int) -> Optional[Path]:
    root = backups_root(dest)
    if not root.is_dir():
        return None
    prefix = f"{appid} - "
    for e in sorted(root.iterdir()):
        if e.is_dir() and (e.name == str(appid) or e.name.startswith(prefix)):
            return e
    return None


def list_backup_files(game_dir: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        entries = list(game_dir.iterdir())
    except OSError:
        return out
    for e in entries:
        m = BACKUP_FILE_RE.match(e.name)
        if not m or not e.is_file():
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), TS_FORMAT))
        except (ValueError, OverflowError):
            ts = e.stat().st_mtime
        out.append({
            "filename": e.name,
            "size": e.stat().st_size,
            "ts": int(ts),
            "pre_restore": e.name.startswith(PRE_RESTORE_PREFIX),
        })
    out.sort(key=lambda b: b["ts"], reverse=True)
    return out


def scan_backup_dirs(dest: Path) -> Dict[int, Dict[str, Any]]:
    res: Dict[int, Dict[str, Any]] = {}
    root = backups_root(dest)
    if not root.is_dir():
        return res
    for e in root.iterdir():
        if not e.is_dir():
            continue
        m = re.match(r"^(\d+)(?: - (.*))?$", e.name)
        if not m:
            continue
        appid = int(m.group(1))
        name = m.group(2) or str(appid)
        try:
            info = json.loads((e / "info.json").read_text(encoding="utf-8"))
            if isinstance(info, dict) and info.get("name"):
                name = str(info["name"])
        except (OSError, ValueError):
            pass
        res[appid] = {"dir": e, "name": name, "backups": list_backup_files(e)}
    return res


def prune(folder: Path, keep: int) -> None:
    files = list_backup_files(folder)
    regular = [b for b in files if not b["pre_restore"]]
    pre = [b for b in files if b["pre_restore"]]
    for b in regular[keep:] + pre[PRE_RESTORE_KEEP:]:
        try:
            (folder / b["filename"]).unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# coleta de arquivos de save
# --------------------------------------------------------------------------

def collect_sources(appid: int, extra_paths: List[str]) -> List[Tuple[str, Path, bool]]:
    """Retorna [(chave, pasta_base, aplicar_exclusões)]."""
    sources: List[Tuple[str, Path, bool]] = []
    compat = find_compatdata(appid)
    if compat:
        users = compat / "pfx" / "drive_c" / "users"
        if (users / "steamuser").is_dir():
            sources.append(("prefix_user", users / "steamuser", True))
        if (users / "Public" / "Documents").is_dir():
            sources.append(("prefix_public", users / "Public" / "Documents", False))
    for uid_dir in userdata_dirs():
        remote = uid_dir / str(appid) / "remote"
        if remote.is_dir():
            sources.append((f"userdata_{uid_dir.name}", remote, False))
    for i, p in enumerate(extra_paths):
        path = Path(p)
        if path.is_dir():
            sources.append((f"custom_{i}", path, False))
    return sources


def iter_files(base: Path, apply_excludes: bool):
    """Gera (caminho_completo, caminho_relativo) de arquivos regulares, sem seguir links."""
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(dirpath, base)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        if apply_excludes:
            keep = []
            for d in dirnames:
                rel = f"{rel_dir}/{d}" if rel_dir else d
                low = rel.lower()
                if d.lower() in EXCLUDE_DIR_NAMES or low in EXCLUDE_REL_PATHS:
                    continue
                if not rel_dir and d.lower() in EXCLUDE_TOP_LEVEL:
                    continue
                keep.append(d)
            dirnames[:] = keep
        for f in filenames:
            full = os.path.join(dirpath, f)
            try:
                if not stat.S_ISREG(os.lstat(full).st_mode):
                    continue
            except OSError:
                continue
            yield full, (f"{rel_dir}/{f}" if rel_dir else f)


def _clean_tarinfo(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _signature(entries: List[Tuple[str, str, str]]) -> str:
    """Assinatura do conteúdo atual dos saves (caminho, tamanho, mtime) para detectar mudanças."""
    h = hashlib.sha1()
    for key, full, rel in sorted(entries, key=lambda e: (e[0], e[2])):
        try:
            st = os.lstat(full)
        except OSError:
            continue
        h.update(f"{key}|{rel}|{st.st_size}|{int(st.st_mtime)}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _read_state(folder: Path) -> Dict[str, Any]:
    try:
        data = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def create_archive(
    game: Dict[str, Any], folder: Path, extra_paths: List[str], prefix: str = "",
    only_if_changed: bool = False,
) -> Tuple[Optional[Path], Dict[str, Any]]:
    appid = game["appid"]
    sources = collect_sources(appid, extra_paths)
    entries: List[Tuple[str, str, str]] = []
    total = 0
    for key, base, excl in sources:
        for full, rel in iter_files(base, excl):
            try:
                total += os.lstat(full).st_size
            except OSError:
                continue
            entries.append((key, full, rel))
    if not entries:
        return None, {"files": 0, "bytes": 0}

    signature = _signature(entries)
    if only_if_changed and _read_state(folder).get("signature") == signature:
        return None, {"files": 0, "bytes": 0, "unchanged": True}

    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if total > free - 8 * 1024 * 1024:
        raise OSError(f"espaço insuficiente (precisa de até {_fmt_size(total)}, livre {_fmt_size(free)})")

    ts = datetime.now().strftime(TS_FORMAT)
    final = folder / f"{prefix}{ts}.tar.gz"
    while final.exists():
        time.sleep(1.0)
        ts = datetime.now().strftime(TS_FORMAT)
        final = folder / f"{prefix}{ts}.tar.gz"
    tmp = folder / (final.name + ".partial")

    counts: Dict[str, int] = {}
    for key, _, _ in entries:
        counts[key] = counts.get(key, 0) + 1
    manifest = {
        "version": 1,
        "plugin": "SaveBackupSD",
        "appid": appid,
        "name": game["name"],
        "kind": game.get("kind", "steam"),
        "created": datetime.now().isoformat(timespec="seconds"),
        "sources": [
            {
                "key": key,
                "custom_path": str(base) if key.startswith("custom_") else None,
                "files": counts.get(key, 0),
            }
            for key, base, _ in sources
            if counts.get(key, 0)
        ],
    }

    written = 0
    try:
        with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
            data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            mi = tarfile.TarInfo("manifest.json")
            mi.size = len(data)
            mi.mtime = int(time.time())
            mi.mode = 0o644
            tar.addfile(mi, io.BytesIO(data))
            for key, full, rel in entries:
                try:
                    f = open(full, "rb")
                except OSError:
                    continue  # sem permissão / sumiu: ignora só esse arquivo
                with f:
                    ti = tar.gettarinfo(full, arcname=f"data/{key}/{rel}", fileobj=f)
                    tar.addfile(_clean_tarinfo(ti), f)
                    written += 1
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if not prefix:
        try:
            (folder / "state.json").write_text(
                json.dumps({"signature": signature, "ts": int(time.time())}), encoding="utf-8"
            )
        except OSError:
            pass
    _chown(final)
    _chown(folder)
    return final, {"files": written, "bytes": total}


def write_info(folder: Path, game: Dict[str, Any]) -> None:
    try:
        (folder / "info.json").write_text(
            json.dumps({"appid": game["appid"], "name": game["name"], "kind": game.get("kind", "steam")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


# --------------------------------------------------------------------------
# operações (bloqueantes; rodam em thread)
# --------------------------------------------------------------------------

def _backup_one(appid: int, only_if_changed: bool = False) -> Dict[str, Any]:
    settings = load_settings()
    dest, err, _ = get_destination(settings)
    if dest is None:
        return _res(False, err)
    game = get_games().get(appid)
    if game is None:
        return _res(False, "Jogo não encontrado entre os instalados.", notfound=True)
    try:
        folder = find_game_dir(dest, appid) or (
            backups_root(dest) / f"{appid} - {_safe_name(game['name'])}"
        )
        path, stats = create_archive(game, folder, settings["custom_paths"].get(str(appid), []),
                                     only_if_changed=only_if_changed)
        if path is None and stats.get("unchanged"):
            return _res(True, "Sem alterações desde o último backup.", unchanged=True, name=game["name"])
        if path is None:
            return _res(False, "Nenhum save encontrado. Abra o jogo ao menos uma vez antes do backup.",
                        nosaves=True, name=game["name"])
        write_info(folder, game)
        prune(folder, settings["max_backups"])
        size = path.stat().st_size
        return _res(True, f"Backup criado: {stats['files']} arquivos ({_fmt_size(size)}).",
                    filename=path.name, size=size, name=game["name"])
    except OSError as e:
        decky.logger.error(f"backup {appid} falhou: {e}")
        return _res(False, f"Falha ao gravar no microSD: {e}", name=game["name"])
    except Exception as e:  # noqa: BLE001
        decky.logger.exception(f"backup {appid}: erro inesperado")
        return _res(False, f"Erro inesperado: {e}", name=game["name"])


def _resolve_targets(appid: int, sources: List[Dict[str, Any]]) -> Dict[str, Tuple[Optional[Path], str]]:
    """chave -> (pasta_destino | None, motivo_se_None)."""
    targets: Dict[str, Tuple[Optional[Path], str]] = {}
    compat = find_compatdata(appid)
    users = compat / "pfx" / "drive_c" / "users" if compat else None
    prefix_missing = "prefixo do Proton ainda não existe (abra o jogo uma vez e restaure de novo)"
    uids = userdata_dirs()
    home = _home().resolve()

    for src in sources:
        key = str(src.get("key", ""))
        if key == "prefix_user":
            targets[key] = (users / "steamuser", "") if users and users.is_dir() else (None, prefix_missing)
        elif key == "prefix_public":
            targets[key] = (
                (users / "Public" / "Documents", "") if users and users.is_dir() else (None, prefix_missing)
            )
        elif key.startswith("userdata_"):
            uid = key[len("userdata_"):]
            match = next((u for u in uids if u.name == uid), None)
            if match is None and len(uids) == 1:
                match = uids[0]
            if match is None:
                targets[key] = (None, "conta Steam do backup não encontrada neste aparelho")
            else:
                targets[key] = (match / str(appid) / "remote", "")
        elif key.startswith("custom_"):
            cp = src.get("custom_path")
            try:
                resolved = Path(str(cp)).resolve()
                resolved.relative_to(home)
                targets[key] = (resolved, "")
            except (ValueError, OSError, TypeError):
                targets[key] = (None, "pasta personalizada fora da sua home")
        else:
            targets[key] = (None, "origem desconhecida")
    return targets


def _restore_one(appid: int, filename: str) -> Dict[str, Any]:
    if not BACKUP_FILE_RE.match(filename or ""):
        return _res(False, "Nome de backup inválido.")
    settings = load_settings()
    dest, err, _ = get_destination(settings)
    if dest is None:
        return _res(False, err)
    game = get_games().get(appid)
    if game is None:
        return _res(False, "Instale o jogo antes de restaurar o save.")
    if is_game_running(appid):
        return _res(False, "O jogo está em execução. Feche-o antes de restaurar.")
    folder = find_game_dir(dest, appid)
    archive = folder / filename if folder else None
    if archive is None or not archive.is_file():
        return _res(False, "Backup não encontrado no microSD.")

    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                mf = tar.extractfile("manifest.json")
                manifest = json.loads(mf.read().decode("utf-8")) if mf else {}
            except KeyError:
                return _res(False, "Arquivo de backup sem manifest.json (não foi criado por este plugin).")
            if int(manifest.get("appid", -1)) != appid:
                return _res(False, "Este backup pertence a outro jogo.")
            sources = [s for s in manifest.get("sources", []) if isinstance(s, dict)]
            targets = _resolve_targets(appid, sources)
            usable = {k for k, (p, _) in targets.items() if p is not None}
            if not usable:
                reasons = "; ".join(sorted({r for _, r in targets.values() if r}))
                return _res(False, f"Nada para restaurar: {reasons or 'sem destino válido'}.")

            # segurança: guarda o estado atual antes de sobrescrever
            try:
                create_archive(game, folder, settings["custom_paths"].get(str(appid), []),
                               prefix=PRE_RESTORE_PREFIX)
                prune(folder, settings["max_backups"])
            except Exception as e:  # noqa: BLE001
                decky.logger.warning(f"backup de segurança falhou: {e}")
                return _res(False, f"Não foi possível criar o backup de segurança dos saves atuais: {e}")

            restored = 0
            unsafe = 0
            real_roots = {k: os.path.realpath(p) for k, (p, _) in targets.items() if p is not None}
            for member in tar:
                if not member.isfile() or not member.name.startswith("data/"):
                    continue
                parts = member.name.split("/", 2)
                if len(parts) != 3 or parts[1] not in usable:
                    continue
                key, rel = parts[1], parts[2]
                rel_parts = rel.split("/")
                if rel.startswith("/") or ".." in rel_parts or "" in rel_parts:
                    unsafe += 1
                    continue
                base = targets[key][0]
                assert base is not None
                dest_file = base / rel
                real_dest = os.path.realpath(dest_file)
                if not (real_dest == real_roots[key] or real_dest.startswith(real_roots[key] + os.sep)):
                    unsafe += 1
                    continue
                src_f = tar.extractfile(member)
                if src_f is None:
                    continue
                _makedirs(dest_file.parent, base.parent)
                with src_f, open(dest_file, "wb") as out:
                    shutil.copyfileobj(src_f, out)
                try:
                    os.chmod(dest_file, (member.mode & 0o777) | 0o600)
                    os.utime(dest_file, (member.mtime, member.mtime))
                except OSError:
                    pass
                _chown(dest_file)
                restored += 1
    except (tarfile.TarError, EOFError, OSError, ValueError) as e:
        decky.logger.error(f"restore {appid} falhou: {e}")
        return _res(False, f"Falha ao restaurar: {e}")
    except Exception as e:  # noqa: BLE001
        decky.logger.exception(f"restore {appid}: erro inesperado")
        return _res(False, f"Erro inesperado: {e}")

    skipped = sorted({r for k, (p, r) in targets.items() if p is None and r})
    msg = f"Save restaurado: {restored} arquivos."
    if skipped:
        msg += " Ignorado: " + "; ".join(skipped) + "."
    if unsafe:
        msg += f" {unsafe} arquivo(s) com caminho inseguro foram ignorados."
    msg += " (Um backup de segurança do estado anterior foi guardado no microSD.)"
    return _res(restored > 0, msg if restored > 0 else "Nenhum arquivo foi restaurado. " + msg,
                restored=restored)


def _delete_backup(appid: int, filename: str) -> Dict[str, Any]:
    if not BACKUP_FILE_RE.match(filename or ""):
        return _res(False, "Nome de backup inválido.")
    dest, err, _ = get_destination(load_settings())
    if dest is None:
        return _res(False, err)
    folder = find_game_dir(dest, appid)
    if folder is None or not (folder / filename).is_file():
        return _res(False, "Backup não encontrado.")
    try:
        (folder / filename).unlink()
        if not list_backup_files(folder):  # pasta vazia: limpa
            shutil.rmtree(folder, ignore_errors=True)
    except OSError as e:
        return _res(False, f"Não foi possível excluir: {e}")
    return _res(True, "Backup excluído.")


def _list_backups(appid: int) -> List[Dict[str, Any]]:
    dest, _, _ = get_destination(load_settings())
    if dest is None:
        return []
    folder = find_game_dir(dest, appid)
    return list_backup_files(folder) if folder else []


def _games_with_data() -> List[Dict[str, Any]]:
    return sorted(
        (g for g in get_games().values() if has_game_data(g["appid"])),
        key=lambda g: g["name"].lower(),
    )


def _scan() -> Dict[str, Any]:
    settings = load_settings()
    dest, err, sd = get_destination(settings)
    games = get_games()
    backups = scan_backup_dirs(dest) if dest else {}

    out_games: List[Dict[str, Any]] = []
    for appid, g in games.items():
        files = [b for b in (backups.get(appid, {}).get("backups") or []) if not b["pre_restore"]]
        out_games.append({
            "appid": appid,
            "name": g["name"],
            "kind": g["kind"],
            "has_data": has_game_data(appid),
            "backup_count": len(files),
            "last_backup": files[0]["ts"] if files else None,
            "backup_size": sum(b["size"] for b in files),
        })
    out_games.sort(key=lambda x: x["name"].lower())

    orphans: List[Dict[str, Any]] = []
    for appid, b in backups.items():
        if appid in games:
            continue
        files = [x for x in b["backups"] if not x["pre_restore"]]
        if files:
            orphans.append({"appid": appid, "name": b["name"], "backup_count": len(files),
                            "last_backup": files[0]["ts"]})
    orphans.sort(key=lambda x: x["name"].lower())

    return {
        "sd": sd,
        "sd_error": "" if dest else err,
        "steam_found": bool(steam_roots()),
        "games": out_games,
        "orphans": orphans,
        "settings": {"max_backups": settings["max_backups"], "destination": settings["destination"],
                     "auto_backup": settings["auto_backup"], "auto_mount": settings["auto_mount"]},
    }


# --------------------------------------------------------------------------
# API exposta ao frontend
# --------------------------------------------------------------------------

# ---------------------------------------------------------------- atualizações

_update_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def current_version() -> str:
    try:
        data = json.loads((Path(decky.DECKY_PLUGIN_DIR) / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "0.0.0")
    except (OSError, ValueError, AttributeError):
        return "0.0.0"


def parse_version(v: str) -> Tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", v)][:4]
    return tuple(nums + [0] * (3 - len(nums)))


def pick_latest(releases: Any) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_v: Tuple[int, ...] = ()
    if not isinstance(releases, list):
        return None
    for r in releases:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        tag = str(r.get("tag_name", ""))
        if not tag.startswith(UPDATE_TAG_PREFIX):
            continue
        ver = parse_version(tag[len(UPDATE_TAG_PREFIX):])
        if best is None or ver > best_v:
            best, best_v = r, ver
    return best


def _http_json(url: str, token: str) -> Tuple[int, Any]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "SaveBackupSD-updater",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return 0, None


def check_update(force: bool = False) -> Dict[str, Any]:
    settings = load_settings()
    cur = current_version()
    token = settings["github_token"]
    base: Dict[str, Any] = {"current": cur, "latest": cur, "available": False, "url": "",
                            "error": "", "has_token": bool(token), "notified": False}
    now = time.time()
    cached = _update_cache["data"]
    if not force and cached and now - _update_cache["ts"] < UPDATE_CACHE_TTL and cached.get("current") == cur:
        out = dict(cached)
        out["has_token"] = bool(token)
        out["notified"] = bool(out["available"] and settings["notified_version"] == out["latest"])
        return out

    repo = settings["update_repo"]
    status, data = _http_json(f"https://api.github.com/repos/{repo}/releases?per_page=30", token)
    if status == 200:
        rel = pick_latest(data)
        if rel:
            latest = str(rel["tag_name"])[len(UPDATE_TAG_PREFIX):]
            base["latest"] = latest
            base["url"] = str(rel.get("html_url", ""))
            base["available"] = parse_version(latest) > parse_version(cur)
    elif status == 0:
        base["error"] = "Sem conexão com a internet."
    elif status in (401, 404) and not token:
        base["error"] = "Repositório privado: informe um token do GitHub (somente leitura) para checar atualizações."
    elif status in (401, 404):
        base["error"] = "Token inválido ou sem acesso ao repositório."
    elif status == 403:
        base["error"] = "Limite de requisições do GitHub atingido. Tente mais tarde."
    else:
        base["error"] = f"GitHub respondeu com erro {status}."
    if not base["error"]:
        _update_cache["ts"], _update_cache["data"] = now, dict(base)
    base["notified"] = bool(base["available"] and settings["notified_version"] == base["latest"])
    return base


_watch_task: Optional["asyncio.Task[None]"] = None
_auto_inflight: set = set()


async def _auto_backup(appid: int) -> None:
    try:
        await asyncio.sleep(AUTO_DELAY)
        if await _run(is_game_running, appid):
            return  # o jogo foi reaberto
        if not (await _run(load_settings)).get("auto_backup"):
            return
        async with _get_lock():
            r = await _run(_backup_one, appid, True)
        if r.get("notfound") or r.get("nosaves") or r.get("unchanged"):
            return
        await _emit("auto_backup", appid, r.get("name", str(appid)), bool(r["ok"]), r["message"])
    except Exception:  # noqa: BLE001
        decky.logger.exception(f"backup automático {appid} falhou")
    finally:
        _auto_inflight.discard(appid)


async def _watch() -> None:
    prev: Optional[set] = None
    while True:
        try:
            await asyncio.sleep(WATCH_INTERVAL)
            if not (await _run(load_settings)).get("auto_backup"):
                prev = None
                continue
            cur = await _run(running_appids)
            if prev is not None:
                for appid in prev - cur:
                    if appid not in _auto_inflight:
                        _auto_inflight.add(appid)
                        asyncio.get_running_loop().create_task(_auto_backup(appid))
            prev = cur
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            decky.logger.exception("falha no monitor de jogos")


class Plugin:
    async def scan(self) -> Dict[str, Any]:
        return await _run(_scan)

    async def list_backups(self, appid: int) -> List[Dict[str, Any]]:
        return await _run(_list_backups, int(appid))

    async def backup_game(self, appid: int) -> Dict[str, Any]:
        async with _get_lock():
            return await _run(_backup_one, int(appid))

    async def backup_all(self) -> Dict[str, Any]:
        async with _get_lock():
            dest, err, _ = await _run(lambda: get_destination(load_settings()))
            if dest is None:
                return _res(False, err)
            candidates = await _run(_games_with_data)
            ok_count = 0
            nosaves = 0
            failed: List[str] = []
            total = len(candidates)
            for i, g in enumerate(candidates, start=1):
                await _emit("backup_progress", i, total, g["name"])
                r = await _run(_backup_one, g["appid"])
                if r["ok"]:
                    ok_count += 1
                elif r.get("nosaves"):
                    nosaves += 1
                else:
                    failed.append(g["name"])
            msg = f"{ok_count} de {total} jogos salvos no microSD."
            if nosaves:
                msg += f" {nosaves} sem saves encontrados."
            if failed:
                msg += f" Falhas: {', '.join(failed[:5])}" + ("…" if len(failed) > 5 else "") + "."
            return _res(not failed, msg, done=ok_count, total=total, failed=failed)

    async def restore_game(self, appid: int, filename: str) -> Dict[str, Any]:
        async with _get_lock():
            return await _run(_restore_one, int(appid), str(filename))

    async def delete_backup(self, appid: int, filename: str) -> Dict[str, Any]:
        async with _get_lock():
            return await _run(_delete_backup, int(appid), str(filename))

    async def set_max_backups(self, value: int) -> Dict[str, Any]:
        settings = load_settings()
        settings["max_backups"] = max(1, min(50, int(value)))
        await _run(save_settings, settings)
        return {"max_backups": settings["max_backups"], "destination": settings["destination"]}

    async def get_update_status(self, force: bool = False) -> Dict[str, Any]:
        return await _run(check_update, bool(force))

    async def ack_update(self, version: str) -> bool:
        settings = load_settings()
        settings["notified_version"] = str(version)
        await _run(save_settings, settings)
        return True

    async def set_github_token(self, token: str) -> bool:
        settings = load_settings()
        settings["github_token"] = str(token).strip()
        settings["notified_version"] = ""
        _update_cache["data"] = None
        await _run(save_settings, settings)
        return True

    async def mount_sd(self) -> Dict[str, Any]:
        async with _get_lock():
            ok, msg = await _run(mount_sd, True)
            if not ok and msg.startswith("Nenhum cartão desmontado"):
                found = await _run(find_sd_mounts)
                if found:
                    return _res(True, "O microSD já está montado.")
            return _res(ok, msg)

    async def set_auto_mount(self, enabled: bool) -> Dict[str, Any]:
        settings = load_settings()
        settings["auto_mount"] = bool(enabled)
        await _run(save_settings, settings)
        return {"auto_mount": settings["auto_mount"]}

    async def set_auto_backup(self, enabled: bool) -> Dict[str, Any]:
        settings = load_settings()
        settings["auto_backup"] = bool(enabled)
        await _run(save_settings, settings)
        return {"auto_backup": settings["auto_backup"]}

    async def _main(self):
        global _watch_task
        decky.logger.info("SaveBackupSD iniciado")
        _watch_task = asyncio.get_running_loop().create_task(_watch())

    async def _unload(self):
        if _watch_task is not None:
            _watch_task.cancel()
        decky.logger.info("SaveBackupSD descarregado")

    async def _uninstall(self):
        decky.logger.info("SaveBackupSD desinstalado (os backups no microSD são mantidos)")

