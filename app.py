"""Point d'entrée d'OmniAgent Windows.

Trois usages :

    python app.py                      # interface graphique
    python app.py --task "…"           # une tâche, en console, puis sortie
    python app.py --autonomy           # session autonome bornée, puis sortie
    python app.py --check              # diagnostic d'installation
    python app.py --replay TRACE       # rejoue une trace, hors-ligne

Codes de sortie de `--task` : 0 succès, 1 échec de l'agent, 3 succès mais
trace incomplète (plafond de taille atteint).

La logique de l'agent reste dans `core/` ; l'interface dans `ui/`. Ce module ne
fait que câbler les deux, configurer le journal et traduire les erreurs en codes
de sortie.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Sequence, TextIO

import config
from config import APP_NAME, APP_VERSION, Settings
from core.events import Event, EventType
from core.factory import build_agent
from core.observability import RunIdFilter
from core.trace import build_trace
from security.redaction import redact

__all__ = [
    "browser_smoke",
    "build_parser",
    "diagnostics",
    "doc_index_smoke",
    "main",
    "ocr_smoke",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="OmniAgent",
        description=f"{APP_NAME} — assistant autonome pour Windows.",
    )
    parser.add_argument(
        "--task",
        metavar="TEXTE",
        help="exécute une tâche en console, sans interface, puis quitte",
    )
    parser.add_argument(
        "--autonomy",
        action="store_true",
        help="session autonome en console, guidée par data/autonomy/goals.md, puis quitte",
    )
    parser.add_argument(
        "--autonomy-cycles",
        type=int,
        metavar="N",
        help="nombre de cycles de la session (défaut : autonomy.max_cycles)",
    )
    parser.add_argument(
        "--autonomy-interval",
        type=float,
        metavar="MIN",
        help="minutes entre deux cycles (défaut : autonomy.interval_minutes)",
    )
    parser.add_argument(
        "--config",
        metavar="CHEMIN",
        help="fichier de configuration alternatif (JSON)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="affiche l'état de l'installation (dépendances, configuration) et quitte",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="n'enregistre pas les outils de navigation",
    )
    parser.add_argument(
        "--no-trace",
        action="store_true",
        help="n'écrit aucune trace sur disque (le contenu de la session reste en mémoire)",
    )
    parser.add_argument(
        "--trace-metadata",
        action="store_true",
        help="trace les faits (étapes, outils) sans la tâche ni les réponses",
    )
    parser.add_argument(
        "--replay",
        metavar="TRACE",
        help="rejoue une trace .jsonl hors-ligne, puis quitte",
    )
    parser.add_argument(
        "--browser-smoke",
        action="store_true",
        help="vérifie le pilotage du navigateur du système, puis quitte",
    )
    parser.add_argument(
        "--doc-index-smoke",
        action="store_true",
        help="vérifie l'indexation d'un PDF par l'index documentaire, puis quitte",
    )
    parser.add_argument(
        "--ocr-smoke",
        action="store_true",
        help="vérifie que l'OCR lit une image (binaire ou moteur embarqué), puis quitte",
    )
    parser.add_argument(
        "--compat-smoke",
        action="store_true",
        help="vérifie AgentRunner, run_task et history(run_id) hors réseau, puis quitte",
    )
    parser.add_argument(
        "--unlock-keyring",
        action="store_true",
        help="ouvre le coffre avec la phrase secrète du trousseau système",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return parser


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - métadonnées cassées
        return False


def diagnostics(settings: Settings) -> list[tuple[bool, str]]:
    """Contrôles d'installation ; le booléen indique si le point est bloquant."""
    config_file = settings.paths.config_file
    key = (os.environ.get(settings.llm.api_key_env) or "").strip()
    checks: list[tuple[bool, str]] = [
        (True, f"Python {platform.python_version()} sur {platform.system()} {platform.release()}"),
        (
            config_file.is_file(),
            f"configuration : {config_file}"
            + ("" if config_file.is_file() else " (absente, valeurs par défaut utilisées)"),
        ),
        (
            bool(key),
            f"clé du fournisseur ({settings.llm.api_key_env}) : "
            + ("présente" if key else "absente de l'environnement"),
        ),
        (
            _module_present("playwright"),
            "playwright : " + ("installé" if _module_present("playwright") else "absent"),
        ),
        (
            _module_present("customtkinter"),
            "customtkinter : " + ("installé" if _module_present("customtkinter") else "absent"),
        ),
        (
            _module_present("cryptography"),
            "cryptography : " + ("installé" if _module_present("cryptography") else "absent"),
        ),
    ]
    # OCR : optionnel, donc jamais bloquant. La distinction entre le module
    # `pytesseract` manquant et le binaire Tesseract absent évite de chercher au
    # mauvais endroit quand `ocr_status` répond `ocr_unavailable`.
    hints: list[str] = []
    try:
        from tools.ocr import Ocr

        ocr_ready, ocr_reason = Ocr.probe(getattr(settings.files, "tesseract_cmd", "") or "")
        if not ocr_ready:
            # Diagnostic autosuffisant : lien officiel, clé exacte, vérification.
            hints = Ocr.install_hint(
                getattr(settings.files, "tesseract_cmd", "") or "", config_file=str(config_file)
            )
    except Exception:  # noqa: BLE001 - paquet incomplet : on rapporte, on ne plante pas
        ocr_ready, ocr_reason = False, "module pytesseract introuvable dans le paquet"
    checks.append(
        (
            True,
            "OCR : " + (f"disponible ({ocr_reason})" if ocr_ready else f"indisponible — {ocr_reason}"),
        )
    )
    # Préfixe ASCII : la console Windows (cp1252) ne sait pas écrire « → ».
    checks.extend((True, f"OCR - {hint}") for hint in hints)

    vault = settings.paths.vault_file
    checks.append((True, f"coffre : {vault}" + ("" if vault.is_file() else " (à créer)")))
    checks.append((True, f"journal : {settings.paths.log_dir / 'omniagent.log'}"))
    # L'index est un cache : désynchronisé, il fait mentir la liste des runs.
    # Le diagnostic ne répare rien, il dit quoi faire (`--index-compact`).
    try:
        from core.index_doctor import diagnose, format_issues

        issues = diagnose(
            settings.paths.log_dir / "runs",
            active_ttl_s=settings.trace.active_ttl_s,
        )
    except Exception as exc:  # noqa: BLE001 - un diagnostic ne bloque pas le démarrage
        checks.append((True, f"index - diagnostic impossible : {type(exc).__name__}: {exc}"))
    else:
        if not issues:
            checks.append((True, f"index - {format_issues(issues)}"))
        for issue in issues[:8]:
            checks.append((not issue.error, f"index - {issue}"))
    return checks


