# Fixture provenance

- `official-doc-example-*.json`: JSON blocks extracted from `sources/api.md`, preserving the example values. Documentation examples; not live captures.
- `recorded-historical-typesafe-request.json` and `recorded-historical-typesafe-response.json`: body objects extracted from TypeSafe's own committed live-test cassette at adapter commit `e1d4cc938204b22fc5a3c3aca7044072fe3f712d`. Historical `speed_latest` / `speed_v12_snowy_flower`, not current Jev-1.13. Capture date is not embedded. Complete original HTTP record is retained under `sources/system-one-adapter-python/tests/cassettes/`.

No synthesized fixture is represented as a live API response. No paid API inference was run for this investigation.
