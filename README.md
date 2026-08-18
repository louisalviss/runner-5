# Runner 5 — WordPress Restore Lab

Purpose: isolated execution repo for disposable WordPress staging sites used to restore, repair, optimize, test, and export WordPress backups.

## Security boundary

- Do not copy Runner-3 browser state, provider tokens, or unrelated infrastructure secrets into this repository.
- WordPress backups are treated as untrusted input.
- Provisioning uses only the minimum Wasmer credentials required by this repo.
- Theme/content/R2/PageSpeed work is not part of Site Factory provisioning.

## Current staging target

`runner5-restore-lab-1`
