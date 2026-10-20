"""
rebuild.py — интерактивное перестроение обеих моделей (разделы 7 и 11 задания).

После каждого изменения выполняются одни и те же шаги, и результат каждого шага
попадает в отчёт для панели «Что изменилось»:
  1. Проверить изменение (словарь отношений / формы SHACL); при нарушении — откат.
  2. Перестроить семантическую сеть (новые вершины и связи подсвечиваются,
     предлагается создать фрейм).
  3. Перестроить фреймовую модель (иерархия AKO, ризонер owlrl, наследование слотов).
  4. Запустить демоны (IF-ADDED при записи, IF-REMOVED при удалении).
  5. Пересчитать вывод (появившиеся и исчезнувшие заключения с правилами).
  6. Пересчитать сценарий (переход из текущей сцены исполняемого сценария).
  7. Сохранить (журнал + файлы Turtle).
Шесть поддерживаемых случаев: вершина и связь в сети; фрейм-экземпляр; фрейм-прототип
в середину иерархии; значение слота; удаление сущности; новое правило.
"""
from __future__ import annotations

from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, OWL

from . import procedures as P
from .mapping import FRAME_GROUPS
from .rules import Ctx
from .store import EX, SN

STEP_TITLES = ["1. Проверка изменения", "2. Семантическая сеть", "3. Фреймовая модель",
               "4. Демоны", "5. Повторный вывод", "6. Сценарий", "7. Сохранение"]
ALL_GRAPHS = ["semantic", "frames", "data", "rules", "scenarios", "inferred", "runs"]


class Report:
    """Отчёт о перестроении для панели «Что изменилось»."""

    def __init__(self, action):
        self.action = action
        self.ok = True
        self.steps = {t: [] for t in STEP_TITLES}
        self.added, self.removed = [], []
        self.highlight = {"sem": [], "frames": [], "scene": None}
        self.suggest = None
        self.choices = None

    def log(self, step_no, text):
        self.steps[STEP_TITLES[step_no - 1]].append(text)

    def to_dict(self):
        return {"action": self.action, "ok": self.ok, "steps": self.steps, "added": self.added[:80],
                "removed": self.removed[:80], "n_added": len(self.added), "n_removed": len(self.removed),
                "highlight": self.highlight, "suggest": self.suggest, "choices": self.choices}


