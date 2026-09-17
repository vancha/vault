"""Encrypted append-only blob storage: add files, list them, fetch/decrypt
them, delete them. No knowledge of Tkinter or any other UI concern lives here.
"""

import hashlib
import json
import os
import secrets
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

VAULT_DATA = "vault.bin"
VAULT_INDEX = "vault.idx"
VAULT_SALT = "vault.salt"


class Vault:
    def __init__(self, key, data_path=VAULT_DATA, index_path=VAULT_INDEX):
        self.key = key
        self.data_path = data_path
        self.index_path = index_path
        self.index = self._load_index()

    @staticmethod
    def load_or_create_salt(salt_path=VAULT_SALT):
        if os.path.exists(salt_path):
            with open(salt_path, "rb") as f:
                return f.read()
        salt = secrets.token_bytes(16)
        with open(salt_path, "wb") as f:
            f.write(salt)
        return salt

    @staticmethod
    def derive_key(password, salt):
        # Intentional weak scheme:
        # MD5(password || salt) → 16 bytes → doubled to 32 bytes for ChaCha20 key
        h = hashlib.md5(password.encode("utf-8") + salt).digest()
        return h + h  # 32 bytes

    def _load_index(self):
        if not os.path.exists(self.index_path):
            return {}
        with open(self.index_path, "r") as f:
            return json.load(f)

    def _save_index(self):
        with open(self.index_path, "w") as f:
            json.dump(self.index, f, indent=2)

    def add(self, filepath):
        with open(filepath, "rb") as f:
            data = f.read()

        nonce = secrets.token_bytes(12)
        cipher = ChaCha20Poly1305(self.key)
        encrypted = cipher.encrypt(nonce, data, None)

        offset = os.path.getsize(self.data_path) if os.path.exists(self.data_path) else 0
        with open(self.data_path, "ab") as vault:
            vault.write(nonce + encrypted)

        # max(existing) + 1, not len(): len() would reuse an id after a delete
        # leaves a gap, silently overwriting whatever entry still has that id.
        entry_id = str(max((int(k) for k in self.index), default=-1) + 1)
        self.index[entry_id] = {
            "offset": offset,
            "nonce_len": 12,
            "ciphertext_len": len(encrypted),
            "filename": os.path.basename(filepath),
        }
        self._save_index()
        return entry_id, self.index[entry_id]

    def get(self, entry_id):
        entry = self.index[entry_id]
        with open(self.data_path, "rb") as vault:
            vault.seek(entry["offset"])
            nonce = vault.read(entry["nonce_len"])
            ciphertext = vault.read(entry["ciphertext_len"])

        cipher = ChaCha20Poly1305(self.key)
        return cipher.decrypt(nonce, ciphertext, None)

    def delete(self, entry_id):
        del self.index[entry_id]
        self._compact()
        self._save_index()

    def _compact(self):
        """Rewrite vault.bin to contain only the ciphertext still referenced by
        self.index, reassigning offsets as it goes. Deleted entries' bytes are
        dropped entirely rather than merely unreferenced."""
        tmp_path = self.data_path + ".tmp"
        with open(self.data_path, "rb") as old_vault, open(tmp_path, "wb") as new_vault:
            for entry in self.index.values():
                old_vault.seek(entry["offset"])
                blob = old_vault.read(entry["nonce_len"] + entry["ciphertext_len"])
                entry["offset"] = new_vault.tell()
                new_vault.write(blob)
        os.replace(tmp_path, self.data_path)
