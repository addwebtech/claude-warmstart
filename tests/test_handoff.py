"""Pruebas del motor handoff.py. No gastan tokens y no tocan tu ~/.claude (usan HANDOFF_HOME falso).

  python pruebas/test_handoff.py                     (todas)
  HANDOFF_REAL=ruta/sesion.jsonl python pruebas/test_handoff.py   (agrega una prueba con una sesion real)
"""
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

MOTOR = Path(__file__).resolve().parent.parent / "plugins" / "warmstart" / "scripts" / "handoff.py"
UTC = dt.timezone.utc
os.environ["WARMSTART_LANG"] = "es"     # las pruebas existentes comprueban los textos en espanol; las de idioma lo cambian a mano


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def asst(t, model, cr, cw, texto="", tool=None, ttl="1h", mid=None):
    content = ([{"type": "text", "text": texto}] if texto else []) + ([tool] if tool else [])
    cc = {"ephemeral_1h_input_tokens": cw if ttl == "1h" else 0, "ephemeral_5m_input_tokens": cw if ttl == "5m" else 0}
    return json.dumps({"type": "assistant", "timestamp": iso(t), "isSidechain": False,
                       "message": {"id": mid or "msg_" + os.urandom(8).hex(), "model": model, "content": content,
                                   "usage": {"input_tokens": 3, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
                                             "output_tokens": 200, "cache_creation": cc}}})


