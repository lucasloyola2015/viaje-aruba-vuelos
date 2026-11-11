#!/usr/bin/env python3
"""Genera index.html + README.md + data/top5.json a partir de data/*.jsonl y config.json.

Solo usa la biblioteca estándar. Uso:  python -X utf8 build_report.py
No hace red. Determinístico: mismos datos -> mismo reporte.
"""
import json
import sys
from datetime import date, datetime
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def load_jsonl(path):
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[warn] {p.name}:{n} línea inválida, se ignora", file=sys.stderr)
    return out


def fmt_date(s):
    d = date.fromisoformat(s)
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


def fmt_dur(mins):
    if mins is None:
        return "s/d"
    return f"{int(mins) // 60}h{int(mins) % 60:02d}"


def usd(x):
    return "US$ {:,.0f}".format(x).replace(",", ".")


def ars(x):
    return "ARS {:,.0f}".format(x).replace(",", ".")


# ---------- costos terrestres ----------
def val(d, k):
    v = d[k]
    return v["value"] if isinstance(v, dict) else v


def ground_cost_ars(sc, nights):
    days = nights + 1
    fuel = 2 * val(sc, "km_one_way") * val(sc, "l_per_100km") / 100 * val(sc, "fuel_price_ars_per_l")
    tolls = val(sc, "tolls_round_trip_ars")
    if "parking_ars_first_7_days" in sc:
        parking = val(sc, "parking_ars_first_7_days") + max(0, days - 7) * val(sc, "parking_ars_per_extra_day")
        extra = val(sc, "transfers_ars")
        parts = {"peajes": tolls, "nafta": fuel, "estacionamiento E4": parking, "traslados": extra}
    else:
        garage = val(sc, "garage_ars_per_day") * days
        uber = val(sc, "uber_ars_per_trip") * sc.get("uber_trips", 2)
        parts = {"peajes": tolls, "nafta": fuel, "cochera CABA": garage, "Uber/taxi": uber}
    return sum(parts.values()), parts


def best_ground(cfg, nights, fx):
    options = []
    for key, sc in cfg["ground"]["scenarios"].items():
        total, parts = ground_cost_ars(sc, nights)
        options.append({
            "key": key, "label": sc["label"], "ars": total, "usd": total / fx,
            "parts_ars": parts,
            "unverified": [k for k, v in sc.items() if isinstance(v, dict) and v.get("verified") is False],
        })
    options.sort(key=lambda o: o["usd"])
    return options


# ---------- scoring ----------
def leg_penalty(leg, sc, reasons_excl, notes, tag):
    pen = 0.0
    stops = leg.get("stops")
    if stops is None:
        notes.append(f"{tag}: escalas s/d")
        stops = 1
    if stops >= sc["exclude_if_stops_gte"]:
        reasons_excl.append(f"{tag}: {stops} escalas")
    pen += sc["stops_penalty_usd_per_leg"].get(str(min(stops, 2)), 0)
    dur = leg.get("duration_min")
    if dur is not None:
        over = dur / 60 - sc["leg_duration_free_hours"]
        if over > 0:
            pen += over * sc["leg_duration_penalty_usd_per_hour_over"]
    sh, lo = leg.get("shortest_layover_min"), leg.get("longest_layover_min")
    if sh is not None:
        if sh < sc["layover_exclude_if_shorter_than_min"]:
            reasons_excl.append(f"{tag}: conexión de {sh} min (riesgo alto con niños)")
        elif sh < 120:
            pen += sc["layover_short_penalty_usd_90_to_119"]
    if lo is not None:
        h = lo / 60
        if h > 6:
            pen += (6 - sc["layover_long_free_hours"]) * sc["layover_long_penalty_usd_per_hour_3_5_to_6"]
            pen += (h - 6) * sc["layover_very_long_penalty_usd_per_hour_over_6"]
        elif h > sc["layover_long_free_hours"]:
            pen += (h - sc["layover_long_free_hours"]) * sc["layover_long_penalty_usd_per_hour_3_5_to_6"]
    dt = leg.get("dep_time")
    if dt:
        hh = int(dt.split(":")[0])
        if 0 <= hh < 6:
            pen += sc["departure_00_to_05_penalty_usd_per_leg"]
    return pen


