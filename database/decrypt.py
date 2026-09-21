"""Decrypt a LeadHunter database snapshot without writing the password."""

from __future__ import annotations

import argparse
import getpass
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, default=Path("database/restored"))
    args = parser.parse_args()
    raw = args.archive.read_bytes()
    if raw[:6] != b"LHPDB1":
        raise SystemExit("Unsupported database archive format")
    label_length = raw[6]
    label = raw[7:7 + label_length]
    offset = 7 + label_length
    salt, nonce, ciphertext = raw[offset:offset + 16], raw[offset + 16:offset + 28], raw[offset + 28:]
    password = getpass.getpass("Snapshot password: ").encode()
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password)
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, label)
    args.output.mkdir(parents=True, exist_ok=True)
    tar_path = args.output / f"{label.decode()}-server-databases.tar.gz"
    tar_path.write_bytes(plaintext)
    with tarfile.open(tar_path, "r:gz") as archive:
        archive.extractall(args.output, filter="data")
    print(f"Restored and verified under {args.output}")


if __name__ == "__main__":
    main()
