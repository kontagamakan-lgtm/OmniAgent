"""Tests du gestionnaire de fichiers."""

from __future__ import annotations

import sys
import zipfile

from support import make_settings, run, tmpdir  # noqa: I001

from tools.base import ToolSet
from tools.file_manager import (
    FileManager,
    FileOperationError,
    PathNotAllowed,
    build_file_tools,
)


def _manager(tmp, **overrides) -> FileManager:
    settings = make_settings(tmp)
    if "denied_globs" in overrides:
        settings.files.denied_globs = overrides["denied_globs"]
    return FileManager(settings)


def test_write_then_read_roundtrip() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        written = manager.write_text("notes.txt", "Bonjour\nSeconde ligne\n")
        assert written["bytes_written"] > 0
        payload = manager.read_text("notes.txt")
        assert payload["text"].startswith("Bonjour")
        assert payload["encoding"] == "utf-8-sig"
        assert payload["lines"] == 2


def test_read_detects_cp1252() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        (tmp / "legacy.txt").write_bytes("Café crème".encode("cp1252"))
        payload = manager.read_text("legacy.txt")
        assert "Café" in payload["text"]


def test_list_dir_with_pattern() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("a.txt", "a")
        manager.write_text("b.pdf", "b")
        manager.create_dir("sous-dossier")
        listing = manager.list_dir(".", pattern="*.pdf")
        assert listing["count"] == 1
        full = manager.list_dir(".")
        assert full["entries"][0]["type"] == "dir"
        assert {entry["name"] for entry in full["entries"]} == {"a.txt", "b.pdf", "sous-dossier"}


def test_path_escapes_are_rejected() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        attempts = ["../evasion.txt", r"C:\Windows\win.ini", "*.txt"]
        for attempt in attempts:
            try:
                manager.read_text(attempt)
            except PathNotAllowed:
                continue
            raise AssertionError(f"chemin accepté à tort : {attempt}")


def test_denied_globs_are_enforced() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp, denied_globs=["**/secret/**"])
        # Le fichier est créé hors du gestionnaire : c'est bien l'accès via
        # l'outil qui doit être refusé, pas seulement l'écriture.
        (tmp / "secret").mkdir()
        (tmp / "secret" / "cle.txt").write_text("donnée sensible", encoding="utf-8")
        for operation in (manager.read_text, manager.info):
            try:
                operation("secret/cle.txt")
            except PathNotAllowed:
                continue
            raise AssertionError(f"{operation.__name__} aurait dû refuser un chemin exclu")
        try:
            manager.write_text("secret/nouveau.txt", "x")
        except PathNotAllowed:
            return
        raise AssertionError("l'écriture dans un chemin exclu doit être refusée")


def test_reserved_names_are_rejected() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        for name in ("CON.txt", "nul", "COM1.log"):
            try:
                manager.write_text(name, "x")
            except PathNotAllowed:
                continue
            raise AssertionError(f"nom réservé accepté : {name}")


def test_info_and_hash() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("rapport.md", "# Titre")
        info = manager.info("rapport.md", with_hash=True)
        assert info["is_file"] and info["size_bytes"] > 0
        assert len(info["sha256"]) == 64
        assert info["suffix"] == ".md"


def test_backup_before_overwrite() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("config.ini", "version=1")
        manager.write_text("config.ini", "version=2")
        assert (tmp / "config.ini.bak").read_text(encoding="utf-8") == "version=1"
        assert manager.read_text("config.ini")["text"] == "version=2"


def test_copy_renames_on_conflict() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("source.txt", "contenu")
        manager.create_dir("cible")
        manager.copy("source.txt", "cible")
        second = manager.copy("source.txt", "cible")
        assert second["results"][0]["target"].endswith("source (1).txt")
        assert sorted(p.name for p in (tmp / "cible").iterdir()) == ["source (1).txt", "source.txt"]


