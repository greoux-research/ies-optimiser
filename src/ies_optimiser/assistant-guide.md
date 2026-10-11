# IES Optimiser modelling guide

This guide is shipped with the package. Run `ies-optimiser guide` to read the
copy that belongs to the installed version. IES Optimiser sizes and dispatches
an integrated energy system over hourly intervals using a continuous linear
model. It is a screening model; a result is not a construction plan, tariff,
market bid or certificate of compliance.

## Prepare a study

Work in a new directory containing a canonical `case.json` (`format_version: 1`)
and any hourly profile files it names. Start from a synthetic example in the
repository or inspect `ies-optimiser schema input`. The schema checks structure;
`ies-optimiser validate case.json --json` also checks references, topology,
profile contents, hourly bounds and thermodynamics. A valid case may still be
infeasible. `ies-optimiser schema options` describes run options, supplied as
`name=value` on the command line or as a mapping to the Python API.

Do not invent demand, weather, costs, emission factors, capacity bounds or
technology parameters for a real study. Record their source and units. Profiles
give shape and are scaled to the declared total or capacity factor. A relative
profile path resolves against the case file's directory, or the explicitly
chosen `--profile-base`; it never resolves against the working directory.
CSV profiles and hourly floor files contain one numeric value per line, no
header. The default horizon is 8,760 hours. Preserve the source files and their
calendar alignment. The program does not convert time zones or repair a leap
year or daylight-saving series for you.

`hourly-coverage-floor=0.9` requires at least 90% of each hour's final
electricity demand to be served. It accepts a scalar, a CSV path or an hourly
list. It is a run option, not a case field. The annual
`non-served-power-constraint` may impose a stricter aggregate requirement.
Neither requirement is a promise that the optimisation is feasible. See the
input and options schemas for accepted values.

## Validate, run and keep evidence

```text
ies-optimiser guide
ies-optimiser schema input
ies-optimiser schema options
ies-optimiser validate STUDY/case.json --json
ies-optimiser run STUDY/case.json --output NEW_DIRECTORY --json
ies-optimiser verify NEW_DIRECTORY --json
```

`validate` solves nothing and writes nothing. Read diagnostic `code`, `layer`,
`path`, `field` and zero-based `hour`; messages explain the problem but codes
are the stable identifiers. Correct an input only with evidence for the
correction. Do not lower a target, add a generator, change data, retry with
different assumptions or loosen tolerances merely to obtain a result. Keep
each user-requested variant as a separate study.

`run` solves once and writes a new, portable bundle. It refuses an existing
destination. The bundle contains the original case, a replay case with local
profile references, exact profile bytes, effective options, configuration,
native result and a file-hash manifest. `verify` checks the retained bytes and
their relationship to the manifest and result. It does not rerun the solver,
certify source data or prove scientific correctness. The original case and
native result can contain local paths and environment information; review them
before sharing the bundle. Do not edit source files during a run: bundle
creation checks their bytes before and after solving but does not lock them.
For Python, use `ies_optimiser.save_bundle` and
`ies_optimiser.read_bundle`.

The original `ies-optimiser case.json [name=value ...]` command remains
available. It writes one result JSON beside the input, replacing a file of the
same name. Prefer `run` when retaining and handing over evidence matters.

## Interpret a result

Read the run outcome before numbers. A non-optimal solve has status and
provenance but no numerical result. For an optimal solve, check
`system.accounting_ok` and every applicable entry in `system.checks`. Report
power in MW, energy in MWh, costs in USD and emissions in kg CO2eq, with the
time horizon and data basis. `kpis.cost` allocates whole-system cost by
electricity-equivalent use; it is not a product production cost or a price.
`surplus` is a balance residual, not renewable curtailment. Demand duals are
marginal values, not prices paid. Several dispatches can have the same optimum;
do not claim a particular hourly dispatch or capacity split is unique.

The bundle verifier establishes consistency of saved evidence, not agreement
with an independent physical model. Re-solving may give a different equally
optimal dispatch under another solver build. Compare objectives, capacities,
balances and accounts before comparing hourly series.
