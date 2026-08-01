# Milestone 1 standalone package

This directory is the complete submission package for the deterministic
Architecture v0.1 Milestone 1 harness. It includes runtime code, dependency and
package metadata, tests, and these instructions.

Implemented scope:

- exact UTF-8 CSV loading and typed schema validation;
- primary/composite key and referential-integrity checks;
- normalized nullable joins;
- same-user, strictly-prior history filtering;
- bounded-header byte-signature media sniffing;
- a read-only diagnostic CLI and standard-library tests.

No routing, model/API call, embedding, confidence, cache, extraction, or UI
logic is included. The diagnostic command never writes `output.csv`.

## Timestamp policy

CSV timestamps are dataset-local naive wall-clock values. The runtime does not
infer a geographic timezone, attach an offset, or convert source timestamps.
All ordering and strictly-prior comparisons use the source wall-clock values
directly. Quiet-hour calculations in later milestones must use the same
dataset-local wall-clock convention.

## Install

Open a terminal in this `code/` directory. Python 3.11 or 3.12 is supported.

```text
python -m pip install -r requirements.txt
python -m pip install -e .
```

Milestone 1 has no third-party runtime dependencies. `requirements.txt`
installs only the package build tool; the editable installation exposes the
optional `notification-router` command.

## Run diagnostics

Pass the participant dataset directory explicitly. From this directory in the
starter repository:

```text
python main.py --dataset-dir ../dataset
```

Equivalent installed command:

```text
notification-router --dataset-dir ../dataset
```

Use `--json` for deterministic machine-readable output:

```text
python main.py --dataset-dir ../dataset --json
```

## Run tests

From this `code/` directory:

```text
python -m unittest discover -s tests -v
```

The integration tests expect the participant dataset at `../dataset`, matching
the challenge repository layout. Runtime operation accepts any dataset path
through `--dataset-dir`; the dataset itself is not part of the submitted code
package.
