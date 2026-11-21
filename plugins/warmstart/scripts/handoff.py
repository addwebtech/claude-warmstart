#!/usr/bin/env python3
"""Handoff - motor sin tokens para retomar proyectos de Claude Code sin pagar el re-cacheo.

Solo biblioteca estandar (Python 3.8+). Funciona igual en Windows, macOS y Linux.

Modos (primer argumento):
  guard-prompt   Hook UserPromptSubmit: frena el mensaje que recargaria una conversacion grande con cache expirado.
  guard-model    Hook PreModelSwitch: frena el cambio de modelo en una conversacion grande con cache caliente.
  session-start  Hook SessionStart: inyecta el handoff al abrir una conversacion nueva; avisa el costo al reanudar una vieja.
  snapshot       Hooks Stop / PreCompact / SessionEnd: guarda un resumen mecanico de la sesion (incremental).
  preparar       Skill save: snapshot + respaldos + datos para escribir HANDOFF.md.
  guardar        Skill save: recibe HANDOFF.md por stdin, rechaza claves, lo escribe y lo verifica.
  verificar      Skill save: comprueba HANDOFF.md y que el archivo de estado no se haya encogido.
  contexto       Skill resume: imprime el contexto compacto.
  ahorro         Skill savings: mide en los registros reales cuanto se gasto y cuanto se ahorra.
  instalar       Agrega los hooks (y copia el motor y los skills). --simular para ver sin escribir;
                 --ambito proyecto --proyecto RUTA para probar sin tocar tu configuracion global.
  desinstalar    Quita hooks y skills (conserva tus datos).

Principio de diseno: los hooks NUNCA rompen la sesion. Ante cualquier error, salen en silencio (codigo 0)
y lo anotan en ~/.claude/handoff/errores.log.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "4.0.0"
HOME = Path(os.environ.get("HANDOFF_HOME") or os.path.expanduser("~"))
# WARMSTART_DATA_DIR separa los datos de Warmstart (eventos, estado, config) de los registros de Claude Code; lo usan las pruebas e2e.
RAIZ = Path(os.environ["WARMSTART_DATA_DIR"]) if os.environ.get("WARMSTART_DATA_DIR") else HOME / ".claude" / "handoff"
DIR_ESTADO = RAIZ / "estado"
CFG = {
    "umbralTokens": 120000,     # a partir de este tamano de contexto actuan las guardias
    "avisoTokens": 200000,      # al cruzarlo, Claude sugiere una vez /handoff-save
    "minutosOverride": 5,       # ventana para "reenvia y pasa"
    "baseArranque": 55000,      # arranque de conversacion nueva (se re-mide solo)
    "guardiaPrompt": True,
    "guardiaModelo": True,
    "inyectarAlInicio": True,
    "limiteIdleMin": None,      # solo pruebas: minutos de pausa que cuentan como "cache frio" (None = segun el TTL real)
    "ahorroMinimoPct": 25,      # las guardias no actuan si una conversacion nueva ahorraria menos que esto
    "idioma": "auto",           # "es", "en" o "auto" (segun el idioma del sistema; env WARMSTART_LANG manda)
}
try:
    CFG.update(json.loads((RAIZ / "config.json").read_text(encoding="utf-8-sig")))
except Exception:
    pass
for _k, _e in (("umbralTokens", "WARMSTART_UMBRAL_TOKENS"), ("avisoTokens", "WARMSTART_AVISO_TOKENS"), ("limiteIdleMin", "WARMSTART_IDLE_MIN"), ("ahorroMinimoPct", "WARMSTART_AHORRO_MIN_PCT")):
    try:
        if os.environ.get(_e):
            CFG[_k] = float(os.environ[_e])
    except ValueError:
        pass

MODO = ""
UTC = dt.timezone.utc
# Claves con formato conocido: se ENMASCARAN en lo que se guarda y se RECHAZAN en HANDOFF.md.
SECRETOS = [re.compile(p) for p in (
    r"sk-[A-Za-z0-9_\-]{20,}", r"ghp_[A-Za-z0-9]{20,}", r"gho_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[bpas]-[A-Za-z0-9\-]{10,}", r"AKIA[0-9A-Z]{16}", r"AIza[0-9A-Za-z_\-]{35}", r"ya29\.[A-Za-z0-9_\-]{20,}",
    r"npm_[A-Za-z0-9]{30,}", r"[sr]k_live_[A-Za-z0-9]{16,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+",
    r"Bearer\s+[A-Za-z0-9._\-]{20,}", r"\b[a-f0-9]{48,}\b", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@")]            # URL con usuario:clave@
# Patrones genericos (mas propensos a falsos positivos): solo se ENMASCARAN, nunca hacen fallar un guardado.
GENERICOS = [re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|clave|contrase[ñn]a)\b\s*[:=]\s*[\"']?[^\s\"']{4,}")]
ARCHIVOS_ESTADO = ("ESTADO.md", "ESTADOS.md", "PROGRESO.md", "estado.md", "progreso.md")   # convencion de cada usuario: opcional
ARCHIVO_MEMORIA = "CLAUDE.md"           # Claude Code ya lo carga solo al abrir: nunca se re-inyecta ni se edita por su cuenta


# ---------------------------------------------------------------- utilidades
def log_error(e):
    try:
        RAIZ.mkdir(parents=True, exist_ok=True)
        with open(RAIZ / "errores.log", "a", encoding="utf-8") as f:
            tb = e.__traceback__
            linea = tb.tb_lineno if tb else "?"
            f.write("%s [%s] %s: %s (linea %s)\n" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), MODO, type(e).__name__, str(e).replace("\n", " "), linea))
    except Exception:
        pass


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if os.name != "nt":
        try:
            os.chmod(str(path), 0o600)       # lo guardado es tuyo: solo tu usuario lo lee
        except OSError:
            pass


def read_text(path):
    return Path(path).read_text(encoding="utf-8-sig", errors="replace")


def read_stdin():
    """Tolerante: ignora BOM u otra basura antes de la primera llave."""
    try:
        t = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        i = t.find("{")
        if i > 0:
            t = t[i:]
        if t.strip():
            return json.loads(t)
    except Exception as e:
        log_error(e)
    return None


_TS = re.compile(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def parse_ts(s):
    if not s:
        return None
    m = _TS.match(str(s).strip())
    if not m:
        return None
    frac = (m.group(3) or ".0")[:7]
    tz = m.group(4)
    base = "%sT%s%s" % (m.group(1), m.group(2), frac if m.group(3) else "")
    d = dt.datetime.fromisoformat(base) if m.group(3) is None else dt.datetime.fromisoformat(base.ljust(26, "0")[:26])
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        d = d - sign * dt.timedelta(hours=hh, minutes=mm)
    return d.replace(tzinfo=UTC)


def now_utc():
    return dt.datetime.now(UTC)


def fmt_tok(n):
    n = float(n)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%dk" % round(n / 1e3)
    return "%d" % round(n)


def fmt_dur(minutos):
    m = float(minutos)
    if m >= 1440:
        return "%d d %d h" % (m // 1440, (m % 1440) // 60)
    if m >= 60:
        return "%d h %d min" % (m // 60, round(m % 60))
    return "%d min" % round(m)


def short(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n] + "..."


def hide_secrets(t):
    t = t or ""
    for rx in SECRETOS + GENERICOS:
        t = rx.sub("[CLAVE OCULTA]", t)
    return t


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.flush()


def cmd(nombre):
    """Nombre del comando segun como se instalo: plugin (/warmstart:save) o copia manual (/warmstart-save)."""
    sep = "-" if Path(__file__).resolve().parent.name in ("handoff", "warmstart") else ":"
    return "/warmstart%s%s" % (sep, nombre)


_IDIOMA = []


def idioma():
    """'es' o 'en' para los mensajes que ve la persona. WARMSTART_LANG > config.json > idioma del sistema > 'en'."""
    if not _IDIOMA:
        v = str(os.environ.get("WARMSTART_LANG") or CFG.get("idioma") or "auto").strip().lower()[:2]
        if v not in ("es", "en"):
            v = ""
            for k in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
                e = os.environ.get(k)
                if e and e not in ("C", "POSIX"):
                    v = "es" if e.lower().startswith("es") else "en"
                    break
            if not v and os.name == "nt":
                try:
                    import ctypes
                    buf = ctypes.create_unicode_buffer(85)
                    ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85)
                    v = "es" if buf.value.lower().startswith("es") else "en"
                except Exception:
                    pass
        _IDIOMA.append(v or "en")
    return _IDIOMA[0]


def T(es, en):
    return es if idioma() == "es" else en


def pct_ahorro(ctx):
    """% que ahorraria abrir una conversacion nueva (arranque medido) en vez de re-enviar `ctx` tokens."""
    return (1 - get_base() / ctx) * 100 if ctx > 0 else 0


def get_base():
    try:
        v = int((DIR_ESTADO / "base-arranque.txt").read_text().strip())
        if v > 5000:
            return v
    except Exception:
        pass
    return int(CFG["baseArranque"])


def set_base(v):
    if v < 25000:
        return
    nuevo = int((get_base() * 3 + v) / 4)
    write_text(DIR_ESTADO / "base-arranque.txt", str(nuevo))


def log_evento(proyecto, sid, tipo, tokens, decision):
    try:
        f = RAIZ / "eventos.csv"
        nuevo = not f.exists()
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            if nuevo:
                w.writerow(["fecha", "proyecto", "sesion", "tipo", "tokens", "base", "decision"])
            w.writerow([dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), proyecto, sid, tipo, int(tokens), get_base(), decision])
    except Exception:
        pass


def project_dir(inp, args):
    if args.get("proyecto"):
        return args["proyecto"].rstrip("\\/")
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return os.environ["CLAUDE_PROJECT_DIR"].rstrip("\\/")
    if inp and inp.get("cwd"):
        return str(inp["cwd"]).rstrip("\\/")
    return os.getcwd().rstrip("\\/")


def _norm(p):
    return re.sub(r"[\\/]+", "/", str(p)).rstrip("/").lower()


def find_sessions_dir(proj):
    """Carpeta de registros de un proyecto. Primero la regla conocida (simbolos -> guiones); si no
    aparece (otro sistema o version), busca por el 'cwd' escrito dentro de los registros."""
    base = HOME / ".claude" / "projects"
    if not base.is_dir():
        return None
    enc = re.sub(r"[^a-zA-Z0-9]", "-", proj)
    dirs = [d for d in base.iterdir() if d.is_dir()]
    for d in dirs:
        if d.name.lower() == enc.lower():
            return d
    objetivo = _norm(proj)
    for d in dirs:
        try:
            recientes = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:1]
            for f in recientes:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for _ in range(60):
                        linea = fh.readline()
                        if not linea:
                            break
                        m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', linea)
                        if m and _norm(json.loads('"%s"' % m.group(1))) == objetivo:
                            return d
        except Exception:
            continue
    return None


def latest_transcript(proj):
    d = find_sessions_dir(proj)
    if not d:
        return None
    fs = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(fs[0]) if fs else None


def read_tail_lines(path, nbytes):
    with open(path, "rb") as fh:
        size = fh.seek(0, 2)
        start = max(0, size - nbytes)
        fh.seek(start)
        data = fh.read()
    lineas = data.decode("utf-8", "replace").split("\n")
    if start > 0 and len(lineas) > 1:
        lineas = lineas[1:]
    return lineas


_R_TS = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
_R_INP = re.compile(r'"input_tokens"\s*:\s*(\d+)')
_R_CW = re.compile(r'"cache_creation_input_tokens"\s*:\s*(\d+)')
_R_CR = re.compile(r'"cache_read_input_tokens"\s*:\s*(\d+)')
_R_OUT = re.compile(r'"output_tokens"\s*:\s*(\d+)')
_R_MODEL = re.compile(r'"model"\s*:\s*"([^"]+)"')
_R_5M = re.compile(r'"ephemeral_5m_input_tokens"\s*:\s*(\d+)')
_R_1H = re.compile(r'"ephemeral_1h_input_tokens"\s*:\s*(\d+)')
_R_ASST = re.compile(r'"type"\s*:\s*"assistant"')
_R_SIDE = re.compile(r'"isSidechain"\s*:\s*true')


def _num(rx, s):
    m = rx.search(s)
    return int(m.group(1)) if m else 0


def get_last_usage(path):
    """Ultima respuesta con 'usage' del hilo principal: fecha, tamano de contexto, modelo, TTL del cache."""
    for kb in (1024, 8192):
        ultimo = None
        for l in reversed(read_tail_lines(path, kb * 1024)):
            if '"usage"' not in l or not _R_ASST.search(l) or _R_SIDE.search(l):
                continue
            m = _R_MODEL.search(l)
            model = m.group(1) if m else ""
            if model == "<synthetic>":
                continue
            m5, m1 = _num(_R_5M, l), _num(_R_1H, l)
            if ultimo is None:
                ts = _R_TS.search(l)
                ultimo = {"ts": parse_ts(ts.group(1)) if ts else None, "model": model, "ttl": None,
                          "ctx": float(_num(_R_INP, l) + _num(_R_CW, l) + _num(_R_CR, l) + _num(_R_OUT, l))}
            # El TTL (5 min o 1 h) solo se ve en respuestas que ESCRIBIERON cache: se toma la mas reciente que lo muestre.
            # (En suscripcion el TTL puede bajar de 1 h a 5 min sin aviso; por eso no se supone.)
            if m5 + m1 > 0:
                ultimo["ttl"] = 5 if m1 == 0 else 60
                break
        if ultimo is not None:
            if ultimo["ttl"] is None:
                ultimo["ttl"] = 60      # sin evidencia: 1 h (lo habitual hoy); es la suposicion menos agresiva
            return ultimo
    return None


def test_override(nombre, huella=""):
    """True si hay un bloqueo reciente del MISMO mensaje (misma huella). Se consume siempre."""
    f = DIR_ESTADO / nombre
    try:
        if f.exists():
            edad = (time.time() - f.stat().st_mtime) / 60
            guardada = f.read_text(encoding="utf-8").strip()
            f.unlink()
            return edad <= float(CFG["minutosOverride"]) and guardada == huella
    except Exception:
        pass
    return False


def set_override(nombre, huella=""):
    write_text(DIR_ESTADO / nombre, huella)


def huella_de(texto):
    import hashlib
    return hashlib.sha256((texto or "").encode("utf-8", "replace")).hexdigest()[:16]


def start_snapshot_detached(proj, tp):
    try:
        cmd = [sys.executable, os.path.abspath(__file__), "snapshot", "--proyecto", proj, "--transcript", tp]
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        subprocess.Popen(cmd, **kw)
    except Exception as e:
        log_error(e)


class Lock:
    """Candado por archivo (portable); un candado huerfano de mas de 60 s se ignora."""
    def __init__(self, path, espera=20):
        self.path, self.espera, self.ok = Path(path), espera, False

    def __enter__(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fin = time.time() + self.espera
        while time.time() < fin:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self.ok = True
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 60:
                        self.path.unlink()
                        continue
                except Exception:
                    pass
                time.sleep(0.2)
        return self

    def __exit__(self, *a):
        if self.ok:
            try:
                self.path.unlink()
            except Exception:
                pass


def ensure_handoff_dir(proj):
    """Carpeta de datos del proyecto. Lleva su propio .gitignore ('*') para que nada se suba por error a git."""
    hdir = Path(proj) / ".claude" / "handoff"
    for sub in ("estado", "sesiones"):
        (hdir / sub).mkdir(parents=True, exist_ok=True)
    gi = hdir / ".gitignore"
    if not gi.exists():
        write_text(gi, "# Handoff guarda tus pedidos de la sesion: no se versiona.\n*\n")
    return hdir


def is_user_prompt(t):
    s = (t or "").lstrip()
    if not s:
        return False
    for p in ("<", "Base directory for this skill", "[Image", "Caveat:", "This session is being continued", "[Request interrupted"):
        if s.startswith(p):
            return False
    return True


# ---------------------------------------------------------------- snapshot
def invoke_snapshot(proj, tp, inp=None):
    if not tp or not os.path.exists(tp):
        return None
    sid = Path(tp).stem
    hdir = ensure_handoff_dir(proj)
    with Lock(hdir / "estado" / (sid + ".lock")) as lk:
        if not lk.ok:
            return None
        stp = hdir / "estado" / (sid + ".json")
        S = {"offset": 0, "archivos": {}, "pedidos": [], "tareas": {}, "externos": {}, "inicio": None, "ultimo": None,
             "modelo": "", "ctx": 0, "ultimaRespuesta": "", "baseMedida": False}
        try:
            S.update(json.loads(stp.read_text(encoding="utf-8")))
        except Exception:
            pass
        size = os.path.getsize(tp)
        if S["offset"] > size:
            S["offset"] = 0
        pos = S["offset"]
        with open(tp, "rb") as fh:
            fh.seek(pos)
            while True:
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break          # linea a medio escribir: se procesa en la proxima pasada
                pos += len(raw)
                if len(raw) < 20:
                    continue
                line = raw.decode("utf-8", "replace")
                if not re.search(r'"type"\s*:\s*"(assistant|user)"', line) or _R_SIDE.search(line):
                    continue
                es_user = bool(re.search(r'"type"\s*:\s*"user"', line))
                if es_user and '"tool_result"' in line and not re.search(r'"type"\s*:\s*"text"', line):
                    continue
                if len(line) > 2000000:
                    for m in re.finditer(r'"file_path"\s*:\s*"([^"]+)"', line):
                        fp = m.group(1).replace("\\\\", "\\")
                        S["archivos"][fp] = S["archivos"].get(fp, 0) + 1
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                ts = parse_ts(o.get("timestamp"))
                if ts:
                    S["inicio"] = S["inicio"] or ts.isoformat()
                    S["ultimo"] = ts.isoformat()
                content = msg.get("content")
                if o.get("type") == "assistant":
                    if msg.get("model") and msg["model"] != "<synthetic>":
                        S["modelo"] = msg["model"]
                    u = msg.get("usage")
                    if isinstance(u, dict):
                        c = sum(float(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
                        if c > 0:
                            if not S["baseMedida"]:
                                S["baseMedida"] = True
                                if c < 150000:
                                    set_base(int(c))
                            S["ctx"] = c
                    for b in (content if isinstance(content, list) else []):
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and b.get("text"):
                            S["ultimaRespuesta"] = b["text"]
                        elif b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                            n, inpt = b.get("name", ""), b["input"]
                            if n in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                                fp = inpt.get("file_path") or inpt.get("notebook_path")
                                if fp:
                                    S["archivos"][str(fp)] = S["archivos"].get(str(fp), 0) + 1
                            elif n == "TodoWrite" and isinstance(inpt.get("todos"), list):
                                S["tareas"] = {short(hide_secrets(str(t.get("content", ""))), 140): str(t.get("status", "")) for t in inpt["todos"] if isinstance(t, dict)}
                            elif n == "TaskCreate" and inpt.get("subject"):
                                S["tareas"][short(hide_secrets(str(inpt["subject"])), 140)] = "pending"
                            elif n.startswith("mcp__"):
                                ids = ["%s=%s" % (k, v) for k, v in inpt.items() if re.search(r"(?i)id$|name$", k) and isinstance(v, str) and len(v) <= 80]
                                S["externos"][n] = short(hide_secrets(", ".join(ids)), 160)
                elif o.get("type") == "user" and not o.get("isMeta"):
                    txt = ""
                    if isinstance(content, str):
                        txt = content
                    elif isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text" and is_user_prompt(b.get("text")):
                                txt = b["text"]
                                break
                    if is_user_prompt(txt):
                        S["pedidos"].append({"t": ts.isoformat() if ts else "", "x": short(hide_secrets(txt), 400)})
                        S["pedidos"] = S["pedidos"][-8:]
        S["offset"] = pos
        if inp and inp.get("last_assistant_message"):
            S["ultimaRespuesta"] = str(inp["last_assistant_message"])
        S["ultimaRespuesta"] = short(hide_secrets(S["ultimaRespuesta"]), 2500)
        write_text(stp, json.dumps(S, ensure_ascii=False))

        # ---- render
        pn = _norm(proj)
        ini, fin = parse_ts(S["inicio"]), parse_ts(S["ultimo"])
        loc = lambda d, f: d.astimezone().strftime(f) if d else "?"
        md = ["# Handoff automatico (generado sin tokens)",
              "Sesion `%s` | %s -> %s | modelo %s | contexto ~%s tokens" % (sid[:8], loc(ini, "%Y-%m-%d %H:%M"), loc(fin, "%Y-%m-%d %H:%M"), S["modelo"], fmt_tok(S["ctx"])),
              "", "## Ultimos pedidos del usuario (el mas reciente al final)"]
        for p in S["pedidos"]:
            pt = parse_ts(p["t"])
            md.append("- [%s] %s" % (loc(pt, "%m-%d %H:%M") if pt else "", p["x"]))
        md += ["", "## Ultima respuesta de Claude", "> " + short(S["ultimaRespuesta"], 900).replace("\n", "\n> ")]
        if S["tareas"]:
            md += ["", "## Lista de tareas (ultimo estado)"] + ["- [%s] %s" % (v, k) for k, v in S["tareas"].items()]
        arch = sorted(((k, v) for k, v in S["archivos"].items() if not re.search(r"[\\/]Temp[\\/]claude[\\/]", k)), key=lambda kv: -kv[1])
        md += ["", "## Archivos creados/editados en la sesion (%d)" % len(arch)]
        for k, v in arch[:40]:
            r = re.sub(r"[\\/]+", "/", k)
            if r.lower().startswith(pn + "/"):
                r = r[len(pn) + 1:]
            md.append("- %s (x%d)" % (r, v))
        if len(arch) > 40:
            md.append("- ... y %d mas" % (len(arch) - 40))
        if S["externos"]:
            md += ["", "## Herramientas externas usadas (ultimo uso, IDs)"] + ["- %s: %s" % (k, v) for k, v in S["externos"].items()]
        txt = "\n".join(md) + "\n"
        fecha = ini.astimezone().strftime("%Y-%m-%d") if ini else dt.date.today().isoformat()
        write_text(hdir / "sesiones" / ("%s-%s.md" % (fecha, sid[:8])), txt)
        write_text(hdir / "auto.md", txt)
        return {"sid": sid, "texto": txt, "ctx": S["ctx"], "archivos": S["archivos"]}


# ---------------------------------------------------------------- contexto
def get_state_files(proj, con_memoria=False):
    """Archivos de estado que el usuario mantiene (ESTADO.md, PROGRESO.md...). CLAUDE.md solo si `con_memoria`
    (para respaldarlo); nunca se lista al inyectar porque Claude Code ya lo carga solo."""
    r = []
    vistos = set()
    for c in ARCHIVOS_ESTADO + ((ARCHIVO_MEMORIA,) if con_memoria else ()):
        p = Path(proj) / c
        if p.exists() and str(p).lower() not in vistos:
            vistos.add(str(p).lower())
            r.append(p)
    return r


def build_contexto(proj, sid_actual="", completo=False):
    hdir = Path(proj) / ".claude" / "handoff"
    hf, af = hdir / "HANDOFF.md", hdir / "auto.md"
    has_h, has_a = hf.exists(), af.exists()
    if not has_h and not has_a:
        return None
    sb = [T("=== HANDOFF del proyecto (inyectado por hook; NO hace falta releer el historial) ===",
            "=== Project HANDOFF (injected by hook; no need to re-read the history) ==="),
          T("INSTRUCCION: si el primer mensaje del usuario es vago (hola, sigue, continua), contesta en UNA linea con la siguiente accion y empieza a trabajar en ella. Si es una tarea concreta, haz esa tarea.",
            "INSTRUCTION: if the user's first message is vague (hi, continue, go on), answer in ONE line with the next action and start working on it. If it is a concrete task, do that task.")]
    h_time = None
    if has_h:
        h_time = hf.stat().st_mtime
        sb.append(T("--- .claude/handoff/HANDOFF.md (guardado %s) ---", "--- .claude/handoff/HANDOFF.md (saved %s) ---") % dt.datetime.fromtimestamp(h_time).strftime("%Y-%m-%d %H:%M"))
        sb.append(read_text(hf).strip())
    if has_a:
        a_txt = read_text(af)
        a_time = af.stat().st_mtime
        misma = bool(sid_actual) and sid_actual[:8] in a_txt
        if not misma and (not has_h or a_time > h_time + 120):
            sb.append("")
            sb.append((T("--- OJO: hubo trabajo DESPUES del ultimo guardado (%s). Resumen automatico de esa sesion: ---",
                         "--- NOTE: there was work AFTER the last save (%s). Automatic summary of that session: ---") if has_h
                       else T("--- Todavia no hay guardado (%s). Resumen automatico de la ultima sesion: ---",
                              "--- Nothing saved yet (%s). Automatic summary of the last session: ---")) % cmd("save"))
            if completo:
                sb.append(a_txt.strip())
            else:
                sec, n = "", 0
                for l in a_txt.split("\n"):
                    if l.startswith("## "):
                        sec, n = l, 0
                    if sec.startswith("## Archivos"):
                        n += 1
                        if n > 16:
                            continue
                    sb.append(l.rstrip())
    estados = get_state_files(proj)
    if estados:
        sb.append("")
        sb.append(T("Archivos de estado del proyecto (leer solo las secciones que la tarea necesite): ",
                    "Project state files (read only the sections the task needs): ") + ", ".join("%s (%.0f KB)" % (p.name, p.stat().st_size / 1024) for p in estados))
    sb.append(T("Historial completo de sesiones anteriores: .claude/handoff/sesiones/ e historial/ (consultar con grep, no cargar entero).",
                "Full history of earlier sessions: .claude/handoff/sesiones/ and historial/ (search with grep, do not load whole)."))
    sb.append(T("=== fin HANDOFF ===", "=== end HANDOFF ==="))
    return "\n".join(sb) + "\n"


# ---------------------------------------------------------------- modos
def modo_guard_prompt(args):
    if not CFG["guardiaPrompt"]:
        return
    inp = read_stdin()
    if not inp:
        return
    tp = str(inp.get("transcript_path") or "")
    if not tp or not os.path.exists(tp):
        return
    proj, sid = project_dir(inp, args), str(inp.get("session_id") or "")
    prompt = str(inp.get("prompt") or "")
    if prompt and not is_user_prompt(prompt):
        return          # avisos automaticos (tareas en segundo plano, etc.): nadie los escribio, no se frenan
    if prompt.strip().lower().startswith(("/warmstart:", "/warmstart-")):
        return          # nuestros propios comandos (p. ej. guardar) son justo lo que la guardia recomienda: nunca se frenan
    huella = huella_de(prompt)
    u = get_last_usage(tp)
    if not u or not u["ts"]:
        return
    idle = (now_utc() - u["ts"]).total_seconds() / 60
    limite = CFG["limiteIdleMin"] or (58 if u["ttl"] >= 60 else u["ttl"] - 2)
    if idle >= limite and u["ctx"] >= CFG["umbralTokens"]:
        if pct_ahorro(u["ctx"]) < CFG["ahorroMinimoPct"]:
            return      # una conversacion nueva no ahorraria lo bastante: no estorbar
        if test_override("override-prompt-" + sid, huella):
            log_evento(proj, sid, "pausa", u["ctx"], "forzado")
            return
        set_override("override-prompt-" + sid, huella)
        start_snapshot_detached(proj, tp)
        log_evento(proj, sid, "pausa", u["ctx"], "bloqueado")
        base = get_base()
        pct = round((1 - base / u["ctx"]) * 100)
        msg = "\n".join([
            T("Handoff: esta conversacion pesa ~%s tokens y el cache expiro hace %s.",
              "Handoff: this conversation is ~%s tokens and its cache expired %s ago.") % (fmt_tok(u["ctx"]), fmt_dur(idle)),
            T("Enviar este mensaje volveria a cargar TODO (~%s tokens).",
              "Sending this message would reload EVERYTHING (~%s tokens).") % fmt_tok(u["ctx"]), "",
            T("Opcion barata: abre una conversacion NUEVA en este proyecto. Arranca con el handoff (~%s tokens, %d%% menos) y sabe donde te quedaste.",
              "Cheap option: open a NEW conversation in this project. It starts with the handoff (~%s tokens, %d%% less) and knows where you left off.") % (fmt_tok(base), pct),
            T("Ya guarde un resumen automatico en .claude/handoff/. Para que no se pierda nada, puedes escribir ",
              "An automatic summary is already saved in .claude/handoff/. To lose nothing, you can type ") + cmd("save")
            + T(" aqui antes (costara esta unica recarga).", " here first (that costs this one reload)."), "",
            T("Si de verdad quieres seguir AQUI, vuelve a enviar tu mensaje en los proximos %s minutos y pasara.",
              "If you really want to continue HERE, send your message again within %s minutes and it will go through.") % CFG["minutosOverride"]])
        emit({"decision": "block", "reason": msg})
        return
    if u["ctx"] >= CFG["avisoTokens"]:
        flag = DIR_ESTADO / ("aviso-" + sid)
        if not flag.exists():
            write_text(flag, "1")
            emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                  T('[handoff] La conversacion ya pesa ~%s tokens. Al terminar tu respuesta, sugiere en UNA linea: "%s y abrir conversacion nueva abarata el siguiente arranque ~%d%%".',
                    '[handoff] This conversation is already ~%s tokens. After your answer, suggest in ONE line: "%s and opening a new conversation makes the next start ~%d%% cheaper".')
                  % (fmt_tok(u["ctx"]), cmd("save"), round(pct_ahorro(u["ctx"])))}})


def modo_guard_model(args):
    if not CFG["guardiaModelo"]:
        return
    inp = read_stdin()
    if not inp:
        return
    ctx = float(inp.get("context_tokens") or 0)
    proj, sid = project_dir(inp, args), str(inp.get("session_id") or "")
    if ctx < CFG["umbralTokens"] or not inp.get("prompt_cache_warm") or pct_ahorro(ctx) < CFG["ahorroMinimoPct"]:
        return          # con cache frio la recarga se paga igual (decide la guardia de prompt); si no ahorra lo bastante, no estorba
    if test_override("override-model-" + sid):
        log_evento(proj, sid, "modelo", ctx, "forzado")
        emit({"hookSpecificOutput": {"hookEventName": "PreModelSwitch", "permissionDecision": "allow"}})
        return
    set_override("override-model-" + sid)
    log_evento(proj, sid, "modelo", ctx, "bloqueado")
    usd = " (~US$%.2f)" % float(inp["estimated_cache_write_usd"]) if inp.get("estimated_cache_write_usd") else ""
    base = get_base()
    msg = (T("Handoff: cambiar de %s a %s ahora re-envia ~%s tokens%s; el cache del modelo actual se pierde. "
             "Mas barato: %s aqui y abre una conversacion NUEVA con el otro modelo (~%s tokens, %d%% menos). "
             "Si de verdad quieres cambiar en esta conversacion, repite el cambio en los proximos %s minutos.",
             "Handoff: switching from %s to %s now re-sends ~%s tokens%s; the current model's cache is lost. "
             "Cheaper: %s here and open a NEW conversation with the other model (~%s tokens, %d%% less). "
             "If you really want to switch in this conversation, repeat the switch within %s minutes.")
           % (inp.get("from_model"), inp.get("to_model"), fmt_tok(ctx), usd, cmd("save"), fmt_tok(base), round((1 - base / ctx) * 100), CFG["minutosOverride"]))
    emit({"hookSpecificOutput": {"hookEventName": "PreModelSwitch", "permissionDecision": "deny", "permissionDecisionReason": msg}})


def modo_session_start(args):
    inp = read_stdin()
    if not inp:
        return
    proj, src, sid = project_dir(inp, args), str(inp.get("source") or ""), str(inp.get("session_id") or "")
    if src in ("resume", "fork"):
        ctx = float(inp.get("context_tokens") or 0)
        if inp.get("prompt_cache_likely_expired") and ctx >= CFG["umbralTokens"] and pct_ahorro(ctx) >= CFG["ahorroMinimoPct"]:
            base = get_base()
            emit({"systemMessage": T("Handoff: retomar esta conversacion costara ~%s tokens en el primer mensaje (cache expirado hace %s). Una conversacion NUEVA arranca con ~%s (%d%% menos) y carga el handoff sola.",
                                     "Handoff: resuming this conversation will cost ~%s tokens on the first message (cache expired %s ago). A NEW conversation starts at ~%s (%d%% less) and loads the handoff by itself.")
                  % (fmt_tok(ctx), fmt_dur(float(inp.get("seconds_since_last_response") or 0) / 60), fmt_tok(base), round((1 - base / ctx) * 100))})
        return
    if src in ("startup", "clear") and CFG["inyectarAlInicio"]:
        c = build_contexto(proj, sid)
        if c:
            emit({"systemMessage": T("Handoff cargado (~%s tokens). Claude ya tiene el contexto de la sesion anterior: pidele lo que quieras, no hace falta ningun comando.",
                                     "Handoff loaded (~%s tokens). Claude already has the previous session's context: just ask for what you want, no command needed.") % fmt_tok(len(c) / 3.5),
                  "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": c}})


def modo_snapshot(args):
    inp = None
    tp = args.get("transcript")
    if not tp:
        inp = read_stdin()
        tp = str((inp or {}).get("transcript_path") or "")
    invoke_snapshot(project_dir(inp, args), tp, inp)


def modo_preparar(args):
    proj = project_dir(None, args)
    hdir = ensure_handoff_dir(proj)
    tp = args.get("transcript") or latest_transcript(proj)
    snap = invoke_snapshot(proj, tp) if tp else None
    hoy = dt.datetime.now()
    out = ["FECHA: " + hoy.strftime("%Y-%m-%d %H:%M"), "PROYECTO: " + proj]
    hf, resp = hdir / "HANDOFF.md", hdir / "respaldo"
    resp.mkdir(parents=True, exist_ok=True)
    if hf.exists():
        (hdir / "historial").mkdir(parents=True, exist_ok=True)
        dest = hdir / "historial" / ("HANDOFF-%s.md" % hoy.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(hf, dest)
        out.append("HANDOFF ANTERIOR archivado en: %s" % dest)
    else:
        out.append("HANDOFF ANTERIOR: no existe (es el primero).")
    estados, meta = get_state_files(proj, con_memoria=True), {}
    for e in estados:
        shutil.copy2(e, resp / e.name)
        meta[e.name] = e.stat().st_size
        out.append("ESTADO: %s (%s bytes) respaldado en .claude/handoff/respaldo/" % (e.name, format(e.stat().st_size, ",")))
    write_text(resp / "tamanos.json", json.dumps(meta))
    propios = [e.name for e in estados if e.name != ARCHIVO_MEMORIA]
    out.append("REGLA: CLAUDE.md %s; Claude Code ya lo carga solo. NO lo edites ni crees archivos de estado salvo que el usuario lo pida."
               % ("existe" if any(e.name == ARCHIVO_MEMORIA for e in estados) else "no existe"))
    out.append("ARCHIVOS DE ESTADO DEL USUARIO: %s" % (", ".join(propios) if propios else "ninguno (no crear ninguno)"))
    if snap:
        todo = "".join(read_text(e).lower() for e in estados) + (read_text(hf).lower() if hf.exists() else "")
        falt = [k for k in snap["archivos"]
                if not re.search(r"[\\/]\.claude[\\/]|[\\/]Temp[\\/]claude[\\/]", k) and not k.endswith(".jsonl")
                and os.path.basename(k.replace("\\", "/")).lower() not in todo]
        out += ["", "ARCHIVOS TOCADOS QUE NO APARECEN EN ESTADO NI HANDOFF (%d): %s" % (len(falt), " | ".join(falt[:25]))]
    out.append("")
    if hf.exists():
        out += ["----- HANDOFF.md ANTERIOR (para arrastrar pendientes) -----", read_text(hf).strip()]
    if snap:
        out += ["", "----- RESUMEN AUTOMATICO DE ESTA SESION (lista de control) -----", snap["texto"].strip()]
    print("\n".join(out))


def modo_verificar(args):
    proj = project_dir(None, args)
    hdir = Path(proj) / ".claude" / "handoff"
    hf = hdir / "HANDOFF.md"
    if not hf.exists():
        print("FALLO: no existe .claude/handoff/HANDOFF.md")
        return 1
    ok, h = True, read_text(hf)
    if not re.search(r"(?i)siguiente acci|next action", h):
        print('FALLO: HANDOFF.md no tiene "Next action" / "Siguiente accion".')
        ok = False
    tok = round(len(h) / 3.5)
    if tok > 1500:
        print("AVISO: HANDOFF.md pesa ~%d tokens (meta: <= 1,200). Recortalo." % tok)
    if any(rx.search(h) for rx in SECRETOS):
        print("FALLO: HANDOFF.md parece contener una CLAVE. Quitala.")
        ok = False
    tm = hdir / "respaldo" / "tamanos.json"
    if tm.exists():
        for nombre, antes in json.loads(read_text(tm)).items():
            f = Path(proj) / nombre
            if not f.exists():
                print("FALLO: desaparecio %s. Restaurar: copiar .claude/handoff/respaldo/%s a la raiz." % (nombre, nombre))
                ok = False
                continue
            ahora = f.stat().st_size
            if ahora < antes * 0.97:
                shutil.copy2(hdir / "respaldo" / nombre, f)
                print("FALLO: %s se encogio de %s a %s bytes. RESTAURADO desde el respaldo; vuelve a agregar solo lineas nuevas con Edit." % (nombre, format(antes, ","), format(ahora, ",")))
                ok = False
            else:
                print("OK: %s %s -> %s bytes" % (nombre, format(antes, ","), format(ahora, ",")))
    print("OK: HANDOFF.md ~%d tokens" % tok)
    print("RESULTADO: OK" if ok else "RESULTADO: FALLO")
    return 0 if ok else 1


def modo_guardar(args):
    """Escribe HANDOFF.md desde stdin y lo verifica. Existe porque Claude Code protege la carpeta .claude/
    y pide permiso (o falla en modo automatico) si el modelo usa Write ahi; el motor si puede escribirla."""
    proj = project_dir(None, args)
    try:
        texto = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
    except Exception:
        texto = ""
    if not texto.strip():
        print("FALLO: no recibi el contenido de HANDOFF.md por stdin.")
        return 1
    if any(rx.search(texto) for rx in SECRETOS):
        print("FALLO: el contenido parece contener una CLAVE. No se escribio nada. Quitala (solo el NOMBRE de la credencial) y vuelve a intentar.")
        return 1
    write_text(ensure_handoff_dir(proj) / "HANDOFF.md", texto if texto.endswith("\n") else texto + "\n")
    print("OK: HANDOFF.md escrito (%s bytes)" % format(len(texto.encode("utf-8")), ","))
    return modo_verificar(args)


def modo_contexto(args):
    proj = project_dir(None, args)
    c = build_contexto(proj, "", completo=True)
    print(c if c else "NO HAY HANDOFF en este proyecto (.claude/handoff/ vacio). Usa %s en la sesion de trabajo." % cmd("save"))
    print("\n----- FRESCURA -----")
    try:
        rama = subprocess.run(["git", "-C", proj, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=10)
        if rama.returncode == 0:
            print("git rama: " + rama.stdout.strip())
            st = subprocess.run(["git", "-C", proj, "status", "--short"], capture_output=True, text=True, timeout=10).stdout.splitlines()[:15]
            if st:
                print("cambios sin commit:")
                for l in st:
                    print("  " + l)
        else:
            print("sin git")
    except Exception:
        print("sin git")
    hf = Path(proj) / ".claude" / "handoff" / "HANDOFF.md"
    if hf.exists():
        print("HANDOFF.md tiene %.0f dias" % ((time.time() - hf.stat().st_mtime) / 86400))


def modo_ahorro(args):
    proj = project_dir(None, args)
    todos = bool(args.get("todos"))
    base_dir = HOME / ".claude" / "projects"
    if todos:
        dirs = [d for d in base_dir.iterdir() if d.is_dir()] if base_dir.is_dir() else []
    else:
        d = find_sessions_dir(proj)
        dirs = [d] if d else []
    if not dirs:
        print("No encontre registros de sesiones para " + proj)
        return 1
    limite_largo = CFG["avisoTokens"] * 0.75
    T = {"costo": 0.0, "pausaN": 0, "pausaTok": 0.0, "pausaCosto": 0.0, "modeloN": 0, "modeloTok": 0.0, "modeloCosto": 0.0,
         "otroN": 0, "otroTok": 0.0, "sesiones": 0, "llamadas": 0, "bases": [], "maxCtx": 0.0, "largoCosto": 0.0, "largoN": 0}
    ejemplos = []
    r_id = re.compile(r'"id"\s*:\s*"(msg_[^"]+)"')
    for d in dirs:
        for f in sorted(d.glob("*.jsonl")):
            vistos, prev, primero = set(), None, True
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for l in fh:
                    if '"usage"' not in l or not _R_ASST.search(l) or _R_SIDE.search(l):
                        continue
                    m = r_id.search(l)
                    if not m or m.group(1) in vistos:
                        continue
                    vistos.add(m.group(1))
                    mm = _R_MODEL.search(l)
                    model = mm.group(1) if mm else ""
                    if model == "<synthetic>":
                        continue
                    tsm = _R_TS.search(l)
                    r = {"t": parse_ts(tsm.group(1)) if tsm else None, "m": model, "inp": _num(_R_INP, l), "cw": _num(_R_CW, l),
                         "cr": _num(_R_CR, l), "out": _num(_R_OUT, l), "c1h": _num(_R_1H, l)}
                    c5 = max(0, r["cw"] - r["c1h"])
                    costo = r["inp"] + 0.1 * r["cr"] + 1.25 * c5 + 2 * r["c1h"] + 5 * r["out"]
                    T["costo"] += costo
                    T["llamadas"] += 1
                    ctx = r["inp"] + r["cw"] + r["cr"]
                    T["maxCtx"] = max(T["maxCtx"], ctx)
                    if r["cr"] > limite_largo:
                        T["largoCosto"] += 0.1 * (r["cr"] - limite_largo)
                        T["largoN"] += 1
                    if primero:
                        primero = False
                        T["sesiones"] += 1
                        if 25000 < ctx < 150000:       # solo arranques reales: se ignoran sesiones diminutas
                            T["bases"].append(ctx)
                    elif prev and ctx >= 60000 and r["cw"] > 0.5 * ctx:
                        wcost = 1.25 * c5 + 2 * r["c1h"]
                        gap = (r["t"] - prev["t"]).total_seconds() / 60 if r["t"] and prev["t"] else 0
                        cuando = r["t"].astimezone().strftime("%Y-%m-%d %H:%M") if r["t"] else "?"
                        if prev["m"] != r["m"]:
                            T["modeloN"] += 1; T["modeloTok"] += r["cw"]; T["modeloCosto"] += wcost
                            ejemplos.append((r["cw"], "%s  cambio %s -> %s  re-escribio %s" % (cuando, prev["m"], r["m"], fmt_tok(r["cw"]))))
                        elif gap >= 55:
                            T["pausaN"] += 1; T["pausaTok"] += r["cw"]; T["pausaCosto"] += wcost
                            ejemplos.append((r["cw"], "%s  pausa de %s  re-escribio %s" % (cuando, fmt_dur(gap), fmt_tok(r["cw"]))))
                        else:
                            T["otroN"] += 1; T["otroTok"] += r["cw"]
                    prev = r
    base_med = get_base()
    if T["bases"]:
        b = sorted(T["bases"])
        base_med = b[len(b) // 2]
    ev_pausa = max(0.0, T["pausaCosto"] - T["pausaN"] * 2 * base_med)
    ev_modelo = max(0.0, T["modeloCosto"] - T["modeloN"] * 2 * base_med)
    evitable = ev_pausa + ev_modelo
    pct = lambda a: "%.1f %%" % (100 * a / T["costo"]) if T["costo"] > 0 else "0 %"
    titulo = "todos los proyectos" if todos else os.path.basename(proj)
    o = ["# Ahorro con handoff: %s" % titulo,
         "Medido en los registros reales de Claude Code: %d conversaciones, %s respuestas." % (T["sesiones"], format(T["llamadas"], ",")), "",
         "| Que | Veces | Tokens re-escritos | % del costo total |", "|---|---:|---:|---:|",
         "| Re-carga por pausa > 1 h (cache expirado) | %d | %s | %s |" % (T["pausaN"], fmt_tok(T["pausaTok"]), pct(T["pausaCosto"])),
         "| Re-carga por cambiar de modelo a media conversacion | %d | %s | %s |" % (T["modeloN"], fmt_tok(T["modeloTok"]), pct(T["modeloCosto"])),
         "| Otras re-cargas (compactacion, etc.) | %d | %s | - |" % (T["otroN"], fmt_tok(T["otroTok"])),
         "| Mensajes en conversacion de mas de %s (cada uno relee todo) | %d | - | %s |" % (fmt_tok(limite_largo), T["largoN"], pct(T["largoCosto"])), "",
         "**Arranque de conversacion nueva (medido):** ~%s tokens. **Conversacion mas grande:** ~%s tokens." % (fmt_tok(base_med), fmt_tok(T["maxCtx"]))]
    if T["maxCtx"] > 0:
        o.append("**Por cada arranque:** retomar la grande cuesta ~%s; una nueva con handoff ~%s -> **%.0f %% menos**." % (fmt_tok(T["maxCtx"]), fmt_tok(base_med), 100 * (1 - base_med / T["maxCtx"])))
    o += ["**Ahorro que daban las guardias en este historial:** %s del costo estimado (pausas %s + cambios de modelo %s)." % (pct(evitable), pct(ev_pausa), pct(ev_modelo)),
          "**Ahorro adicional cerrando con %s al llegar a ~%s:** hasta %s (aprox.; cada conversacion nueva paga su arranque)." % (cmd("save"), fmt_tok(limite_largo), pct(T["largoCosto"])),
          "**Total posible:** hasta %s del costo de %s." % (pct(evitable + T["largoCosto"]), titulo)]
    ef = RAIZ / "eventos.csv"
    if ef.exists():
        with open(ef, "r", encoding="utf-8", newline="") as fh:
            ev = list(csv.DictReader(fh))
        if not todos:
            ev = [e for e in ev if _norm(e["proyecto"]) == _norm(proj)]
        bl = [e for e in ev if e["decision"] == "bloqueado"]
        fz = [e for e in ev if e["decision"] == "forzado"]
        tok = lambda L: sum(max(0.0, float(e["tokens"]) - float(e["base"])) for e in L)
        o += ["", "## Desde que instalaste las guardias",
              "- Recargas frenadas: %d (~%s tokens evitados si abriste conversacion nueva)." % (len(bl), fmt_tok(tok(bl) - tok(fz))),
              "- Veces que elegiste seguir de todos modos: %d." % len(fz)]
    if ejemplos:
        o += ["", "## Las re-cargas mas caras"] + ["- " + t for _, t in sorted(ejemplos, reverse=True)[:8]]
    o += ["", "_Costo estimado con los pesos de precio de Anthropic (lectura de cache 0.1x, escritura 1.25x/2x, salida 5x). Supone que tu cuota se consume en proporcion al costo._"]
    txt = "\n".join(o) + "\n"
    if args.get("guardar"):         # por defecto NO escribe nada en tu proyecto: solo imprime
        write_text(ensure_handoff_dir(proj) / "ahorro.md", txt)
    print(txt)
    return 0


# ---------------------------------------------------------------- instalador
MARCA_HOOK = "--ws"                       # token que identifica nuestros hooks en settings.json
SKILLS_ENVIADOS = ("save", "resume", "savings", "setup")
PLACEHOLDER_MOTOR = "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py"


def _hooks_a_instalar(py, motor):
    """(evento, matcher, modo, opciones). El comando se genera para el shell del sistema:
    Windows -> PowerShell (operador &), resto -> bash. Ruta absoluta del Python que ejecuta el instalador."""
    def comando(modo):
        if os.name == "nt":
            return {"type": "command", "shell": "powershell", "command": '& "%s" "%s" %s %s' % (py, motor, modo, MARCA_HOOK)}
        return {"type": "command", "command": '"%s" "%s" %s %s' % (py, motor, modo, MARCA_HOOK)}
    spec = [("UserPromptSubmit", None, "guard-prompt", {"timeout": 10}),
            ("PreModelSwitch", None, "guard-model", {"timeout": 10}),
            ("SessionStart", "startup|resume|clear", "session-start", {"timeout": 10}),
            ("Stop", None, "snapshot", {"async": True}),
            ("PreCompact", None, "snapshot", {"async": True}),
            ("SessionEnd", None, "snapshot", {"timeout": 20})]
    out = []
    for ev, matcher, modo, opts in spec:
        h = comando(modo)
        h.update(opts)
        out.append((ev, matcher, {"matcher": matcher, "hooks": [h]} if matcher else {"hooks": [h]}))
    return out


def _es_nuestro(grupo):
    return any(MARCA_HOOK in (h.get("command") or "") and "handoff.py" in (h.get("command") or "") for h in grupo.get("hooks", []))


def _quitar_hooks(settings):
    n, hooks = 0, settings.get("hooks") or {}
    for ev in list(hooks):
        keep = [g for g in hooks[ev] if not _es_nuestro(g)]
        n += len(hooks[ev]) - len(keep)
        if keep:
            hooks[ev] = keep
        else:
            del hooks[ev]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]
    return n


ENV_PRUEBA = {"WARMSTART_UMBRAL_TOKENS": "5000", "WARMSTART_AVISO_TOKENS": "9000", "WARMSTART_IDLE_MIN": "1", "WARMSTART_AHORRO_MIN_PCT": "-100"}


def _quitar_env_prueba(settings):
    env = settings.get("env")
    if isinstance(env, dict):
        for k in ENV_PRUEBA:
            env.pop(k, None)
        if not env:
            del settings["env"]


def _sin_nuestros(settings):
    import copy
    s = copy.deepcopy(settings)
    _quitar_hooks(s)
    _quitar_env_prueba(s)
    return s


def _rutas_instalacion(args):
    posix = lambda p: str(p).replace("\\", "/")
    if args.get("ambito") == "proyecto":
        proj = Path(project_dir(None, args))
        base = proj / ".claude"
        return {"settings": base / "settings.local.json", "motor": base / "warmstart" / "handoff.py", "skills": base / "skills", "posix": posix}
    base = HOME / ".claude"
    return {"settings": base / "settings.json", "motor": base / "handoff" / "handoff.py", "skills": base / "skills", "posix": posix}


def _instalar(args, quitar=False):
    import copy
    R = _rutas_instalacion(args)
    sj, motor = R["settings"], R["motor"]
    py = R["posix"](sys.executable)
    original = {}
    if sj.exists():
        try:
            original = json.loads(sj.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print("settings no es JSON valido (%s). No toco nada." % e)
            return 1
    nuevo = copy.deepcopy(original)
    previos = _quitar_hooks(nuevo)
    _quitar_env_prueba(nuevo)
    nuevos = [] if quitar else _hooks_a_instalar(py, R["posix"](motor))
    for ev, _m, grupo in nuevos:
        nuevo.setdefault("hooks", {}).setdefault(ev, []).append(grupo)
    if args.get("modo-prueba") and args.get("ambito") == "proyecto" and not quitar:
        nuevo.setdefault("env", {}).update(ENV_PRUEBA)      # umbrales bajos SOLO en el proyecto de prueba
    if _sin_nuestros(nuevo) != _sin_nuestros(original):
        print("ERROR interno: la fusion alteraria algo ajeno. Abortado sin cambios.")
        return 1
    skills_src = Path(__file__).resolve().parent.parent / "skills"
    con_skills = not args.get("sin-skills") and skills_src.is_dir()
    print(("DESINSTALAR" if quitar else "INSTALAR") + " (ambito: %s)" % (args.get("ambito") or "usuario"))
    print("  settings : %s  (hooks propios antes: %d, despues: %d)" % (sj, previos, len(nuevos)))
    if not quitar:
        print("  motor    : %s" % motor)
        print("  python   : %s" % py)
        print("  eventos  : %s" % ", ".join(sorted({ev for ev, _m, _g in nuevos})))
        print("  skills   : %s" % (", ".join("warmstart-" + s for s in SKILLS_ENVIADOS) + " -> " + str(R["skills"]) if con_skills else "no se copian (los trae el plugin)"))
    if args.get("simular"):
        print("(simulacion: no se escribio nada)")
        return 0
    bak = None
    if sj.exists():
        # El respaldo va FUERA del proyecto (nunca junto al codigo, donde podria subirse a git) y solo legible por ti.
        import hashlib
        bak = RAIZ / "respaldos" / ("settings-%s-%s.json" % (hashlib.sha1(str(sj).encode()).hexdigest()[:8], dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sj, bak)
        if os.name != "nt":
            os.chmod(str(bak), 0o600)
        print("  respaldo : %s" % bak)
    sj.parent.mkdir(parents=True, exist_ok=True)
    sj.write_text(json.dumps(nuevo, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if _sin_nuestros(json.loads(sj.read_text(encoding="utf-8"))) != _sin_nuestros(original):
        print("ERROR: la verificacion fallo; restaurando el respaldo.")
        if bak:
            shutil.copy2(bak, sj)
        return 1
    if quitar:
        for s in SKILLS_ENVIADOS:
            shutil.rmtree(R["skills"] / ("warmstart-" + s), ignore_errors=True)
        print("  quitado. Tus datos (.claude/handoff de cada proyecto y ~/.claude/handoff) se conservan.")
        return 0
    motor.parent.mkdir(parents=True, exist_ok=True)
    if Path(__file__).resolve() != motor.resolve():
        shutil.copy2(__file__, motor)
    if con_skills:
        for s in SKILLS_ENVIADOS:
            txt = (skills_src / s / "SKILL.md").read_text(encoding="utf-8")
            txt = txt.replace(PLACEHOLDER_MOTOR, R["posix"](motor)).replace("\nname: %s\n" % s, "\nname: warmstart-%s\n" % s, 1)
            write_text(R["skills"] / ("warmstart-" + s) / "SKILL.md", txt)
    print("  OK. Abre una conversacion NUEVA de Claude Code para que carguen los hooks y los skills.")
    return 0


def modo_instalar(args):
    return _instalar(args)


def modo_desinstalar(args):
    return _instalar(args, quitar=True)


MODOS = {"instalar": modo_instalar, "desinstalar": modo_desinstalar, "guard-prompt": modo_guard_prompt, "guard-model": modo_guard_model, "session-start": modo_session_start,
         "snapshot": modo_snapshot, "preparar": modo_preparar, "guardar": modo_guardar, "verificar": modo_verificar, "contexto": modo_contexto, "ahorro": modo_ahorro}
HOOKS = {"guard-prompt", "guard-model", "session-start", "snapshot"}


def main(argv):
    global MODO
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    MODO = argv[0] if argv else "ayuda"
    args, i = {}, 1
    while i < len(argv):
        a = argv[i]
        if a in ("--proyecto", "--transcript", "--ambito") and i + 1 < len(argv):
            args[a[2:]] = argv[i + 1]
            i += 2
        elif a in ("--todos", "--guardar", "--simular", "--sin-skills", "--modo-prueba"):
            args[a[2:]] = True
            i += 1
        else:
            i += 1
    if MODO in ("--version", "version"):
        print(VERSION)
        return 0
    if MODO not in MODOS:
        print("Uso: handoff.py <%s> [--proyecto ruta] [--todos] [--transcript ruta.jsonl]" % "|".join(MODOS))
        return 0
    try:
        rc = MODOS[MODO](args)
        return rc if isinstance(rc, int) and MODO not in HOOKS else 0
    except Exception as e:
        log_error(e)
        if MODO in HOOKS:
            return 0            # los hooks nunca rompen la sesion
        print("ERROR (%s): %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
