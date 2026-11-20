# Cortex évolutif

Ce dossier est la seule partie de l'agent que le moteur d'auto-évolution peut
modifier. Le noyau (`ui/`, `core/`, `.exe`) reste immuable.

- `skills/` : compétences Python chargées à chaud (`core/skill_loader.py`).
  Chaque module expose `NAME`, `DESCRIPTION`, `PARAMETERS` (schéma objet JSON)
  et `run(**kwargs)`. Les imports `subprocess`, `socket`, `ctypes`, `winreg` et
  `multiprocessing` sont refusés, ainsi que `eval`, `exec`, `compile` et
  `os.system`.
- `prompts/adaptations.md` : prompt d'adaptation, injecté dans le prompt
  système de l'agent. Réécrit par le moteur, borné par
  `evolution.max_prompt_chars`.

Le moteur est inactif par défaut. Il ne démarre qu'avec
`evolution.enabled = true`, après `idle_minutes` d'inactivité, au plus
`max_cycles_per_day` fois par jour, et les cycles forcés à
`max_forced_cycles_per_day` ; le budget global est vérifié avant chaque cycle.
Chaque écriture est précédée d'un
instantané dans `data/evolution/backups/`, et une compétence dont le test
échoue est annulée.
