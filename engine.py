#!/usr/bin/env python3
"""
engine.py - TrueHD Atmos -> DDP Atmos (DEE workflow)

Everything that is not GUI lives here:
  * Atmos YAML editing   (was atmos_editor.py)
  * DEE XML generation   (was ddp_config.py)
  * decode / encode flow (was main.py)

The GUI (main.py) drives it through the Encoder class. It can also be used
directly from a terminal:  python engine.py -i movie.thd
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Tuple
from xml.dom import minidom

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq

# ---------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(SCRIPT_DIR, "binaries")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "ddp_encode")

ATMOS_MODES = ("5.1", "7.1", "both")
BITRATES_5_1 = (384, 448, 576, 640, 768, 1024)
BITRATES_7_1 = (1152, 1280, 1536, 1664)
DRC_PROFILES = ("none", "film_standard", "film_light", "music_standard", "music_light", "speech")
DOWNMIX_MODES = ("not_indicated", "loro", "ltrt", "ltrt-pl2")
WARP_MODES = ("normal", "warping", "prologiciix", "loro")

REQUIRED_TOOLS = ("truehdd", "dee", "eac3_7.1_atmos_fix")

RE_DEE_PROGRESS = re.compile(r"Overall progress: (\d+\.\d+)")
RE_DEE_VERSION = re.compile(r"Version\s+([\d\.]+)")
RE_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")

# Hide console windows of child processes when running from the GUI on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LogFn = Callable[[str, str], None]                          # (level, message)
ProgressFn = Callable[[str, Optional[float], str], None]    # (stage, percent|None, detail)


class EncodeError(Exception):
    """A recoverable, user-presentable failure."""


class EncodeCancelled(Exception):
    """Raised when the run was cancelled by the user."""


# ---------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------

@dataclass
class EncodeOptions:
    input_path: str
    atmos_mode: str = "both"
    bitrate_5_1: int = 1024
    bitrate_7_1: int = 1536
    drc: str = "none"
    dialogue_intelligence: str = "true"
    disable_dbfs: bool = False
    preferred_downmix: str = "not_indicated"
    warp_mode: str = "normal"
    keep_intermediate: bool = True   # keep .atmos/.atmos.audio/.atmos.metadata after encoding


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def format_time(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02}:{(s % 3600) // 60:02}:{s % 60:02}"


def estimate_remaining(elapsed: float, percent: float) -> float:
    if percent <= 0:
        return 0.0
    return max(0.0, elapsed / (percent / 100.0) - elapsed)


def safe_rename(src: str, dst: str) -> None:
    if os.path.exists(dst):
        os.remove(dst)
    os.rename(src, dst)


def safe_copy_or_move(src: str, dst: str) -> None:
    """Move if possible; fall back to copy+remove across devices."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        os.remove(dst)
    try:
        os.rename(src, dst)
    except OSError:
        shutil.copy2(src, dst)
        try:
            os.remove(src)
        except FileNotFoundError:
            pass


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _iter_lines(stream) -> Iterator[str]:
    """
    Yield text pieces from a binary pipe, splitting on \\r and \\n so that
    progress bars (which redraw with \\r) arrive as separate updates.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        pending += decoder.decode(chunk)
        parts = re.split(r"[\r\n]+", pending)
        pending = parts.pop()
        for part in parts:
            part = part.strip()
            if part:
                yield part
    pending = (pending + decoder.decode(b"", final=True)).strip()
    if pending:
        yield pending


# ---------------------------------------------------------------------
# Tool validation
# ---------------------------------------------------------------------

def exe_path(name: str) -> str:
    exe = f"{name}.exe" if platform.system().lower() == "windows" else name
    return os.path.join(BIN_DIR, exe)


def missing_tools(atmos_mode: str = "both") -> List[str]:
    """File names missing from the binaries folder (the 7.1 fix tool is only needed for 7.1)."""
    names = ["truehdd", "dee"]
    if atmos_mode in ("7.1", "both"):
        names.append("eac3_7.1_atmos_fix")
    return [os.path.basename(exe_path(n)) for n in names if not os.path.isfile(exe_path(n))]


@dataclass(frozen=True)
class Tools:
    truehdd: str
    dee: str
    eac3_fix: str

    @staticmethod
    def load(atmos_mode: str, log: LogFn) -> "Tools":
        log("info", "Verifying required binaries...")
        missing = missing_tools(atmos_mode)
        if missing:
            raise EncodeError(f"Missing in '{BIN_DIR}': {', '.join(missing)}")
        tools = Tools(
            truehdd=exe_path("truehdd"),
            dee=exe_path("dee"),
            eac3_fix=exe_path("eac3_7.1_atmos_fix"),
        )
        try:
            res = subprocess.run([tools.dee], capture_output=True, text=True, timeout=30,
                                 encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
            m = RE_DEE_VERSION.search(res.stdout or "")
            if m:
                log("info", f"DEE Encoder version: {m.group(1)}")
            else:
                log("warn", "Could not determine DEE version.")
        except Exception:
            log("warn", "Could not run DEE to get its version.")
        log("ok", "All required binaries found.")
        return tools


# ---------------------------------------------------------------------
# Atmos YAML editing (was atmos_editor.py)
# ---------------------------------------------------------------------

def transform_atmos_file_inplace(file_path: str, warp_mode: str = "normal") -> bool:
    """
    Rewrites an Atmos YAML file in place, but only if scBedConfiguration is
    exactly [0, 1, 2, 3, 6, 7, 4, 5].

    Changes applied:
      - scBedConfiguration  -> [3]
      - creationTool        -> "DRX-Lab", creationToolVersion -> "0.4.0"
      - warpMode            -> given value
      - bedInstances[0]     -> LFE only
      - objects             -> IDs 10..20
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096

    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.load(f)

    presentations = data.get("presentations", [])
    if not presentations:
        return False

    presentation = presentations[0]
    if presentation.get("scBedConfiguration", []) != [0, 1, 2, 3, 6, 7, 4, 5]:
        return False

    seq = CommentedSeq([3])
    seq.fa.set_flow_style()
    presentation["scBedConfiguration"] = seq
    presentation["creationTool"] = "DRX-Lab"
    presentation["creationToolVersion"] = "0.4.0"
    presentation["warpMode"] = warp_mode
    if "bedInstances" in presentation and len(presentation["bedInstances"]) > 0:
        presentation["bedInstances"][0]["channels"] = [{"channel": "LFE", "ID": 3}]
    presentation["objects"] = [{"ID": i} for i in range(10, 21)]

    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)
    return True