def test_copy_overwrite_strategy() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("a.txt", "v1")
        manager.create_dir("d")
        manager.write_text("d/a.txt", "ancien")
        manager.copy("a.txt", "d", conflict="overwrite")
        assert (tmp / "d" / "a.txt").read_text(encoding="utf-8") == "v1"


def test_move_and_delete_permanent() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("mobile.txt", "x")
        manager.create_dir("archive")
        manager.move("mobile.txt", "archive")
        assert (tmp / "archive" / "mobile.txt").is_file()
        result = manager.delete("archive/mobile.txt", use_recycle_bin=False)
        assert result["succeeded"] == 1
        assert not (tmp / "archive" / "mobile.txt").exists()


def test_delete_uses_recycle_bin() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("corbeille.txt", "x")
        result = manager.delete("corbeille.txt")
        assert result["succeeded"] == 1, result
        assert not (tmp / "corbeille.txt").exists()


def test_root_deletion_is_refused() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        result = manager.delete(".", use_recycle_bin=False)
        assert result["failed"] == 1
        assert tmp.exists()


def test_rename_bulk_dry_run_then_apply() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("IMG_001.jpg", "1")
        manager.write_text("IMG_002.jpg", "2")

        preview = manager.rename_bulk(".", r"IMG_(\d+)", r"Photo_\1")
        assert preview["dry_run"] is True and preview["planned"] == 2
        assert (tmp / "IMG_001.jpg").exists()

        applied = manager.rename_bulk(".", r"IMG_(\d+)", r"Photo_\1", dry_run=False)
        assert applied["planned"] == 2
        assert (tmp / "Photo_001.jpg").exists()
        assert not (tmp / "IMG_001.jpg").exists()


def test_rename_bulk_detects_collisions() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("a1.txt", "1")
        manager.write_text("a2.txt", "2")
        plan = manager.rename_bulk(".", r"a\d", "a", dry_run=True)
        statuses = sorted(change["status"] for change in plan["changes"])
        assert "collision" in statuses
        assert plan["planned"] == 1


def test_rename_bulk_rolls_back_on_error() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("x1.txt", "1")
        manager.write_text("y1.txt", "2")
        # Le second remplacement produit un nom déjà pris par un autre fichier
        # hors du plan : la collision est détectée avant écriture.
        manager.write_text("z.txt", "3")
        plan = manager.rename_bulk(".", r"[xy]1", "z", dry_run=True)
        assert any(change["status"] == "collision" for change in plan["changes"])


def test_search_by_name_and_content() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.create_dir("projets")
        manager.write_text("projets/facture.txt", "Montant total : 1200 EUR\nClient : ACME\n")
        manager.write_text("projets/notes.txt", "Rien à signaler\n")

        by_name = manager.search("facture")
        assert by_name["count"] == 1 and by_name["hits"][0]["kind"] == "nom"

        by_content = manager.search(content_regex=r"ACME", glob="*.txt")
        assert by_content["count"] == 1
        assert by_content["hits"][0]["line"] == 2


def test_find_duplicates() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.create_dir("docs")
        manager.write_text("docs/1.txt", "contenu identique")
        manager.write_text("docs/2.txt", "contenu identique")
        manager.write_text("docs/3.txt", "contenu différent")
        report = manager.find_duplicates(".")
        assert report["groups"] == 1
        assert report["duplicates"][0]["count"] == 2
        assert report["wasted_bytes"] > 0


def test_organize_dry_run_leaves_files_in_place() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("rapport.pdf", "x")
        manager.write_text("photo.png", "y")
        report = manager.organize(".", dry_run=True)
        assert report["files"] == 2
        assert set(report["categories"]) == {"Documents", "Images"}
        assert (tmp / "rapport.pdf").exists()

        applied = manager.organize(".", dry_run=False)
        assert applied["files"] == 2
        assert (tmp / "Documents" / "rapport.pdf").is_file()
        assert (tmp / "Images" / "photo.png").is_file()


