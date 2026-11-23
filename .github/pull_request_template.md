## Ce que change cette PR

<!-- Une ou deux phrases : le problème, et ce qui le règle. -->

## Vérifications

- [ ] `python -m ruff check --select F,B,E9 security tools config.py core tests ui app.py smoke_exe.py pyinstaller_recipe.py build_smoke`
- [ ] `python -m pytest -q` (la CI ajoute `OMNIAGENT_SLOW_TESTS=1`)
- [ ] `python -m mypy core config.py security`
- [ ] Aucun test désactivé, aucune assertion affaiblie, aucun `# type: ignore` ajouté pour faire passer un contrôle.

## Si cette PR touche `.github/workflows/`

- [ ] Le job agrégé s'appelle toujours `Vérifications obligatoires` : c'est le seul check exigé par la protection de `master`. Le renommer, c'est bloquer toutes les fusions.
- [ ] `release.yml` publie **à la demande** : `--version` doit égaler `APP_VERSION`, l'état du tag est contrôlé avant construction, et le tag n'est créé qu'à la fin (`gh release create --target`). Un échec de construction ne doit laisser aucun tag `v*` derrière lui.
- [ ] Le remplacement d'assets reste réversible : les assets présents sont republiés en `precedent-*` avant `--clobber`.

## Notes de version

<!-- Sert à `--generate-notes` et au CHANGELOG : une ligne compréhensible par un utilisateur. -->
