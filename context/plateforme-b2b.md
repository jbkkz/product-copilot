# Fiche de contexte — Plateforme B2B (exemple)

## Qui
- Domaine métier : gestion d'entreprise (RH, projets, facturation, prestataires)
- Type de produit : **plateforme configurable multi-clients** → slot `config_vs_custom` ACTIF
- Utilisateurs / rôles types : collaborateur, manager, RH, administrateur, client externe

## Le produit / module
- Ce qu'il fait : centralise congés, contrats, missions freelance, facturation et suivi projet
- Objets métier principaux : Collaborateur, Congé/Absence, Contrat, Facture, Mission, Freelance, Client, Contact
- Concepts clés : circuits de validation, soldes/quotas, cycles de vie à états, notifications par rôle

## L'existant (surface déjà construite)
- Fonctionnalités majeures : gestion des absences, contrats, facturation, annuaire clients/contacts
- Modules / zones sensibles : les circuits de validation et les règles de permission sont partagés
  entre modules — un changement de règle touche souvent plusieurs écrans

## Sensibilités & contraintes
- Réglementaire : droit du travail français (congés payés, RTT), RGPD sur les données RH
- Pièges récurrents :
  - "validation" cache presque toujours une **vérification de solde/quota** + un **circuit multi-niveaux**
  - les permissions sont oubliées jusqu'à la recette
  - "notifier" implique de préciser **qui**, **quand**, et **par quel canal**
  - une règle métier peut varier selon le **client**, le **contrat** ou le **pays**

## Configurabilité
- Standard pour tous : le socle des entités et des cycles de vie
- Spécifique par client : circuits de validation, règles métier, libellés, droits
