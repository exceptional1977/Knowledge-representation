"""
semnet.py — Модель 1. Семантическая сеть (взгляд бизнес-аналитика).

Что умеет модуль (раздел 3 задания):
  1) карточка понятия: собственные свойства + унаследованные по «является»
     с указанием понятия-источника;
  2) поиск связи между двумя понятиями: поиск в ширину и поиск в глубину
     написаны вручную (без функций графовых библиотек), возвращают порядок
     обхода, найденный путь и число посещённых вершин;
  3) запросы по образцу на SPARQL с переменными;
  4) проверка сети: циклы в «является», нарушения словаря отношений,
     изолированные вершины, дубликаты;
  5) фильтрация графа по группам понятий и типам отношений.
Добавление вершины и связи сверяется со словарём отношений (sn:Relation).
"""
from __future__ import annotations

from collections import deque

from rdflib import Literal, URIRef, BNode
from rdflib.namespace import RDF, RDFS

from .store import SN, EX, SPARQL_PREFIXES, slugify


class SemanticNet:
    def __init__(self, kb):
        self.kb = kb

    @property
    def g(self):
        return self.kb.g["semantic"]

    # ------------------------------------------------------------ справочники
    def groups(self):
        """Группы понятий с цветами для раскраски графа."""
        res = []
        for grp in self.g.subjects(RDF.type, SN.Group):
            if grp == SN.AnyGroup:
                continue
            res.append({"iri": grp, "id": self.kb.qname(grp), "label": str(self.g.value(grp, RDFS.label)),
                        "color": str(self.g.value(grp, SN.color) or "#ccc"),
                        "order": int(self.g.value(grp, SN.order) or 50)})
        return sorted(res, key=lambda r: r["order"])

    def relations(self) -> dict:
        """Словарь отношений: IRI -> описание (имя, обратное имя, домен, диапазон, транзитивность)."""
        rel = {}
        for r in self.g.subjects(RDF.type, SN.Relation):
            rel[r] = {
                "iri": r, "id": self.kb.qname(r),
                "label": str(self.g.value(r, RDFS.label)),
                "inverse": str(self.g.value(r, SN.inverseLabel)),
                "group": str(self.g.value(r, SN.relGroup)),
                "domain": set(self.g.objects(r, SN.domainGroup)),
                "range": set(self.g.objects(r, SN.rangeGroup)),
                "same_group": bool(self.g.value(r, SN.sameGroup)),
                "transitive": bool(self.g.value(r, SN.transitive)),
                "inherits": bool(self.g.value(r, SN.inheritsAttributes)),
            }
        return rel

    def concepts(self):
        res = []
        for c in self.g.subjects(RDF.type, SN.Concept):
            res.append({"iri": c, "id": self.kb.qname(c), "label": self.kb.label(c),
                        "group": self.g.value(c, SN.group)})
        return sorted(res, key=lambda r: r["label"])

    def group_of(self, c):
        return self.g.value(c, SN.group)

    def edges(self, rel_filter=None):
        """Все связи сети: список (s, p, o) только по отношениям из словаря."""
        rels = self.relations()
        out = []
        for p in rels:
            if rel_filter and p not in rel_filter:
                continue
            for s, o in self.g.subject_objects(p):
                out.append((s, p, o))
        return out

    # ------------------------------------------------------------ проверка по словарю
    def validate_edge(self, s, p, o) -> list[str]:
        """Проверить новую связь по словарю отношений. Пустой список — связь допустима."""
        errs = []
        rels = self.relations()
        if p not in rels:
            return [f"Отношения {self.kb.qname(p)} нет в словаре отношений"]
        r = rels[p]
        for x in (s, o):
            if (x, RDF.type, SN.Concept) not in self.g:
                errs.append(f"Вершины {self.kb.qname(x)} нет в сети")
        if errs:
            return errs
        if s == o:
            errs.append("Связь вершины с самой собой не допускается")
        gs, go = self.group_of(s), self.group_of(o)
        if SN.AnyGroup not in r["domain"] and gs not in r["domain"]:
            errs.append(f"«{r['label']}»: субъект из группы «{self.kb.label(gs)}», а допустимы: "
                        + ", ".join(self.kb.label(x) for x in r["domain"]))
        if SN.AnyGroup not in r["range"] and go not in r["range"]:
            errs.append(f"«{r['label']}»: объект из группы «{self.kb.label(go)}», а допустимы: "
                        + ", ".join(self.kb.label(x) for x in r["range"]))
        if r["same_group"] and gs != go:
            errs.append(f"«{r['label']}» соединяет только понятия одной группы")
        if (s, p, o) in self.g:
            errs.append("Такая связь уже есть (дубликат)")
        if r["transitive"] and self._reachable(o, s, {p}):
            errs.append(f"Связь создаст цикл по транзитивному отношению «{r['label']}»")
        return errs

    def _reachable(self, start, goal, rels) -> bool:
        """Есть ли путь start -> goal по направленным связям из rels (обход в глубину)."""
        stack, seen = [start], set()
        while stack:
            v = stack.pop()
            if v == goal:
                return True
            if v in seen:
                continue
            seen.add(v)
            for p in rels:
                stack.extend(self.g.objects(v, p))
        return False

    # ------------------------------------------------------------ изменения
    def new_concept_iri(self, label: str) -> URIRef:
        base = slugify(label)
        iri, i = SN[base], 2
        while (iri, None, None) in self.g:
            iri, i = SN[f"{base}{i}"], i + 1
        return iri

    def add_concept(self, label, group, definition="", attrs=None) -> URIRef:
        iri = self.new_concept_iri(label)
        self.g.add((iri, RDF.type, SN.Concept))
        self.g.add((iri, RDFS.label, Literal(label, lang="ru")))
        self.g.add((iri, SN.group, group))
        if definition:
            self.g.add((iri, SN.definition, Literal(definition)))
        for name, value in (attrs or []):
            b = BNode()
            self.g.add((iri, SN.attr, b))
            self.g.add((b, SN.attrName, Literal(name)))
            self.g.add((b, SN.attrValue, Literal(value)))
        return iri

    def add_edge(self, s, p, o):
        self.g.add((s, p, o))

    def remove_concept(self, c):
        for b in list(self.g.objects(c, SN.attr)):
            self.g.remove((b, None, None))
        self.g.remove((c, None, None))
        self.g.remove((None, None, c))

    # ------------------------------------------------------------ запрос 1: карточка
    def own_attrs(self, c):
        res = []
        for b in self.g.objects(c, SN.attr):
            res.append((str(self.g.value(b, SN.attrName)), str(self.g.value(b, SN.attrValue))))
        return sorted(res)

    def ancestors(self, c):
        """Предки по «является»/«экземпляр класса» в порядке удалённости (поиск в ширину)."""
        order, seen, q = [], {c}, deque([(c, 0)])
        while q:
            v, d = q.popleft()
            for p in (SN.isA, SN.instanceOf):
                for parent in self.g.objects(v, p):
                    if parent not in seen:
                        seen.add(parent)
                        order.append((parent, d + 1))
                        q.append((parent, d + 1))
        return order

    def card(self, c) -> dict:
        """Всё, что известно о понятии: свойства собственные и унаследованные, связи."""
        rels = self.relations()
        own = self.own_attrs(c)
        names = {n for n, _ in own}
        inherited = []
        for anc, depth in self.ancestors(c):
            for n, v in self.own_attrs(anc):
                # ближайший предок перекрывает дальнего, собственное значение — всех
                if n in names:
                    inherited.append({"name": n, "value": v, "from": self.kb.label(anc),
                                      "from_id": self.kb.qname(anc), "depth": depth, "overridden": True})
                else:
                    names.add(n)
                    inherited.append({"name": n, "value": v, "from": self.kb.label(anc),
                                      "from_id": self.kb.qname(anc), "depth": depth, "overridden": False})
        out_links = [{"rel": rels[p]["label"], "to": self.kb.label(o), "to_id": self.kb.qname(o)}
                     for p in rels for o in self.g.objects(c, p)]
        in_links = [{"rel": rels[p]["inverse"], "to": self.kb.label(s), "to_id": self.kb.qname(s)}
                    for p in rels for s in self.g.subjects(p, c)]
        frames = [{"id": self.kb.qname(f), "label": self.kb.label(f),
                   "kind": "класс" if (f, RDF.type, URIRef("http://www.w3.org/2002/07/owl#Class")) in self.kb.g["frames"] else "экземпляр"}
                  for gname in ("frames", "data", "scenarios") for f in self.kb.g[gname].subjects(EX.semNode, c)]
        rules = [self.kb.label(r) for r in self.kb.g["rules"].subjects(EX.source, c)]
        return {"id": self.kb.qname(c), "label": self.kb.label(c),
                "group": self.kb.label(self.group_of(c)),
                "definition": str(self.g.value(c, SN.definition) or ""),
                "own": own, "inherited": inherited,
                "out": out_links, "in": in_links, "frames": frames, "rules": rules}

    # ------------------------------------------------------------ запрос 2: поиск пути
    def _adjacency(self, rel_filter=None):
        """Неориентированные списки смежности (связь можно пройти в обе стороны).
        Соседи упорядочены по имени, чтобы порядок обхода был воспроизводим."""
        adj = {}
        for s, p, o in self.edges(rel_filter):
            adj.setdefault(s, []).append((o, p, "→"))
            adj.setdefault(o, []).append((s, p, "←"))
        for v in adj:
            adj[v].sort(key=lambda x: self.kb.label(x[0]))
        return adj

    def search(self, start, goal, method="bfs", rel_filter=None) -> dict:
        """Поиск пути между понятиями. method = 'bfs' (в ширину) или 'dfs' (в глубину).
        Реализация собственная: очередь (deque) для BFS, явный стек для DFS."""
        adj = self._adjacency(rel_filter)
        parent = {start: None}            # откуда пришли в вершину (для восстановления пути)
        order = []                        # порядок посещения вершин
        found = False
        if method == "bfs":
            frontier = deque([start])
            while frontier:
                v = frontier.popleft()
                order.append(v)
                if v == goal:
                    found = True
                    break
                for nb, p, d in adj.get(v, []):
                    if nb not in parent:
                        parent[nb] = (v, p, d)
                        frontier.append(nb)
        else:
            # в стеке храним пару (вершина, откуда пришли): родитель фиксируется
            # в момент посещения, поэтому путь соответствует фактическому обходу
            stack = [(start, None)]
            visited = set()
            while stack:
                v, par = stack.pop()
                if v in visited:
                    continue
                visited.add(v)
                parent[v] = par
                order.append(v)
                if v == goal:
                    found = True
                    break
                # кладём соседей в обратном порядке, чтобы первым раскрылся первый по алфавиту
                for nb, p, d in reversed(adj.get(v, [])):
                    if nb not in visited:
                        stack.append((nb, (v, p, d)))
        path = []
        if found:
            v = goal
            while parent.get(v):
                u, p, d = parent[v]
                path.append((u, p, d, v))
                v = u
            path.reverse()
        rels = self.relations()
        return {
            "method": "в ширину" if method == "bfs" else "в глубину",
            "found": found,
            "order": [self.kb.label(v) for v in order],
            "order_ids": [self.kb.qname(v) for v in order],
            "visited": len(order),
            "path": [{"from": self.kb.label(u), "rel": rels[p]["label"] if d == "→" else rels[p]["inverse"],
                      "to": self.kb.label(v), "from_id": self.kb.qname(u), "to_id": self.kb.qname(v)}
                     for u, p, d, v in path],
            "path_ids": [self.kb.qname(start)] + [self.kb.qname(v) for *_, v in path] if found else [],
        }

    # ------------------------------------------------------------ запрос 3: SPARQL по образцу
    TEMPLATES = {
        "Какие роли выполняют процессы, создающие объект X": """# X = «Прогнозная модель»; учитываются виды процессов и объектов (sn:isA*)
SELECT ?role ?process ?object WHERE {
  ?X rdfs:label "Прогнозная модель"@ru .
  ?role sn:performs ?general .
  ?process sn:isA* ?general .
  ?process sn:creates ?object .
  ?object sn:isA* ?X .
}""",
        "Какие модули автоматизируют процессы, регламентируемые нормативом": """SELECT ?module ?process ?norm WHERE {
  ?norm sn:regulates ?process .
  ?module sn:automates ?process .
}""",
        "Последовательность работ (транзитивно «предшествует»)": """SELECT ?a ?b WHERE {
  ?a sn:precedes+ ?b .
  ?a rdfs:label "Сбор журнала событий"@ru .
}""",
        "Все виды понятия «Информационный объект» (транзитивно)": """SELECT ?kind WHERE {
  ?kind sn:isA+ sn:InformationObject .
}""",
        "Участники N-арного события «Эскалация риска SLA»": """SELECT ?role ?participant WHERE {
  sn:SLAEscalation ?r ?participant .
  ?r a sn:Relation ; rdfs:label ?role .
}""",
        "Что расположено в серверной (транзитивно)": """SELECT ?thing WHERE {
  ?thing sn:locatedIn+ sn:DataCenter .
}""",
    }

    def sparql(self, query: str):
        """Выполнить SPARQL SELECT над графом семантической сети; вернуть заголовки и строки."""
        q = query if "PREFIX" in query.upper() else SPARQL_PREFIXES + query
        res = self.g.query(q)
        head = [str(v) for v in res.vars]
        rows = [[self.kb.label(x) if isinstance(x, URIRef) else ("" if x is None else str(x)) for x in row]
                for row in res]
        return head, rows

    # ------------------------------------------------------------ запрос 4: проверка сети
    def check(self) -> dict:
        rels = self.relations()
        problems = {"cycles": [], "dictionary": [], "isolated": [], "duplicates": []}
        # (а) циклы в «является»: поиск в глубину с цветами вершин
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {c["iri"]: WHITE for c in self.concepts()}
        def dfs(v, path):
            color[v] = GRAY
            for parent in self.g.objects(v, SN.isA):
                if color.get(parent) == GRAY:
                    cyc = path[path.index(parent):] + [parent] if parent in path else [v, parent]
                    problems["cycles"].append(" → ".join(self.kb.label(x) for x in cyc))
                elif color.get(parent) == WHITE:
                    dfs(parent, path + [parent])
            color[v] = BLACK
        for c in list(color):
            if color[c] == WHITE:
                dfs(c, [c])
        # (б) связи между понятиями не тех групп, что в словаре
        for s, p, o in self.edges():
            r = rels[p]
            gs, go = self.group_of(s), self.group_of(o)
            bad = ((SN.AnyGroup not in r["domain"] and gs not in r["domain"]) or
                   (SN.AnyGroup not in r["range"] and go not in r["range"]) or
                   (r["same_group"] and gs != go))
            if bad:
                problems["dictionary"].append(f"{self.kb.label(s)} —{r['label']}→ {self.kb.label(o)}: "
                                              f"группы «{self.kb.label(gs)}» → «{self.kb.label(go)}»")
        # (в) изолированные вершины
        linked = set()
        for s, p, o in self.edges():
            linked.add(s); linked.add(o)
        for c in self.concepts():
            if c["iri"] not in linked:
                problems["isolated"].append(c["label"])
        # (г) дубликаты: одинаковые имена вершин и встречные связи несимметричных отношений
        seen = {}
        for c in self.concepts():
            key = c["label"].strip().lower()
            if key in seen:
                problems["duplicates"].append(f"Одинаковое имя: «{c['label']}» ({c['id']} и {seen[key]})")
            seen[key] = c["id"]
        for s, p, o in self.edges():
            if (o, p, s) in self.g and str(s) < str(o):
                problems["duplicates"].append(f"Встречные связи «{rels[p]['label']}»: "
                                              f"{self.kb.label(s)} ⇄ {self.kb.label(o)}")
        problems["ok"] = not any(problems[k] for k in ("cycles", "dictionary", "isolated", "duplicates"))
        return problems

    # ------------------------------------------------------------ статистика объёма
    def stats(self) -> dict:
        rels = self.relations()
        edges = self.edges()
        used = {p for _, p, _ in edges}
        # глубина иерархии «является»: самая длинная цепочка вверх
        def depth(c, seen=()):
            parents = list(self.g.objects(c, SN.isA))
            if not parents or c in seen:
                return 0
            return 1 + max(depth(p, seen + (c,)) for p in parents)
        max_depth = max((depth(c["iri"]) for c in self.concepts()), default=0)
        nary = [self.kb.label(c["iri"]) for c in self.concepts()
                if len({p for p in self.g.predicates(c["iri"], None) if p in rels and
                        rels[p]["group"].startswith("Падежные")}) >= 3]
        return {"concepts": len(self.concepts()), "edges": len(edges), "relation_types": len(used),
                "dictionary_size": len(rels), "isa_depth": max_depth, "nary": nary}
