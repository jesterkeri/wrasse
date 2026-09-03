# Rapport

Rapport is a hackathon prototype in which agents remember how a counterparty
behaved, use that evidence to produce the terms of the next negotiation, execute
accepted terms on Base, and write the verified outcome back to Sibyl Memory.

> Memory changes real economic behaviour across sessions, and the outcome comes
> back as verified evidence.

The implementation is in progress during the Sibyl Labs Hackathon build window.

## Development wallets

Rapport uses dedicated encrypted Foundry keystores. Their public addresses are
listed in `.env.example`; the keystores and their generated password file live
under the gitignored `.rapport/` directory. No private key belongs in `.env`.

## Prior Work

All code in this repository was written between 2026-09-03 and 2026-09-10,
inside the hackathon build window. No prior work is reused.

## License

MIT
