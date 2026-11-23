# Dashboard TBW — Livre de règles

> Version 0.2 — 2026-11-23 — MVP
> Ce document fait foi. Toute évolution du produit passe d'abord par une mise à jour de ce fichier.

---

## 0. Vision

Un site qui permet de **créer des dashboards on-chain d'actifs tokenisés** (stablecoins, fonds monétaires tokenisés…) sans écrire de code :
je crée un projet, je déclare ses tokens et leurs déploiements par chaîne, je clique sur **Initialiser**, et le dashboard
se construit tout seul puis se met à jour chaque jour.

Héritage : on reprend ce qui marche dans les dashboards **Spiko** (`E:/Claude/spiko`) et **SG Forge / CoinVertible**
(`E:/Claude/soge`, publié sur bifflou.github.io/stablecoin_dashboard).

### Choix d'architecture MVP : 100 % GitHub

| Rôle | Techno |
|---|---|
| Front public (dashboards, liste des projets) | GitHub Pages, HTML/JS statique |
| Admin (création / édition des projets) | Page statique protégée par un token GitHub |
| Back (récupération et calcul de la donnée) | GitHub Actions + scripts Python |
| Stockage | Fichiers JSON dans le dépôt `Bifflou/dashboard_tbw` |

La séparation front / back est volontairement nette : **le front lit, l'admin écrit la config, les Actions calculent.**

### Qui fait quoi

- **Claude construit l'outil** : le moteur de données, l'admin, le dashboard public.
- **L'utilisateur crée les projets** : c'est lui qui, depuis l'admin, crée chaque projet, saisit les tokens, les
  contrats, les adresses exclues et lance l'initialisation. Le premier vrai dashboard est créé par lui.
- Le code ne contient donc **aucun projet, aucun token et aucune adresse de contrat** codés en dur. Tout passe par
  la config saisie dans l'admin.

---

## 1. Vocabulaire

| Terme | Définition |
|---|---|
| **Projet** | Un dashboard. Ex. « SG Forge ». |
| **Token** | Un actif du projet. Ex. EURCV. Porte la devise, la catégorie et le mode de prix. |
| **Ligne** | Un déploiement d'un token sur une chaîne. Ex. EURCV sur XRPL. |
| **Chaîne** | Une blockchain du registre `config/chains.json`. |
| **Devise de référence** | La devise dans laquelle le token est libellé (EUR, USD, CHF, GBP). |
| **Devise d'affichage** | La devise choisie par le visiteur avec le sélecteur du dashboard. |

---

## 2. Modèle de configuration

### 2.1 Projet

| Champ | Obligatoire | Règle |
|---|---|---|
| Nom | oui | |
| Slug | auto | Dérivé du nom à la création, **immuable** ensuite (c'est l'URL). |
| Image | non | Redimensionnée côté navigateur (max 256×256, PNG/WebP, < 200 Ko). |
| Tokens | oui (≥ 1) | |

Les **devises d'affichage** du projet = l'ensemble des devises de référence de ses tokens (pas de saisie).
La devise par défaut à l'ouverture du dashboard = la devise qui représente la plus grosse market cap.

### 2.2 Token

| Champ | Obligatoire | Règle |
|---|---|---|
| Nom | oui | Ex. « EUR CoinVertible » |
| Symbole | oui | Ex. EURCV — utilisé dans les légendes et le tableau |
| Logo | non | Mêmes règles que l'image projet |
| Devise de référence | oui | EUR, USD, CHF ou GBP. **Au niveau du token, pas de la ligne** : un EURCV est en EUR sur toutes les chaînes. Elle dit seulement quel taux appliquer : les taux eux-mêmes sont globaux (§4.5). |
| Catégorie | oui | `stablecoin` ou `mmf` (liste extensible, cf. 2.4) |
| Type de NAV | si `mmf` | `rebase` ou `accumulating` (capitalisante) (cf. 2.4) |
| Source de NAV | si `accumulating` | cf. 2.5 |
| Date de lancement | non | Sert à interpoler la NAV avant le premier point du feed |
| Lignes | oui (≥ 1) | |

### 2.3 Ligne

