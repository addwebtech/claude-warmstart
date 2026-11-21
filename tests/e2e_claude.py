#!/usr/bin/env python3
"""Prueba de punta a punta con Claude Code REAL (terminal o extension). Gasta unos centavos de tu cuota.

  python tests/e2e_claude.py                       usa `claude` del PATH
  python tests/e2e_claude.py --claude RUTA --model haiku --espera 70

Debe ejecutarse desde una terminal NORMAL, no dentro de una sesion de Claude Code (versiones viejas se niegan).
Crea un proyecto temporal con solo CLAUDE.md, instala los hooks SOLO ahi (umbrales bajos), recorre el flujo
completo y verifica cada paso. Sus datos van a una carpeta temporal: no toca tus estadisticas ni tu settings.json.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MOTOR = Path(__file__).resolve().parent.parent / "plugins" / "warmstart" / "scripts" / "handoff.py"
FALSOS = ["hunter2-FAKE", "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE"]
RESULTADOS = []


def check(nombre, ok, detalle=""):
    RESULTADOS.append((nombre, bool(ok)))
    print("  [%s] %s%s" % ("OK " if ok else "FALLA", nombre, ("  -> " + str(detalle)[:300]) if (detalle and not ok) else ""))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", default=os.environ.get("CLAUDE_BIN") or shutil.which("claude"))
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--espera", type=int, default=70, help="segundos de pausa (el modo prueba usa 1 min)")
    ap.add_argument("--keep", action="store_true", help="no borrar el proyecto temporal")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not a.claude:
        print("No encuentro `claude`. Usa --claude RUTA o define CLAUDE_BIN.")
        return 2
    ver = subprocess.run([a.claude, "--version"], capture_output=True, text=True).stdout.strip()
    print("Claude Code: %s | modelo de prueba: %s | motor: %s" % (ver, a.model, MOTOR.name))

    tmp = Path(tempfile.mkdtemp(prefix="warmstart-e2e-"))
    proj, datos = tmp / "proyecto", tmp / "datos"
    proj.mkdir()
    # La ultima linea aisla la prueba de reglas personales del usuario (p. ej. un CLAUDE.md global que ordena crear ESTADO.md al abrir cada sesion).
    (proj / "CLAUDE.md").write_text("# Proyecto demo\nResponde siempre breve.\nNo crees ni edites ESTADO.md ni ningun otro archivo de estado.\n", encoding="utf-8")
    claude_md = hashlib.sha256((proj / "CLAUDE.md").read_bytes()).hexdigest()
    env = dict(os.environ, WARMSTART_DATA_DIR=str(datos))   # no se fuerza nada: si la version se niega a anidarse, se avisa

    def claude(prompt, resume=None, timeout=300):
        cmd = [a.claude, "-p", prompt, "--model", a.model, "--permission-mode", "acceptEdits", "--output-format", "json"]
        if resume:
            cmd += ["--resume", resume]
        p = subprocess.run(cmd, cwd=str(proj), env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        if "cannot be launched inside another Claude Code session" in err:
            print("\nEsta version se niega a correr dentro de otra sesion de Claude Code. Ejecuta este script desde una terminal normal.")
            sys.exit(2)
        try:
            return json.loads(out)
        except Exception:
            return {"result": out + err, "num_turns": -1, "is_error": True}

    def archivos(base):
        return [f for f in Path(base).rglob("*") if f.is_file() and f.name != ".gitignore"] if Path(base).exists() else []

    def hay_falsos():
        malos = []
        for base in (proj / ".claude" / "handoff", datos):
            for f in archivos(base):
                t = f.read_text(encoding="utf-8", errors="replace")
                malos += [(f.name, s[:6]) for s in FALSOS if s in t]
        return malos

    try:
        print("\n1. Instalar los hooks SOLO en el proyecto temporal (modo prueba)")
        r = subprocess.run([sys.executable, str(MOTOR), "instalar", "--ambito", "proyecto", "--proyecto", str(proj), "--modo-prueba"], capture_output=True, text=True, env=env)
        check("instalador termina bien", r.returncode == 0, r.stdout + r.stderr)
        check("los hooks quedaron en settings.local.json del proyecto", (proj / ".claude" / "settings.local.json").exists())

        print("\n2. Pedido normal (con datos de prueba que parecen secretos)")
        j = claude("Crea el archivo hola.txt con la palabra prueba. Datos de prueba, no los uses: password=%s y la clave %s. Responde solo: listo" % tuple(FALSOS))
        sid = j.get("session_id")
        check("Claude contesta sin error", sid and not j.get("is_error"), j.get("result"))
        check("hola.txt creado", (proj / "hola.txt").exists())
        time.sleep(3)
        check("el hook Stop guardo el resumen automatico (auto.md)", (proj / ".claude" / "handoff" / "auto.md").exists())
        check("ningun secreto de prueba aparece en lo que guarda Warmstart", not hay_falsos(), hay_falsos())

        print("\n3. Pausa de %d s y mensaje nuevo -> debe BLOQUEARSE sin gastar tokens" % a.espera)
        time.sleep(a.espera)
        j = claude("cuanto es 2+2, responde solo el numero", resume=sid)
        check("bloqueado por el hook (0 turnos)", j.get("num_turns") == 0 and "Handoff" in str(j.get("result")), j.get("result"))
        check("el aviso nombra un comando de warmstart", "/warmstart" in str(j.get("result")), j.get("result"))

        print("\n4. Reenviar el MISMO mensaje -> debe pasar")
        j = claude("cuanto es 2+2, responde solo el numero", resume=sid)
        check("pasa y Claude responde 4", (j.get("num_turns") or 0) >= 1 and "4" in str(j.get("result")), j.get("result"))

        print("\n5. Conversacion NUEVA: debe saber lo anterior solo por el handoff inyectado")
        j = claude("Sin usar herramientas ni leer archivos: segun el contexto que ya trae esta conversacion, que archivo se creo en la sesion anterior? Responde en una linea.")
        check("la conversacion nueva menciona hola.txt", "hola.txt" in str(j.get("result")), j.get("result"))

        print("\n5b. Conversacion NUEVA con un saludo vago ('hola'): debe retomar sola, sin ningun comando")
        j = claude("hola")
        check("responde sin error", not j.get("is_error"), j.get("result"))
        # Informativo, no cuenta como exito ni falla: que el modelo retome ante un saludo vago depende del modelo y de las reglas
        # propias del usuario (p. ej. un CLAUDE.md que ordena revisar un ESTADO.md). Lo garantizado es el paso 5 (pregunta concreta).
        retoma = any(s in str(j.get("result")).lower() for s in ("hola.txt", "2+2", "2 + 2"))
        print("  [INFO] saludo vago -> %s" % ("retomo solo el trabajo anterior" if retoma else "NO retomo solo (no garantizado): " + str(j.get("result"))[:160].replace("\n", " ")))

        print("\n6. Guardar con el skill (/warmstart-save)")
        j = claude("/warmstart-save", resume=sid)
        h = proj / ".claude" / "handoff" / "HANDOFF.md"
        check("HANDOFF.md escrito", h.exists(), j.get("result"))
        if h.exists():
            t = h.read_text(encoding="utf-8", errors="replace")
            check("HANDOFF.md tiene la siguiente accion", "next action" in t.lower() or "siguiente acci" in t.lower())
        denegados = j.get("permission_denials") or []
        check("el guardado no choco con permisos", not denegados, denegados)

        print("\n7. Privacidad e integridad al final")
        check("ningun secreto de prueba en NINGUN archivo de Warmstart", not hay_falsos(), hay_falsos())
        check("CLAUDE.md del usuario quedo intacto", hashlib.sha256((proj / "CLAUDE.md").read_bytes()).hexdigest() == claude_md)
        check("la carpeta de datos se ignora sola en git", (proj / ".claude" / "handoff" / ".gitignore").exists())
        check("los respaldos no estan dentro del proyecto", not [f for f in proj.rglob("*") if "bak" in f.name.lower()])
        errores = datos / "errores.log"
        check("sin errores del motor", not errores.exists(), errores.read_text(encoding="utf-8") if errores.exists() else "")
        if (proj / "ESTADO.md").exists():
            print("  [INFO] Existe ESTADO.md: Warmstart no lo crea; probablemente tu CLAUDE.md GLOBAL le pide a Claude crearlo.")
    finally:
        if a.keep:
            print("\nProyecto temporal conservado en:", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    fallos = [n for n, ok in RESULTADOS if not ok]
    print("\nResultado: %d OK, %d FALLAN" % (len(RESULTADOS) - len(fallos), len(fallos)))
    for n in fallos:
        print("  - " + n)
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
