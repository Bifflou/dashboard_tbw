# Formats des fichiers

> Complète [SPEC.md](../SPEC.md) §9. Toute modification d'un format incrémente son `schema_version`.
> Conventions communes : dates de jour `YYYY-MM-DD` (UTC), horodatages ISO 8601 en UTC (`2026-11-23T06:00:00Z`),
> devises `EUR | USD | CHF | GBP`, montants on-chain bruts en **chaîne de caractères** (entiers sans limite de taille),
> montants affichables en nombres flottants.

---

## 1. `config/chains.json` — registre des chaînes

Écrit à la main (par Claude). Deux parties :

- `families` : pour chaque famille (`evm`, `solana`, `xrpl`, `stellar`), la liste des champs d'identifiant d'une ligne
  et le format d'une adresse exclue. Chaque champ a un `key`, un `label` (affiché dans l'admin), un `placeholder`, un
  `pattern` (regex compatible Python et JavaScript) et une règle `normalize` :
  - `lower` / `upper` : casse forcée ;
  - `none` : valeur gardée telle quelle (les adresses base58 sont sensibles à la casse) ;
  - `xrpl_currency` : code à 3 caractères gardé tel quel, code hexadécimal de 40 caractères passé en majuscules.
- `chains` : une entrée par chaîne (`name`, `family`, `logo`, URL d'explorateur, et un bloc propre à la famille :
  nœuds pour XRPL…). Pour l'EVM : `etherscan_chainid`, `rpc_urls` (lectures d'état), `logs_rpc` (nœuds pour
  `eth_getLogs`, `"archive": true` s'ils servent tout l'historique) et `logs_max_range` (taille des fenêtres).

Les gabarits d'URL d'explorateur utilisent les clés d'identifiant (`{address}`, `{mint}`, `{issuer}`, `{currency}`,
`{code}`) et `{account}` pour une adresse quelconque.

**Ajouter une chaîne EVM = ajouter une entrée dans `chains`**, avec `"family": "evm"` et son `etherscan_chainid`.

---

## 2. `projects/<slug>/project.json` — config d'un projet

Écrit **uniquement par l'admin** (donc par l'utilisateur). Les images sont à côté, dans `projects/<slug>/img/`.

```json
{
  "schema_version": 1,
  "slug": "my-project",
  "name": "My Project",
  "image": "img/project.webp",
  "created_at": "2026-11-23T10:00:00Z",
  "updated_at": "2026-11-23T10:00:00Z",
  "tokens": [
    {
      "id": "eurx",
      "name": "Euro Example",
      "symbol": "EURX",
      "logo": "img/eurx.webp",
      "currency": "EUR",
      "category": "stablecoin",
      "nav_type": null,
      "nav_source": null,
      "launch_date": null,
      "lines": [
        {
          "id": "eurx_ethereum_1a2b3c4d",
          "chain": "ethereum",
          "identifier": { "address": "0x…" },
          "excluded_addresses": ["0x…"]
        }
      ]
    }
  ]
}
```

| Champ | Règle |
|---|---|
| `slug` | `^[a-z0-9]+(-[a-z0-9]+)*$`, dérivé du nom à la création, **immuable**. Égal au nom du dossier. |
| `image`, `logo` | Chemin relatif au dossier du projet, ou `null`. |
| `tokens[].id` | `^[a-z0-9]{1,32}$`, dérivé du symbole à la création, **immuable**, unique dans le projet. |
| `currency` | `EUR`, `USD`, `CHF` ou `GBP`. |
| `category` | `stablecoin` ou `mmf`. |
| `nav_type` | `null` pour un stablecoin ; `rebase` ou `accumulating` (NAV capitalisante) pour un MMF. |
| `nav_source` | Obligatoire si et seulement si `nav_type = accumulating`. `{"type": "chainlink", "chain": "<chaîne evm>", "address": "0x…"}` ou `{"type": "coingecko", "coin_id": "…"}`. |
| `launch_date` | `YYYY-MM-DD` ou `null`. Sert à interpoler la NAV avant le premier point du feed. |
| `lines[].identifier` | Exactement les clés de la famille de la chaîne (§1), au format normalisé. |
| `lines[].excluded_addresses` | Liste (éventuellement vide) d'adresses au format de la famille, normalisées, sans doublon. |

**Mode de prix** (dérivé, jamais stocké) : `peg` pour un stablecoin ou un MMF `rebase`, `nav_feed` pour un MMF `accumulating`.

### Identité et empreinte d'une ligne

- **Forme canonique** de l'identifiant : `<chaîne>|<valeur 1>|<valeur 2>…`, valeurs normalisées dans l'ordre des champs
  de la famille. Ex. `ethereum|0xabc…`, `xrpl|rIssuer…|EUR`, `stellar|EURCV|GIssuer…`.