def test_archive_and_extract_roundtrip() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.create_dir("projet/src")
        manager.write_text("projet/src/main.py", "print('ok')")
        created = manager.archive("projet", "sauvegarde.zip")
        assert created["entries"] == 1

        (tmp / "sauvegarde.zip").rename(tmp / "archive.zip")
        extracted = manager.extract("archive.zip", "restauration")
        assert extracted["files"] == 1
        assert (tmp / "restauration" / "projet" / "src" / "main.py").is_file()


def test_zip_slip_is_blocked() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        with zipfile.ZipFile(tmp / "piege.zip", "w") as archive:
            archive.writestr("../evasion.txt", "attaque")
        try:
            manager.extract("piege.zip", "sortie")
        except FileOperationError as exc:
            assert "archive suspecte" in str(exc)
            return
        raise AssertionError("une archive avec traversée de répertoire doit être refusée")


def test_convert_csv_to_xlsx() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("donnees.csv", "nom;ville\njean;Paris\nmarie;Lyon\n")
        result = manager.convert("donnees.csv", "xlsx")
        assert result["format"] == "xlsx"
        import openpyxl

        workbook = openpyxl.load_workbook(tmp / "donnees.xlsx")
        values = [[cell.value for cell in row] for row in workbook.active.iter_rows()]
        assert values[0] == ["nom", "ville"]
        assert values[1] == ["jean", "Paris"]


def test_convert_text_to_docx_and_back() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("note.md", "# Titre\n\nPremier paragraphe.\n")
        created = manager.convert("note.md", "docx")
        assert created["format"] == "docx"
        manager.convert("note.docx", "txt")
        text = (tmp / "note.txt").read_text(encoding="utf-8")
        assert "Titre" in text
        assert "Premier paragraphe." in text


def test_convert_image_to_jpeg() -> None:
    with tmpdir("fs") as tmp:
        from PIL import Image

        manager = _manager(tmp)
        Image.new("RGBA", (32, 32), (255, 0, 0, 128)).save(tmp / "carre.png")
        result = manager.convert("carre.png", "jpg")
        assert result["format"] == "jpg"
        with Image.open(tmp / "carre.jpg") as opened:
            assert opened.mode == "RGB"


def test_unsupported_conversion_is_reported() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        manager.write_text("note.txt", "x")
        try:
            manager.convert("note.txt", "mp3")
        except FileOperationError as exc:
            assert "non prise en charge" in str(exc)
            return
        raise AssertionError("une conversion inconnue doit échouer explicitement")


def test_disk_usage() -> None:
    with tmpdir("fs") as tmp:
        manager = _manager(tmp)
        usage = manager.disk_usage(".")
        assert usage["total_bytes"] > 0 and 0 <= usage["percent_used"] <= 100


def test_tool_layer_validates_arguments() -> None:
    with tmpdir("fs") as tmp:
        tools = ToolSet(build_file_tools(_manager(tmp)))
        bad = tools.dispatch("fs_write_text", {"path": "a.txt", "content": 42})
        assert bad.ok is False and bad.error_code == "invalid_arguments"

        missing = tools.dispatch("fs_info", {})
        assert missing.ok is False and missing.error_code == "invalid_arguments"

        good = tools.dispatch("fs_write_text", {"path": "a.txt", "content": "texte"})
        assert good.ok is True
        read = tools.dispatch("fs_read_text", {"path": "a.txt"})
        assert read.ok is True and read.data["text"] == "texte"


def test_tool_layer_reports_unknown_tool() -> None:
    with tmpdir("fs") as tmp:
        tools = ToolSet(build_file_tools(_manager(tmp)))
        result = tools.dispatch("fs_introuvable", {})
        assert result.ok is False and result.error_code == "unknown_tool"


if __name__ == "__main__":
    sys.exit(run(globals()))
