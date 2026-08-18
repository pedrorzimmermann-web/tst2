#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import html
import re
import sys
from datetime import datetime, timedelta
from collections import defaultdict, Counter

# -----------------------------
# Config
# -----------------------------
CATEGORIES = ["gpu_temp", "sensor_temp", "gpu", "ib"]

# Máximo de eventos individuais exibidos por host no drill-down do HTML
# (evita relatórios gigantes para hosts com milhares de ocorrências)
MAX_EVENTS_PER_HOST = 300

# Regras (ajuste fino se necessário)
RE_IB = re.compile(r"\bIB\b|INFINIBAND|\bib0\b|\bib1\b|IB\s*-", re.IGNORECASE)
RE_GPU_TEMP = re.compile(r"GPU\s*-\s*TEMPERATURA|\[TST23-07\]", re.IGNORECASE)
RE_SENSOR_TEMP = re.compile(r"TEMPERATURA|\[TST13-01\]", re.IGNORECASE)
RE_GPU = re.compile(
    r"GPU\s*-\s*NVIDIA|NVRM|NVLINK|\[TST23-02\]|\[TST23-04\]|\[TST23-05\]|\[TST23-08\]|\[TST23-10\]",
    re.IGNORECASE
)

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

def parse_dt(s: str):
    # formato do CSV: "2026-08-17 09:41:34"
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def categorize(details: str):
    """
    Pode retornar múltiplas categorias (ex.: GPU + IB no mesmo evento).
    Isso é útil para reincidência por tipo.
    """
    d = details or ""
    cats = set()

    if RE_IB.search(d):
        cats.add("ib")
    if RE_GPU_TEMP.search(d):
        cats.add("gpu_temp")
    # sensor_temp: temperatura genérica, excluindo os casos já marcados como gpu_temp
    if RE_SENSOR_TEMP.search(d) and ("gpu_temp" not in cats):
        cats.add("sensor_temp")
    if RE_GPU.search(d):
        cats.add("gpu")

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
    per_host_cat = {c: Counter() for c in CATEGORIES}
    samples = defaultdict(lambda: defaultdict(list))
    host_events = defaultdict(list)

    for r in rows:
        host = r["Hostname"]
        per_host_total[host] += 1

        cats = categorize(r["Detalhes"])
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

    # Ordenação: prioriza GPU, depois GPU temp, depois sensor temp, IB, total
    def sort_key(h):
        return (
            per_host_cat["gpu"][h],
            per_host_cat["gpu_temp"][h],
            per_host_cat["sensor_temp"][h],
            per_host_cat["ib"][h],
            per_host_total[h]
        )

    hosts_sorted = sorted(per_host_total.keys(), key=sort_key, reverse=True)

    totals = {
        "hosts_afetados": len(hosts_sorted),
        "total_events": sum(per_host_total.values()),
        "gpu": sum(per_host_cat["gpu"].values()),
        "gpu_temp": sum(per_host_cat["gpu_temp"].values()),
        "sensor_temp": sum(per_host_cat["sensor_temp"].values()),
        "ib": sum(per_host_cat["ib"].values()),
    }

    return {
        "hosts_sorted": hosts_sorted,
        "per_host_total": per_host_total,
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
    lines.append("Hostname | total | gpu | gpu_temp | sensor_temp | ib")
    for h in agg["hosts_sorted"][:top_n]:
        lines.append(
            f"{h} | {agg['per_host_total'][h]} | {agg['per_host_cat']['gpu'][h]} | "
            f"{agg['per_host_cat']['gpu_temp'][h]} | {agg['per_host_cat']['sensor_temp'][h]} | "
            f"{agg['per_host_cat']['ib'][h]}"
        )

    lines.append("")
    lines.append("TOP por categoria:")
    for c in CATEGORIES:
        lines.append(f"- {c}:")
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
                lines.append(f"* {h} / {c} ({agg['per_host_cat'][c][h]} ocorrências):")
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

        summary_lines = [["Hostname", "total_events", "gpu", "gpu_temp", "sensor_temp", "ib"]]
        for h in global_agg["hosts_sorted"]:
            t = global_agg["per_host_total"]
            c = global_agg["per_host_cat"]
            summary_lines.append([h, str(t[h]), str(c["gpu"][h]), str(c["gpu_temp"][h]), str(c["sensor_temp"][h]), str(c["ib"][h])])
    else:
        cluster_rows = defaultdict(list)
        for r in filtered:
            cluster_rows[r["Cluster"] or "(sem cluster)"].append(r)
        cluster_aggs = {c: aggregate_events(cluster_rows[c], top_n) for c in clusters}

        def cluster_sort_key(c):
            t = cluster_aggs[c]["totals"]
            return (t["gpu"], t["gpu_temp"], t["sensor_temp"], t["ib"], t["total_events"])

        clusters_sorted = sorted(clusters, key=cluster_sort_key, reverse=True)

        report.append(f"ÍNDICE DE CLUSTERS ({len(clusters_sorted)}):")
        report.append("Cluster | hosts_afetados | total | gpu | gpu_temp | sensor_temp | ib")
        for c in clusters_sorted:
            t = cluster_aggs[c]["totals"]
            report.append(
                f"{c} | {t['hosts_afetados']} | {t['total_events']} | {t['gpu']} | {t['gpu_temp']} | "
                f"{t['sensor_temp']} | {t['ib']}"
            )
        report.append("")

        for c in clusters_sorted:
            report.append("=" * 60)
            report.append(f"CLUSTER: {c}")
            report.append("=" * 60)
            report.extend(render_scope_report_text(cluster_aggs[c], top_n))
            report.append("")

        summary_lines = [["Cluster", "Hostname", "total_events", "gpu", "gpu_temp", "sensor_temp", "ib"]]
        for c in clusters_sorted:
            agg = cluster_aggs[c]
            t = agg["per_host_total"]
            pc = agg["per_host_cat"]
            for h in agg["hosts_sorted"]:
                summary_lines.append([c, h, str(t[h]), str(pc["gpu"][h]), str(pc["gpu_temp"][h]), str(pc["sensor_temp"][h]), str(pc["ib"][h])])

    report.append("")
    report.append("Recomendação operacional:")
    report.append("- Abrir registros individuais para os hosts com reincidência muito acima dos demais, priorizando GPU e IB.")
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
    lines.append("| Hostname | Total | GPU | GPU Temp | Sensor Temp | IB |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for h in agg["hosts_sorted"][:top_n]:
        t = agg["per_host_total"]
        c = agg["per_host_cat"]
        lines.append(f"| {h} | {t[h]} | {c['gpu'][h]} | {c['gpu_temp'][h]} | {c['sensor_temp'][h]} | {c['ib'][h]} |")
    return lines

def render_markdown_report(report_data: dict, top_n: int = 15):
    md = []
    md.append("# Relatório – Reincidência de eventos críticos (Monit)")
    md.append("")

    if report_data["multi_cluster"]:
        md.append("## Índice de clusters")
        md.append("")
        md.append("| Cluster | Hosts afetados | Total | GPU | GPU Temp | Sensor Temp | IB |")
        md.append("|---|---:|---:|---:|---:|---:|---:|")
        for c in report_data["clusters_sorted"]:
            t = report_data["cluster_aggs"][c]["totals"]
            md.append(f"| {c} | {t['hosts_afetados']} | {t['total_events']} | {t['gpu']} | {t['gpu_temp']} | {t['sensor_temp']} | {t['ib']} |")
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

def render_host_detail_table(host: str, host_events: dict):
    """
    Monta a sub-tabela HTML com todos os eventos de um host (usada no
    drill-down ao clicar na linha do host no TOP N). Os eventos são
    ordenados do mais recente para o mais antigo e limitados a
    MAX_EVENTS_PER_HOST para não estourar o tamanho do arquivo.
    """
    events = sorted(host_events.get(host, []), key=lambda e: e["dt"], reverse=True)
    shown = events[:MAX_EVENTS_PER_HOST]

    rows_html = []
    for e in shown:
        if e["cats"]:
            cats_html = "".join(
                f"<span class='badge badge-match'>{html.escape(c)}</span>" for c in sorted(e["cats"])
            )
        else:
            cats_html = "<span class='badge badge-none'>sem correspondência</span>"
        rows_html.append(
            "<tr>"
            f"<td class='mono'>{html.escape(e['date'])}</td>"
            f"<td>{html.escape(e['status'])}</td>"
            f"<td>{cats_html}</td>"
            f"<td class='detail-cell'>{html.escape(e['detail'])}</td>"
            "</tr>"
        )

    note = ""
    if len(events) > MAX_EVENTS_PER_HOST:
        note = (
            f"<p class='note-small'>Mostrando {MAX_EVENTS_PER_HOST} de {len(events)} "
            "eventos (mais recentes primeiro).</p>"
        )

    if not rows_html:
        body = "<p class='note-small'>Nenhum evento no período.</p>"
    else:
        body = (
            "<div class='detail-scroll'>"
            "<table class='detail-table'>"
            "<thead><tr><th>Data</th><th>Status</th><th>Categorias</th><th>Detalhes</th></tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody>"
            "</table>"
            "</div>"
            f"{note}"
        )
    return body

def render_top_hosts_table_html(agg: dict, top_n: int, id_prefix: str):
    """
    Tabela "Top hosts" com drill-down clicável. id_prefix garante ids únicos
    quando essa tabela é renderizada várias vezes (uma por cluster).
    """
    hosts = agg["hosts_sorted"][:top_n]
    max_total = max([agg["per_host_total"][h] for h in hosts], default=1)

    def bar(v):
        pct = int((v / max_total) * 100) if max_total else 0
        return f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'

    table_rows = []
    for i, h in enumerate(hosts):
        detail_id = f"{id_prefix}-detail-{i}"
        chevron_id = f"{id_prefix}-chevron-{i}"
        t = agg["per_host_total"]
        c = agg["per_host_cat"]
        matched = (c["gpu"][h] + c["gpu_temp"][h] + c["sensor_temp"][h] + c["ib"][h]) > 0
        match_badge = (
            "<span class='badge badge-match'>&#10003; regra identificada</span>" if matched
            else "<span class='badge badge-none'>sem correspondência</span>"
        )
        table_rows.append(
            "<tr class='host-row' "
            f"data-host='{html.escape(h)}' data-detail-id='{detail_id}' "
            f"onclick=\"toggleDetail('{detail_id}', '{chevron_id}')\">"
            "<td class='mono'>"
            f"<span class='chevron' id='{chevron_id}'>&#9656;</span> {html.escape(h)}"
            "</td>"
            f"<td>{match_badge}</td>"
            f"<td class='num'>{t[h]}</td>"
            f"<td>{bar(t[h])}</td>"
            f"<td class='num'>{c['gpu'][h]}</td>"
            f"<td class='num'>{c['gpu_temp'][h]}</td>"
            f"<td class='num'>{c['sensor_temp'][h]}</td>"
            f"<td class='num'>{c['ib'][h]}</td>"
            "</tr>"
        )
        table_rows.append(
            f"<tr class='detail-row' id='{detail_id}'>"
            "<td colspan='8'>"
            f"<div class='detail-wrap'>{render_host_detail_table(h, agg['host_events'])}</div>"
            "</td></tr>"
        )

    return (
        "<table>"
        "<thead><tr>"
        "<th>Hostname</th><th>Correspondência</th><th>Total</th><th>Visual</th><th>GPU</th><th>GPU Temp</th><th>Sensor Temp</th><th>IB</th>"
        "</tr></thead>"
        f"<tbody>{''.join(table_rows)}</tbody>"
        "</table>"
    )

def render_html_report(report_data: dict, top_n: int = 15):
    if report_data["multi_cluster"]:
        clusters_sorted = report_data["clusters_sorted"]
        cluster_aggs = report_data["cluster_aggs"]

        index_rows = []
        sections = []
        for idx, c in enumerate(clusters_sorted):
            t = cluster_aggs[c]["totals"]
            anchor = f"cluster-{idx}"
            cluster_matched = (t["gpu"] + t["gpu_temp"] + t["sensor_temp"] + t["ib"]) > 0
            match_badge = (
                "<span class='badge badge-match'>&#10003; regra identificada</span>" if cluster_matched
                else "<span class='badge badge-none'>sem correspondência</span>"
            )
            index_rows.append(
                "<tr>"
                f"<td class='mono'><a href='#{anchor}'>{html.escape(c)}</a></td>"
                f"<td>{match_badge}</td>"
                f"<td class='num'>{t['hosts_afetados']}</td>"
                f"<td class='num'>{t['total_events']}</td>"
                f"<td class='num'>{t['gpu']}</td>"
                f"<td class='num'>{t['gpu_temp']}</td>"
                f"<td class='num'>{t['sensor_temp']}</td>"
                f"<td class='num'>{t['ib']}</td>"
                "</tr>"
            )
            sections.append(
                f"<div class='cluster-section' id='{anchor}' data-cluster-section>"
                f"<h2>Cluster: {html.escape(c)}</h2>"
                + render_top_hosts_table_html(cluster_aggs[c], top_n, id_prefix=f"c{idx}")
                + "</div>"
            )

        body_main = f"""
  <h2>Índice de clusters</h2>
  <table>
    <thead>
      <tr>
        <th>Cluster</th><th>Correspondência</th><th>Hosts afetados</th><th>Total</th><th>GPU</th><th>GPU Temp</th><th>Sensor Temp</th><th>IB</th>
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
  <h2>Top hosts (consolidado)</h2>
  {render_top_hosts_table_html(report_data["global_agg"], top_n, id_prefix="g")}
  </div>
"""

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

  .toolbar {{ display: flex; align-items: center; gap: 10px; margin: 16px 0; }}
  .toolbar input[type="text"] {{
    flex: 0 1 320px; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 0.95em;
  }}
  .toolbar input[type="text"]:focus {{ outline: 2px solid #8ab4f8; border-color: #8ab4f8; }}
  #searchCount {{ color: #666; font-size: 0.85em; }}

  .badge {{
    display: inline-block; padding: 2px 9px; border-radius: 999px;
    font-size: 0.82em; font-weight: 600; white-space: nowrap;
  }}
  .badge-match {{ background: #e6f7ec; color: #0b7a3b; }}
  .badge-none {{ background: #eef1f5; color: #5b6b7c; }}

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
</style>
</head>
<body>
  <h1>Relatório – Reincidência de eventos críticos (Monit)</h1>
  <div class="sub">Ranking consolidado e evidências (Top {top_n}) — clique em um host para ver todos os eventos</div>

  <div class="note">
    <b>Orientação operacional:</b> priorizar abertura de registros individuais para hosts com maior reincidência (GPU/IB) e tratar aos poucos.
  </div>

  <div class="toolbar">
    <input type="text" id="hostSearch" placeholder="Buscar host..." oninput="filterHosts(this.value)" autocomplete="off"/>
    <span id="searchCount"></span>
  </div>
  {body_main}
  <h2>Detalhes e evidências (texto)</h2>
  <pre>{html.escape(report_data["report_txt"])}</pre>

  <script>
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
  </script>
</body>
</html>"""
    return html_out

def main():
    if len(sys.argv) < 2:
        print("Uso: python3 hpc_event_report.py <arquivo.csv> [dias] [topN]")
        sys.exit(1)

    csv_path = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) >= 3 else 60
    topn = int(sys.argv[3]) if len(sys.argv) >= 4 else 15

    rows = read_monit_csv(csv_path)
    report_data = generate_reports(rows, days_window=days, top_n=topn)

    # Saídas base
    write_csv("report_summary.csv", report_data["summary_lines"])
    write_text("report_top.txt", report_data["report_txt"])

    # Relatórios amigáveis
    md_report = render_markdown_report(report_data, top_n=topn)
    html_report = render_html_report(report_data, top_n=topn)

    write_text("report.md", md_report)
    write_text("report.html", html_report)

    print("OK: gerados report_summary.csv, report_top.txt, report.md e report.html")

if __name__ == "__main__":
    main()
