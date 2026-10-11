"""Read-only Git baseline lint comparison and redacted replay extraction."""

import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
initial = json.loads((root / "memory/p0d-ruff-initial.json").read_text("utf-8"))
baseline = []
for filename in sorted({row["filename"] for row in initial}):
    relative = Path(filename).relative_to(root).as_posix()
    source = subprocess.run(["git", "show", "513d57e:" + relative], cwd=root, check=True, capture_output=True).stdout
    result = subprocess.run(
        [str(root / ".venv/Scripts/ruff.exe"), "check", "--stdin-filename", relative, "--output-format=json", "-"],
        cwd=root, input=source, capture_output=True, check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode())
    baseline.extend(json.loads(result.stdout))
(root / "memory/p0d-ruff-main-baseline.json").write_text(json.dumps(baseline, indent=2), "utf-8")
print("main@513d57e Ruff findings:", len(baseline))
for row in baseline:
    print(Path(row["filename"]).relative_to(root), row["location"]["row"], row["code"])

replay_path = root / "memory/p0d-replay.log"
if replay_path.exists():
    events = []
    seen = set()
    for line in replay_path.read_text("utf-8").splitlines():
        start = line.find('{"ts":')
        if start < 0:
            continue
        event = json.loads(line[start:])
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
        if str(event.get("run_id", "")).startswith("redacted-ashare-stream") and encoded not in seen:
            seen.add(encoded)
            events.append(event)
    (root / "memory/p0d-replay.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), "utf-8",
    )
    print("Replay events:", len(events))
    print("Verdicts:", sorted({event["verdict"] for event in events if event["event"] == "critic"}))
