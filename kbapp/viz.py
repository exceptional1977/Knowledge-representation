"""
viz.py — рисование графов в SVG (раскладка вершин — библиотека networkx, рисование — своё).

Четыре рисунка:
  semantic_svg  — семантическая сеть, вершины раскрашены по группам понятий;
  frames_svg    — сеть фреймов: дуги AKO (rdfs:subClassOf) и дуги свойств-связей;
  scenario_svg  — схема сценария: пройденные сцены, текущая, невыбранные ветви пунктиром;
  rules_svg     — граф зависимостей правил.
Координаты вершин семантической сети запоминаются в kb/current/layout.json, чтобы
после добавления вершины картинка не «перемешивалась»: старые вершины стоят на месте,
новая встаёт рядом со своими соседями и подсвечивается.
"""
from __future__ import annotations

import html
import json
import math
from pathlib import Path

import networkx as nx

W, H = 1700, 1050


def esc(s):
    return html.escape(str(s), quote=True)


def _wrap(text, width=18):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines[:3]


REL_COLORS = {"Родовидовые": "#3d5a80", "Атрибутивные": "#9c6644", "Часть – целое": "#6a4c93",
              "Функциональные": "#2a9d8f", "Причинно-следственные": "#d62828", "Временные": "#e76f51",
              "Нормативные": "#bc4749", "Пространственные и количественные": "#6c757d",
              "Падежные (N-арные)": "#f4a261"}


# ====================================================================== семантическая сеть
def semantic_layout(sem, kb_dir: Path):
    """Координаты вершин: из файла layout.json, новые — раскладкой networkx с
    закреплёнными старыми вершинами (spring_layout с fixed)."""
    path = Path(kb_dir) / "layout.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    G = nx.Graph()
    concepts = sem.concepts()
    groups = [g["iri"] for g in sem.groups()]
    for c in concepts:
        G.add_node(c["id"])
    for s, p, o in sem.edges():
        G.add_edge(sem.kb.qname(s), sem.kb.qname(o))
    ids = [c["id"] for c in concepts]
    missing = [i for i in ids if i not in saved]
    if missing:
        init = {}
        for c in concepts:
            if c["id"] in saved:
                init[c["id"]] = tuple(saved[c["id"]])
            else:
                nbrs = [saved[n] for n in G.neighbors(c["id"]) if n in saved]
                if nbrs:      # новая вершина — рядом с соседями
                    init[c["id"]] = (sum(x for x, _ in nbrs) / len(nbrs) + 0.03,
                                     sum(y for _, y in nbrs) / len(nbrs) - 0.03)
                else:         # первая раскладка — группы по кругу
                    k = groups.index(c["group"]) if c["group"] in groups else 0
                    ang = 2 * math.pi * k / max(1, len(groups))
                    init[c["id"]] = (0.6 * math.cos(ang), 0.6 * math.sin(ang))
        fixed = [i for i in ids if i in saved] or None
        if fixed is None:
            # первая раскладка: Камада–Каваи (если есть scipy) даёт равномернее, иначе пружинная
            try:
                pos = nx.kamada_kawai_layout(G)
            except Exception:                                   # noqa: BLE001
                pos = nx.spring_layout(G, pos=init, seed=7, k=0.5, iterations=300)
        else:
            # новые вершины — в центре масс соседей (уже записано в init), без пересчёта
            pos = {n: init[n] for n in ids}
        # нормировка в [0,1] только при первой раскладке
        if not saved:
            xs = [p[0] for p in pos.values()]
            ys = [p[1] for p in pos.values()]
            pos = {n: ((x - min(xs)) / (max(xs) - min(xs) + 1e-9), (y - min(ys)) / (max(ys) - min(ys) + 1e-9))
                   for n, (x, y) in pos.items()}
        for n in missing:
            x, y = pos[n]
            saved[n] = [min(0.96, max(0.04, float(x))), min(0.96, max(0.04, float(y)))]
        _spread(saved, movable=set(missing) if fixed else set(ids))
        for n in ids:
            saved.setdefault(n, list(map(float, pos[n])))
        path.write_text(json.dumps(saved, ensure_ascii=False, indent=0))
    return {i: saved[i] for i in ids}


