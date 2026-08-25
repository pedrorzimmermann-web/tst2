#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import html
import json
import re
import sys
from datetime import datetime, timedelta
from collections import defaultdict, Counter

from rules import RULES

# -----------------------------
# Config
# -----------------------------
# CATEGORIES/CATEGORY_LABELS vêm de rules.py — para adicionar ou ajustar
# uma regra de categorização, edite aquele arquivo (ele tem um "README"
# comentado explicando como). A ordem de RULES define tanto a prioridade
# de ranking quanto a ordem padrão das colunas nos relatórios.
CATEGORIES = [r["category"] for r in RULES]
CATEGORY_LABELS = {r["category"]: r["label"] for r in RULES}
_COMPILED_RULES = [
    {**r, "regex": re.compile(r["pattern"], re.IGNORECASE)} for r in RULES
]

# Colunas fixas (não configuráveis) que sempre precedem as de categoria
# nas tabelas do HTML + margem de segurança para o colspan das linhas
# de drill-down.
FIXED_COLUMNS_HTML = 5
DETAIL_COLSPAN = FIXED_COLUMNS_HTML + len(CATEGORIES) + 4

# Máximo de eventos exibidos POR CATEGORIA no drill-down do HTML (em vez de
# um corte único por host). Evita tanto relatórios gigantes quanto o caso de
# uma categoria muito frequente (ex.: 2.000 eventos de GPU) afogar as demais
# — cada categoria identificada garante sua cota de exemplos mais recentes.
MAX_EVENTS_PER_CATEGORY = 10

# Marca o início de cada sub-evento dentro de "Detalhes" (ex.: "[2026-08-07 10:41:01]")
RE_EVENT_MARKER = re.compile(r"(\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\])")

def humanize_detail(detail: str) -> str:
    """
    "Detalhes" do Monit concatena vários sub-eventos numa única string,
    cada um iniciado por "[AAAA-MM-DD HH:MM:SS]". Aqui a gente normaliza
    espaços/quebras internas e reinsere uma quebra de linha antes de cada
    marcador, para exibição legível no drill-down.
    """
    d = (detail or "").replace("\r", "").strip()
    d = re.sub(r"\s+", " ", d)

    parts = RE_EVENT_MARKER.split(d)
    if len(parts) <= 1:
        return d

    lines = []
    if parts[0].strip():
        lines.append(parts[0].strip())
    for i in range(1, len(parts), 2):
        marker = parts[i]
        rest = parts[i + 1] if i + 1 < len(parts) else ""
        lines.append((marker + rest).strip())

    return "\n".join(lines)

# Nível de severidade identificado dentro de cada linha do histórico (o
# marcador "[ CRITICAL ]", "[ WARNING ]" etc. que o Monit imprime logo
# após o timestamp). Usado só para colorir a linha no drill-down do HTML —
# não tem relação com as categorias de rules.py. Ordem importa: a primeira
# regra que bater define a cor da linha.
LOG_LEVEL_RULES = [
    ("recovery-fail", re.compile(r"\[\s*RECOVERY\s*FAIL(?:ED)?\s*\]", re.IGNORECASE)),
    ("critical", re.compile(r"\[\s*CRITICAL\s*\]", re.IGNORECASE)),
    ("warning", re.compile(r"\[\s*WARNING\s*\]", re.IGNORECASE)),
]

def classify_log_level(line: str):
    for level, rx in LOG_LEVEL_RULES:
        if rx.search(line):
            return level
    return None

def render_detail_html(detail: str) -> str:
    """
    Renderiza o texto de "Detalhes" (já com uma linha por sub-evento, via
    humanize_detail) como HTML, colorindo cada linha inteira conforme o
    nível de severidade encontrado nela (ver LOG_LEVEL_RULES). Preserva as
    quebras de linha reais para o CSS "white-space: pre-wrap" do
    .detail-cell continuar funcionando.
    """
    spans = []
    for line in detail.split("\n"):
        level = classify_log_level(line)
        css_class = f"log-line log-{level}" if level else "log-line"
        spans.append(f"<span class='{css_class}'>{html.escape(line)}</span>")
    return "\n".join(spans)

def parse_dt(s: str):
    # formato do CSV: "2026-08-17 09:41:34"
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def categorize(details: str):
    """
    Aplica as regras de rules.py na ordem definida lá. Pode retornar
    múltiplas categorias (ex.: GPU + IB no mesmo evento) — útil para
    reincidência por tipo. "exclude_if_matched" permite que uma regra
    seja pulada se outra categoria já tiver batido no mesmo evento.
    """
    d = details or ""
    cats = set()

    for rule in _COMPILED_RULES:
        if any(dep in cats for dep in rule.get("exclude_if_matched", ())):
            continue
        if rule["regex"].search(d):
            cats.add(rule["category"])

    return cats

def read_monit_csv(path: str):
    """
    CSV com colunas:
    Cluster, Hostname, Date, Last Update, Status, Detalhes
    Detalhes pode ter novas linhas dentro de aspas => csv.reader dá conta.
    """
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or len(header) < 6:
            raise ValueError("Header inválido ou arquivo não compatível com o export do Monit.")

        for r in reader:
            if len(r) < 6:
                continue
            cluster, hostname, date_s, last_update, status, details = r[:6]
            rows.append({
                "Cluster": cluster.strip(),
                "Hostname": hostname.strip(),
                "Date": date_s.strip(),
                "Last Update": last_update.strip(),
                "Status": status.strip(),
                "Detalhes": details
            })
    return rows