# ---------------------------------------------------------------------
# DEE XML generation (was ddp_config.py)
# ---------------------------------------------------------------------

def _sub(parent: ET.Element, tag: str, text=None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, **attrs)
    if text is not None:
        el.text = str(text)
    return el


def _prettify(elem: ET.Element) -> str:
    return minidom.parseString(ET.tostring(elem, "utf-8")).toprettyxml(indent="  ")


def write_dee_xml(
    output_dir: str,
    atmos_file: str,
    ec3_file: str,
    data_rate: int,
    xml_path: str,
    drc_profile: str,
    dialogue_intelligence: str,
    dialogue_level: int,
    preferred_downmix_mode: str,
    use_7_1: bool = False,
) -> str:
    root = ET.Element("job_config")

    # Input
    atmos = _sub(_sub(_sub(root, "input"), "audio"), "atmos_mezz", version="1")
    _sub(atmos, "file_name", atmos_file)
    _sub(atmos, "timecode_frame_rate", "23.976")
    _sub(atmos, "offset", "auto")
    _sub(atmos, "ffoa", "auto")
    _sub(_sub(_sub(atmos, "storage"), "local"), "path", output_dir)

    # Filter
    encode = _sub(_sub(_sub(root, "filter"), "audio"), "encode_to_atmos_ddp", version="1")
    measure = _sub(_sub(encode, "loudness"), "measure_only")
    _sub(measure, "metering_mode", "1770-4")
    _sub(measure, "dialogue_intelligence", dialogue_intelligence)
    _sub(measure, "speech_threshold", 15)
    _sub(encode, "data_rate", data_rate)
    _sub(encode, "timecode_frame_rate", "23.976")
    _sub(encode, "start", "first_frame_of_action")
    _sub(encode, "end", "end_of_file")
    _sub(encode, "time_base", "file_position")
    _sub(encode, "prepend_silence_duration", 0.0)
    _sub(encode, "append_silence_duration", 0.0)

    drc = _sub(encode, "drc")
    _sub(drc, "line_mode_drc_profile", drc_profile)
    _sub(drc, "rf_mode_drc_profile", drc_profile)

    downmix = _sub(encode, "downmix")
    _sub(downmix, "loro_center_mix_level", "-3")
    _sub(downmix, "loro_surround_mix_level", "-3")
    _sub(downmix, "ltrt_center_mix_level", "-3")
    _sub(downmix, "ltrt_surround_mix_level", "-3")
    _sub(downmix, "preferred_downmix_mode", preferred_downmix_mode)

    trims = _sub(encode, "custom_trims")
    _sub(trims, "surround_trim_5_1", 0)
    _sub(trims, "height_trim_5_1", -3)

    _sub(encode, "custom_dialnorm", dialogue_level)
    if use_7_1:
        _sub(encode, "encoding_backend", "atmosprocessor")
        _sub(encode, "encoder_mode", "bluray")

    # Output
    ec3 = _sub(_sub(root, "output"), "ec3", version="1")
    _sub(ec3, "file_name", ec3_file)
    _sub(_sub(_sub(ec3, "storage"), "local"), "path", output_dir)

    # Misc
    temp_dir = _sub(_sub(root, "misc"), "temp_dir")
    _sub(temp_dir, "clean_temp", "true")
    _sub(temp_dir, "path", output_dir)

    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(_prettify(root))
    return xml_path


