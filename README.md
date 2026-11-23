# Dashboard TBW

Outil de création de dashboards on-chain pour actifs tokenisés (stablecoins, fonds monétaires tokenisés) :
market cap et holders par token et par chaîne, historique jour par jour, mise à jour quotidienne.

- **Produit et règles** : [SPEC.md](SPEC.md)
- **Formats des fichiers** : [docs/FORMATS.md](docs/FORMATS.md)
- **Règles de développement** : [CLAUDE.md](CLAUDE.md)

## Fonctionnement

100 % GitHub : le site est servi par GitHub Pages, la donnée est calculée par GitHub Actions (scripts Python) et
stockée en JSON dans ce dépôt.

| Workflow | Rôle |
|---|---|
| `daily.yml` | Tous les jours à 06:00 UTC : taux de change, puis mise à jour de chaque projet |
| `init.yml` | Déclenché par l'admin : reconstruction de l'historique des lignes à initialiser |
| `tests.yml` | Tests unitaires à chaque push qui touche au code |

## Secrets GitHub Actions

| Secret | Usage |
|---|---|
| `ETHERSCAN_API_KEY` | Ethereum et chaînes EVM |
| `HELIUS_API_KEY` | Solana |
| `DISCORD_WEBHOOK_URL` | Notifications (fin d'initialisation, erreurs du run quotidien) |
| `COINGECKO_API_KEY` | Optionnel : source de NAV de secours |

## En local

```
pip install -r requirements.txt
python -m unittest discover -s tests
python -m scripts.fetch_fx
```
