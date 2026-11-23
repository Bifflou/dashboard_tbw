# Dashboard TBW — instructions pour Claude

Le produit est décrit dans [SPEC.md](SPEC.md), qui fait foi. Si une demande contredit SPEC.md, on met d'abord SPEC.md à
jour (avec accord) avant de coder.

## Rôles

- **Claude construit l'outil** (moteur, admin, dashboard). **L'utilisateur crée les projets** depuis l'admin.
- Ne jamais créer de projet dans `projects/`, ni de donnée dans `data/projects/`, ni déclencher `init.yml` pour un projet.
- Aucun projet, token, contrat ou adresse codé en dur dans le code ou la config. `config/chains.json` ne décrit que des chaînes.
- Les tests passent par `tests/fixtures/` en local, avec une sortie ignorée par Git (cf. SPEC §11).

## Git

- Une seule branche : `main`. Pas de pull request, pas de branche durable.
- **Toujours `git pull --rebase origin main` avant `git push`** : les GitHub Actions commitent la donnée sur `main`
  chaque jour, il faut se mettre à jour avant de pousser.
- Les workflows suivent la même règle avant leur propre push.
- Si le travail se fait dans un worktree : ramener la branche sur `main` en fast-forward, pousser, puis supprimer la
  branche du worktree (locale et distante).

## Références

- `E:/Claude/soge` — dashboard SG Forge (EURCV / USDCV sur Ethereum, Solana, XRPL, Stellar). Référence pour l'interface et les adaptateurs XRPL / Stellar / Solana.
- `E:/Claude/spiko` — dashboard Spiko. Référence pour la NAV Chainlink (`scripts/fetch_nav_data.py`), les chaînes EVM et le FX partagé.
- On s'inspire de leur code, mais on ne copie pas leurs approximations (cf. SPEC §4.3 holders).

## Règles de données

- Sources autorisées : API d'explorateur / indexeur (Etherscan v2, Blockscout, Helius, Horizon, stellar.expert),
  RPC / nœuds publics, Chainlink, Frankfurter. CoinGecko seulement comme source de NAV de secours.
- **Jamais** de DeFiLlama, rwa.xyz ou autre agrégateur dans le pipeline (vérification manuelle seulement).
- Jours en **UTC**, séries jusqu'à J-1 inclus, jamais de journée partielle.
- Décimales toujours lues on-chain, jamais en dur.
- Tout recalcul est idempotent : on écrase les jours recalculés, on ne duplique jamais.
- Taux de change : **un seul** fichier global `data/fx/rates.json`, partagé par tous les projets.
- Une ligne en erreur ne doit jamais casser les autres lignes ni les autres projets.
- Toute nouvelle source ou particularité de chaîne découverte (décimales, format de code devise…) est documentée dans
  SPEC.md §8 avec sa date de vérification.

## Règles de code

- Back : Python 3.12, dépendance `requests` uniquement (sauf besoin justifié). Un adaptateur par famille de chaînes
  (`evm`, `solana`, `xrpl`, `stellar`) avec la même interface ; ajouter une chaîne EVM = config, pas code.
- Front : HTML / CSS / JS sans framework ni build, Chart.js 4.4.3. Le front ne fait **aucun** calcul métier : il lit
  `dashboard.json`. Tous les textes de l'interface sont en anglais.
- Les clés d'API et le webhook (`ETHERSCAN_API_KEY`, `HELIUS_API_KEY`, `COINGECKO_API_KEY`, `DISCORD_WEBHOOK_URL`)
  viennent des secrets GitHub Actions via les variables d'environnement. Jamais dans le code, la config, les JSON
  publiés, les logs ou le front.
- Le token GitHub de l'admin reste dans le navigateur de l'utilisateur : jamais commité, jamais loggé.

## Commandes

Sur ce poste Windows, `python` est le raccourci du Microsoft Store : utiliser `py -3.12`.

- Tests : `py -3.12 -m unittest discover -s tests`
- Taux de change : `py -3.12 -m scripts.fetch_fx`
- Scripts du pipeline : `py -3.12 -m scripts.run_init`, `py -3.12 -m scripts.run_daily`, `py -3.12 -m scripts.build_index`
- Vérification du moteur sur données réelles : `py -3.12 -m scripts.check_engine [fixture]`. Les clés d'API ne sont
  pas sur ce poste : ce check tourne sur GitHub (`engine-check.yml`, déclenché par le push). Résultats lisibles sans
  token via `https://api.github.com/repos/Bifflou/dashboard_tbw/check-runs/<job_id>/annotations`.
- Les formats de tous les fichiers JSON sont dans [docs/FORMATS.md](docs/FORMATS.md).

## Validation

- Avant de déclarer une étape terminée : comparer les chiffres avec `soge` / `spiko` sur les mêmes tokens et noter les
  écarts de méthode attendus.
- Tester le front dans le navigateur (clair / sombre, mobile) avant de dire qu'il fonctionne.
