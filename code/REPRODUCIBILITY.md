# Reproducibility and submission commands

All commands below run from this `code/` directory. Python 3.11 or 3.12 is
required.

## Install and offline verification

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The offline suite requires no credentials and makes no network calls.

## Local configuration

Copy `.env.example` to the repository-root `.env`, then fill the Vertex
project, location, model names, and ADC settings locally. Keep `.env`, ADC or
service-account files, raw artifacts, caches, and logs outside commits.

The final run used explicit Vertex ADC, backend `vertex`, and model
`gemini-2.5-flash`. There is no automatic backend fallback.

## Development and holdout boundaries

Development uses only sanitized `sample_messages.csv` inputs and context
tables:

```powershell
python -m notification_router.baseline --dataset-dir ../dataset --env-file ../.env --artifact-dir ../.artifacts/milestone4a-dev --cache-dir ../.artifacts/milestone4a-dev/cache --run-id 20260802T-development --max-cost-usd 1.00 --json
```

The sealed holdout requires an explicit reveal flag and must be executed only
once for an evaluation run:

```powershell
python -m notification_router.baseline --dataset-dir ../dataset --env-file ../.env --artifact-dir ../.artifacts/milestone4a-holdout --cache-dir ../.artifacts/milestone4a-holdout/cache --run-id 20260802T-holdout-once --partition holdout --reveal-holdout --max-cost-usd 1.00 --json
```

Do not rerun that command for the frozen evaluation record.

## Target output

After the development configuration is frozen and the holdout has been
recorded, run the target command once with a new artifact namespace:

```powershell
python -m notification_router.target --dataset-dir ../dataset --output ../dataset/output.csv --env-file ../.env --artifact-dir ../.artifacts/target-final --cache-dir ../.artifacts/milestone4a-operational-retry-20260802-123621652/cache --run-id 20260802T-target-final --max-cost-usd 1.00 --json
```

This command is the only prediction entry point that opens
`dataset/messages.csv`. It validates exact row count and ID set, output
columns and enums, confidence range, evidence allowlists, and atomic writing.
It never reads the sealed holdout or evaluator labels.

## Packaging

`code.zip` is a clean archive of the contents of `code/` only. It excludes
Python bytecode, editable-install metadata, `.env`, caches, logs, raw
artifacts, and media. `dataset/output.csv` is the separate submission output.