def aggregate_events(rows, top_n: int):
    """
    Agrega uma lista de eventos (já filtrados por data) em contadores por
    host, amostras de evidência e a lista completa de eventos por host
    (usada no drill-down do HTML). Serve tanto para o CSV inteiro quanto
    para o subconjunto de linhas de um único cluster.
    """
    per_host_total = Counter()
    per_host_matched = Counter()
    per_host_cat = {c: Counter() for c in CATEGORIES}
    samples = defaultdict(lambda: defaultdict(list))
    host_events = defaultdict(list)

    for r in rows:
        host = r["Hostname"]
        per_host_total[host] += 1

        cats = categorize(r["Detalhes"])
        if cats:
            # conta a LINHA (não a categoria) — um evento com 2+ categorias
            # ainda soma só 1 aqui, por isso pode ser menor que a soma das
            # colunas de categoria individualmente.
            per_host_matched[host] += 1
        for c in cats:
            per_host_cat[c][host] += 1
            if len(samples[host][c]) < 3:
                snippet = (r["Detalhes"] or "").replace("\r", "").strip()
                snippet = re.sub(r"\s+", " ", snippet)
                if len(snippet) > 220:
                    snippet = snippet[:220] + "..."
                samples[host][c].append(f"{r['Date']} | {snippet}")

        host_events[host].append({
            "date": r["Date"],
            "dt": r["Date_dt"],
            "status": r["Status"],
            "cats": cats,
            "detail": humanize_detail(r["Detalhes"]),
        })

    # Ordenação: segue a prioridade definida pela ordem de CATEGORIES
    # (rules.py), com o total de eventos como critério de desempate final.
    def sort_key(h):
        return tuple(per_host_cat[c][h] for c in CATEGORIES) + (per_host_total[h],)

    hosts_sorted = sorted(per_host_total.keys(), key=sort_key, reverse=True)

    totals = {
        "hosts_afetados": len(hosts_sorted),
        "total_events": sum(per_host_total.values()),
        "matched_events": sum(per_host_matched.values()),
    }
    for c in CATEGORIES:
        totals[c] = sum(per_host_cat[c].values())

    return {
        "hosts_sorted": hosts_sorted,
        "per_host_total": per_host_total,
        "per_host_matched": per_host_matched,
        "per_host_cat": per_host_cat,
        "samples": samples,
        "host_events": host_events,
        "totals": totals,
        "top_n": top_n,
    }

def render_scope_report_text(agg, top_n: int, title: str = None):
    lines = []
    if title:
        lines.append(title)
    lines.append(f"TOP {top_n} hosts (consolidado):")
    lines.append("(total = todas as linhas do Monit no período; eventos_c_regra = quantas bateram alguma categoria)")
    lines.append("Hostname | total | eventos_c_regra | " + " | ".join(CATEGORIES))
    for h in agg["hosts_sorted"][:top_n]:
        cat_values = " | ".join(str(agg["per_host_cat"][c][h]) for c in CATEGORIES)
        lines.append(f"{h} | {agg['per_host_total'][h]} | {agg['per_host_matched'][h]} | {cat_values}")

    lines.append("")
    lines.append("TOP por categoria:")
    for c in CATEGORIES:
        lines.append(f"- {CATEGORY_LABELS[c]}:")
        top = agg["per_host_cat"][c].most_common(top_n)
        if not top:
            lines.append("  (sem ocorrências)")
        else:
            for h, v in top:
                lines.append(f"  {h}: {v}")

    lines.append("")
    lines.append("Evidências (amostras por host/categoria – até 3 exemplos):")
    for h in agg["hosts_sorted"][:top_n]:
        for c in CATEGORIES:
            if agg["per_host_cat"][c][h] > 0:
                lines.append(f"* {h} / {CATEGORY_LABELS[c]} ({agg['per_host_cat'][c][h]} ocorrências):")
                for s in agg["samples"][h][c]:
                    lines.append(f"  - {s}")

    return lines

def generate_reports(rows, days_window: int, top_n: int = 15):
    now = datetime.now()
    start = now - timedelta(days=days_window)

    # Filtra por Date
    filtered = []
    for r in rows:
        dt = parse_dt(r["Date"])
        if dt is None:
            continue
        if dt >= start and dt <= now:
            r["Date_dt"] = dt
            filtered.append(r)

    clusters = sorted({(r["Cluster"] or "(sem cluster)") for r in filtered})
    multi_cluster = len(clusters) > 1

    date_min = min((r["Date_dt"] for r in filtered), default=None)
    date_max = max((r["Date_dt"] for r in filtered), default=None)

    report = []
    report.append("RELATÓRIO – REINCIDÊNCIA DE EVENTOS CRÍTICOS (Monit)")
    report.append(f"Janela analisada (últimos {days_window} dias): {start:%Y-%m-%d %H:%M:%S} até {now:%Y-%m-%d %H:%M:%S}")
    report.append(f"Eventos considerados (linhas filtradas): {len(filtered)}")
    if date_min and date_max:
        report.append(f"Range de datas dentro do CSV: {date_min:%Y-%m-%d %H:%M:%S} até {date_max:%Y-%m-%d %H:%M:%S}")
    report.append("")

    global_agg = None
    clusters_sorted = None
    cluster_aggs = None

    if not multi_cluster:
        # Comportamento original: um único cluster (ou nenhum informado) não
        # precisa de segregação, a visão principal continua consolidada.
        global_agg = aggregate_events(filtered, top_n)
        report.extend(render_scope_report_text(global_agg, top_n))

        summary_lines = [["Hostname", "total_events", "matched_events"] + CATEGORIES]
        for h in global_agg["hosts_sorted"]:
            t = global_agg["per_host_total"]
            m = global_agg["per_host_matched"]
            c = global_agg["per_host_cat"]
            summary_lines.append([h, str(t[h]), str(m[h])] + [str(c[cat][h]) for cat in CATEGORIES])
    else:
        cluster_rows = defaultdict(list)
        for r in filtered:
            cluster_rows[r["Cluster"] or "(sem cluster)"].append(r)
        cluster_aggs = {c: aggregate_events(cluster_rows[c], top_n) for c in clusters}

        def cluster_sort_key(c):
            t = cluster_aggs[c]["totals"]
            return tuple(t[cat] for cat in CATEGORIES) + (t["total_events"],)

        clusters_sorted = sorted(clusters, key=cluster_sort_key, reverse=True)

        report.append(f"ÍNDICE DE CLUSTERS ({len(clusters_sorted)}):")
        report.append("(total = todas as linhas do Monit no período; eventos_c_regra = quantas bateram alguma categoria)")
        report.append("Cluster | hosts_afetados | total | eventos_c_regra | " + " | ".join(CATEGORIES))
        for c in clusters_sorted:
            t = cluster_aggs[c]["totals"]
            cat_values = " | ".join(str(t[cat]) for cat in CATEGORIES)
            report.append(f"{c} | {t['hosts_afetados']} | {t['total_events']} | {t['matched_events']} | {cat_values}")
        report.append("")

        for c in clusters_sorted:
            report.append("=" * 60)
            report.append(f"CLUSTER: {c}")
            report.append("=" * 60)
            report.extend(render_scope_report_text(cluster_aggs[c], top_n))
            report.append("")

        summary_lines = [["Cluster", "Hostname", "total_events", "matched_events"] + CATEGORIES]
        for c in clusters_sorted:
            agg = cluster_aggs[c]
            t = agg["per_host_total"]
            m = agg["per_host_matched"]
            pc = agg["per_host_cat"]
            for h in agg["hosts_sorted"]:
                summary_lines.append([c, h, str(t[h]), str(m[h])] + [str(pc[cat][h]) for cat in CATEGORIES])

    report.append("")
    report.append("Recomendação operacional:")
    report.append("- Abrir registros individuais para os hosts com reincidência muito acima dos demais, priorizando as categorias no topo da lista de regras.")
    report.append("- Tratar aos poucos (1 host por vez) junto ao fornecedor, anexando este relatório e os logs do host quando necessário.")

    return {
        "summary_lines": summary_lines,
        "report_txt": "\n".join(report),
        "multi_cluster": multi_cluster,
        "global_agg": global_agg,
        "clusters_sorted": clusters_sorted,
        "cluster_aggs": cluster_aggs,
    }