class Rebuilder:
    def __init__(self, kb, sem, frames, engine, scen, mapping):
        self.kb, self.sem, self.frames, self.engine, self.scen, self.mapping = kb, sem, frames, engine, scen, mapping

    # ------------------------------------------------------------ общие шаги
    def _begin(self, action):
        rep = Report(action)
        snap = self.kb.snapshot(["semantic", "frames", "data", "rules", "scenarios"])
        types = self.frames.reasoner_types()
        return rep, snap, types

    def _rollback(self, snap):
        for name, triples in snap.items():
            g = self.kb.g[name]
            g.remove((None, None, None))
            for t in triples:
                g.add(t)
        self.frames.invalidate()

    def _shacl(self, rep, snap):
        """Шаг 1 для данных фреймов: проверка формами SHACL; при нарушении — откат."""
        res = self.frames.validate()
        if res["conforms"]:
            rep.log(1, "SHACL: нарушений нет (pySHACL, формы графа shapes)")
            return True
        for r in res["results"]:
            rep.log(1, f"SHACL НАРУШЕНИЕ: «{r['focus']}», слот «{r['path']}» = {r['value'] or '—'}: "
                       f"{r['constraint']} — {r['message']}")
        rep.log(1, "Изменение НЕ сохранено, база возвращена в прежнее состояние")
        self._rollback(snap)
        rep.ok = False
        return False

    def _reasoner(self, rep, types_before):
        after = self.frames.reasoner_types()
        changed = False
        for i, cls in after.items():
            new = set(cls) - set(types_before.get(i, []))
            if new:
                changed = True
                rep.log(3, f"Ризонер owlrl отнёс «{self.kb.label(i)}» к классам: "
                           + ", ".join(self.kb.label(c) for c in cls))
        if not changed:
            rep.log(3, "Ризонер owlrl: принадлежность экземпляров классам не изменилась")

    def _infer(self, rep):
        res = self.engine.run_forward(label="повторный вывод после изменения")
        rep.log(5, f"Прямой вывод: {res['cycles']} циклов, выведено триплетов: {res['n_derived']}")
        for x in res["appeared"]:
            rep.log(5, f"+ появилось: {x}")
        for x in res["disappeared"]:
            rep.log(5, f"− исчезло: {x}")
        if not res["appeared"] and not res["disappeared"]:
            rep.log(5, "Заключения не изменились")
        return res

    def _scenario(self, rep):
        run = self.scen.active_run()
        if run is None:
            rep.log(6, "Исполняемого сценария нет — пересчёт ветвления не требуется")
            return
        pv = self.scen.preview_transition(run)
        if pv is None:
            rep.log(6, f"{self.kb.label(run)}: ветвящиеся сцены ещё не пройдены")
            return
        if pv["changed"] and pv["rerouted"]:
            rep.log(6, f"{self.kb.label(run)}: переход из сцены «{pv['scene']}» пересчитан: "
                       f"«{pv['before']}» → «{pv['after']}» (правило {pv['rule']}); сценарий пойдёт по новой ветке")
        elif pv["changed"]:
            rep.log(6, f"{self.kb.label(run)}: ветвление «{pv['scene']}» уже пройдено по «{pv['before']}»; "
                       f"при текущих данных правило {pv['rule']} выбрало бы «{pv['after']}»")
        else:
            rep.log(6, f"{self.kb.label(run)}: переход из «{pv['scene']}» прежний — «{pv['after']}» ({pv['rule']})")
        rep.highlight["scene"] = self.kb.qname(pv["after_iri"]) if pv["after_iri"] is not None else None

    def _finish(self, rep, snap, saved_graphs=None, summary=None):
        added, removed = self.kb.diff(snap)
        rep.added = [f"[{g}] {self.kb.fmt_triple(t)}" for g, t in added]
        rep.removed = [f"[{g}] {self.kb.fmt_triple(t)}" for g, t in removed]
        self.kb.save(saved_graphs or ALL_GRAPHS)
        node = self.kb.journal(rep.action, summary or {"added": len(added), "removed": len(removed)})
        rep.log(7, f"Записано в журнал ({self.kb.qname(node)}) и в файлы kb/current/*.ttl; "
                   f"после перезапуска состояние восстановится")
        self.frames.invalidate()
        return rep.to_dict()

    def _demons_added(self, rep, subject, props_values):
        """Шаг 4: вызвать демоны IF-ADDED у записанных слотов."""
        ctx = Ctx(self.kb, self.frames, self.engine)
        fired = False
        for p, v in props_values:
            name = self.frames.prop_info(p)["on_added"]
            if name and name in P.DEMONS:
                msg = P.DEMONS[name](ctx, subject, p, v)
                rep.log(4, f"IF-ADDED «{name}» у слота «{self.kb.label(p)}»: {msg}")
                fired = True
        if not fired:
            rep.log(4, "У записанных слотов нет демонов IF-ADDED")
        return ctx

    # ------------------------------------------------------------ случай 1: вершина и связь
    def add_vertex(self, label, group, definition="", attrs=(), edges=()):
        """edges: [(отношение, другая вершина, 'out'|'in')]"""
        with self.kb.lock:
            rep, snap, _ = self._begin(f"Сеть: добавлена вершина «{label}»")
            v = self.sem.add_concept(label, group, definition, attrs)
            errs = []
            for rel, other, direction in edges:
                s, o = (v, other) if direction == "out" else (other, v)
                e = self.sem.validate_edge(s, rel, o)
                if e:
                    errs += [f"«{self.kb.label(s)} —{self.kb.label(rel)}→ {self.kb.label(o)}»: {x}" for x in e]
                else:
                    self.sem.add_edge(s, rel, o)
                    rep.log(1, f"Связь «{self.kb.label(s)} —{self.kb.label(rel)}→ {self.kb.label(o)}» "
                               f"соответствует словарю отношений")
            if errs:
                for e in errs:
                    rep.log(1, "ОТКАЗ: " + e)
                rep.log(1, "Изменение НЕ сохранено")
                self._rollback(snap)
                rep.ok = False
                return rep.to_dict()
            if not edges:
                rep.log(1, "Вершина без связей будет помечена проверкой сети как изолированная")
            rep.highlight["sem"] = [self.kb.qname(v)] + [self.kb.qname(o) for _, o, _ in edges]
            rep.log(2, f"Вершина «{label}» ({self.kb.label(group)}) появилась на графе и подсвечена")
            self._suggest_frame(rep, v)
            rep.log(3, "Фреймовая модель не изменена — можно создать фрейм по предложению")
            rep.log(4, "Демоны фреймов не вызывались (изменилась только сеть)")
            rep.log(5, "Продукции читают слоты фреймов, сеть на вывод не влияет — повторный вывод не нужен")
            rep.log(6, "Сценарий не затронут")
            return self._finish(rep, snap, ["semantic", "journal"])

    def add_edge(self, s, rel, o):
        with self.kb.lock:
            rep, snap, _ = self._begin(f"Сеть: связь {self.kb.label(s)} —{self.kb.label(rel)}→ {self.kb.label(o)}")
            errs = self.sem.validate_edge(s, rel, o)
            if errs:
                for e in errs:
                    rep.log(1, "ОТКАЗ по словарю отношений: " + e)
                rep.ok = False
                return rep.to_dict()
            self.sem.add_edge(s, rel, o)
            rep.log(1, "Связь соответствует словарю отношений (домен, диапазон, группы, циклы, дубликаты)")
            rep.highlight["sem"] = [self.kb.qname(s), self.kb.qname(o)]
            rep.log(2, "Связь появилась на графе и подсвечена")
            if rel == SN.isA:
                fs, fo = self.mapping.frame_for_vertex(s), self.mapping.frame_for_vertex(o)
                if fs is not None and fo is not None and self.frames.is_class(fs) and self.frames.is_class(fo):
                    rep.log(3, f"Подсказка: связи «является» соответствует AKO "
                               f"{self.kb.label(fs)} → {self.kb.label(fo)} во фреймовой модели")
            for i in range(3, 7):
                rep.log(i, "не затронуто")
            return self._finish(rep, snap, ["semantic", "journal"])

    def _suggest_frame(self, rep, v):
        grp = self.sem.group_of(v)
        if grp not in FRAME_GROUPS:
            rep.log(2, "Для вершин этой группы фрейм-прототип не требуется (норматив → правило, процесс → сценарий)")
            return
        parent = None
        for anc, _ in self.sem.ancestors(v):
            f = self.mapping.frame_for_vertex(anc)
            if f is not None and self.frames.is_class(f):
                parent = f
                break
        rep.suggest = {"vertex": self.kb.qname(v), "label": self.kb.label(v),
                       "parent": self.kb.qname(parent) if parent is not None else "ex:Entity",
                       "parent_label": self.kb.label(parent) if parent is not None else "Сущность"}
        rep.log(2, f"Предложение: создать фрейм-прототип «{self.kb.label(v)}» AKO "
                   f"«{rep.suggest['parent_label']}»")

    # ------------------------------------------------------------ фрейм для вершины / вставка в иерархию
    def add_class(self, label, parent, move_children=(), sem_node=None, defaults=()):
        """Создать фрейм-прототип. Если указаны потомки — вставка в середину иерархии."""
        with self.kb.lock:
            what = "вставлен в середину иерархии" if move_children else "создан"
            rep, snap, types = self._begin(f"Фреймы: прототип «{label}» {what}")
            inst_before = {i: self._inherited_view(i) for ch in move_children for i in self.frames.instances(ch)}
            c = self.frames.create_class(label, parent, move_children, sem_node)
            for p, val in defaults:
                self.frames.set_class_default(c, p, self.frames.cast(p, val))
            self.frames.invalidate()
            if not self._shacl(rep, snap):
                return rep.to_dict()
            rep.highlight["frames"] = [self.kb.qname(c)] + [self.kb.qname(x) for x in move_children]
            if sem_node is not None:
                rep.log(2, f"Фрейм связан с вершиной сети «{self.kb.label(sem_node)}» (ex:semNode)")
                rep.highlight["sem"] = [self.kb.qname(sem_node)]
            else:
                rep.log(2, "Фрейм не связан с вершиной сети — появится в списке расхождений")
            rep.log(3, f"Класс {self.kb.qname(c)} встал в иерархию AKO: "
                       + " → ".join(self.kb.label(a) for a, _ in self.frames.ancestors(c)))
            for ch in move_children:
                rep.log(3, f"Переподчинён потомок «{self.kb.label(ch)}»: AKO «{self.kb.label(parent)}» → "
                           f"«{label}»")
            self._reasoner(rep, types)
            for i, before in inst_before.items():
                after = self._inherited_view(i)
                diff = [f"{k}: {before.get(k, '—')} → {v}" for k, v in after.items() if before.get(k) != v]
                rep.log(3, f"Унаследованные значения «{self.kb.label(i)}»: " + ("; ".join(diff) or "не изменились"))
            rep.log(4, "Демоны слотов не вызывались (новые значения экземпляров не записывались)")
            self._infer(rep)
            self._scenario(rep)
            return self._finish(rep, snap)

    def _inherited_view(self, i):
        card = self.frames.instance_card(i)
        return {r["slot"]: f"{', '.join(map(str, r['vals']))} ({r['how']}{' от ' + r['from'] if r['from'] else ''})"
                for r in card["rows"] if r["vals"] and r["how"].startswith("наслед")}

    # ------------------------------------------------------------ случай 2: фрейм-экземпляр
    def add_instance(self, cls, label, values: dict):
        with self.kb.lock:
            rep, snap, types = self._begin(f"Фреймы: экземпляр «{label}» класса «{self.kb.label(cls)}»")
            try:
                i = self.frames.create_instance(cls, label, values)
            except ValueError as e:
                rep.log(1, f"ОТКАЗ: {e}")
                rep.ok = False
                self._rollback(snap)
                return rep.to_dict()
            self.frames.invalidate()
            if not self._shacl(rep, snap):
                return rep.to_dict()
            rep.highlight["frames"] = [self.kb.qname(i), self.kb.qname(cls)]
            link = self.frames.instance_card(i)["sem"]
            if link:
                rep.log(2, f"Экземпляр прослеживается до вершины сети «{link['label']}» ({link['via']})")
                rep.highlight["sem"] = [link["id"]]
            card = self.frames.instance_card(i)
            for r in card["rows"]:
                if r["how"].startswith("наслед"):
                    rep.log(3, f"Слот «{r['slot']}» = {', '.join(self.kb.label(v) if isinstance(v, URIRef) else str(v) for v in r['vals'])} "
                               f"унаследован от «{r['from']}» (указатель {r['ptr']})")
            self._reasoner(rep, types)
            pv = [(p, self.kb.g["data"].value(i, p)) for p in values if self.kb.g["data"].value(i, p) is not None]
            ctx = self._demons_added(rep, i, pv)
            self._infer(rep)
            self._scenario(rep)
            return self._finish(rep, snap)

    # ------------------------------------------------------------ случай 4: значение слота
    def set_slot(self, i, p, text):
        with self.kb.lock:
            rep, snap, types = self._begin(f"Фреймы: «{self.kb.label(i)}».«{self.kb.label(p)}» = {text}")
            info = self.frames.prop_info(p)
            if info["inheritance"] == "S":
                default, dcls = self.frames.class_default(self.frames.type_of(i), p)
                if default is not None and str(default) != str(text):
                    rep.log(1, f"ОТКАЗ: слот с указателем S наследует значение «{default}» от "
                               f"«{self.kb.label(dcls)}» и не может быть переопределён")
                    rep.ok = False
                    return rep.to_dict()
            try:
                val = self.frames.cast(p, text)
            except ValueError as e:
                rep.log(1, f"ОТКАЗ: {e}")
                rep.ok = False
                return rep.to_dict()
            old = self.frames.set_slot(i, p, val)
            self.frames.invalidate()
            if not self._shacl(rep, snap):
                return rep.to_dict()
            rep.highlight["frames"] = [self.kb.qname(i)]
            rep.log(2, "Семантическая сеть не изменилась (значение слота экземпляра)")
            rep.log(3, f"Слот «{self.kb.label(p)}»: {', '.join(str(o) for o in old) or '—'} → {val}")
            self._reasoner(rep, types)
            self._demons_added(rep, i, [(p, val)])
            self._infer(rep)
            self._scenario(rep)
            return self._finish(rep, snap)

    # ------------------------------------------------------------ случай 5: удаление
    def delete(self, x, mode="check"):
        """mode: 'check' — только найти висячие ссылки и предложить выбор;
                 'cascade' — удалить вместе с зависимыми; 'refuse' — отказаться."""
        with self.kb.lock:
            rep, snap, types = self._begin(f"Удаление «{self.kb.label(x)}»")
            ctx = Ctx(self.kb, self.frames, self.engine)
            ctx.removed_node = x
            # шаг 4: IF-REMOVED у слотов, которые связывают удаляемый узел
            fired = False
            for gname in ("data", "frames"):
                g = self.kb.g[gname]
                for s, p, o in list(g.triples((x, None, None))) + list(g.triples((None, None, x))):
                    name = self.frames.prop_info(p)["on_removed"] if p in self.frames.all_props() else ""
                    if name and name in P.DEMONS and not fired:
                        msg = P.DEMONS[name](ctx, s, p, o)
                        rep.log(4, f"IF-REMOVED «{name}» у слота «{self.kb.label(p)}»: {msg}")
                        fired = True
            if not fired:
                P.DEMONS["find_dangling_refs"](ctx, x, None, None)
                rep.log(4, "У связей узла нет собственного демона IF-REMOVED; выполнен общий поиск ссылок")
            refs = ctx.dangling
            deps = ctx.dependents
            sem_refs = [self.kb.label(s) for s in self.kb.g["semantic"].subjects(None, x)] if \
                (x, RDF.type, SN.Concept) in self.kb.g["semantic"] else []
            rep.log(1, f"Висячих ссылок: {len(refs)}; зависимых выведенных данных: {len(deps)}")
            for r in refs[:20]:
                rep.log(1, f"  {r['s_label']} —{r['p_label']}→ {self.kb.label(x)} [{r['graph']}]")
            if mode == "check":
                rep.choices = {"node": self.kb.qname(x), "refs": len(refs), "deps": len(deps)}
                rep.log(1, "Выберите: отменить удаление или удалить вместе с зависимыми данными")
                rep.ok = None
                return rep.to_dict()
            if mode == "refuse":
                rep.log(1, "Удаление отменено пользователем")
                return rep.to_dict()
            # каскад: удаляем узел, ссылающиеся на него экземпляры данных и исполнения сценария
            cascade = {r["s"] for r in refs if r["graph"] in ("data", "runs") and r["p"] != EX.semNode}
            cascade |= set(deps)
            if (x, RDF.type, SN.Concept) in self.kb.g["semantic"]:
                self.sem.remove_concept(x)
                rep.log(2, f"Вершина удалена из сети вместе со связями ({len(sem_refs)} входящих)")
            else:
                rep.log(2, "Семантическая сеть не изменилась")
            removed = self.frames.delete_node(x, sorted(cascade, key=str))
            rep.log(3, f"Удалено триплетов: {len(removed)}; каскадно удалены: "
                       + (", ".join(self.kb.label(c) for c in cascade) or "—"))
            self.frames.invalidate()
            if not self._shacl(rep, snap):
                return rep.to_dict()
            self._reasoner(rep, types)
            self._infer(rep)
            self._scenario(rep)
            return self._finish(rep, snap)

    # ------------------------------------------------------------ случай 6: новое правило
    def add_rule(self, name, scope, priority, source, pre, cond, concl, post, core):
        with self.kb.lock:
            rep, snap, _ = self._begin(f"Правила: добавлено правило {name}")
            chk, tmp = self.engine.check_rule(name, core, scope, priority)
            if chk["syntax"]:
                rep.log(1, "ОТКАЗ: " + chk["syntax"])
                rep.ok = False
                return rep.to_dict()
            rep.log(1, "Синтаксис SPARQL Update корректен (разбор rdflib)")
            if chk["unknown"]:
                rep.log(1, "ОТКАЗ: в ядре есть свойства, которых нет во фреймовой модели: " + ", ".join(chk["unknown"]))
                rep.ok = False
                return rep.to_dict()
            rep.log(1, "Все свойства ядра объявлены во фреймовой модели")
            if source is None or self.kb.g["semantic"].value(source, SN.group) != SN.G_Rule:
                rep.log(1, "ОТКАЗ: источник правила должен быть вершиной группы «Бизнес-правила и ограничения»")
                rep.ok = False
                return rep.to_dict()
            for c in chk["conflicts"]:
                rep.log(1, f"Конфликт: {name} записывает тот же слот «{c['slots']}», что и {c['rule']} "
                           f"(приоритет {c['priority']})" + (" — слот функциональный, порядок решит приоритет" if c["functional"] else ""))
            if not chk["conflicts"]:
                rep.log(1, "Конфликтов с другими правилами нет")
            g = self.kb.g["rules"]
            r = EX[name]
            g.remove((r, None, None))
            g.add((r, RDF.type, EX.Rule))
            g.add((r, RDFS.label, Literal(name)))
            if scope is not None:
                g.add((r, EX.scope, scope))
            g.add((r, EX.priority, Literal(int(priority))))
            g.add((r, EX.source, source))
            g.add((r, EX.precondition, Literal(pre)))
            g.add((r, EX.conditionText, Literal(cond)))
            g.add((r, EX.conclusionText, Literal(concl)))
            if post:
                g.add((r, EX.postAction, Literal(post)))
            g.add((r, EX.core, Literal(core)))
            g.add((r, EX.createdAt, self.kb.now()))
            g.add((r, EX.modifiedBy, Literal(self.kb.author)))
            rep.log(2, f"Правило связано с источником «{self.kb.label(source)}» в сети (ex:source)")
            rep.highlight["sem"] = [self.kb.qname(source)]
            rep.log(3, "Фреймы не изменились; правило будет читать их слоты из рабочей памяти")
            rep.log(4, "Демоны не вызывались")
            res = self._infer(rep)
            fired = [c for c in res["trace"] if c.get("chosen") == name]
            rep.log(5, f"Правило {name} " + (f"сработало {len(fired)} раз(а)" if fired else "пока не сработало: условие не выполнено"))
            self._scenario(rep)
            return self._finish(rep, snap)