def _spread(pos, movable, dmin=0.055, rounds=80):
    """Раздвинуть слишком близкие вершины (свой простой проход отталкивания),
    чтобы подписи не налезали друг на друга."""
    keys = list(pos)
    for _ in range(rounds):
        moved = False
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                (x1, y1), (x2, y2) = pos[a], pos[b]
                d = math.hypot(x2 - x1, y2 - y1)
                if d < dmin:
                    dx, dy = ((x2 - x1) / d, (y2 - y1) / d) if d > 1e-9 else (1.0, 0.0)
                    push = (dmin - d) / 2
                    if a in movable:
                        pos[a] = [min(1, max(0, x1 - dx * push)), min(1, max(0, y1 - dy * push))]
                    if b in movable:
                        pos[b] = [min(1, max(0, x2 + dx * push)), min(1, max(0, y2 + dy * push))]
                    moved = True
        if not moved:
            break


def semantic_svg(sem, kb_dir, groups_on=None, rels_on=None, highlight=(), path_ids=(), order_ids=(),
                 selected=None, show_labels=None):
    kb = sem.kb
    pos = semantic_layout(sem, kb_dir)
    rels = sem.relations()
    grp_color = {g["iri"]: g["color"] for g in sem.groups()}
    concepts = [c for c in sem.concepts() if groups_on is None or kb.qname(c["group"]) in groups_on]
    vis = {c["id"] for c in concepts}
    edges = [(s, p, o) for s, p, o in sem.edges()
             if (rels_on is None or kb.qname(p) in rels_on) and kb.qname(s) in vis and kb.qname(o) in vis]
    if groups_on is not None or rels_on is not None:
        linked = {kb.qname(x) for s, _, o in edges for x in (s, o)}
        if rels_on is not None:
            concepts = [c for c in concepts if c["id"] in linked]
            vis = {c["id"] for c in concepts}
    if show_labels is None:
        show_labels = len(edges) <= 70
    M = 60

    def xy(i):
        x, y = pos[i]
        return M + x * (W - 2 * M), M + y * (H - 2 * M)

    path_pairs = {frozenset(p) for p in zip(path_ids, path_ids[1:])}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" class="graph" id="semsvg">',
           '<defs>']
    for name, col in REL_COLORS.items():
        out.append(f'<marker id="m{abs(hash(name)) % 10000}" viewBox="0 0 10 10" refX="19" refY="5" markerWidth="7" '
                   f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{col}"/></marker>')
    out.append('</defs><g class="viewport">')
    for s, p, o in edges:
        si, oi = kb.qname(s), kb.qname(o)
        (x1, y1), (x2, y2) = xy(si), xy(oi)
        r = rels[p]
        col = REL_COLORS.get(r["group"], "#999")
        on_path = frozenset((si, oi)) in path_pairs
        width = 4 if on_path else (1.6 if (si in highlight or oi in highlight) else 1)
        dash = ' stroke-dasharray="5 3"' if r["group"].startswith("Падеж") else ""
        cls = "edge path" if on_path else "edge"
        out.append(f'<line class="{cls}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" '
                   f'stroke-width="{width}"{dash} marker-end="url(#m{abs(hash(r["group"])) % 10000})">'
                   f'<title>{esc(kb.label(s))} —{esc(r["label"])}→ {esc(kb.label(o))}</title></line>')
        if show_labels or on_path:
            out.append(f'<text class="elabel" x="{(x1 + x2) / 2:.1f}" y="{(y1 + y2) / 2 - 3:.1f}" fill="{col}">'
                       f'{esc(r["label"])}</text>')
    order_pos = {i: n + 1 for n, i in enumerate(order_ids)}
    for c in concepts:
        x, y = xy(c["id"])
        col = grp_color.get(c["group"], "#ddd")
        hl = c["id"] in highlight
        sel = c["id"] == selected
        stroke = "#d00000" if hl else ("#111" if sel else "#555")
        sw = 4 if hl or sel else 1.2
        out.append(f'<a href="/semantic?node={esc(c["id"])}"><g class="node{" hl" if hl else ""}">'
                   f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{12 if hl or sel else 9}" fill="{col}" stroke="{stroke}" '
                   f'stroke-width="{sw}"><title>{esc(c["label"])} ({esc(kb.label(c["group"]))})</title></circle>')
        for k, line in enumerate(_wrap(c["label"], 20)):
            out.append(f'<text class="nlabel" x="{x:.1f}" y="{y + 22 + k * 11:.1f}">{esc(line)}</text>')
        if c["id"] in order_pos:
            out.append(f'<circle cx="{x + 12:.1f}" cy="{y - 12:.1f}" r="9" fill="#ffd166" stroke="#333"/>'
                       f'<text class="order" x="{x + 12:.1f}" y="{y - 8.5:.1f}">{order_pos[c["id"]]}</text>')
        out.append('</g></a>')
    out.append('</g></svg>')
    return "\n".join(out)


# ====================================================================== сеть фреймов
def frames_svg(frames, highlight=(), selected=None):
    kb = frames.kb
    nodes, edges = frames.network()
    byid = {n["id"]: n for n in nodes}
    # уровни: глубина по AKO; горизонтальный порядок — обход дерева
    depth = {n["id"]: max(d for _, d in frames.ancestors(kb.expand(n["id"]))) for n in nodes}
    order, seen = [], set()

    def visit(c):
        cid = kb.qname(c)
        if cid in seen or cid not in byid:
            return
        seen.add(cid)
        order.append(cid)
        for ch in frames.children(c):
            visit(ch)
    for r in frames.roots():
        visit(r)
    levels = {}
    for cid in order:
        levels.setdefault(depth[cid], []).append(cid)
    maxd = max(levels) if levels else 0
    Wf, row_h = 1700, 150
    Hf = (maxd + 1) * row_h + 80
    pos = {}
    for d, items in levels.items():
        step = Wf / (len(items) + 1)
        for k, cid in enumerate(items):
            pos[cid] = (step * (k + 1), 50 + d * row_h)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {Wf} {Hf}" class="graph" id="frsvg">',
           '<defs><marker id="ako" viewBox="0 0 12 12" refX="12" refY="6" markerWidth="10" markerHeight="10" '
           'orient="auto"><path d="M0,0 L12,6 L0,12 z" fill="#fff" stroke="#333"/></marker>'
           '<marker id="slot" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           '<path d="M0,0 L10,5 L0,10 z" fill="#2a9d8f"/></marker></defs><g class="viewport">']
    for e in edges:
        if e["from"] not in pos or e["to"] not in pos:
            continue
        (x1, y1), (x2, y2) = pos[e["from"]], pos[e["to"]]
        if e["kind"] == "ako":
            out.append(f'<line x1="{x1:.0f}" y1="{y1 - 18:.0f}" x2="{x2:.0f}" y2="{y2 + 18:.0f}" stroke="#333" '
                       f'stroke-width="1.4" marker-end="url(#ako)"><title>AKO</title></line>')
        else:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2 - 40
            out.append(f'<path d="M{x1:.0f},{y1:.0f} Q{mx:.0f},{my:.0f} {x2:.0f},{y2:.0f}" fill="none" '
                       f'stroke="#2a9d8f" stroke-dasharray="6 4" stroke-width="1.3" marker-end="url(#slot)">'
                       f'<title>{esc(e["label"])}</title></path>'
                       f'<text class="elabel" x="{mx:.0f}" y="{my + 16:.0f}" fill="#2a9d8f">{esc(e["label"])}</text>')
    kind_fill = {"прототип-объект": "#e8f1fa", "прототип-роль": "#fde4cf", "ситуация": "#fff3b0",
                 "сценарий": "#d8f3dc", "экземпляр сценария": "#d8f3dc", "правило": "#f1e3f3"}
    for cid, (x, y) in pos.items():
        n = byid[cid]
        hl = cid in highlight
        sel = cid == selected
        lines = _wrap(n["label"], 16)
        w = max(110, 8 * max(len(l) for l in lines))
        h = 18 + 13 * len(lines)
        out.append(f'<a href="/frames?cls={esc(cid)}"><g><rect x="{x - w / 2:.0f}" y="{y - h / 2:.0f}" width="{w}" '
                   f'height="{h}" rx="6" fill="{kind_fill.get(n["kind"], "#eee")}" stroke="{"#d00000" if hl else ("#111" if sel else "#666")}" '
                   f'stroke-width="{4 if hl or sel else 1.2}"><title>{esc(n["label"])} — {esc(n["kind"])}</title></rect>')
        for k, line in enumerate(lines):
            out.append(f'<text class="flabel" x="{x:.0f}" y="{y - h / 2 + 16 + k * 13:.0f}">{esc(line)}</text>')
        out.append('</g></a>')
    out.append('</g></svg>')
    return "\n".join(out)


# ====================================================================== схема сценария
def scenario_svg(scenes, state=None, highlight_scene=None):
    """Основной путь — по горизонтали, ветви — выше и ниже; пройденные сцены зелёные,
    текущая — с оранжевой рамкой, невыбранные переходы — пунктиром."""
    state = state or {"current": None, "visited": set(), "taken": set()}
    main = [s for s in scenes if s["main"]]
    side = [s for s in scenes if not s["main"]]
    pos = {}
    step = 205
    for k, s in enumerate(main):
        pos[s["iri"]] = (110 + k * step, 250)
    incoming = {}
    for s in scenes:
        for t in s["transitions"]:
            incoming.setdefault(t["to"], []).append(s["iri"])
        if s["next"] is not None:
            incoming.setdefault(s["next"], []).append(s["iri"])
    up = True
    for s in side:
        src = [pos[x][0] for x in incoming.get(s["iri"], []) if x in pos]
        nxt = pos.get(s["next"], (src[0] + step if src else 600, 250))[0]
        x = ((min(src) if src else nxt - step) + nxt) / 2
        y = 90 if up else 410
        while any(abs(px - x) < 150 and abs(py - y) < 10 for px, py in pos.values()):
            y += 120 if not up else -0
            if up:
                x += 60
        pos[s["iri"]] = (x, y)
        up = not up
    Ws = max(x for x, _ in pos.values()) + 140
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {Ws:.0f} 520" class="graph" id="scsvg">',
           '<defs><marker id="arr" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto">'
           '<path d="M0,0 L10,5 L0,10 z" fill="#333"/></marker>'
           '<marker id="arrg" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="13" markerHeight="13" markerUnits="userSpaceOnUse" orient="auto">'
           '<path d="M0,0 L10,5 L0,10 z" fill="#2d6a4f"/></marker></defs>']
    bw, bh = 150, 62

    def edge(a, b, label, style):
        """style: taken — пройденный переход; untaken — невыбранная ветвь (пунктир);
        normal — переход, до которого исполнение ещё не дошло."""
        (x1, y1), (x2, y2) = pos[a], pos[b]
        if abs(y1 - y2) < 5:
            sx, sy, ex, ey = x1 + bw / 2, y1, x2 - bw / 2, y2
        elif y2 < y1:
            sx, sy, ex, ey = x1 + 20, y1 - bh / 2, x2 - 20, y2 + bh / 2
        else:
            sx, sy, ex, ey = x1 + 20, y1 + bh / 2, x2 - 20, y2 - bh / 2
        col = "#2d6a4f" if style == "taken" else "#555"
        dash = ' stroke-dasharray="6 5"' if style == "untaken" else ""
        mk = "arrg" if style == "taken" else "arr"
        res = (f'<line x1="{sx:.0f}" y1="{sy:.0f}" x2="{ex:.0f}" y2="{ey:.0f}" stroke="{col}" '
               f'stroke-width="{3.5 if style == "taken" else 1.4}"{dash} marker-end="url(#{mk})"/>')
        if label:
            lx, ly = sx + (ex - sx) * 0.35, sy + (ey - sy) * 0.35
            res += (f'<rect x="{lx - 14:.0f}" y="{ly - 16:.0f}" width="28" height="13" fill="#fbfcfd"/>'
                    f'<text class="elabel" x="{lx:.0f}" y="{ly - 6:.0f}" fill="{col}" font-weight="700">{esc(label)}</text>')
        return res

    taken = state.get("taken", set())
    visited = state.get("visited", set())
    for s in scenes:
        outs = [(t["to"], t["rule"]) for t in s["transitions"]]
        if s["next"] is not None:
            outs.append((s["next"], ""))
        for to, label in outs:
            if to not in pos:
                continue
            if (s["iri"], to) in taken:
                style = "taken"
            elif s["iri"] in visited:
                style = "untaken"
            else:
                style = "normal"
            out.append(edge(s["iri"], to, label, style))
    for s in scenes:
        x, y = pos[s["iri"]]
        visited = s["iri"] in state.get("visited", set())
        cur = s["iri"] == state.get("current")
        hl = highlight_scene is not None and str(s["iri"]).endswith(str(highlight_scene).split(":")[-1])
        fill = "#b7e4c7" if visited else ("#fff" if s["typical"] else "#ffe5e5")
        stroke = "#f77f00" if cur else ("#d00000" if hl else ("#c1121f" if not s["typical"] else "#444"))
        sw = 5 if cur or hl else 1.4
        dash = ' stroke-dasharray="7 4"' if not s["typical"] else ""
        out.append(f'<g><rect x="{x - bw / 2:.0f}" y="{y - bh / 2:.0f}" width="{bw}" height="{bh}" rx="8" fill="{fill}" '
                   f'stroke="{stroke}" stroke-width="{sw}"{dash}><title>{esc(s["pre"])} → {esc(s["action"])}</title></rect>')
        for k, line in enumerate(_wrap(s["label"], 20)):
            out.append(f'<text class="flabel" x="{x:.0f}" y="{y - 12 + k * 14:.0f}">{esc(line)}</text>')
        if s["rules"]:
            out.append(f'<text class="rlabel" x="{x:.0f}" y="{y + bh / 2 - 6:.0f}">Q: {esc(", ".join(s["rules"]))}</text>')
        out.append('</g>')
    out.append('</svg>')
    return "\n".join(out)


