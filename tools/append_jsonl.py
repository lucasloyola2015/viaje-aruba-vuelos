"""Anexa líneas JSON de un archivo de entrada a un .jsonl del repo, validando cada una.
Uso: python -X utf8 tools/append_jsonl.py <entrada.jsonl> <data/destino.jsonl>
Para observaciones (destino observations.jsonl) completa campos por defecto y arma
la URL de Google Flights si falta. Solo biblioteca estándar."""
import json
import sys
from pathlib import Path

GF = ("https://www.google.com/travel/flights?q=Flights%20from%20{o}%20to%20AUA%20on%20{d}"
      "%20through%20{r}%202%20adults%202%20children&curr=USD&hl=es")
DEFAULTS = {"live": True, "currency_original": "USD", "airport_change": False,
            "bags_included_checked": None, "bags_extra_usd": None, "notes": "",
            "outbound": {}, "inbound": {}}

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
is_obs = dst.name == "observations.jsonl"
out = []
for i, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
    if not line.strip():
        continue
    o = json.loads(line)
    if is_obs:
        for k, v in DEFAULTS.items():
            o.setdefault(k, v)
        o.setdefault("price_original", o["price_total_usd"])
        if not o.get("url") and o.get("source") == "google_flights":
            o["url"] = GF.format(o=o["origin"], d=o["depart"], r=o["return"])
        for k in ("run_id", "captured_at", "source", "origin", "depart", "return", "price_total_usd"):
            assert k in o, f"línea {i}: falta {k}"
    out.append(json.dumps(o, ensure_ascii=False))

text = dst.read_text(encoding="utf-8") if dst.exists() else ""
sep = "" if (not text or text.endswith("\n")) else "\n"
with dst.open("a", encoding="utf-8", newline="\n") as f:
    f.write(sep + "\n".join(out) + "\n")
print(f"{len(out)} líneas anexadas a {dst}")
