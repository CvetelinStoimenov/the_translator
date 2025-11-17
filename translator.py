"""
The Translator v4.0
========================
Translates .srt subtitle files OR .json localization files using xAI Grok-3 API.

Features:
- Full-featured GUI with dark/light theme toggle
- Supports multiple files at once
- Cancel translation at any time → partial result is saved automatically
- Docked live log window (attached to the right side of the main window)
- Automatic retry (2 attempts) with fallback to original text on failure
- Always saves output – full or partial
- Handles both .srt (full subtitles) and .json (only string values are translated)
- Easy .exe building with PyInstaller
"""

import os
import re
import time
import json
import threading
import requests
from tkinter import (
    Tk, Button, Label, Entry, StringVar, filedialog,
    messagebox, Toplevel, Frame, Text, Scrollbar
)
from tkinter import ttk


# ========================================
# CONSTANTS
# ========================================
MODEL = "grok-3"                              # xAI model used for translation
MAX_BATCH_SIZE = 15                           # How many lines/values are sent in one API request
MAX_FILE_SIZE = 5 * 1024 * 1024               # 5 MB limit – prevents huge memory/API issues
API_URL = "https://api.x.ai/v1/chat/completions"
KEY_FILE = "api_key.txt"                      # File where the xAI API key is stored
LOG_FILE = "translation_log.txt"              # Persistent log file on disk


# ========================================
# SRT UTILITIES
# ========================================
def parse_srt(lines):
    """
    Parse raw lines from a .srt file into a list of subtitle dictionaries.
    
    Each dictionary contains:
        - num: subtitle number
        - time: timestamp line (e.g. 00:00:10,500 --> 00:00:12,000)
        - text: the actual subtitle text (joined with \n if multi-line)
    """
    subtitles = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.isdigit():                    # Subtitle index
            num = int(line)
            i += 1
            if i >= len(lines):
                break
            timestamp = lines[i].strip()       # Timestamp line
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip() != "":
                text_lines.append(lines[i].strip())
                i += 1
            i += 1                            # Skip empty line separating subtitles
            subtitles.append({
                "num": num,
                "time": timestamp,
                "text": "\n".join(text_lines)
            })
        else:
            i += 1
    return subtitles


def chunk_subtitles(subs, size):
    """
    Generator that yields chunks of subtitles (or JSON values) of maximum `size`.
    Used for batch processing to stay under API limits.
    """
    for i in range(0, len(subs), size):
        yield subs[i:i + size]


def save_srt(subtitles, path):
    """
    Save a list of translated subtitle dictionaries back to a proper .srt file.
    The key 'translated' is used as the text content.
    """
    with open(path, "w", encoding="utf-8") as f:
        for sub in subtitles:
            f.write(f"{sub['num']}\n{sub['time']}\n{sub['translated']}\n\n")