def _report(settings: Settings) -> int:
    print(f"{APP_NAME} {APP_VERSION}")
    blocking = 0
    for ok, line in diagnostics(settings):
        print(f"  {'ok  ' if ok else '!!  '}{line}")
        if not ok:
            blocking += 1
    for issue in settings.validate():
        print(f"  ~~  {issue}")
    return 1 if blocking else 0


class _ConsoleStream:
    """N'écrit au fil de l'eau que le texte qui aboutit à la réponse finale.

    `AgentCore` publie chaque fragment sur le bus, sans dire à l'avance si
    l'étape en cours appellera un outil. Les fragments sont donc mis en attente
    par étape : un `tool_started` jette le tampon de son étape (c'était un
    commentaire de travail), un `final` écrit celui de l'étape qui conclut.
    Le texte est ainsi diffusé pendant l'exécution, mais jamais pour les tours
    intermédiaires. On retient ce qui a été écrit pour ne pas réimprimer la
    réponse après coup.
    """

    def __init__(self, out: TextIO | None) -> None:
        self._out = out
        self._pending: dict[int, list[str]] = {}
        self.text = ""

    def __call__(self, event: Event) -> None:
        if event.type == EventType.STREAM_CHUNK:
            chunk = str(event.payload.get("text") or "")
            if chunk:
                step = int(event.payload.get("step") or 0)
                self._pending.setdefault(step, []).append(chunk)
        elif event.type == EventType.TOOL_STARTED:
            # L'étape agit : son texte était un préambule, pas une réponse.
            self._pending.pop(int(event.payload.get("step") or 0), None)
        elif event.type == EventType.FINAL:
            # L'étape conclut : son texte est la réponse, on le libère maintenant
            # (fragment par fragment, comme il a été reçu). Sans flux, rien n'est
            # en attente et `_run_task` imprimera la réponse après coup.
            step = int(event.payload.get("step") or 0)
            for chunk in self._pending.pop(step, []):
                self._write(chunk)
        elif event.type == EventType.LLM_RETRY:
            # Tentative coupée, puis reprise : ses fragments n'ont jamais été
            # affichés et ne doivent pas rejoindre ceux de la tentative
            # suivante sous le même numéro d'étape. La reprise est marquée pour
            # que la transcription reste lisible.
            self._pending.clear()
            self._write(
                f"\n• reprise {event.payload.get('attempt', '?')} "
                f"après {event.payload.get('code', 'erreur')}\n\n"
            )
        elif event.type == EventType.CONTEXT_TRIMMED:
            self._write(
                # Flèche ASCII : la console Windows écrit en cp1252 et « → »
                # y lève UnicodeEncodeError, comme le note le diagnostic.
                "\n• contexte replié : "
                f"{event.payload.get('before_messages', '?')} -> "
                f"{event.payload.get('after_messages', '?')} messages "
                f"(~{event.payload.get('before_tokens', '?')} -> "
                f"{event.payload.get('after_tokens', '?')} tokens, "
                f"{event.payload.get('summarized', 0)} résumé(s))\n\n"
            )
        elif event.type == EventType.RUN_FINISHED:
            self._pending.clear()

    def _write(self, text: str) -> None:
        if not text:
            return
        self.text += text
        if self._out is None:  # exe fenêtré lancé sans console : rien à écrire
            return
        self._out.write(text)
        self._out.flush()

    def covers(self, text: str) -> bool:
        """Vrai si `text` a déjà été affiché par les fragments."""
        target = str(text or "").strip()
        return bool(target) and target in self.text