# ====================================================================== граф зависимостей правил
def rules_svg(names, edges, fired=(), highlight=()):
    preds = {n: set() for n in names}
    for a, b in edges:
        preds[b].add(a)
    level = {}

    def lvl(n, stack=()):
        if n in level:
            return level[n]
        if n in stack:
            return 0
        level[n] = 0 if not preds[n] else 1 + max(lvl(p, stack + (n,)) for p in preds[n])
        return level[n]
    for n in names:
        lvl(n)
    cols = {}
    for n in names:
        cols.setdefault(level[n], []).append(n)
    Wr = (max(cols) + 1) * 150 + 80 if cols else 400
    Hr = max(len(v) for v in cols.values()) * 54 + 80 if cols else 300
    pos = {}
    for c, items in cols.items():
        for k, n in enumerate(sorted(items)):
            pos[n] = (70 + c * 150, 50 + k * 54 + (Hr - 80 - len(items) * 54) / 2)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {Wr} {Hr}" class="graph" id="rlsvg">',
           '<defs><marker id="ra" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           '<path d="M0,0 L10,5 L0,10 z" fill="#6c757d"/></marker></defs>']
    for a, b in edges:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        out.append(f'<line x1="{x1 + 22:.0f}" y1="{y1:.0f}" x2="{x2 - 22:.0f}" y2="{y2:.0f}" stroke="#adb5bd" '
                   f'stroke-width="1.1" marker-end="url(#ra)"><title>{a} → {b}</title></line>')
    for n, (x, y) in pos.items():
        f = n in fired
        hl = n in highlight
        out.append(f'<a href="/rules?rule={n}"><circle cx="{x:.0f}" cy="{y:.0f}" r="21" fill="{"#95d5b2" if f else "#f8f9fa"}" '
                   f'stroke="{"#d00000" if hl else "#495057"}" stroke-width="{4 if hl else 1.3}"/>'
                   f'<text class="flabel" x="{x:.0f}" y="{y + 4:.0f}">{n}</text></a>')
    out.append('</svg>')
    return "\n".join(out)
