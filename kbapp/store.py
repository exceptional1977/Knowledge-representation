"""
store.py — хранилище базы знаний.

База знаний хранится как набор ИМЕНОВАННЫХ ГРАФОВ RDF в одном наборе данных
rdflib.Dataset. Каждый именованный граф сохраняется в свой файл Turtle:

    semantic  — семантическая сеть (понятия, словарь отношений, связи)
    frames    — фреймовая модель: классы, слоты, аннотации, значения по умолчанию
    data      — фреймы-экземпляры (рабочая память механизма вывода)
    shapes    — формы SHACL
    rules     — продукционные правила (узлы ex:Rule с ядром на SPARQL Update)
    scenarios — фреймы-сценарии и сцены
    inferred  — выведенные триплеты и записи о том, каким правилом они получены
    runs      — экземпляры исполнения сценариев и журналы их шагов
    journal   — журнал изменений базы знаний

При первом запуске рабочие файлы kb/current/*.ttl копируются из kb/seed/*.ttl,
дальше программа работает только с kb/current и сохраняет туда каждое изменение.
"""
from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

from rdflib import Dataset, Graph, Literal, Namespace, URIRef, BNode
from rdflib.namespace import RDF, RDFS, OWL, XSD

# --- пространства имён проекта ---
EX = Namespace("http://bmstu.ru/iu12/ocpa#")
SN = Namespace("http://bmstu.ru/iu12/ocpa/sn#")
SH = Namespace("http://www.w3.org/ns/shacl#")
GRAPH_BASE = "http://bmstu.ru/iu12/ocpa/graph/"

GRAPH_NAMES = ["semantic", "frames", "data", "shapes", "rules", "scenarios",
               "inferred", "runs", "journal"]

# Префиксы, которые подставляются в каждый SPARQL-запрос пользователя и правила
SPARQL_PREFIXES = """PREFIX ex: <http://bmstu.ru/iu12/ocpa#>
PREFIX sn: <http://bmstu.ru/iu12/ocpa/sn#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX sh: <http://www.w3.org/ns/shacl#>
"""

# Таблица транслитерации для построения IRI новых вершин из русских названий
_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o",
                      "p", "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(text: str) -> str:
    """Превращает «Датчик температуры» в «DatchikTemperatury» — локальное имя для IRI."""
    out, up = [], True
    for ch in text.strip():
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.capitalize() if up else t)
            up = False
        elif ch.isalnum() and ch.isascii():
            out.append(ch.upper() if up else ch)
            up = False
        else:
            up = True          # пробел/знак — следующая буква заглавная
    s = "".join(out) or "Node"
    return s if s[0].isalpha() else "N" + s