def _minimal_pdf_bytes(text: str) -> bytes:
    """PDF d'une page (Helvetica) sans dépendance, lisible par pypdf."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        xref,
    )
    return bytes(out)


def doc_index_smoke(settings: Settings) -> int:
    """Vérifie dans ce processus que `doc_index_build` lit un PDF et le retrouve.

    Un PDF n'est pas du texte : ce contrôle prouve que la conversion de
    `FileManager` est bien embarquée et utilisée par l'index, pas seulement que
    l'outil existe.
    """
    from tools.doc_index import DocIndex
    from tools.file_manager import FileManager

    marker = "DOC_INDEX_SMOKE_OK"
    # Le dossier de travail doit être sous une racine autorisée, sinon
    # `FileManager` refuse le chemin (c'est le confinement, pas un bug).
    roots = [Path(os.path.expandvars(str(root))).expanduser() for root in settings.files.allowed_roots]
    base = next((root for root in roots if root.is_dir()), Path.home())
    workdir = Path(tempfile.mkdtemp(prefix=".omniagent-docsmoke-", dir=str(base)))
    try:
        (workdir / "rapport.pdf").write_bytes(
            _minimal_pdf_bytes("Le budget du projet Atlas est de 42 EUR.")
        )
        files = FileManager(settings)
        with DocIndex(settings, files, db_path=workdir / "index.sqlite3") as index:
            built = index.index_path(str(workdir))
            print(
                f"  ok   {built['documents']} document(s), "
                f"{built['chunks']} morceau(x) indexé(s)"
            )
            if built["documents"] != 1:
                print(f"  !!   PDF non indexé : {built}", file=sys.stderr)
                return 1
            found = index.search("budget Atlas")
            if found["count"] < 1:
                print(f"  !!   passage introuvable : {found}", file=sys.stderr)
                return 1
            best = found["results"][0]
            print(f"  ok   passage retrouvé dans {Path(best['path']).name}")
            print(f"  ok   extrait : {best['excerpt'][:80]}")
        print(f"Contrôle index documentaire réussi ({marker}).")
        return 0
    except Exception as exc:  # noqa: BLE001 - un contrôle rapporte, il ne plante pas
        print(f"  !!   {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def ocr_smoke(settings: Settings) -> int:
    """Vérifie que l'OCR lit réellement du texte, binaire système ou non.

    L'image est rendue localement (Pillow), donc le contrôle ne dépend ni du
    réseau ni d'un fichier fourni par l'utilisateur. Il passe par le moteur en
    processus quand `tesseract.exe` est absent : c'est ce qui prouve que le
    paquet sait faire de l'OCR sans installation système.
    """
    from PIL import Image, ImageDraw, ImageFont

    from tools.ocr import Ocr

    marker = "OCR_SMOKE_OK"
    workdir = Path(tempfile.mkdtemp(prefix="omniagent-ocrsmoke-"))
    try:
        image = Image.new("RGB", (900, 160), "white")
        font = ImageFont.load_default(size=64)
        ImageDraw.Draw(image).text((20, 40), "ATLAS BUDGET 42", fill="black", font=font)
        target = workdir / "facture.png"
        image.save(target)

        ocr = Ocr(settings)
        status = ocr.status()
        print(f"  moteur : {status['backend'] or 'aucun'}")
        if not status["available"]:
            print(f"  !!   OCR indisponible : {status['detail']}", file=sys.stderr)
            return 1
        result = ocr.image_to_text(str(target), max_chars=2000)
        print(f"  langue : {result['language']}")
        print(f"  texte  : {result['text'][:60]!r}")
        text = result["text"].upper()
        if "ATLAS" not in text or "42" not in text:
            print(f"  !!   texte inattendu : {result['text']!r}", file=sys.stderr)
            return 1
        print(f"Contrôle OCR réussi ({marker}).")
        return 0
    except Exception as exc:  # noqa: BLE001 - un contrôle rapporte, il ne plante pas
        print(f"  !!   {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def compat_smoke(settings: Settings) -> int:
    """Exerce la façade d'exécution et le filtrage par run, sans réseau.

    Ni `AgentRunner` ni `run_task` ne sont utilisés par l'application : ce
    contrôle existe pour prouver, depuis l'exécutable figé, que ce chemin
    fonctionne encore une fois empaqueté. Le fournisseur est local et
    scripté, donc aucun appel sortant.
    """
    from core import AgentResult, AgentRunner, run_task
    from core.events import EventBus, EventType
    from core.llm_client import LLMResponse
    from core.tool_registry import ToolRegistry, ToolSpec
    from tools.base import object_schema, param

    class _FixedLLM:
        """Fournisseur minimal : répond sans réseau, en une étape."""

        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="compat ok", finish_reason="stop")

    llm = _FixedLLM()
    marker = "COMPAT_SMOKE_OK"
    wanted = {EventType.RUN_STARTED, EventType.FINAL, EventType.RUN_FINISHED}
    problems: list[str] = []

    # Chemin 1 : façade AgentRunner, avec un bus fourni par l'appelant.
    bus = EventBus()
    with AgentRunner(
        settings,
        llm=llm,
        bus=bus,
        with_web=False,
        with_browser=False,
    ) as runner:
        reply = runner.run("contrôle de compatibilité")
        run_id = runner.bundle.agent.run_id
    if not isinstance(reply, AgentResult) or not reply.ok:
        problems.append("AgentRunner n'a pas produit de résultat exploitable")
    events = bus.history(run_id)
    if not events or not wanted.issubset({event.type for event in events}):
        problems.append(f"history(run_id) incomplet : {[e.type for e in events]}")
    if any(event.run_id != run_id for event in events):
        problems.append("history(run_id) mélange plusieurs exécutions")

    # Chemin 2 : run_task, avec un second bus pour vérifier l'isolement.
    other = EventBus()
    second = run_task(
        "second contrôle",
        settings=settings,
        llm=llm,
        bus=other,
        with_web=False,
        with_browser=False,
    )
    second_events = other.history()
    if not isinstance(second, AgentResult) or not second.ok:
        problems.append("run_task n'a pas produit de résultat exploitable")
    if not second_events or EventType.RUN_STARTED not in {
        event.type for event in second_events
    }:
        problems.append("run_task n'a publié aucun événement")
    if len({event.run_id for event in second_events}) != 1:
        problems.append("run_task a publié plusieurs identifiants d'exécution")

    # Chemin 3 : un outil déclaré en données, converti et exécuté.
    def _echo(text: str) -> str:
        return text

    spec = ToolSpec(
        name="compat_echo",
        description="Répète le texte reçu.",
        parameters=object_schema({"text": param("string", "Texte")}, required=["text"]),
        func=_echo,
    )
    registry = ToolRegistry([spec], max_output_chars=3)
    shaped = registry.dispatch("compat_echo", {"text": "salut"})
    if not shaped.ok or shaped.data != "salut":
        problems.append("ToolSpec n'a pas produit de résultat exploitable")
    if registry.schemas()[0]["function"]["name"] != "compat_echo":
        problems.append("ToolSpec n'a pas produit de schéma exploitable")
    if "tronqué" not in registry.observation(shaped):
        problems.append("l'observation d'un ToolSpec n'est pas bornée par le registre")

    print(f"  AgentRunner : run_id={run_id or '(vide)'}, {len(events)} événement(s)")
    print(f"  run_task    : {len(second_events)} événement(s)")
    print(f"  ToolSpec    : {len(registry.names())} outil(s) converti(s)")
    if problems:
        for problem in problems:
            print(f"  !!   {problem}", file=sys.stderr)
        return 1
    print(f"Contrôle compatibilité réussi ({marker}).")
    return 0


def browser_smoke(settings: Settings) -> int:
    """Vérifie que ce processus sait piloter le navigateur du système.

    Aucun paquet n'embarque Node.js : si ce contrôle réussit dans l'exe, c'est
    que Playwright a trouvé un runtime Node et un navigateur sur la machine.
    La page est servie en local, donc le contrôle ne dépend pas du réseau.
    """
    import http.server

    from tools.browser_agent import (
        BrowserAgent,
        bundled_browser_available,
        ensure_system_node,
        playwright_browsers_root,
        system_browser_channel,
    )

    marker = "BROWSER_SMOKE_OK"
    workdir = Path(tempfile.mkdtemp(prefix="omniagent-smoke-"))
    (workdir / "index.html").write_text(
        "<!doctype html><html><head><title>OmniAgent smoke</title></head>"
        f"<body><h1>{marker}</h1></body></html>",
        encoding="utf-8",
    )

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, directory=str(workdir), **kwargs)

        def log_message(self, *args) -> None:  # silence : sortie dédiée au contrôle
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    agent = BrowserAgent(settings)
    try:
        node = ensure_system_node()
        print(f"Node.js : {node or 'pilote embarqué'}")
        print(
            f"Navigateurs Playwright : {playwright_browsers_root()} "
            f"(chromium présent : {bundled_browser_available()})"
        )
        print(f"Navigateur système : {system_browser_channel() or 'aucun'}")
        agent.start(headless=True)
        status = agent.status()
        print(f"  ok   navigateur lancé (canal {status.channel or 'chromium'})")
        agent.goto(url)
        print(f"  ok   page locale chargée : {url}")
        content = agent.page.content()
        if marker not in content:
            print(f"  !!   marqueur {marker} absent de la page", file=sys.stderr)
            return 1
        print(f"  ok   marqueur {marker} lu dans le DOM")
        print(f"Contrôle navigateur réussi ({marker}).")
        return 0
    except Exception as exc:  # noqa: BLE001 - un contrôle doit rapporter, pas planter
        print(f"  !!   {exc}", file=sys.stderr)
        cause = exc.__cause__
        if cause is not None:  # la cause porte le détail technique de Playwright
            print(f"  !!   cause : {cause}", file=sys.stderr)
        return 1
    finally:
        try:
            agent.stop()
        finally:
            server.shutdown()
            shutil.rmtree(workdir, ignore_errors=True)


def _run_task(settings: Settings, args: argparse.Namespace) -> int:
    bundle = build_agent(settings, with_browser=not args.no_browser)
    stream = _ConsoleStream(sys.stdout)
    if getattr(args, "no_trace", False):
        trace = None
    elif getattr(args, "trace_metadata", False):
        settings.trace.content = "metadata"
        trace = build_trace(settings)
    else:
        trace = build_trace(settings)
    unsubscribe_trace = (
        bundle.agent.bus.subscribe(trace) if trace is not None else (lambda: None)
    )
    unsubscribe = bundle.agent.bus.subscribe(
        stream,
        types=[
            EventType.STREAM_CHUNK,
            EventType.TOOL_STARTED,
            EventType.FINAL,
            EventType.LLM_RETRY,
            EventType.CONTEXT_TRIMMED,
            EventType.RUN_FINISHED,
        ],
    )
    try:
        if args.unlock_keyring:
            if not bundle.vault.unlock_from_keyring():
                print(
                    "Coffre non déverrouillé : aucune phrase secrète dans le trousseau.",
                    file=sys.stderr,
                )
        reply = bundle.agent.run(args.task)
        if stream.covers(reply.text):
            print()  # termine la ligne déjà écrite fragment par fragment
        else:
            print(reply.text or "(aucune réponse)")
        if not reply.ok:
            print(f"[{reply.stop_reason}] {reply.error}", file=sys.stderr)
            return 1
        # Une trace incomplète ne doit pas ressembler à une exécution propre :
        # le dire sur stderr, et le signaler par un code de sortie distinct.
        if trace is not None and trace.truncated:
            print(
                f"Attention : trace incomplète ({trace.path.name}), plafond de "
                f"{trace.max_bytes} octets atteint ; la fin de l'exécution n'est "
                "pas enregistrée.",
                file=sys.stderr,
            )
            return 3
        return 0
    finally:
        unsubscribe()
        unsubscribe_trace()
        if trace is not None:
            trace.close()
        bundle.close()


def _run_autonomy(settings: Settings, args: argparse.Namespace) -> int:
    """Session autonome en console : objectifs dans goals.md, journal JSONL."""
    from core.autonomy import AutonomySession, goals_path, journal_path, read_goals

    goals = goals_path(settings)
    if not read_goals(goals):
        print(
            f"Aucun objectif : renseignez {goals} avant de lancer l'autonomie.",
            file=sys.stderr,
        )
        return 2

    bundle = build_agent(settings, with_browser=not args.no_browser)
    trace = None if getattr(args, "no_trace", False) else build_trace(settings)
    unsubscribe_trace = (
        bundle.agent.bus.subscribe(trace) if trace is not None else (lambda: None)
    )
    session = AutonomySession(settings, bundle.agent)
    cycles = (
        args.autonomy_cycles
        if args.autonomy_cycles is not None
        else settings.autonomy.max_cycles
    )
    interval = (
        None if args.autonomy_interval is None else args.autonomy_interval * 60.0
    )
    failures = 0

    def report(record: dict) -> None:
        nonlocal failures
        if record.get("type") == "no_goals":
            print("Aucun objectif : session terminée.", file=sys.stderr)
            return
        status = "ok" if record.get("ok") else "echec"
        reason = record.get("stop_reason") or "?"
        print(
            f"Cycle {record.get('cycle')} : {status} "
            f"({reason}, {record.get('duration_s')} s, run {record.get('run_id') or '?'})"
        )
        excerpt = str(record.get("excerpt") or "")
        if excerpt:
            print(f"  {excerpt[:300]}")
        if record.get("error"):
            print(f"  erreur : {record['error']}", file=sys.stderr)
        if not record.get("ok"):
            failures += 1

    try:
        session.run(cycles=cycles, interval_s=interval, on_cycle=report)
    except KeyboardInterrupt:
        session.request_stop()
        print("Session autonome interrompue.", file=sys.stderr)
        return 130
    finally:
        unsubscribe_trace()
        if trace is not None:
            trace.close()
        bundle.close()
    print(f"Journal : {journal_path(settings)}")
    return 1 if failures else 0


def _run_gui(settings: Settings) -> int:
    from ui.app_gui import available, run_gui

    if not available():
        print(
            "customtkinter est absent : installez les dépendances "
            "(pip install -r requirements.txt), ou utilisez --task.",
            file=sys.stderr,
        )
        return 2
    bundle = build_agent(settings)
    try:
        return run_gui(settings, bundle)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = config.Settings.load(args.config, apply_env=True, strict=False)
    except config.ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2

    if args.check:
        return _report(settings)

    if args.browser_smoke:
        return browser_smoke(settings)

    if args.doc_index_smoke:
        return doc_index_smoke(settings)

    if args.ocr_smoke:
        return ocr_smoke(settings)

    if args.compat_smoke:
        return compat_smoke(settings)

    if args.replay:
        from core.replay import format_run, replay

        path = Path(args.replay)
        if not path.is_file():
            print(f"Trace introuvable : {path}", file=sys.stderr)
            return 2
        print(format_run(replay(path)))
        return 0

    config.configure_logging(settings, redact, filters=(RunIdFilter(),))
    if args.autonomy:
        return _run_autonomy(settings, args)
    if args.task is not None:
        return _run_task(settings, args)
    return _run_gui(settings)


if __name__ == "__main__":
    sys.exit(main())
