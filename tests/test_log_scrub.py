"""Épuration rétroactive des journaux : masquer les clés déjà écrites.

Le masquage de `security/redaction.py` n'agit qu'à l'écriture : une clé
consignée avant la 1.1.3 reste en clair sur le disque. Ces tests figent le
périmètre (journaux et autonomie, jamais l'audit ni la mémoire), l'idempotence
et le fait qu'un aperçu ne réécrit rien.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, run, tmpdir  # noqa: E402

from core.log_scrub import scrub_tree, targets  # noqa: E402

#: Clés synthétiques : aucune clé réelle ne doit entrer dans le dépôt.
_OPENAI = "sk-proj-AbCdEf0123456789AbCdEf0123456789"
_GEMINI = "AQ.AbCdEf0123456789AbCdEf0123456789"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_un_apercu_ne_reecrit_rien_et_annonce_les_fichiers() -> None:
    with tmpdir("scrub-preview") as root:
        settings = make_settings(root)
        journal = _write(
            settings.paths.log_dir / "omniagent.log",
            f"appel refusé pour {_OPENAI}\nseconde ligne sans secret\n",
        )
        report = scrub_tree(targets(settings), apply=False)

        assert report.files, report.format()
        assert report.leaked_files == 1, report.format()
        assert report.leaked_lines == 1, report.format()
        assert _OPENAI in journal.read_text(encoding="utf-8")
        assert "--redact-logs-apply" in report.format()


def test_l_application_masque_la_cle_et_preserve_le_reste() -> None:
    with tmpdir("scrub-apply") as root:
        settings = make_settings(root)
        journal = _write(
            settings.paths.log_dir / "runs" / "trace.jsonl",
            '{"type": "llm_error", "detail": "clé %s rejetée"}\n' % _GEMINI,
        )
        scrub_tree(targets(settings), apply=True)

        written = journal.read_text(encoding="utf-8")
        assert _GEMINI not in written
        assert "llm_error" in written
        assert "rejetée" in written
        # Le fichier reste une ligne JSONL valide : un masque n'ajoute pas de saut.
        assert len(written.splitlines()) == 1


def test_une_seconde_passe_ne_trouve_plus_rien() -> None:
    with tmpdir("scrub-idempotent") as root:
        settings = make_settings(root)
        _write(settings.paths.log_dir / "omniagent.log", f"clé {_OPENAI}\n")
        scrub_tree(targets(settings), apply=True)

        again = scrub_tree(targets(settings), apply=True)
        assert again.files == (), again.format()
        assert "aucune clé en clair" in again.format()


def test_les_rotations_de_journaux_sont_relues() -> None:
    with tmpdir("scrub-rotation") as root:
        settings = make_settings(root)
        rotated = _write(
            settings.paths.log_dir / "omniagent.log.3", f"archive : {_OPENAI}\n"
        )
        scrub_tree(targets(settings), apply=True)
        assert _OPENAI not in rotated.read_text(encoding="utf-8")


def test_le_journal_d_audit_et_les_notes_ne_sont_pas_relus() -> None:
    """Réécrire l'audit romprait la chaîne ; la mémoire n'est pas un journal."""
    with tmpdir("scrub-perimetre") as root:
        settings = make_settings(root)
        audit = _write(root / "audit" / "chain.jsonl", f'{{"key": "{_OPENAI}"}}\n')
        memory = _write(root / "memory" / "notes.jsonl", f'{{"text": "{_OPENAI}"}}\n')

        report = scrub_tree(targets(settings), apply=True)

        assert report.scanned == 0, report.format()
        assert _OPENAI in audit.read_text(encoding="utf-8")
        assert _OPENAI in memory.read_text(encoding="utf-8")


def test_un_fichier_non_textuel_est_ignore() -> None:
    with tmpdir("scrub-binaire") as root:
        settings = make_settings(root)
        binary = settings.paths.log_dir / "capture.png"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"\x89PNG" + _OPENAI.encode())

        report = scrub_tree(targets(settings), apply=True)

        assert report.scanned == 0, report.format()
        assert binary.read_bytes().endswith(_OPENAI.encode())


def test_les_fichiers_non_utf8_sont_laissees_intacts() -> None:
    with tmpdir("scrub-encodage") as root:
        settings = make_settings(root)
        legacy = settings.paths.log_dir / "ancien.log"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        raw = f"clé {_OPENAI}".encode("cp1252", errors="replace") + b"\xff\xfe"
        legacy.write_bytes(raw)

        report = scrub_tree(targets(settings), apply=True)

        assert report.files == (), report.format()
        assert legacy.read_bytes() == raw


if __name__ == "__main__":
    sys.exit(run(globals()))
