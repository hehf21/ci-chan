import json
import subprocess
import sys

out = subprocess.run(
    ["gh", "api", "repos/hehf21/ci-chan/actions/runs/41014445643/jobs?per_page=100&page=1", "--paginate"],
    capture_output=True, text=True, encoding="utf-8",
)
raw = out.stdout
dec = json.JSONDecoder()
idx, jobs = 0, []
while idx < len(raw):
    while idx < len(raw) and raw[idx] in " \n\r\t":
        idx += 1
    if idx >= len(raw):
        break
    obj, end = dec.raw_decode(raw, idx)
    jobs.extend(obj.get("jobs", []))
    idx = end

for j in jobs:
    if j["name"].startswith("merge") or j["status"] != "completed":
        print("JOB", j["name"], j["status"], j.get("conclusion"), j.get("started_at"))
        for s in j.get("steps", []):
            print("   ", s["number"], s["name"], s["status"], s.get("started_at"), s.get("completed_at"))
print("jobs:", len(jobs))
