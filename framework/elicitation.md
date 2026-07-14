# Framework d'élicitation

Le cœur du produit. Product-agnostic : il encode *comment* un PM transforme une demande floue en
modèle de solution. Le contexte (`context/*.md`) l'ancre dans un produit précis.

## Les 4 piliers (navigation + pitch)

| Pilier | Question | Slots |
|---|---|---|
| 🟢 **Why**      | Pourquoi ?  | Problème réel · Processus actuel (as-is) · Critères de succès |
| 🟡 **What**     | Quoi ?      | Acteurs & rôles · Objets métier · Règles métier · Workflow/cycle de vie |
| 🔵 **How**      | Comment ?   | Intégrations & notifications · Permissions · Config vs custom · Contraintes |
| 🔴 **Validate** | Validation  | Cas limites · Reporting · Critères d'acceptation · Risques & rollout |

Les piliers sont des **buckets de navigation**. L'unité atomique reste le **slot** — c'est lui qui se
remplit, se track, et depuis lequel les artefacts se génèrent.

## Chaque slot est un objet

```
slot {
  completeness : 0-100        # à quel point on le connaît
  confidence   : explicit | inferred | empty
  impact       : low | medium | high    # combien il change la forme/le coût de la solution
  value        : ce qu'on sait
  evidence     : d'où ça vient (mots du client / inférence / réponse)
}
```

`confidence` porte la **provenance** : un slot `inferred` est une **hypothèse à confirmer** — c'est
exactement ce qui alimente la section *"Hypothèses posées"* du résumé.

## Le driver : Incertitude × Impact

Le moteur ne pose **pas** une question parce qu'un slot est vide. Il calcule la **valeur de
l'information** :

```
information_value = incertitude × impact
```

- **Incertitude** ← dérivée de `completeness` + `confidence`.
- **Impact** ← combien ce slot change la solution. **Estimé grâce au contexte produit.** Sans contexte,
  l'impact est une devinette : le moteur n'est aussi intelligent que le contexte qu'on lui donne.

On pose **les bonnes** questions, pas toutes.

- Slot vide, impact bas (ex. Reporting sur un ajout de champ) → **on ne demande pas.**
- Slot partiel, impact haut, confiance moyenne (ex. une règle métier qui varie selon le pays) → **on creuse.**

## Le config-vs-custom : l'edge des plateformes

Slot `optional` : ON pour les produits configurables (plateformes multi-clients), OFF pour une app
one-shot. La quasi-totalité des outils de discovery sont pensés greenfield et ne posent jamais
*"hardcodé / configurable / spécifique client / réutilisable pour tous ?"*. C'est LA question qui
sépare une plateforme scalable d'un plat de forks clients.

## Ce que le moteur produit (v0)

Deux rendus du même modèle :
1. **Résumé métier** — objectif · périmètre pressenti · hypothèses posées · angle mort principal.
2. **Questions prioritaires** — triées par valeur d'information, avec le *pourquoi* de chaque question.
