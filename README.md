# Product Copilot — Requirements Engine

Transforme une demande client floue en **modèle structuré de solution**, prêt à être implémenté.

## Ce que c'est (et ce que ce n'est pas)

Ce n'est pas un chat qui pose des questions. C'est un **moteur** qui construit progressivement une
représentation du système métier, jusqu'à ce qu'elle soit assez précise pour partir en dev.

```
Demande floue  →  [ Moteur ]  →  Modèle structuré  →  Artefacts (questions, résumé, PRD, stories…)
                       ↑
                   Contexte produit + clients
```

Le chat n'est que l'interface. Le produit, c'est le moteur et le modèle qu'il remplit.

## Le v0 (wedge)

Un seul saut, celui qui fait mal chaque semaine :

**Demande client floue → (1) liste de questions prioritaires + (2) résumé métier structuré.**

Les deux sorties ne sont que **deux rendus du même modèle** :
- le résumé = le modèle rendu en prose,
- les questions = les **trous** du modèle, rendus en interrogations.

## Le principe qui rend ça intelligent

Le moteur ne pose pas une question *parce qu'un slot est vide*. Il pose une question quand
**Incertitude × Impact** est élevé. Un slot vide mais sans enjeu (ex. Reporting sur un ajout de champ) →
on ne demande pas. Un slot rempli mais risqué (ex. une règle métier qui change selon le pays) → on creuse.

On pose **les bonnes** questions, pas toutes.

## Structure

| Dossier | Rôle |
|---|---|
| `framework/` | Le modèle : slots, piliers (Why/What/How/Validate), driver Incertitude×Impact. **Le cœur.** |
| `context/`   | Fiches de contexte produit + clients. Ce qui ancre la prioritisation dans le réel. |
| `prompts/`   | Le prompt moteur : requête + contexte + modèle → questions + résumé. |
| `src/`       | Runner Python fin. |
| `examples/`  | Cas de test réels. |

## Lancer

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # puis renseigner ANTHROPIC_API_KEY
python src/engine.py "Nous aimerions mettre en place un système de validation des congés."
# ou :
python src/engine.py examples/cas1_conges.md
```
