import os
import sys
import subprocess
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, PhotoImage

APP_DIR = Path.home() / ".robocopy_gui"
LOGS_DIR = APP_DIR / "logs"

APP_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


class RobocopyGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RoBoCopy Manager")
        self.geometry("880x560")
        self.process = None
        self._starting = False  # immediate lock to prevent races

        # Preset defaults (we no longer display advanced controls)
        self.var_R = 1
        self.var_W = 1
        self.var_MT = 32

        # For multiple-file source support
        self.src_var = tk.StringVar()      # for display only (comma-separated basenames)
        self.src_files = []                # list[str] of full file paths selected

        self.dst_var = tk.StringVar()

        self._build_ui()

    def _build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        # Source / Destination
        paths = ttk.LabelFrame(frm, text="Paths", padding=8)
        paths.pack(fill=tk.X)

        src_row = ttk.Frame(paths)
        src_row.pack(fill=tk.X, pady=2)
        ttk.Label(src_row, text="Source (select files):", width=20).pack(side=tk.LEFT)
        ttk.Entry(src_row, textvariable=self.src_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(src_row, text="Browse Source (files)", command=self._browse_src).pack(side=tk.LEFT, padx=4)

        dst_row = ttk.Frame(paths)
        dst_row.pack(fill=tk.X, pady=6)
        # Destination is explicitly a folder — simpler for users
        ttk.Label(dst_row, text="Destination (folder):", width=20).pack(side=tk.LEFT)
        ttk.Entry(dst_row, textvariable=self.dst_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dst_row, text="Browse Folder", command=self._browse_dst).pack(side=tk.LEFT, padx=4)

        # Simple tip label (explicit about destination behavior)
        tip = (
            "This application uses the default 'Fast Copy' preset: /E /MT:32 /R:1 /W:1.\n"
            "Source should be files (you can select multiple files). All selected files must be from the same folder.\n"
            "Destination must be a folder.\n"
            "Same File can not be copy again."
        )
        ttk.Label(frm, text=tip, wraplength=820, justify=tk.LEFT).pack(fill=tk.X, pady=6)

        # Command preview + controls
        ctrl = ttk.Frame(frm)
        ctrl.pack(fill=tk.X, pady=6)

        # store references so we can enable/disable them
        self.preview_btn = ttk.Button(ctrl, text="Preview Command", command=self._preview_command)
        self.preview_btn.pack(side=tk.LEFT)

        self.run_btn = ttk.Button(ctrl, text="Run", command=self._run)
        self.run_btn.pack(side=tk.LEFT, padx=6)

        self.stop_btn = ttk.Button(ctrl, text="Stop", command=self._stop)
        self.stop_btn.pack(side=tk.LEFT)
        # initially disabled
        self.stop_btn.config(state=tk.DISABLED)

        self.save_log_btn = ttk.Button(ctrl, text="Save Log", command=self._save_log_prompt)
        self.save_log_btn.pack(side=tk.LEFT, padx=6)

        self.open_logs_btn = ttk.Button(ctrl, text="Open Logs Folder", command=self._open_logs_folder)
        self.open_logs_btn.pack(side=tk.LEFT, padx=6)

        # Output / Log
        out_frame = ttk.LabelFrame(frm, text="Robocopy Output", padding=6)
        out_frame.pack(fill=tk.BOTH, expand=True)
        self.txt = tk.Text(out_frame, wrap=tk.NONE)
        self.txt.pack(fill=tk.BOTH, expand=True)
        scroll_y = ttk.Scrollbar(out_frame, orient=tk.VERTICAL, command=self.txt.yview)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt.config(yscrollcommand=scroll_y.set)

        # status bar
        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status.pack(fill=tk.X, side=tk.BOTTOM)

    def _browse_src(self):
        # Allow selecting multiple files. They must be from the same parent folder.
        files = filedialog.askopenfilenames(title="Select one or more source files")
        if not files:
            return
        files = list(map(str, files))
        parents = {str(Path(f).parent) for f in files}
        if len(parents) > 1:
            messagebox.showerror("Multiple folders selected",
                                 "Selected files come from different folders. Please select files from the same folder.")
            return
        # store files and update display variable (show basenames, comma-separated)
        self.src_files = files
        basenames = [Path(f).name for f in files]
        display = ", ".join(basenames)
        # truncate display if too long
        if len(display) > 320:
            display = display[:320] + " ... (truncated)"
        self.src_var.set(display)

    def _browse_dst(self):
        # Only allow selecting a folder (destination must be folder)
        d = filedialog.askdirectory(title="Select destination folder")
        if d:
            self.dst_var.set(d)

    def _quote_for_display(self, s: str) -> str:
        if ' ' in s or '\t' in s:
            return f'"{s}"'
        return s

    def _build_command(self):
        """
        Build the robocopy command and return:
          - cmd_list: list for subprocess
          - dst_folder_for_display: string of final destination folder (used for messages)
        """
        # Destination checks
        dst = self.dst_var.get().strip()
        if not dst:
            raise ValueError("Source and destination folder must be set")

        dst_path = Path(dst)

        # Destination: ensure it's a folder. If user typed a filename, convert to parent folder
        if dst_path.exists() and dst_path.is_file():
            folder = str(dst_path.parent)
            self.dst_var.set(folder)
            self._append_text(f"Note: Destination filename ignored — using folder: {folder}\n")
        else:
            if dst_path.suffix:
                folder = str(dst_path.parent)
                self.dst_var.set(folder)
                self._append_text(f"Note: Destination filename ignored — using folder: {folder}\n")
            else:
                folder = str(dst_path)

        cmd_dst = folder

        # Source handling
        if self.src_files:
            # All files selected — use common parent folder as cmd_src
            parents = {str(Path(f).parent) for f in self.src_files}
            if len(parents) != 1:
                raise ValueError("Selected source files must be from the same folder")
            cmd_src = parents.pop()
            file_filters = [Path(f).name for f in self.src_files]
        else:
            # Fallback: allow typed/pasted path in src_var (file or folder)
            src_text = self.src_var.get().strip()
            if not src_text:
                raise ValueError("Source and destination folder must be set")
            src_path = Path(src_text)
            if src_path.exists() and src_path.is_file():
                file_filters = [src_path.name]
                cmd_src = str(src_path.parent)
            else:
                # non-existing path with suffix -> treat like file (copy from parent)
                if not src_path.exists() and src_path.suffix:
                    file_filters = [src_path.name]
                    cmd_src = str(src_path.parent)
                else:
                    file_filters = ["*.*"]
                    cmd_src = str(src_path)

        # Build base command with preset options (no advanced UI).
        cmd = [
            "robocopy",
            cmd_src,
            cmd_dst,
        ]
        cmd.extend(file_filters)  # add each filename or pattern as separate token
        cmd.extend([
            "/E",
            f"/MT:{int(self.var_MT)}",
            f"/R:{int(self.var_R)}",
            f"/W:{int(self.var_W)}"
        ])

        return cmd, folder

    def _preview_command(self):
        try:
            cmd, dst_folder = self._build_command()
        except ValueError as e:
            messagebox.showerror("Missing paths", str(e))
            return
        preview = "Preview: " + " ".join(self._quote_for_display(c) for c in cmd) + "\n"
        preview += f"Files will be copied into: {self._quote_for_display(dst_folder)} (original filenames kept)\n"
        self._append_text(preview)

    def _confirm_mir_if_needed(self, cmd):
        # The simple UI doesn't expose /MIR, but keep a check in case someone edits the command manually
        if any(part.upper() == "/MIR" for part in cmd):
            return messagebox.askyesno(
                "Warning: MIR will delete files",
                "/MIR will make the destination exactly match the source and will DELETE files that are not present in the source.\n\nAre you sure you want to continue?"
            )
        return True

    def _run(self):
        # Prevent double-starts and show running state immediately
        if getattr(self, "_starting", False) or self.process:
            messagebox.showinfo("Already running", "A robocopy process is already running")
            return

        try:
            cmd, dst_folder = self._build_command()
        except ValueError as e:
            messagebox.showerror("Missing paths", str(e))
            return

        if not self._confirm_mir_if_needed(cmd):
            self._append_text("Run cancelled by user (MIR warning)\n")
            return

        # Mark starting immediately to block other invocations
        self._starting = True
        try:
            self.run_btn.config(state=tk.DISABLED)
            self.preview_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
        except Exception:
            pass

        logfile = LOGS_DIR / f"robocopy_{int(time.time())}.log"
        self.status_var.set("Running...")
        self._append_text(f"Starting: {' '.join(self._quote_for_display(c) for c in cmd)}\n")
        self._append_text(f"Destination folder: {dst_folder} (filenames preserved)\n")

        def target():
            try:
                # On Windows, avoid showing a new console window for the subprocess.
                startupinfo = None
                creationflags = 0
                if os.name == 'nt':
                    # Hide the console window for the child process if possible
                    try:
                        creationflags = subprocess.CREATE_NO_WINDOW
                    except Exception:
                        creationflags = 0
                    try:
                        si = subprocess.STARTUPINFO()
                        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startupinfo = si
                    except Exception:
                        startupinfo = None

                    # Use explicit path to robocopy.exe in System32 to avoid redirection issues
                    # (helps when running from a packaged EXE)
                    try:
                        system_root = os.environ.get("SystemRoot", r"C:\Windows")
                        robocopy_path = os.path.join(system_root, "System32", "robocopy.exe")
                        if os.path.exists(robocopy_path):
                            # replace token only if first token is "robocopy"
                            if isinstance(cmd, list) and cmd and os.path.basename(cmd[0]).lower() == "robocopy":
                                cmd[0] = robocopy_path
                    except Exception:
                        pass

                # Start the subprocess and store the Popen immediately
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                    shell=False
                )

                with open(logfile, 'w', encoding='utf-8') as f:
                    for line in self.process.stdout:
                        f.write(line)
                        f.flush()
                        self._append_text(line)
                ret = self.process.wait()
                self._append_text(f"Process exited with code {ret}\n")
            except Exception as ex:
                self._append_text(f"Error: {ex}\n")
            finally:
                # cleanup
                self.process = None
                self._starting = False
                self.status_var.set("Ready")

                def restore():
                    try:
                        self.run_btn.config(state=tk.NORMAL)
                        self.preview_btn.config(state=tk.NORMAL)
                        self.stop_btn.config(state=tk.DISABLED)
                    except Exception:
                        pass

                try:
                    self.after(0, restore)
                except Exception:
                    restore()

        t = threading.Thread(target=target, daemon=True)
        t.start()

    def _stop(self):
        if not self.process:
            messagebox.showinfo("Not running", "No robocopy process is currently running")
            return
        try:
            self.process.terminate()
            self._append_text("Sent terminate signal to process\n")
            # disable stop immediately to avoid repeated clicks
            try:
                self.stop_btn.config(state=tk.DISABLED)
            except Exception:
                pass
        except Exception as e:
            self._append_text(f"Error stopping process: {e}\n")

    def _append_text(self, text):
        def append():
            self.txt.insert(tk.END, text)
            self.txt.see(tk.END)
        try:
            self.after(0, append)
        except Exception:
            try:
                self.txt.insert(tk.END, text)
                self.txt.see(tk.END)
            except Exception:
                pass

    def _open_logs_folder(self):
        try:
            path = str(LOGS_DIR.resolve())
            if os.name == 'nt':
                os.startfile(path)
            elif os.name == 'posix':
                subprocess.Popen(['xdg-open', path])
            else:
                messagebox.showinfo("Logs folder", path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open logs folder: {e}")

    def _save_log_prompt(self):
        path = filedialog.asksaveasfilename(defaultextension='.log', filetypes=[('Log files', '*.log'), ('Text files', '*.txt')])
        if not path:
            return
        content = self.txt.get('1.0', tk.END)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            messagebox.showinfo("Saved", f"Log saved to {path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save log: {e}")


if __name__ == '__main__':
    app = RobocopyGUI()
    app.mainloop()
