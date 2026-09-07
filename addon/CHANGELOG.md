# Changelog

## 0.2.1

- Fixed Core/journal log reads rejected by Supervisor with HTTP 400: the generic HTTP client accepts all response media types instead of requiring JSON.
- Kept the five-tool contract and existing JSON/text/binary decoding, authentication, redaction and output limits.
- No Core restart or integration configuration change is required; update the companion app.

## 0.2.0

- Original integration and companion brand icons, including local HA brand assets.
- Public HACS integration and Home Assistant app repository.
- Prebuilt amd64 GHCR image and release automation with locked dependencies.
- Automatic discovery supports the repository companion and migration from the local app.
- Removed site-specific addresses and routing from the distributed source and skill.
- Preserved the five-tool contract, native LLM API, internal Supervisor authorization and automatic bridge identity.

## 0.1.1

- Internal Core/Influx networking and automatic Supervisor-mediated bridge pairing.
- Removed manually configured HA and bridge tokens.

## 0.1.0

- Initial generic MCP engine, native HA integration, skill and live acceptance checks.
