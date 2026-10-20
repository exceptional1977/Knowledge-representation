"""
rules.py — база продукций и механизм вывода (написан самостоятельно).

Правило хранится в графе rules как узел ex:Rule в полной форме <i, Q, P, A→B, N>;
ядро A→B — запрос SPARQL Update «INSERT {B} WHERE {A}». Рабочая память — текущие
значения слотов фреймов: экземпляры + унаследованные значения по умолчанию +
выведенные факты + иерархия классов.

Прямой вывод (от данных), цикл «распознавание — выбор — действие»:
  1) для каждого активного правила часть WHERE выполняется как SELECT над рабочей
     памятью; каждая строка результата — активация (правило + подстановка);
  2) конфликтный набор упорядочивается стратегией: приоритет → специфичность
     условия → новизна фактов; функциональный слот (owl:FunctionalProperty) не может
     получить второе значение — такая активация блокируется;
  3) исполняется одна активация: INSERT добавляет триплеты, _:x создаёт новый
     фрейм-экземпляр, затем вызывается постусловие N; у каждого нового триплета
     запоминается правило, цикл и подстановка (для объяснения «как получено?»);
  4) пересчитываются только правила, читающие изменённые слоты; цикл повторяется,
     пока применимые правила не кончатся или не достигнута цель.
Обратный вывод (от гипотезы): ищутся правила с подходящим заключением, условие
разбивается на подцели, недостающие значения слотов спрашиваются у пользователя.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from rdflib import BNode, Graph, Literal, URIRef, Variable
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rdflib.paths import Path
from rdflib.plugins.sparql.algebra import translateUpdate
from rdflib.plugins.sparql.parser import parseUpdate
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql import prepareQuery

from . import procedures as P
from .store import EX, SN, SPARQL_PREFIXES

RULE_PREFIXES = SPARQL_PREFIXES


# ====================================================================== разбор ядра
def _block_after(text: str, start: int):
    """Найти блок {...} начиная с позиции start (с учётом вложенных скобок и строк)."""
    i = text.index("{", start)
    depth, j, in_str = 0, i, None
    while j < len(text):
        ch = text[j]
        if in_str:
            if ch == in_str and text[j - 1] != "\\":
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i, j + 1
        j += 1
    raise ValueError("несбалансированные фигурные скобки")


def split_core(core: str):
    """Разделить ядро «INSERT {B} WHERE {A}» на тексты B и A."""
    up = core.upper()
    k = up.index("INSERT")
    i1, j1 = _block_after(core, k)
    w = up.index("WHERE", j1)
    i2, j2 = _block_after(core, w)
    return core[i1:j1], core[i2:j2]


def _walk(node, out, negated=False, optional=False):
    """Обход алгебры SPARQL: собрать шаблоны триплетов и число фильтров.
    negated — шаблон внутри NOT EXISTS, optional — внутри OPTIONAL."""
    if isinstance(node, CompValue):
        name = node.name
        if name in ("BGP", "TriplesBlock"):
            for t in node["triples"]:
                t = tuple(t)
                if len(t) == 3:
                    out["patterns"].append((t, negated, optional))
                else:                       # плоский список из нескольких триплетов
                    for k in range(0, len(t) - 2, 3):
                        out["patterns"].append((tuple(t[k:k + 3]), negated, optional))
            return
        if name == "Filter":
            out["filters"] += 1
            _walk(node.get("p"), out, negated, optional)
            _walk(node.get("expr"), out, negated, optional)
            return
        if name == "LeftJoin":
            _walk(node.get("p1"), out, negated, optional)
            _walk(node.get("p2"), out, negated, True)
            return
        if name == "Builtin_NOTEXISTS":
            _walk(node.get("graph"), out, True, optional)
            return
        for v in node.values():
            _walk(v, out, negated, optional)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk(v, out, negated, optional)
    elif isinstance(node, dict):
        for v in node.values():
            _walk(v, out, negated, optional)


def pattern_key(t):
    """(предикат, константный объект или None) — для графа зависимостей правил.
    Путь a/rdfs:subClassOf* трактуется как чтение rdf:type."""
    s, p, o = t
    if isinstance(p, Path):
        p = RDF.type
    return p, (o if isinstance(o, (URIRef, Literal)) else None)


@dataclass
class Rule:
    iri: URIRef
    name: str
    scope: URIRef | None
    priority: int
    source: URIRef | None
    pre: str
    cond: str
    concl: str
    post: str
    core: str
    insert_tpl: list = field(default_factory=list)
    where: str = ""
    patterns: list = field(default_factory=list)     # верхнего уровня: для обратного вывода
    specificity: int = 0
    reads: set = field(default_factory=set)
    writes: set = field(default_factory=set)
    read_preds: set = field(default_factory=set)

    @classmethod
    def from_graph(cls, kb, iri):
        g = kb.g["rules"]
        r = cls(iri=iri, name=str(g.value(iri, RDFS.label)), scope=g.value(iri, EX.scope),
                priority=int(g.value(iri, EX.priority) or 0), source=g.value(iri, EX.source),
                pre=str(g.value(iri, EX.precondition) or ""), cond=str(g.value(iri, EX.conditionText) or ""),
                concl=str(g.value(iri, EX.conclusionText) or ""), post=str(g.value(iri, EX.postAction) or ""),
                core=str(g.value(iri, EX.core) or ""))
        r.parse()
        return r

    def parse(self):
        """Синтаксический разбор ядра средствами rdflib (проверка SPARQL Update)."""
        alg = translateUpdate(parseUpdate(RULE_PREFIXES + self.core))
        op = alg.algebra[0]
        if op.name != "Modify" or op.get("insert") is None:
            raise ValueError("ядро должно иметь вид INSERT { ... } WHERE { ... }")
        self.insert_tpl = list(op["insert"]["triples"])
        _, self.where = split_core(self.core)
        acc = {"patterns": [], "filters": 0}
        _walk(op["where"], acc)
        self.patterns = [t for t, neg, opt in acc["patterns"] if not neg and not opt]
        self.specificity = len(acc["patterns"]) + acc["filters"]
        self.reads = {pattern_key(t) for t, _, _ in acc["patterns"]} - {(RDFS.label, None)}
        self.read_preds = {k[0] for k in self.reads} | {RDFS.subClassOf, RDFS.label}
        self.writes = {pattern_key(t) for t in self.insert_tpl if t[1] != RDFS.label}
        for pr in P.PRODUCES.get(self.post, []):
            self.writes.add((pr, None))

    def prepared(self):
        """Подготовленный (разобранный один раз) запрос SELECT для части WHERE."""
        if getattr(self, "_prep", None) is None:
            self._prep = prepareQuery(self.select_query())
        return self._prep

    def match(self, graph, init: dict | None = None):
        """Все подстановки, при которых выполнено условие правила (строки как dict)."""
        res = graph.query(self.prepared(), initBindings={Variable(k): v for k, v in (init or {}).items()})
        out = []
        for row in res:
            d = row.asdict()
            for k, v in (init or {}).items():
                d.setdefault(k, v)
            out.append(d)
        return out

    def select_query(self, values: dict | None = None) -> str:
        """Часть WHERE как SELECT; values — начальная подстановка (для обратного вывода)."""
        body = self.where
        if values:
            vs = " ".join(f"?{k}" for k in values)
            row = " ".join(v.n3() for v in values.values())
            body = "{ VALUES (" + vs + ") { (" + row + ") } " + body[1:]
        return RULE_PREFIXES + "SELECT * WHERE " + body


# ====================================================================== контекст
class Ctx:
    """Контекст для процедур и демонов: доступ к базе, рабочей памяти и протоколу."""

    def __init__(self, kb, frames, engine=None, wm=None):
        self.kb, self.frames, self.engine, self.wm = kb, frames, engine, wm
        self.inference_requests = []
        self.demon_log = []
        self.last_created = {}
        self.removed_node = None
        self.dangling, self.dependents = [], []
        self.cycle = 0

    def request_inference(self, why):
        self.inference_requests.append(why)

    def add(self, t, source):
        """Добавить выведенный триплет (используется постусловиями правил).
        Запоминается подстановка правила, чьё постусловие выполняется — для объяснения."""
        if self.engine is not None:
            raw = getattr(self, "binding", {}) or {}
            self.engine._assert(t, {"rule": source, "cycle": self.cycle,
                                    "binding": self.engine._short(raw), "raw": raw, "kind": "post"})


# ====================================================================== механизм вывода
class Engine:
    def __init__(self, kb, frames):
        self.kb, self.frames = kb, frames
        self.derivations: dict = {}
        self.trace: list = []
        self.wm: Graph | None = None
        self._rules_cache = {}
        self._load_derivations()

    # ------------------------------------------------------------ правила
    def rules(self):
        res = []
        for iri in self.kb.g["rules"].subjects(RDF.type, EX.Rule):
            core = str(self.kb.g["rules"].value(iri, EX.core) or "")
            key = (iri, core, str(self.kb.g["rules"].value(iri, EX.priority)),
                   str(self.kb.g["rules"].value(iri, EX.scope)))
            if key not in self._rules_cache:
                self._rules_cache[key] = Rule.from_graph(self.kb, iri)
            res.append(self._rules_cache[key])
        return sorted(res, key=lambda r: r.name)

    def rule(self, name):
        return next((r for r in self.rules() if r.name == name), None)

    def functional(self):
        return set(self.frames.fg.subjects(RDF.type, OWL.FunctionalProperty))

    def is_active(self, r: Rule, scopes):
        """Активно ли правило при заданной сфере применения Q."""
        if scopes is None or r.scope is None:
            return True
        return r.scope in scopes or self.frames.is_class(r.scope)

    # ------------------------------------------------------------ рабочая память
    def base_wm(self) -> Graph:
        wm = Graph()
        for g in (self.frames.schema_graph(), self.kb.g["data"], self.frames.materialized_defaults()):
            for t in g:
                wm.add(t)
        return wm

    def _assert(self, t, info):
        if t in self.wm:
            return False
        self.wm.add(t)
        self.kb.g["inferred"].add(t)
        self.derivations[t] = info
        self._new_preds.add(t[1])
        self._added.append((t, info["rule"]))
        return True

    # ------------------------------------------------------------ прямой вывод
    def run_forward(self, scopes=None, goal=None, max_cycles=400, persist=True, label="прямой вывод"):
        """Прямой вывод. scopes — множество сцен/фреймов (сфера Q) или None — все правила.
        goal — (s, p, o): остановиться, когда этот факт выведен."""
        with self.kb.lock:
            old = self.conclusions()
            self.kb.g["inferred"].remove((None, None, None))
            self.derivations = {}
            self.trace = []
            self.wm = self.base_wm()
            functional = self.functional()
            active = [r for r in self.rules() if self.is_active(r, scopes)]
            ctx = Ctx(self.kb, self.frames, self, self.wm)
            fired, blocked_keys = set(), set()
            cache = {}
            dirty = set(r.name for r in active)
            cycle = 0
            goal_reached = False
            while cycle < max_cycles:
                cycle += 1
                for r in active:                       # (1) распознавание
                    if r.name in dirty:
                        cache[r.name] = r.match(self.wm)
                dirty = set()
                acts, blocked_now = [], []
                for r in active:
                    for b in cache.get(r.name, []):
                        key = (r.name, tuple(sorted((k, str(v)) for k, v in b.items())))
                        if key in fired or key in blocked_keys:
                            continue
                        inst = self._instantiate(r.insert_tpl, b)
                        if not any(isinstance(x, BNode) for t in inst for x in t) and all(t in self.wm for t in inst):
                            fired.add(key)                  # заключение уже есть — рефракция
                            continue
                        clash = [t for t in inst if t[1] in functional and not isinstance(t[0], BNode)
                                 and any(o != t[2] for o in self.wm.objects(t[0], t[1]))]
                        if clash:
                            blocked_keys.add(key)
                            s, p, o = clash[0]
                            have = next(o2 for o2 in self.wm.objects(s, p) if o2 != o)
                            who = self.derivations.get((s, p, have), {}).get("rule", "факт")
                            blocked_now.append({"rule": r.name, "binding": self._short(b),
                                                "why": f"слот «{self.kb.label(p)}» у «{self.kb.label(s)}» уже "
                                                       f"= «{self.kb.label(have)}» (по {who})"})
                            continue
                        acts.append({"rule": r, "b": b, "inst": inst, "key": key,
                                     "recency": self._recency(b)})
                if not acts:
                    if blocked_now:
                        self.trace.append({"cycle": cycle, "conflict": [], "chosen": None,
                                           "blocked": blocked_now, "added": [], "post": ""})
                    break
                # (2) выбор: приоритет → специфичность → новизна
                acts.sort(key=lambda a: (-a["rule"].priority, -a["rule"].specificity, -a["recency"], a["rule"].name))
                ch = acts[0]
                reason = self._reason(ch, acts)
                # (3) действие
                self._new_preds, self._added = set(), []
                ctx.cycle, ctx.wm = cycle, self.wm
                ctx.last_created = {}
                digest = hashlib.md5(repr(ch["key"]).encode()).hexdigest()[:6]
                bmap = {}
                for s, p, o in ch["inst"]:
                    def mint(x):
                        if isinstance(x, BNode):
                            if x not in bmap:
                                bmap[x] = EX[f"{ch['rule'].name}_{digest}_{len(bmap) + 1}"]
                                ctx.last_created["_:" + str(x)] = bmap[x]
                            return bmap[x]
                        return x
                    self._assert((mint(s), p, mint(o)),
                                 {"rule": ch["rule"].name, "cycle": cycle, "binding": self._short(ch["b"]),
                                  "raw": ch["b"], "kind": "rule"})
                fired.add(ch["key"])
                post_msg = ""
                ctx.binding, ctx.rule = ch["b"], ch["rule"]
                if ch["rule"].post and ch["rule"].post in P.POST_ACTIONS:
                    post_msg = P.POST_ACTIONS[ch["rule"].post](ctx, ch["b"], ch["rule"])
                self.trace.append({
                    "cycle": cycle,
                    "conflict": [{"rule": a["rule"].name, "priority": a["rule"].priority,
                                  "spec": a["rule"].specificity, "binding": self._short(a["b"])} for a in acts],
                    "chosen": ch["rule"].name, "binding": self._short(ch["b"]), "reason": reason,
                    "added": [self.kb.fmt_triple(t) for t, _ in self._added],
                    "post": f"N = {ch['rule'].post}: {post_msg}" if ch["rule"].post else "",
                    "blocked": blocked_now})
                # (4) пересчитать только правила, читающие изменённые слоты
                dirty = {r.name for r in active if r.read_preds & self._new_preds}
                if goal is not None and goal in self.wm:
                    goal_reached = True
                    break
            if persist:
                self._persist_derivations()
                self.kb.save(["inferred"])
            new = self.conclusions()
            result = {"label": label, "cycles": len(self.trace), "trace": self.trace,
                      "appeared": self._fmt_concl(new - old), "disappeared": self._fmt_concl(old - new),
                      "goal_reached": goal_reached, "n_derived": len(self.derivations)}
            if persist:
                self.last_result = result     # для экрана «Правила и вывод» и выгрузки трассы
            return result

    def evaluate_scoped(self, scopes):
        """Вывод с ограниченной сферой применения Q (для сценария) во временном графе:
        основное содержимое графа inferred и протокол глобального вывода не меняются.
        Возвращает (результат, выведенные триплеты с правилами, граф выведенного)."""
        with self.kb.lock:
            saved_g, saved_d, saved_t, saved_wm = self.kb.g["inferred"], self.derivations, self.trace, self.wm
            tmp = Graph()
            self.kb.g["inferred"] = tmp
            try:
                res = self.run_forward(scopes=scopes, persist=False, label="вывод в сцене сценария")
                derivs = self.derivations
            finally:
                self.kb.g["inferred"] = saved_g
                self.derivations, self.trace, self.wm = saved_d, saved_t, saved_wm
            return res, derivs, tmp

    def _instantiate(self, tpl, b):
        out = []
        for s, p, o in tpl:
            def sub(x):
                if isinstance(x, Variable):
                    return b.get(str(x))
                return x
            s2, p2, o2 = sub(s), sub(p), sub(o)
            if None in (s2, p2, o2):
                continue
            out.append((s2, p2, o2))
        return out

    def _recency(self, b):
        """Новизна: номер цикла самого свежего выведенного факта об узлах подстановки."""
        nodes = {v for v in b.values() if isinstance(v, URIRef)}
        best = 0
        for t, info in self.derivations.items():
            if t[0] in nodes:
                best = max(best, info["cycle"])
        return best

    def _reason(self, ch, acts):
        if len(acts) == 1:
            return "единственное применимое правило"
        second = acts[1]
        r1, r2 = ch["rule"], second["rule"]
        if r1.priority != r2.priority:
            return f"приоритет {r1.priority} > {r2.priority} ({r2.name})"
        if r1.specificity != r2.specificity:
            return f"специфичность {r1.specificity} > {r2.specificity} ({r2.name})"
        if ch["recency"] != second["recency"]:
            return f"новизна фактов: цикл {ch['recency']} > {second['recency']}"
        return "равные оценки — по имени правила"

    def _short(self, b):
        def fmt(v):
            if isinstance(v, URIRef):
                return self.kb.label(v)
            s = str(v)
            if isinstance(v, Literal) and v.datatype in (XSD.decimal, XSD.double) and len(s) > 8:
                return f"{float(s):.3f}"          # длинные дроби округляем для читаемости
            return s
        return {k: fmt(v) for k, v in b.items() if k not in ("lbl", "txt")}

    # ------------------------------------------------------------ хранение вывода
    def _persist_derivations(self):
        """Сохранить в графе inferred, каким правилом получен каждый триплет (узлы ex:Derivation)."""
        g = self.kb.g["inferred"]
        for n, (t, info) in enumerate(sorted(self.derivations.items(), key=lambda x: x[1]["cycle"]), 1):
            d = EX[f"Deriv_{n:04d}"]
            g.add((d, RDF.type, EX.Derivation))
            g.add((d, EX.dSubject, t[0]))
            g.add((d, EX.dPredicate, t[1]))
            g.add((d, EX.dObject, t[2]))
            g.add((d, EX.byRule, Literal(info["rule"])))
            g.add((d, EX.cycle, Literal(info["cycle"])))
            g.add((d, EX.binding, Literal(json.dumps(info["binding"], ensure_ascii=False))))

    def _load_derivations(self):
        g = self.kb.g["inferred"]
        self.derivations = {}
        for d in g.subjects(RDF.type, EX.Derivation):
            t = (g.value(d, EX.dSubject), g.value(d, EX.dPredicate), g.value(d, EX.dObject))
            self.derivations[t] = {"rule": str(g.value(d, EX.byRule)), "cycle": int(g.value(d, EX.cycle) or 0),
                                   "binding": json.loads(str(g.value(d, EX.binding) or "{}")),
                                   "kind": "post" if str(g.value(d, EX.byRule)).startswith("N(") else "rule"}

    SIGNIFICANT = (EX.qualityStatus, EX.needsCleaning, EX.dataStatus, EX.graphReady, EX.objectComplexity,
                   EX.applicableModel, EX.hasConflict, EX.selectedModel, EX.selectedForecastHours,
                   EX.forecastTotalHours, EX.riskLevel, EX.bottleneckCandidate, EX.decision,
                   EX.retrainingRequired, EX.predictedHours, EX.action)

    def conclusions(self) -> set:
        """Значимые заключения (для списка «появились/исчезли» после повторного вывода)."""
        return {(t, info["rule"]) for t, info in self.derivations.items() if t[1] in self.SIGNIFICANT}

    def _fmt_concl(self, items):
        return sorted(f"{self.kb.label(t[0])}: {self.kb.label(t[1])} = {self.kb.label(t[2])}  [{rule}]"
                      for t, rule in items)

    def results(self):
        """Сводка выводов по объектам для экрана «Правила и вывод»."""
        rows = []
        for (s, p, o), info in sorted(self.derivations.items(), key=lambda x: x[1]["cycle"]):
            if p in self.SIGNIFICANT:
                rows.append({"subject": self.kb.label(s), "slot": self.kb.label(p), "value": self.kb.label(o),
                             "rule": info["rule"], "cycle": info["cycle"],
                             "t": "|".join(self.kb.qname(x) if isinstance(x, URIRef) else str(x) for x in (s, p, o))})
        return rows

    def recommendations(self):
        g = self.kb.g["inferred"]
        res = []
        for r in g.subjects(RDF.type, EX.Recommendation):
            t = (r, RDF.type, EX.Recommendation)
            res.append({"id": self.kb.qname(r), "action": str(g.value(r, EX.action) or ""),
                        "to": self.kb.label(g.value(r, EX.addressee)),
                        "for": self.kb.label(g.value(r, EX.forCase)),
                        "rule": self.derivations.get(t, {}).get("rule", "?"),
                        "notified": bool(g.value(r, EX.notified))})
        return res

    # ------------------------------------------------------------ объяснение
    def explain(self, t, depth=0, seen=None, memo=None):
        """«Как это получено?» — дерево из правил и фактов, приведших к триплету t.
        Для выведенного триплета берётся правило и подстановка, сохранённые при выводе,
        шаблоны условия правила конкретизируются и объясняются рекурсивно."""
        seen = seen or set()
        memo = set() if memo is None else memo     # уже раскрытые выводы — не повторяем
        info = self.derivations.get(t)
        node = {"fact": self.kb.fmt_triple(t),
                "text": f"{self.kb.label(t[0])}: {self.kb.label(t[1])} = {self.kb.label(t[2])}", "children": []}
        if info is None:
            src = "факт из базы (слот экземпляра)"
            if depth == 0 or getattr(self, "_defaults", None) is None:
                self._defaults = self.frames.materialized_defaults()
            if t in self._defaults:
                src = "значение по умолчанию, унаследованное по AKO"
            node.update({"kind": "fact", "source": src})
            return node
        if t in seen or t in memo or depth > 12:
            node.update({"kind": "loop", "rule": info["rule"], "source": f"{info['rule']} — см. выше"})
            return node
        seen = seen | {t}
        memo.add(t)
        b = info.get("raw", {})
        if info["kind"] == "post":
            rname = info["rule"][2:-1]
            r = self.rule(rname)
            node.update({"kind": "post", "rule": info["rule"], "cycle": info["cycle"], "binding": info["binding"],
                         "source": f"постусловие N правила {rname}: процедура «{r.post if r else ''}»"})
            if r is not None:
                for t2 in self._instantiate(r.insert_tpl, b):
                    if not any(isinstance(x, BNode) for x in t2):
                        node["children"].append(self.explain(t2, depth + 1, seen, memo))
            return node
        r = self.rule(info["rule"])
        node.update({"kind": "rule", "rule": info["rule"], "cycle": info["cycle"], "binding": info["binding"],
                     "source": f"{info['rule']}: {r.cond} → {r.concl}" if r else info["rule"]})
        if r is None:
            return node
        for s, p, o in r.patterns:
            if isinstance(p, Path) or p in (RDF.type, RDFS.subClassOf, RDFS.label):
                continue
            s2 = b.get(str(s)) if isinstance(s, Variable) else s
            o2 = b.get(str(o)) if isinstance(o, Variable) else o
            if s2 is None or o2 is None:
                continue
            node["children"].append(self.explain((s2, p, o2), depth + 1, seen, memo))
        return node

    def base_wm_with_inferred(self):
        wm = self.base_wm()
        for t in self.kb.g["inferred"]:
            wm.add(t)
        return wm

    def explain_text(self, node, indent=0) -> str:
        pad = "  " * indent
        line = f"{pad}• {node['text']} — {node.get('source', '')}"
        if node.get("binding"):
            line += "  {" + ", ".join(f"{k}={v}" for k, v in node["binding"].items()) + "}"
        return "\n".join([line] + [self.explain_text(c, indent + 1) for c in node.get("children", [])])

    # ------------------------------------------------------------ обратный вывод
    def backward(self, goal, answers: dict | None = None, hypothetical: dict | None = None):
        """Проверка гипотезы goal = (s, p, o) от цели к данным.
        answers — уже полученные от пользователя значения {(s, p): Literal};
        hypothetical — дополнительные факты (например, новый кейс, которого нет в базе).
        Возвращает статус: 'доказана' / 'не доказана' / 'вопрос' и протокол шагов."""
        with self.kb.lock:
            saved_wm, saved_der = self.wm, dict(self.derivations)
            self.wm = self.base_wm()
            for t in self.kb.g["inferred"]:
                self.wm.add(t)
            for t in (hypothetical or {}).get("triples", []):
                self.wm.add(t)
            for (s, p), v in (answers or {}).items():
                self.wm.set((s, p, v))
                if p in (EX.prefixLength, EX.elapsedHours):    # зависимый слот, как демон IF-ADDED
                    n, e = self.wm.value(s, EX.prefixLength), self.wm.value(s, EX.elapsedHours)
                    if n is not None and e is not None:
                        self.wm.set((s, EX.avgWaitHours, P.dec(P._num(e) / max(1, int(n) - 1))))
            self._bw_steps, self._bw_fired = [], set()
            self._new_preds, self._added = set(), []
            ctx = Ctx(self.kb, self.frames, self, self.wm)
            self._bw_ctx = ctx
            # обратный вывод не меняет базу: выведенное пишем во временный граф
            real_inferred = self.kb.g["inferred"]
            tmp = Graph()
            self.kb.g["inferred"] = tmp
            try:
                status = self._prove(goal, [], 0)
            finally:
                self.kb.g["inferred"] = real_inferred
            result = {"goal": f"{self.kb.label(goal[0])}: {self.kb.label(goal[1])} = {self.kb.label(goal[2])}",
                      "steps": self._bw_steps}
            if isinstance(status, dict):
                result.update({"status": "вопрос", "question": status})
            else:
                result["status"] = "доказана" if status else "не доказана"
                if status:
                    result["explanation"] = self.explain_text(self.explain(goal))
            self.wm, self.derivations = saved_wm, saved_der
            return result

    def _ask_wm(self, pattern):
        s, p, o = pattern
        if not isinstance(p, Path) and not isinstance(s, Variable) and not isinstance(o, Variable):
            return (s, p, o) in self.wm
        if not isinstance(p, Path):
            return next(self.wm.triples((None if isinstance(s, Variable) else s, p,
                                         None if isinstance(o, Variable) else o)), None) is not None
        q = RULE_PREFIXES + f"ASK {{ {s.n3()} {p.n3()} {o.n3()} }}"
        return bool(self.wm.query(q).askAnswer)

    def _prove(self, pattern, stack, depth):
        s, p, o = pattern
        text = f"{self.kb.label(s) if not isinstance(s, Variable) else s.n3()} — {self.kb.label(p) if not isinstance(p, Path) else p.n3()} — " \
               f"{self.kb.label(o) if not isinstance(o, Variable) else o.n3()}"
        if self._ask_wm(pattern):
            self._bw_steps.append({"depth": depth, "goal": text, "result": "есть в рабочей памяти"})
            return True
        if depth > 12 or isinstance(p, Path):
            self._bw_steps.append({"depth": depth, "goal": text, "result": "не выводится"})
            return False
        cands = []
        for r in self.rules():
            matched = False
            for ts, tp, to in r.insert_tpl:
                # правило подходит, если его заключение даёт этот слот у существующего узла
                if tp != p or isinstance(ts, BNode):
                    continue
                if not isinstance(to, (Variable, BNode)) and not isinstance(o, Variable) and to != o:
                    continue
                init = {}
                if isinstance(ts, Variable) and not isinstance(s, Variable):
                    init[str(ts)] = s
                if isinstance(to, Variable) and not isinstance(o, Variable):
                    init[str(to)] = o
                cands.append((r, init))
                matched = True
                break
            if not matched and p in P.PRODUCES.get(r.post, []):
                # слот заполняет процедура-постусловие правила (например, прогноз модели)
                init = {}
                if p == EX.forCase and not isinstance(o, Variable):
                    init = {"case": o}
                cands.append((r, init))
        cands.sort(key=lambda x: -x[0].priority)
        if not cands:
            # ни одно правило не выводит этот слот: если субъект известен — спросить пользователя
            if not isinstance(s, Variable) and isinstance(o, Variable) and self._askable(p):
                why = [f"{st['rule']}: {st['goal']}" for st in stack]
                self._bw_steps.append({"depth": depth, "goal": text, "result": "нет в базе — вопрос пользователю"})
                return {"subject": self.kb.qname(s), "subject_label": self.kb.label(s),
                        "slot": self.kb.qname(p), "slot_label": self.kb.label(p),
                        "type": self.frames.prop_info(p)["range_label"], "why": why}
            self._bw_steps.append({"depth": depth, "goal": text, "result": "нет правил с таким заключением"})
            return False
        # если субъект подцели не определён (например, «какой-то прогноз для кейса»),
        # пробуем все правила-кандидаты: каждое может дать свой вариант факта
        exhaustive = isinstance(s, Variable)
        proved = False
        for r, init in cands:
            res = self._try_rule(r, init, pattern, text, stack, depth)
            if isinstance(res, dict):
                return res
            if res:
                proved = True
                if not exhaustive:
                    return True
        return proved

    def _try_rule(self, r, init, pattern, text, stack, depth):
        """Попытка доказать pattern правилом r: если условие уже выполнено — применить
        правило; иначе разбить условие на подцели и доказывать их рекурсивно."""
        self._bw_steps.append({"depth": depth, "goal": text, "result": f"подцель правила {r.name}: {r.cond}"})
        for _ in range(3):
            rows = r.match(self.wm, init)
            if rows:
                if r.post in P.REQUIRES:
                    for need in P.REQUIRES[r.post]:
                        cs = init.get("case") or rows[0].get("case")
                        if cs is not None and self.wm.value(cs, need) is None:
                            q = self._prove((cs, need, Variable("v")), stack + [{"rule": r.name, "goal": text}], depth + 1)
                            if isinstance(q, dict):
                                return q
                for b in rows:
                    self._fire_bw(r, b)
                if self._ask_wm(pattern):
                    self._bw_steps.append({"depth": depth, "goal": text, "result": f"доказано правилом {r.name}"})
                    return True
                break
            progressed = False
            for ps, pp, po in r.patterns:
                ps2 = init.get(str(ps), ps) if isinstance(ps, Variable) else ps
                po2 = init.get(str(po), po) if isinstance(po, Variable) else po
                sub = (ps2, pp, po2)
                if self._ask_wm(sub):
                    continue
                res = self._prove(sub, stack + [{"rule": r.name, "goal": text}], depth + 1)
                if isinstance(res, dict):
                    return res
                if res:
                    progressed = True
                else:
                    break
            if not progressed:
                break
        self._bw_steps.append({"depth": depth, "goal": text, "result": f"{r.name} не применимо: условие не выполнено"})
        return False

    def _askable(self, p):
        info = self.frames.prop_info(p)
        return info["askable"] or (not info["derived"] and not info["object"])

    def _fire_bw(self, r, b):
        key = (r.name, tuple(sorted((k, str(v)) for k, v in b.items())))
        if key in self._bw_fired:
            return
        self._bw_fired.add(key)
        inst = self._instantiate(r.insert_tpl, b)
        digest = hashlib.md5(repr(key).encode()).hexdigest()[:6]
        bmap = {}
        ctx = self._bw_ctx
        ctx.last_created, ctx.wm = {}, self.wm
        for s, p, o in inst:
            def mint(x):
                if isinstance(x, BNode):
                    if x not in bmap:
                        bmap[x] = EX[f"{r.name}_{digest}_{len(bmap) + 1}"]
                        ctx.last_created["_:" + str(x)] = bmap[x]
                    return bmap[x]
                return x
            self._assert((mint(s), p, mint(o)), {"rule": r.name, "cycle": len(self._bw_fired),
                                                 "binding": self._short(b), "raw": b, "kind": "rule"})
        if r.post in P.POST_ACTIONS:
            ctx.binding, ctx.rule = b, r
            P.POST_ACTIONS[r.post](ctx, b, r)

    # ------------------------------------------------------------ проверка нового правила
    def check_rule(self, name, core, scope, priority):
        """Проверка синтаксиса ядра, ссылок на свойства и конфликтов с другими правилами."""
        report = {"syntax": "", "unknown": [], "conflicts": [], "ok": False}
        try:
            tmp = Rule(iri=EX[name], name=name, scope=scope, priority=priority, source=None, pre="", cond="",
                       concl="", post="", core=core)
            tmp.parse()
        except Exception as e:                                   # noqa: BLE001
            report["syntax"] = f"Ошибка синтаксиса SPARQL Update: {e}"
            return report, None
        known = self.frames.all_props() | {RDF.type, RDFS.label, RDFS.subClassOf}
        for t in tmp.insert_tpl + tmp.patterns:
            p = t[1]
            if isinstance(p, URIRef) and p not in known:
                report["unknown"].append(self.kb.qname(p))
        functional = self.functional()
        for r in self.rules():
            if r.name == name:
                continue
            common = {w for w in tmp.writes if any(w[0] == x[0] for x in r.writes)} - {(RDF.type, None), (RDFS.label, None)}
            common = {w for w in common if w[0] not in (RDF.type, RDFS.label, EX.forCase, EX.addressee, EX.action,
                                                            EX.situationOf)}
            if common:
                slots = ", ".join(sorted({self.kb.label(w[0]) for w in common}))
                report["conflicts"].append({"rule": r.name, "priority": r.priority, "slots": slots,
                                            "functional": any(w[0] in functional for w in common)})
        report["ok"] = not report["unknown"]
        return report, tmp

    def dependency_graph(self):
        """Граф зависимостей правил: Ri → Rj, если заключение Ri участвует в условии Rj."""
        rules = self.rules()
        edges = []
        for a in rules:
            for b in rules:
                if a is b:
                    continue
                link = False
                for wp, wo in a.writes:
                    for rp, ro in b.reads:
                        if wp != rp:
                            continue
                        if wp == RDF.type:
                            if wo is not None and ro is not None and self.frames.is_class(wo) and self.frames.is_subclass(wo, ro):
                                link = True
                        elif wo is None or ro is None or wo == ro:
                            link = True
                if link:
                    edges.append((a.name, b.name))
        return [r.name for r in rules], edges

    def chains(self, min_len=3):
        """Цепочки вывода по фактическим срабатываниям: для каждого итогового заключения
        строится дерево объяснения, из него берётся самый длинный путь правил
        (заключение предыдущего правила входит в условие следующего)."""
        def paths(node):
            rule = node.get("rule")
            name = rule[2:-1] if rule and rule.startswith("N(") else rule
            subs = [p for c in node.get("children", []) for p in paths(c)] or [[]]
            return [p + [name] if name and (not p or p[-1] != name) else p for p in subs]
        res = []
        finals = [t for t in self.derivations if t[1] in (EX.decision, EX.riskLevel, EX.retrainingRequired)]
        for t in finals:
            best = max(paths(self.explain(t)), key=len, default=[])
            if len(best) >= min_len:
                res.append({"chain": best, "conclusion": f"{self.kb.label(t[0])}: {self.kb.label(t[1])} = "
                                                          f"{self.kb.label(t[2])}"})
        res.sort(key=lambda x: -len(x["chain"]))
        uniq, seen = [], set()
        for r in res:
            key = tuple(r["chain"])
            if key not in seen:
                seen.add(key)
                uniq.append(r)
        return uniq
