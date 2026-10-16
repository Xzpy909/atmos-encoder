#!/usr/bin/env python3
"""
main.py - CustomTkinter front end for the Atmos Encoder.

All the real work is done by engine.py; this file only collects the settings,
runs the Encoder in a worker thread and shows progress / log output.

    pip install customtkinter ruamel.yaml
    python main.py
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

import engine

LOG_COLORS = {"ok": "#4cd964", "warn": "#ffcc00", "error": "#ff6b6b"}


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Atmos Encoder")
        self.geometry("840x800")
        self.minsize(740, 680)

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.encoder: Optional[engine.Encoder] = None
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self.bar_indeterminate = False

        # settings variables
        self.input_var = ctk.StringVar()
        self.mode_var = ctk.StringVar(value="both")
        self.br51_var = ctk.StringVar(value="1024")
        self.br71_var = ctk.StringVar(value="1536")
        self.drc_var = ctk.StringVar(value="none")
        self.di_var = ctk.StringVar(value="true")
        self.downmix_var = ctk.StringVar(value="not_indicated")
        self.warp_var = ctk.StringVar(value="normal")
        self.nodbfs_var = ctk.BooleanVar(value=False)
        self.keep_var = ctk.BooleanVar(value=True)

        self._setting_widgets: list = []

        self._build_ui()
        self._on_mode_change()
        self._refresh_tools_status()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._poll)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)  # log expands

        # header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text="Atmos Encoder",
                     font=ctk.CTkFont(size=22, weight="bold")).grid(row=0, column=0, sticky="w")
        self.tools_label = ctk.CTkLabel(header, text="", anchor="e")
        self.tools_label.grid(row=0, column=1, sticky="e")

        # input
        card = ctk.CTkFrame(self)
        card.grid(row=1, column=0, sticky="ew", padx=16, pady=6)
        card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(card, text="Input (.thd / .mlp)").grid(row=0, column=0, padx=12, pady=12, sticky="w")
        self.input_entry = ctk.CTkEntry(card, textvariable=self.input_var,
                                        placeholder_text="Select a TrueHD Atmos file...")
        self.input_entry.grid(row=0, column=1, padx=6, pady=12, sticky="ew")
        self.browse_btn = ctk.CTkButton(card, text="Browse", width=90, command=self._browse)
        self.browse_btn.grid(row=0, column=2, padx=(6, 12), pady=12)
        self._setting_widgets += [self.input_entry, self.browse_btn]

        # settings
        sett = ctk.CTkFrame(self)
        sett.grid(row=2, column=0, sticky="ew", padx=16, pady=6)
        for c in (1, 3):
            sett.grid_columnconfigure(c, weight=1)

        ctk.CTkLabel(sett, text="Output").grid(row=0, column=0, padx=12, pady=(12, 6), sticky="w")
        self.mode_btn = ctk.CTkSegmentedButton(sett, values=list(engine.ATMOS_MODES),
                                               variable=self.mode_var, command=self._on_mode_change)
        self.mode_btn.grid(row=0, column=1, columnspan=3, padx=6, pady=(12, 6), sticky="w")
        self._setting_widgets.append(self.mode_btn)

        self.br51_menu = self._menu(sett, 1, 0, "Atmos 5.1 bitrate (kbps)", self.br51_var,
                                    [str(b) for b in engine.BITRATES_5_1])
        self.br71_menu = self._menu(sett, 1, 2, "Atmos 7.1 bitrate (kbps)", self.br71_var,
                                    [str(b) for b in engine.BITRATES_7_1])
        self._menu(sett, 2, 0, "DRC", self.drc_var, list(engine.DRC_PROFILES))
        self._menu(sett, 2, 2, "Dialogue intelligence", self.di_var, ["true", "false"])
        self._menu(sett, 3, 0, "Preferred downmix", self.downmix_var, list(engine.DOWNMIX_MODES))
        self._menu(sett, 3, 2, "Warp mode", self.warp_var, list(engine.WARP_MODES))

        cb1 = ctk.CTkCheckBox(sett, text="Disable DBFS (dialogue level 0)", variable=self.nodbfs_var)
        cb1.grid(row=4, column=0, columnspan=2, padx=12, pady=(6, 12), sticky="w")
        cb2 = ctk.CTkCheckBox(sett, text="Keep intermediate Atmos files", variable=self.keep_var)
        cb2.grid(row=4, column=2, columnspan=2, padx=12, pady=(6, 12), sticky="w")
        self._setting_widgets += [cb1, cb2]

        # run card
        run = ctk.CTkFrame(self)
        run.grid(row=3, column=0, sticky="ew", padx=16, pady=6)
        run.grid_columnconfigure(0, weight=1)

        btns = ctk.CTkFrame(run, fg_color="transparent")
        btns.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        self.start_btn = ctk.CTkButton(btns, text="Start encoding", width=150,
                                       font=ctk.CTkFont(weight="bold"), command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ctk.CTkButton(btns, text="Cancel", width=90, state="disabled",
                                        fg_color="#8b2c2c", hover_color="#a33636", command=self._cancel)
        self.cancel_btn.pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Open output folder", width=150, fg_color="gray30",
                      hover_color="gray40", command=self._open_output).pack(side="right")

        self.stage_label = ctk.CTkLabel(run, text="Idle", anchor="w")
        self.stage_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 0))
        self.bar = ctk.CTkProgressBar(run)
        self.bar.set(0)
        self.bar.grid(row=2, column=0, sticky="ew", padx=14, pady=6)
        self.detail_label = ctk.CTkLabel(run, text="", anchor="w", text_color="gray70")
        self.detail_label.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 12))

        # log
        self.log_box = ctk.CTkTextbox(self, wrap="word", state="disabled")
        self.log_box.grid(row=4, column=0, sticky="nsew", padx=16, pady=6)
        for level, color in LOG_COLORS.items():
            self.log_box.tag_config(level, foreground=color)

        # footer
        ctk.CTkLabel(
            self, text=f"Binaries folder: {engine.BIN_DIR}   |   Output folder: {engine.OUTPUT_DIR}",
            text_color="gray50", font=ctk.CTkFont(size=11), anchor="w",
        ).grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 10))

    def _menu(self, parent, row: int, col: int, label: str, var, values) -> ctk.CTkOptionMenu:
        ctk.CTkLabel(parent, text=label).grid(row=row, column=col, padx=12, pady=6, sticky="w")
        menu = ctk.CTkOptionMenu(parent, variable=var, values=values, width=170)
        menu.grid(row=row, column=col + 1, padx=6, pady=6, sticky="w")
        self._setting_widgets.append(menu)
        return menu

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _log(self, level: str, msg: str) -> None:
        self.log_box.configure(state="normal")
        prefix = f"[{level.upper()}] " if level in LOG_COLORS else ""
        self.log_box.insert("end", prefix + msg + "\n", level if level in LOG_COLORS else ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _refresh_tools_status(self) -> list:
        missing = engine.missing_tools(self.mode_var.get())
        if missing:
            self.tools_label.configure(text=f"Missing in binaries: {', '.join(missing)}",
                                       text_color="#ff6b6b")
        else:
            self.tools_label.configure(text="Binaries OK", text_color="#4cd964")
        return missing

    def _on_mode_change(self, _value: Optional[str] = None) -> None:
        mode = self.mode_var.get()
        self.br51_menu.configure(state="normal" if mode in ("5.1", "both") else "disabled")
        self.br71_menu.configure(state="normal" if mode in ("7.1", "both") else "disabled")
        self._refresh_tools_status()

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.start_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        state = "disabled" if running else "normal"
        for w in self._setting_widgets:
            w.configure(state=state)
        if not running:
            self._on_mode_change()  # re-apply bitrate menu states

    def _set_bar(self, percent: Optional[float]) -> None:
        if percent is None:
            if not self.bar_indeterminate:
                self.bar.configure(mode="indeterminate")
                self.bar.start()
                self.bar_indeterminate = True
        else:
            if self.bar_indeterminate:
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self.bar_indeterminate = False
            self.bar.set(max(0.0, min(1.0, percent / 100.0)))

    def _reset_bar(self, value: float = 0.0) -> None:
        if self.bar_indeterminate:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.bar_indeterminate = False
        self.bar.set(value)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Select TrueHD Atmos file",
            filetypes=[("TrueHD / MLP", "*.thd *.mlp"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)

    def _open_output(self) -> None:
        os.makedirs(engine.OUTPUT_DIR, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(engine.OUTPUT_DIR)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", engine.OUTPUT_DIR])
        else:
            subprocess.Popen(["xdg-open", engine.OUTPUT_DIR])

    def _start(self) -> None:
        if self.running:
            return
        path = self.input_var.get().strip().strip('"')
        if not path:
            messagebox.showerror("Input missing", "Please select an input file.")
            return
        if not os.path.isfile(path):
            messagebox.showerror("Input not found", f"File does not exist:\n{path}")
            return
        if not path.lower().endswith((".thd", ".mlp")):
            messagebox.showerror("Unsupported file", "Please select a .thd or .mlp file.")
            return
        missing = self._refresh_tools_status()
        if missing:
            messagebox.showerror("Missing binaries",
                                 f"Put these files in:\n{engine.BIN_DIR}\n\n" + "\n".join(missing))
            return

        opts = engine.EncodeOptions(
            input_path=path,
            atmos_mode=self.mode_var.get(),
            bitrate_5_1=int(self.br51_var.get()),
            bitrate_7_1=int(self.br71_var.get()),
            drc=self.drc_var.get(),
            dialogue_intelligence=self.di_var.get(),
            disable_dbfs=self.nodbfs_var.get(),
            preferred_downmix=self.downmix_var.get(),
            warp_mode=self.warp_var.get(),
            keep_intermediate=self.keep_var.get(),
        )
        enc = engine.Encoder(
            opts,
            log=lambda level, msg: self.events.put(("log", level, msg)),
            progress=lambda stage, pct, detail: self.events.put(("progress", stage, pct, detail)),
        )
        self.encoder = enc

        self._set_running(True)
        self._reset_bar()
        self.stage_label.configure(text="Starting...")
        self.detail_label.configure(text="")
        self._log("info", f"--- {os.path.basename(path)} ---")

        def work() -> None:
            try:
                files = enc.run()
                self.events.put(("done", files))
            except engine.EncodeCancelled:
                self.events.put(("cancelled",))
            except engine.EncodeError as e:
                self.events.put(("error", str(e)))
            except Exception:
                self.events.put(("error", traceback.format_exc()))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        if self.encoder and self.running:
            self._log("warn", "Cancelling...")
            self.cancel_btn.configure(state="disabled")
            self.encoder.cancel()

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("Encoding in progress", "Cancel the running encode and exit?"):
                return
            if self.encoder:
                self.encoder.cancel()
            if self.worker:
                self.worker.join(timeout=3)
        self.destroy()

    # ------------------------------------------------------------------
    # Event pump (worker thread -> GUI thread)
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(50, self._poll)

    def _handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "log":
            self._log(ev[1], ev[2])
        elif kind == "progress":
            _, stage, pct, detail = ev
            self.stage_label.configure(text=stage)
            self._set_bar(pct)
            self.detail_label.configure(text=(f"{pct:.1f}%  |  {detail}" if pct is not None else detail))
        elif kind == "done":
            files = ev[1]
            self._set_running(False)
            self._reset_bar(1.0)
            self.stage_label.configure(text="Done")
            self.detail_label.configure(text="")
            self._log("ok", "Encoding completed.")
            messagebox.showinfo("Success", "Encoding completed:\n\n"
                                + "\n".join(os.path.basename(f) for f in files))
        elif kind == "cancelled":
            self._set_running(False)
            self._reset_bar()
            self.stage_label.configure(text="Cancelled")
            self.detail_label.configure(text="")
            self._log("warn", "Encoding cancelled.")
        elif kind == "error":
            msg = ev[1]
            self._set_running(False)
            self._reset_bar()
            self.stage_label.configure(text="Failed")
            self.detail_label.configure(text="")
            self._log("error", msg)
            messagebox.showerror("Encoding failed", msg if len(msg) < 700 else msg[-700:])


if __name__ == "__main__":
    App().mainloop()