# ---------------------------------------------------------------------
# TrueHD stream analysis (Atmos required)
# ---------------------------------------------------------------------

@dataclass
class StreamInfo:
    atmos_flag: str                       # "true" / "false"
    last_presentation_num: Optional[int]
    last_dialogue_level: int              # capped to >= -31, or 0 if disabled


def analyze_stream(truehdd_exec: str, input_path: str, disable_dbfs: bool) -> StreamInfo:
    result = subprocess.run(
        [truehdd_exec, "info", input_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, creationflags=_NO_WINDOW,
    )
    lines = result.stdout.splitlines()

    atmos_flag: Optional[str] = None
    for line in lines:
        if "Dolby Atmos" in line:
            atmos_flag = line.split()[-1].lower()
            break
    if atmos_flag is None:
        atmos_flag = "false"

    current_presentation: Optional[int] = None
    last_presentation: Optional[int] = None
    last_dialogue_level = -31

    for line in lines:
        if line.strip().startswith("Presentation "):
            try:
                current_presentation = int(line.strip().split()[1])
            except Exception:
                current_presentation = None
                continue

        if "Dialogue Level" in line and current_presentation is not None and not disable_dbfs:
            last_presentation = current_presentation
            try:
                level_value = int(line.split()[-2])
                last_dialogue_level = max(level_value, -31)
            except Exception:
                last_dialogue_level = -31

    if disable_dbfs:
        last_dialogue_level = 0

    return StreamInfo(atmos_flag, last_presentation, last_dialogue_level)


# ---------------------------------------------------------------------
# Run id / artifact handling
# ---------------------------------------------------------------------

def compute_run_ids(input_path: str) -> Tuple[str, str]:
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    hex_id = hashlib.md5(base_name.encode("utf-8")).hexdigest()[:6]
    return base_name, hex_id


def _find_artifact(folder: str, ext: str, prefix: Optional[str] = None) -> Optional[str]:
    if not os.path.isdir(folder):
        return None
    for name in os.listdir(folder):
        low = name.lower()
        if low.endswith(ext) and (prefix is None or low.startswith(prefix.lower())):
            return os.path.join(folder, name)
    return None


def normalize_atmos_artifacts_to_hex(hex_id: str, work_dir: str) -> Tuple[str, str, str]:
    exts = (".atmos", ".atmos.audio", ".atmos.metadata")
    targets = {ext: os.path.join(OUTPUT_DIR, f"{hex_id}{ext}") for ext in exts}

    sources = {}
    for ext in exts:
        # work_dir belongs to this run only; OUTPUT_DIR is shared, so match on the run id there.
        sources[ext] = _find_artifact(work_dir, ext) or _find_artifact(OUTPUT_DIR, ext, prefix=hex_id)

    missing = [ext for ext, src in sources.items() if src is None]
    if missing:
        raise EncodeError(
            f"Missing Atmos artifacts: {', '.join(missing)} (searched in: {work_dir} and {OUTPUT_DIR})"
        )

    for ext, src in sources.items():
        if os.path.abspath(src) != os.path.abspath(targets[ext]):
            safe_copy_or_move(src, targets[ext])

    return targets[".atmos"], targets[".atmos.audio"], targets[".atmos.metadata"]


# ---------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------

class Encoder:
    """
    One encode run. Call run() (blocking, intended for a worker thread);
    call cancel() from any thread to stop it.
    """

    def __init__(self, options: EncodeOptions, log: Optional[LogFn] = None,
                 progress: Optional[ProgressFn] = None):
        self.opts = options
        self._log = log or (lambda level, msg: None)
        self._progress = progress or (lambda stage, pct, detail: None)
        self._cancel = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._step = 0
        self._steps_total = 0
        self._last_emit = 0.0

    # ---- control ------------------------------------------------------

    def log(self, level: str, msg: str) -> None:
        self._log(level, msg)

    def cancel(self) -> None:
        self._cancel.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise EncodeCancelled()

    # ---- progress -----------------------------------------------------

    def _emit(self, stage: str, percent: Optional[float], start: float,
              extra: str = "", force: bool = False) -> None:
        now = time.time()
        if not force and percent is not None and percent < 100 and now - self._last_emit < 0.1:
            return
        self._last_emit = now
        elapsed = now - start
        parts = [f"elapsed {format_time(elapsed)}"]
        if percent is not None:
            percent = max(0.0, min(100.0, percent))
            parts.append(f"remaining {format_time(estimate_remaining(elapsed, percent))}")
        if extra:
            parts.append(extra)
        label = f"[{self._step}/{self._steps_total}] {stage}" if self._step else stage
        self._progress(label, percent, ", ".join(parts))

    def _begin_step(self, stage: str, percent: Optional[float] = None) -> float:
        self._step += 1
        start = time.time()
        self._emit(stage, percent, start, force=True)
        return start

    # ---- subprocess runner -------------------------------------------

    def _run(self, cmd: List[str], on_line: Callable[[str], bool],
             cwd: Optional[str] = None) -> Tuple[int, List[str]]:
        """
        Runs cmd with stdout+stderr merged and streamed line by line.
        on_line returns True when it consumed the line as progress; every
        other line is kept in a short tail that is shown if the tool fails.
        """
        self._check_cancel()
        tail: deque = deque(maxlen=12)
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, creationflags=_NO_WINDOW,
        )
        with self._proc_lock:
            self._proc = proc
        try:
            for line in _iter_lines(proc.stdout):
                if not on_line(line):
                    tail.append(line)
            proc.wait()
        finally:
            with self._proc_lock:
                self._proc = None
            if proc.poll() is None:
                proc.kill()
            if proc.stdout:
                proc.stdout.close()
        self._check_cancel()
        return proc.returncode, list(tail)

    def _fail(self, what: str, returncode: int, tail: List[str]) -> None:
        for line in tail:
            self.log("error", f"  {line}")
        raise EncodeError(f"{what} failed with exit code {returncode}")

    # ---- workflow steps ----------------------------------------------

    def _log_settings(self, stream: StreamInfo, hex_id: str) -> None:
        o = self.opts
        self.log("info", f"Run ID: {hex_id}")
        if o.atmos_mode in ("5.1", "both"):
            self.log("info", f"Atmos 5.1 bitrate: {o.bitrate_5_1} kbps")
        if o.atmos_mode in ("7.1", "both"):
            self.log("info", f"Atmos 7.1 bitrate: {o.bitrate_7_1} kbps")
        self.log("info", f"Dialogue level: {stream.last_dialogue_level} dB | "
                         f"Dialogue intelligence: {o.dialogue_intelligence} | DRC: {o.drc}")
        self.log("info", f"Preferred downmix: {o.preferred_downmix} | Warp mode: {o.warp_mode}")
        if stream.last_presentation_num is not None:
            self.log("info", f"Last presentation: {stream.last_presentation_num}")

    def _decode(self, tools: Tools, input_abs: str, stream: StreamInfo,
                work_dir: str, hex_id: str) -> None:
        cmd = [
            tools.truehdd, "decode",
            "--loglevel", "off",
            "--progress", input_abs,
            "--output-path", hex_id,
            "--bed-conform",
        ]
        if self.opts.warp_mode:
            cmd += ["--warp-mode", self.opts.warp_mode]
        if stream.last_presentation_num is not None:
            cmd += ["--presentation", str(stream.last_presentation_num)]

        stage = "Decoding TrueHD Atmos (truehdd)"
        self.log("info", "Starting TrueHDD Atmos decode...")
        start = self._begin_step(stage)

        def on_line(line: str) -> bool:
            m = RE_PERCENT.search(line)
            if m:
                self._emit(stage, float(m.group(1)), start)
                return True
            return False

        rc, tail = self._run(cmd, on_line, cwd=OUTPUT_DIR)
        if rc != 0:
            self._fail("Decoding (truehdd)", rc, tail)
        self._emit(stage, 100.0, start, force=True)

        atmos_abs, _, _ = normalize_atmos_artifacts_to_hex(hex_id, work_dir)
        if transform_atmos_file_inplace(atmos_abs, self.opts.warp_mode):
            self.log("ok", "The Atmos file was successfully transformed.")
        else:
            self.log("info", "No changes were made to the Atmos file.")

    def _run_dee(self, tools: Tools, xml_abs: str, stage: str, dialnorm: int) -> None:
        start = self._begin_step(stage, 0.0)
        extra = f"dialnorm {dialnorm} dB"

        def on_line(line: str) -> bool:
            m = RE_DEE_PROGRESS.search(line)
            if m:
                self._emit(stage, float(m.group(1)), start, extra)
                return True
            return False

        rc, tail = self._run([tools.dee, "-x", xml_abs], on_line)
        if rc != 0:
            self._fail("DEE", rc, tail)
        self._emit(stage, 100.0, start, extra, force=True)

    def _run_fix(self, tools: Tools, src: str, dst: str) -> None:
        stage = "Fixing 7.1 channel layout"
        self.log("info", "Running eac3_7.1_atmos_fix...")
        self._begin_step(stage)
        rc, tail = self._run([tools.eac3_fix, "-i", src, "-o", dst], lambda line: False)
        if rc != 0:
            self._fail("eac3_7.1_atmos_fix", rc, tail)

    def _encode_variant(self, tools: Tools, stream: StreamInfo, hex_id: str, base_name: str,
                        label: str, bitrate: int, use_7_1: bool, cleanup_xml: List[str]) -> str:
        o = self.opts
        tag = label.replace(".", "_")
        out_name = f"{hex_id}_atmos_{tag}.eac3"
        xml_abs = os.path.join(OUTPUT_DIR, f"{hex_id}_encode_atmos_{tag}.xml")
        cleanup_xml.append(xml_abs)

        self.log("info", f"Creating Atmos {label} XML...")
        write_dee_xml(
            output_dir=OUTPUT_DIR,
            atmos_file=f"{hex_id}.atmos",
            ec3_file=out_name,
            data_rate=bitrate,
            xml_path=xml_abs,
            drc_profile=o.drc,
            dialogue_intelligence=o.dialogue_intelligence,
            dialogue_level=stream.last_dialogue_level,
            preferred_downmix_mode=o.preferred_downmix,
            use_7_1=use_7_1,
        )
        if not os.path.isfile(xml_abs):
            raise EncodeError(f"XML was not created where expected: {xml_abs}")

        self.log("info", f"Starting Atmos {label} encoding...")
        self._run_dee(tools, xml_abs, f"Encoding Atmos {label} (DEE)", stream.last_dialogue_level)

        produced = os.path.join(OUTPUT_DIR, out_name)
        if not os.path.isfile(produced):
            raise EncodeError(f"DEE finished but did not write {out_name}")

        if use_7_1:
            fixed = os.path.join(OUTPUT_DIR, f"{hex_id}_atmos_7_1_fix.eac3")
            self._run_fix(tools, produced, fixed)
            if not os.path.isfile(fixed):
                raise EncodeError("eac3_7.1_atmos_fix finished but did not write its output")
            _remove_quiet(produced)
            produced = fixed

        final_name = f"{base_name}_atmos_{tag}.eac3"
        final_abs = os.path.join(OUTPUT_DIR, final_name)
        safe_rename(produced, final_abs)
        self.log("ok", f"Saved: {final_name}")
        return final_abs

    # ---- main entry ---------------------------------------------------

    def run(self) -> List[str]:
        """Runs the whole pipeline. Returns the list of finished .eac3 files."""
        o = self.opts
        if o.atmos_mode not in ATMOS_MODES:
            raise EncodeError(f"Invalid Atmos mode: {o.atmos_mode}")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        tools = Tools.load(o.atmos_mode, self.log)

        input_abs = os.path.abspath(o.input_path)
        if not os.path.isfile(input_abs):
            raise EncodeError(f"Input must be a file: {input_abs}")
        if not input_abs.lower().endswith((".thd", ".mlp")):
            raise EncodeError("Unsupported file type. Provide .thd or .mlp (Atmos).")

        base_name, hex_id = compute_run_ids(input_abs)
        work_dir = os.path.join(OUTPUT_DIR, hex_id)
        os.makedirs(work_dir, exist_ok=True)

        want_51 = o.atmos_mode in ("5.1", "both")
        want_71 = o.atmos_mode in ("7.1", "both")
        self._steps_total = 1 + (1 if want_51 else 0) + (2 if want_71 else 0)
        self._step = 0

        self.log("info", "Analyzing TrueHD stream...")
        self._emit("Analyzing TrueHD stream", None, time.time(), force=True)
        self._check_cancel()
        try:
            stream = analyze_stream(tools.truehdd, input_abs, o.disable_dbfs)
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or "").strip().splitlines()
            raise EncodeError(f"truehdd info failed (exit {e.returncode})"
                              + (f": {detail[-1]}" if detail else ""))
        if stream.atmos_flag != "true":
            raise EncodeError("Input is not Dolby Atmos. This tool only supports Atmos streams.")

        self._log_settings(stream, hex_id)
        self._decode(tools, input_abs, stream, work_dir, hex_id)

        outputs: List[str] = []
        cleanup_xml: List[str] = []
        try:
            if want_51:
                outputs.append(self._encode_variant(tools, stream, hex_id, base_name, "5.1",
                                                    o.bitrate_5_1, False, cleanup_xml))
            if want_71:
                outputs.append(self._encode_variant(tools, stream, hex_id, base_name, "7.1",
                                                    o.bitrate_7_1, True, cleanup_xml))
        finally:
            for xml_abs in cleanup_xml:
                _remove_quiet(xml_abs)

        if not o.keep_intermediate:
            for ext in (".atmos", ".atmos.audio", ".atmos.metadata"):
                _remove_quiet(os.path.join(OUTPUT_DIR, f"{hex_id}{ext}"))
            try:
                os.rmdir(work_dir)
            except OSError:
                pass
            self.log("info", "Intermediate Atmos files removed.")

        return outputs