| Champ | Obligatoire | Règle |
|---|---|---|
| Chaîne | oui | Choix dans le registre |
| Identifiant | oui | Les champs dépendent de la chaîne, cf. §3 |
| Adresses exclues | non (champ affiché par défaut) | Trésorerie de l'émetteur, bridge, adresse de burn, fonds gelés… Retirées de la supply **et** des holders. Saisies par l'utilisateur dans l'admin, ligne par ligne, à la création ou à la modification du projet. |

Les **décimales ne se saisissent jamais** : elles sont lues on-chain à l'initialisation.

**Identité d'une ligne** : `line_id = {symbole}_{chaîne}_{hash8(identifiant normalisé)}`.
Changer l'identifiant crée donc mécaniquement une nouvelle ligne.

### 2.4 Catégorie et mode de prix

La **catégorie** (ce qu'est l'actif) est séparée du **mode de prix** (comment on calcule sa valeur), pour pouvoir ajouter
d'autres catégories plus tard (or, obligations, actions…) sans toucher au moteur.

| Catégorie | Type de NAV | Mode de prix | Prix unitaire |
|---|---|---|---|
| `stablecoin` | — | `peg` | 1 unité de la devise de référence |
| `mmf` | `rebase` (les intérêts sont versés en nouveaux tokens, ex. BUIDL) | `peg` | 1 |
| `mmf` | `accumulating` — capitalisante (les intérêts augmentent la valeur du token, ex. EUTBL) | `nav_feed` | NAV du jour |

### 2.5 Sources de NAV (`nav_feed`)

Une source par token. Elle s'applique à toutes les lignes du token.

| Priorité | Source | Ce qu'on saisit | Remarques |
|---|---|---|---|
| 1 | **Oracle Chainlink** (ou tout oracle compatible `AggregatorV3` : `latestRoundData` / `getRoundData`) | chaîne + adresse du proxy du feed | NAV officielle, historique complet via les rounds, gratuit via RPC public. C'est ce que fait `spiko/scripts/fetch_nav_data.py`. |
| 2 | **CoinGecko** | identifiant CoinGecko du coin | En secours : c'est un prix de marché, pas une NAV, et le plan gratuit limite l'historique à 1 an. |

Règles :
- La NAV doit être exprimée dans la devise de référence du token.
- Entre deux publications, on garde la dernière NAV connue (week-ends, jours fériés).
- Si le feed démarre après la date de lancement : interpolation linéaire de 1.0 à la date de lancement jusqu'au premier point du feed (méthode Spiko).

---

## 3. Identifiants par chaîne

Chaînes de base du MVP : **Ethereum, Solana, XRPL, Stellar**. Le formulaire affiche les bons champs selon la chaîne et
valide leur format avant d'enregistrer.

| Chaîne | Famille | Champs saisis | Format |
|---|---|---|---|
| Ethereum | `evm` | Adresse du contrat ERC-20 | `0x` + 40 hex |
| Solana | `solana` | Adresse du mint (SPL Token ou Token-2022) | base58 |
| XRPL | `xrpl` | Adresse de l'émetteur + code devise | `r…` + code à 3 caractères ou 40 hex |
| Stellar | `stellar` | Code de l'asset + adresse de l'émetteur | `G…` (asset classique). Contrat Soroban `C…` : hors MVP |

