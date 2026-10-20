"""
frames.py — Модель 2. Фреймовая модель (взгляд проектировщика), хранимая в графе RDF.

Фрейм-прототип = owl:Class, связь AKO = rdfs:subClassOf, фрейм-экземпляр = индивид
класса, слот = свойство с аннотациями:
    ex:inheritance  — указатель наследования U / S / R / O;
    ex:valueSource  — способ получения значения;
    ex:onAdded / ex:onRemoved / ex:ifNeeded — имена демонов и процедур;
    ex:slotDefault  — значение по умолчанию, заданное у класса.

Наследование значений по умолчанию программа выполняет сама (обход AKO вверх
до ближайшего предка, где задано значение) с учётом указателей:
    U (unique)   — значение не наследуется, у каждого экземпляра своё;
    S (same)     — значение берётся у предка и не может быть переопределено;
    R (range)    — значение задаётся в экземпляре в пределах диапазона из SHACL,
                   значение предка служит начальным;
    O (override) — значение предка, если в экземпляре не задано своё.
Принадлежность экземпляров родительским классам выводит ризонер owlrl,
проверку данных выполняет pySHACL.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation

import owlrl
from pyshacl import validate as shacl_validate
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from .store import EX, SN, SH, SPARQL_PREFIXES, slugify

SERVICE_SLOTS = (EX.createdAt, EX.modifiedAt, EX.modifiedBy)
SCHEMA_GRAPHS = ("frames", "scenarios")      # где объявлены классы и слоты
INSTANCE_GRAPHS = ("data", "inferred", "runs", "scenarios", "rules")


class FrameModel:
    def __init__(self, kb):
        self.kb = kb
        self._pcache = {}      # кэш описаний слотов; сбрасывается после каждого изменения
        self._scache = {}

    def invalidate(self):
        """Сбросить кэши после изменения базы знаний."""
        self._pcache.clear()
        self._scache.clear()

    @property
    def fg(self):
        return self.kb.g["frames"]

    # ================================================================ иерархия AKO
    def frame_classes(self):
        """Все фреймы-прототипы (классы, у которых указан вид фрейма)."""
        return sorted(set(self.fg.subjects(EX.frameKind, None)), key=lambda c: self.kb.label(c))

    def is_class(self, c) -> bool:
        return (c, RDF.type, OWL.Class) in self.fg

    def parents(self, c):
        return list(self.fg.objects(c, RDFS.subClassOf))

    def children(self, c):
        return sorted(self.fg.subjects(RDFS.subClassOf, c), key=lambda x: self.kb.label(x))

    def ancestors(self, c, include_self=True):
        """Предки по AKO в порядке удалённости (собственный обход в ширину)."""
        res = [(c, 0)] if include_self else []
        seen, q = {c}, deque([(c, 0)])
        while q:
            v, d = q.popleft()
            for p in self.parents(v):
                if p not in seen:
                    seen.add(p)
                    res.append((p, d + 1))
                    q.append((p, d + 1))
        return res

    def is_subclass(self, c, ancestor) -> bool:
        return any(a == ancestor for a, _ in self.ancestors(c))

    def roots(self):
        return [c for c in self.frame_classes() if not any(p in self.frame_classes() for p in self.parents(c))]

    def tree(self):
        """Дерево классов по AKO для экрана «Фреймы»."""
        def node(c):
            return {"id": self.kb.qname(c), "label": self.kb.label(c),
                    "kind": str(self.fg.value(c, EX.frameKind) or ""),
                    "n": len(self.instances(c, include_sub=False)),
                    "children": [node(ch) for ch in self.children(c)]}
        return [node(r) for r in self.roots()]

    def depth(self):
        return max((max(d for _, d in self.ancestors(c)) for c in self.frame_classes()), default=0)

    # ================================================================ слоты
    def prop_info(self, p) -> dict:
        if p in self._pcache:
            return dict(self._pcache[p])
        src = self.fg if (p, None, None) in self.fg else self.kb.g["scenarios"]
        rng = src.value(p, RDFS.range)
        info = {
            "iri": p, "id": self.kb.qname(p), "label": self.kb.label(p),
            "object": (p, RDF.type, OWL.ObjectProperty) in src,
            "range": rng, "range_label": self.kb.qname(rng) if rng is not None else "—",
            "domain": src.value(p, RDFS.domain),
            "inheritance": str(src.value(p, EX.inheritance) or "U"),
            "source": str(src.value(p, EX.valueSource) or ""),
            "on_added": str(src.value(p, EX.onAdded) or ""),
            "on_removed": str(src.value(p, EX.onRemoved) or ""),
            "if_needed": str(src.value(p, EX.ifNeeded) or ""),
            "derived": bool(src.value(p, EX.derived)),
            "functional": (p, RDF.type, OWL.FunctionalProperty) in src,
            "askable": bool(src.value(p, EX.askable)),
        }
        self._pcache[p] = info
        return dict(info)

    def all_props(self):
        props = set()
        for gname in SCHEMA_GRAPHS:
            g = self.kb.g[gname]
            props |= set(g.subjects(RDF.type, OWL.DatatypeProperty))
            props |= set(g.subjects(RDF.type, OWL.ObjectProperty))
        return props

    def slots_of_class(self, c, include_derived=True):
        """Слоты фрейма: свойства, домен которых — сам класс или его предок по AKO."""
        key = (c, include_derived)
        if key in self._scache:
            return [dict(x) for x in self._scache[key]]
        anc = {a: d for a, d in self.ancestors(c)}
        slots = []
        for p in self.all_props():
            info = self.prop_info(p)
            if info["domain"] in anc and (include_derived or not info["derived"]):
                info["declared_in"] = self.kb.label(info["domain"])
                info["depth"] = anc[info["domain"]]
                slots.append(info)
        # сначала собственные слоты класса, затем унаследованные; служебные — в конце
        slots.sort(key=lambda s: (s["iri"] in SERVICE_SLOTS, s["derived"], s["depth"], s["label"]))
        self._scache[key] = slots
        return [dict(x) for x in slots]

    def class_default(self, c, p):
        """Значение по умолчанию слота p для класса c: у ближайшего предка (или у самого c)."""
        for a, _ in self.ancestors(c):
            for b in self.fg.objects(a, EX.slotDefault):
                if self.fg.value(b, EX.slot) == p:
                    return self.fg.value(b, EX.value), a
        return None, None

    def own_defaults(self, c):
        res = {}
        for b in self.fg.objects(c, EX.slotDefault):
            res[self.fg.value(b, EX.slot)] = self.fg.value(b, EX.value)
        return res

    # ================================================================ экземпляры
    def instances(self, c, include_sub=True):
        classes = {c}
        if include_sub:
            classes |= {x for x in self.frame_classes() if self.is_subclass(x, c)}
        res = set()
        for gname in ("data", "inferred", "runs", "scenarios"):
            for cls in classes:
                res |= set(self.kb.g[gname].subjects(RDF.type, cls))
        return sorted(res, key=lambda x: self.kb.label(x))

    def type_of(self, i):
        """Самый конкретный asserted-класс экземпляра."""
        for gname in ("data", "inferred", "runs", "scenarios", "rules"):
            for t in self.kb.g[gname].objects(i, RDF.type):
                if self.is_class(t):
                    return t
        return None

    def graph_of(self, i):
        for gname in ("data", "inferred", "runs", "scenarios", "rules"):
            if (i, RDF.type, None) in self.kb.g[gname]:
                return gname
        return None

    def own_values(self, i, p):
        vals = []
        for gname in ("data", "runs", "scenarios", "rules"):
            vals += [(v, gname) for v in self.kb.g[gname].objects(i, p)]
        return vals

    def resolve_slot(self, i, slot, cls=None, derivations=None):
        """Значение слота экземпляра и откуда оно взято (собственное / от предка / вывод / IF-NEEDED)."""
        cls = cls or self.type_of(i)
        p = slot["iri"]
        own = self.own_values(i, p)
        inferred = list(self.kb.g["inferred"].objects(i, p))
        default, dcls = self.class_default(cls, p) if cls is not None else (None, None)
        ptr = slot["inheritance"]
        row = {"values": [], "how": "", "from": "", "violation": ""}
        if own:
            row["values"] = [v for v, _ in own]
            row["how"] = "собственное значение"
            if ptr == "S" and default is not None and any(v != default for v, _ in own):
                row["violation"] = f"S-слот: значение должно совпадать с «{default}» у {self.kb.label(dcls)}"
        elif inferred:
            row["values"] = inferred
            rules = []
            if derivations is not None:
                rules = sorted({d["rule"] for (s, pp, o), d in derivations.items() if s == i and pp == p})
            row["how"] = "выведено правилом " + ", ".join(rules) if rules else "выведено механизмом вывода"
        elif default is not None and ptr in ("S", "O", "R"):
            row["values"] = [default]
            row["how"] = "наследование через AKO"
            row["from"] = self.kb.label(dcls)
        elif slot["if_needed"]:
            row["how"] = f"IF-NEEDED: вычисляется процедурой «{slot['if_needed']}» при обращении"
        return row

    def instance_card(self, i, derivations=None):
        cls = self.type_of(i)
        slots = self.slots_of_class(cls) if cls is not None else []
        rows = []
        for s in slots:
            r = self.resolve_slot(i, s, cls, derivations)
            if s["derived"] and not r["values"]:
                continue                     # пустые выводимые слоты не загромождают карточку
            demon = "; ".join(x for x in (
                f"IF-ADDED: {s['on_added']}" if s["on_added"] else "",
                f"IF-REMOVED: {s['on_removed']}" if s["on_removed"] else "",
                f"IF-NEEDED: {s['if_needed']}" if s["if_needed"] else "") if x) or "–"
            rows.append({"slot": s["label"], "id": s["id"], "vals": [self.fmt(v) for v in r["values"]],
                         "type": s["range_label"], "ptr": s["inheritance"], "how": r["how"],
                         "from": r["from"], "source": s["source"], "demon": demon,
                         "violation": r["violation"], "if_needed": s["if_needed"],
                         "empty": not r["values"], "derived": s["derived"]})
        reasoner = self.reasoner_types().get(i, [])
        return {"id": self.kb.qname(i), "label": self.kb.label(i),
                "class": self.kb.label(cls) if cls is not None else "—",
                "class_id": self.kb.qname(cls) if cls is not None else "",
                "ako_chain": [self.kb.label(a) for a, _ in self.ancestors(cls)] if cls is not None else [],
                "reasoner": [self.kb.label(t) for t in reasoner],
                "sem": self._sem_link(i, cls), "rows": rows, "graph": self.graph_of(i)}

    def class_card(self, c):
        rows = []
        for s in self.slots_of_class(c, include_derived=False):
            default, dcls = self.class_default(c, s["iri"])
            demon = "; ".join(x for x in (
                f"IF-ADDED – {s['on_added']}" if s["on_added"] else "",
                f"IF-REMOVED – {s['on_removed']}" if s["on_removed"] else "",
                f"IF-NEEDED – {s['if_needed']}" if s["if_needed"] else "") if x) or "–"
            rows.append({"slot": s["label"], "id": s["id"],
                         "value": self.fmt(default) if default is not None else "–",
                         "value_from": self.kb.label(dcls) if dcls is not None and dcls != c else "",
                         "type": "frame" if s["object"] else self.kb.qname(s["range"]).replace("xsd:", ""),
                         "ptr": s["inheritance"], "source": s["source"], "demon": demon,
                         "declared_in": s["declared_in"]})
        ako = self.parents(c)
        return {"id": self.kb.qname(c), "label": self.kb.label(c),
                "kind": str(self.fg.value(c, EX.frameKind) or ""),
                "ako": ", ".join(self.kb.label(p) for p in ako) or "—",
                "chain": [self.kb.label(a) for a, _ in self.ancestors(c)],
                "rows": rows, "created": str(self.fg.value(c, EX.createdAt) or "исходная база"),
                "author": str(self.fg.value(c, EX.modifiedBy) or "И.С. Бронников"),
                "sem": self._sem_link(c, None),
                "instances": [{"id": self.kb.qname(x), "label": self.kb.label(x)} for x in self.instances(c)],
                "shapes": self.shapes_for(c)}

    def _sem_link(self, x, cls):
        for gname in ("frames", "data", "scenarios"):
            n = self.kb.g[gname].value(x, EX.semNode)
            if n is not None:
                return {"id": self.kb.qname(n), "label": self.kb.label(n), "via": "сам фрейм"}
        if cls is not None:
            for a, _ in self.ancestors(cls):
                n = self.fg.value(a, EX.semNode)
                if n is not None:
                    return {"id": self.kb.qname(n), "label": self.kb.label(n), "via": f"класс {self.kb.label(a)}"}
        return None

    def shapes_for(self, c):
        """Ограничения SHACL, действующие на класс (и унаследованные от предков)."""
        sg = self.kb.g["shapes"]
        res = []
        for a, _ in self.ancestors(c):
            for shape in sg.subjects(SH.targetClass, a):
                for ps in sg.objects(shape, SH.property):
                    parts = []
                    for key, name in ((SH.minCount, "min"), (SH.maxCount, "max"), (SH.minInclusive, "≥"),
                                      (SH.maxInclusive, "≤"), (SH.minExclusive, ">"), (SH.datatype, "тип"),
                                      (SH["class"], "класс"), (SH.lessThanOrEquals, "≤ слота")):
                        v = sg.value(ps, key)
                        if v is not None:
                            parts.append(f"{name} {self.kb.qname(v) if isinstance(v, URIRef) else v}")
                    res.append({"shape": self.kb.qname(shape), "path": self.kb.label(sg.value(ps, SH.path)),
                                "rule": ", ".join(parts), "from": self.kb.label(a)})
        return res

    @staticmethod
    def fmt(v):
        if v is None:
            return ""
        if isinstance(v, Literal):
            return str(v)
        return v  # IRI отображается шаблоном через label

    # ================================================================ рабочая память
    def materialized_defaults(self) -> Graph:
        """Триплеты унаследованных значений по умолчанию (S, O, R) для всех экземпляров.
        Используются как часть рабочей памяти механизма вывода и при проверке SHACL."""
        g = Graph()
        data = self.kb.g["data"]
        for i in set(data.subjects(RDF.type, None)):
            cls = self.type_of(i)
            if cls is None:
                continue
            for s in self.slots_of_class(cls, include_derived=False):
                if s["inheritance"] not in ("S", "O", "R"):
                    continue
                if (i, s["iri"], None) in data:
                    continue
                v, _ = self.class_default(cls, s["iri"])
                if v is not None:
                    g.add((i, s["iri"], v))
        return g

    def schema_graph(self) -> Graph:
        """Иерархия классов и объявления — для путей a/rdfs:subClassOf* в запросах."""
        g = Graph()
        for t in self.fg.triples((None, RDFS.subClassOf, None)):
            g.add(t)
        for t in self.fg.triples((None, RDF.type, None)):
            g.add(t)
        for t in self.fg.triples((None, RDFS.label, None)):
            g.add(t)
        return g

    # ================================================================ ризонер owlrl
    def reasoner_types(self) -> dict:
        """Вывод принадлежности экземпляров родительским классам ризонером owlrl (RDFS).
        Возвращает {экземпляр: [выведенные классы, которых нет среди заданных явно]}."""
        g = Graph()
        for t in self.fg.triples((None, RDFS.subClassOf, None)):
            g.add(t)
        asserted = {}
        for gname in ("data", "inferred", "runs"):
            for i, c in self.kb.g[gname].subject_objects(RDF.type):
                if self.is_class(c):
                    g.add((i, RDF.type, c))
                    asserted.setdefault(i, set()).add(c)
        owlrl.DeductiveClosure(owlrl.RDFS_Semantics, axiomatic_triples=False,
                               datatype_axioms=False).expand(g)
        res = {}
        for i in asserted:
            extra = [c for c in g.objects(i, RDF.type)
                     if isinstance(c, URIRef) and c not in asserted[i] and c in set(self.frame_classes())]
            res[i] = sorted(extra, key=lambda c: -len(self.ancestors(c)))
        return res

    # ================================================================ SHACL
    def validate(self) -> dict:
        """Проверить фреймы-экземпляры формами SHACL (pySHACL). Данные = экземпляры +
        унаследованные значения по умолчанию; онтология = иерархия классов."""
        data = Graph()
        for t in self.kb.g["data"]:
            data.add(t)
        for t in self.materialized_defaults():
            data.add(t)
        ont = Graph()
        for t in self.fg.triples((None, RDFS.subClassOf, None)):
            ont.add(t)
        conforms, rg, _ = shacl_validate(data, shacl_graph=self.kb.g["shapes"], ont_graph=ont,
                                         inference="none", advanced=True, allow_warnings=True)
        results = []
        for r in rg.subjects(RDF.type, SH.ValidationResult):
            path = rg.value(r, SH.resultPath)
            results.append({
                "focus": self.kb.label(rg.value(r, SH.focusNode)),
                "focus_id": self.kb.qname(rg.value(r, SH.focusNode)),
                "path": self.kb.label(path) if path is not None else "",
                "value": str(rg.value(r, SH.value) or ""),
                "message": str(rg.value(r, SH.resultMessage) or ""),
                "constraint": self.kb.qname(rg.value(r, SH.sourceConstraintComponent)).replace(
                    "http://www.w3.org/ns/shacl#", "sh:"),
            })
        return {"conforms": conforms, "results": results}

    # ================================================================ изменения
    def cast(self, p, text):
        """Привести введённую строку к типу слота (rdfs:range)."""
        info = self.prop_info(p)
        text = str(text).strip()
        if info["object"]:
            return self.kb.expand(text)
        rng = info["range"]
        try:
            if rng == XSD.integer:
                return Literal(int(float(text.replace(",", "."))), datatype=XSD.integer)
            if rng == XSD.decimal:
                return Literal(Decimal(text.replace(",", ".")), datatype=XSD.decimal)
            if rng == XSD.boolean:
                return Literal(text.lower() in ("true", "1", "да", "yes"), datatype=XSD.boolean)
        except (ValueError, InvalidOperation):
            raise ValueError(f"Значение «{text}» не соответствует типу слота {info['range_label']}")
        return Literal(text)

    def new_iri(self, label, prefix=""):
        base = prefix + slugify(label)
        iri, k = EX[base], 2
        while any((iri, None, None) in self.kb.g[n] for n in ("frames", "data", "runs")):
            iri, k = EX[f"{base}{k}"], k + 1
        return iri

    def touch(self, x, graph, created=False):
        """Служебные слоты: дата создания и автор последнего изменения."""
        g = self.kb.g[graph]
        if created:
            g.set((x, EX.createdAt, self.kb.now()))
        g.set((x, EX.modifiedAt, self.kb.now()))
        g.set((x, EX.modifiedBy, Literal(self.kb.author)))

    def create_class(self, label, parent, move_children=(), sem_node=None, kind=None):
        c = self.new_iri(label)
        self.fg.add((c, RDF.type, OWL.Class))
        self.fg.add((c, RDFS.label, Literal(label, lang="ru")))
        self.fg.add((c, RDFS.subClassOf, parent))
        self.fg.add((c, EX.frameKind, Literal(kind or str(self.fg.value(parent, EX.frameKind) or "прототип-объект"))))
        if sem_node is not None:
            self.fg.add((c, EX.semNode, sem_node))
        # вставка в середину иерархии: переподчинить выбранных потомков новому классу
        for ch in move_children:
            self.fg.remove((ch, RDFS.subClassOf, parent))
            self.fg.add((ch, RDFS.subClassOf, c))
            self.touch(ch, "frames")
        self.touch(c, "frames", created=True)
        return c

    def set_class_default(self, c, p, value):
        for b in list(self.fg.objects(c, EX.slotDefault)):
            if self.fg.value(b, EX.slot) == p:
                self.fg.remove((b, None, None))
                self.fg.remove((c, EX.slotDefault, b))
        b = BNode()
        self.fg.add((c, EX.slotDefault, b))
        self.fg.add((b, EX.slot, p))
        self.fg.add((b, EX.value, value))
        self.touch(c, "frames")

    def create_instance(self, cls, label, values: dict):
        i = self.new_iri(label)
        d = self.kb.g["data"]
        d.add((i, RDF.type, cls))
        d.add((i, RDFS.label, Literal(label, lang="ru")))
        for p, text in values.items():
            if text is None or str(text).strip() == "":
                continue
            d.add((i, p, self.cast(p, text)))
        self.touch(i, "data", created=True)
        return i

    def set_slot(self, i, p, value):
        d = self.kb.g["data"]
        old = list(d.objects(i, p))
        d.remove((i, p, None))
        d.add((i, p, value))
        self.touch(i, "data")
        return old

    def dangling_refs(self, x):
        """Ссылки на узел x из других узлов — они станут висячими после удаления."""
        refs = []
        for gname in ("data", "inferred", "runs", "frames", "rules", "scenarios"):
            for s, p in self.kb.g[gname].subject_predicates(x):
                if (s, RDF.type, EX.Derivation) in self.kb.g["inferred"]:
                    continue                     # служебные записи о выводе удаляются вместе с выводом
                if s != x and not isinstance(s, BNode):
                    refs.append({"graph": gname, "s": s, "p": p, "s_label": self.kb.label(s),
                                 "p_label": self.kb.label(p)})
                elif isinstance(s, BNode):
                    owner = next(self.kb.g[gname].subjects(None, s), None)
                    if owner is not None:
                        refs.append({"graph": gname, "s": owner, "p": p, "s_label": self.kb.label(owner),
                                     "p_label": self.kb.label(p) + " (по умолчанию)"})
        if self.is_class(x):
            for sub in self.children(x):
                refs.append({"graph": "frames", "s": sub, "p": RDFS.subClassOf,
                             "s_label": self.kb.label(sub), "p_label": "AKO"})
        return refs

    def delete_node(self, x, cascade_nodes=()):
        """Удалить узел (и при каскадном удалении — зависимые узлы). Возвращает удалённые триплеты."""
        removed = []
        for node in [x, *cascade_nodes]:
            for gname in ("data", "inferred", "runs", "frames"):
                g = self.kb.g[gname]
                for t in list(g.triples((node, None, None))) + list(g.triples((None, None, node))):
                    g.remove(t)
                    removed.append((gname, t))
        return removed

    # ================================================================ SPARQL
    TEMPLATES = {
        "Все экземпляры класса «Прогнозная модель» и наследников": """SELECT ?model ?class ?mae WHERE {
  ?model a ?class . ?class rdfs:subClassOf* ex:PredictiveModel .
  OPTIONAL { ?model ex:mae ?mae }
}""",
        "Кейсы журнала OCEL, у которых прошло больше 300 ч": """SELECT ?case ?id ?elapsed WHERE {
  ?case ex:inLog ?log ; ex:caseId ?id ; ex:elapsedHours ?elapsed .
  ?log a/rdfs:subClassOf* ex:OCELLog .
  FILTER (?elapsed > 300)
}""",
        "Модели на GPU-серверах, загрузка которых выше 0,4": """SELECT ?model ?server ?load WHERE {
  ?model ex:trainedOn ?log .
  ?mod ex:hostedOn ?server . ?server a ex:GPUServer ; ex:load ?load .
  FILTER (?load > 0.4)
}""",
        "Слоты с демонами и присоединёнными процедурами": """SELECT ?slot ?ifAdded ?ifRemoved ?ifNeeded WHERE {
  ?slot a ?t . FILTER (?t IN (owl:DatatypeProperty, owl:ObjectProperty))
  OPTIONAL { ?slot ex:onAdded ?ifAdded }
  OPTIONAL { ?slot ex:onRemoved ?ifRemoved }
  OPTIONAL { ?slot ex:ifNeeded ?ifNeeded }
  FILTER (BOUND(?ifAdded) || BOUND(?ifRemoved) || BOUND(?ifNeeded))
}""",
        "Иерархия AKO: класс и его родитель": """SELECT ?class ?ako WHERE {
  ?class rdfs:subClassOf ?ako . ?class ex:frameKind ?k .
}""",
    }

    def sparql(self, query: str, derived=True):
        g = self.kb.union("frames", "data", "scenarios", *(("inferred",) if derived else ()))
        q = query if "PREFIX" in query.upper() else SPARQL_PREFIXES + query
        res = g.query(q)
        head = [str(v) for v in res.vars]
        rows = [[self.kb.label(x) if isinstance(x, URIRef) else ("" if x is None else str(x)) for x in row]
                for row in res]
        return head, rows

    # ================================================================ сеть фреймов
    def network(self):
        """Узлы — фреймы-прототипы; дуги AKO (rdfs:subClassOf) и дуги свойств-связей
        (owl:ObjectProperty: домен -> область значений), как на рисунке 1 методички."""
        classes = set(self.frame_classes())
        nodes = [{"id": self.kb.qname(c), "label": self.kb.label(c),
                  "kind": str(self.fg.value(c, EX.frameKind) or "")} for c in classes]
        edges = []
        for c in classes:
            for p in self.parents(c):
                if p in classes:
                    edges.append({"from": self.kb.qname(c), "to": self.kb.qname(p), "label": "AKO", "kind": "ako"})
        for p in self.all_props():
            info = self.prop_info(p)
            if info["object"] and info["domain"] in classes and info["range"] in classes and not info["derived"]:
                edges.append({"from": self.kb.qname(info["domain"]), "to": self.kb.qname(info["range"]),
                              "label": info["label"], "kind": "slot"})
        return nodes, edges
