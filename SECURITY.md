# Security

## Reporting

Please report security issues privately to the repository owner rather than opening
a public issue. Include the affected version, reproduction steps, impact, and any
suggested mitigation. Do not include real credentials or owner data.

## Trust boundaries

Lilith treats model output, retained memories, research pages, downloaded files,
and generated tool source as untrusted data. Authority comes from runtime code and
explicit owner actions. Model text cannot grant filesystem, process, network,
desktop, Git, or tool permissions.

Public-web research rejects private and reserved destinations, revalidates redirects,
pins resolved addresses, limits bytes and requests, and quarantines downloads.
Generated tools run only through the bounded JSON/AST interpreter and must pass hash,
schema, test, review, and canary checks.

The local capability broker is a containment and audit boundary, not a hostile-code
sandbox. Owner-authorized terminal and application commands execute with the owner's
OS permissions. Keep the workspace narrow and do not expose secrets through command
arguments, environment variables, logs, or audit records.

## Supported baseline

Security fixes target the current main branch and Python 3.12. Offline tests must
never use live owner state, credentials, real desktop input, or unrestricted network
acceptance tests.
