# Security policy

## Supported use

LAIka is supported **only on a private network or behind a VPN**. Exposing
it to the internet is unsupported; see [docs/security.md](docs/security.md).

## Reporting a vulnerability

Please report security problems **privately** through the repository's
"Report a vulnerability" (GitHub security advisories) rather than in a
public issue. Include the version (`laika version`), what you did, what
happened, and `sudo laika doctor --json` if relevant. Never include real
keys, tokens or passwords.

## Supported versions

Security fixes go into the latest release. Update with `sudo laika update`
or Settings → System.