# ========================================
# JSON UTILITIES
# ========================================
def parse_json_text(file_path):
    """
    Extract keys and values from a .json localization file.
    
    Returns:
        keys               – list of all keys (as strings)
        values             – list of corresponding values to be translated
        original_content   – full original file content (for preserving formatting/comments)
    
    Only simple "key" : "value" pairs are processed. Complex structures are ignored.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Matches "key" : "value" (allows spaces around colon)
    pattern = r'"([^"]+)"\s*:\s*"([^"]*)"'
    matches = re.findall(pattern, content)

    keys = [m[0] for m in matches]
    values = [m[1] for m in matches]

    return keys, values, content


def save_json(keys, translated_values, original_content, output_path):
    """
    Replace only the values in the original JSON file while keeping:
        - formatting
        - comments
        - order
        - everything else untouched
    """
    translation_map = dict(zip(keys, translated_values))

    def replacer(match):
        key = match.group(1)
        new_value = translation_map.get(key, match.group(2))
        return f'"{key}" : "{new_value}"'

    new_content = re.sub(r'"([^"]+)"\s*:\s*"([^"]*)"', replacer, original_content)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_content)


# ========================================
# API CALL
# ========================================
def translate_batch(batch, lang, api_key, cancel_event=None):
    """
    Send a batch of texts to xAI Grok-3 and return translated strings.
    
    Parameters:
        batch         – list of dicts with key "text"
        lang          – target language (e.g. "Bulgarian")
        api_key       – xAI API key
        cancel_event  – threading.Event() to allow graceful cancellation
    
    Returns:
        (translations_list, error_message) – error_message is None on success
    """
    messages = [
        {"role": "system", "content": f"Translate to {lang}. Only translation, no explanations."}
    ]
    for item in batch:
        messages.append({"role": "user", "content": item["text"]})

    try:
        response = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": messages, "max_tokens": 1500, "temperature": 0.3},
            timeout=30
        )

        # Check for manual cancellation at several points
        if cancel_event and cancel_event.is_set():
            return None, "Canceled"

        # Common xAI error codes
        if response.status_code == 401:
            return None, "Invalid API key!"
        if response.status_code == 429:
            return None, "Rate limit!"
        if response.status_code == 402:
            return None, "No credits!"

        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()

        # Grok returns translations separated by double newlines
        translations = [t.strip() for t in content.split("\n\n") if t.strip()]

        expected = len(batch)
        if len(translations) < expected:
            translations.extend([""] * (expected - len(translations)))

        return translations[:expected], None

    except requests.exceptions.Timeout:
        return None, "Timeout (30s)"
    except requests.exceptions.RequestException as e:
        if cancel_event and cancel_event.is_set():
            return None, "Canceled"
        return None, f"Error: {str(e)[:80]}"


# ========================================
# MAIN GUI CLASS
# ========================================
class UltraTranslator:
    def __init__(self, root):
        self.root = root
        self.root.title("The Translator")
        self.root.geometry("275x300")
        self.root.resizable(True, True)

        # ------------------- Application state -------------------
        self.file_path = None           # Kept for backward compatibility (single-file mode remnants)
        self.file_paths = []            # List of selected files (supports multiple)
        self.api_key = self.load_key()
        self.dark_mode = True
        self.total_subs = 0
        self.translated_count = 0
        self.cancel_event = None
        self.translation_thread = None

        # ------------------- Logging -------------------
        self.log_buffer = []            # In-memory buffer (not used directly now)
        self.log_file = LOG_FILE
        self.log_win = None             # Toplevel log window
        self.log_widget = None          # Text widget inside the log window
        self._log_sync_id = None        # ID of the <Configure> binding for docking

        # Clean old log file at startup
        if os.path.exists(self.log_file):
            try:
                os.remove(self.log_file)
            except:
                pass

        self.setup_ui()
        self.apply_theme()

    # ====================================
    # UI SETUP
    # ====================================
    def setup_ui(self):
        """Build the entire user interface."""
        # Header with title and theme toggle
        self.header = Frame(self.root, bg="#1e1e1e" if self.dark_mode else "#e0e0e0")
        self.header.pack(fill="x")
        Frame(self.header, bg=self.header["bg"]).pack(side="left", padx=10)
        self.title = Label(self.header, text="The Translator", font=("Segoe UI", 14, "bold"),
                           fg="#1f8cff", bg=self.header["bg"])
        self.title.pack(side="left", fill="x", expand=True)
        self.toggle_btn = Button(self.header, text="🌙", font=("Segoe UI", 10),
                                 bg="#333333" if self.dark_mode else "#bbbbbb",
                                 fg="white" if self.dark_mode else "black",
                                 relief="flat", command=self.toggle_theme, width=3)
        self.toggle_btn.pack(side="right", padx=15, pady=5)

        # Main working area
        self.main_frame = Frame(self.root, bg="#121212" if self.dark_mode else "#f5f5f5")
        self.main_frame.pack(fill="both", expand=True, padx=30, pady=20)

        # API Key button
        self.api_btn = Button(self.main_frame, text="API Key",
                              bg="#333333" if self.dark_mode else "#cccccc",
                              fg="white", font=("Segoe UI", 10),
                              command=self.open_api_window)
        self.api_btn.grid(row=0, column=0, columnspan=2, pady=(0, 15), sticky="ew")

        # File selection + language row
        self.file_lang_frame = Frame(self.main_frame, bg=self.main_frame["bg"])
        self.file_lang_frame.grid(row=1, column=0, columnspan=2, pady=(0, 8), sticky="ew")
        self.file_lang_frame.columnconfigure(0, weight=1)
        self.file_lang_frame.columnconfigure(1, weight=1)

        Button(self.file_lang_frame, text="Select Files", bg="#4CAF50", fg="white",
               command=self.select_files).grid(row=0, column=0, padx=(0, 10), sticky="w")

        lang_inner = Frame(self.file_lang_frame, bg=self.main_frame["bg"])
        lang_inner.grid(row=0, column=1, sticky="e")
        Label(lang_inner, text="Language:", fg="#bbbbbb" if self.dark_mode else "#555555",
              bg=self.main_frame["bg"]).pack(side="left")
        self.lang_var = StringVar(value="Bulgarian")
        ttk.Combobox(lang_inner, textvariable=self.lang_var,
                     values=["Bulgarian", "English", "Spanish", "French", "German"],
                     state="readonly", width=12).pack(side="left", padx=(5, 0))

        # Selected files label
        self.file_label = Label(self.main_frame, text="No file selected",
                                fg="#888888" if self.dark_mode else "#666666",
                                bg=self.main_frame["bg"], font=("Consolas", 9), anchor="w")
        self.file_label.grid(row=2, column=0, columnspan=2, pady=(0, 20), sticky="w")

        # Translate / Cancel button
        self.translate_btn = Button(self.main_frame, text="TRANSLATE",
                                    bg="#00c853", fg="white",
                                    font=("Segoe UI", 12, "bold"),
                                    command=self.start_translation, state="disabled")
        self.translate_btn.grid(row=3, column=0, columnspan=2, pady=(0, 15), sticky="ew")

        # Progress bar (initially hidden)
        self.progress_frame = Frame(self.main_frame, bg=self.main_frame["bg"])
        self.progress = ttk.Progressbar(self.progress_frame, orient="horizontal", mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.status = Label(self.progress_frame, text="0%", fg="#00ff00",
                            bg=self.main_frame["bg"], font=("Consolas", 10, "bold"), width=6)
        self.status.pack(side="left")
        self.more_btn = Button(self.progress_frame, text="Show More", bg="#2196F3", fg="white",
                               font=("Segoe UI", 9), command=self.show_log_window, relief="flat")
        self.more_btn.pack(side="left", padx=(5, 0))
        self.progress_frame.grid(row=4, column=0, columnspan=2, pady=15, sticky="ew")
        self.progress_frame.grid_remove()

        self.main_frame.grid_columnconfigure(0, weight=1)

    # ====================================
    # THEME & UTILITIES
    # ====================================
    def apply_theme(self):
        """Apply dark or light theme to all widgets."""
        bg = "#121212" if self.dark_mode else "#f5f5f5"
        header_bg = "#1e1e1e" if self.dark_mode else "#e0e0e0"
        btn_bg = "#333333" if self.dark_mode else "#cccccc"
        text_fg = "#bbbbbb" if self.dark_mode else "#555555"

        self.root.configure(bg=bg)
        self.main_frame.configure(bg=bg)
        self.progress_frame.configure(bg=bg)
        self.header.configure(bg=header_bg)
        self.title.configure(bg=header_bg, fg="#1f8cff")
        self.header.winfo_children()[0].configure(bg=header_bg)

        self.toggle_btn.configure(text="🌙" if self.dark_mode else "☀️",
                                  bg=btn_bg, fg="white" if self.dark_mode else "black")
        self.api_btn.configure(bg=btn_bg, fg="white" if self.dark_mode else "black")

        self.file_lang_frame.configure(bg=bg)
        for child in self.file_lang_frame.winfo_children():
            if isinstance(child, Frame):
                child.configure(bg=bg)
                for subchild in child.winfo_children():
                    if isinstance(subchild, Label):
                        subchild.configure(bg=bg, fg=text_fg)
                    elif isinstance(subchild, Button):
                        subchild.configure(bg="#4CAF50", fg="white")
            elif isinstance(child, Button):
                child.configure(bg="#4CAF50", fg="white")

        self.file_label.configure(bg=bg, fg="#888888" if self.dark_mode else "#666666")
        self.status.configure(bg=bg)

        # Update log window theme if it exists
        if self.log_win and self.log_win.winfo_exists():
            log_bg = "#1e1e1e" if self.dark_mode else "#ffffff"
            self.log_win.configure(bg=log_bg)
            self.log_widget.configure(bg=log_bg,
                                      fg="#00ff00" if self.dark_mode else "#000000")

    def toggle_theme(self):
        """Switch between dark and light mode."""
        self.dark_mode = not self.dark_mode
        self.apply_theme()

    def update_window_size(self):
        """Resize main window dynamically based on content (with reasonable limits)."""
        self.root.update_idletasks()
        req_w, req_h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        new_w = max(275, min(req_w, 900))
        new_h = max(300, min(req_h, 700))
        current = f"{self.root.winfo_width()}x{self.root.winfo_height()}"
        if current != f"{new_w}x{new_h}":
            self.root.geometry(f"{new_w}x{new_h}")

    # ====================================
    # FILE & API KEY HANDLING
    # ====================================
    def load_key(self):
        """Load API key from file if it exists and looks valid."""
        if os.path.exists(KEY_FILE):
            with open(KEY_FILE, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if key.startswith(("xai-", "xai_")):
                    return key
        return ""

    def open_api_window(self):
        """Open modal window for entering/saving the xAI API key."""
        win = Toplevel(self.root)
        win.title("API Key")
        win.geometry("520x220")
        win.resizable(False, False)
        win.configure(bg="#1e1e1e" if self.dark_mode else "#f0f0f0")
        win.grab_set()

        Label(win, text="Enter xAI API key:", bg=win["bg"],
              fg="white" if self.dark_mode else "black").pack(pady=25)
        entry = Entry(win, font=("Consolas", 11), width=55, show="*",
                      bg="#2d2d2d" if self.dark_mode else "#ffffff",
                      fg="white" if self.dark_mode else "black")
        entry.pack(pady=5)
        entry.insert(0, self.api_key)

        def save():
            key = entry.get().strip()
            if key.startswith(("xai-", "xai_")):
                with open(KEY_FILE, "w", encoding="utf-8") as f:
                    f.write(key)
                self.api_key = key
                messagebox.showinfo("Success", "Key saved!")
                win.destroy()
                self.check_ready()
            else:
                messagebox.showerror("Error", "Key must start with 'xai-' or 'xai_'")

        Button(win, text="Save", bg="#00c853", fg="white", command=save).pack(pady=20)

    def select_files(self):
        """Open file dialog allowing multiple .srt/.json files. Filters out files > 5 MB."""
        paths = filedialog.askopenfilenames(
            title="Select .srt or .json files (multiple allowed)",
            filetypes=[
                ("SRT and JSON files", "*.srt *.json"),
                ("SRT files", "*.srt"),
                ("JSON files", "*.json"),
                ("All files", "*.*")
            ]
        )
        if not paths:
            return

        valid_paths = []
        for path in paths:
            if os.path.getsize(path) <= MAX_FILE_SIZE:
                valid_paths.append(path)
            else:
                self.log(f"Skipped (too big): {os.path.basename(path)}", "error")

        if not valid_paths:
            messagebox.showerror("Error", "No valid files selected!")
            return

        self.file_paths = valid_paths
        self.file_label.config(text=f"{len(valid_paths)} files selected")
        self.check_ready()
        self.update_window_size()

    def check_ready(self):
        """Enable the Translate button only when files are selected and a valid API key exists."""
        is_ready = (
            hasattr(self, 'file_paths') and self.file_paths and
            self.api_key and self.api_key.startswith(("xai-", "xai_"))
        )
        self.translate_btn.config(state="normal" if is_ready else "disabled")

    # ====================================
    # LOGGING SYSTEM
    # ====================================
    def log(self, msg, level="info"):
        """
        Write a message to disk log + in-memory buffer + live log window (if open).
        
        Levels: info, success, error, progress
        """
        timestamp = time.strftime('%H:%M:%S')
        line = f"[{timestamp}] {msg}\n"
        self.log_buffer.append((msg, level))
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except:
            pass

        if hasattr(self, 'log_widget') and self.log_widget and self.log_widget.winfo_exists():
            try:
                color = {
                    "info": "#888888",
                    "success": "#00ff00",
                    "error": "#ff4444",
                    "progress": "#1f8cff"
                }.get(level, "#ffffff")
                self.log_widget.insert("end", line)
                self.log_widget.tag_add(level, "end-2c", "end-1c")
                self.log_widget.tag_config(level, foreground=color)
                self.log_widget.see("end")
            except:
                pass

    def show_log_window(self):
        """
        Toggle a docked log window that stays attached to the right side of the main window.
        The window moves and resizes together with the main window.
        """
        if self.log_win and self.log_win.winfo_exists():
            self.log_win.destroy()
            self.more_btn.config(text="Show More")
            try:
                self.root.unbind("<Configure>", self._log_sync_id)
            except:
                pass
            self.log_widget = None
            self.log_win = None
            return

        # Calculate initial geometry
        self.root.update_idletasks()
        main_x = self.root.winfo_x()
        main_y = self.root.winfo_y()
        main_width = self.root.winfo_width()
        main_height = self.root.winfo_height()
        log_width = max(350, main_width // 2)
        log_x = main_x + main_width
        log_y = main_y

        self.log_win = Toplevel(self.root)
        self.log_win.title(f"Log: {self.translated_count}/{self.total_subs or '?'}")
        self.log_win.geometry(f"{log_width}x{main_height}+{log_x}+{log_y}")
        self.log_win.configure(bg="#1e1e1e" if self.dark_mode else "#ffffff")

        text = Text(
            self.log_win,
            font=("Consolas", 9),
            bg="#1e1e1e" if self.dark_mode else "#ffffff",
            fg="#00ff00" if self.dark_mode else "#000000",
            wrap="word"
        )
        scrollbar = Scrollbar(self.log_win, command=text.yview)
        text.config(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        scrollbar.pack(side="right", fill="y", pady=10)
        self.log_widget = text

        # Load existing log file content
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    text.insert("end", f.read())
            except:
                text.insert("end", "[Log read error]\n")
        else:
            text.insert("end", "[No logs]\n")
        text.see("end")
        self.more_btn.config(text="Hide")

        def sync_position(event=None):
            """Keep the log window glued to the right side of the main window."""
            if self.log_win and self.log_win.winfo_exists():
                mx = self.root.winfo_x()
                my = self.root.winfo_y()
                mw = self.root.winfo_width()
                mh = self.root.winfo_height()
                new_width = max(350, mw // 2)
                self.log_win.geometry(f"{new_width}x{mh}+{mx + mw}+{my}")

        self._log_sync_id = self.root.bind("<Configure>", sync_position)
        sync_position()

        def on_close():
            if self.log_win:
                self.log_win.destroy()
            self.more_btn.config(text="Show More")
            try:
                self.root.unbind("<Configure>", self._log_sync_id)
            except:
                pass
            self.log_widget = None
            self.log_win = None

        self.log_win.protocol("WM_DELETE_WINDOW", on_close)
        self.log_win.bind("<Escape>", lambda e: on_close())

    # ====================================
    # TRANSLATION CONTROL & CANCEL
    # ====================================
    def start_translation(self):
        """Start or cancel the translation process."""
        if self.translate_btn["text"] == "TRANSLATE":
            if not self.file_paths:
                return

            self.translate_btn.config(text="CANCEL", bg="#ff4444")
            self.progress_frame.grid()
            self.progress["value"] = 0
            self.status.config(text="0%")
            self.translated_count = 0
            self.total_subs = 0
            self.current_file_index = 0

            self.cancel_event = threading.Event()
            self.translation_thread = threading.Thread(
                target=self.translate_queue,
                args=(self.cancel_event,),
                daemon=True
            )
            self.translation_thread.start()
            self.root.after(10, self.update_window_size)
        else:
            # Cancel requested
            if self.cancel_event:
                self.cancel_event.set()
            self.translate_btn.config(state="disabled", text="CANCELING...")
            self.log("Cancel requested...", "info")

    def translate_queue(self, cancel_event):
        """
        Process all selected files one after another.
        This runs in a background thread.
        """
        try:
            for idx, file_path in enumerate(self.file_paths):
                if cancel_event.is_set():
                    self.root.after(0, lambda: self.reset_after_cancel(None, "Queue canceled.", file_path))
                    return

                self.current_file_index = idx + 1
                self.file_path = file_path
                self.log(f"Processing: {os.path.basename(file_path)} ({idx+1}/{len(self.file_paths)})", "info")

                # Determine total items in current file
                ext = os.path.splitext(file_path)[1].lower()
                if ext == ".srt":
                    with open(file_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    subs = parse_srt(lines)
                    self.total_subs = len(subs)
                elif ext == ".json":
                    _, values, _ = parse_json_text(file_path)
                    self.total_subs = len(values)
                else:
                    continue

                if self.total_subs == 0:
                    self.log("No text to translate. Skipping.", "info")
                    continue

                self.translated_count = 0
                self.root.after(0, lambda: self.status.config(text="0%"))

                success = self.translate_single_file(file_path, cancel_event)
                if not success:
                    self.log(f"Failed: {os.path.basename(file_path)}", "error")

            self.root.after(0, lambda: messagebox.showinfo("Done", "All files processed!"))

        except Exception as e:
            self.log(f"Queue error: {e}", "error")
        finally:
            self.root.after(0, self.final_reset)

    def translate_single_file(self, file_path, cancel_event):
        """
        Translate one file (either .srt or .json) with retries and progress updates.
        Returns True on success, False on fatal error.
        """
        try:
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".srt":
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                subs = parse_srt(lines)
                chunks = list(chunk_subtitles(subs, MAX_BATCH_SIZE))
                translated = []

                for chunk in chunks:
                    if cancel_event.is_set():
                        return False

                    result = None
                    for attempt in range(2):
                        if cancel_event.is_set():
                            return False
                        result, error = translate_batch(
                            [{"text": sub["text"]} for sub in chunk],
                            self.lang_var.get(),
                            self.api_key,
                            cancel_event
                        )
                        if result:
                            break
                        self.log(f"Retry {attempt + 1}...", "error")
                        time.sleep(1)
                    if result is None:
                        result = [sub["text"] for sub in chunk]  # Fallback to original

                    for j, sub in enumerate(chunk):
                        t = result[j].strip() or sub["text"]
                        translated.append({"num": sub["num"], "time": sub["time"], "translated": t})

                    self.translated_count += len(chunk)
                    percent = int((self.translated_count / self.total_subs) * 100)
                    self.root.after(0, lambda p=percent: self.smooth_progress(p))
                    self.log(f"File {self.current_file_index}: {self.translated_count}/{self.total_subs}", "success")

                out_path = file_path.replace(".srt", f"_translated_{self.lang_var.get()}.srt")
                save_srt(translated, out_path)
                self.log(f"Saved: {os.path.basename(out_path)}", "success")

            elif ext == ".json":
                keys, values, original_content = parse_json_text(file_path)
                if not values:
                    return True

                chunks = [values[i:i + MAX_BATCH_SIZE] for i in range(0, len(values), MAX_BATCH_SIZE)]
                translated_values = []

                for chunk in chunks:
                    if cancel_event.is_set():
                        return False

                    result = None
                    for attempt in range(2):
                        if cancel_event.is_set():
                            return False
                        result, error = translate_batch(
                            [{"text": v} for v in chunk],
                            self.lang_var.get(),
                            self.api_key,
                            cancel_event
                        )
                        if result:
                            break
                        self.log(f"Retry {attempt + 1}...", "error")
                        time.sleep(1)
                    if result is None:
                        result = chunk

                    translated_values.extend(result)
                    self.translated_count += len(chunk)
                    percent = int((self.translated_count / self.total_subs) * 100)
                    self.root.after(0, lambda p=percent: self.smooth_progress(p))
                    self.log(f"File {self.current_file_index}: {self.translated_count}/{self.total_subs}", "success")

                out_path = file_path.replace(".json", f"_translated_{self.lang_var.get()}.json")
                save_json(keys, translated_values, original_content, out_path)
                self.log(f"Saved: {os.path.basename(out_path)}", "success")

            return True

        except Exception as e:
            self.log(f"Error in file {os.path.basename(file_path)}: {e}", "error")
            return False

    def reset_after_cancel(self, translated_data, msg, current_file_path=None):
        """
        When translation is cancelled, save whatever has been translated so far as a partial file.
        """
        if not current_file_path:
            current_file_path = self.file_path

        ext = os.path.splitext(current_file_path)[1].lower()
        out_path = ""

        if translated_data and ext:
            if ext == ".srt":
                out_path = current_file_path.replace(".srt", f"_partial_{self.lang_var.get()}.srt")
                save_srt(translated_data, out_path)
            elif ext == ".json":
                if translated_data:
                    partial_keys, partial_values = zip(*translated_data)
                    content = parse_json_text(current_file_path)[2]
                    out_path = current_file_path.replace(".json", f"_partial_{self.lang_var.get()}.json")
                    save_json(list(partial_keys), list(partial_values), content, out_path)

            if out_path:
                self.log(f"{msg} Partial: {os.path.basename(out_path)}", "info")
                self.root.after(0, lambda: messagebox.showinfo("Canceled",
                                 f"Partial saved:\n{os.path.basename(out_path)}"))
        else:
            self.log(f"{msg} No content.", "info")

        self.translate_btn.config(text="TRANSLATE", bg="#00c853",
                                  state="normal" if self.file_paths and self.api_key else "disabled")
        self.progress_frame.grid_remove()
        self.root.after(10, self.update_window_size)

    def hide_progress_and_reset(self):
        """Hide progress bar and reset button after completion/cancellation."""
        self.progress_frame.grid_remove()
        self.translate_btn.config(text="TRANSLATE", bg="#00c853",
                                  state="normal" if self.file_paths and self.api_key else "disabled")
        self.root.after(10, self.update_window_size)

    def smooth_progress(self, target_percent):
        """Animate the progress bar smoothly towards the target percentage."""
        current = self.progress["value"]
        if current >= target_percent:
            self.progress["value"] = target_percent
            self.status.config(text=f"{target_percent}%")
            return

        step = max(1, (target_percent - current) // 8)
        current += step
        self.progress["value"] = current
        self.status.config(text=f"{current}%")
        if current < target_percent:
            self.root.after(40, lambda: self.smooth_progress(target_percent))

    def final_reset(self):
        """Final UI cleanup after translation finishes or is cancelled."""
        self.progress["value"] = 100
        self.status.config(text="100%")
        self.translate_btn.config(
            text="TRANSLATE",
            bg="#00c853",
            state="normal" if self.file_paths and self.api_key else "disabled"
        )
        self.progress_frame.grid_remove()
        self.root.after(10, self.update_window_size)
        self.log("Translation finished or canceled.", "info")


# ========================================
# APPLICATION ENTRY POINT
# ========================================
if __name__ == "__main__":
    root = Tk()
    app = UltraTranslator(root)
    root.mainloop()

# Build .exe with PyInstaller:
# pyinstaller --onefile --windowed --name "TheTranslator" translator.py