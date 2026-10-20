"""
tests/demo_protocol.py — воспроизводимый протокол перестроения (раздел 11 задания).

Скрипт работает с ЗАПУЩЕННЫМ приложением (python run.py) через его веб-интерфейс:
отправляет те же формы, что и пользователь, и сохраняет снимки экранов в out/screens
(если установлен playwright), а тексты панели «Что изменилось» — в out/protocol.md.

Запуск:  python run.py   (в одном окне)
         python tests/demo_protocol.py [--port 5000]   (в другом)
"""
from __future__ import annotations

import argparse
import html as htmlmod
import json
import re
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
OUT = BASE_DIR / "out"
SHOTS = OUT / "screens"


class Demo:
    def __init__(self, port):
        self.url = f"http://127.0.0.1:{port}"
        self.s = requests.Session()
        self.md = ["# Протокол перестроения базы знаний", ""]
        self.pw = None
        try:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch()
            self.page = self.browser.new_page(viewport={"width": 1700, "height": 1100}, device_scale_factor=1.5)
        except Exception as e:                                       # noqa: BLE001
            print("playwright недоступен — снимки экранов не делаются:", e)
        SHOTS.mkdir(parents=True, exist_ok=True)

    def post(self, path, data):
        r = self.s.post(self.url + path, data=data, allow_redirects=False)
        loc = r.headers.get("Location", path)
        return loc if loc.startswith("/") else re.sub(r"^https?://[^/]+", "", loc)

    def panel_text(self):
        html = self.s.get(self.url + "/journal").text
        m = re.search(r'<aside class="changes" id="changes">(.*?)</aside>', html, re.S)
        txt = re.sub(r"<[^>]+>", "\n", m.group(1)) if m else ""
        lines = [htmlmod.unescape(x.strip()) for x in txt.splitlines() if x.strip()]
        return "\n".join(lines)

    def shot(self, name, path, element=None, full=False):
        if not self.pw:
            return
        # уникальный параметр, чтобы браузер не взял страницу из кэша при том же адресе
        base, _, anchor = path.partition("#")
        self._n = getattr(self, "_n", 0) + 1
        sep = "&" if "?" in base else "?"
        self.page.goto(self.url + base + f"{sep}_t={self._n}" + (("#" + anchor) if anchor else ""))
        self.page.wait_for_timeout(300)
        if element:
            self.page.locator(element).first.screenshot(path=str(SHOTS / f"{name}.png"))
        else:
            self.page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)

    def note(self, title, extra=""):
        self.md += [f"## {title}", "", "```", self.panel_text(), "```", extra, ""]
        print("✓", title)

    def close(self):
        (OUT / "protocol.md").write_text("\n".join(self.md), encoding="utf-8")
        if self.pw:
            self.browser.close()
            self.pw.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    d = Demo(a.port)
    d.post("/reset", {})
    d.shot("01_semantic_full", "/semantic", element=".svgwrap")
    d.shot("02_semantic_card", "/semantic?node=sn:HeteroGNN")
    d.shot("03_semantic_bfs", "/semantic?from=sn:DataEngineer&to=sn:GoalSLARisk&method=bfs")
    d.shot("04_semantic_filter", "/semantic?g=sn:G_Process&r=sn:precedes&r=sn:during", element=".svgwrap")
    d.shot("05_semantic_query", "/semantic?run_query=1#q", full=True)
    d.shot("06_frames_network", "/frames", element=".graphbox .svgwrap")
    d.shot("07_frames_class", "/frames?cls=ex:GNNModel")
    d.shot("08_frames_instance", "/frames?cls=ex:LoanApplicationCase&inst=ex:Case_A1001")

    # ---------------- Сущность 1: новое понятие «Платёжный документ»
    loc = d.post("/semantic/add_vertex", {
        "label": "Платёжный документ", "group": "sn:G_Info",
        "definition": "Документ об оплате или выдаче средств по кредитной заявке",
        "attr_name": "Реквизиты", "attr_value": "номер, дата, сумма",
        "dir1": "out", "rel1": "sn:isA", "other1": "sn:BusinessObject",
        "dir2": "in", "rel2": "sn:creates", "other2": "sn:ERPSystem",
        "dir3": "in", "rel3": "sn:uses", "other3": "sn:BottleneckAnalysis"})
    d.note("Сущность 1, шаги 1–2. Добавлена вершина «Платёжный документ» и три связи")
    d.shot("10_e1_semantic", loc)
    d.shot("10_e1_panel", loc, element="#changes")
    d.post("/semantic/add_edge", {"s": "sn:ProcessOwner", "rel": "sn:creates", "o": "sn:Forecast"})
    d.note("Сущность 1, шаг 1. Связь, нарушающая словарь отношений, отклонена")
    d.shot("11_e1_reject", "/semantic?node=sn:ProcessOwner", element="#changes")
    loc = d.post("/semantic/create_frame", {"vertex": "sn:PlatezhnyyDokument", "parent": "ex:InformationObject"})
    d.note("Сущность 1, шаг 3. По предложению создан фрейм-прототип «Платёжный документ»")
    d.shot("12_e1_frame", loc)

    # ---------------- Сущность 2: новый фрейм-экземпляр «Кейс Application_1005»
    base = {"cls": "ex:LoanApplicationCase", "label": "Кейс Application_1005",
            "slot__ex:caseId": "Application_1005", "slot__ex:prefixLength": "9",
            "slot__ex:elapsedHours": "520", "slot__ex:nRelatedObjects": "3", "slot__ex:nObjectTypes": "2",
            "slot__ex:bottleneckActivity": "W_Complete application"}
    d.post("/frames/add_instance", base)
    d.note("Сущность 2, шаг 1. Экземпляр без обязательного слота «Журнал» — SHACL не дал сохранить")
    d.shot("20_e2_shacl", "/frames?cls=ex:LoanApplicationCase", element="#changes")
    loc = d.post("/frames/add_instance", {**base, "slot__ex:inLog": "ex:Log_BPIC2017"})
    d.note("Сущность 2, шаги 2–7. Экземпляр создан: наследование, ризонер, IF-ADDED, повторный вывод")
    d.shot("21_e2_instance", loc)
    d.shot("21_e2_panel", loc, element="#changes")

    # ---------------- Правила: вывод, объяснение, обратный вывод
    d.post("/rules/forward", {})
    tr = d.s.get(d.url + "/rules/trace").text
    (OUT / "forward_trace_baseline.md").write_text(tr, encoding="utf-8")
    d.shot("30_rules", "/rules?rule=R20")
    d.shot("31_explain", "/rules?explain=ex:Case_A1001|ex:decision|ex:EscalateToOwner#expl", element="#expl")
    d.post("/rules/backward", {"case": "ex:Case_A1001", "new_case": "Application_2001", "slot": "ex:riskLevel",
                               "value": "ex:HighRisk"})
    d.shot("32_backward_q", "/rules?v=1#backward", element="#backward")
    for slot, val in (("ex:elapsedHours", "480"), ("ex:prefixLength", "10"), ("ex:nRelatedObjects", "5")):
        html = d.s.get(d.url + "/rules").text
        m = re.search(r'name="slot" value="(ex:[A-Za-z]+)"', html)
        q = m.group(1) if m else slot
        ans = {"ex:elapsedHours": "480", "ex:prefixLength": "10", "ex:nRelatedObjects": "5"}.get(q, val)
        subj = re.search(r'name="subject" value="([^"]+)"', html)
        d.post("/rules/bw_answer", {"subject": subj.group(1) if subj else "ex:Hyp_Application_2001",
                                    "slot": q, "value": ans})
    d.shot("33_backward_done", "/rules?v=2#backward", element="#backward")
    (OUT / "backward_trace.md").exists()

    # ---------------- Сценарий: полный прогон для A1001 и прогон, остановленный после сцены 5
    loc = d.post("/scenarios/start", {"sc": "ex:Sc_Monitoring"})
    run1 = re.search(r"run=([^&]+)", loc).group(1).replace("%3A", ":")
    d.post("/scenarios/answer", {"sc": "ex:Sc_Monitoring", "run": run1, "slot": "ex:runCase", "value": "ex:Case_A1001"})
    for _ in range(6):
        d.post("/scenarios/step", {"sc": "ex:Sc_Monitoring", "run": run1})
    d.post("/scenarios/answer", {"sc": "ex:Sc_Monitoring", "run": run1, "slot": "ex:ownerConfirmed", "value": "да"})
    d.post("/scenarios/run_all", {"sc": "ex:Sc_Monitoring", "run": run1})
    d.s.get(d.url + f"/scenarios/export?run={run1}")
    d.shot("40_scenario_full", f"/scenarios?sc=ex:Sc_Monitoring&run={run1}", full=True)
    loc = d.post("/scenarios/start", {"sc": "ex:Sc_Monitoring"})
    run2 = re.search(r"run=([^&]+)", loc).group(1).replace("%3A", ":")
    d.post("/scenarios/answer", {"sc": "ex:Sc_Monitoring", "run": run2, "slot": "ex:runCase", "value": "ex:Case_A1001"})
    for _ in range(6):
        d.post("/scenarios/step", {"sc": "ex:Sc_Monitoring", "run": run2})
    d.shot("41_scenario_before", f"/scenarios?sc=ex:Sc_Monitoring&run={run2}", element=".graphbox")

    # ---------------- Изменение значения слота: дрейф GNN
    d.post("/frames/set_slot", {"inst": "ex:GNN_v1", "cls": "ex:HeteroGNNModel", "slot": "ex:driftScore", "value": "0.27"})
    d.note("Изменение слота: индекс дрейфа HeteroGNN v1 = 0,27 (IF-ADDED → вывод → пересчёт ветвления)")
    d.shot("42_drift_panel", "/frames?cls=ex:HeteroGNNModel&inst=ex:GNN_v1", element="#changes")
    d.shot("43_scenario_after", f"/scenarios?sc=ex:Sc_Monitoring&run={run2}", element=".graphbox")
    d.post("/scenarios/run_all", {"sc": "ex:Sc_Monitoring", "run": run2})
    d.s.get(d.url + f"/scenarios/export?run={run2}")
    d.post("/frames/set_slot", {"inst": "ex:GNN_v1", "cls": "ex:HeteroGNNModel", "slot": "ex:driftScore", "value": "1.7"})
    d.note("Изменение слота с нарушением диапазона (R): индекс дрейфа = 1,7 — SHACL отклонил")

    # ---------------- Сущность 3: новое правило R29
    core = ("INSERT { ?case ex:riskLevel ex:HighRisk }\nWHERE {\n  ?case ex:elapsedHours ?e ; ex:slaNorm ?n .\n"
            "  ?n ex:thresholdHours ?thr .\n  FILTER (?e > ?thr)\n}")
    d.post("/rules/add", {"name": "R29", "scope": "ex:S5", "priority": "50", "source": "sn:SLANorm",
                          "pre": "кейс выбран для мониторинга",
                          "cond": "ЕСЛИ прошедшее время кейса уже больше норматива SLA",
                          "concl": "ТО риск нарушения SLA = высокий (без прогноза)", "post": "log_event", "core": core})
    d.note("Сущность 3. Добавлено правило R29 (проверка синтаксиса, ссылок, конфликтов, повторный вывод)")
    d.shot("50_e3_rule", "/rules?rule=R29")
    d.shot("50_e3_panel", "/rules?rule=R29", element="#changes")
    d.post("/frames/set_slot", {"inst": "ex:Case_A1004", "cls": "ex:LoanApplicationCase",
                                "slot": "ex:elapsedHours", "value": "740"})
    d.note("Сущность 3, проверка: у кейса Application_1004 прошло 740 ч — R29 срабатывает")
    d.shot("51_e3_fired", "/frames?cls=ex:LoanApplicationCase&inst=ex:Case_A1004", element="#changes")

    # ---------------- Вставка прототипа в середину иерархии
    d.post("/frames/add_class", {"label": "Ансамблевая модель", "parent": "ex:PredictiveModel",
                                 "children": "ex:XGBoostModel", "sem": "sn:XGBModel",
                                 "def_slot": "ex:inputRepresentation", "def_value": "табличные признаки префикса"})
    d.note("Вставлен фрейм-прототип «Ансамблевая модель» между «Прогнозная модель» и «Модель XGBoost»")
    d.shot("60_insert_class", "/frames?cls=ex:AnsamblevayaModel")
    d.shot("60_insert_panel", "/frames?cls=ex:AnsamblevayaModel", element="#changes")

    # ---------------- Удаление сущности
    d.post("/frames/delete", {"node": "ex:KeysApplication1005", "cls": "ex:LoanApplicationCase", "mode": "check"})
    d.note("Удаление «Кейс Application_1005»: IF-REMOVED нашёл висячие ссылки, предложен выбор")
    d.shot("70_delete_check", "/frames?cls=ex:LoanApplicationCase", element="#changes")
    d.post("/frames/delete", {"node": "ex:KeysApplication1005", "cls": "ex:LoanApplicationCase", "mode": "cascade"})
    d.note("Удаление «Кейс Application_1005» каскадом, вывод пересчитан")
    d.shot("71_delete_done", "/frames?cls=ex:LoanApplicationCase", element="#changes")
    d.shot("80_mapping", "/mapping", full=True)
    d.shot("81_journal", "/journal")
    d.close()
    print("Готово: out/protocol.md, out/screens/*.png")


if __name__ == "__main__":
    main()
