# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Use GitHub's private reporting on the repository's **Security** tab (**Report a vulnerability**), and include steps to reproduce.

Never include real credentials in a report.

## How credentials are handled

- Users create and own all API apps and credentials. The project ships none.
- Account passwords are never requested. YouTube uses Google's OAuth flow, and the desktop app uses Twitch's Device Code flow.
- Secrets come from environment variables, a git-ignored `.env`, or the OS keychain.
- OAuth tokens are stored in the OS keychain.
- Error messages are written to avoid exposing webhook URLs, tokens and client secrets.
- CI scans every push for common credential patterns.

## If you exposed a credential

Revoke it immediately. Removing it from a later commit doesn't remove it from history. See [the credentials guide](docs/credentials.md#if-a-credential-leaks).
