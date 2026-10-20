"""
tests/kb_report.py — сводка по базе знаний для пояснительной записки.

Работает напрямую с модулями kbapp (без веб-интерфейса) на ИСХОДНОЙ базе kb/seed
во временной копии и выгружает в out/report.json:
  объём сети и словарь отношений, результаты пяти видов запросов, сравнение
  BFS/DFS на трёх парах понятий, объём фреймовой модели, проверку SHACL, таблицу
  правил, цепочки вывода, трассу прямого вывода и журнал исполнения сценария.
Запуск: python tests/kb_report.py
"""
import json
from rdflib import URIRef
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from kbapp.frames import FrameModel          # noqa: E402
from kbapp.mapping import Mapping            # noqa: E402
from kbapp.rules import Engine               # noqa: E402
from kbapp.scenario import ScenarioEngine    # noqa: E402
from kbapp.semnet import SemanticNet         # noqa: E402
from kbapp.store import EX, SN, KnowledgeBase  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(BASE / "kb" / "seed", tmp / "kb" / "seed")
    kb = KnowledgeBase(tmp)
    sem, fm = SemanticNet(kb), FrameModel(kb)
    eng = Engine(kb, fm)
    rep = {"sem_stats": sem.stats(), "sem_check": sem.check()}
    rels = sem.relations()
    used = {}
    for s, p, o in sem.edges():
        used[p] = used.get(p, 0) + 1
    rep["relations"] = [{"label": r["label"], "inverse": r["inverse"], "group": r["group"],
                         "domain": ", ".join(sorted(kb.label(x) for x in r["domain"])),
                         "range": ", ".join(sorted(kb.label(x) for x in r["range"])),
                         "same_group": r["same_group"], "transitive": r["transitive"], "n": used.get(p, 0)}
                        for p, r in sorted(rels.items(), key=lambda x: (x[1]["group"], x[1]["label"]))]
    rep["groups"] = [{"label": g["label"], "concepts": [c["label"] for c in sem.concepts() if c["group"] == g["iri"]]}
                     for g in sem.groups()]
    rep["card_heterognn"] = sem.card(SN.HeteroGNN)
    pairs = [(SN.DataEngineer, SN.GoalSLARisk), (SN.HeteroGNN, SN.ProcessOwner), (SN.GPU01, SN.Recommendation)]
    rep["search"] = []
    for a, b in pairs:
        row = {"from": kb.label(a), "to": kb.label(b)}
        for m in ("bfs", "dfs"):
            r = sem.search(a, b, m)
            row[m] = {"visited": r["visited"], "len": len(r["path"]), "order": r["order"],
                      "path": " → ".join([r["path"][0]["from"]] + [f"[{e['rel']}] {e['to']}" for e in r["path"]]) if r["path"] else ""}
        rep["search"].append(row)
    rep["sparql"] = {}
    for name, q in sem.TEMPLATES.items():
        rep["sparql"][name] = {"query": q, "result": sem.sparql(q)}
    # фильтр: процессы + «предшествует»
    rep["filter"] = [(kb.label(s), kb.label(o)) for s, p, o in sem.edges({SN.precedes})]
    rep["frames"] = {"classes": len(fm.frame_classes()), "depth": fm.depth(),
                     "kinds": {}, "instances": len(set(kb.g["data"].subjects()))}
    for c in fm.frame_classes():
        k = str(fm.fg.value(c, EX.frameKind))
        rep["frames"]["kinds"].setdefault(k, []).append(kb.label(c))
    rep["frames"]["instances_list"] = [(kb.label(i), kb.label(fm.type_of(i))) for i in
                                       sorted(set(kb.g["data"].subjects(None, None)), key=str)
                                       if fm.type_of(i) is not None]
    rep["shacl"] = fm.validate()
    rep["class_cards"] = {kb.qname(c): fm.class_card(c) for c in (EX.GNNModel, EX.LoanApplicationCase, EX.ProcessOwnerRole,
                                                                   EX.InsufficientData)}
    for cc in rep["class_cards"].values():
        for r in cc["rows"]:
            r["value"] = kb.label(r["value"]) if isinstance(r["value"], URIRef) else str(r["value"])
    ic = fm.instance_card(EX.GNN_v1)
    for r in ic["rows"]:
        r["vals"] = [kb.label(v) if isinstance(v, URIRef) else str(v) for v in r["vals"]]
    rep["inst_card"] = ic
    rep["sparql_frames"] = {n: fm.sparql(q) for n, q in fm.TEMPLATES.items()}
    res = eng.run_forward(label="прямой вывод на исходной базе")
    rep["forward"] = {"cycles": res["cycles"], "n": res["n_derived"], "trace": res["trace"]}
    rep["rules"] = [{"i": r.name, "Q": kb.label(r.scope), "P": r.pre, "A": r.cond, "B": r.concl, "N": r.post,
                     "prio": r.priority, "spec": r.specificity, "src": kb.label(r.source), "core": r.core}
                    for r in eng.rules()]
    rep["chains"] = eng.chains()
    rep["deps"] = eng.dependency_graph()
    rep["recs"] = eng.recommendations()
    rep["explain"] = eng.explain_text(eng.explain((EX.Case_A1001, EX.decision, EX.EscalateToOwner)))
    rep["mapping"] = Mapping(kb, sem, fm, eng).table()
    rep["discrepancies"] = Mapping(kb, sem, fm, eng).discrepancies()
    sc = ScenarioEngine(kb, fm, eng)
    rep["scenes"] = [{k: (kb.label(v) if k in ("next",) else v) for k, v in s.items()
                      if k in ("label", "pre", "action", "proc", "result", "next_label", "rules")}
                     | {"transitions": [f"{t['rule']} → {t['to_label']}" for t in s["transitions"]]}
                     for s in sc.scenes(EX.Sc_Monitoring)]
    run = sc.start(EX.Sc_Monitoring)
    sc.run_to_end(run, {"ex:runCase": "ex:Case_A1001", "ex:ownerConfirmed": "да"})
    rep["run"] = {"steps": sc.steps(run), "slots": sc.run_slots(run)}
    run3 = sc.start(EX.Sc_Monitoring)
    sc.run_to_end(run3, {"ex:runCase": "ex:Case_A1003"})
    rep["run_atypical"] = {"steps": sc.steps(run3)}
    out = BASE / "out"
    out.mkdir(exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("out/report.json готов:", rep["sem_stats"], "правил:", len(rep["rules"]))


if __name__ == "__main__":
    main()
