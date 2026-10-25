import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals import run_evals


def test_synthetic_pdf_fixture_is_a_readable_one_page_pdf():
    sys.path.insert(0, str(ROOT / "app"))
    import app as portal

    assert run_evals.PDF_FIXTURE.startswith(b"%PDF-")
    assert portal.proof_content_is_readable(".pdf", run_evals.PDF_FIXTURE)


@pytest.mark.parametrize(
    ("database_name", "marker_table", "marker", "application_count", "expected_error"),
    [
        ("sewasetu", "evaluation_sandbox_marker", 1, 0, ValueError),
        ("sewasetu_eval", None, 1, 0, ValueError),
        ("sewasetu_eval", "evaluation_sandbox_marker", 0, 0, ValueError),
        ("sewasetu_eval", "evaluation_sandbox_marker", 1, 3, ValueError),
        ("sewasetu_eval", "evaluation_sandbox_marker", 1, 0, None),
    ],
)
def test_database_guard_requires_marked_empty_evaluation_database(
    monkeypatch,
    database_name,
    marker_table,
    marker,
    application_count,
    expected_error,
):
    responses = iter([
        (database_name, marker_table),
        (marker,),
        (application_count,),
    ])

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query, _params=None):
            pass

        def fetchone(self):
            return next(responses)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(run_evals.psycopg2, "connect", lambda _url: Connection())

    if expected_error:
        with pytest.raises(expected_error):
            run_evals.verify_disposable_database("postgresql://test")
    else:
        run_evals.verify_disposable_database("postgresql://test")


def test_results_are_atomically_saved_in_dashboard_format(tmp_path, monkeypatch):
    result_path = tmp_path / "results.json"
    monkeypatch.setattr(run_evals, "RESULTS_FILE", result_path)
    expected = [{"persona_id": "synthetic-test", "status": "passed", "checks": []}]

    run_evals.write_results(expected, "passed", "1 of 1 persona evaluations passed.")

    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert saved["evaluation_status"] == "passed"
    assert saved["results"] == expected
    assert not result_path.with_suffix(".json.tmp").exists()
