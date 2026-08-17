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

    # Contabiliza
    per_host_total = Counter()
    per_host_cat = {c: Counter() for c in CATEGORIES}

    # Para exemplos de "evidência" (2-3 trechos por host/categoria) no texto/markdown
    samples = defaultdict(lambda: defaultdict(list))

    # Lista completa de eventos por host, usada no drill-down interativo do HTML
    host_events = defaultdict(list)

    for r in filtered:
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

        detail_clean = (r["Detalhes"] or "").replace("\r", "").strip()
        detail_clean = re.sub(r"\s+", " ", detail_clean)
        host_events[host].append({
            "date": r["Date"],
            "dt": r["Date_dt"],
            "status": r["Status"],
            "cats": cats,
            "detail": detail_clean,
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

    # Saída CSV: por host
    summary_lines = []
    summary_lines.append(["Hostname", "total_events", "gpu", "gpu_temp", "sensor_temp", "ib"])
    for h in hosts_sorted:
        summary_lines.append([
            h,
            str(per_host_total[h]),
            str(per_host_cat["gpu"][h]),
            str(per_host_cat["gpu_temp"][h]),
            str(per_host_cat["sensor_temp"][h]),
            str(per_host_cat["ib"][h]),
        ])

    # Texto do relatório (report_top.txt)
    date_min = min((r["Date_dt"] for r in filtered), default=None)
    date_max = max((r["Date_dt"] for r in filtered), default=None)

    report = []
    report.append("RELATÓRIO – REINCIDÊNCIA DE EVENTOS CRÍTICOS (Monit)")
    report.append(f"Janela analisada (últimos {days_window} dias): {start:%Y-%m-%d %H:%M:%S} até {now:%Y-%m-%d %H:%M:%S}")
    report.append(f"Eventos considerados (linhas filtradas): {len(filtered)}")
    if date_min and date_max:
        report.append(f"Range de datas dentro do CSV: {date_min:%Y-%m-%d %H:%M:%S} até {date_max:%Y-%m-%d %H:%M:%S}")
    report.append("")
    report.append(f"TOP {top_n} hosts (consolidado):")
    report.append("Hostname | total | gpu | gpu_temp | sensor_temp | ib")
    for h in hosts_sorted[:top_n]:
        report.append(
            f"{h} | {per_host_total[h]} | {per_host_cat['gpu'][h]} | {per_host_cat['gpu_temp'][h]} | "
            f"{per_host_cat['sensor_temp'][h]} | {per_host_cat['ib'][h]}"
        )

    report.append("")
    report.append("TOP por categoria:")
    for c in CATEGORIES:
        report.append(f"- {c}:")
        top = per_host_cat[c].most_common(top_n)
        if not top:
            report.append("  (sem ocorrências)")
        else:
            for h, v in top:
                report.append(f"  {h}: {v}")

    report.append("")
    report.append("Evidências (amostras por host/categoria – até 3 exemplos):")
    for h in hosts_sorted[:top_n]:
        for c in CATEGORIES:
            if per_host_cat[c][h] > 0:
                report.append(f"* {h} / {c} ({per_host_cat[c][h]} ocorrências):")
                for s in samples[h][c]:
                    report.append(f"  - {s}")

    report.append("")
    report.append("Recomendação operacional:")
    report.append("- Abrir registros individuais para os hosts com reincidência muito acima dos demais, priorizando GPU e IB.")
    report.append("- Tratar aos poucos (1 host por vez) junto ao fornecedor, anexando este relatório e os logs do host quando necessário.")

    return summary_lines, "\n".join(report), host_events

def write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerows(rows)

def write_text(path, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def csv_rows_to_dicts(summary_lines):
    header = summary_lines[0]
    out = []
    for r in summary_lines[1:]:
        d = dict(zip(header, r))
        for k in ["total_events", "gpu", "gpu_temp", "sensor_temp", "ib"]:
            d[k] = int(d.get(k, "0") or "0")
        out.append(d)
    return out

def render_markdown_report(summary_lines, report_txt: str, top_n: int = 15):
    rows = csv_rows_to_dicts(summary_lines)
    top = rows[:top_n]

    md = []
    md.append("# Relatório – Reincidência de eventos críticos (Monit)")
    md.append("")
    md.append("## Ranking consolidado (Top hosts)")
    md.append("")
    md.append("| Hostname | Total | GPU | GPU Temp | Sensor Temp | IB |")
    md.append("|---|---:|---:|---:|---:|---:|")
    for r in top:
        md.append(f"| {r['Hostname']} | {r['total_events']} | {r['gpu']} | {r['gpu_temp']} | {r['sensor_temp']} | {r['ib']} |")
    md.append("")
    md.append("## Detalhes e evidências (texto)")
    md.append("```")
    md.append(report_txt.strip())
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
        cats = ", ".join(sorted(e["cats"])) if e["cats"] else "-"
        rows_html.append(
            "<tr>"
            f"<td class='mono'>{html.escape(e['date'])}</td>"
            f"<td>{html.escape(e['status'])}</td>"
            f"<td>{html.escape(cats)}</td>"
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

def render_html_report(summary_lines, report_txt: str, host_events: dict, top_n: int = 15):
    rows = csv_rows_to_dicts(summary_lines)
    top = rows[:top_n]
    max_total = max([r["total_events"] for r in top], default=1)

    def bar(v):
        pct = int((v / max_total) * 100) if max_total else 0
        return f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'

    table_rows = []
    for i, r in enumerate(top):
        host = r["Hostname"]
        detail_id = f"detail-{i}"
        table_rows.append(
            f"<tr class='host-row' onclick=\"toggleDetail('{detail_id}', {i})\">"
            "<td class='mono'>"
            f"<span class='chevron' id='chevron-{i}'>&#9656;</span> {html.escape(host)}"
            "</td>"
            f"<td class='num'>{r['total_events']}</td>"
            f"<td>{bar(r['total_events'])}</td>"
            f"<td class='num'>{r['gpu']}</td>"
            f"<td class='num'>{r['gpu_temp']}</td>"
            f"<td class='num'>{r['sensor_temp']}</td>"
            f"<td class='num'>{r['ib']}</td>"
            "</tr>"
        )
        table_rows.append(
            f"<tr class='detail-row' id='{detail_id}'>"
            "<td colspan='7'>"
            f"<div class='detail-wrap'>{render_host_detail_table(host, host_events)}</div>"
            "</td></tr>"
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

  <h2>Top hosts (consolidado)</h2>
  <table>
    <thead>
      <tr>
        <th>Hostname</th>
        <th>Total</th>
        <th>Visual</th>
        <th>GPU</th>
        <th>GPU Temp</th>
        <th>Sensor Temp</th>
        <th>IB</th>
      </tr>
    </thead>
    <tbody>
      {''.join(table_rows)}
    </tbody>
  </table>

  <h2>Detalhes e evidências (texto)</h2>
  <pre>{html.escape(report_txt)}</pre>

  <script>
    function toggleDetail(id, idx) {{
      var row = document.getElementById(id);
      var chevron = document.getElementById('chevron-' + idx);
      if (!row) return;
      row.classList.toggle('open');
      if (chevron) chevron.classList.toggle('rotated');
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
    summary_csv, report_txt, host_events = generate_reports(rows, days_window=days, top_n=topn)

    # Saídas base
    write_csv("report_summary.csv", summary_csv)
    write_text("report_top.txt", report_txt)

    # Relatórios amigáveis
    md_report = render_markdown_report(summary_csv, report_txt, top_n=topn)
    html_report = render_html_report(summary_csv, report_txt, host_events, top_n=topn)

    write_text("report.md", md_report)
    write_text("report.html", html_report)

    print("OK: gerados report_summary.csv, report_top.txt, report.md e report.html")

if __name__ == "__main__":
    main()
