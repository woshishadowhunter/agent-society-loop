# Security Policy

## Supported versions

Security fixes are applied to the latest release on `main`.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities, leaked credentials, or prompt/data exposure. Use GitHub's private vulnerability reporting feature on this repository. Include affected versions, reproduction steps, impact, and any proposed mitigation.

You should receive an acknowledgement within seven days. We will validate the report, coordinate a fix and disclosure timeline, and credit reporters who want attribution.

## Operational guidance

- Keep model credentials in environment variables or a secret manager.
- Treat model responses, memory entries, and JSON goal specs as untrusted input.
- Review tool permissions before connecting workers to external systems.
- Use bounded attempts and actions for every production run.
- Do not store personal or confidential data in demonstration databases.
- Protect the SQLite database as an operator trust boundary; policy, deployment, attestation, and decision records are not externally signed.
- Run the official A2A TCK outside the runtime at a reviewed pinned revision, preserve its artifact, and import only the bounded compatibility JSON.
- Treat TCK source revision and tool version fields as operator provenance, not cryptographic proof.
- Run `agent-society a2a doctor` before enabling remote production traffic and `agent-society a2a self-test` after transport changes.

