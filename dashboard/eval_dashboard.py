import json
import os
from pathlib import Path

from flask import Blueprint, render_template_string

eval_dashboard_bp = Blueprint("eval_dashboard", __name__)

RESULTS_FILE = Path(
    os.getenv(
        "EVAL_RESULTS_FILE",
        "evals/results/results.json",
    )
)


@eval_dashboard_bp.get("/eval-dashboard")
def eval_dashboard():
    if RESULTS_FILE.exists():
        try:
            data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"evaluation_status": "unavailable", "results": []}
    else:
        data = {"evaluation_status": "not_run", "results": []}

    page = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Sewa Setu Evaluation Dashboard</title>
      <style>
        :root {
          --bg-main: #f8fafc;
          --bg-card: #ffffff;
          --border-color: #e2e8f0;
          --text-primary: #0f172a;
          --text-secondary: #475569;
          --text-muted: #64748b;
          --primary-color: #2563eb;
          --pass-bg: #dcfce7;
          --pass-text: #15803d;
          --fail-bg: #fee2e2;
          --fail-text: #b91c1c;
          --notrun-bg: #fef3c7;
          --notrun-text: #b45309;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          background-color: var(--bg-main);
          color: var(--text-primary);
          line-height: 1.5;
          padding: 24px;
        }
        .container { max-width: 1200px; margin: 0 auto; }

        .header-card {
          background: var(--bg-card);
          border: 1px solid var(--border-color);
          border-radius: 12px;
          padding: 24px;
          margin-bottom: 24px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        .header-top {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 12px;
          flex-wrap: wrap;
          gap: 12px;
        }
        h1 { font-size: 1.6rem; font-weight: 700; color: var(--text-primary); }
        .badge {
          display: inline-block;
          padding: 4px 12px;
          border-radius: 9999px;
          font-size: 0.825rem;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }
        .badge-passed { background: var(--pass-bg); color: var(--pass-text); }
        .badge-failed { background: var(--fail-bg); color: var(--fail-text); }
        .badge-not_run { background: var(--notrun-bg); color: var(--notrun-text); }
        .badge-unavailable { background: #e2e8f0; color: #475569; }

        .note-banner {
          background: #eff6ff;
          border-left: 4px solid var(--primary-color);
          padding: 12px 16px;
          border-radius: 4px;
          font-size: 0.9rem;
          color: #1e40af;
          margin-top: 12px;
        }

        .stats-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 16px;
          margin-bottom: 24px;
        }
        .stat-card {
          background: var(--bg-card);
          border: 1px solid var(--border-color);
          border-radius: 10px;
          padding: 16px;
          box-shadow: 0 1px 2px rgba(0,0,0,0.03);
        }
        .stat-label { font-size: 0.85rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }
        .stat-value { font-size: 1.8rem; font-weight: 800; margin-top: 4px; }

        .progress-bar-bg {
          height: 8px;
          background: #e2e8f0;
          border-radius: 4px;
          margin-top: 8px;
          overflow: hidden;
        }
        .progress-bar-fill {
          height: 100%;
          background: #16a34a;
          width: 0%;
          transition: width 0.3s ease;
        }

        .controls-bar {
          display: flex;
          gap: 8px;
          margin-bottom: 16px;
        }
        .filter-btn {
          background: #ffffff;
          border: 1px solid var(--border-color);
          padding: 6px 14px;
          border-radius: 6px;
          font-size: 0.875rem;
          cursor: pointer;
          font-weight: 500;
          color: var(--text-secondary);
        }
        .filter-btn.active {
          background: var(--primary-color);
          color: #ffffff;
          border-color: var(--primary-color);
        }

        .table-container {
          background: var(--bg-card);
          border: 1px solid var(--border-color);
          border-radius: 12px;
          overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        table { border-collapse: collapse; width: 100%; text-align: left; }
        th {
          background: #f1f5f9;
          padding: 14px 16px;
          font-size: 0.85rem;
          font-weight: 700;
          color: var(--text-secondary);
          text-transform: uppercase;
          border-bottom: 1px solid var(--border-color);
        }
        td {
          padding: 16px;
          border-bottom: 1px solid var(--border-color);
          vertical-align: top;
          font-size: 0.925rem;
        }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background-color: #f8fafc; }

        .persona-name { font-weight: 700; font-size: 1rem; color: var(--text-primary); }
        .persona-meta { font-size: 0.85rem; color: var(--text-muted); margin-top: 2px; }
        .persona-talk { font-size: 0.825rem; color: #64748b; font-style: italic; margin-top: 6px; }

        details {
          background: #f8fafc;
          border: 1px solid var(--border-color);
          border-radius: 6px;
          padding: 8px 12px;
        }
        summary {
          font-weight: 600;
          cursor: pointer;
          color: var(--primary-color);
          font-size: 0.875rem;
        }
        pre {
          background: #0f172a;
          color: #f8fafc;
          padding: 12px;
          border-radius: 6px;
          margin-top: 8px;
          white-space: pre-wrap;
          overflow-wrap: anywhere;
          font-size: 0.825rem;
          max-height: 350px;
          overflow-y: auto;
        }

        .footer {
          margin-top: 24px;
          text-align: center;
          font-size: 0.85rem;
          color: var(--text-muted);
        }
      </style>
    </head>
    <body>
      <div class="container">
        {% set results = data.get("results", []) %}
        {% set total_count = results | length %}
        {% set passed_count = results | selectattr("status", "equalto", "passed") | list | length %}
        {% set failed_count = results | selectattr("status", "equalto", "failed") | list | length %}
        {% set not_run_count = results | selectattr("status", "equalto", "not_run") | list | length %}
        {% set completed_count = passed_count + failed_count %}
        {% if completed_count > 0 %}
          {% set pass_rate = (100 * passed_count / completed_count) | round(1) %}
        {% else %}
          {% set pass_rate = 0 %}
        {% endif %}

        <div class="header-card">
          <div class="header-top">
            <h1>Sewa Setu Evaluation Dashboard</h1>
            <span class="badge badge-{{ data.get('evaluation_status', 'not_run') }}">
              Status: {{ data.get("evaluation_status", "unknown") }}
            </span>
          </div>
          {% if data.get("note") %}
          <div class="note-banner">
            <strong>System Notice:</strong> {{ data.get("note") }}
          </div>
          {% endif %}
        </div>

        <div class="stats-grid">
          <div class="stat-card">
            <div class="stat-label">Total Personas</div>
            <div class="stat-value">{{ total_count }}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">Pass Rate</div>
            <div class="stat-value">
              {% if completed_count > 0 %}{{ pass_rate }}%{% else %}N/A{% endif %}
            </div>
            <div class="progress-bar-bg">
              <div class="progress-bar-fill" style="width: {{ pass_rate }}%;"></div>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-label">Passed / Failed</div>
            <div class="stat-value" style="color: var(--pass-text);">
              {{ passed_count }} <span style="color: var(--text-muted); font-size: 1rem;">/</span>
              <span style="color: var(--fail-text);">{{ failed_count }}</span>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-label">Pending Run</div>
            <div class="stat-value" style="color: var(--notrun-text);">{{ not_run_count }}</div>
          </div>
        </div>

        <div class="controls-bar">
          <button class="filter-btn active" onclick="filterResults('all', this)">All ({{ total_count }})</button>
          <button class="filter-btn" onclick="filterResults('passed', this)">Passed ({{ passed_count }})</button>
          <button class="filter-btn" onclick="filterResults('failed', this)">Failed ({{ failed_count }})</button>
          <button class="filter-btn" onclick="filterResults('not_run', this)">Not Run ({{ not_run_count }})</button>
        </div>

        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th style="width: 28%;">Citizen Persona</th>
                <th style="width: 22%;">Goal & Expected State</th>
                <th style="width: 12%;">Status</th>
                <th style="width: 38%;">Trace Details</th>
              </tr>
            </thead>
            <tbody>
            {% for item in results %}
              <tr class="result-row status-{{ item.get('status', 'unknown') }}">
                <td>
                  <div class="persona-name">{{ item.get("persona_name", item.get("persona_id", "")) }}</div>
                  <div class="persona-meta">
                    Age {{ item.get("age", "-") }} &bull; {{ item.get("village", "-") }}, {{ item.get("block", "-") }}
                  </div>
                  {% if item.get("how_they_talk") %}
                  <div class="persona-talk">&ldquo;{{ item.get("how_they_talk") }}&rdquo;</div>
                  {% endif %}
                </td>
                <td>
                  <strong>Goal:</strong> {{ item.get("goal", "-") }}<br><br>
                  <strong>Expected:</strong> {{ item.get("expected_portal_state", "-") }}
                </td>
                <td>
                  <span class="badge badge-{{ item.get('status', 'not_run') }}">
                    {{ item.get("status", "unknown") }}
                  </span>
                </td>
                <td>
                  <details>
                    <summary>View Conversation & Assertion Checks</summary>
                    <pre>{{ item | tojson(indent=2) }}</pre>
                  </details>
                </td>
              </tr>
            {% else %}
              <tr>
                <td colspan="4" style="text-align: center; color: var(--text-muted); padding: 32px;">
                  No evaluation results recorded.
                </td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>

        <div class="footer">
          Generated at: {{ data.get("generated_at", "Not available") }} &bull; Sewa Setu MOC Evaluation Pipeline
        </div>
      </div>

      <script>
        function filterResults(status, btn) {
          document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          const rows = document.querySelectorAll('.result-row');
          rows.forEach(row => {
            if (status === 'all' || row.classList.contains('status-' + status)) {
              row.style.display = '';
            } else {
              row.style.display = 'none';
            }
          });
        }
      </script>
    </body>
    </html>
    """
    return render_template_string(page, data=data)