def score_obs(o, cfg, fx):
    sc = cfg["scoring"]
    trip = cfg["trip"]
    nights = (date.fromisoformat(o["return"]) - date.fromisoformat(o["depart"])).days
    excl, notes, breakdown = [], [], {}
    price = float(o["price_total_usd"])
    breakdown["Pasajes (4 personas, precio final)"] = price
    if nights not in trip["nights_allowed"]:
        excl.append(f"{nights} noches fuera de {trip['nights_allowed']}")
    # equipaje
    bags = o.get("bags_included_checked")
    if bags is None:
        notes.append("equipaje incluido s/d: se asume 0 valijas")
        bags = 0
    missing = max(0, sc["bags_needed"] - bags)
    bag_cost = o.get("bags_extra_usd")
    if bag_cost is None:
        bag_cost = missing * sc["missing_checked_bag_usd_each"]
    if bag_cost:
        breakdown[f"Valijas faltantes (~{missing})"] = bag_cost
    # terrestre
    ground = None
    if o["origin"] in ("EZE", "AEP"):
        opts = best_ground(cfg, nights, fx)
        ground = opts[0]
        breakdown["Auto a Bs As: " + ground["label"]] = ground["usd"]
        breakdown["Penalización comodidad (ruta con chicos)"] = sc["drive_comfort_penalty_usd"]
    pen = 0.0
    for tag, leg in (("ida", o.get("outbound", {})), ("vuelta", o.get("inbound", {}))):
        pen += leg_penalty(leg, sc, excl, notes, tag)
    if pen:
        breakdown["Penalización escalas/duración/conexiones/horarios"] = pen
    dev = abs(nights - trip["nights_preferred"])
    if dev:
        breakdown[f"Desvío de noches ({nights} vs {trip['nights_preferred']})"] = dev * sc["nights_deviation_penalty_usd_per_night"]
    late = (date.fromisoformat(o["return"]) - date.fromisoformat(trip["return_must_be_on_or_before"])).days
    if late > 0:
        breakdown["Vuelve después del 31/ene"] = late * sc["return_after_jan31_penalty_usd_per_day"]
    if o.get("airport_change"):
        breakdown["Cambio de aeropuerto"] = sc["airport_change_penalty_usd"]
    total_real = price + bag_cost + (ground["usd"] if ground else 0)
    score = sum(breakdown.values())
    return {"score": score, "cost_real_usd": total_real, "nights": nights, "excluded": excl,
            "notes": notes, "breakdown": breakdown, "ground": ground}


def itin_key(o):
    ob, ib = o.get("outbound", {}), o.get("inbound", {})
    return (o["origin"], o["depart"], o["return"], o.get("source", ""), o.get("airlines", ""), ob.get("via", ""),
            ob.get("dep_time", ""), ib.get("dep_time", ""))


