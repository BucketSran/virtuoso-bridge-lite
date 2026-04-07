#!/usr/bin/env python3
"""
Deep library harvest for real-world Virtuoso libraries.

Compared with the earlier GraduateSAR-specific scripts, this version:
- accepts any library name
- opens the actual schematic-like view (for example schematic_bac)
- enumerates simulation session views instead of hardcoding only "maestro"
- probes likely result directories for each TB/view pair
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "src"))

from dotenv import load_dotenv

load_dotenv(SCRIPT_DIR / ".env")

from virtuoso_bridge import VirtuosoClient  # type: ignore

IL_DIR = SCRIPT_DIR / "examples" / "01_virtuoso" / "assets"
SESSION_VIEW_PREFIXES = ("maestro", "adexl", "normvim", "topsim")
RESULT_VIEW_PREFIXES = ("spectre",)
CONFIG_VIEW_PREFIXES = ("config",)
IGNORE_VIEW_PREFIXES = (
    "layout",
    "symbol",
    "veriloga",
    "au",
    "hspice",
    "calibre",
    "constraint",
    "ideal",
)
REMOTE_MAX_FILES = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lib", required=True, help="Virtuoso library name")
    parser.add_argument(
        "--out-root",
        default="/Users/bucketsran/Documents/TsingProject/iccad/artifacts",
        help="Where to write harvest artifacts",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.12,
        help="Delay between heavier bridge calls, in seconds",
    )
    return parser.parse_args()


def save_json(out_dir: Path, name: str, data: object) -> None:
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"  saved -> {path}")


def sk(client: VirtuosoClient, code: str) -> str:
    result = client.execute_skill(code)
    return (result.output or "").strip().strip('"')


def load_il(client: VirtuosoClient, name: str) -> None:
    result = client.load_il(str(IL_DIR / name))
    print(f"  loaded {name}: {result.output}")


def parse_inventory(raw: str) -> dict[str, list[str]]:
    inventory: dict[str, list[str]] = {}
    for line in raw.split("\\n"):
        line = line.strip().strip('"')
        if "|views=" not in line:
            continue
        cell, view_part = line.split("|views=", 1)
        inventory[cell.strip()] = [v for v in view_part.strip().split() if v]
    return inventory


def split_skill_list(raw: str) -> list[str]:
    values = [item.strip() for item in raw.split("||") if item.strip()]
    return [item for item in values if item.lower() != "nil"]


def _clean_text(value: str) -> str:
    return value.strip().strip('"').strip()


def remote_run(command: str) -> str:
    remote_user = os.getenv("VB_REMOTE_USER") or os.getenv("VB_USER")
    remote_host = os.getenv("VB_REMOTE_HOST") or os.getenv("VB_HOST")
    jump_host = os.getenv("VB_JUMP_HOST")
    if not remote_user or not remote_host:
        return ""

    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=30",
    ]
    if jump_host:
        cmd += ["-J", f"{remote_user}@{jump_host}"]
    cmd += [f"{remote_user}@{remote_host}", f"sh -lc {shlex.quote(command)}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def remote_find(base: str, patterns: list[str], *, maxdepth: int = 6, kind: str | None = None, limit: int = 20) -> list[str]:
    if not base:
        return []
    type_clause = f"-type {kind}" if kind else ""
    results: list[str] = []
    for pattern in patterns:
        command = (
            f"if [ -e {shlex.quote(base)} ]; then "
            f"find {shlex.quote(base)} -maxdepth {maxdepth} {type_clause} -name {shlex.quote(pattern)}; "
            "fi"
        )
        for line in remote_run(command).splitlines():
            line = line.strip()
            if line:
                results.append(line)
    return sorted(set(results))[-limit:]


def remote_read_text(path: str, *, max_lines: int = 200) -> str:
    if not path:
        return ""
    return remote_run(f"sed -n '1,{max_lines}p' {shlex.quote(path)}")


def remote_sqlite_rows(db_path: str, query: str) -> list[list[str]]:
    normalized_query = " ".join(query.split())
    quoted_query = normalized_query.replace('"', '""')
    command = (
        f"if [ -f {shlex.quote(db_path)} ]; then "
        f"sqlite3 -separator '|' {shlex.quote(db_path)} \"{quoted_query}\"; "
        "fi"
    )
    rows = []
    for line in remote_run(command).splitlines():
        if not line.strip():
            continue
        rows.append([cell.strip() for cell in line.split("|")])
    return rows


def first_schematic_view(views: list[str]) -> str | None:
    for view in views:
        if view.lower().startswith("schematic"):
            return view
    return None


def is_tb_cell(cell: str) -> bool:
    lower = cell.lower()
    return lower.startswith("_tb_") or lower.startswith("tb_") or "_tb_" in lower


def classify_views(views: list[str]) -> dict[str, list[str]]:
    session_views: list[str] = []
    result_views: list[str] = []
    config_views: list[str] = []
    other_views: list[str] = []

    for view in views:
        lower = view.lower()
        if lower.startswith(SESSION_VIEW_PREFIXES):
            session_views.append(view)
        elif lower.startswith(RESULT_VIEW_PREFIXES):
            result_views.append(view)
        elif lower.startswith(CONFIG_VIEW_PREFIXES):
            config_views.append(view)
        elif lower.startswith("schematic") or lower.startswith(IGNORE_VIEW_PREFIXES):
            continue
        else:
            other_views.append(view)

    return {
        "session_views": session_views,
        "result_views": result_views,
        "config_views": config_views,
        "other_views": other_views,
    }


def probe_schematic_summary(client: VirtuosoClient, lib: str, cell: str, view: str) -> dict[str, object]:
    raw = sk(
        client,
        f'''
let((cv counts insts)
  cv = dbOpenCellViewByType("{lib}" "{cell}" "{view}" nil "r")
  if(cv != nil
    progn(
      counts = sprintf(nil "inst=%d||nets=%d||pins=%d"
        length(cv~>instances) length(cv~>nets) length(cv~>terminals))
      insts = ""
      foreach(inst cv~>instances
        when(strlen(insts) < 1200
          insts = strcat(insts inst~>name "||" inst~>cellName "||" inst~>libName "||" inst~>viewName ";;")
        )
      )
      dbClose(cv)
      strcat(counts "##" insts)
    )
    "OPEN_FAILED"
  )
)''',
    )
    if raw == "OPEN_FAILED":
        return {"view": view, "open_ok": False}

    counts_part, _, inst_part = raw.partition("##")
    counts: dict[str, object] = {"view": view, "open_ok": True}
    for token in counts_part.split("||"):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        counts[key] = int(value)

    preview: list[dict[str, str]] = []
    for entry in inst_part.split(";;"):
        parts = entry.strip().split("||")
        if len(parts) != 4:
            continue
        preview.append(
            {
                "name": parts[0],
                "cell": parts[1],
                "lib": parts[2],
                "view": parts[3],
            }
        )
    counts["instance_preview"] = preview[:12]
    return counts


def probe_maestro_view(client: VirtuosoClient, lib: str, cell: str, view: str) -> dict[str, object]:
    entry: dict[str, object] = {"view": view, "setups": []}
    raw_setups = sk(
        client,
        f'''
let((setups result)
  setups = maeGetSetupNames("{lib}" "{cell}" "{view}")
  result = ""
  if(setups != nil
    foreach(s setups result = strcat(result s "||"))
    result = "NO_SETUP"
  )
  result
)''',
    )
    if not raw_setups or raw_setups == "NO_SETUP":
        return entry

    setups = split_skill_list(raw_setups)
    if not setups:
        return entry
    entry["setups"] = setups
    for setup in setups[:8]:
        payload = {
            "analyses": sk(
                client,
                f'''
let((r)
  r = ""
  foreach(x maeGetEnabledAnalysis("{lib}" "{cell}" "{view}" "{setup}")
    r = strcat(r (sprintf(nil "%s" x) "||"))
  )
  r
)''',
            ),
            "outputs": sk(
                client,
                f'''
let((r)
  r = ""
  foreach(x maeGetTestOutputs("{lib}" "{cell}" "{view}" "{setup}")
    r = strcat(r (sprintf(nil "%s" x) "||"))
  )
  r
)''',
            ),
            "variables": sk(
                client,
                f'''
let((r)
  r = ""
  foreach(v maeGetVariables("{lib}" "{cell}" "{view}" "{setup}")
    r = strcat(r (car v) "=" (cadr v) "||")
  )
  r
)''',
            ),
            "corners": sk(
                client,
                f'''
let((r)
  r = ""
  foreach(x maeGetCornerNames("{lib}" "{cell}" "{view}" "{setup}")
    r = strcat(r (sprintf(nil "%s" x) "||"))
  )
  r
)''',
            ),
        }
        entry[setup] = {
            key: split_skill_list(value)
            for key, value in payload.items()
        }
    return entry


def probe_result_paths(client: VirtuosoClient, lib_path: str, cell: str, view: str) -> dict[str, object]:
    items = []
    base = f"{lib_path}/{cell}/{view}"
    for path in (
        f"{base}/results",
        f"{base}/results/maestro",
        f"{base}/results/data",
        f"{base}/psf",
        f"{base}/netlist",
    ):
        state = sk(client, f'if(isDir("{path}") "DIR" "MISS")')
        items.append({"path": path, "exists": state == "DIR"})
    return {"view": view, "paths": items}


def probe_library_root(client: VirtuosoClient, lib: str) -> str:
    for expr in (
        f'(ddGetObj "{lib}")~>libPath',
        f'(ddGetObj "{lib}")~>writePath',
        f'(ddGetObj "{lib}")~>readPath',
    ):
        value = sk(client, expr)
        if value and value.lower() != "nil":
            return value
    return "nil"


def parse_log_text(text: str) -> dict[str, object]:
    specs: list[dict[str, str]] = []
    params: dict[str, str] = {}
    run_info: dict[str, str] = {}
    current_corner = ""
    section = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Design specs:"):
            section = "specs"
            continue
        if line.startswith("Design parameters:"):
            section = "params"
            continue
        if line.startswith("Current time:"):
            run_info.setdefault("current_time", line.split(":", 1)[1].strip())
            continue
        if line.startswith("Best design point:"):
            run_info["best_design_point"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Number of points completed:"):
            run_info["points_completed"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Number of simulation errors:"):
            run_info["simulation_errors"] = line.split(":", 1)[1].strip()
            continue

        if section == "specs":
            if "corner" in line and "-" in line:
                current_corner = line
                continue
            parts = re.split(r"\s{2,}|\t+", line)
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                specs.append({"context": current_corner, "name": parts[0], "value": parts[-1]})
        elif section == "params":
            parts = re.split(r"\s{2,}|\t+", line)
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                params[parts[0]] = parts[-1]

    return {"run_info": run_info, "specs": specs[:40], "parameters": params}


def parse_state_xml(text: str) -> dict[str, object]:
    if not text:
        return {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {"raw_preview": text[:1000]}

    data: dict[str, object] = {}
    project_dir = root.find('.//field[@Name="projectDir"]')
    if project_dir is not None and project_dir.text:
        data["project_dir"] = _clean_text(project_dir.text)

    design_info = root.find('.//field[@Name="designInfo"]')
    if design_info is not None and design_info.text:
        data["design_info"] = _clean_text(design_info.text)

    analyses: dict[str, dict[str, str]] = {}
    interesting = {
        "stop",
        "start",
        "step",
        "save",
        "strobeperiod",
        "write",
        "writefinal",
        "compression",
        "errpreset",
        "maxiters",
        "finalTimeOP",
        "saveOption",
    }
    for analysis in root.findall(".//analysis"):
        aname = analysis.attrib.get("Name")
        if not aname:
            continue
        opts: dict[str, str] = {}
        for field in analysis.findall('./partition[@Name="options"]/field'):
            name = field.attrib.get("Name", "")
            value = _clean_text("".join(field.itertext()))
            if not value or value in ("nil", '""'):
                continue
            if name in interesting or len(opts) < 12:
                opts[name] = value
        if opts:
            analyses[aname] = opts
    if analyses:
        data["analyses"] = analyses
    return data


def probe_rdb(rdb_path: str) -> dict[str, object]:
    outputs = remote_sqlite_rows(
        rdb_path,
        "select resultID,name,type,expression from result order by resultID limit 50;",
    )
    metrics = remote_sqlite_rows(
        rdb_path,
        """