def write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerows(rows)

def write_text(path, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def render_markdown_scope_table(agg, top_n: int):
    lines = []
    lines.append(
        "| Hostname | Total | Eventos c/ regra | "
        + " | ".join(CATEGORY_LABELS[c] for c in CATEGORIES) + " |"
    )
    lines.append("|---|---:|---:|" + "---:|" * len(CATEGORIES))
    for h in agg["hosts_sorted"][:top_n]:
        t = agg["per_host_total"]
        m = agg["per_host_matched"]
        c = agg["per_host_cat"]
        cat_values = " | ".join(str(c[cat][h]) for cat in CATEGORIES)
        lines.append(f"| {h} | {t[h]} | {m[h]} | {cat_values} |")
    return lines

def render_markdown_report(report_data: dict, top_n: int = 15):
    md = []
    md.append("# Relatório – Reincidência de eventos críticos (Monit)")
    md.append("")

    if report_data["multi_cluster"]:
        md.append("## Índice de clusters")
        md.append("")
        md.append(
            "| Cluster | Hosts afetados | Total | Eventos c/ regra | "
            + " | ".join(CATEGORY_LABELS[c] for c in CATEGORIES) + " |"
        )
        md.append("|---|---:|---:|---:|" + "---:|" * len(CATEGORIES))
        for c in report_data["clusters_sorted"]:
            t = report_data["cluster_aggs"][c]["totals"]
            cat_values = " | ".join(str(t[cat]) for cat in CATEGORIES)
            md.append(f"| {c} | {t['hosts_afetados']} | {t['total_events']} | {t['matched_events']} | {cat_values} |")
        md.append("")

        for c in report_data["clusters_sorted"]:
            md.append(f"## Cluster: {c}")
            md.append("")
            md.extend(render_markdown_scope_table(report_data["cluster_aggs"][c], top_n))
            md.append("")
    else:
        md.append("## Ranking consolidado (Top hosts)")
        md.append("")
        md.extend(render_markdown_scope_table(report_data["global_agg"], top_n))
        md.append("")

    md.append("## Detalhes e evidências (texto)")
    md.append("```")
    md.append(report_data["report_txt"].strip())
    md.append("```")
    md.append("")
    return "\n".join(md)

def select_sample_events(events: list) -> tuple:
    """
    Escolhe quais eventos (já filtrados só os que bateram alguma regra)
    entram no drill-down: até MAX_EVENTS_PER_CATEGORY exemplos mais
    recentes de CADA categoria identificada, em vez de um corte único por
    host — assim uma categoria muito frequente não afoga as demais.
    Um evento com mais de uma categoria conta cota em todas elas.
    Retorna (eventos_selecionados, quantidade_omitida).
    """
    per_category_count = Counter()
    shown = []
    for e in events:
        if not any(per_category_count[c] < MAX_EVENTS_PER_CATEGORY for c in e["cats"]):
            continue
        for c in e["cats"]:
            per_category_count[c] += 1
        shown.append(e)

    return shown, len(events) - len(shown)

def render_host_detail_table(host: str, host_events: dict):
    """
    Monta a sub-tabela HTML com o histórico de um host (usada no
    drill-down ao clicar na linha do host no TOP N). Eventos que não
    bateram nenhuma regra de categorização ("sem correspondência") são
    ignorados aqui — só interessa dar exemplo do que foi identificado.
    """
    events = sorted(host_events.get(host, []), key=lambda e: e["dt"], reverse=True)
    matched_events = [e for e in events if e["cats"]]
    shown, omitted_matched = select_sample_events(matched_events)
    ignored_no_match = len(events) - len(matched_events)

    rows_html = []
    for e in shown:
        ordered_cats = sorted(e["cats"], key=lambda c: CATEGORIES.index(c))
        cats_text = ", ".join(CATEGORY_LABELS[c] for c in ordered_cats)
        cats_html = "".join(
            f"<span class='badge badge-match'>{html.escape(CATEGORY_LABELS[c])}</span>" for c in ordered_cats
        )
        rows_html.append(
            "<tr>"
            f"<td class='mono'>{html.escape(e['date'])}</td>"
            f"<td>{html.escape(e['status'])}</td>"
            f"<td class='searchable' data-search='{html.escape(cats_text.lower())}'>{cats_html}</td>"
            f"<td class='detail-cell searchable'>{render_detail_html(e['detail'])}</td>"
            "</tr>"
        )

    if not events:
        return "<p class='note-small'>Nenhum evento no período.</p>"

    if not matched_events:
        return (
            f"<p class='note-small'>Nenhum dos {len(events)} eventos deste host "
            "correspondeu a uma regra de categorização.</p>"
        )

    note_parts = [
        f"Exemplos por categoria (até {MAX_EVENTS_PER_CATEGORY} mais recentes de cada) — "
        f"{len(matched_events)} evento(s) com correspondência de regra"
    ]
    if omitted_matched > 0:
        note_parts.append(f"{omitted_matched} não exibido(s) por já terem exemplo suficiente")
    if ignored_no_match > 0:
        note_parts.append(f"{ignored_no_match} sem correspondência de regra foram ignorados")
    note = f"<p class='note-small'>{'; '.join(note_parts)}.</p>"

    search = (
        "<div class='detail-search'>"
        "<input type='text' placeholder='Buscar por categoria ou detalhes...' "
        "oninput=\"filterDetailRows(this)\" autocomplete='off'/>"
        "<span class='detail-search-count'></span>"
        "</div>"
    )

    body = (
        f"{search}"
        "<div class='detail-scroll'>"
        "<table class='detail-table'>"
        "<thead><tr><th>Data</th><th>Status</th><th>Categorias</th><th>Detalhes</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        "</table>"
        "</div>"
        f"{note}"
    )
    return body

def render_top_hosts_table_html(agg: dict, id_prefix: str):
    """
    Tabela de hosts com drill-down clicável. Lista TODOS os hosts (sem
    cortar em top_n) — a ordenação por prioridade fixa (GPU > GPU Temp >
    Sensor Temp > IB) só decide a ordem inicial das linhas, não quais
    hosts aparecem: um host dominante só numa categoria menos prioritária
    (ex.: GPU Temp) não pode ficar de fora só porque muitos outros hosts
    têm 1 evento de uma categoria de prioridade maior. A busca e a
    ordenação clicável por coluna é que ajudam a navegar em listas
    grandes. id_prefix garante ids únicos quando essa tabela é renderizada
    várias vezes (uma por cluster). As colunas de categoria levam
    data-cat="<categoria>" e a tabela leva a classe "cfg-table" — é o que
    o seletor "Configurar colunas" do HTML usa para mostrar/ocultar/
    reordenar colunas.
    """
    hosts = agg["hosts_sorted"]
    max_total = max([agg["per_host_total"][h] for h in hosts], default=1)

    def bar(v):
        pct = int((v / max_total) * 100) if max_total else 0
        return f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'

    header_cats = "".join(
        f"<th data-cat='{c}' data-sort-type='num'>{html.escape(CATEGORY_LABELS[c])}</th>" for c in CATEGORIES
    )

    table_rows = []
    for i, h in enumerate(hosts):
        detail_id = f"{id_prefix}-detail-{i}"
        chevron_id = f"{id_prefix}-chevron-{i}"
        t = agg["per_host_total"]
        c = agg["per_host_cat"]
        matched = any(c[cat][h] > 0 for cat in CATEGORIES)
        match_badge = (
            "<span class='badge badge-match'>&#10003; regra identificada</span>" if matched
            else "<span class='badge badge-none'>sem correspondência</span>"
        )
        row_cats = "".join(f"<td class='num' data-cat='{cat}'>{c[cat][h]}</td>" for cat in CATEGORIES)
        table_rows.append(
            "<tr class='host-row' "
            f"data-host='{html.escape(h)}' data-detail-id='{detail_id}' "
            f"onclick=\"toggleDetail('{detail_id}', '{chevron_id}')\">"
            "<td class='mono'>"
            f"<span class='chevron' id='{chevron_id}'>&#9656;</span> {html.escape(h)}"
            "</td>"
            f"<td>{match_badge}</td>"
            f"<td class='num'>{t[h]}</td>"
            f"<td class='num'>{agg['per_host_matched'][h]}</td>"
            f"<td>{bar(t[h])}</td>"
            f"{row_cats}"
            "</tr>"
        )
        table_rows.append(
            f"<tr class='detail-row' id='{detail_id}'>"
            f"<td colspan='{DETAIL_COLSPAN}'>"
            f"<div class='detail-wrap'>{render_host_detail_table(h, agg['host_events'])}</div>"
            "</td></tr>"
        )

    return (
        "<table class='cfg-table'>"
        "<thead><tr>"
        "<th data-sort-type='text-natural'>Hostname</th>"
        "<th data-sort-type='text'>Correspondência</th>"
        "<th data-sort-type='num' title='Todas as linhas do Monit deste host no período, batendo regra ou não'>Total</th>"
        "<th data-sort-type='num' title='Quantas dessas linhas bateram pelo menos uma regra de categorização'>Eventos c/ regra</th>"
        "<th>Visual</th>"
        f"{header_cats}"
        "</tr></thead>"
        f"<tbody>{''.join(table_rows)}</tbody>"
        "</table>"
    )

def _safe_json_for_script(data) -> str:
    """json.dumps, protegido contra a sequência "</script" quebrar a tag."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

def render_html_report(report_data: dict):
    if report_data["multi_cluster"]:
        clusters_sorted = report_data["clusters_sorted"]
        cluster_aggs = report_data["cluster_aggs"]

        index_header_cats = "".join(
            f"<th data-cat='{c}' data-sort-type='num'>{html.escape(CATEGORY_LABELS[c])}</th>" for c in CATEGORIES
        )

        index_rows = []
        sections = []
        for idx, c in enumerate(clusters_sorted):
            t = cluster_aggs[c]["totals"]
            anchor = f"cluster-{idx}"
            cluster_matched = any(t[cat] > 0 for cat in CATEGORIES)
            match_badge = (
                "<span class='badge badge-match'>&#10003; regra identificada</span>" if cluster_matched
                else "<span class='badge badge-none'>sem correspondência</span>"
            )
            index_row_cats = "".join(f"<td class='num' data-cat='{cat}'>{t[cat]}</td>" for cat in CATEGORIES)
            index_rows.append(
                "<tr>"
                f"<td class='mono'><a href='#{anchor}'>{html.escape(c)}</a></td>"
                f"<td>{match_badge}</td>"
                f"<td class='num'>{t['hosts_afetados']}</td>"
                f"<td class='num'>{t['total_events']}</td>"
                f"<td class='num'>{t['matched_events']}</td>"
                f"{index_row_cats}"
                "</tr>"
            )
            sections.append(
                f"<div class='cluster-section' id='{anchor}' data-cluster-section>"
                f"<h2>Cluster: {html.escape(c)}</h2>"
                + render_top_hosts_table_html(cluster_aggs[c], id_prefix=f"c{idx}")
                + "</div>"
            )

        body_main = f"""
  <h2>Índice de clusters</h2>
  <table class="cfg-table">
    <thead>
      <tr>
        <th data-sort-type="text-natural">Cluster</th>
        <th data-sort-type="text">Correspondência</th>
        <th data-sort-type="num">Hosts afetados</th>
        <th data-sort-type="num" title="Todas as linhas do Monit deste cluster no período, batendo regra ou não">Total</th>
        <th data-sort-type="num" title="Quantas dessas linhas bateram pelo menos uma regra de categorização">Eventos c/ regra</th>
        {index_header_cats}
      </tr>
    </thead>
    <tbody>
      {''.join(index_rows)}
    </tbody>
  </table>

  {''.join(sections)}
"""
    else:
        body_main = f"""
  <div class="cluster-section" data-cluster-section>
  <h2>Hosts (consolidado)</h2>
  {render_top_hosts_table_html(report_data["global_agg"], id_prefix="g")}
  </div>
"""

    all_categories_json = _safe_json_for_script(
        [{"key": c, "label": CATEGORY_LABELS[c]} for c in CATEGORIES]
    )

    html_out = f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Relatório – Reincidência de eventos críticos (Monit)</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; margin: 24px; color: #111; }}
  h1 {{ margin: 0 0 8px 0; }}
  .sub {{ color: #444; margin-bottom: 18px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
  th, td {{ border: 1px solid #ddd; padding: 8px; vertical-align: middle; }}
  th {{ background: #f6f6f6; text-align: left; }}
  td.num {{ text-align: right; width: 80px; }}
  td.mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }}
  .bar {{ height: 10px; background: #eee; border-radius: 6px; overflow: hidden; min-width: 120px; }}
  .fill {{ height: 10px; background: #0b5; }}
  pre {{ white-space: pre-wrap; background: #0b0b0b; color: #f2f2f2; padding: 12px; border-radius: 8px; }}
  .note {{ background: #fff7e6; padding: 10px 12px; border: 1px solid #f1d28a; border-radius: 8px; }}
  .note-small {{ color: #666; font-size: 0.85em; margin: 6px 2px; }}

  .toolbar {{ display: flex; align-items: center; gap: 10px; margin: 16px 0; flex-wrap: wrap; }}
  .toolbar input[type="text"] {{
    flex: 0 1 320px; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 0.95em;
  }}
  .toolbar input[type="text"]:focus {{ outline: 2px solid #8ab4f8; border-color: #8ab4f8; }}
  #searchCount {{ color: #666; font-size: 0.85em; }}
  .toolbar button {{
    padding: 8px 12px; border: 1px solid #ccc; border-radius: 6px; background: #fff;
    cursor: pointer; font-size: 0.9em;
  }}
  .toolbar button:hover {{ background: #f0f7ff; }}

  .badge {{
    display: inline-block; padding: 2px 9px; border-radius: 999px;
    font-size: 0.82em; font-weight: 600; white-space: nowrap; margin: 1px 2px 1px 0;
  }}
  .badge-match {{ background: #e6f7ec; color: #0b7a3b; }}
  .badge-none {{ background: #eef1f5; color: #5b6b7c; }}

  th[data-sort-type] {{ cursor: pointer; user-select: none; white-space: nowrap; }}
  th[data-sort-type]:hover {{ background: #eef1f5; }}
  th[data-sort-type]::after {{ content: '\\2195'; margin-left: 5px; opacity: 0.3; font-size: 0.85em; }}
  th[data-sort-type][data-sort-dir="asc"]::after {{ content: '\\25B2'; opacity: 1; }}
  th[data-sort-type][data-sort-dir="desc"]::after {{ content: '\\25BC'; opacity: 1; }}

  tr.host-row {{ cursor: pointer; }}
  tr.host-row:hover {{ background: #f0f7ff; }}
  .chevron {{ display: inline-block; transition: transform .15s ease; }}
  .chevron.rotated {{ transform: rotate(90deg); }}

  tr.detail-row {{ display: none; }}
  tr.detail-row.open {{ display: table-row; }}
  tr.detail-row > td {{ background: #fafafa; padding: 12px; }}
  .detail-wrap {{ padding: 4px; }}
  .detail-scroll {{ max-height: 420px; overflow-y: auto; }}
  table.detail-table {{ width: 100%; margin-top: 0; font-size: 0.9em; }}
  table.detail-table th {{ position: sticky; top: 0; }}
  td.detail-cell {{ white-space: pre-wrap; word-break: break-word; }}
  .log-line {{ padding: 0 2px; border-radius: 3px; }}
  .log-line.log-warning {{ background: #fff3cd; color: #7a5b00; }}
  .log-line.log-critical {{ background: #f8d7da; color: #d32f2f; font-weight: 700; }}
  .log-line.log-recovery-fail {{ background: #f1b0b7; color: #7a1414; font-weight: 700; }}

  .detail-search {{ display: flex; align-items: center; gap: 8px; margin: 0 0 8px 0; }}
  .detail-search input[type="text"] {{
    flex: 0 1 280px; padding: 6px 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 0.88em;
  }}
  .detail-search-count {{ color: #666; font-size: 0.8em; }}

  .col-config-panel {{
    background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 14px 16px;
    margin: 8px 0 16px 0; max-width: 420px;
  }}
  .col-config-panel ul {{ list-style: none; margin: 10px 0; padding: 0; }}
  .col-config-item {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 5px 4px; border-bottom: 1px solid #f0f0f0;
  }}
  .col-config-item label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; }}
  .col-config-arrows button {{
    border: 1px solid #ccc; background: #fafafa; border-radius: 4px; cursor: pointer;
    width: 26px; height: 24px; margin-left: 4px;
  }}
  .col-config-arrows button:hover {{ background: #eef1f5; }}
  .col-config-actions {{ display: flex; gap: 8px; margin-top: 10px; }}
</style>
</head>
<body>
  <h1>Relatório – Reincidência de eventos críticos (Monit)</h1>
  <div class="sub">Todos os hosts, ordenados por prioridade — clique num cabeçalho de coluna para reordenar, ou num host para ver seu histórico</div>

  <div class="note">
    <b>Orientação operacional:</b> priorizar abertura de registros individuais para hosts com maior reincidência (categorias no topo da lista de regras).
    <br/>"Total" conta todas as linhas do Monit no período (batendo regra ou não); "Eventos c/ regra" conta só as que bateram alguma categoria — a diferença é esperada quando o host tem bastante tráfego de rotina do Monit sem relação com as regras.
  </div>

  <div class="toolbar">
    <input type="text" id="hostSearch" placeholder="Buscar host..." oninput="filterHosts(this.value)" autocomplete="off"/>
    <span id="searchCount"></span>
    <button type="button" onclick="toggleColConfig()">Configurar colunas &#9881;</button>
  </div>

  <div id="colConfigPanel" class="col-config-panel" style="display:none">
    <p class="note-small">Marque quais categorias exibir como coluna nas tabelas e use as setas para reordenar. A escolha fica salva neste navegador.</p>
    <ul id="colConfigList"></ul>
    <div class="col-config-actions">
      <button type="button" onclick="applyColConfigFromPanel()">Aplicar</button>
      <button type="button" onclick="resetColConfig()">Restaurar padrão</button>
    </div>
  </div>

  {body_main}
  <h2>Detalhes e evidências (texto)</h2>
  <pre>{html.escape(report_data["report_txt"])}</pre>

  <script>
    var ALL_CATEGORIES = {all_categories_json};
    var COL_CONFIG_STORAGE_KEY = 'hpcReportColumns';
    var currentOrder = ALL_CATEGORIES.map(function(c) {{ return c.key; }});
    var currentChecked = {{}};

    function filterDetailRows(inputEl) {{
      var term = inputEl.value.trim().toLowerCase();
      var wrap = inputEl.closest('.detail-wrap');
      if (!wrap) return;
      var rows = wrap.querySelectorAll('table.detail-table tbody tr');
      var visible = 0;

      rows.forEach(function(row) {{
        var text = '';
        row.querySelectorAll('.searchable').forEach(function(cell) {{
          text += ' ' + (cell.getAttribute('data-search') || cell.textContent);
        }});
        var match = !term || text.toLowerCase().indexOf(term) !== -1;
        row.style.display = match ? '' : 'none';
        if (match) visible++;
      }});

      var counter = wrap.querySelector('.detail-search-count');
      if (counter) counter.textContent = term ? (visible + ' de ' + rows.length) : '';
    }}

    function toggleDetail(rowId, chevronId) {{
      var row = document.getElementById(rowId);
      var chevron = document.getElementById(chevronId);
      if (!row) return;
      row.classList.toggle('open');
      if (chevron) chevron.classList.toggle('rotated');
    }}

    function filterHosts(term) {{
      term = term.trim().toLowerCase();
      var hostRows = document.querySelectorAll('tr.host-row');
      var visible = 0;

      hostRows.forEach(function(row) {{
        var host = (row.getAttribute('data-host') || '').toLowerCase();
        var match = !term || host.indexOf(term) !== -1;

        row.style.display = match ? '' : 'none';
        var detail = document.getElementById(row.getAttribute('data-detail-id'));
        if (detail) detail.style.display = match ? '' : 'none';
        if (match) visible++;
      }});

      var counter = document.getElementById('searchCount');
      if (counter) {{
        counter.textContent = term ? (visible + ' de ' + hostRows.length + ' hosts') : '';
      }}

      document.querySelectorAll('[data-cluster-section]').forEach(function(section) {{
        var anyVisible = false;
        section.querySelectorAll('tr.host-row').forEach(function(row) {{
          if (row.style.display !== 'none') anyVisible = true;
        }});
        section.style.display = (!term || anyVisible) ? '' : 'none';
      }});
    }}

    function applyColumnConfig(selectedCats) {{
      document.querySelectorAll('table.cfg-table').forEach(function(table) {{
        table.querySelectorAll('tr').forEach(function(row) {{
          row.querySelectorAll('[data-cat]').forEach(function(cell) {{ cell.style.display = 'none'; }});
          selectedCats.forEach(function(cat) {{
            var cell = row.querySelector('[data-cat="' + cat + '"]');
            if (cell) {{
              cell.style.display = '';
              row.appendChild(cell);
            }}
          }});
        }});
      }});
    }}

    function renderColConfigList() {{
      var list = document.getElementById('colConfigList');
      list.innerHTML = '';
      currentOrder.forEach(function(key, idx) {{
        var meta = ALL_CATEGORIES.find(function(c) {{ return c.key === key; }});
        var label = meta ? meta.label : key;

        var checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = !!currentChecked[key];
        checkbox.onchange = function() {{ currentChecked[key] = checkbox.checked; }};

        var labelEl = document.createElement('label');
        labelEl.appendChild(checkbox);
        labelEl.appendChild(document.createTextNode(' ' + label));

        var upBtn = document.createElement('button');
        upBtn.type = 'button';
        upBtn.textContent = '\\u25B2';
        upBtn.title = 'Mover para cima';
        upBtn.onclick = function() {{ moveColConfig(idx, -1); }};

        var downBtn = document.createElement('button');
        downBtn.type = 'button';
        downBtn.textContent = '\\u25BC';
        downBtn.title = 'Mover para baixo';
        downBtn.onclick = function() {{ moveColConfig(idx, 1); }};

        var arrows = document.createElement('span');
        arrows.className = 'col-config-arrows';
        arrows.appendChild(upBtn);
        arrows.appendChild(downBtn);

        var li = document.createElement('li');
        li.className = 'col-config-item';
        li.appendChild(labelEl);
        li.appendChild(arrows);
        list.appendChild(li);
      }});
    }}

    function moveColConfig(idx, dir) {{
      var newIdx = idx + dir;
      if (newIdx < 0 || newIdx >= currentOrder.length) return;
      var tmp = currentOrder[idx];
      currentOrder[idx] = currentOrder[newIdx];
      currentOrder[newIdx] = tmp;
      renderColConfigList();
    }}

    function toggleColConfig() {{
      var panel = document.getElementById('colConfigPanel');
      panel.style.display = (panel.style.display === 'none') ? '' : 'none';
    }}

    function applyColConfigFromPanel() {{
      var selected = currentOrder.filter(function(k) {{ return currentChecked[k]; }});
      applyColumnConfig(selected);
      try {{
        localStorage.setItem(COL_CONFIG_STORAGE_KEY, JSON.stringify({{ order: currentOrder, checked: currentChecked }}));
      }} catch (e) {{ /* localStorage indisponível (ex.: aberto via file:// em modo restrito) */ }}
      document.getElementById('colConfigPanel').style.display = 'none';
    }}

    function resetColConfig() {{
      currentOrder = ALL_CATEGORIES.map(function(c) {{ return c.key; }});
      currentChecked = {{}};
      currentOrder.forEach(function(k) {{ currentChecked[k] = true; }});
      renderColConfigList();
      applyColumnConfig(currentOrder);
      try {{ localStorage.removeItem(COL_CONFIG_STORAGE_KEY); }} catch (e) {{}}
    }}

    function initColConfig() {{
      var saved = null;
      try {{ saved = JSON.parse(localStorage.getItem(COL_CONFIG_STORAGE_KEY) || 'null'); }} catch (e) {{ saved = null; }}

      if (saved && Array.isArray(saved.order) && saved.checked) {{
        var validOrder = saved.order.filter(function(k) {{
          return ALL_CATEGORIES.some(function(c) {{ return c.key === k; }});
        }});
        ALL_CATEGORIES.forEach(function(c) {{
          if (validOrder.indexOf(c.key) === -1) validOrder.push(c.key);
        }});
        currentOrder = validOrder;
        currentChecked = {{}};
        currentOrder.forEach(function(k) {{
          currentChecked[k] = saved.checked[k] !== undefined ? !!saved.checked[k] : true;
        }});
      }} else {{
        currentOrder = ALL_CATEGORIES.map(function(c) {{ return c.key; }});
        currentChecked = {{}};
        currentOrder.forEach(function(k) {{ currentChecked[k] = true; }});
      }}

      renderColConfigList();
      applyColumnConfig(currentOrder.filter(function(k) {{ return currentChecked[k]; }}));
    }}

    // Ordenação "natural": trata sequências de dígitos como número, não
    // caractere a caractere — assim hostb01n01, hostb01n02, ..., hostb01n32,
    // hostb02n01 saem na ordem certa em vez de hostb01n1 < hostb01n10 < hostb01n2.
    function naturalCompare(a, b) {{
      var re = /(\d+)|(\D+)/g;
      var ax = a.match(re) || [];
      var bx = b.match(re) || [];
      var len = Math.max(ax.length, bx.length);
      for (var i = 0; i < len; i++) {{
        var av = ax[i] || '';
        var bv = bx[i] || '';
        var an = /^\d+$/.test(av);
        var bn = /^\d+$/.test(bv);
        if (an && bn) {{
          var diff = parseInt(av, 10) - parseInt(bv, 10);
          if (diff !== 0) return diff;
        }} else if (av !== bv) {{
          return av < bv ? -1 : 1;
        }}
      }}
      return 0;
    }}

    // Agrupa cada tr.host-row com sua tr.detail-row logo em seguida (se
    // houver), para que ao reordenar o histórico expandido continue
    // colado embaixo do host certo. Linhas do índice de clusters (sem
    // detail-row associado) viram grupos de 1.
    function getRowGroups(tbody) {{
      var groups = [];
      var rows = Array.prototype.slice.call(tbody.children);
      for (var i = 0; i < rows.length; i++) {{
        var row = rows[i];
        if (row.classList.contains('detail-row')) continue;
        var group = [row];
        var next = rows[i + 1];
        if (next && next.classList.contains('detail-row')) group.push(next);
        groups.push(group);
      }}
      return groups;
    }}

    function sortTable(table, colIndex, sortType, dir) {{
      var tbody = table.querySelector('tbody');
      if (!tbody) return;
      var groups = getRowGroups(tbody);

      groups.sort(function(ga, gb) {{
        var ca = ga[0].children[colIndex];
        var cb = gb[0].children[colIndex];
        var va = (ca ? ca.textContent : '').trim();
        var vb = (cb ? cb.textContent : '').trim();
        var cmp;
        if (sortType === 'num') {{
          cmp = (parseFloat(va) || 0) - (parseFloat(vb) || 0);
        }} else if (sortType === 'text-natural') {{
          cmp = naturalCompare(va.toLowerCase(), vb.toLowerCase());
        }} else {{
          cmp = va.toLowerCase() < vb.toLowerCase() ? -1 : (va.toLowerCase() > vb.toLowerCase() ? 1 : 0);
        }}
        return dir === 'asc' ? cmp : -cmp;
      }});

      groups.forEach(function(g) {{
        g.forEach(function(row) {{ tbody.appendChild(row); }});
      }});
    }}

    function initSortableTables() {{
      document.querySelectorAll('table.cfg-table thead th[data-sort-type]').forEach(function(th) {{
        th.addEventListener('click', function() {{
          var table = th.closest('table');
          var headerRow = th.parentNode;
          var colIndex = Array.prototype.indexOf.call(headerRow.children, th);
          var sortType = th.getAttribute('data-sort-type');
          var newDir = th.getAttribute('data-sort-dir') === 'asc' ? 'desc' : 'asc';

          headerRow.querySelectorAll('th[data-sort-type]').forEach(function(other) {{
            if (other !== th) other.removeAttribute('data-sort-dir');
          }});
          th.setAttribute('data-sort-dir', newDir);

          sortTable(table, colIndex, sortType, newDir);
        }});
      }});
    }}

    initColConfig();
    initSortableTables();
  </script>
</body>
</html>"""
    return html_out

def render_audit_report(rows: list, days_window: int, hostname: str) -> str:
    """
    Relatório de conferência para UM host: lista, categoria a categoria,
    TODAS as linhas de "Detalhes" originais do CSV que bateram cada regra
    e as que não bateram nenhuma — sem nenhum corte de amostragem (ao
    contrário do drill-down do HTML, que limita a MAX_EVENTS_PER_CATEGORY
    exemplos). Serve para confrontar a contagem do script linha a linha
    com uma ferramenta externa (grep, outra regex, etc.) e achar
    exatamente onde duas regras concorrentes divergem.
    """
    now = datetime.now()
    start = now - timedelta(days=days_window)

    host_rows = []
    for r in rows:
        if r["Hostname"] != hostname:
            continue
        dt = parse_dt(r["Date"])
        if dt is None or dt < start or dt > now:
            continue
        host_rows.append((dt, r))
    host_rows.sort(key=lambda item: item[0])

    per_category = {c: [] for c in CATEGORIES}
    no_match = []
    for dt, r in host_rows:
        cats = categorize(r["Detalhes"])
        raw = re.sub(r"\s+", " ", (r["Detalhes"] or "").replace("\r", " ").replace("\n", " ")).strip()
        if cats:
            for c in cats:
                per_category[c].append((r["Date"], raw))
        else:
            no_match.append((r["Date"], raw))

    matched_lines = len(host_rows) - len(no_match)

    lines = []
    lines.append(f"AUDITORIA — {hostname}")
    lines.append(f"Janela analisada (últimos {days_window} dias): {start:%Y-%m-%d %H:%M:%S} até {now:%Y-%m-%d %H:%M:%S}")
    lines.append(f"Total de linhas do host no período (batendo regra ou não): {len(host_rows)}")
    lines.append(f"Linhas que bateram alguma regra: {matched_lines}")
    lines.append(
        "(a soma das categorias abaixo pode ser MAIOR que 'linhas que bateram alguma regra' "
        "se uma mesma linha bater mais de uma categoria ao mesmo tempo)"
    )
    lines.append("")

    for c in CATEGORIES:
        items = per_category[c]
        lines.append(f"=== {CATEGORY_LABELS[c]} — {len(items)} linha(s) ===")
        for date_s, raw in items:
            lines.append(f"{date_s} | {raw}")
        lines.append("")

    lines.append(f"=== SEM CORRESPONDÊNCIA (nenhuma regra bateu) — {len(no_match)} linha(s) ===")
    for date_s, raw in no_match:
        lines.append(f"{date_s} | {raw}")

    return "\n".join(lines)

def main():
    args = sys.argv[1:]

    audit_host = None
    if "--audit" in args:
        idx = args.index("--audit")
        if idx + 1 >= len(args):
            print("Uso: --audit <hostname>")
            sys.exit(1)
        audit_host = args[idx + 1]
        del args[idx:idx + 2]

    if len(args) < 1:
        print("Uso: python3 hpc_event_report.py <arquivo.csv> [dias] [topN] [--audit <hostname>]")
        print("  --audit <hostname>  gera audit_<hostname>.txt com TODAS as linhas do host,")
        print("                      sem corte de amostragem, para conferir contagem manualmente.")
        sys.exit(1)

    csv_path = args[0]
    days = int(args[1]) if len(args) >= 2 else 60
    topn = int(args[2]) if len(args) >= 3 else 15

    rows = read_monit_csv(csv_path)
    report_data = generate_reports(rows, days_window=days, top_n=topn)

    # Saídas base
    write_csv("report_summary.csv", report_data["summary_lines"])
    write_text("report_top.txt", report_data["report_txt"])

    # Relatórios amigáveis
    md_report = render_markdown_report(report_data, top_n=topn)
    html_report = render_html_report(report_data)

    write_text("report.md", md_report)
    write_text("report.html", html_report)

    print("OK: gerados report_summary.csv, report_top.txt, report.md e report.html")

    if audit_host:
        audit_path = f"audit_{audit_host}.txt"
        write_text(audit_path, render_audit_report(rows, days, audit_host))
        print(f"OK: gerado {audit_path}")

if __name__ == "__main__":
    main()