def build():
    cfg = load_json(ROOT / "config.json")
    fxj = load_json(DATA / "fx.json", {})
    fx = float(fxj.get("ars_per_usd") or cfg["fx"]["fallback_ars_per_usd"])
    fx_note = (f"{fx:,.0f} ARS/USD · {fxj.get('source', 'placeholder no verificado')} · {fxj.get('captured_at', 's/d')}"
               .replace(",", "."))
    obs = load_jsonl(DATA / "observations.jsonl")
    runs = load_jsonl(DATA / "runs.jsonl")
    registry = (load_json(DATA / "sources.json", {}) or {}).get("sources", [])
    slog = load_jsonl(DATA / "source_log.jsonl")
    reg_by_id = {s["id"]: s for s in registry}

    def rank_ok(o):
        # rankean fuentes activas o útiles solo-con-Chrome (o desconocidas); el resto solo sirve de contraste
        return reg_by_id.get(o.get("source"), {"status": "active"}).get("status") in ("active", "needs_chrome")

    valid = []
    for o in obs:
        try:
            float(o["price_total_usd"]); date.fromisoformat(o["depart"]); date.fromisoformat(o["return"])
            valid.append(o)
        except Exception:
            print(f"[warn] observación inválida ignorada: {str(o)[:100]}", file=sys.stderr)

    # si una combinación (origen, fechas, fuente) se volvió a consultar, solo cuentan los itinerarios
    # de la corrida más reciente: uno que no se volvió a registrar ya no existe o dejó de estar entre los mejores
    last_run = {}
    for o in sorted(valid, key=lambda x: x.get("captured_at", "")):
        last_run[(o["origin"], o["depart"], o["return"], o.get("source", ""))] = o.get("run_id")
    valid = [o for o in valid if o.get("run_id") == last_run[(o["origin"], o["depart"], o["return"], o.get("source", ""))]]

    # última observación por itinerario
    latest = {}
    for o in sorted(valid, key=lambda x: x.get("captured_at", "")):
        latest[itin_key(o)] = o
    # una observación sin vuelta queda superada si hay otra igual o más nueva, misma ida, con la vuelta leída
    with_inb = {}
    for k, o in latest.items():
        if o.get("inbound"):
            with_inb[k[:7]] = max(with_inb.get(k[:7], ""), o.get("captured_at", ""))
    latest = {k: o for k, o in latest.items()
              if o.get("inbound") or with_inb.get(k[:7], "") < o.get("captured_at", "")}
    scored = []
    for o in latest.values():
        s = score_obs(o, cfg, fx)
        scored.append({**o, **{"_s": s}})
    ok_all = sorted([x for x in scored if not x["_s"]["excluded"]], key=lambda x: x["_s"]["score"])
    ok = [x for x in ok_all if rank_ok(x)]
    for x in ok:  # contraste: mismo origen y fechas en otras fuentes
        x["_others"] = [(y.get("source"), float(y["price_total_usd"])) for y in scored
                        if y is not x and (y["origin"], y["depart"], y["return"]) == (x["origin"], x["depart"], x["return"])
                        and y.get("source") != x.get("source")]
    top, seen, per_depart = [], set(), {}
    for x in ok:
        k = (x["origin"], x["depart"], x["return"])
        if k in seen or per_depart.get(x["depart"], 0) >= 2:
            continue
        seen.add(k)
        per_depart[x["depart"]] = per_depart.get(x["depart"], 0) + 1
        top.append(x)
        if len(top) == 5:
            break

    best = {}
    for x in ok:
        best.setdefault(x["origin"], x)

    # historial por corrida
    by_run = {}
    for o in valid:
        by_run.setdefault(o.get("run_id", "?"), []).append(o)
    history = []
    for rid, items in by_run.items():
        t = min(i.get("captured_at", "") for i in items)
        history.append({"run": rid, "t": t, "min_price": min(float(i["price_total_usd"]) for i in items), "n": len(items)})
    history.sort(key=lambda h: h["t"])

    last_run = runs[-1] if runs else None
    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fx": fx_note, "n_obs": len(valid), "n_runs": len(runs),
        "top5": [{k: v for k, v in x.items()} for x in top],
    }
    (DATA / "top5.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "index.html").write_text(render_html(cfg, top, best, history, last_run, runs, out, fx_note, fx, registry, slog), encoding="utf-8")
    (ROOT / "README.md").write_text(render_md(cfg, top, best, last_run, out, fx_note, registry), encoding="utf-8")
    print(f"OK: {len(valid)} observaciones, {len(top)} en el top, {len(runs)} corridas")


def recommendation(best, cfg):
    ros, eze = best.get("ROS"), best.get("EZE")
    if not ros and not eze:
        return "Todavía no hay datos suficientes."
    if ros and not eze:
        return f"Mejor opción hoy: salir desde Rosario ({usd(ros['_s']['score'])} puntaje). Aún no hay datos de Ezeiza."
    if eze and not ros:
        return f"Mejor opción hoy: Ezeiza en auto ({usd(eze['_s']['score'])} puntaje). Aún no hay datos de Rosario."
    diff = ros["_s"]["score"] - eze["_s"]["score"]
    if diff > 300:
        return (f"Conviene ir en auto a Buenos Aires: ahorra ≈ {usd(diff)} de puntaje frente a salir de Rosario "
                f"(ya incluye cochera, peajes, nafta, traslados y penalización por manejar).")
    return (f"Conviene salir desde Rosario: la opción vía Ezeiza no supera el umbral de ahorro (diferencia {usd(diff)} a favor de Ezeiza; "
            f"se recomienda el auto solo si ahorra más de US$ 300).")


def leg_html(leg, label):
    if not leg:
        return ""
    return (f"<div class='leg'><b>{label}</b> {escape(str(leg.get('dep_time', '')))} · "
            f"{leg.get('stops', '?')} escala(s) · {fmt_dur(leg.get('duration_min'))}"
            f"{' · vía ' + escape(str(leg['via'])) if leg.get('via') else ''}"
            f"{' · conexión más larga ' + fmt_dur(leg.get('longest_layover_min')) if leg.get('longest_layover_min') else ''}</div>")


def svg_history(history):
    if len(history) < 2:
        return "<p class='muted'>El gráfico aparece cuando haya al menos 2 corridas.</p>"
    w, h, pad = 640, 160, 30
    ys = [x["min_price"] for x in history]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1
    pts = []
    for i, y in enumerate(ys):
        px = pad + i * (w - 2 * pad) / (len(ys) - 1)
        py = h - pad - (y - lo) * (h - 2 * pad) / span
        pts.append((px, py, y))
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
    dots = "".join(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3'><title>{usd(v)}</title></circle>" for x, y, v in pts)
    return (f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='Precio mínimo observado por corrida'>"
            f"<polyline points='{line}' fill='none' stroke='currentColor' stroke-width='2'/>{dots}"
            f"<text x='{pad}' y='14' font-size='11'>máx {usd(hi)}</text><text x='{pad}' y='{h - 6}' font-size='11'>mín {usd(lo)}</text></svg>")


ACC = {"ok": ("✅", "ok"), "partial": ("⚠️", "parcial"), "untested": ("⏳", "sin probar"), "captcha": ("🤖", "CAPTCHA"),
       "http_403": ("⛔", "403"), "blocked": ("⛔", "bloqueada"), "empty": ("⚠️", "vacía"), "robots_disallow": ("⛔", "robots.txt"),
       "login_wall": ("🔒", "pide login"), "js_only": ("⚠️", "solo JS"), "timeout": ("⚠️", "timeout"), "error": ("⚠️", "error"),
       "http_404": ("⚠️", "404"), "not_applicable": ("–", "n/a")}
STATUS = {"active": "activa", "partial": "solo contraste", "needs_chrome": "solo con tu Chrome (3 AM)",
          "candidate": "en prueba", "manual_only": "solo manual", "discarded": "descartada"}
VERDICT = {"util": "útil", "parcial": "parcial", "inutil": "inútil", "sin_evaluar": "sin evaluar", "no_evaluable": "no evaluable"}


def acc_badge(v):
    ico, txt = ACC.get(v or "untested", ("⏳", str(v)))
    return f"{ico} {escape(txt)}"


def https_link(url, label):
    url = str(url or "")
    if url.startswith("https://"):
        return f"<a href='{escape(url, quote=True)}' rel='noopener noreferrer'>{escape(label)}</a>"
    return escape(label)


def source_stats(slog):
    st = {}
    for e in slog:
        d = st.setdefault(e.get("source_id"), {"n": 0, "ok": 0})
        d["n"] += 1
        d["ok"] += 1 if e.get("result") == "ok" else 0
    return st


def render_sources_html(registry, slog, runs):
    if not registry:
        return ""
    st = source_stats(slog)
    counts = {}
    for s in registry:
        counts[s.get("status")] = counts.get(s.get("status"), 0) + 1
    chips = " · ".join(f"{STATUS.get(k, k)}: <b>{v}</b>" for k, v in counts.items())
    order = {"active": 0, "needs_chrome": 1, "partial": 2, "candidate": 3, "manual_only": 4, "discarded": 5}
    rows = ""
    for s in sorted(registry, key=lambda s: (order.get(s.get("status"), 9), s.get("name", ""))):
        u = s.get("usefulness", {}) or {}
        sc = u.get("score")
        util = (f"{sc}/14 · " if sc is not None else "") + VERDICT.get(u.get("verdict", "sin_evaluar"), "sin evaluar")
        stt = st.get(s["id"])
        rate = f"{stt['ok']}/{stt['n']}" if stt else "–"
        a = s.get("access", {})
        rows += (f"<tr><td>{https_link(s.get('url'), s.get('name', s['id']))}<br><span class='muted'>{escape(str(s.get('type', '')))}</span></td>"
                 f"<td>{acc_badge(a.get('agent'))}</td><td>{acc_badge(a.get('chrome'))}</td>"
                 f"<td>{escape(util)}</td><td class='n'>{rate}</td><td>{escape(STATUS.get(s.get('status'), str(s.get('status'))))}</td>"
                 f"<td class='muted'>{escape(str(s.get('last_checked') or '–')[:16])}</td>"
                 f"<td class='muted'>{escape(str(s.get('notes', ''))[:220])}</td></tr>")
    new_cards = ""
    last_ids = [r.get("run_id") for r in runs[-3:] if r.get("run_id")]
    for s in [s for s in registry if s.get("discovered_by") in last_ids]:
        u = s.get("usefulness", {}) or {}
        score = (" " + str(u["score"]) + "/14") if u.get("score") is not None else ""
        new_cards += (f"<li><b>{https_link(s.get('url'), s.get('name', s['id']))}</b> (hallada en {escape(str(s.get('discovered_by')))}): "
                      f"{escape(VERDICT.get(u.get('verdict', 'sin_evaluar'), ''))}{score} — {escape(str(s.get('notes', ''))[:200])}</li>")
    new_block = f"<h3>Fuentes nuevas de las últimas corridas</h3><ul>{new_cards}</ul>" if new_cards else ""
    recent = ""
    by_run = {}
    for e in slog:
        by_run.setdefault(e.get("run_id", "?"), []).append(e)
    main_runs = [r for r in runs if r.get("kind", "main") != "chrome"]
    chrome_runs = [r for r in runs if r.get("kind") == "chrome"]
    for title, lst in (("Última corrida del agente", main_runs[-1:]), ("Última corrida con tu Chrome (3 AM)", chrome_runs[-1:])):
        for r in lst:
            items = ""
            for e in by_run.get(r.get("run_id"), []):
                items += (f"<tr><td>{https_link(e.get('url'), str(e.get('url', ''))[:90])}</td>"
                          f"<td>{escape(str(e.get('via', '')))}</td><td>{escape(str(e.get('purpose', '')))}</td>"
                          f"<td>{acc_badge(e.get('result'))}</td><td class='muted'>{escape(str(e.get('note', ''))[:140])}</td></tr>")
            if items:
                recent += (f"<h3>{title} <span class='muted'>({escape(str(r.get('run_id')))})</span></h3>"
                           f"<table><tr><td><b>Página</b></td><td><b>Vía</b></td><td><b>Para</b></td><td><b>Resultado</b></td><td><b>Nota</b></td></tr>{items}</table>")
    manual = ""
    for s in registry:
        if s.get("status") == "manual_only" or s.get("access", {}).get("chrome") == "captcha":
            manual += f"<li>{https_link(s.get('url'), s.get('name', s['id']))} — {escape(str(s.get('notes', ''))[:160])}</li>"
    manual_block = f"<h3>Para abrir vos a mano</h3><ul>{manual}</ul>" if manual else ""
    return (f"<h2>Fuentes de información</h2><p class='muted'>{chips}. “Éxito” = consultas que devolvieron datos / consultas totales. "
            f"Utilidad = puntaje de la evaluación estándar (máx 14).</p>"
            f"<div style='overflow-x:auto'><table><tr><td><b>Fuente</b></td><td><b>Agente</b></td><td><b>Tu Chrome</b></td><td><b>Utilidad</b></td>"
            f"<td><b>Éxito</b></td><td><b>Estado</b></td><td><b>Último chequeo</b></td><td><b>Notas</b></td></tr>{rows}</table></div>"
            f"{new_block}{recent}{manual_block}")


def render_sources_md(registry):
    if not registry:
        return ""
    lines = ["## Fuentes", "", "| Fuente | Agente | Tu Chrome | Utilidad | Estado |", "|---|---|---|---|---|"]
    for s in registry:
        u = s.get("usefulness", {}) or {}
        sc = f"{u['score']}/14 " if u.get("score") is not None else ""
        a = s.get("access", {})
        lines.append(f"| {s.get('name', s['id'])} | {ACC.get(a.get('agent'), ('', a.get('agent')))[1]} | "
                     f"{ACC.get(a.get('chrome'), ('', a.get('chrome')))[1]} | {sc}{VERDICT.get(u.get('verdict', 'sin_evaluar'), '')} | "
                     f"{STATUS.get(s.get('status'), s.get('status'))} |")
    return "\n".join(lines)


def render_html(cfg, top, best, history, last_run, runs, out, fx_note, fx, registry, slog):
    stale = ""
    if last_run:
        try:
            lt = datetime.fromisoformat(last_run.get("started_at"))
            age = (datetime.now(lt.tzinfo) - lt).total_seconds() / 3600
            if age > 12:
                stale = f"<div class='warn'>⚠ Datos desactualizados: la última corrida fue hace {age:.0f} h.</div>"
        except Exception:
            pass
    src = ""
    if last_run and last_run.get("sources"):
        src = " · ".join(f"{escape(k)}: <b>{escape(str(v))}</b>" for k, v in last_run["sources"].items())
    cards = []
    for i, x in enumerate(top, 1):
        s = x["_s"]
        rows = "".join(f"<tr><td>{escape(k)}</td><td class='n'>{usd(v)}</td></tr>" for k, v in s["breakdown"].items())
        g = ""
        if s["ground"]:
            gp = "".join(f"<li>{escape(k)}: {ars(v)}</li>" for k, v in s["ground"]["parts_ars"].items())
            un = ", ".join(s["ground"]["unverified"]) or "—"
            g = (f"<details><summary>Desglose del auto ({escape(s['ground']['label'])})</summary><ul>{gp}</ul>"
                 f"<p class='muted'>Supuestos sin verificar: {escape(un)}</p></details>")
        notes = "".join(f"<li>{escape(n)}</li>" for n in s["notes"])
        if x.get("_others"):
            notes += "<li>Contraste: " + ", ".join(f"{escape(str(n))} {usd(p)}" for n, p in x["_others"]) + "</li>"
        link = f"<a href='{escape(x['url'], quote=True)}' rel='noopener noreferrer'>Abrir búsqueda</a>" if str(x.get("url", "")).startswith("https://") else ""
        cards.append(f"""
<article class='card'>
  <header><span class='rank'>#{i}</span>
    <div><h3>{escape(x['origin'])} → AUA · {fmt_date(x['depart'])} → {fmt_date(x['return'])} ({s['nights']} noches)</h3>
    <p class='muted'>{escape(str(x.get('airlines', 's/d')))} · fuente: {escape(str(x.get('source', 's/d')))} · {'en vivo' if x.get('live', True) else 'caché'} · {escape(str(x.get('captured_at', ''))[:16])}</p></div>
    <div class='price'><div>{usd(s['cost_real_usd'])}</div><small>costo real estimado<br>puntaje {usd(s['score'])}</small></div></header>
  {leg_html(x.get('outbound'), 'Ida')}{leg_html(x.get('inbound'), 'Vuelta')}
  <table>{rows}</table>{g}
  {('<ul class="muted">' + notes + '</ul>') if notes else ''}
  <p>{link}</p>
</article>""")
    body = "\n".join(cards) or "<p>Todavía no hay opciones. El agente publica acá después de su primera corrida.</p>"
    ground_rows = ""
    for key, sc in cfg["ground"]["scenarios"].items():
        for k, v in sc.items():
            if isinstance(v, dict):
                mark = "✔ verificado" if v.get("verified") else "≈ estimado"
                ground_rows += f"<tr><td>{escape(sc['label'])}</td><td>{escape(k)}</td><td class='n'>{escape(str(v['value']))}</td><td>{mark}</td><td class='muted'>{escape(v.get('note', ''))}</td></tr>"
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Aruba enero 2027 · mejores vuelos</title>
<style>
:root{{--bg:#fafaf7;--fg:#1c1c1a;--mut:#6b6b66;--card:#fff;--bd:#e3e1da;--ac:#0b6e8a;--warn:#fff4d6}}
@media(prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--mut:#a3a29b;--card:#1f1f1d;--bd:#34332f;--ac:#5cc3e0;--warn:#3a3217}}}}
body{{font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:1rem}}
main{{max-width:880px;margin:auto}} h1{{margin:.2rem 0}} .muted{{color:var(--mut);font-size:.9rem}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:1rem;margin:1rem 0}}
.card header{{display:flex;gap:.8rem;align-items:flex-start}} .card h3{{margin:0;font-size:1.05rem}}
.rank{{font-size:1.6rem;font-weight:700;color:var(--ac)}} .price{{margin-left:auto;text-align:right;font-size:1.3rem;font-weight:700}}
.price small{{font-weight:400;color:var(--mut);font-size:.75rem}} table{{border-collapse:collapse;width:100%;margin:.6rem 0;font-size:.9rem}}
td{{padding:.2rem .4rem;border-bottom:1px solid var(--bd)}} td.n{{text-align:right;white-space:nowrap}}
.leg{{font-size:.92rem;margin:.15rem 0}} a{{color:var(--ac)}} .warn{{background:var(--warn);padding:.6rem .8rem;border-radius:8px;margin:.6rem 0}}
.reco{{background:var(--card);border-left:4px solid var(--ac);padding:.7rem 1rem;border-radius:6px;margin:1rem 0}}
svg{{width:100%;height:auto;color:var(--ac)}} circle{{fill:var(--ac)}} details{{margin:.4rem 0}}
</style></head><body><main>
<h1>✈ Rosario → Aruba · enero 2027</h1>
<p class="muted">2 adultos + 2 niños (5 y 3 años) · 7–9 noches · actualizado {escape(out['generated_at'][:16].replace('T', ' '))} · {out['n_runs']} corridas, {out['n_obs']} precios observados</p>
{stale}
<div class="reco"><b>Recomendación actual:</b> {escape(recommendation(best, cfg))}</div>
<p class="muted">Fuentes última corrida: {src or 's/d'} · Tipo de cambio: {escape(fx_note)}</p>
<h2>Las 5 mejores opciones</h2>
{body}
<h2>Precio mínimo observado por corrida</h2>{svg_history(history)}
{render_sources_html(registry, slog, runs)}
<h2>Supuestos del costo en auto a Buenos Aires</h2>
<table><tr><td><b>Escenario</b></td><td><b>Parámetro</b></td><td><b>Valor</b></td><td><b>Estado</b></td><td><b>Nota</b></td></tr>{ground_rows}</table>
<h2>Antes de comprar</h2>
<ul><li>Confirmá el precio final, el equipaje y la tarifa de los niños en la web de la aerolínea: este reporte es informativo y el agente nunca compra.</li>
<li>Pasaporte vigente para los 4 (los chicos también). Si no lo tienen, tramitarlo ya.</li>
<li>ED Card de Aruba (gratis) dentro de los 7 días previos al viaje; llevar partida de nacimiento o libreta de familia.</li></ul>
</main></body></html>"""


def render_md(cfg, top, best, last_run, out, fx_note, registry):
    lines = ["# Aruba enero 2027 — mejores vuelos", "",
             f"_Actualizado {out['generated_at'][:16].replace('T', ' ')} · {out['n_runs']} corridas · {out['n_obs']} precios · TC {fx_note}_", "",
             f"**Recomendación:** {recommendation(best, cfg)}", "",
             "| # | Origen | Salida → Vuelta | Noches | Aerolíneas | Escalas ida/vuelta | Costo real est. | Puntaje |",
             "|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(top, 1):
        s = x["_s"]
        lines.append(f"| {i} | {x['origin']} | {fmt_date(x['depart'])} → {fmt_date(x['return'])} | {s['nights']} | "
                     f"{x.get('airlines', 's/d')} | {x.get('outbound', {}).get('stops', '?')}/{x.get('inbound', {}).get('stops', '?')} | "
                     f"{usd(s['cost_real_usd'])} | {usd(s['score'])} |")
    if not top:
        lines.append("| – | – | Sin datos todavía | – | – | – | – | – |")
    lines += ["", render_sources_md(registry), "", "El detalle completo (desglose, gráfico, supuestos) está en `docs/index.html` (también publicado como página web).", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    build()
