"""
mapping.py — связь двух моделей (раздел 7 задания).

Каждый элемент фреймовой модели прослеживается до семантической сети явной
ссылкой ex:semNode (фрейм → вершина), правило — ссылкой ex:source (правило →
вершина группы «Бизнес-правила и ограничения»), сценарий и сцены — ex:semNode
(→ бизнес-процесс). Модуль строит таблицу соответствия и список расхождений.
"""
from __future__ import annotations

from rdflib.namespace import RDF, OWL

from .store import EX, SN

# какие группы понятий сети должны иметь фрейм-прототип
FRAME_GROUPS = (SN.G_Role, SN.G_Info, SN.G_System, SN.G_Phys)


class Mapping:
    def __init__(self, kb, sem, frames, engine):
        self.kb, self.sem, self.frames, self.engine = kb, sem, frames, engine

    def table(self):
        """Таблица: вершина сети — фреймы (классы/экземпляры) — правила — сцены."""
        rows = []
        for c in self.sem.concepts():
            iri = c["iri"]
            classes = [self.kb.label(x) for x in self.kb.g["frames"].subjects(EX.semNode, iri)
                       if (x, RDF.type, OWL.Class) in self.kb.g["frames"]]
            inst = [self.kb.label(x) for x in self.kb.g["data"].subjects(EX.semNode, iri)]
            rules = sorted(self.kb.label(r) for r in self.kb.g["rules"].subjects(EX.source, iri))
            scenes = [self.kb.label(s) for s in self.kb.g["scenarios"].subjects(EX.semNode, iri)]
            if classes or inst or rules or scenes:
                rows.append({"vertex": c["label"], "id": c["id"], "group": self.kb.label(c["group"]),
                             "classes": ", ".join(classes), "instances": ", ".join(inst),
                             "rules": ", ".join(rules), "scenes": ", ".join(scenes)})
        return rows

    def discrepancies(self):
        """Расхождения: фрейм без вершины, правило без источника или с источником не той
        группы, вершина-объект без фрейма, ссылка на несуществующую вершину."""
        out = []
        sem = self.kb.g["semantic"]
        for c in self.frames.frame_classes():
            n = self.kb.g["frames"].value(c, EX.semNode)
            if n is None and not self.frames.parents(c):
                continue                         # корневой служебный фрейм «Сущность» вершины не требует
            if n is None:
                out.append(f"Фрейм «{self.kb.label(c)}» не связан с вершиной семантической сети")
            elif (n, RDF.type, SN.Concept) not in sem:
                out.append(f"Фрейм «{self.kb.label(c)}» ссылается на отсутствующую вершину {self.kb.qname(n)}")
        for r in self.engine.rules():
            if r.source is None:
                out.append(f"Правило {r.name} без источника в семантической сети")
            elif sem.value(r.source, SN.group) != SN.G_Rule:
                out.append(f"Источник правила {r.name} — «{self.kb.label(r.source)}» — не из группы "
                           f"«Бизнес-правила и ограничения»")
        linked = set(self.kb.g["frames"].objects(None, EX.semNode)) | set(self.kb.g["data"].objects(None, EX.semNode))
        for c in self.sem.concepts():
            if c["group"] in FRAME_GROUPS and c["iri"] not in linked:
                # допускается, если у вершины есть предок с фреймом (понятие покрыто обобщённым фреймом)
                anc = [a for a, _ in self.sem.ancestors(c["iri"]) if a in linked]
                if not anc:
                    out.append(f"Вершина «{c['label']}» ({self.kb.label(c['group'])}) не имеет фрейма")
        for sc in self.kb.g["scenarios"].subjects(RDF.type, EX.Scenario):
            n = self.kb.g["scenarios"].value(sc, EX.semNode)
            if n is None or sem.value(n, SN.group) != SN.G_Process:
                out.append(f"Сценарий «{self.kb.label(sc)}» не связан с бизнес-процессом сети")
        return out

    def frame_for_vertex(self, v):
        for gname in ("frames", "data", "scenarios"):
            for x in self.kb.g[gname].subjects(EX.semNode, v):
                return x
        return None
