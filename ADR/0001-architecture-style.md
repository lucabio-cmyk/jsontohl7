# ADR-0001: Modular monolith con adapter layer

## Decision
Adottiamo modular monolith Python con separazione netta adapter/core e modello canonico.

## Consequences
- Più semplice validazione e deploy MVP.
- Evoluzione a microservizi per bounded context ad alto carico.
