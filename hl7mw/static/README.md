# Asset vendorizzati

`chart.umd.min.js` — [Chart.js](https://www.chartjs.org/) v4.4.0, build UMD
minificata, scaricata da `https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js`.
Licenza MIT (© 2014-2024 Chart.js Contributors).

Vendorizzato (non caricato da CDN a runtime) perché la dashboard di
`hl7mw/api.py` non deve dipendere dalla raggiungibilità di internet: reti
cliniche/di laboratorio spesso bloccano l'accesso a domini esterni per
policy, e questo middleware deve restare utilizzabile anche in quel caso
(vedi CLAUDE.md — "è il pezzo che resta in piedi quando il resto non
funziona"). Servito localmente da `GET /static/chart.min.js`.

Per aggiornare la versione: scaricare il nuovo build UMD minificato da
jsdelivr/npm e sovrascrivere questo file, aggiornando il numero di versione
qui sopra.
