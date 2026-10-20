"""
procedures.py — демоны и присоединённые процедуры.

Код процедур лежит здесь, а КАКОЙ слот какую процедуру вызывает, записано в графе
знаний аннотациями ex:onAdded / ex:onRemoved / ex:ifNeeded (у свойств-слотов),
ex:postAction (у правил — постусловие N) и ex:actionProc (у сцен сценария).
Программа находит функцию по имени в реестрах ниже.

Каждый демон выполняет реальную работу и возвращает строку-описание сделанного:
  IF-ADDED   update_case_dependents — пересчитывает зависимый слот «среднее ожидание»
             и запускает повторный вывод; run_inference — запускает повторный вывод;
  IF-REMOVED find_dangling_refs     — ищет ссылки и зависимые данные, которые повиснут;
  IF-NEEDED  compute_density, estimate_confidence — вычисляют значение при первом
             обращении к слоту и кэшируют его в графе.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from rdflib import Literal
from rdflib.namespace import RDF, RDFS, XSD

from .store import EX

DEMONS: dict = {}          # имя -> функция демона (IF-ADDED / IF-REMOVED)
IF_NEEDED: dict = {}       # имя -> процедура вычисления значения слота
POST_ACTIONS: dict = {}    # имя -> процедура-постусловие правила (элемент N)
SCENE_ACTIONS: dict = {}   # имя -> процедура действия сцены сценария
PRODUCES: dict = {}        # какие слоты заполняет постусловие (для графа зависимостей правил)
REQUIRES: dict = {}        # какие слоты нужны процедуре (для обратного вывода)


def demon(name):
    def deco(fn):
        DEMONS[name] = fn
        return fn
    return deco


def if_needed(name):
    def deco(fn):
        IF_NEEDED[name] = fn
        return fn
    return deco


def post_action(name, produces=(), requires=()):
    def deco(fn):
        POST_ACTIONS[name] = fn
        PRODUCES[name] = list(produces)
        REQUIRES[name] = list(requires)
        return fn
    return deco


def scene_action(name):
    def deco(fn):
        SCENE_ACTIONS[name] = fn
        return fn
    return deco


def _num(x):
    return None if x is None else Decimal(str(x.toPython()))


def dec(x, places="0.01"):
    return Literal(Decimal(x).quantize(Decimal(places), rounding=ROUND_HALF_UP), datatype=XSD.decimal)


# ============================================================ демоны IF-ADDED
@demon("run_inference")
def run_inference(ctx, s, p, v):
    """После записи значения слота, от которого зависят правила, запускается вывод."""
    ctx.request_inference(f"{ctx.kb.label(p)} = {v}")
    return f"запрошен повторный прямой вывод (изменился слот «{ctx.kb.label(p)}»)"


@demon("update_case_dependents")
def update_case_dependents(ctx, s, p, v):
    """Пересчитать зависимый слот «Среднее ожидание между событиями» = прошедшее время /
    (длина префикса − 1) и запустить вывод: от этих слотов зависят прогноз и риск."""
    d = ctx.kb.g["data"]
    n, e = d.value(s, EX.prefixLength), d.value(s, EX.elapsedHours)
    if n is None or e is None:
        return "зависимый слот не пересчитан: нет длины префикса или прошедшего времени"
    old = d.value(s, EX.avgWaitHours)
    new = dec(_num(e) / max(1, int(n) - 1))
    d.set((s, EX.avgWaitHours, new))
    ctx.request_inference(f"{ctx.kb.label(p)} = {v}")
    return (f"зависимый слот «Среднее ожидание между событиями» пересчитан: "
            f"{old if old is not None else '—'} → {new} ч; запрошен повторный вывод")


# ============================================================ демон IF-REMOVED
@demon("find_dangling_refs")
def find_dangling_refs(ctx, s, p, v):
    """При удалении узла ищет данные, которые от него зависят и станут висячими:
    входящие ссылки, прогнозы/рекомендации/ситуации по нему, исполнения сценариев."""
    node = ctx.removed_node
    refs = ctx.frames.dangling_refs(node)
    deps = set()
    inf = ctx.kb.g["inferred"]
    for prop in (EX.forCase, EX.situationOf):
        deps |= set(inf.subjects(prop, node))
    runs = set(ctx.kb.g["runs"].subjects(EX.runCase, node))
    ctx.dangling = refs
    ctx.dependents = sorted(deps | runs, key=str)
    parts = [f"{r['s_label']} —{r['p_label']}→ {ctx.kb.label(node)}" for r in refs]
    if deps:
        parts.append("выведенные данные: " + ", ".join(ctx.kb.label(x) for x in sorted(deps, key=str)))
    if runs:
        parts.append("исполнения сценария: " + ", ".join(ctx.kb.label(x) for x in runs))
    return ("найдены висячие ссылки и зависимые данные: " + "; ".join(parts)) if parts else \
        "висячих ссылок не найдено"


# ============================================================ IF-NEEDED
@if_needed("compute_density")
def compute_density(ctx, log):
    """Плотность графа «событие–объект»: density = 2|E| / (|V|(|V|−1)), V = события + объекты."""
    d = ctx.kb.g["data"]
    ne, no, e = d.value(log, EX.nEvents), d.value(log, EX.nObjects), d.value(log, EX.nE2O)
    if None in (ne, no, e):
        return None, "нет числа событий, объектов или связей"
    V = int(ne) + int(no)
    dens = Decimal(2 * int(e)) / Decimal(V * (V - 1))
    val = Literal(dens.quantize(Decimal("0.0000000001")), datatype=XSD.decimal)
    d.set((log, EX.graphDensity, val))           # кэшируем вычисленное значение в графе
    return val, f"V = {V}, |E| = {e}"


@if_needed("estimate_confidence")
def estimate_confidence(ctx, fc):
    """Уверенность прогноза = p / (p + MAE модели)."""
    g = ctx.kb.union("data", "inferred")
    p, m = g.value(fc, EX.predictedHours), g.value(fc, EX.byModel)
    mae = g.value(m, EX.mae) if m is not None else None
    if p is None or mae is None:
        return None, "нет прогноза или MAE модели"
    val = dec(_num(p) / (_num(p) + _num(mae)), "0.001")
    ctx.kb.g["inferred"].set((fc, EX.confidence, val))
    return val, f"p = {p}, MAE = {mae}"


# ============================================================ постусловия правил (N)
@post_action("log_event")
def log_event(ctx, b, rule):
    """Записать событие о срабатывании правила в журнал объекта (слот ex:logEntry)."""
    subj = next((v for k, v in b.items() if k in ("case", "log", "m")), None)
    if subj is None:
        return "нет объекта для записи"
    ctx.add((subj, EX.logEntry, Literal(f"{rule.name}: {rule.concl}")), f"N({rule.name})")
    return f"событие записано в журнал «{ctx.kb.label(subj)}»"


@post_action("predict_forecast", produces=[EX.forCase, EX.byModel, EX.predictedHours, RDF.type],
             requires=[EX.prefixLength, EX.elapsedHours, EX.nRelatedObjects])
def predict_forecast(ctx, b, rule):
    """Получить прогноз остаточного времени кейса моделью (вызов ML-/GNN-модуля).
    В прототипе базы знаний инференс заменён детерминированной функцией признаков:
      base = среднее ожидание × max(1, медианная длина трассы − длина префикса)
      прогноз = base × biasFactor × (1 + objectSensitivity × (число объектов − 1)).
    Коэффициенты модели берутся из её фрейма с наследованием через AKO."""
    case, m = b.get("case"), b.get("m")
    wm = ctx.wm
    n, e, k = wm.value(case, EX.prefixLength), wm.value(case, EX.elapsedHours), wm.value(case, EX.nRelatedObjects)
    if None in (n, e, k):
        return "прогноз не получен: не хватает признаков кейса"
    avg = wm.value(case, EX.avgWaitHours)
    avg = _num(avg) if avg is not None else _num(e) / max(1, int(n) - 1)
    log = wm.value(case, EX.inLog)
    med = wm.value(log, EX.medianTraceLength) if log is not None else None
    med = int(med) if med is not None else 20
    bias = _num(wm.value(m, EX.biasFactor) or Literal(1.0))
    sens = _num(wm.value(m, EX.objectSensitivity) or Literal(0.0))
    base = avg * max(1, med - int(n))
    pred = base * bias * (1 + sens * (int(k) - 1))
    fc = EX[f"Fc_{ctx.kb.qname(case).split(':')[1]}_{ctx.kb.qname(m).split(':')[1]}"]
    src = f"N({rule.name})"
    ctx.add((fc, RDF.type, EX.Forecast), src)
    ctx.add((fc, RDFS.label, Literal(f"Прогноз {ctx.kb.label(m)} для {ctx.kb.label(case)}")), src)
    ctx.add((fc, EX.forCase, case), src)
    ctx.add((fc, EX.byModel, m), src)
    ctx.add((fc, EX.predictedHours, dec(pred, "0.1")), src)
    return f"{ctx.kb.label(m)}: прогноз остатка {dec(pred, '0.1')} ч для {ctx.kb.label(case)}"


@post_action("notify_addressee", produces=[EX.notified])
def notify_addressee(ctx, b, rule):
    """Создать фрейм-экземпляр «Уведомление» адресату рекомендации. Канал доставки
    берётся из фрейма роли с наследованием (слот «Канал уведомлений», указатель O)."""
    who = b.get("who")
    rec = next((x for x in ctx.last_created.values() if (x, RDF.type, EX.Recommendation) in ctx.wm), None)
    if who is None or rec is None:
        return "уведомлять некого"
    chan = ctx.frames.resolve_slot(who, ctx.frames.prop_info(EX.notifyChannel))["values"]
    chan = str(chan[0]) if chan else "электронная почта"
    note = EX[f"Note_{str(rec).split('#')[1]}"]
    src = f"N({rule.name})"
    ctx.add((note, RDF.type, EX.Notification), src)
    ctx.add((note, RDFS.label, Literal(f"Уведомление: {ctx.kb.label(who)}")), src)
    ctx.add((note, EX.addressee, who), src)
    ctx.add((note, EX.forCase, rec), src)
    ctx.add((note, EX.channel, Literal(chan)), src)
    ctx.add((rec, EX.notified, Literal(True)), src)
    return f"уведомление для «{ctx.kb.label(who)}» поставлено в очередь ({chan})"


@post_action("schedule_monitoring", produces=[EX.nextCheckHours])
def schedule_monitoring(ctx, b, rule):
    """Назначить кейсу интервал контроля 4 ч (слот «Интервал контроля»)."""
    case = b.get("case")
    ctx.add((case, EX.nextCheckHours, dec(4, "0.1")), f"N({rule.name})")
    if b.get("who") is not None:
        notify_addressee(ctx, b, rule)
    return f"кейс {ctx.kb.label(case)} поставлен на контроль каждые 4 ч"


# ============================================================ действия сцен сценария
@scene_action("bind_case")
def bind_case(ctx, run, case):
    return {"Кейс": ctx.kb.label(case)}


@scene_action("check_quality")
def check_quality(ctx, run, case):
    d = ctx.kb.g["data"]
    log = d.value(case, EX.inLog)
    return {"Журнал": ctx.kb.label(log),
            "Доля пропусков": str(d.value(log, EX.missingRatio)),
            "Доля дубликатов": str(d.value(log, EX.duplicateRatio)),
            "Длина префикса": str(d.value(case, EX.prefixLength))}


@scene_action("extract_features")
def extract_features(ctx, run, case):
    d = ctx.kb.g["data"]
    log = d.value(case, EX.inLog)
    # обращение к слоту «Плотность графа» вызывает демон IF-NEEDED, если значения нет
    dens = d.value(log, EX.graphDensity)
    if dens is None:
        dens, why = compute_density(ctx, log)
        ctx.demon_log.append(f"IF-NEEDED compute_density для «{ctx.kb.label(log)}»: {dens} ({why})")
    return {"Плотность графа событие–объект": str(dens),
            "Среднее ожидание, ч": str(d.value(case, EX.avgWaitHours)),
            "Число связанных объектов": str(d.value(case, EX.nRelatedObjects))}


@scene_action("forecast_kpi")
def forecast_kpi(ctx, run, case):
    return {}


@scene_action("resolve_conflict")
def resolve_conflict(ctx, run, case):
    return {}


@scene_action("assess_risk")
def assess_risk(ctx, run, case):
    return {}


@scene_action("apply_recommendation")
def apply_recommendation(ctx, run, case):
    runs = ctx.kb.g["runs"]
    conf = runs.value(run, EX.ownerConfirmed)
    if conf is not None and str(conf).lower() in ("нет", "no", "false"):
        return {"Отправка": "отменена пользователем"}
    return {"Отправка": "рекомендация передана адресату"}


@scene_action("close_run")
def close_run(ctx, run, case):
    runs = ctx.kb.g["runs"]
    runs.set((run, EX.finishedAt, ctx.kb.now()))
    runs.set((run, EX.runStatus, Literal("завершён")))
    return {"Состояние": "завершён"}