- **`line_id`** = `<token_id>_<chaîne>_<8 premiers caractères hex du SHA-256 de la forme canonique>`.
  Changer l'identifiant change donc le `line_id`, ce qui déclenche une nouvelle initialisation (SPEC §6).
- **Empreinte** (`fingerprint`) = SHA-256 de `<forme canonique>#<adresses exclues triées, séparées par des virgules>`.
  Si l'empreinte de la config diffère de celle de la donnée, la ligne est « à initialiser ».

Le même calcul est fait en Python (`scripts/lib/project.py`) et en JavaScript (admin). Les tests figent des valeurs de
référence pour garantir que les deux donnent le même résultat.

---

## 3. `data/fx/rates.json` — taux de change globaux

Écrit par `scripts/fetch_fx.py`, partagé par tous les projets.

```json
{
  "schema_version": 1,
  "source": "https://api.frankfurter.dev/v1 (ECB reference rates)",
  "base": "EUR",
  "currencies": ["CHF", "GBP", "USD"],
  "start": "2019-01-01",
  "end": "2026-11-22",
  "last_published": "2026-11-21",
  "updated_at": "2026-11-23T06:00:00Z",
  "rates": {
    "2019-01-01": { "CHF": 1.1269, "GBP": 0.89453, "USD": 1.1450 }
  }
}
```

- `rates` contient **chaque jour calendaire** de `start` à `end` (J-1). Les jours sans publication BCE (week-ends,
  jours fériés) reprennent le dernier taux publié.
- `last_published` : dernier jour réellement publié par la BCE. À chaque mise à jour, on recharge depuis
  `min(last_published, end) − 3 jours`.
- Conversion d'un montant de la devise A vers B le jour d : `montant × rates[d][B] / rates[d][A]`, avec `rates[d]["EUR"] = 1`.

---

## 4. `data/projects/<slug>/lines/<line_id>.json` — série d'une ligne

```json
{
  "schema_version": 1,
  "line_id": "eurx_ethereum_1a2b3c4d",
  "token_id": "eurx",
  "chain": "ethereum",
  "fingerprint": "…",
  "decimals": 6,
  "first_date": "2024-04-30",
  "last_date": "2026-11-22",
  "updated_at": "2026-11-23T06:05:00Z",
  "days": [
    { "date": "2024-04-30", "supply": 1000000.0, "holders": 3 }
  ]
}
```

- `days` couvre **chaque jour** de `first_date` à `last_date`, sans trou.
- `supply` = supply circulante (supply on-chain − adresses exclues), en unités du token.
- `holders` = adresses avec un solde > 0, hors émetteur et adresses exclues (SPEC §4.3).

## 5. `data/projects/<slug>/lines/<line_id>.state.json` — état de reprise

Sert à reprendre sans tout recalculer. Le principe est un **point de contrôle placé 3 jours avant `last_date`** :
chaque run recharge l'état à ce point, rejoue les événements jusqu'à J-1, réécrit les jours concernés, puis enregistre
un nouveau point de contrôle à J-1 − 3 jours. Une panne de N jours se rattrape donc d'elle-même, sans doublon.

```json
{
  "schema_version": 1,
  "line_id": "eurx_ethereum_1a2b3c4d",
  "fingerprint": "…",
  "decimals": 6,
  "checkpoint": {
    "date": "2026-11-19",
    "position": {},
    "supply_raw": "1000000000000",
    "balances": { "0x…": "1000000" }
  },
  "holder_periods": { "0x…": [["2024-04-30", "2025-01-10"], ["2025-03-02", null]] }
}
```

- `checkpoint.position` : position propre à la chaîne (bloc, ledger, signature, curseur de pagination). Son contenu
  sera défini avec chaque adaptateur (étapes 2 et 3).
- `checkpoint.balances` : soldes bruts > 0 à la fin du jour `checkpoint.date`, **toutes adresses comprises** (les
  exclusions sont appliquées au calcul, pas au stockage).
- `holder_periods` : pour chaque holder (hors émetteur et exclues), ses périodes de détention `[début, fin]`
  (jours inclus, `fin = null` = toujours holder à `last_date`). Sert à compter les holders **uniques par chaîne**
  jour par jour (SPEC §4.3).

## 6. `data/projects/<slug>/nav/<token_id>.json` — NAV d'un token

