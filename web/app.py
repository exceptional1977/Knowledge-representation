"""
web/app.py — веб-интерфейс прототипа базы знаний (Flask).

Четыре экрана по заданию + общая панель «Что изменилось» справа:
  /semantic  — семантическая сеть: граф, фильтры, карточка вершины, добавление вершин
               и связей, поиск пути BFS/DFS, запросы SPARQL по образцу, проверка сети;
  /frames    — фреймы: дерево AKO, карточки класса и экземпляра (таблица слотов как
               в методичке), создание класса/экземпляра, изменение слота, удаление,
               граф сети фреймов, SPARQL;
  /scenarios — сценарии: схема, запуск, шаг, до конца, сброс, ответы на вопросы, журнал;
  /rules     — правила и вывод: таблица и карточка правила, добавление, прямой и
               обратный вывод, объяснение «как получено?», выгрузка трассы.
Все изменения идут через Rebuilder, который возвращает отчёт для панели изменений.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, redirect, render_template, request, url_for
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from kbapp import procedures as P                       # noqa: E402
from kbapp.frames import FrameModel                     # noqa: E402
from kbapp.mapping import Mapping                       # noqa: E402
from kbapp.rebuild import Rebuilder                     # noqa: E402
from kbapp.rules import Ctx, Engine                     # noqa: E402
from kbapp.scenario import ScenarioEngine               # noqa: E402
from kbapp.semnet import SemanticNet                    # noqa: E402
from kbapp.store import EX, SN, KnowledgeBase           # noqa: E402
from kbapp import viz                                   # noqa: E402


class App:
    """Все сервисы приложения в одном месте (одна база знаний на процесс)."""

    def __init__(self, base=BASE):
        self.kb = KnowledgeBase(base)
        self.build()

    def build(self):
        self.sem = SemanticNet(self.kb)
        self.frames = FrameModel(self.kb)
        self.engine = Engine(self.kb, self.frames)
        self.scen = ScenarioEngine(self.kb, self.frames, self.engine)
        self.mapping = Mapping(self.kb, self.sem, self.frames, self.engine)
        self.rb = Rebuilder(self.kb, self.sem, self.frames, self.engine, self.scen, self.mapping)
        self.last_report = None
        self.last_forward = self.engine.run_forward(label="вывод при запуске")
        self.bw_state = None


S = App()
app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"),
            static_folder=str(Path(__file__).parent / "static"))


def q(name):
    return S.kb.expand(name) if name else None


@app.context_processor
def inject():
    return {"report": S.last_report, "kb": S.kb, "author": S.kb.author, "qn": S.kb.qname, "lbl": S.kb.label}


@app.route("/")
def index():
    return redirect(url_for("semantic"))


@app.post("/reset")
def reset():
    """Сброс к исходной базе знаний (перед демонстрацией)."""
    S.kb.reset_to_seed()
    lp = S.kb.cur_dir / "layout.json"
    if lp.exists():
        lp.unlink()
    S.build()
    S.last_report = {"action": "База знаний возвращена к исходному состоянию", "ok": True,
                     "steps": {"7. Сохранение": ["Файлы kb/current/*.ttl перезаписаны из kb/seed"]},
                     "added": [], "removed": [], "n_added": 0, "n_removed": 0, "highlight": {}, "suggest": None}
    return redirect(request.referrer or url_for("semantic"))


@app.post("/author")
def set_author():
    S.kb.author = request.form.get("author", S.kb.author).strip() or S.kb.author
    return redirect(request.referrer or url_for("semantic"))


# ============================================================ Семантическая сеть
@app.route("/semantic")
def semantic():
    sem = S.sem
    groups = sem.groups()
    rels = sem.relations()
    groups_on = request.args.getlist("g") or None
    rels_on = request.args.getlist("r") or None
    node = request.args.get("node")
    hl = list((S.last_report or {}).get("highlight", {}).get("sem", []) or [])
    search = None
    if request.args.get("from") and request.args.get("to"):
        rf = {S.kb.expand(x) for x in rels_on} if rels_on else None
        search = sem.search(q(request.args["from"]), q(request.args["to"]),
                            request.args.get("method", "bfs"), rf)
        both = [sem.search(q(request.args["from"]), q(request.args["to"]), m, rf) for m in ("bfs", "dfs")]
        search["compare"] = [{"method": b["method"], "visited": b["visited"], "len": len(b["path"])} for b in both]
    svg = viz.semantic_svg(sem, S.kb.cur_dir, groups_on, rels_on, highlight=hl + ([node] if node else []),
                           path_ids=search["path_ids"] if search else (),
                           order_ids=search["order_ids"] if search else (), selected=node)
    card = sem.card(q(node)) if node else None
    tpl = request.args.get("tpl")
    query_text = request.args.get("sparql") or (sem.TEMPLATES.get(tpl) if tpl else None) or \
        list(sem.TEMPLATES.values())[0]
    qres = None
    if request.args.get("run_query"):
        try:
            qres = sem.sparql(query_text)
        except Exception as e:                                     # noqa: BLE001
            qres = (["Ошибка"], [[str(e)]])
    check = sem.check() if request.args.get("check") else None
    return render_template("semantic.html", svg=svg, groups=groups, rels=sorted(rels.values(), key=lambda r: r["group"]),
                           groups_on=groups_on, rels_on=rels_on, card=card, node=node,
                           concepts=sem.concepts(), search=search, templates=sem.TEMPLATES, query_text=query_text,
                           qres=qres, check=check, stats=sem.stats())


@app.post("/semantic/add_vertex")
def sem_add_vertex():
    f = request.form
    edges = []
    for k in range(1, 4):
        rel, other = f.get(f"rel{k}"), f.get(f"other{k}")
        if rel and other:
            edges.append((q(rel), q(other), f.get(f"dir{k}", "out")))
    attrs = [(f.get("attr_name"), f.get("attr_value"))] if f.get("attr_name") else []
    S.last_report = S.rb.add_vertex(f["label"].strip(), q(f["group"]), f.get("definition", ""), attrs, edges)
    first = (S.last_report.get("highlight") or {}).get("sem") or [None]
    return redirect(url_for("semantic", node=first[0]) if S.last_report["ok"] else url_for("semantic"))


@app.post("/semantic/add_edge")
def sem_add_edge():
    f = request.form
    S.last_report = S.rb.add_edge(q(f["s"]), q(f["rel"]), q(f["o"]))
    return redirect(url_for("semantic", node=f["s"]))


@app.post("/semantic/create_frame")
def sem_create_frame():
    f = request.form
    v = q(f["vertex"])
    S.last_report = S.rb.add_class(S.kb.label(v), q(f["parent"]), (), v)
    hl = (S.last_report.get("highlight") or {}).get("frames") or [None]
    return redirect(url_for("frames", cls=hl[0]))


# ============================================================ Фреймы
@app.route("/frames")
def frames():
    fm = S.frames
    cls = request.args.get("cls")
    inst = request.args.get("inst")
    hl = (S.last_report or {}).get("highlight", {}).get("frames", []) or []
    ccard = fm.class_card(q(cls)) if cls else None
    icard = fm.instance_card(q(inst), S.engine.derivations) if inst else None
    if icard:
        for r in icard["rows"]:
            r["vals"] = [S.kb.label(v) if isinstance(v, URIRef) else v for v in r["vals"]]
    if ccard:
        for r in ccard["rows"]:
            r["value"] = S.kb.label(r["value"]) if isinstance(r["value"], URIRef) else r["value"]
    form_slots = []
    if cls:
        for s in fm.slots_of_class(q(cls), include_derived=False):
            if s["iri"] in (EX.createdAt, EX.modifiedAt, EX.modifiedBy):
                continue
            req = any(sh["path"] == s["label"] and "min 1" in sh["rule"] for sh in ccard["shapes"])
            default, dcls = fm.class_default(q(cls), s["iri"])
            opts = []
            if s["object"] and s["range"] is not None:
                opts = [{"id": S.kb.qname(x), "label": S.kb.label(x)} for x in fm.instances(s["range"])]
            form_slots.append({"id": s["id"], "label": s["label"], "type": s["range_label"], "ptr": s["inheritance"],
                               "required": req, "default": S.kb.label(default) if default is not None else "",
                               "from": S.kb.label(dcls) if dcls is not None else "", "options": opts,
                               "object": s["object"]})
    tpl = request.args.get("tpl")
    query_text = request.args.get("sparql") or (fm.TEMPLATES.get(tpl) if tpl else None) or list(fm.TEMPLATES.values())[0]
    qres = None
    if request.args.get("run_query"):
        try:
            qres = fm.sparql(query_text)
        except Exception as e:                                     # noqa: BLE001
            qres = (["Ошибка"], [[str(e)]])
    shacl = fm.validate() if request.args.get("shacl") else None
    return render_template("frames.html", tree=fm.tree(), ccard=ccard, icard=icard, cls=cls, inst=inst,
                           svg=viz.frames_svg(fm, highlight=hl, selected=cls),
                           classes=[{"id": S.kb.qname(c), "label": S.kb.label(c)} for c in fm.frame_classes()],
                           form_slots=form_slots, templates=fm.TEMPLATES, query_text=query_text, qres=qres,
                           shacl=shacl, depth=fm.depth(), n_classes=len(fm.frame_classes()),
                           sem_concepts=S.sem.concepts())


@app.post("/frames/add_class")
def fr_add_class():
    f = request.form
    children = [q(x) for x in request.form.getlist("children")]
    sem_node = q(f.get("sem")) if f.get("sem") else None
    defaults = []
    if f.get("def_slot") and f.get("def_value"):
        defaults.append((q(f["def_slot"]), f["def_value"]))
    S.last_report = S.rb.add_class(f["label"].strip(), q(f["parent"]), children, sem_node, defaults)
    hl = (S.last_report.get("highlight") or {}).get("frames") or [None]
    return redirect(url_for("frames", cls=hl[0] if S.last_report["ok"] else f["parent"]))


@app.post("/frames/add_instance")
def fr_add_instance():
    f = request.form
    cls = q(f["cls"])
    values = {}
    for key, val in f.items():
        if key.startswith("slot__") and val.strip():
            values[q(key[6:])] = val
    S.last_report = S.rb.add_instance(cls, f["label"].strip(), values)
    hl = (S.last_report.get("highlight") or {}).get("frames") or []
    if S.last_report["ok"] and hl:
        return redirect(url_for("frames", cls=f["cls"], inst=hl[0]))
    return redirect(url_for("frames", cls=f["cls"]))


@app.post("/frames/set_slot")
def fr_set_slot():
    f = request.form
    S.last_report = S.rb.set_slot(q(f["inst"]), q(f["slot"]), f["value"])
    return redirect(url_for("frames", cls=f.get("cls"), inst=f["inst"]))


@app.post("/frames/delete")
def fr_delete():
    f = request.form
    S.last_report = S.rb.delete(q(f["node"]), f.get("mode", "check"))
    if f.get("mode") == "cascade" and S.last_report["ok"]:
        return redirect(url_for("frames", cls=f.get("cls")))
    return redirect(url_for("frames", cls=f.get("cls"), inst=f["node"] if f.get("mode") != "cascade" else None))


@app.post("/frames/if_needed")
def fr_if_needed():
    """Обращение к пустому слоту с демоном IF-NEEDED: вычислить значение процедурой."""
    f = request.form
    inst, slot = q(f["inst"]), q(f["slot"])
    name = S.frames.prop_info(slot)["if_needed"]
    ctx = Ctx(S.kb, S.frames, S.engine)
    snap = S.kb.snapshot(["data", "inferred"])
    val, why = P.IF_NEEDED[name](ctx, inst)
    added, _ = S.kb.diff(snap)
    S.kb.save(["data", "inferred"])
    S.kb.journal(f"IF-NEEDED {name}", {"inst": S.kb.qname(inst), "value": str(val)})
    S.last_report = {"action": f"IF-NEEDED: «{S.kb.label(inst)}».«{S.kb.label(slot)}»", "ok": True,
                     "steps": {"4. Демоны": [f"IF-NEEDED «{name}» вычислил значение {val} ({why}) и сохранил его в слот"],
                               "7. Сохранение": ["Значение закэшировано в графе data"]},
                     "added": [f"[{g}] {S.kb.fmt_triple(t)}" for g, t in added], "removed": [], "n_added": len(added),
                     "n_removed": 0, "highlight": {"frames": [f["inst"]]}, "suggest": None}
    return redirect(url_for("frames", cls=f.get("cls"), inst=f["inst"]))


# ============================================================ Сценарии
@app.route("/scenarios")
def scenarios():
    sc_list = S.scen.scenarios()
    sc = q(request.args.get("sc") or sc_list[0]["id"])
    run_id = request.args.get("run")
    run = q(run_id) if run_id else S.scen.active_run()
    scenes = S.scen.scenes(sc)
    state = S.scen.state(run) if run is not None and (run, RDF.type, EX.ScenarioRun) in S.kb.g["runs"] else None
    hl_scene = (S.last_report or {}).get("highlight", {}).get("scene")
    return render_template("scenarios.html", sc_list=sc_list, sc=S.kb.qname(sc), scenes=scenes,
                           svg=viz.scenario_svg(scenes, state, hl_scene), run=S.kb.qname(run) if state else None,
                           runs=S.scen.runs(), steps=S.scen.steps(run) if state else [],
                           slots=S.scen.run_slots(run) if state else [],
                           question=S.scen.pending_question(run) if state else None, state=state,
                           cases=[{"id": S.kb.qname(c), "label": S.kb.label(c)} for c in S.frames.instances(EX.ProcessCase)
                                  if (c, RDF.type, None) in S.kb.g["data"]])


@app.post("/scenarios/start")
def sc_start():
    f = request.form
    case = q(f["case"]) if f.get("case") else None
    run = S.scen.start(q(f["sc"]), case)
    S.kb.journal("Сценарий: запуск", {"run": S.kb.qname(run)})
    return redirect(url_for("scenarios", sc=f["sc"], run=S.kb.qname(run)))


@app.post("/scenarios/step")
def sc_step():
    f = request.form
    run = q(f["run"])
    res = S.scen.step(run)
    if res.get("scene"):
        S.last_report = {"action": f"Сценарий: шаг «{res['scene']}»", "ok": True,
                         "steps": {"6. Сценарий": [f"Предусловие: {res['pre']}",
                                                   "Сработали правила: " + (", ".join(res["fired"]) or "—"),
                                                   *[f"Демон/процедура: {d}" for d in res["demons"]],
                                                   *res.get("blocked", []),
                                                   f"Переход → «{res['next']}»" + (f" по правилу {res['by_rule']}" if res["by_rule"] else " по слоту «Следующая сцена»")],
                                   "5. Повторный вывод": [f"Вывод в сфере сцен: {res['inference']['cycles']} циклов"]},
                         "added": [], "removed": [], "n_added": 0, "n_removed": 0,
                         "highlight": {"scene": None}, "suggest": None}
    return redirect(url_for("scenarios", sc=f["sc"], run=f["run"]))


@app.post("/scenarios/answer")
def sc_answer():
    f = request.form
    S.scen.answer(q(f["run"]), f["slot"], f["value"])
    return redirect(url_for("scenarios", sc=f["sc"], run=f["run"]))


@app.post("/scenarios/run_all")
def sc_run_all():
    f = request.form
    run = q(f["run"])
    log, question = S.scen.run_to_end(run)
    return redirect(url_for("scenarios", sc=f["sc"], run=f["run"]))


@app.post("/scenarios/reset")
def sc_reset():
    f = request.form
    S.scen.reset(q(f["run"]))
    return redirect(url_for("scenarios", sc=f["sc"]))


@app.route("/scenarios/export")
def sc_export():
    run = q(request.args["run"])
    lines = [f"# Журнал исполнения: {S.kb.label(run)}", ""]
    for s in S.scen.run_slots(run):
        lines.append(f"- {s['slot']}: {s['value']}")
    lines.append("")
    for st in S.scen.steps(run):
        lines.append(f"## Шаг {st['n']}. {st['scene']}")
        lines.append(f"- предусловие: {st['pre']}")
        lines.append(f"- действие: {st['action']}")
        for k, v in st["slots"].items():
            lines.append(f"- слот «{k}» = {v}")
        lines.append(f"- сработавшие правила: {st['rules'] or '—'}")
        if st["demons"]:
            lines.append(f"- демоны и процедуры: {st['demons']}")
        if st["conflict"]:
            lines.append(f"- разрешение конфликта: {st['conflict']}")
        lines.append(f"- переход → {st['to']}" + (f" (правило {st['by_rule']})" if st["by_rule"] else ""))
        lines.append("")
    text = "\n".join(lines)
    (S.kb.out_dir / f"run_{S.kb.qname(run).split(':')[1]}.md").write_text(text, encoding="utf-8")
    return Response(text, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=run_{S.kb.qname(run).split(':')[1]}.md"})


# ============================================================ Правила и вывод
@app.route("/rules")
def rules():
    eng = S.engine
    rname = request.args.get("rule")
    rule = eng.rule(rname) if rname else None
    names, edges = eng.dependency_graph()
    fired = {c["chosen"] for c in (S.engine.last_result or {}).get("trace", []) if c.get("chosen")}
    expl = None
    if request.args.get("explain"):
        s, p, o = request.args["explain"].split("|")
        o_term = S.kb.expand(o) if o.startswith(("ex:", "sn:", "http")) else None
        if o_term is None:
            cand = [t for t in eng.derivations if S.kb.qname(t[0]) == s and S.kb.qname(t[1]) == p and str(t[2]) == o]
            t = cand[0] if cand else None
        else:
            t = (S.kb.expand(s), S.kb.expand(p), o_term)
        if t is not None and t in eng.derivations:
            expl = eng.explain(t)
    return render_template("rules.html", rules=eng.rules(), rule=rule, fwd=S.engine.last_result,
                           results=eng.results(), recs=eng.recommendations(), chains=eng.chains(),
                           svg=viz.rules_svg(names, edges, fired, [rname] if rname else []), expl=expl,
                           bw=S.bw_state, cases=[{"id": S.kb.qname(c), "label": S.kb.label(c)}
                                                  for c in S.frames.instances(EX.ProcessCase)],
                           statuses=[{"id": S.kb.qname(x), "label": S.kb.label(x)}
                                     for x in S.kb.g["frames"].subjects(RDF.type, EX.Status)],
                           sources=[c for c in S.sem.concepts() if c["group"] == SN.G_Rule],
                           scopes=[{"id": S.kb.qname(x), "label": S.kb.label(x)} for x in
                                   list(S.kb.g["scenarios"].subjects(RDF.type, EX.Scene)) + S.frames.frame_classes()],
                           posts=sorted(P.POST_ACTIONS), strategy=str(S.kb.g["rules"].value(EX.ConflictStrategy, RDFS.comment)))


@app.post("/rules/forward")
def r_forward():
    S.engine.run_forward(label="прямой вывод (запуск пользователем)")
    S.last_report = {"action": "Прямой вывод запущен", "ok": True,
                     "steps": {"5. Повторный вывод": [f"{S.engine.last_result['cycles']} циклов, выведено {S.engine.last_result['n_derived']} триплетов"]
                               + [f"+ {x}" for x in S.engine.last_result["appeared"]] + [f"− {x}" for x in S.engine.last_result["disappeared"]]},
                     "added": [], "removed": [], "n_added": 0, "n_removed": 0, "highlight": {}, "suggest": None}
    return redirect(url_for("rules"))


@app.route("/rules/trace")
def r_trace():
    """Выгрузить трассу последнего прямого вывода в файл (для пояснительной записки)."""
    fwd = S.engine.last_result
    lines = [f"# Трасса: {fwd['label']} ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"Стратегия разрешения конфликта: {S.kb.g['rules'].value(EX.ConflictStrategy, RDFS.comment)}", ""]
    for c in fwd["trace"]:
        cs = ", ".join(f"{a['rule']}(p={a['priority']},s={a['spec']})" for a in c["conflict"])
        lines.append(f"Цикл {c['cycle']}. Конфликтный набор: {cs or '—'}")
        if c.get("chosen"):
            b = ", ".join(f"{k}={v}" for k, v in c["binding"].items())
            lines.append(f"  выбрано {c['chosen']} [{c['reason']}] {{{b}}}")
            for t in c["added"]:
                lines.append(f"  + {t}")
            if c["post"]:
                lines.append(f"  {c['post']}")
        for bl in c.get("blocked", []):
            lines.append(f"  заблокировано {bl['rule']}: {bl['why']}")
    text = "\n".join(lines)
    (S.kb.out_dir / "forward_trace.md").write_text(text, encoding="utf-8")
    return Response(text, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=forward_trace.md"})


@app.post("/rules/backward")
def r_backward():
    f = request.form
    if f.get("new_case"):
        node = EX["Hyp_" + f["new_case"].strip().replace(" ", "_")]
        hyp = {"triples": [(node, RDF.type, EX.LoanApplicationCase), (node, RDFS.label, Literal(f"Гипотетический кейс {f['new_case']}")),
                           (node, EX.caseId, Literal(f["new_case"])), (node, EX.inLog, EX.Log_BPIC2017),
                           (node, EX.slaNorm, EX.Norm_SLA_Loan)]}
        S.kb.tmp_labels = {node: f"Гипотетический кейс {f['new_case']}"}
    else:
        node, hyp = q(f["case"]), {"triples": []}
    goal = (node, q(f["slot"]), q(f["value"]))
    S.bw_state = {"goal": goal, "answers": {}, "hyp": hyp}
    return _bw_run()


@app.post("/rules/bw_answer")
def r_bw_answer():
    f = request.form
    st = S.bw_state
    s, p = q(f["subject"]), q(f["slot"])
    st["answers"][(s, p)] = S.frames.cast(p, f["value"])
    return _bw_run()


def _bw_run():
    st = S.bw_state
    res = S.engine.backward(st["goal"], st["answers"], st["hyp"])
    res["answers"] = [f"{S.kb.label(s)}.{S.kb.label(p)} = {v}" for (s, p), v in st["answers"].items()]
    st["result"] = res
    with open(S.kb.out_dir / "backward_trace.md", "w", encoding="utf-8") as fh:
        fh.write(f"# Обратный вывод: {res['goal']}\n\nСтатус: {res['status']}\n\n")
        for a in res["answers"]:
            fh.write(f"- ответ пользователя: {a}\n")
        for s in res["steps"]:
            fh.write("  " * s["depth"] + f"- {s['goal']} → {s['result']}\n")
        if res.get("explanation"):
            fh.write("\n## Объяснение\n\n" + res["explanation"] + "\n")
    return redirect(url_for("rules", _anchor="backward"))


@app.post("/rules/add")
def r_add():
    f = request.form
    S.last_report = S.rb.add_rule(f["name"].strip(), q(f["scope"]) if f.get("scope") else None, int(f.get("priority", 10)),
                                  q(f["source"]) if f.get("source") else None, f.get("pre", ""), f.get("cond", ""),
                                  f.get("concl", ""), f.get("post", ""), f["core"])
    return redirect(url_for("rules", rule=f["name"].strip() if S.last_report["ok"] else None))


# ============================================================ соответствие и журнал
@app.route("/mapping")
def mapping():
    return render_template("mapping.html", table=S.mapping.table(), disc=S.mapping.discrepancies())


@app.route("/journal")
def journal():
    return render_template("journal.html", entries=S.kb.journal_entries(100))


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Прототип базы знаний модуля процессной аналитики")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print(f"Откройте в браузере: http://{a.host}:{a.port}/")
    app.run(host=a.host, port=a.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
