# Encrypted server database snapshots

These snapshots were created on 2026-09-21 with SQLite's online backup API,
then integrity checked and encrypted before being added to Git. The repository
is public, so the password is deliberately not stored here.

- `test-server-databases-2026-09-21.tar.gz.enc`: 11 current databases from the test VPS.
- `main-server-databases-2026-09-21.tar.gz.enc`: 4 current databases from the main VPS.
- `decrypt.py`: prompts for the snapshot password, authenticates/decrypts the archive,
  validates the tar file, and extracts its databases plus `manifest.json`.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe database\decrypt.py database\test-server-databases-2026-09-21.tar.gz.enc
```

Each encrypted archive contains a manifest with every database filename,
byte size, SHA-256 checksum, SQLite integrity result, and table list.

Encrypted archive SHA-256 checksums:

- test: `e59e96d17c78d0ec3a60c16c66a44f168e98c3022e7b557ca0687024a506499b`
- main: `ed0e6e5eedc862c6046cb68baa42e0138bcb4d8ec525594c00d790d1f0d6682c`