```json
{
  "schema_version": 1,
  "token_id": "fundx",
  "currency": "EUR",
  "source": { "type": "chainlink", "chain": "ethereum", "address": "0x…" },
  "first_date": "2024-04-30",
  "last_date": "2026-11-22",
  "updated_at": "2026-11-23T06:01:00Z",
  "days": [
    { "date": "2024-04-30", "nav": 1.0, "origin": "interpolated" },
    { "date": "2024-06-01", "nav": 1.0061, "origin": "feed" },
    { "date": "2024-06-02", "nav": 1.0061, "origin": "carried" }
  ]
}
```

`origin` : `feed` (publié par la source ce jour-là), `carried` (dernière valeur reprise), `interpolated` (avant le premier point du feed).

## 7. `data/projects/<slug>/status.json` — statut des lignes

Lu par l'admin.

```json
{
  "schema_version": 1,
  "project": "my-project",
  "updated_at": "2026-11-23T06:05:00Z",
  "lines": {
    "eurx_ethereum_1a2b3c4d": {
      "fingerprint": "…",
      "state": "ok",
      "last_run": "2026-11-23T06:05:00Z",
      "last_date": "2026-11-22",
      "message": null,
      "reconciliation": { "onchain": 1000000.0, "reconstructed": 1000000.0, "diff_pct": 0.0 }
    }
  }
}
```

`state` : `ok`, `warning` (écart de rapprochement), `error` (dernier run en échec, donnée précédente conservée).
Une ligne de la config **absente** de ce fichier, ou dont l'empreinte diffère, est « à initialiser ».

## 8. `data/projects/<slug>/dashboard.json` — agrégat affiché

Tout ce que le dashboard affiche, déjà calculé. Toutes les séries sont alignées sur `dates` (`null` avant le début
d'une ligne).

```json
{
  "schema_version": 1,
  "slug": "my-project",
  "name": "My Project",
  "image": "projects/my-project/img/project.webp",
  "generated_at": "2026-11-23T06:06:00Z",
  "last_date": "2026-11-22",
  "currencies": ["EUR", "USD"],
  "default_currency": "EUR",
  "chains": [{ "id": "ethereum", "name": "Ethereum", "logo": "assets/chains/ethereum.png" }],
  "tokens": [{ "id": "eurx", "name": "Euro Example", "symbol": "EURX", "logo": "projects/my-project/img/eurx.webp",
               "currency": "EUR", "category": "stablecoin", "nav_type": null,
               "lines": [{ "id": "eurx_ethereum_1a2b3c4d", "chain": "ethereum", "explorer_url": "…" }] }],
  "dates": ["2024-04-30", "…"],
  "price": { "eurx": [1.0, "…"] },
  "supply": { "by_line": { "eurx_ethereum_1a2b3c4d": [1000000.0, "…"] } },
  "mcap": {
    "EUR": { "total": [], "by_token": {}, "by_chain": {}, "by_line": {} },
    "USD": { "total": [], "by_token": {}, "by_chain": {}, "by_line": {} }
  },
  "holders": { "total": [], "by_token": {}, "by_chain": {}, "by_line": {} }
}
```

- Les chemins d'images (`image`, `logo`) sont relatifs à la racine du site, directement utilisables par le front.
- Une ligne dont la dernière mise à jour a échoué garde sa dernière valeur jusqu'à `last_date` (son statut est `error`).
- `mcap` existe pour chaque devise de `currencies` (les devises de référence des tokens du projet).
- `holders.by_chain` et `holders.total` sont dédoublonnés par chaîne ; `holders.by_token` est la somme des lignes (SPEC §4.3).

## 9. `data/projects.json` — index de la page d'accueil

```json
{
  "schema_version": 1,
  "generated_at": "2026-11-23T06:06:00Z",
  "projects": [
    {
      "slug": "my-project",
      "name": "My Project",
      "image": "projects/my-project/img/project.webp",
      "status": "ok",
      "last_date": "2026-11-22",
      "default_currency": "EUR",
      "mcap": { "EUR": 1000000.0, "USD": 1150000.0 },
      "holders": 3,
      "chains": ["ethereum"],
      "tokens": ["EURX"]
    }
  ]
}
```

`status` : `pending` (rien d'initialisé), `ok`, `warning` ou `error` (le pire statut de ses lignes).

## 10. Rapport de run (non commité)

Chaque script de pipeline ajoute ses messages dans `.run/report.json` (ignoré par Git). `scripts/notify.py` le lit en
fin de workflow pour composer le message Discord.

```json
{
  "workflow": "init",
  "request_id": "…",
  "project_hint": "my-project",
  "started_at": "…",
  "entries": [{ "level": "error", "scope": "my-project/eurx_ethereum_1a2b3c4d", "message": "…" }],
  "initialized": [{ "project": "my-project", "line_id": "…", "first_date": "…", "last_date": "…" }]
}
```
