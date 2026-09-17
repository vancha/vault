import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox
import threading
import queue
import time
from PIL import Image, ImageTk
import io

from vault import Vault

try:
    import av
except ImportError:
    av = None

try:
    import sounddevice as sd
except (ImportError, OSError):
    # OSError: PortAudio native library missing. Video still plays, just muted.
    sd = None

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")

# ======================================================
# Embedded video/audio player (decodes straight from bytes
# in memory, nothing is ever written to disk)
# ======================================================
class VideoPlayer(tk.Frame):
    """Decodes and plays video+audio straight from in-memory bytes. No temp files."""

    MAX_DIM = 960  # cap decoded frame size so large videos don't balloon memory

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        # Pack the controls bar first, anchored to the bottom, so it always
        # keeps its natural size -- the video area (packed after) fills
        # whatever space is left and is the one that shrinks if space is tight.
        controls = tk.Frame(self)
        controls.pack(side="bottom", fill="x")
        # takefocus=0: a focused Button intercepts <space> itself (activates
        # whichever button has focus) -- these shortcuts should always go to
        # the key bindings below instead, regardless of what was last clicked.
        tk.Button(controls, text="-10s", command=lambda: self.seek(-10), takefocus=0).pack(side="left", padx=4, pady=4)
        self.play_btn = tk.Button(controls, text="Pause", command=self.toggle_pause, takefocus=0)
        self.play_btn.pack(side="left", padx=4, pady=4)
        tk.Button(controls, text="+10s", command=lambda: self.seek(10), takefocus=0).pack(side="left", padx=4, pady=4)

        self.video_label = tk.Label(self, bg="black")
        self.video_label.pack(side="top", expand=True, fill="both")

        # Bind at the window level so the shortcuts work no matter what has
        # focus, and use add="+" so we don't clobber any other bindings.
        root_widget = self.winfo_toplevel()
        self._key_bindings = [
            (seq, root_widget.bind(seq, handler, add="+"))
            for seq, handler in (
                ("<Left>", lambda e: self.seek(-10)),
                ("<Right>", lambda e: self.seek(10)),
                ("<space>", lambda e: self.toggle_pause()),
            )
        ]
        self._root_widget = root_widget

        self._stop = threading.Event()
        self._closed = False  # widget destroyed for good; distinct from the transient _stop used during seeks
        self._seek_target = None
        self._decode_thread = None
        self._queue = queue.Queue(maxsize=64)
        self._pending = None
        self._audio_buf = bytearray()
        self._audio_lock = threading.Lock()
        self._container = None
        self._vstream = None
        self._astream = None
        self._resampler = None
        self._target_size = None
        self._out = None
        self._photo = None
        self._paused = False
        self._base_time = 0.0   # video-time position when playback last (re)started
        self._epoch = time.monotonic()  # wall-clock moment _base_time was set
        self._last_shown_pts = 0.0  # timestamp of the frame actually on screen right now
        self.bind("<Destroy>", lambda e: self.stop())

    def load(self, data):
        self._container = av.open(io.BytesIO(data))
        self._vstream = next(iter(self._container.streams.video), None)
        self._astream = next(iter(self._container.streams.audio), None)

        if self._vstream is not None and self._vstream.width > self.MAX_DIM:
            scale = self.MAX_DIM / self._vstream.width
            self._target_size = (self.MAX_DIM, max(int(self._vstream.height * scale), 1))

        if self._astream is not None and sd is not None:
            self._out = sd.RawOutputStream(samplerate=44100, channels=2,
                                            dtype="int16", callback=self._audio_cb)
            self._out.start()

        self._base_time = 0.0
        self._epoch = time.monotonic()
        self._spawn_decode_thread()
        self._tick()

    def toggle_pause(self):
        if self._container is None:
            return
        if self._paused:
            self._epoch = time.monotonic()
            self._paused = False
            self.play_btn.config(text="Pause")
        else:
            # Freeze at the frame actually on screen, not the idealized wall-clock
            # position -- decode/display can lag behind, leaving a backlog of
            # already-"due" frames that would otherwise keep flushing through
            # after pause.
            self._base_time = self._last_shown_pts
            self._paused = True
            self.play_btn.config(text="Play")

    def seek(self, delta_seconds):
        # Runs the blocking parts (thread join, container seek) off the main
        # thread so the button doesn't sit "pressed" while the UI is stalled.
        if self._container is None:
            return
        current = self._base_time if self._paused else self._last_shown_pts
        target = max(current + delta_seconds, 0.0)

        # Clear synchronously, right now, on the main thread: otherwise _tick()
        # keeps showing leftover pre-seek frames for the brief moment before
        # the background thread below gets around to it.
        self._pending = None
        self._queue = queue.Queue(maxsize=64)

        threading.Thread(target=self._do_seek, args=(target,), daemon=True).start()

    def _do_seek(self, target):
        try:
            # stop the current decode thread before touching the (non-thread-safe) container
            self._stop.set()
            if self._decode_thread is not None:
                self._decode_thread.join(timeout=1)

            with self._audio_lock:
                self._audio_buf.clear()

            self._container.seek(int(target * 1_000_000))  # microseconds, AV_TIME_BASE

            # container.seek() can only land on a keyframe at or before `target`,
            # so decode has to walk forward from there. _seek_target tells _decode
            # to skip the expensive resize/convert/queue work for those throwaway
            # frames so nothing is displayed until the real target is reached.
            self._seek_target = target

            # _paused is left untouched: if we were paused, _video_time() stays frozen
            # at `target` and _tick simply displays the seeked-to frame and stops there.
            self._base_time = target
            self._epoch = time.monotonic()

            self._stop.clear()
            self._spawn_decode_thread()
        except Exception:
            pass

    def stop(self):
        self._closed = True
        self._stop.set()
        for seq, funcid in self._key_bindings:
            self._root_widget.unbind(seq, funcid)
        if self._decode_thread is not None:
            # Must fully stop before touching the container -- closing it while
            # the decode thread is still mid-call into libav (decoding/resizing
            # a frame) is a real crash risk, worse for slow-to-decode video.
            self._decode_thread.join(timeout=1)
        if self._out is not None:
            self._out.stop()
            self._out.close()
        if self._container is not None:
            self._container.close()

    def _spawn_decode_thread(self):
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100) \
            if self._astream is not None and sd is not None else None
        # Bind this thread permanently to the queue instance live right now: if
        # seek() swaps in a fresh self._queue while this (old) thread is still
        # winding down, it must keep writing to its own queue, never the new one.
        self._decode_thread = threading.Thread(target=self._decode, args=(self._queue,), daemon=True)
        self._decode_thread.start()

    def _video_time(self):
        if self._paused:
            return self._base_time
        return self._base_time + (time.monotonic() - self._epoch)

    def _decode(self, out_queue):
        streams = [s for s in (self._vstream, self._astream) if s is not None]
        try:
            for packet in self._container.demux(streams):
                if self._stop.is_set():
                    return
                for frame in packet.decode():
                    pts = float(frame.pts * frame.time_base) if frame.pts is not None else None
                    if self._seek_target is not None and pts is not None and pts < self._seek_target:
                        continue  # fast-forwarding to a seek target -- skip this throwaway frame entirely
                    if packet.stream.type == "video":
                        if self._seek_target is not None:
                            self._seek_target = None  # reached it -- resume normal display from here
                            if pts is not None:
                                # Land the clock exactly on this frame's timestamp rather than
                                # the requested target: frame pts are discrete, so the target
                                # itself usually falls between two frames and would never
                                # become "due" (frozen forever if paused).
                                self._base_time = pts
                                self._epoch = time.monotonic()
                        if self._target_size is not None:
                            frame = frame.reformat(width=self._target_size[0], height=self._target_size[1])
                        if not self._queue_put(out_queue, (pts, frame.to_image())):
                            return  # _stop was set while blocked waiting for queue space
                    elif self._resampler is not None:
                        for resampled in self._resampler.resample(frame):
                            with self._audio_lock:
                                self._audio_buf.extend(bytes(resampled.planes[0]))
        except Exception:
            pass

    def _queue_put(self, out_queue, item):
        # A plain blocking put() could deadlock stop()/seek()'s join() if the
        # queue is full and nothing will ever drain it again (e.g. paused).
        # Retry with a timeout so we keep noticing _stop.
        while not self._stop.is_set():
            try:
                out_queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _audio_cb(self, outdata, frames, time_info, status):
        need = frames * 4  # stereo int16 = 4 bytes/frame
        if self._paused:
            outdata[:] = b"\x00" * need
            return
        with self._audio_lock:
            chunk = bytes(self._audio_buf[:need])
            del self._audio_buf[:need]
        outdata[:] = chunk.ljust(need, b"\x00")

    def _show(self, pts, img):
        if pts is not None:
            self._last_shown_pts = pts
        img.thumbnail((self.video_label.winfo_width() or 640, self.video_label.winfo_height() or 480))
        self._photo = ImageTk.PhotoImage(img)
        self.video_label.configure(image=self._photo)

    def _tick(self):
        if self._closed:
            return
        # Always evaluated, paused or not: _video_time() is frozen while paused,
        # so this naturally displays up to the current frame and then stalls.
        now = self._video_time()
        if self._pending is None:
            try:
                self._pending = self._queue.get_nowait()
            except queue.Empty:
                self._pending = None

        while self._pending is not None:
            pts, img = self._pending
            if pts is not None and pts > now:
                break  # not due yet
            try:
                newer = self._queue.get_nowait()
            except queue.Empty:
                self._show(pts, img)
                self._pending = None
                break
            if newer[0] is None or newer[0] <= now:
                self._pending = newer  # a newer frame is due too -- this one's stale, drop it
                continue
            self._show(pts, img)
            self._pending = newer
            break

        self.after(15, self._tick)