# ---------------------------------------------------------------------
# Optional command line (same options as the old main.py)
# ---------------------------------------------------------------------

def main_cli(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="TrueHD Atmos -> DDP Atmos (DEE workflow)")
    p.add_argument("-i", "--input", required=True, help="Input TrueHD/MLP file (.thd/.mlp), Atmos required")
    p.add_argument("-am", "--atmos-mode", choices=ATMOS_MODES, default="both")
    p.add_argument("-ba", "--bitrate-atmos-5-1", type=int, choices=BITRATES_5_1, default=1024)
    p.add_argument("-b7", "--bitrate-atmos-7-1", type=int, choices=BITRATES_7_1, default=1536)
    p.add_argument("-d", "--drc", choices=DRC_PROFILES, default="none")
    p.add_argument("-di", "--dialogue-intelligence", choices=("true", "false"), default="true")
    p.add_argument("-nd", "--disable-dbfs", action="store_true")
    p.add_argument("-pd", "--preferred-downmix-mode", choices=DOWNMIX_MODES, default="not_indicated")
    p.add_argument("-w", "--warp-mode", choices=WARP_MODES, default="normal")
    p.add_argument("--no-keep", action="store_true", help="Delete intermediate Atmos files when done")
    args = p.parse_args(argv)

    opts = EncodeOptions(
        input_path=args.input,
        atmos_mode=args.atmos_mode,
        bitrate_5_1=args.bitrate_atmos_5_1,
        bitrate_7_1=args.bitrate_atmos_7_1,
        drc=args.drc,
        dialogue_intelligence=args.dialogue_intelligence,
        disable_dbfs=args.disable_dbfs,
        preferred_downmix=args.preferred_downmix_mode,
        warp_mode=args.warp_mode,
        keep_intermediate=not args.no_keep,
    )

    inline = {"active": False}

    def log(level: str, msg: str) -> None:
        if inline["active"]:
            print()
            inline["active"] = False
        print(f"[{level.upper()}] {msg}")

    def progress(stage: str, pct: Optional[float], detail: str) -> None:
        shown = "  ...  " if pct is None else f"{pct:5.1f}%"
        sys.stdout.write(f"\r{stage}: {shown} ({detail})".ljust(110))
        sys.stdout.flush()
        inline["active"] = True

    enc = Encoder(opts, log, progress)
    try:
        enc.run()
    except EncodeCancelled:
        log("warn", "Cancelled.")
        return 1
    except KeyboardInterrupt:
        enc.cancel()
        log("warn", "Cancelled by user.")
        return 1
    except EncodeError as e:
        log("error", str(e))
        return 1
    log("ok", "Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
