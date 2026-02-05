import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox
import hashlib
import os
import json
import secrets
from PIL import Image, ImageTk
import io
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

VAULT_DATA = "vault.bin"
VAULT_INDEX = "vault.idx"
VAULT_SALT = "vault.salt"

# ======================================================
# Password → Key (MD5 on purpose, as requested)
# ======================================================
def load_or_create_salt():
    if os.path.exists(VAULT_SALT):
        with open(VAULT_SALT, "rb") as f:
            return f.read()
    salt = secrets.token_bytes(16)
    with open(VAULT_SALT, "wb") as f:
        f.write(salt)
    return salt

def derive_key(password, salt):
    # Intentional weak scheme:
    # MD5(password || salt) → 16 bytes → doubled to 32 bytes for ChaCha20 key
    h = hashlib.md5(password.encode("utf-8") + salt).digest()
    return h + h   # 32 bytes

# ======================================================
# Index helpers
# ======================================================
def load_index():
    if not os.path.exists(VAULT_INDEX):
        return {}
    with open(VAULT_INDEX, "r") as f:
        return json.load(f)

def save_index(index):
    with open(VAULT_INDEX, "w") as f:
        json.dump(index, f, indent=2)

# ======================================================
# Vault encryption routines
# ======================================================
def encrypt_and_append(filepath, key):
    with open(filepath, "rb") as f:
        data = f.read()

    nonce = secrets.token_bytes(12)
    cipher = ChaCha20Poly1305(key)
    encrypted = cipher.encrypt(nonce, data, None)

    offset = os.path.getsize(VAULT_DATA) if os.path.exists(VAULT_DATA) else 0

    with open(VAULT_DATA, "ab") as vault:
        vault.write(nonce + encrypted)

    return {
        "offset": offset,
        "nonce_len": 12,
        "ciphertext_len": len(encrypted),
        "filename": os.path.basename(filepath)
    }

def decrypt_entry(entry, key):
    with open(VAULT_DATA, "rb") as vault:
        vault.seek(entry["offset"])
        nonce = vault.read(entry["nonce_len"])
        ciphertext = vault.read(entry["ciphertext_len"])

    cipher = ChaCha20Poly1305(key)
    return cipher.decrypt(nonce, ciphertext, None)

# ======================================================
# Viewer window (in-memory)
# ======================================================
class Viewer(tk.Toplevel):
    def __init__(self, root, data, filename):
        super().__init__(root)
        self.title(f"Viewing: {filename}")
        self.geometry("600x500")

        # Try image
        if filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp")):
            try:
                img = Image.open(io.BytesIO(data))
                img.thumbnail((580, 480))
                photo = ImageTk.PhotoImage(img)
                lbl = tk.Label(self, image=photo)
                lbl.image = photo
                lbl.pack(padx=10, pady=10)
                return
            except:
                pass

        # Try text
        try:
            text = data.decode("utf-8")
            text_area = tk.Text(self, wrap="word")
            text_area.insert("1.0", text)
            text_area.pack(expand=True, fill="both")
            return
        except:
            pass

        # Hex fallback
        hex_area = tk.Text(self, wrap="word")
        hex_area.insert("1.0", data[:2048].hex() + "\n\n(First 2KB shown as hex)")
        hex_area.pack(expand=True, fill="both")

# ======================================================
# Main app
# ======================================================
class BlobApp:
    def __init__(self, root, key):
        self.root = root
        self.root.title("Blob Assimilator")

        self.key = key
        self.index = load_index()

        frame = tk.Frame(root)
        frame.pack(padx=20, pady=20)

        self.btn = tk.Button(frame, text="Assimilate File Into Blob",
                             width=30, command=self.assimilate)
        self.btn.pack(pady=10)

        self.listbox = tk.Listbox(frame, width=60, height=12)
        self.listbox.pack()

        self.listbox.bind("<Double-1>", self.on_double_click)

        self.refresh_list()

    def refresh_list(self):
        self.listbox.delete(0, tk.END)
        for key, entry in self.index.items():
            self.listbox.insert(
                tk.END, f"{key}: {entry['filename']} ({entry['ciphertext_len']} bytes encrypted)"
            )

    def assimilate(self):
        path = filedialog.askopenfilename(title="Select a file to assimilate")
        if not path:
            return

        entry_id = str(len(self.index))
        meta = encrypt_and_append(path, self.key)
        self.index[entry_id] = meta
        save_index(self.index)

        messagebox.showinfo("Assimilated", f"Absorbed: {meta['filename']}")
        self.refresh_list()

    def on_double_click(self, event):
        selection = self.listbox.curselection()
        if not selection:
            return

        entry_id = str(selection[0])
        entry = self.index[entry_id]

        try:
            plaintext = decrypt_entry(entry, self.key)
        except Exception as e:
            messagebox.showerror("Decryption failed",
                                 "Wrong password or corrupted vault!")
            return

        Viewer(self.root, plaintext, entry["filename"])


# ======================================================
# Application Entry Point
# ======================================================
if __name__ == "__main__":
    # Load/create salt
    salt = load_or_create_salt()

    # Ask for password
    root = tk.Tk()
    root.withdraw()  # Hide root until password entered

    pw = simpledialog.askstring(
        "Vault Password",
        "Enter vault password:\n(First time = creates new vault key)",
        show="*"
    )

    if pw is None:
        exit()

    key = derive_key(pw, salt)

    root.deiconify()
    app = BlobApp(root, key)
    root.mainloop()

