# MVP Backlog (user stories)

1. **Come POCT coordinator** voglio vedere risultati in quarantena con motivo, così posso risolvere i casi bloccati.
   - AC: filtro per motivo, audit azioni, doppia conferma su release.
2. **Come validatore** voglio approvare/rifiutare risultati pending review.
   - AC: commento obbligatorio su rifiuto/correzione.
3. **Come integration engineer** voglio retry automatico su ACK mancante.
   - AC: backoff, max tentativi, escalation.
4. **Come quality manager** voglio bloccare rilascio se QC fallito/scaduto.
   - AC: stato `quarantined`, notifica POCT coordinator. ✅ (blocco QC a livello pipeline con stato `QUARANTINED`)
