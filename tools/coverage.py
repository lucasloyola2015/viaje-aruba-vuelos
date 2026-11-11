"""Cobertura de consultas en Google Flights: combinaciones origen x ida x noches
nunca consultadas y las consultadas hace más tiempo. Solo biblioteca estándar."""
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
t = cfg["trip"]
d0 = date.fromisoformat(t["depart_window"][0])
d1 = date.fromisoformat(t["depart_window"][1])
last_ret = date.fromisoformat(t["return_must_be_on_or_before"])

last = {}
for line in (ROOT / "data" / "observations.jsonl").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        o = json.loads(line)
    except Exception:
        continue
    if o.get("source") != "google_flights":
        continue
    k = (o.get("origin"), o.get("depart"), o.get("return"))
    ts = o.get("captured_at", "")
    if ts > last.get(k, ""):
        last[k] = ts

never, seen = [], []
d = d0
while d <= d1:
    for n in t["nights_allowed"]:
        r = d + timedelta(days=n)
        if r > last_ret:
            continue
        for org in t["origins"]:
            k = (org, d.isoformat(), r.isoformat())
            (seen if k in last else never).append((last.get(k, ""), k))
    d += timedelta(days=1)

print(f"Combinaciones: {len(never) + len(seen)} · consultadas: {len(seen)} · nunca: {len(never)}")
print("NUNCA:")
for _, k in never:
    print("  ", *k)
print("MÁS VIEJAS (20):")
for ts, k in sorted(seen)[:20]:
    print("  ", ts[:16], *k)
