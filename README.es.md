# Warmstart

> English: [README.md](README.md)

**Abre conversaciones nuevas de Claude Code ya con contexto y deja de pagar por recargar las viejas.**

Warmstart es un plugin de Claude Code (Python, solo biblioteca estándar) que:

1. **Guarda un handoff solo**, sin gastar tokens del modelo: tus últimos pedidos, la última respuesta de Claude (incluida una pregunta sin contestar), la lista de tareas, archivos tocados e IDs de workflows u hojas. Las claves se ocultan.
2. **Cuida los momentos caros.** Si envías un mensaje a una conversación grande con el caché expirado, o cambias de modelo a media conversación, te muestra cuánto cuesta y cuánto costaría una conversación nueva. Reenvía el mismo mensaje para seguir de todos modos.
3. **Arranca las conversaciones nuevas con contexto.** El handoff (~1-2k tokens) se inyecta al abrirlas.
4. **Mide el ahorro con tus propios registros** (`/warmstart:savings`), para que no tengas que creerle a nadie.

## Por qué

Una conversación de cientos de miles de tokens es barata mientras su caché está caliente y muy cara de retomar cuando expiró (horas después). Abrir una conversación nueva con un handoff compacto lo evita.

Medido en los registros del autor (un usuario, 350 conversaciones, 8.3k respuestas; los tuyos serán distintos):

| Fuente de costo | % del costo total |
|---|---:|
| Recargas tras una pausa > 1 h | 13.1 % |
| Recargas por cambiar de modelo a media conversación | 10.3 % |
| Mensajes dentro de conversaciones > 150k tokens | 31.2 % |

Las guardias habrían evitado ~20 % del costo; cerrar las conversaciones largas con un handoff guardado suma hasta ~31 % (es un tope, no una promesa). El costo usa los pesos de precio de lista de Anthropic (lectura de caché 0.1x, escritura 1.25x/2x, salida 5x) y supone que tu cuota crece con el costo. Ejecuta `/warmstart:savings` para ver el tuyo.

## Instalar

Requiere Claude Code y Python 3.8+. Funciona en la extensión de VS Code y en la terminal (ambas ejecutan los mismos hooks). Las guardias que usan datos del caché (aviso al reanudar, aviso al cambiar de modelo) necesitan Claude Code 2.1.251 o más nuevo; el guardado automático y la guardia de mensajes no dependen de eso.

**Se adapta a como trabaja la mayoría:** no necesita ningún archivo de estado. Si tu proyecto solo tiene un `CLAUDE.md`, Warmstart nunca lo edita (Claude Code ya lo carga) ni crea `ESTADO.md` ni nada más en tu proyecto. Lo único que escribe ahí es `.claude/handoff/`. Si tú llevas tu propio archivo de estado (p. ej. `ESTADO.md`), `/warmstart:save` puede agregarle una línea, con respaldo.

```
/plugin marketplace add addwebtech/claude-warmstart
/plugin install warmstart@warmstart
/warmstart:setup
```

`/warmstart:setup` te muestra exactamente qué agregará a `~/.claude/settings.json`, hace un respaldo y pregunta antes. Después abre una conversación **nueva**.

Sin el sistema de plugins: clona este repo y ejecuta `python plugins/warmstart/scripts/handoff.py instalar --simular` (vista previa) y luego sin `--simular`. Se quita todo con `desinstalar`; tus datos guardados se conservan.

## Uso

| Comando | Qué hace |
|---|---|
| `/warmstart:save` | Cierra una sesión: escribe `.claude/handoff/HANDOFF.md` (siguiente acción, pregunta abierta, decisiones con su porqué, qué no funcionó) y lo verifica |
| `/warmstart:resume` | En una conversación nueva: recibo corto y sigue desde la siguiente acción |
| `/warmstart:savings` | Reporte de costo medido con tus registros (`--todos` para todos los proyectos) |
| `/warmstart:setup` | Instala / previsualiza los hooks |

Día a día: termina con `/warmstart:save`; al día siguiente abre una conversación **nueva** (no retomes la vieja).

## Lo que NO hace