select r.name, count(*), min(CAST(rv.value AS REAL)), max(CAST(rv.value AS REAL))
from resultValue rv
join result r on rv.resultID=r.resultID
where r.type=0
group by r.resultID
order by r.resultID
limit 50;
""".strip(),
    )
    metric_samples = remote_sqlite_rows(
        rdb_path,
        """
select p.designPointNumber, r.name, rv.value
from resultValue rv
join point p on rv.pointID=p.pointID
join result r on rv.resultID=r.resultID
where r.type=0
order by p.designPointNumber, r.resultID
limit 80;
""".strip(),
    )
    waveforms = remote_sqlite_rows(
        rdb_path,
        "select name, expression from result where type=1 order by resultID limit 50;",
    )
    parameters = remote_sqlite_rows(
        rdb_path,
        "select name, defaultValue, expression from parameter order by parameterID limit 50;",
    )
    corners = remote_sqlite_rows(
        rdb_path,
        "select name from corner order by cornerID limit 20;",
    )
    return {
        "path": rdb_path,
        "outputs": [
            {"result_id": row[0], "name": row[1], "type": row[2], "expression": row[3] if len(row) > 3 else ""}
            for row in outputs
            if len(row) >= 3
        ],
        "metric_summary": [
            {"name": row[0], "samples": row[1], "min": row[2], "max": row[3]}
            for row in metrics
            if len(row) >= 4
        ],
        "metric_samples": [
            {"design_point": row[0], "name": row[1], "value": row[2]}
            for row in metric_samples
            if len(row) >= 3
        ],
        "waveform_outputs": [
            {"name": row[0], "expression": row[1] if len(row) > 1 else ""}
            for row in waveforms
            if row
        ],
        "parameters": [
            {"name": row[0], "default": row[1] if len(row) > 1 else "", "expression": row[2] if len(row) > 2 else ""}
            for row in parameters
            if row
        ],
        "corners": [row[0] for row in corners if row],
    }


def probe_project_dir(project_dir: str) -> dict[str, object]:
    if not project_dir:
        return {}
    files = remote_find(
        project_dir,
        ["spectre.out", "spectre.inp", "artSimEnvLog", "si.foregnd.log", "designInfo", "*.scs"],
        maxdepth=10,
        kind="f",
        limit=40,
    )
    psf_dirs = remote_find(project_dir, ["psf"], maxdepth=10, kind="d", limit=20)
    previews = []
    for path in files[:6]:
        if path.endswith(("spectre.inp", "si.foregnd.log", "designInfo")):
            previews.append({"path": path, "preview": remote_read_text(path, max_lines=40)})
    return {"files": files, "psf_dirs": psf_dirs, "previews": previews}


def probe_runtime_artifacts(lib_path: str, cell: str, view: str) -> dict[str, object]:
    base = f"{lib_path}/{cell}/{view}"
    state_files = remote_find(f"{base}/test_states", ["*.state"], maxdepth=6, kind="f", limit=REMOTE_MAX_FILES)
    log_files = remote_find(f"{base}/results", ["*.log"], maxdepth=6, kind="f", limit=REMOTE_MAX_FILES)
    rdb_files = remote_find(f"{base}/results", ["*.rdb"], maxdepth=6, kind="f", limit=REMOTE_MAX_FILES)

    parsed_states = []
    project_dirs = []
    for path in state_files:
        parsed = parse_state_xml(remote_read_text(path, max_lines=600))
        parsed["path"] = path
        parsed_states.append(parsed)
        project_dir = parsed.get("project_dir")
        if isinstance(project_dir, str) and project_dir:
            project_dirs.append(project_dir)

    parsed_logs = []
    for path in log_files:
        parsed = parse_log_text(remote_read_text(path, max_lines=300))
        parsed["path"] = path
        parsed_logs.append(parsed)

    parsed_rdbs = [probe_rdb(path) for path in rdb_files]
    project_artifacts = [probe_project_dir(path) for path in project_dirs[:2]]

    return {
        "state_files": parsed_states,
        "log_files": parsed_logs,
        "rdb_files": parsed_rdbs,
        "project_dirs": project_artifacts,
    }


def main() -> None:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_root) / f"{args.lib}_deep_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    client = VirtuosoClient.from_env()

    print("=== Step 0: bridge + IL helpers ===")
    assert sk(client, "1+2") == "3"
    load_il(client, "list_library_cells.il")
    print("  bridge ok")

    print("\n=== Step 1: full inventory ===")
    raw_inventory = client.execute_skill(f'ListLibraryCells("{args.lib}")').output or ""
    inventory = parse_inventory(raw_inventory)
    print(f"  library: {args.lib}")
    print(f"  cells: {len(inventory)}")
    save_json(out_dir, "01_full_inventory", {"library": args.lib, "cells": inventory})

    print("\n=== Step 2: classification ===")
    classification: dict[str, list[str]] = defaultdict(list)
    tb_cells: list[str] = []
    schematic_cells: list[str] = []

    for cell, views in inventory.items():
        lower = cell.lower()
        if is_tb_cell(cell):
            classification["testbenches"].append(cell)
            tb_cells.append(cell)
        elif any(v.lower() == "veriloga" for v in views):
            classification["behavioral_veriloga"].append(cell)
        elif lower.startswith("l0_") or lower.startswith("l1_"):
            classification["top_level"].append(cell)
        elif lower.startswith("lb_"):
            classification["lb_blocks"].append(cell)
        else:
            classification["other_cells"].append(cell)

        if first_schematic_view(views):
            schematic_cells.append(cell)

    save_json(out_dir, "02_classification", classification)
    print(f"  testbenches: {len(tb_cells)}")
    print(f"  schematic-like cells: {len(schematic_cells)}")

    print("\n=== Step 3: schematic probes ===")
    schematic_probes: dict[str, dict[str, object]] = {}
    for cell in schematic_cells:
        view = first_schematic_view(inventory[cell])
        if not view:
            continue
        schematic_probes[cell] = probe_schematic_summary(client, args.lib, cell, view)
        time.sleep(args.delay)
    save_json(out_dir, "03_schematic_probes", schematic_probes)
    print(f"  probed schematic-like cells: {len(schematic_probes)}")

    print("\n=== Step 4: simulation view inventory ===")
    simulation_views: dict[str, dict[str, list[str]]] = {}
    for cell in tb_cells:
        simulation_views[cell] = classify_views(inventory[cell])
    save_json(out_dir, "04_simulation_views", simulation_views)

    print("\n=== Step 5: session/setup probes ===")
    session_data: dict[str, dict[str, object]] = {}
    for cell in tb_cells:
        entry: dict[str, object] = {"views": inventory[cell]}
        view_groups = simulation_views[cell]
        for view in view_groups["session_views"]:
            print(f"  {cell} :: {view}")
            entry[view] = probe_maestro_view(client, args.lib, cell, view)
            time.sleep(args.delay)
        session_data[cell] = entry
    save_json(out_dir, "05_session_data", session_data)

    print("\n=== Step 6: result-path probes ===")
    lib_path = probe_library_root(client, args.lib)
    result_paths: dict[str, object] = {"lib_path": lib_path, "cells": {}}
    if lib_path and lib_path != "nil":
        for cell in tb_cells:
            cell_paths: list[dict[str, object]] = []
            view_groups = simulation_views[cell]
            probe_views = view_groups["session_views"] + view_groups["result_views"] + view_groups["config_views"]
            for view in probe_views:
                cell_paths.append(probe_result_paths(client, lib_path, cell, view))
                time.sleep(args.delay)
            result_paths["cells"][cell] = cell_paths
    save_json(out_dir, "06_result_paths", result_paths)

    print("\n=== Step 7: runtime artifact parse ===")
    runtime_data: dict[str, dict[str, object]] = {}
    if lib_path and lib_path != "nil":
        for cell in tb_cells:
            entry: dict[str, object] = {}
            for view in simulation_views[cell]["session_views"][:4]:
                print(f"  {cell} :: {view} runtime")
                entry[view] = probe_runtime_artifacts(lib_path, cell, view)
                time.sleep(args.delay)
            runtime_data[cell] = entry
    save_json(out_dir, "07_runtime_artifacts", runtime_data)

    summary = {
        "library": args.lib,
        "timestamp": timestamp,
        "total_cells": len(inventory),
        "testbench_count": len(tb_cells),
        "schematic_like_count": len(schematic_cells),
        "output_dir": str(out_dir),
    }
    save_json(out_dir, "00_summary", summary)
    print(f"\n=== DONE ===\n{out_dir}")


if __name__ == "__main__":
    main()
