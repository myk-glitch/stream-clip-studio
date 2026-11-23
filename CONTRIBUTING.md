# Contributing

Thanks for helping improve Stream Clip Studio. Bug reports, fixes, documentation and small focused features are all welcome.

## Set up

Follow the install steps in the [README](README.md). Then:

```powershell
cd pipeline
```

```powershell
python -m pip install -r requirements.txt pytest
```

```powershell
python -m pytest -q
```

For the desktop app:

```powershell
cd desktop
```

```powershell
uv sync --frozen
```

```powershell
uv run --frozen pytest -q
```

The pipeline tests don't use your real settings, credentials or network. The desktop tests use real FFmpeg with generated media.

## Before opening a pull request

1. Keep the change focused, and open an issue first for large changes.
2. Add or update tests for behavior you change.
3. Update the docs and [CHANGELOG.md](CHANGELOG.md).
4. Run the privacy scan from the repository root:

   ```powershell
   python scripts/check_secrets.py
   ```

## Project rules

- **Never commit** credentials, `.env`, `config.toml`, OAuth files, tokens, generated videos, logs or personal data.
- **Everything user-specific must be configurable.** Add settings to `settings.py`, `config.example.toml` and `docs/configuration.md`.
- **Prefer official APIs.** Anything unofficial must be off by default, gated under `[unofficial]`, and documented with its risks.
- **Credentials must not reach logs or error messages.** Use authorization flows, and never ask users for account passwords.
- **Don't add media** unless its license clearly allows redistribution.
- **Default to safe behavior:** private uploads and no automatic posting unless the user opts in.

## Versioning

The project follows [Semantic Versioning](https://semver.org/). Releases are Git tags such as `v0.1.0`, with notes in [CHANGELOG.md](CHANGELOG.md).

- **MAJOR:** breaking changes to commands or settings
- **MINOR:** new backward-compatible features
- **PATCH:** bug fixes

## Code of conduct

Be respectful and constructive. Harassment isn't tolerated.