# ======================================================
# Viewer (in-memory), swapped into the main window
# ======================================================
class Viewer(tk.Frame):
    def __init__(self, master, data, filename, on_back):
        super().__init__(master)

        header = tk.Frame(self)
        header.pack(fill="x")
        tk.Button(header, text="< Back", command=on_back).pack(side="left", padx=6, pady=6)
        tk.Label(header, text=filename).pack(side="left", padx=6)

        content = tk.Frame(self)
        content.pack(expand=True, fill="both")

        # Try video
        if filename.lower().endswith(VIDEO_EXTENSIONS) and av is not None:
            player = VideoPlayer(content)
            player.pack(expand=True, fill="both")
            player.load(data)
            return

        # Try image
        if filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp")):
            try:
                img = Image.open(io.BytesIO(data))
                lbl = tk.Label(content, bg="black")
                lbl.pack(expand=True, fill="both")

                # At this point in __init__ the caller (Viewer(...)) hasn't been
                # pack()'d into the window yet, so winfo_width/height would still
                # reflect an unmapped 1x1 widget. <Configure> fires once real
                # geometry is actually established (and again on later resizes).
                def on_configure(event):
                    if event.width <= 1 or event.height <= 1:
                        return
                    sized = img.copy()
                    sized.thumbnail((event.width, event.height))
                    photo = ImageTk.PhotoImage(sized)
                    lbl.configure(image=photo)
                    lbl.image = photo

                lbl.bind("<Configure>", on_configure)
                return
            except Exception:
                pass

        # Try text
        try:
            text = data.decode("utf-8")
            text_area = tk.Text(content, wrap="word")
            text_area.insert("1.0", text)
            text_area.pack(expand=True, fill="both")
            return
        except:
            pass

        # Hex fallback
        hex_area = tk.Text(content, wrap="word")
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
        self.viewer = None

        self.list_frame = tk.Frame(root)
        self.list_frame.pack(padx=20, pady=20, expand=True, fill="both")

        self.btn = tk.Button(self.list_frame, text="Assimilate File Into Blob",
                             width=30, command=self.assimilate)
        self.btn.pack(pady=10)

        self.delete_btn = tk.Button(self.list_frame, text="Delete Selected",
                                    width=30, command=self.delete_selected)
        self.delete_btn.pack(pady=5)

        self.listbox = tk.Listbox(self.list_frame, width=60, height=12)
        self.listbox.pack()

        self.listbox.bind("<Double-1>", self.on_double_click)

        self._row_to_id = []  # listbox row index -> entry id (ids have gaps once deletion is used)
        self.refresh_list()

    def refresh_list(self):
        self.listbox.delete(0, tk.END)
        self._row_to_id = list(self.index.keys())
        for entry_id in self._row_to_id:
            entry = self.index[entry_id]
            self.listbox.insert(
                tk.END, f"{entry_id}: {entry['filename']} ({entry['ciphertext_len']} bytes encrypted)"
            )

    def assimilate(self):
        paths = filedialog.askopenfilenames(title="Select file(s) to assimilate")
        if not paths:
            return

        names = []
        for path in paths:
            # max(existing) + 1, not len(): len() would reuse an id after a delete
            # leaves a gap, silently overwriting whatever entry still has that id.
            entry_id = str(max((int(k) for k in self.index), default=-1) + 1)
            meta = encrypt_and_append(path, self.key)
            self.index[entry_id] = meta
            names.append(meta["filename"])
        save_index(self.index)

        messagebox.showinfo("Assimilated", f"Absorbed {len(names)} file(s): {', '.join(names)}")
        self.refresh_list()

    def delete_selected(self):
        selection = self.listbox.curselection()
        if not selection:
            return

        entry_id = self._row_to_id[selection[0]]
        filename = self.index[entry_id]["filename"]
        if not messagebox.askyesno("Delete file", f"Permanently delete '{filename}' from the vault?"):
            return

        del self.index[entry_id]
        compact_vault(self.index)  # physically drop the deleted ciphertext, not just its index entry
        save_index(self.index)
        self.refresh_list()

    def on_double_click(self, event):
        selection = self.listbox.curselection()
        if not selection:
            return

        entry_id = self._row_to_id[selection[0]]
        entry = self.index[entry_id]

        try:
            plaintext = decrypt_entry(entry, self.key)
        except Exception as e:
            messagebox.showerror("Decryption failed",
                                 "Wrong password or corrupted vault!")
            return

        self.list_frame.pack_forget()
        self.viewer = Viewer(self.root, plaintext, entry["filename"], on_back=self.show_list)
        self.viewer.pack(expand=True, fill="both")

    def show_list(self):
        self.viewer.destroy()
        self.viewer = None
        self.list_frame.pack(padx=20, pady=20, expand=True, fill="both")


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