class KnowledgeBase:
    """Единое хранилище: Dataset с именованными графами + загрузка/сохранение Turtle."""

    def __init__(self, base_dir: str | Path):
        self.base = Path(base_dir)
        self.seed_dir = self.base / "kb" / "seed"
        self.cur_dir = self.base / "kb" / "current"
        self.out_dir = self.base / "out"
        self.out_dir.mkdir(exist_ok=True)
        self.lock = threading.RLock()          # Flask может обслуживать запросы параллельно
        self.author = "И.С. Бронников"          # автор изменений (служебный слот фреймов)
        self.load()

    # ------------------------------------------------------------------ загрузка
    def load(self):
        """Загрузить все именованные графы из kb/current (или из seed при первом запуске)."""
        self.cur_dir.mkdir(parents=True, exist_ok=True)
        self.ds = Dataset()
        self.g: dict[str, Graph] = {}
        for name in GRAPH_NAMES:
            graph = self.ds.graph(URIRef(GRAPH_BASE + name))
            cur = self.cur_dir / f"{name}.ttl"
            seed = self.seed_dir / f"{name}.ttl"
            if not cur.exists() and seed.exists():
                shutil.copy(seed, cur)
            if cur.exists():
                graph.parse(cur, format="turtle")
            self._bind(graph)
            self.g[name] = graph

    @staticmethod
    def _bind(graph: Graph):
        for p, ns in (("ex", EX), ("sn", SN), ("sh", SH), ("owl", OWL), ("rdfs", RDFS), ("xsd", XSD)):
            graph.bind(p, ns, override=True)

    def save(self, names=None):
        """Сохранить указанные (или все) именованные графы в kb/current/*.ttl."""
        with self.lock:
            for name in (names or GRAPH_NAMES):
                self.g[name].serialize(self.cur_dir / f"{name}.ttl", format="turtle")

    def reset_to_seed(self):
        """Вернуть базу знаний к исходному состоянию (используется перед демонстрацией)."""
        with self.lock:
            for name in GRAPH_NAMES:
                cur = self.cur_dir / f"{name}.ttl"
                if cur.exists():
                    cur.unlink()
            log = self.out_dir / "journal.jsonl"
            if log.exists():
                log.unlink()
            self.load()

    # ------------------------------------------------------------------ утилиты
    def union(self, *names) -> Graph:
        """Объединение нескольких именованных графов в одном Graph (для SPARQL)."""
        u = Graph()
        self._bind(u)
        for n in names:
            for t in self.g[n]:
                u.add(t)
        return u

    def label(self, node) -> str:
        """Человекочитаемое имя узла: rdfs:label из любого графа или локальное имя IRI."""
        if node is None:
            return "—"
        if isinstance(node, Literal):
            return str(node)
        if node in getattr(self, "tmp_labels", {}):      # подписи гипотетических узлов обратного вывода
            return self.tmp_labels[node]
        for name in ("semantic", "frames", "data", "scenarios", "rules", "inferred", "runs"):
            lbl = self.g[name].value(node, RDFS.label)
            if lbl is not None:
                return str(lbl)
        return self.qname(node)

    def qname(self, node) -> str:
        s = str(node)
        for pfx, ns in (("ex:", str(EX)), ("sn:", str(SN)), ("rdfs:", str(RDFS)), ("rdf:", str(RDF)),
                        ("owl:", str(OWL)), ("xsd:", str(XSD))):
            if s.startswith(ns):
                return pfx + s[len(ns):]
        return s

    def expand(self, text: str) -> URIRef:
        """'ex:Case_A1001' -> полный IRI; полный IRI возвращается как есть."""
        text = text.strip()
        for pfx, ns in (("ex:", EX), ("sn:", SN), ("rdfs:", RDFS), ("owl:", OWL)):
            if text.startswith(pfx):
                return URIRef(str(ns) + text[len(pfx):])
        return URIRef(text)

    def fmt_triple(self, t) -> str:
        """Короткая запись триплета для панели «Что изменилось»."""
        s, p, o = t
        def f(x):
            if isinstance(x, Literal):
                return f"\"{x}\""
            if isinstance(x, BNode):
                return "[]"
            return self.qname(x)
        return f"{f(s)} {f(p)} {f(o)}"

    def snapshot(self, names) -> dict:
        """Снимок содержимого графов — для вычисления добавленных/удалённых триплетов."""
        return {n: set(self.g[n]) for n in names}

    def diff(self, snap: dict):
        """Сравнить текущие графы со снимком: (добавлено, удалено) списками (граф, триплет)."""
        added, removed = [], []
        for n, before in snap.items():
            now = set(self.g[n])
            added += [(n, t) for t in now - before]
            removed += [(n, t) for t in before - now]
        return added, removed

    def now(self) -> Literal:
        return Literal(datetime.now().replace(microsecond=0).isoformat(), datatype=XSD.dateTime)

    # ------------------------------------------------------------------ журнал
    def journal(self, action: str, summary: dict):
        """Записать изменение в граф journal и в out/journal.jsonl."""
        with self.lock:
            n = len(list(self.g["journal"].subjects(RDF.type, EX.JournalEntry))) + 1
            node = EX[f"J{n:04d}"]
            j = self.g["journal"]
            j.add((node, RDF.type, EX.JournalEntry))
            j.add((node, EX.at, self.now()))
            j.add((node, EX.author, Literal(self.author)))
            j.add((node, RDFS.label, Literal(action)))
            j.add((node, EX.summary, Literal(json.dumps(summary, ensure_ascii=False))))
            with open(self.out_dir / "journal.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"n": n, "at": str(self.now()), "action": action, **summary},
                                    ensure_ascii=False) + "\n")
            return node

    def journal_entries(self, limit=50):
        rows = []
        j = self.g["journal"]
        for node in j.subjects(RDF.type, EX.JournalEntry):
            rows.append({"id": self.qname(node), "at": str(j.value(node, EX.at)),
                         "action": str(j.value(node, RDFS.label)),
                         "summary": json.loads(str(j.value(node, EX.summary) or "{}"))})
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows[:limit]