- No baja la base fija de cada conversación (prompt del sistema, herramientas MCP, skills, CLAUDE.md: decenas de miles de tokens).
- No abarata una conversación larga y caliente mientras sigues trabajando en ella.
- Una vez expirado el caché, retomar esa conversación siempre cuesta; Warmstart solo te avisa antes.
- El resumen automático es mecánico: no capta razones ni decisiones que solo se dijeron hablando. `/warmstart:save` (escrito por el modelo) también puede omitir cosas.
- No mantiene el caché caliente (herramientas como cache-tax sí, a costa de tokens). Es complementario: si ya usas otra guardia de caché, pon `"guardiaPrompt": false` en `~/.claude/handoff/config.json` para no tener dos.
- Solo Claude Code. No claude.ai ni asistentes de otros editores.

## Privacidad

Todo queda en tu máquina; no se envía nada a ningún lado. Los datos por proyecto viven en `.claude/handoff/`, que trae su propio `.gitignore` (`*`) para que nunca se suba a git por accidente. Guarda tus pedidos recientes, la última respuesta de Claude, tareas y nombres de archivos.

Secretos: los formatos de clave conocidos (claves de API, tokens, JWT, encabezados de llave privada, URLs `usuario:clave@`) y valores tipo `password=`/`token:` se ocultan como `[CLAVE OCULTA]` antes de escribir nada, y `HANDOFF.md` se rechaza si aún trae un formato de clave conocido. El ocultado es por patrones: un secreto raro puede escaparse, así que no pegues secretos en tus mensajes. Los respaldos de tu `settings.json` van a `~/.claude/handoff/respaldos/` (solo tu usuario en macOS/Linux), nunca dentro de tu proyecto. Recuerda que Claude Code guarda por sí mismo las conversaciones completas en `~/.claude/projects/`; Warmstart solo las lee. Las pruebas del repositorio escanean cada archivo en busca de patrones de clave y del nombre de usuario del desarrollador.

## Configuración

`~/.claude/handoff/config.json` (todo opcional): `umbralTokens` (120000: las guardias actúan por encima de este tamaño), `avisoTokens` (200000), `minutosOverride` (5), `guardiaPrompt` / `guardiaModelo` / `inyectarAlInicio` (true), `idioma` (`auto` sigue el idioma de tu sistema; `es` o `en` fuerza uno; la variable de entorno `WARMSTART_LANG` manda sobre todo). Los hooks nunca rompen una sesión: ante cualquier error salen en silencio y lo anotan en `~/.claude/handoff/errores.log`.

## Estado

Pre-lanzamiento. Probado en Windows: 33 pruebas automáticas más sesiones reales de hasta 81 MB, y una prueba de punta a punta contra Claude Code 2.1.312 real (19 verificaciones, todas en verde). macOS/Linux y Python 3.8 los cubre la integración continua, pero el autor aún no los ha ejecutado. Todavía no verificado en versiones más viejas de Claude Code (p. ej. 2.1.58) ni en la interfaz interactiva. Los mensajes que ves salen en español o inglés (automático). Los informes internos que lee el modelo (`preparar`, `verificar`, `ahorro`) siguen solo en español; los skills están en inglés.

Abrir una conversación nueva no requiere ningún comando: el handoff ya está en el contexto. Verificado: una pregunta concreta sobre la sesión anterior se contesta bien. **No garantizado:** que un saludo vago como "hola" retome solo. En las 3 corridas de prueba del autor lo hizo una vez; las otras Claude fue a buscar un archivo de estado, probablemente porque el `CLAUDE.md` global del autor se lo ordena (no comprobado). Pide directamente lo que quieres.

Corre tú mismo la prueba de punta a punta desde una terminal normal (no dentro de una sesión de Claude Code; cuesta unos centavos de cuota): `python tests/e2e_claude.py` (o `--claude RUTA`, `--model haiku`). Arma un proyecto desechable con solo un `CLAUDE.md`, instala los hooks únicamente ahí, recorre todo el flujo y comprueba que no se filtre ninguna clave ni archivo personal.

## Licencia

MIT

Warmstart es un proyecto comunitario independiente. No lo hace, avala ni da soporte Anthropic. "Claude" y "Claude Code" son marcas de Anthropic, usadas aquí solo para decir con qué funciona el plugin.