**Chaînes EVM supplémentaires** (Polygon, Base, Arbitrum…) : prévues dans le moteur dès le départ. Ajouter une chaîne EVM
= ajouter une entrée dans `config/chains.json` (nom, logo, `chainid` Etherscan v2, URL d'explorateur). Aucun code à écrire.

---

## 4. Règles de calcul

### 4.1 Granularité

- Un point par **jour UTC**, valeur = état à la **fin** de la journée (23:59:59 UTC).
- Les séries s'arrêtent au **dernier jour UTC complet** (J-1). On n'écrit jamais de journée partielle.
- L'historique d'une ligne commence au jour de sa première émission (premier mint).

### 4.2 Supply circulante (par ligne)

`supply = supply on-chain − Σ soldes des adresses exclues`

Rappel : un solde **gelé** reste compté dans la supply on-chain (le gel bloque les transferts, il ne détruit pas les
tokens). Seuls un burn ou un clawback la font baisser. Pour sortir des fonds gelés de la market cap, on ajoute
l'adresse aux exclusions.

### 4.3 Holders

**Holder = adresse avec un solde strictement positif**, hors émetteur et hors adresses exclues. Même règle sur toutes les chaînes :

| Chaîne | Ce qu'on compte |
|---|---|
| EVM | Adresses avec `balance > 0` |
| Solana | **Propriétaires** (owners) avec un solde > 0, pas les token accounts (un wallet peut en avoir plusieurs) |
| XRPL | Trustlines avec un solde > 0 (une trustline à 0 n'est pas un holder) |
| Stellar | Trustlines avec un solde > 0 |

L'historique des holders est reconstruit **en rejouant les transferts**. Il peut donc baisser, contrairement à
l'approximation « date d'arrivée des holders actuels » utilisée sur Solana dans `soge`.

**Agrégation des holders** :

| Indicateur | Règle |
|---|---|
| Holders par token | Somme des holders des lignes du token |
| Holders par chaîne | Adresses **uniques** de la chaîne qui détiennent au moins un token du projet |
| Total holders | Somme des holders par chaîne |

Exemple : une adresse Ethereum détient EURCV et USDCV. Elle compte 1 fois dans EURCV et 1 fois dans USDCV (détail par
token), mais **1 seule fois** dans Ethereum et dans le total. La somme du détail par token peut donc dépasser le total.
Le dashboard l'indique en petit sous la carte.
On ne dédoublonne pas entre chaînes : impossible de relier une adresse Solana à une adresse Ethereum.

### 4.4 Market cap

- `mcap_native(ligne, jour) = supply × prix_unitaire(token, jour)` (en devise de référence)
- `mcap(devise d'affichage X) = mcap_native × FX(devise de référence → X, jour)`
- **Chaque jour est converti au taux de ce jour-là**, jamais au taux actuel.

### 4.5 Taux de change (global)

- **Un seul fichier pour tous les dashboards** : `data/fx/rates.json`, un taux par devise et par date.
  On ne récupère jamais les taux par projet. Exemple : si deux projets ont chacun un token en EUR et un en USD, le
  taux EUR/USD de chaque jour est récupéré **une seule fois** et lu par les deux projets.
- Le run quotidien met à jour le FX **avant** tous les projets. L'initialisation d'un projet lit le fichier existant
  et ne complète que s'il manque des dates (token plus ancien que la couverture actuelle).
- Source : Frankfurter (`https://api.frankfurter.dev/v1/`), taux BCE, base EUR, devises USD / CHF / GBP.
  Les taux croisés (ex. USD → CHF) se déduisent de la base EUR.
- Récupération incrémentale : seulement les jours manquants (+ les 3 derniers jours, par sécurité).
- Week-ends et jours fériés : on reprend le dernier taux connu.
- Couverture : depuis le 2019-01-01, étendue automatiquement si un token démarre plus tôt.

### 4.6 Variations

1J, 7J et 1M, en % ou en nombre (bouton comme dans `soge`). Une variation est calculée seulement si le point de référence existe.

---

## 5. Écrans

**Toute l'interface est en anglais** (pages publiques et admin). Ce document reste en français.

### 5.1 Liste des projets — public (`index.html`)

Une carte par projet : image, nom, market cap totale (dans sa devise par défaut), logos des chaînes, date de mise à jour.
Un clic ouvre le dashboard.

### 5.2 Dashboard — public (`dashboard.html?p=<slug>`)

Reprise de `soge`.

**En-tête** : image + nom du projet, sélecteur de devise (**seulement les devises du projet**, masqué s'il n'y en a
qu'une), thème clair / sombre.

**Cartes KPI** :
1. Market cap totale, avec le détail par token en dessous
2. Market cap par chaîne
3. Total holders, avec le détail par token en dessous
4. Holders par chaîne

**Graphiques** : market cap par token, market cap par chaîne, holders par token, holders par chaîne.
Bascule ligne / camembert.
**Clic sur une pastille de légende : la série est barrée, masquée, et l'axe se recalcule sur les séries restantes.**

**Tableau** : vue « par token » / « par chaîne », triable, filtrable par chaîne et par token.
Colonnes : holders + 1J/7J/1M, market cap + 1J/7J/1M, bascule %/Nb.

Toutes les valeurs monétaires suivent la devise d'affichage choisie.

### 5.3 Admin — protégé (`admin.html`)

- **Connexion** : je colle un token GitHub *fine-grained*, limité au dépôt `Bifflou/dashboard_tbw`, avec les droits
  Contents (lecture/écriture) et Actions (lecture/écriture). Il est vérifié via l'API GitHub puis gardé dans le
  navigateur (localStorage). Il n'est jamais commité. Sans token, la page ne peut rien faire.
- **Liste des projets** : statut de chaque projet (à initialiser / en cours / OK / erreur / avertissement), date de la
  dernière mise à jour, boutons modifier / supprimer.
- **Formulaire projet** : projet → tokens → lignes, comme décrit en §2.
- **Enregistrer** : commit de `projects/<slug>/project.json` et des images.
- **Initialiser la donnée** : déclenche le workflow d'initialisation pour les lignes « à initialiser ». La page suit le
  run GitHub Actions (en file / en cours / terminé / échec) puis affiche « publié » quand GitHub Pages a redéployé.
- **Fin d'initialisation** : quand le run se termine, l'admin envoie une notification du navigateur (si tu l'as
  autorisée) et change le titre de l'onglet, même s'il est en arrière-plan. L'onglet doit rester ouvert ; sinon, c'est
  la notification externe qui prend le relais (§7.4).

---

## 6. Règles de modification d'un projet

| Action dans l'admin | Effet sur la donnée |
|---|---|
| Ajouter une ligne | La ligne passe « à initialiser ». **Seule cette ligne** est initialisée. |
| Modifier l'identifiant d'une ligne | Nouvelle ligne (nouvel `line_id`) : la donnée de l'ancienne est supprimée, la nouvelle est initialisée. |
| Supprimer une ligne | Sa donnée est supprimée. |
| Modifier les adresses exclues | Ré-initialisation de la ligne. |
| Modifier la devise, la catégorie ou la source de NAV d'un token | Aucun rappel aux chaînes : on recalcule seulement les agrégats (rapide). La NAV est récupérée si la source a changé. |
| Modifier nom, symbole ou logo | Aucun recalcul. |
| Supprimer un projet | Confirmation, puis suppression de sa config et de sa donnée. |

---

## 7. Pipeline de données (GitHub Actions)

### 7.1 Workflows

| Workflow | Déclencheur | Rôle |
|---|---|---|
| `init.yml` | bouton admin (`workflow_dispatch`, entrées : slug + lignes) | Reconstruire l'historique complet des lignes demandées, puis les agrégats du projet. |
| `daily.yml` | cron 06:00 UTC + manuel | FX → NAV → pour chaque ligne initialisée : reprise depuis son curseur → agrégats de chaque projet. |

Les deux workflows partagent un groupe de `concurrency` : ils passent l'un après l'autre et ne commitent jamais en même temps.

GitHub ne garde qu'**un seul run en attente** par groupe : si deux initialisations sont demandées pendant qu'un run
tourne, la première en attente est remplacée par la seconde. C'est pourquoi `init.yml` traite **toutes les lignes à
initialiser de tous les projets**, et pas seulement celles du projet qui a cliqué. Rien n'est donc jamais perdu.
L'admin retrouve son run grâce à un identifiant aléatoire passé au déclenchement (`request_id`, visible dans le nom du run).

Un 3e workflow, `tests.yml`, lance les tests unitaires à chaque push qui touche au code.

### 7.2 Reprise après panne (le « day check »)

- Chaque ligne garde un **curseur** : dernier jour complet traité + position propre à la chaîne (bloc, ledger, signature, paging token).
- Chaque run repart de **curseur − 3 jours** jusqu'à J-1. Que la panne dure 48 h ou 3 semaines, le rattrapage est automatique.
- Le recalcul est **idempotent** : les jours recalculés sont écrasés, jamais dupliqués.

### 7.3 Contrôles et erreurs

- **Rapprochement** : après chaque run, la supply reconstruite au dernier point est comparée à la supply on-chain
  actuelle. Écart au-delà de la tolérance → statut « avertissement » visible dans l'admin (non bloquant).
- **Isolation** : une ligne en erreur ne bloque ni les autres lignes ni les autres projets. Sa donnée précédente est
  conservée, l'erreur est affichée dans l'admin.
- Reprises automatiques avec backoff exponentiel sur les erreurs 429 / 5xx.

### 7.4 Notifications externes

Pour être prévenu même quand l'admin est fermé, la dernière étape des workflows envoie un message :

| Workflow | Quand | Contenu |
|---|---|---|
| `init.yml` | toujours (succès ou échec) | projet, lignes initialisées, erreurs éventuelles, lien vers le dashboard |
| `daily.yml` | seulement en cas d'erreur ou d'avertissement | projet, ligne, message d'erreur |

Canal : **Discord**, via un webhook. Son URL est le secret GitHub `DISCORD_WEBHOOK_URL`. Si le secret n'est pas défini,
l'étape est sautée sans faire échouer le run. Un échec d'envoi à Discord ne fait jamais échouer le run non plus.

---

## 8. Sources de données

Principe : **API d'explorateur ou d'indexeur pour l'historique, RPC ou nœud pour l'état actuel et le rapprochement.**
Pas d'agrégateurs (DeFiLlama, rwa.xyz…) dans le pipeline : ils servent seulement à des vérifications manuelles.

| Chaîne | Historique | État actuel / rapprochement | Clé |
|---|---|---|---|
| Ethereum + EVM | Etherscan API v2 (`chainid`), logs `Transfer`. Secours : Blockscout | RPC public : `totalSupply`, `decimals`, `balanceOf` | `ETHERSCAN_API_KEY` |
| Solana | Helius RPC (signatures + transactions parsées) | Helius `getTokenAccounts` (DAS) | `HELIUS_API_KEY` |
| XRPL | `account_tx` sur l'émetteur (nœuds publics : xrplcluster.com, s1.ripple.com) | `gateway_balances`, `account_lines` | aucune |
| Stellar | Horizon (opérations / effets de l'émetteur) + API stellar.expert | Horizon `/assets`, stellar.expert `/holders` | aucune |
| NAV | Oracle Chainlink via RPC Ethereum public (lots de `getRoundData`) ; CoinGecko en secours | — | `COINGECKO_API_KEY` (seulement si utilisé) |
| FX | Frankfurter | — | aucune |

Les clés vivent **uniquement** dans les secrets GitHub Actions du dépôt. Jamais dans le front, la config ou les JSON publiés.

---

## 9. Organisation du dépôt

```
index.html                     liste publique des projets
dashboard.html                 dashboard public (?p=<slug>)
admin.html                     admin
assets/                        CSS / JS partagés, logos des chaînes
config/chains.json             registre des chaînes
projects/<slug>/project.json   config écrite par l'admin
projects/<slug>/img/           image du projet + logos des tokens
data/projects.json             index lu par la page d'accueil (généré)
data/fx/rates.json             taux de change globaux
data/projects/<slug>/
    lines/<line_id>.json        série journalière : date, supply, holders
    lines/<line_id>.state.json  curseur, soldes courants, périodes de détention par adresse
    nav/<token>.json            NAV journalière
    dashboard.json              agrégat prêt à afficher (toutes devises du projet)
    status.json                 statut par ligne (lu par l'admin)
scripts/                       Python (adaptateurs par chaîne, agrégation, FX, NAV)
tests/                         tests unitaires
tests/fixtures/                configs de test (pas des projets, cf. §11)
docs/FORMATS.md                format détaillé de chaque fichier JSON
.github/workflows/             init.yml, daily.yml, tests.yml
```

`dashboard.json` contient tout ce que le dashboard affiche, déjà calculé : séries par token et par chaîne, market cap
dans chaque devise du projet, holders dédoublonnés. **Le front ne fait aucun calcul métier**, il affiche.

Les périodes de détention par adresse (dans `.state.json`) servent à calculer les holders uniques par chaîne, jour par jour.

---

## 10. Front

- HTML / CSS / JS sans framework ni étape de build, Chart.js 4.4.3 (comme `soge` et `spiko`).
- Thème clair / sombre, utilisable sur mobile.
- Logos des chaînes livrés avec le site, logos des tokens et des projets uploadés depuis l'admin.

---

## 11. Plan de construction

Chaque étape est validée avant de passer à la suivante.

**Comment on teste sans créer de projet.** Le moteur est testé en local avec des **fixtures** : des fichiers de config
de test dans `tests/fixtures/`, qui pointent vers des tokens déjà suivis par `soge` et `spiko`. Les résultats vont dans
un dossier de sortie ignoré par Git. Les fixtures ne sont **pas** des projets : elles ne passent jamais par `projects/`
ni par `data/projects/` et n'apparaissent pas sur le site.
Ce qui demande une clé d'API (Etherscan, Helius) tourne **sur GitHub** : le workflow `engine-check.yml` se lance à
chaque push qui touche au moteur, utilise les secrets du dépôt, ne commite rien et publie ses résultats en annotations
(lisibles par l'API publique de GitHub). Il vérifie aussi qu'une initialisation suivie de mises à jour donne exactement
le même résultat qu'une initialisation complète.
Critère principal : **sur les mêmes tokens, les chiffres doivent correspondre à ceux de `soge` et `spiko`**, aux écarts
de méthode près (ceux-ci sont documentés).

1. **Socle** : structure du dépôt, `chains.json`, FX global, format des JSON, squelettes des workflows.
2. **Moteur Ethereum** : adaptateur, reprise par curseur, agrégats. Testé sur fixture, comparaison avec `soge`.
3. **Moteurs XRPL, Stellar, Solana** : un adaptateur à la fois, chacun comparé avec `soge`.
4. **NAV** : Chainlink + interpolation. Testé sur fixture, comparaison avec `spiko`.
5. **Admin** : connexion par token, formulaire, upload, enregistrement, déclenchement et suivi de l'initialisation.
6. **Dashboard public** : reprise de l'interface de `soge` branchée sur `dashboard.json`, plus la page liste.
7. **Recette** : l'utilisateur crée lui-même le premier projet depuis l'admin et lance l'initialisation. On corrige ce
   qui coince.

---

## 12. Hors MVP (prévu dans la conception, pas développé)

- Tokens MPT sur XRPL, tokens Soroban custom sur Stellar
- Chaînes non EVM supplémentaires (Starknet…) et chaînes EVM absentes d'Etherscan (Etherlink → Blockscout)
- Devises au-delà de EUR / USD / CHF / GBP
- Nouvelles catégories (or, obligations, actions…)
- NAV dans une autre devise que celle du token
- Vrai backend (serveur + base de données) si l'outil devient multi-utilisateur

---

## 13. Points ouverts et risques

| # | Sujet | Statut |
|---|---|---|
| R1 | **Historique des holders sur Solana** : rejouer tous les transferts coûte plus cher que les seuls mint/burn. Méthode à valider sur EURCV / USDCV à l'étape 3. | à valider |
| R2 | Chaînes EVM couvertes par le plan gratuit Etherscan v2 : à vérifier par chaîne, sinon Blockscout. | à vérifier quand on ajoutera une chaîne EVM |
| R3 | Taille du dépôt : les fichiers d'état grossissent avec le nombre de holders. OK pour des tokens à quelques milliers de holders ; à surveiller. | à surveiller |
| D1 | Langue de l'interface : **anglais** (cf. §5). | décidé le 2026-11-23 |
| D2 | Git : une seule branche `main`, pas de PR, **toujours `pull --rebase` avant `push`** (cf. CLAUDE.md). | décidé le 2026-11-23 |
| D3 | Notifications externes (§7.4) : **webhook Discord**, secret `DISCORD_WEBHOOK_URL`. | décidé le 2026-11-23 |