def user(t, texto):
    return json.dumps({"type": "user", "timestamp": iso(t), "isSidechain": False,
                       "message": {"role": "user", "content": [{"type": "text", "text": texto}]}})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="handoff-py-"))
        cls.home = cls.tmp / "home"
        cls.proj = cls.tmp / "proyecto"
        cls.proj.mkdir(parents=True)
        cls.home.mkdir()
        cls.sdir = cls.home / ".claude" / "projects" / "".join(c if c.isalnum() else "-" for c in str(cls.proj))
        cls.sdir.mkdir(parents=True)
        cls.now = dt.datetime.now(UTC)
        cls.sid = "aaaabbbb-1111-2222-3333-444455556666"
        cls.tp = cls.sdir / (cls.sid + ".jsonl")
        n = cls.now
        L = [user(n - dt.timedelta(hours=14), "Arma el configurador de marca blanca para Play"),
             asst(n - dt.timedelta(hours=14), "claude-sonnet-5-5", 26000, 30000, "Empiezo."),
             user(n - dt.timedelta(hours=13), "Usa esta clave sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 para probar"),
             asst(n - dt.timedelta(hours=13), "claude-sonnet-5-5", 300000, 2000, "", {"type": "tool_use", "name": "Edit", "input": {"file_path": str(cls.proj / "web" / "config.js"), "old_string": "a", "new_string": "b"}}),
             asst(n - dt.timedelta(hours=12.5), "claude-sonnet-5-5", 340000, 1000, "", {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [{"content": "Re-renderizar videos con marca Acme", "status": "pending"}, {"content": "Quitar logo Acme Raffle", "status": "completed"}]}}),
             asst(n - dt.timedelta(hours=12.2), "claude-sonnet-5-5", 410000, 500, "", {"type": "tool_use", "name": "mcp__n8n-mcp__update_workflow", "input": {"workflowId": "Ab12Cd34Ef56Gh78", "name": "Signup workflow"}}),
             user(n - dt.timedelta(hours=12.1), "ok"),
             asst(n - dt.timedelta(hours=12), "claude-sonnet-5-5", 414000, 500, "Quite el logo generico. Falta re-renderizar los videos con la marca de Acme. Sigo con eso?")]
        cls.tp.write_text("\n".join(L) + "\n", encoding="utf-8")
        (cls.proj / "ESTADO.md").write_text("# ESTADO\n" + "linea de contenido del cliente\n" * 200, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_motor(self, modo, entrada=None, extra=(), proyecto=True, raw=None):
        env = dict(os.environ, HANDOFF_HOME=str(self.home), PYTHONIOENCODING="utf-8")
        if proyecto:
            env["CLAUDE_PROJECT_DIR"] = str(self.proj)
        else:
            env.pop("CLAUDE_PROJECT_DIR", None)
        data = raw if raw is not None else (json.dumps(entrada).encode("utf-8") if entrada is not None else b"")
        p = subprocess.run([sys.executable, "-I", str(MOTOR), modo, *extra], input=data, capture_output=True, env=env, timeout=300)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def hook(self, modo, **campos):
        base = {"session_id": self.sid, "transcript_path": str(self.tp), "cwd": str(self.proj), "prompt": "sigue"}
        base.update(campos)
        return self.run_motor(modo, base)

    @property
    def hdir(self):
        return self.proj / ".claude" / "handoff"


class Pruebas(Base):
    # ---- guardia de prompt
    def test_01_bloquea_conversacion_grande_con_pausa(self):
        code, out, _ = self.hook("guard-prompt")
        j = json.loads(out)
        self.assertEqual(j["decision"], "block")
        self.assertIn("415k", j["reason"])
        self.assertIn("%", j["reason"])
        self.assertEqual(code, 0)

    def test_02_reenviar_el_mismo_mensaje_pasa(self):
        code, out, _ = self.hook("guard-prompt")
        self.assertEqual(out.strip(), "")
        csv_txt = (self.home / ".claude" / "handoff" / "eventos.csv").read_text(encoding="utf-8")
        self.assertIn("bloqueado", csv_txt)
        self.assertIn("forzado", csv_txt)

    def test_03_un_mensaje_distinto_vuelve_a_bloquear(self):
        _, out, _ = self.hook("guard-prompt", prompt="otra cosa distinta")
        self.assertEqual(json.loads(out)["decision"], "block")
        _, out2, _ = self.hook("guard-prompt", prompt="otra cosa distinta")
        self.assertEqual(out2.strip(), "")

    def test_03b_los_comandos_de_warmstart_nunca_se_frenan(self):
        for p in ("/warmstart:save", "/warmstart-save", "  /Warmstart-resume  "):
            _, out, _ = self.hook("guard-prompt", prompt=p, session_id="cmd-" + str(abs(hash(p)) % 1000))
            self.assertEqual(out.strip(), "", p)

    def test_04_avisos_automaticos_no_se_frenan(self):
        _, out, _ = self.hook("guard-prompt", prompt="<task-notification>build terminado</task-notification>")
        self.assertEqual(out.strip(), "")

    # ---- snapshot
    def test_05_snapshot_automatico_tiene_lo_importante(self):
        auto = self.hdir / "auto.md"
        for _ in range(60):
            if auto.exists():
                break
            time.sleep(0.25)
        self.assertTrue(auto.exists(), "el snapshot en segundo plano no se creo")
        a = auto.read_text(encoding="utf-8")
        for esperado in ("Arma el configurador", "Acme", "web/config.js", "[pending] Re-renderizar", "Ab12Cd34Ef56Gh78", "CLAVE OCULTA"):
            self.assertIn(esperado, a)
        self.assertNotIn("sk-ABCDEFGHIJ", a)

    def test_06_snapshot_incremental(self):
        with open(self.tp, "a", encoding="utf-8") as f:
            f.write(user(self.now, "Ahora cambia la tipografia del boleto") + "\n")
        self.hook("snapshot", last_assistant_message="Cambie la tipografia.")
        a = (self.hdir / "auto.md").read_text(encoding="utf-8")
        self.assertIn("tipografia del boleto", a)
        self.assertIn("web/config.js", a)          # conserva lo anterior
        self.assertIn("Cambie la tipografia", a)
        st = json.loads((self.hdir / "estado" / (self.sid + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(st["offset"], self.tp.stat().st_size)
        self.assertTrue(list((self.hdir / "sesiones").glob("*.md")))

    def test_07_linea_a_medio_escribir_no_se_pierde(self):
        sid = "eeee0000-1111-2222-3333-444455556666"
        tp = self.sdir / (sid + ".jsonl")
        completa = user(self.now, "primer pedido") + "\n"
        parcial = user(self.now, "segundo pedido")
        tp.write_text(completa + parcial[:30], encoding="utf-8")        # la ultima linea llega cortada
        self.run_motor("snapshot", {"session_id": sid, "transcript_path": str(tp), "cwd": str(self.proj)})
        with open(tp, "a", encoding="utf-8") as f:
            f.write(parcial[30:] + "\n")
        self.run_motor("snapshot", {"session_id": sid, "transcript_path": str(tp), "cwd": str(self.proj)})
        a = (self.hdir / "sesiones").glob("*" + sid[:8] + ".md")
        txt = next(a).read_text(encoding="utf-8")
        self.assertIn("primer pedido", txt)
        self.assertIn("segundo pedido", txt)

    # ---- no estorba
    def test_08_casos_donde_no_debe_intervenir(self):
        n = self.now
        grande_caliente = self.sdir / "cccc0000.jsonl"
        grande_caliente.write_text(asst(n - dt.timedelta(minutes=3), "claude-sonnet-5-5", 400000, 100, "hola") + "\n", encoding="utf-8")
        _, out, _ = self.hook("guard-prompt", session_id="cccc0000", transcript_path=str(grande_caliente))
        self.assertNotIn('"block"', out)
        self.assertIn("warmstart:save", out)                            # sugiere guardar, una vez
        _, out2, _ = self.hook("guard-prompt", session_id="cccc0000", transcript_path=str(grande_caliente))
        self.assertEqual(out2.strip(), "")
        chica_fria = self.sdir / "dddd0000.jsonl"
        chica_fria.write_text(asst(n - dt.timedelta(hours=20), "claude-sonnet-5-5", 30000, 100, "hola") + "\n", encoding="utf-8")
        _, out3, _ = self.hook("guard-prompt", session_id="dddd0000", transcript_path=str(chica_fria))
        self.assertEqual(out3.strip(), "")
        code, out4, _ = self.hook("guard-prompt", session_id="x", transcript_path=str(self.tmp / "no-existe.jsonl"))
        self.assertEqual((code, out4.strip()), (0, ""))
        code, out5, _ = self.run_motor("guard-prompt")
        self.assertEqual((code, out5.strip()), (0, ""))

    def test_09_ttl_de_5_minutos_se_detecta(self):
        n = self.now
        t5 = self.sdir / "ffff5555.jsonl"
        t5.write_text(asst(n - dt.timedelta(minutes=10), "claude-sonnet-5-5", 150000, 800, "x", ttl="5m") + "\n", encoding="utf-8")
        _, out, _ = self.hook("guard-prompt", session_id="ffff5555", transcript_path=str(t5), prompt="p5")
        self.assertEqual(json.loads(out)["decision"], "block", "con TTL de 5 min, 10 min de pausa ya es cache frio")
        t1 = self.sdir / "ffff1111.jsonl"
        t1.write_text(asst(n - dt.timedelta(minutes=10), "claude-sonnet-5-5", 150000, 800, "x", ttl="1h") + "\n", encoding="utf-8")
        _, out2, _ = self.hook("guard-prompt", session_id="ffff1111", transcript_path=str(t1), prompt="p1")
        self.assertEqual(out2.strip(), "", "con TTL de 1 h, 10 min de pausa sigue caliente")
        # sin evidencia de escritura reciente: se mira mas atras en el registro
        mix = self.sdir / "ffff2222.jsonl"
        mix.write_text("\n".join([asst(n - dt.timedelta(minutes=40), "claude-sonnet-5-5", 100000, 90000, "a", ttl="5m"),
                                  asst(n - dt.timedelta(minutes=10), "claude-sonnet-5-5", 150000, 0, "b", ttl="5m")]) + "\n", encoding="utf-8")
        _, out3, _ = self.hook("guard-prompt", session_id="ffff2222", transcript_path=str(mix), prompt="p2")
        self.assertEqual(json.loads(out3)["decision"], "block")

    # ---- cambio de modelo
    def test_10_guardia_de_cambio_de_modelo(self):
        m = {"session_id": self.sid, "transcript_path": str(self.tp), "cwd": str(self.proj), "from_model": "claude-sonnet-5-5",
             "to_model": "claude-opus-5-5", "context_tokens": 414000, "prompt_cache_warm": True, "cache_ttl": "1h", "estimated_cache_write_usd": 4.14}
        _, out, _ = self.run_motor("guard-model", m)
        h = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(h["permissionDecision"], "deny")
        self.assertIn("US$4", h["permissionDecisionReason"])
        _, out2, _ = self.run_motor("guard-model", m)
        self.assertEqual(json.loads(out2)["hookSpecificOutput"]["permissionDecision"], "allow")
        _, out3, _ = self.run_motor("guard-model", dict(m, context_tokens=40000))
        self.assertEqual(out3.strip(), "")
        _, out4, _ = self.run_motor("guard-model", dict(m, prompt_cache_warm=False))
        self.assertEqual(out4.strip(), "")

    # ---- inicio de sesion
    def test_11_inicio_de_conversacion_nueva(self):
        self.hdir.mkdir(parents=True, exist_ok=True)
        hf = self.hdir / "HANDOFF.md"
        hf.write_text("## Siguiente sesion\n**Siguiente accion:** Re-renderizar videos con marca Acme\n", encoding="utf-8")
        t = time.time() - 3600
        os.utime(hf, (t, t))
        _, out, _ = self.run_motor("session-start", {"session_id": "nueva-0000", "cwd": str(self.proj), "source": "startup", "model": "claude-sonnet-5-5"})
        j = json.loads(out)
        ctx = j["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Re-renderizar videos", ctx)
        self.assertIn("DESPUES del ultimo", ctx)
        self.assertLess(len(ctx) / 3.5, 2000)
        self.assertIn("Handoff cargado", j["systemMessage"])
        _, out2, _ = self.run_motor("session-start", {"session_id": self.sid, "cwd": str(self.proj), "source": "resume", "context_tokens": 414000, "prompt_cache_likely_expired": True, "seconds_since_last_response": 44000})
        self.assertIn("414k", json.loads(out2)["systemMessage"])      # usa el dato que entrega el hook
        _, out3, _ = self.run_motor("session-start", {"session_id": "z", "cwd": str(self.proj), "source": "compact"})
        self.assertEqual(out3.strip(), "")

    # ---- /handoff-save
    def test_12_preparar_y_verificar(self):
        code, out, _ = self.run_motor("preparar", extra=("--transcript", str(self.tp)))
        self.assertIn("archivado en", out)
        self.assertEqual(len(list((self.hdir / "historial").glob("*.md"))), 1)
        self.assertTrue((self.hdir / "respaldo" / "ESTADO.md").exists())
        self.assertIn("config.js", out)
        self.assertIn("RESUMEN AUTOMATICO", out)
        code, out, _ = self.run_motor("verificar")
        self.assertEqual(code, 0, out)
        self.assertIn("RESULTADO: OK", out)
        (self.proj / "ESTADO.md").write_text("# ESTADO\nresumido\n", encoding="utf-8")
        code, out, _ = self.run_motor("verificar")
        self.assertNotEqual(code, 0)
        self.assertIn("RESTAURADO", out)
        self.assertGreater((self.proj / "ESTADO.md").stat().st_size, 1000)
        (self.hdir / "HANDOFF.md").write_text("**Siguiente accion:** x\nclave: AIzaXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX\n", encoding="utf-8")
        code, out, _ = self.run_motor("verificar")
        self.assertNotEqual(code, 0)
        self.assertIn("CLAVE", out)

    def test_12b_guardar_por_stdin(self):
        h = self.hdir / "HANDOFF.md"
        ok = "# HANDOFF x\n**Next action:** seguir con los videos\n**Open question for the user:** Sigo con eso?\n".encode("utf-8")
        code, out, _ = self.run_motor("guardar", raw=ok)
        self.assertEqual(code, 0, out)
        self.assertIn("OK: HANDOFF.md escrito", out)
        self.assertIn("RESULTADO: OK", out)
        self.assertIn("seguir con los videos", h.read_text(encoding="utf-8"))
        antes = h.read_text(encoding="utf-8")
        code, out, _ = self.run_motor("guardar", raw=b"**Next action:** x\nclave: AIzaXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX\n")
        self.assertNotEqual(code, 0)
        self.assertIn("CLAVE", out)
        self.assertEqual(h.read_text(encoding="utf-8"), antes, "con una clave no debe escribir nada")
        code, out, _ = self.run_motor("guardar", raw=b"   \n")
        self.assertNotEqual(code, 0)
        code, out, _ = self.run_motor("guardar", raw="# sin accion siguiente\ncontenido con acentos: ñandú\n".encode("utf-8"))
        self.assertNotEqual(code, 0)           # verificar exige "Next action"

    def test_13_ahorro(self):
        _, out, _ = self.run_motor("ahorro")
        self.assertIn("| Re-carga por pausa", out)
        self.assertIn("%", out)
        self.assertIn("Recargas frenadas:", out)
        self.assertFalse((self.hdir / "ahorro.md").exists(), "por defecto el reporte no debe escribir en el proyecto")
        self.run_motor("ahorro", extra=("--guardar",))
        self.assertTrue((self.hdir / "ahorro.md").exists())

    # ---- privacidad y robustez
    def test_14_la_carpeta_de_datos_se_ignora_sola_en_git(self):
        gi = self.hdir / ".gitignore"
        self.assertTrue(gi.exists())
        self.assertIn("*", gi.read_text(encoding="utf-8"))
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
            r = subprocess.run(["git", "-C", str(self.proj), "check-ignore", "-q", ".claude/handoff/auto.md"])
            self.assertEqual(r.returncode, 0, "git no ignora .claude/handoff/")

    def test_15_encuentra_el_proyecto_aunque_el_nombre_de_carpeta_no_siga_la_regla(self):
        proj2 = self.tmp / "otro proyecto ñ"
        proj2.mkdir()
        raro = self.home / ".claude" / "projects" / "nombre-que-no-sigue-la-regla"
        raro.mkdir(parents=True)
        (raro / "s1.jsonl").write_text(json.dumps({"type": "queue-operation", "cwd": str(proj2)}) + "\n" +
                                       asst(self.now, "claude-sonnet-5-5", 30000, 100, "hola", mid="msg_x1") + "\n", encoding="utf-8")
        _, out, _ = self.run_motor("ahorro", extra=("--proyecto", str(proj2)), proyecto=False)
        self.assertIn("1 conversaciones", out)

    def test_16_nunca_rompe_la_sesion(self):
        for modo in ("guard-prompt", "guard-model", "session-start", "snapshot"):
            code, out, _ = self.run_motor(modo, raw=b"\xef\xbb\xbf{esto no es json")
            self.assertEqual((code, out.strip()), (0, ""), modo)
            code, out, _ = self.run_motor(modo, raw=b"\x00\xff\xfe\x01basura")
            self.assertEqual((code, out.strip()), (0, ""), modo)
        feo = self.sdir / "feo0000.jsonl"
        feo.write_bytes(b'{"type":"assistant","message":{"usage":\n\xff\xfe\x00\n{"type":"user"}\n')
        code, out, _ = self.hook("snapshot", session_id="feo0000", transcript_path=str(feo))
        self.assertEqual(code, 0)
        code, out, _ = self.hook("guard-prompt", session_id="feo0000", transcript_path=str(feo))
        self.assertEqual((code, out.strip()), (0, ""))

    def test_17_entrada_con_bom_se_lee(self):
        log = self.home / ".claude" / "handoff" / "errores.log"
        if log.exists():
            log.unlink()                    # la prueba 16 lo lleno a proposito con JSON roto
        datos = b"\xef\xbb\xbf" + json.dumps({"session_id": "bom", "transcript_path": str(self.tmp / "no.jsonl"), "cwd": str(self.proj)}).encode()
        code, out, err = self.run_motor("guard-prompt", raw=datos)
        self.assertEqual(code, 0)
        self.assertFalse(log.exists(), "un BOM valido no debe generar errores")
        # y un BOM real con un mensaje que SI debe frenarse funciona de punta a punta
        n = self.now
        g = self.sdir / "bom00000.jsonl"
        g.write_text(asst(n - dt.timedelta(hours=5), "claude-sonnet-5-5", 300000, 100, "x") + "\n", encoding="utf-8")
        datos = b"\xef\xbb\xbf" + json.dumps({"session_id": "bom00000", "transcript_path": str(g), "cwd": str(self.proj), "prompt": "hola"}).encode()
        _, out, _ = self.run_motor("guard-prompt", raw=datos)
        self.assertEqual(json.loads(out)["decision"], "block")

    # ---- instalador
    def _inst(self, home, *a):
        env = dict(os.environ, HANDOFF_HOME=str(home), PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-I", str(MOTOR), *a], env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_19_instalador_es_seguro_idempotente_y_reversible(self):
        home2 = self.tmp / "home2"
        (home2 / ".claude").mkdir(parents=True)
        original = {"model": "opus", "env": {"CLAVE": "valor-que-no-se-debe-tocar"},
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo ya-existia"}]}],
                              "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo otro"}]}]}}
        sj = home2 / ".claude" / "settings.json"
        sj.write_text(json.dumps(original, indent=2), encoding="utf-8")
        antes = sj.read_text(encoding="utf-8")
        r = self._inst(home2, "instalar", "--simular")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sj.read_text(encoding="utf-8"), antes, "--simular no debe escribir nada")
        self.assertEqual(self._inst(home2, "instalar").returncode, 0)
        nuevo = json.loads(sj.read_text(encoding="utf-8"))
        for ev in ("UserPromptSubmit", "PreModelSwitch", "SessionStart", "Stop", "PreCompact", "SessionEnd"):
            self.assertTrue(any("--ws" in h["command"] for g in nuevo["hooks"][ev] for h in g["hooks"]), ev)
        self.assertEqual(len(nuevo["hooks"]["Stop"]), 2)                    # el tuyo + el nuestro
        self.assertEqual(nuevo["env"], original["env"])
        self._inst(home2, "instalar")                                       # reinstalar no duplica
        self.assertEqual(len(json.loads(sj.read_text(encoding="utf-8"))["hooks"]["Stop"]), 2)
        sk = (home2 / ".claude" / "skills" / "warmstart-save" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: warmstart-save", sk)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", sk)
        self.assertIn(".claude/handoff/handoff.py", sk)
        self.assertTrue((home2 / ".claude" / "handoff" / "handoff.py").exists())
        self.assertTrue(list((home2 / ".claude" / "handoff" / "respaldos").glob("settings-*.json")))
        self.assertFalse(list((home2 / ".claude").glob("settings.json.bak*")), "el respaldo no debe quedar junto a settings.json")
        self._inst(home2, "desinstalar")
        self.assertEqual(json.loads(sj.read_text(encoding="utf-8")), original, "desinstalar debe dejar tu configuracion identica")
        self.assertFalse((home2 / ".claude" / "skills" / "warmstart-save").exists())

    def test_20_el_comando_de_hook_generado_funciona_de_punta_a_punta(self):
        proj3 = self.tmp / "p3"
        proj3.mkdir()
        r = self._inst(self.home, "instalar", "--ambito", "proyecto", "--proyecto", str(proj3))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = json.loads((proj3 / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        h = st["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertTrue((proj3 / ".claude" / "warmstart" / "handoff.py").exists())
        self.assertTrue((proj3 / ".claude" / "skills" / "warmstart-resume" / "SKILL.md").exists())
        entrada = json.dumps({"session_id": self.sid, "transcript_path": str(self.tp), "cwd": str(proj3), "prompt": "via comando generado"})
        shell = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", h["command"]] if os.name == "nt" else ["bash", "-c", h["command"]]
        env = dict(os.environ, HANDOFF_HOME=str(self.home), CLAUDE_PROJECT_DIR=str(proj3))
        p = subprocess.run(shell, input=entrada.encode("utf-8"), capture_output=True, env=env, timeout=120)
        self.assertEqual(json.loads(p.stdout.decode("utf-8"))["decision"], "block", p.stdout + p.stderr)

    def test_21_modo_prueba_solo_en_proyecto_y_se_deshace(self):
        proj4 = self.tmp / "p4"
        (proj4 / ".claude").mkdir(parents=True)
        original = {"env": {"MI_VAR": "1"}}
        f = proj4 / ".claude" / "settings.local.json"
        f.write_text(json.dumps(original), encoding="utf-8")
        self._inst(self.home, "instalar", "--ambito", "proyecto", "--proyecto", str(proj4), "--modo-prueba")
        env = json.loads(f.read_text(encoding="utf-8"))["env"]
        self.assertEqual(env["WARMSTART_IDLE_MIN"], "1")
        self.assertEqual(env["MI_VAR"], "1")
        self._inst(self.home, "desinstalar", "--ambito", "proyecto", "--proyecto", str(proj4))
        self.assertEqual(json.loads(f.read_text(encoding="utf-8")), original)
        # en ambito usuario el modo prueba se ignora: nunca baja umbrales globales
        home5 = self.tmp / "home5"
        (home5 / ".claude").mkdir(parents=True)
        self._inst(home5, "instalar", "--modo-prueba")
        self.assertNotIn("env", json.loads((home5 / ".claude" / "settings.json").read_text(encoding="utf-8")))

    def test_22_umbrales_bajos_por_entorno_frenan_con_1_minuto(self):
        n = self.now
        t = self.sdir / "pr000000.jsonl"
        t.write_text(asst(n - dt.timedelta(minutes=2), "claude-sonnet-5-5", 30000, 100, "x") + "\n", encoding="utf-8")
        entrada = {"session_id": "pr000000", "transcript_path": str(t), "cwd": str(self.proj), "prompt": "probando"}
        env = dict(os.environ, HANDOFF_HOME=str(self.home), CLAUDE_PROJECT_DIR=str(self.proj),
                   WARMSTART_UMBRAL_TOKENS="5000", WARMSTART_IDLE_MIN="1", WARMSTART_AHORRO_MIN_PCT="-100")
        p = subprocess.run([sys.executable, "-I", str(MOTOR), "guard-prompt"], input=json.dumps(entrada).encode(), capture_output=True, env=env, timeout=60)
        self.assertEqual(json.loads(p.stdout.decode("utf-8"))["decision"], "block")

    def test_23_no_estorba_si_una_conversacion_nueva_no_ahorra_lo_bastante(self):
        n = self.now
        t = self.sdir / "poco0000.jsonl"
        t.write_text(asst(n - dt.timedelta(hours=3), "claude-sonnet-5-5", 130000, 100, "x") + "\n", encoding="utf-8")
        home6 = self.tmp / "home6"                      # HOME aislado: los snapshots en segundo plano de otras pruebas no lo tocan
        env = dict(os.environ, HANDOFF_HOME=str(home6), CLAUDE_PROJECT_DIR=str(self.proj))
        (home6 / ".claude" / "handoff" / "estado").mkdir(parents=True, exist_ok=True)
        (home6 / ".claude" / "handoff" / "estado" / "base-arranque.txt").write_text("110000", encoding="utf-8")   # base enorme (muchos MCP)
        entrada = json.dumps({"session_id": "poco0000", "transcript_path": str(t), "cwd": str(self.proj), "prompt": "x"}).encode()
        p = subprocess.run([sys.executable, "-I", str(MOTOR), "guard-prompt"], input=entrada, capture_output=True, env=env, timeout=60)
        self.assertEqual(p.stdout.decode().strip(), "", "130k vs base 110k = 15 %% de ahorro: no debe frenar")

    def test_24_los_mensajes_usan_el_nombre_correcto_del_comando(self):
        _, out, _ = self.hook("guard-prompt", prompt="mensaje para nombre de comando", session_id="nombre-00", transcript_path=str(self.tp))
        razon = json.loads(out)["reason"]
        self.assertIn("/warmstart:save", razon)          # el motor del repo no esta en una carpeta "handoff"/"warmstart" -> estilo plugin
        self.assertNotIn("/handoff-save", razon)

    # ---- secretos y privacidad
    def test_25_enmascara_secretos_en_todo_lo_que_guarda(self):
        sid = "sec00000-1111-2222-3333-444455556666"
        tp = self.sdir / (sid + ".jsonl")
        falsos = ["hunter2-FAKE", "S3cr3tPass-FAKE", "abcd1234efgh-FAKE", "github_pat_11FAKEFAKEFAKEFAKEFAKEFAKEFAKE0000",
                  "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE", "ya29.FAKEFAKEFAKEFAKEFAKEFAKEFAKE"]
        n = self.now
        L = [user(n, "mi password=hunter2-FAKE y token: abcd1234efgh-FAKE; db postgres://admin:S3cr3tPass-FAKE@db.example.com/app; " + falsos[3] + " " + falsos[4]),
             asst(n, "claude-sonnet-5-5", 30000, 100, "listo, guarde api_key=hunter2-FAKE y " + falsos[5],
                  {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [{"content": "rotar secret=" + falsos[0], "status": "pending"}]}}),
             asst(n, "claude-sonnet-5-5", 30000, 100, "", {"type": "tool_use", "name": "mcp__demo__run", "input": {"name": falsos[4], "workflowId": "wf1"}})]
        tp.write_text("\n".join(L) + "\n", encoding="utf-8")
        self.run_motor("snapshot", {"session_id": sid, "transcript_path": str(tp), "cwd": str(self.proj), "last_assistant_message": "ok token=" + falsos[2]})
        todo = ""
        for f in self.hdir.rglob("*"):
            if f.is_file() and f.name != ".gitignore":
                todo += f.read_text(encoding="utf-8", errors="replace")
        for s in falsos:
            self.assertNotIn(s, todo, "se filtro un secreto: " + s[:8] + "...")
        self.assertIn("CLAVE OCULTA", todo)
        self.assertIn("db.example.com", todo)           # lo que no es secreto se conserva

    def test_26_el_repositorio_no_contiene_secretos_ni_datos_personales(self):
        import importlib.util
        import re
        spec = importlib.util.spec_from_file_location("motor_handoff", str(MOTOR))
        motor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(motor)
        raiz = MOTOR.parents[3]
        genericos = {"runner", "user", "root", "admin", "ubuntu", "runneradmin", "vagrant", "default", "docker", "home", "github", "test"}
        nombres = {x.lower() for x in (os.environ.get("USERNAME"), os.environ.get("USER"), os.path.basename(os.path.expanduser("~"))) if x and len(x) >= 4}
        nombres -= genericos
        falsos = re.compile(r"(?i)fake|xxxx|example|ejemplo|abcdefgh|\.\.\.")
        correo = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
        problemas = []
        for f in raiz.rglob("*"):
            if not f.is_file() or any(p in (".git", "__pycache__") for p in f.parts):
                continue
            txt = f.read_text(encoding="utf-8", errors="ignore")
            rel = str(f.relative_to(raiz))
            for rx in motor.SECRETOS:
                for m in rx.finditer(txt):
                    if not falsos.search(m.group(0)):
                        problemas.append("%s: patron de clave %s... (%d car.)" % (rel, m.group(0)[:4], len(m.group(0))))
            for nombre in nombres:
                if nombre in txt.lower():
                    problemas.append("%s: contiene el nombre de usuario de esta maquina" % rel)
            for m in correo.finditer(txt):
                if not re.search(r"example\.|noreply", m.group(0)):
                    problemas.append("%s: correo %s" % (rel, m.group(0)[:3] + "***"))
        self.assertEqual(problemas, [], "\n".join(problemas))

    def test_27_el_respaldo_de_settings_queda_fuera_del_proyecto(self):
        proj7 = self.tmp / "p7"
        (proj7 / ".claude").mkdir(parents=True)
        (proj7 / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"X": "1"}}), encoding="utf-8")
        self._inst(self.home, "instalar", "--ambito", "proyecto", "--proyecto", str(proj7))
        self.assertEqual([p.name for p in proj7.rglob("*") if "bak" in p.name.lower()], [], "ningun respaldo dentro del proyecto")
        self.assertTrue(list((self.home / ".claude" / "handoff" / "respaldos").glob("settings-*.json")))

    def test_28_proyecto_con_solo_claude_md(self):
        proj8 = self.tmp / "p8"
        proj8.mkdir()
        (proj8 / "CLAUDE.md").write_text("# demo\nReglas del proyecto.\n", encoding="utf-8")
        _, out, _ = self.run_motor("preparar", extra=("--proyecto", str(proj8), "--transcript", str(self.tp)), proyecto=False)
        self.assertIn("CLAUDE.md existe", out)
        self.assertIn("ninguno (no crear ninguno)", out)
        self.assertFalse((proj8 / "ESTADO.md").exists(), "nunca se crea ESTADO.md")
        antes = (proj8 / "CLAUDE.md").read_bytes()
        code, out, _ = self.run_motor("guardar", extra=("--proyecto", str(proj8)), raw=b"**Next action:** seguir\n", proyecto=False)
        self.assertEqual(code, 0, out)
        _, out, _ = self.run_motor("session-start", {"session_id": "n8", "cwd": str(proj8), "source": "startup"}, proyecto=False)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("seguir", ctx)
        self.assertNotIn("CLAUDE.md", ctx, "CLAUDE.md ya lo carga Claude Code: no se re-inyecta ni se lista")
        self.assertEqual((proj8 / "CLAUDE.md").read_bytes(), antes, "CLAUDE.md queda intacto")

    def test_29_los_skills_no_crean_archivos_del_usuario(self):
        txt = (MOTOR.parent.parent / "skills" / "save" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("never create one", txt)
        self.assertNotIn("create ESTADO.md", txt)
        for s in ("save", "resume", "savings", "setup"):
            t = (MOTOR.parent.parent / "skills" / s / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("disable-model-invocation: true", t, s + ": costo fijo 0 hasta que el usuario lo invoque")

    # ---- idioma y arranque sin comandos
    def _motor_con_entorno(self, modo, entrada, **entorno):
        env = {k: v for k, v in os.environ.items() if k not in ("WARMSTART_LANG", "LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG")}
        env.update(HANDOFF_HOME=str(self.home), CLAUDE_PROJECT_DIR=str(self.proj), **entorno)
        p = subprocess.run([sys.executable, "-I", str(MOTOR), modo], input=json.dumps(entrada).encode(), capture_output=True, env=env, timeout=60)
        return p.stdout.decode("utf-8", "replace")

    def test_30_mensajes_en_ingles_y_segun_el_idioma_del_sistema(self):
        entrada = {"session_id": "idioma-00", "transcript_path": str(self.tp), "cwd": str(self.proj), "prompt": "mensaje de idioma"}
        en = json.loads(self._motor_con_entorno("guard-prompt", entrada, WARMSTART_LANG="en"))["reason"]
        self.assertIn("Cheap option", en)
        self.assertNotIn("Opcion barata", en)
        entrada2 = dict(entrada, session_id="idioma-01", prompt="otro mensaje de idioma")
        sistema_es = json.loads(self._motor_con_entorno("guard-prompt", entrada2, LANG="es_MX.UTF-8"))["reason"]
        self.assertIn("Opcion barata", sistema_es)
        entrada3 = dict(entrada, session_id="idioma-02", prompt="tercer mensaje de idioma")
        sistema_en = json.loads(self._motor_con_entorno("guard-prompt", entrada3, LANG="en_US.UTF-8"))["reason"]
        self.assertIn("Cheap option", sistema_en)
        modelo = {"session_id": "idioma-03", "cwd": str(self.proj), "from_model": "a", "to_model": "b", "context_tokens": 414000, "prompt_cache_warm": True}
        razon = json.loads(self._motor_con_entorno("guard-model", modelo, WARMSTART_LANG="en"))["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Cheaper", razon)

    def test_31_el_arranque_no_pide_ningun_comando(self):
        self.hdir.mkdir(parents=True, exist_ok=True)
        (self.hdir / "HANDOFF.md").write_text("## Next session\n**Next action:** ship the thing\n", encoding="utf-8")
        for lang, esperado in (("en", "no command needed"), ("es", "ningun comando")):
            j = json.loads(self._motor_con_entorno("session-start", {"session_id": "arr-" + lang, "cwd": str(self.proj), "source": "startup"}, WARMSTART_LANG=lang))
            self.assertIn(esperado, j["systemMessage"])
            self.assertNotIn("/warmstart", j["systemMessage"], "el aviso de arranque no debe pedir /warmstart:resume")
            ctx = j["hookSpecificOutput"]["additionalContext"]
            self.assertIn("ship the thing", ctx)
            self.assertIn("INSTRUCTION" if lang == "en" else "INSTRUCCION", ctx, "el bloque inyectado le dice a Claude que arranque solo")
            self.assertLess(len(ctx) / 3.5, 2000)

    @unittest.skipUnless(os.environ.get("HANDOFF_REAL"), "defina HANDOFF_REAL=ruta/sesion.jsonl")
    def test_18_sesion_real(self):
        copia = self.sdir / "real-0000.jsonl"
        shutil.copy(os.environ["HANDOFF_REAL"], copia)
        t0 = time.time()
        self.run_motor("snapshot", {"session_id": "real-0000", "transcript_path": str(copia), "cwd": str(self.proj)})
        t1 = time.time() - t0
        t0 = time.time()
        self.run_motor("snapshot", {"session_id": "real-0000", "transcript_path": str(copia), "cwd": str(self.proj)})
        t2 = time.time() - t0
        t0 = time.time()
        self.hook("guard-prompt", session_id="real-0000", transcript_path=str(copia), prompt="rapido")
        t3 = time.time() - t0
        print("\n  sesion real: primera pasada %.1f s | incremental %.1f s | guardia %.2f s" % (t1, t2, t3))
        self.assertLess(t2, 3)
        self.assertLess(t3, 2)
        a = (self.hdir / "auto.md").read_text(encoding="utf-8")
        print("  auto.md real: ~%d tokens" % (len(a) / 3.5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
