# PRODUCT REQUIREMENTS — POCT Middleware

## Executive summary
Questo progetto evolve il middleware HL7 esistente in una piattaforma POCT enterprise: non solo trasporto risultati, ma governance clinica, qualità, sicurezza, auditabilità e interoperabilità HL7/FHIR.

## Problem statement
Il POCT decentralizzato riduce il TAT ma aumenta rischio di errori pre/analitici/post-analitici se non governato centralmente dal laboratorio.

## Product goals
1. Connettività multivendor (HL7, POCT1-A2, ASTM, API vendor).
2. Modello canonico con conservazione del raw message.
3. Workflow risultati con stati espliciti e regole spiegabili.
4. Quality management (QC/EQA), operator competency, device fleet.
5. Tracciabilità end-to-end device→middleware→LIS con ACK.

## Users and personas
POCT Coordinator, Direzione laboratorio, validatori, tecnici, QM/RM, IT, biomedici, operatori reparto, auditor, DPO.

## MVP scope (vertical slice)
- Device simulator invia ORU (quantitativo/qualitativo).
- ADT simulator crea paziente/encounter.
- Risultato normalizzato + controlli minimi: operatore/device/lotto/QC.
- Rule engine decide `auto_validated` / `pending_review` / `quarantined`.
- Validazione manuale da UI.
- Invio a LIS simulator con retry su ACK mancante.
- Audit trail e dashboard KPI base.

## Non-goals (MVP)
- Diagnostica assistita AI.
- Interpretazione clinica automatica.
- Copertura completa di tutti i protocolli proprietari.

## Acceptance criteria (MVP)
- Risultato tracciabile con raw payload conservato.
- Stato risultato esplicito e transizioni auditate.
- Gestione casi: QC fail, operatore non autorizzato, mismatch paziente, duplicato, ACK mancante.
