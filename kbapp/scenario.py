"""
scenario.py — исполнение фреймов-сценариев (раздел 5 задания).

Сценарий, его сцены и переходы лежат в графе scenarios как экземпляры классов
ex:Scenario и ex:Scene, связанные свойствами ex:nextScene и ex:transition.
При запуске создаётся фрейм-экземпляр сценария ex:Run_N (класс ex:ScenarioRun)
и заполняется по ходу исполнения, как «Посещение „Вкусной еды“» в методичке.

Один шаг исполнения:
  1) если у сцены есть слот из диалога (ex:askSlot), а значения нет — задаётся вопрос;
  2) проверяется предусловие сцены (SPARQL ASK с подставленным кейсом);
  3) выполняется действие — присоединённая процедура ex:actionProc;
  4) запускается прямой вывод на правилах, сфера Q которых — пройденные сцены;
  5) выбирается следующая сцена: если сработало правило одного из переходов
     ex:transition — переход по нему (правило указывается в журнале), иначе по слоту
     «Следующая сцена»; результаты вывода записываются в слоты экземпляра сценария.
"""
from __future__ import annotations

import json

from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from . import procedures as P
from .rules import Ctx
from .store import EX, SPARQL_PREFIXES


class ScenarioEngine:
    def __init__(self, kb, frames, engine):
        self.kb, self.frames, self.engine = kb, frames, engine
        self._last = {}         # граф выведенного на последнем шаге каждого исполнения
        self._last_derivs = {}  # и какими правилами это выведено

    @property
    def sg(self):
        return self.kb.g["scenarios"]

    @property
    def rg(self):
        return self.kb.g["runs"]

    # ------------------------------------------------------------ описание сценария
    def scenarios(self):
        return [{"id": self.kb.qname(s), "label": self.kb.label(s)} for s in self.sg.subjects(RDF.type, EX.Scenario)]

    def scenes(self, sc):
        res = []
        for s in self.sg.objects(sc, EX.hasScene):
            trans = []
            for tr in self.sg.objects(s, EX.transition):
                r, to = self.sg.value(tr, EX.byRule), self.sg.value(tr, EX.toScene)
                trans.append({"rule": self.kb.label(r), "rule_iri": r, "to": to, "to_label": self.kb.label(to)})
            nxt = self.sg.value(s, EX.nextScene)
            res.append({"iri": s, "id": self.kb.qname(s), "label": self.kb.label(s),
                        "order": int(self.sg.value(s, EX.order) or 0),
                        "main": bool(self.sg.value(s, EX.mainPath)),
                        "typical": self.sg.value(s, EX.typical) is None or bool(self.sg.value(s, EX.typical)),
                        "pre": str(self.sg.value(s, EX.precondition) or ""),
                        "action": str(self.sg.value(s, EX.actionText) or ""),
                        "proc": str(self.sg.value(s, EX.actionProc) or ""),
                        "result": str(self.sg.value(s, EX.resultText) or ""),
                        "next": nxt, "next_label": self.kb.label(nxt) if nxt is not None else "конец",
                        "transitions": sorted(trans, key=lambda t: t["rule"]),
                        "ask": self.sg.value(s, EX.askSlot), "ask_text": str(self.sg.value(s, EX.askText) or ""),
                        "rules": sorted(r.name for r in self.engine.rules() if r.scope == s)})
        return sorted(res, key=lambda x: x["order"])

    # ------------------------------------------------------------ экземпляры исполнения
    def runs(self):
        res = []
        for r in self.rg.subjects(RDF.type, EX.ScenarioRun):
            res.append({"id": self.kb.qname(r), "label": self.kb.label(r),
                        "status": str(self.rg.value(r, EX.runStatus) or "")})
        return sorted(res, key=lambda x: x["id"], reverse=True)

    def active_run(self):
        for r in self.runs():
            if r["status"] == "исполняется":
                return self.kb.expand(r["id"])
        return None

    def start(self, sc, case=None):
        n = len(list(self.rg.subjects(RDF.type, EX.ScenarioRun))) + 1
        run = EX[f"Run_{n:03d}"]
        self.rg.add((run, RDF.type, EX.ScenarioRun))
        self.rg.add((run, RDFS.label, Literal(f"Исполнение №{n}: {self.kb.label(sc)}", lang="ru")))
        self.rg.add((run, EX.ofScenario, sc))
        self.rg.add((run, EX.currentScene, self.sg.value(sc, EX.firstScene)))
        self.rg.add((run, EX.runStatus, Literal("исполняется")))
        self.rg.add((run, EX.startedAt, self.kb.now()))
        self.rg.add((run, EX.createdAt, self.kb.now()))
        self.rg.add((run, EX.modifiedBy, Literal(self.kb.author)))
        if case is not None:
            self.rg.add((run, EX.runCase, case))
        self.kb.save(["runs"])
        return run

    def reset(self, run):
        for s in list(self.rg.objects(run, EX.hasStep)):
            self.rg.remove((s, None, None))
        self.rg.remove((run, None, None))
        self.kb.save(["runs"])

    def steps(self, run):
        res = []
        for st in self.rg.objects(run, EX.hasStep):
            res.append({"n": int(self.rg.value(st, EX.stepNo)),
                        "scene": self.kb.label(self.rg.value(st, EX.scene)),
                        "scene_id": self.kb.qname(self.rg.value(st, EX.scene)),
                        "pre": str(self.rg.value(st, EX.preResult) or ""),
                        "action": str(self.rg.value(st, EX.actionResult) or ""),
                        "slots": json.loads(str(self.rg.value(st, EX.filledSlots) or "{}")),
                        "rules": str(self.rg.value(st, EX.firedRules) or ""),
                        "demons": str(self.rg.value(st, EX.demonsFired) or ""),
                        "by_rule": str(self.rg.value(st, EX.transitionRule) or ""),
                        "conflict": str(self.rg.value(st, EX.conflictNote) or ""),
                        "to": self.kb.label(self.rg.value(st, EX.toScene)) if self.rg.value(st, EX.toScene) else "конец"})
        return sorted(res, key=lambda x: x["n"])

    def run_slots(self, run):
        rows = []
        for p, o in self.rg.predicate_objects(run):
            if p in (EX.hasStep, RDF.type):
                continue
            rows.append({"slot": self.kb.label(p), "value": self.kb.label(o) if isinstance(o, URIRef) else str(o)})
        return sorted(rows, key=lambda r: r["slot"])

    def visited(self, run):
        return set(self.rg.objects(run, EX.visitedScene))

    def state(self, run):
        cur = self.rg.value(run, EX.currentScene)
        taken = {(self.rg.value(st, EX.scene), self.rg.value(st, EX.toScene)) for st in self.rg.objects(run, EX.hasStep)}
        return {"current": cur, "visited": self.visited(run), "taken": taken,
                "status": str(self.rg.value(run, EX.runStatus) or ""), "case": self.rg.value(run, EX.runCase)}

    # ------------------------------------------------------------ один шаг
    def pending_question(self, run):
        scene = self.rg.value(run, EX.currentScene)
        if scene is None:
            return None
        slot = self.sg.value(scene, EX.askSlot)
        if slot is not None and self.rg.value(run, slot) is None:
            return {"slot": self.kb.qname(slot), "text": str(self.sg.value(scene, EX.askText)),
                    "scene": self.kb.label(scene),
                    "options": self._options(slot)}
        return None

    def _options(self, slot):
        if slot == EX.runCase:
            return [{"id": self.kb.qname(c), "label": self.kb.label(c)}
                    for c in self.frames.instances(EX.ProcessCase) if (c, RDF.type, None) in self.kb.g["data"]]
        return [{"id": "да", "label": "да"}, {"id": "нет", "label": "нет"}]

    def answer(self, run, slot, value):
        slot = self.kb.expand(slot)
        val = self.kb.expand(value) if slot == EX.runCase else Literal(value)
        self.rg.set((run, slot, val))
        self.kb.save(["runs"])

    def scopes_for(self, run, scene):
        return self.visited(run) | {scene}

    def step(self, run):
        """Выполнить текущую сцену и перейти к следующей. Возвращает описание шага."""
        with self.kb.lock:
            q = self.pending_question(run)
            if q:
                return {"question": q}
            scene = self.rg.value(run, EX.currentScene)
            if scene is None:
                return {"done": True}
            case = self.rg.value(run, EX.runCase)
            ctx = Ctx(self.kb, self.frames, self.engine)
            # (2) предусловие
            ask = self.sg.value(scene, EX.preconditionAsk)
            pre = str(self.sg.value(scene, EX.precondition) or "")
            if ask is not None and case is not None:
                wm = self.engine.base_wm()
                prev = self._last.get(run)
                if prev is None:
                    _, _, prev = self.engine.evaluate_scoped(self.visited(run) or {scene})
                for t in prev:
                    wm.add(t)
                q = SPARQL_PREFIXES + str(ask).replace("?case", case.n3())
                ok = bool(wm.query(q).askAnswer)
                pre_res = f"{pre}: {'выполнено' if ok else 'НЕ выполнено'}"
            else:
                pre_res = f"{pre}: выполнено"
            # (3) действие — присоединённая процедура сцены
            proc = str(self.sg.value(scene, EX.actionProc) or "")
            filled = {}
            if proc in P.SCENE_ACTIONS and case is not None:
                filled.update(P.SCENE_ACTIONS[proc](ctx, run, case))
            # (4) прямой вывод с правилами пройденных сцен (сфера применения Q)
            prev_rules = {(t, i["rule"]) for t, i in (self._last_derivs.get(run) or {}).items()}
            res, derivs, ginf = self.engine.evaluate_scoped(self.scopes_for(run, scene))
            nodes = self._case_nodes(case, ginf) if case is not None else set()
            fired = sorted({info["rule"] for t, info in derivs.items()
                            if (t, info["rule"]) not in prev_rules and t[0] in nodes and info["kind"] == "rule"})
            self._last[run] = ginf
            self._last_derivs[run] = derivs
            # постусловия N и заблокированные стратегией конфликта правила — тоже в журнал шага
            clabel = self.kb.label(case) if case is not None else ""
            posts, blocked = [], []
            for cyc in res["trace"]:
                if cyc.get("chosen") in fired and cyc.get("binding", {}).get("case") == clabel and cyc.get("post"):
                    posts.append(f"{cyc['chosen']}: {cyc['post']}")
                for bl in cyc.get("blocked", []):
                    if bl["binding"].get("case") == clabel:
                        blocked.append(f"{bl['rule']} заблокировано: {bl['why']}")
            # (5) выбор перехода
            nxt, by_rule = self.choose_next(scene, case, derivs, ginf)
            filled.update(self._collect(run, case, ginf))
            n = len(list(self.rg.objects(run, EX.hasStep))) + 1
            st = EX[f"{self.kb.qname(run).split(':')[1]}_step{n}"]
            self.rg.add((run, EX.hasStep, st))
            self.rg.add((st, EX.stepNo, Literal(n)))
            self.rg.add((st, EX.scene, scene))
            self.rg.add((st, EX.preResult, Literal(pre_res)))
            self.rg.add((st, EX.actionResult, Literal(str(self.sg.value(scene, EX.actionText) or ""))))
            self.rg.add((st, EX.filledSlots, Literal(json.dumps(filled, ensure_ascii=False))))
            self.rg.add((st, EX.firedRules, Literal(", ".join(fired))))
            self.rg.add((st, EX.demonsFired, Literal("; ".join(ctx.demon_log + posts))))
            if blocked:
                self.rg.add((st, EX.conflictNote, Literal("; ".join(blocked))))
            if by_rule:
                self.rg.add((st, EX.transitionRule, Literal(by_rule)))
            if nxt is not None:
                self.rg.add((st, EX.toScene, nxt))
            self.rg.add((run, EX.visitedScene, scene))
            if nxt is not None:
                self.rg.set((run, EX.currentScene, nxt))
            else:
                self.rg.remove((run, EX.currentScene, None))
                if str(self.rg.value(run, EX.runStatus)) != "завершён":
                    self.rg.set((run, EX.runStatus, Literal("завершён")))
            self.kb.save(["runs"])
            return {"scene": self.kb.label(scene), "pre": pre_res, "slots": filled, "fired": fired,
                    "demons": ctx.demon_log + posts, "blocked": blocked, "by_rule": by_rule,
                    "next": self.kb.label(nxt) if nxt is not None else "конец", "inference": res}

    def run_to_end(self, run, answers=None, max_steps=20):
        log = []
        for _ in range(max_steps):
            q = self.pending_question(run)
            if q:
                ans = (answers or {}).get(q["slot"])
                if ans is None:
                    return log, q
                self.answer(run, q["slot"], ans)
            r = self.step(run)
            if r.get("done"):
                break
            log.append(r)
            if self.rg.value(run, EX.currentScene) is None:
                break
        return log, None

    def _case_nodes(self, case, inf=None):
        nodes = {case}
        inf = inf if inf is not None else self.kb.g["inferred"]
        nodes |= set(inf.subjects(EX.forCase, case)) | set(inf.subjects(EX.situationOf, case))
        log = self.kb.g["data"].value(case, EX.inLog)
        if log is not None:
            nodes.add(log)
        return nodes

    def choose_next(self, scene, case, derivs=None, ginf=None):
        """Переход выбирает правило: если в выводе есть заключение правила перехода
        для этого кейса — идём по нему; при нескольких — по большему приоритету."""
        derivs = self.engine.derivations if derivs is None else derivs
        nodes = self._case_nodes(case, ginf)
        cands = []
        for tr in self.sg.objects(scene, EX.transition):
            rule_iri, to = self.sg.value(tr, EX.byRule), self.sg.value(tr, EX.toScene)
            rname = self.kb.label(rule_iri)
            if any(info["rule"] == rname and t[0] in nodes for t, info in derivs.items()):
                r = self.engine.rule(rname)
                cands.append((r.priority if r else 0, rname, to))
        if cands:
            cands.sort(reverse=True)
            return cands[0][2], cands[0][1]
        return self.sg.value(scene, EX.nextScene), ""

    def preview_transition(self, run):
        """Пересчёт ветвления исполняемого сценария после изменения данных (шаг 6).
        Берётся последний выполненный шаг: если его сцена ветвящаяся и следующая сцена
        ещё не выполнялась — переход вычисляется заново и сценарий направляется по новой
        ветке. Если ветвление уже пройдено — сообщается, куда повёл бы переход теперь."""
        case = self.rg.value(run, EX.runCase)
        steps = sorted(self.rg.objects(run, EX.hasStep), key=lambda s: int(self.rg.value(s, EX.stepNo)), reverse=True)
        if not steps:
            return None
        last = steps[0]
        scopes = (self.visited(run) | {self.rg.value(run, EX.currentScene)}) - {None}
        _, derivs, ginf = self.engine.evaluate_scoped(scopes)
        self._last[run] = ginf
        for st in steps:
            scene = self.rg.value(st, EX.scene)
            if not list(self.sg.objects(scene, EX.transition)):
                continue
            before = self.rg.value(st, EX.toScene)
            nxt, rule = self.choose_next(scene, case, derivs, ginf)
            res = {"scene": self.kb.label(scene), "scene_iri": scene, "before": self.kb.label(before),
                   "after": self.kb.label(nxt), "after_iri": nxt, "rule": rule or "слот «Следующая сцена»",
                   "changed": nxt != before, "rerouted": False}
            if nxt != before and st == last:
                # следующая сцена ещё не выполнялась — перенаправляем сценарий по новой ветке
                self.rg.set((st, EX.toScene, nxt))
                self.rg.remove((st, EX.transitionRule, None))
                if rule:
                    self.rg.add((st, EX.transitionRule, Literal(rule)))
                self.rg.set((run, EX.currentScene, nxt))
                self.rg.set((run, EX.runStatus, Literal("исполняется")))
                res["rerouted"] = True
                self.kb.save(["runs"])
            return res
        return None

    def _collect(self, run, case, ginf):
        """Записать результаты вывода в слоты экземпляра сценария."""
        if case is None:
            return {}
        wm = self.kb.union("data")
        for t in ginf:
            wm.add(t)
        out = {}
        mapping = [(EX.dataStatus, EX.runDataStatus, "Достаточность данных"),
                   (EX.objectComplexity, EX.runComplexity, "Объектная сложность"),
                   (EX.selectedForecastHours, EX.runSelectedHours, "Принятый прогноз остатка, ч"),
                   (EX.selectedModel, EX.runSelectedModel, "Выбранная модель"),
                   (EX.forecastTotalHours, EX.runTotalHours, "Прогноз времени цикла, ч"),
                   (EX.riskLevel, EX.runRisk, "Риск нарушения SLA"),
                   (EX.decision, EX.runDecision, "Решение")]
        for src, dst, name in mapping:
            v = wm.value(case, src)
            if v is not None:
                self.rg.set((run, dst, v))
                out[name] = self.kb.label(v) if isinstance(v, URIRef) else str(v)
        for fc in ginf.subjects(EX.forCase, case):
            if (fc, RDF.type, EX.Forecast) in wm:
                m, p = wm.value(fc, EX.byModel), wm.value(fc, EX.predictedHours)
                out[f"Прогноз {self.kb.label(m)}, ч"] = str(p)
                self.rg.set((run, EX.runForecastXGB if self.frames.is_subclass(self.frames.type_of(m), EX.XGBoostModel)
                             else EX.runForecastGNN, p))
            if (fc, RDF.type, EX.Recommendation) in wm:
                out["Рекомендация"] = str(wm.value(fc, EX.action))
                self.rg.set((run, EX.runRecommendation, wm.value(fc, EX.action)))
        return out
