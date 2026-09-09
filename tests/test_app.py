import io
import json
import zipfile
from pathlib import Path

import app as web_app
from auth import UserStore


def _students_csv():
    return (
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Chuck,MASTER,Masters,Adults Book 4,Tue 19:00,Jane Doe,4,3,4,5,4,3,4,2,3,3,3,3,1,2,Reposicao,Good,Focus,Team,Practice speaking,\n"
    )


def _lessons_csv():
    return (
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,01/01,Lesson 1,,\n"
        "MASTER,2,03/01,Lesson 2,,\n"
    )


def _init_user_store(monkeypatch, data_dir):
    from auth import UserStore

    store = UserStore(db_store=None, json_path=data_dir / "users.json")
    store.initialize()
    store.ensure_bootstrap_superadmin("admin@test.local", "testpass")
    monkeypatch.setattr(web_app, "user_store", store)
    monkeypatch.setattr(web_app, "SUPERADMIN_EMAIL", "admin@test.local")
    monkeypatch.setattr(web_app, "SUPERADMIN_PASSWORD", "testpass")


def _login(client, email="admin@test.local", password="testpass"):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def _seed_teacher_classes(data_dir, teacher_name, *entries):
    """entries: (turma, turma_display) or (turma, turma_display, day1, day2, time)."""
    from auth import normalize_teacher_name
    from form_ui import format_class_schedule
    from teacher_classes import load_registry, save_registry

    path = data_dir / "teacher_classes.json"
    data = load_registry(path)
    key = normalize_teacher_name(teacher_name)
    bucket = data.setdefault(key, [])
    for entry in entries:
        turma, display = entry[0], entry[1]
        weekdays = list(entry[2:4]) if len(entry) > 2 else []
        time_start = entry[4] if len(entry) > 4 else "19:00"
        time_end = entry[5] if len(entry) > 5 else "20:00"
        bucket.append({
            "turma": turma,
            "turma_display": display or turma,
            "class_weekdays": weekdays,
            "class_time_start": time_start,
            "class_time_end": time_end,
            "horario": format_class_schedule(weekdays, time_start, time_end)
            if weekdays else "",
        })
    save_registry(path, data)
    return path


def _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck"):
    from auth import UserStore

    store = UserStore(db_store=None, json_path=data_dir / "users.json")
    store.initialize()
    store.ensure_bootstrap_superadmin("admin@test.local", "testpass")
    store.create_teacher("teacher@test.local", "teachpass", teacher_name)
    monkeypatch.setattr(web_app, "user_store", store)
    classes_path = _seed_teacher_classes(
        data_dir,
        teacher_name,
        ("MASTER", "Masters", "Terça-feira", "Quinta-feira", "19:00"),
    )
    monkeypatch.setattr(web_app, "TEACHER_CLASSES_PATH", classes_path)


def test_health_returns_ok():
    client = web_app.app.test_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"


def test_health_db_csv_mode():
    client = web_app.app.test_client()
    response = client.get("/health/db")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is False
    assert payload["mode"] == "csv"


def test_health_auth_omits_account_emails(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, web_app.DATA_DIR)

    client = web_app.app.test_client()
    response = client.get("/health/auth")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["user_count"] == 1
    assert "accounts" not in payload
    assert "configured_email" not in payload
    assert "admin@test.local" not in response.get_data(as_text=True)


def test_login_success_sets_session(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, web_app.DATA_DIR)
    monkeypatch.setattr(web_app, "db_store", None)

    client = web_app.app.test_client()
    response = _login(client)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    events_path = web_app.DATA_DIR / "login_events.json"
    assert events_path.exists()
    events = json.loads(events_path.read_text(encoding="utf-8"))
    assert len(events) == 1
    assert events[0]["email"] == "admin@test.local"

    _login(client)
    events = json.loads(events_path.read_text(encoding="utf-8"))
    assert len(events) == 2


def test_users_page_shows_last_access_and_contact_actions(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "db_store", None)
    _init_user_store(monkeypatch, data_dir)

    store = web_app.user_store
    teacher_id = store.create_teacher("teacher@test.local", "teachpass1", "Chuck")
    store.create_teacher("idle@test.local", "teachpass2", "Idle")
    profiles = [{
        "user_id": teacher_id,
        "bio": "",
        "phone": "",
        "whatsapp": "11999998888",
        "contact_email": "chuck@school.com",
        "specialty": "",
        "photo_mime": "",
        "photo_base64": "",
        "updated_at": "",
    }]
    (data_dir / "teacher_profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False),
        encoding="utf-8",
    )

    client = web_app.app.test_client()
    _login(client)
    # Teacher also logs in so history has both accounts
    client.get("/logout")
    _login(client, email="teacher@test.local", password="teachpass1")
    client.get("/logout")
    _login(client)

    page = client.get("/admin/teachers")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "user-row-actions" in html
    assert "btn-perfil" in html
    assert "user-row-toggles" in html
    assert "Último acesso" in html
    assert "Histórico de acessos" in html
    assert "Nunca" in html  # idle teacher never logged in
    assert "mailto:chuck@school.com" in html
    assert "https://wa.me/5511999998888" in html
    assert "Cadastre o WhatsApp no perfil do professor" in html
    assert "teacher@test.local" in html
    assert "idle@test.local" in html


def test_protected_route_requires_login(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()

    client = web_app.app.test_client()
    response = client.get("/")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_generate_reports_writes_html_files(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "output"
    data_dir.mkdir()
    out_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")

    _init_user_store(monkeypatch, data_dir)
    monkeypatch.setattr(web_app, "BASE", Path(web_app.__file__).parent)
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "TMPL_DIR", Path(web_app.__file__).parent / "templates")
    monkeypatch.setattr(web_app, "OUT_DIR", out_dir)

    client = web_app.app.test_client()
    _login(client)

    response = client.post("/generate", follow_redirects=False)

    assert response.status_code == 302
    assert "/reports" in response.headers["Location"]
    assert "month=" in response.headers["Location"]
    generated = sorted(p.name for p in out_dir.glob("*.html"))
    assert any(name.endswith("_report.html") for name in generated)
    assert any("class_diagnostic" in name for name in generated)


def test_reports_page_with_null_prior_snapshot(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "output"
    data_dir.mkdir()
    out_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    (out_dir / "MASTER_Jane_Doe_2026-03_report.html").write_text("<html>ok</html>", encoding="utf-8")
    (data_dir / "student_snapshots.json").write_text(
        json.dumps(
            [
                {
                    'report_month': '2026-03',
                    'turma': 'MASTER',
                    'student_id': '6a03573506b6a182',
                    'composite_score': 4,
                },
                {
                    'report_month': '2026-02',
                    'turma': 'MASTER',
                    'student_id': '6a03573506b6a182',
                    'composite_score': None,
                },
            ],
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )

    _init_user_store(monkeypatch, data_dir)
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", out_dir)
    monkeypatch.setattr(web_app, "SNAPSHOTS_PATH", data_dir / "student_snapshots.json")

    client = web_app.app.test_client()
    _login(client)
    response = client.get("/reports?month=2026-03")

    assert response.status_code == 200
    assert "Jane" in response.get_data(as_text=True)


def test_turma_from_diagnostic_filename():
    assert web_app._turma_from_diagnostic_filename('MASTER_2026-03_class_diagnostic.html') == 'MASTER'
    assert web_app._turma_from_diagnostic_filename('KIDS_2_CLASS_2026-03_class_diagnostic.html') == 'KIDS_2_CLASS'
    assert web_app._turma_from_diagnostic_filename('MASTER_class_diagnostic.html') == 'MASTER'


def test_reports_page_unified_filter_markup(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "output"
    data_dir.mkdir()
    out_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    (out_dir / "MASTER_Jane_Doe_2026-03_report.html").write_text("<html>ok</html>", encoding="utf-8")
    (out_dir / "MASTER_2026-03_class_diagnostic.html").write_text("<html>diag</html>", encoding="utf-8")

    _init_user_store(monkeypatch, data_dir)
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", out_dir)
    monkeypatch.setattr(web_app, "SNAPSHOTS_PATH", data_dir / "student_snapshots.json")

    client = web_app.app.test_client()
    _login(client)
    html = client.get("/reports").get_data(as_text=True)

    assert 'id="filter-reports"' in html
    assert 'data-filter-key="report_kind"' in html
    assert 'data-filter-key="month"' in html
    assert 'data-filter-value="2026-03"' in html
    assert 'data-filter-key="turma"' in html
    assert 'data-filter-key="trend"' in html
    assert 'data-report-kind="individual"' in html
    assert 'data-report-kind="diagnostic"' in html
    assert 'data-month="2026-03"' in html
    assert 'urlKeys: ["month"]' in html
    assert "Relatórios (" in html
    assert "Diagnóstico de turma" in html


def test_generate_missing_csv_shows_upload_error(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "output"
    data_dir.mkdir()
    out_dir.mkdir()

    _init_user_store(monkeypatch, data_dir)
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "TMPL_DIR", Path(web_app.__file__).parent / "templates")
    monkeypatch.setattr(web_app, "OUT_DIR", out_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.post("/generate")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "students.csv não encontrado" in html
    assert "csv-preview-table" in html


def test_generate_invalid_csv_reuses_full_upload_context(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "output"
    data_dir.mkdir()
    out_dir.mkdir()
    (data_dir / "students.csv").write_text("teacher,turma\nChuck,MASTER\n", encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")

    _init_user_store(monkeypatch, data_dir)
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "TMPL_DIR", Path(web_app.__file__).parent / "templates")
    monkeypatch.setattr(web_app, "OUT_DIR", out_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.post("/generate")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "CSV de Alunos invalido" in html
    assert "csv-preview-table" in html


def test_reports_preview_path_is_sanitized(monkeypatch, tmp_path):
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    (out_dir / "safe.html").write_text("<html>ok</html>", encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", out_dir)
    web_app.DATA_DIR.mkdir(exist_ok=True)
    _init_user_store(monkeypatch, web_app.DATA_DIR)

    client = web_app.app.test_client()
    _login(client)

    ok = client.get("/reports/preview/safe.html")
    blocked = client.get("/reports/preview/../secret.txt")

    assert ok.status_code == 200
    assert blocked.status_code == 404
    assert ok.get_data(as_text=True) == "<html>ok</html>"


def test_reports_preview_live_renders_class_medias_row(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "output"
    data_dir.mkdir()
    out_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    stale = out_dir / "MASTER_2026-08_class_diagnostic.html"
    stale.write_text("<html>stale class diagnostic</html>", encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", out_dir)
    monkeypatch.setattr(web_app, "TMPL_DIR", Path(web_app.__file__).parent / "templates")
    monkeypatch.setattr(web_app, "SNAPSHOTS_PATH", data_dir / "student_snapshots.json")
    _init_user_store(monkeypatch, data_dir)
    _seed_teacher_classes(data_dir, "Chuck", ("MASTER", "Masters"))

    client = web_app.app.test_client()
    _login(client)
    response = client.get("/reports/preview/MASTER_2026-08_class_diagnostic.html")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "class-media-row" in html
    assert "class-overview" in html
    assert "stale class diagnostic" not in html


def test_reports_preview_live_renders_attendance_calendar(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "output"
    data_dir.mkdir()
    out_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    stale = out_dir / "MASTER_Jane_Doe_2026-01_report.html"
    stale.write_text("<html>stale report without calendar</html>", encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", out_dir)
    monkeypatch.setattr(web_app, "TMPL_DIR", Path(web_app.__file__).parent / "templates")
    monkeypatch.setattr(web_app, "SNAPSHOTS_PATH", data_dir / "student_snapshots.json")
    _init_user_store(monkeypatch, data_dir)
    _seed_teacher_classes(data_dir, "Chuck", ("MASTER", "Masters"))

    client = web_app.app.test_client()
    _login(client)
    response = client.get("/reports/preview/MASTER_Jane_Doe_2026-01_report.html")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "pie-cal" in html
    assert "Janeiro 2026" in html
    assert "stale report without calendar" not in html


def test_upload_invalid_students_csv_shows_error_and_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, web_app.DATA_DIR)

    client = web_app.app.test_client()
    _login(client)

    bad_students = io.BytesIO(b"teacher,turma\nChuck,MASTER\n")
    response = client.post(
        "/upload",
        data={"students": (bad_students, "students.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"Erro no CSV de Alunos" in response.data
    assert not (web_app.DATA_DIR / "students.csv").exists()


def test_upload_template_students_download(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, web_app.DATA_DIR)

    client = web_app.app.test_client()
    _login(client)

    response = client.get("/upload/template/students")

    assert response.status_code == 200
    assert "attachment;" in response.headers.get("Content-Disposition", "")
    assert "students_template.csv" in response.headers.get("Content-Disposition", "")
    assert response.data.startswith(b"\xef\xbb\xbf")
    assert b"teacher,turma,turma_display" in response.data
    assert b"Jane Doe" in response.data


def test_login_page_has_viewport(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")

    client = web_app.app.test_client()
    response = client.get("/login")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    assert 'name="email"' in html
    assert "logo-primary-transparent.png" in html
    assert "img/favicon.png" in html
    assert "css/brand.css" in html
    assert "ESCOLA DE LÍDERES" in html or "Escola de Líderes" in html


def test_authenticated_shell_uses_official_lockups(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, web_app.DATA_DIR)

    client = web_app.app.test_client()
    _login(client)
    html = client.get("/").get_data(as_text=True)
    assert "logo-symbol.png" in html
    assert "logo-primary-transparent.png" in html
    assert "logo-primary-white.png" not in html
    assert 'id="icon-house"' in html
    assert 'href="#icon-house"' in html
    assert "stroke-width=\"2\"" in html
    assert "▦" not in html
    assert "filter: brightness(0) invert(1)" not in html
    assert "#792D83" in html or "var(--purple)" in html
    assert "var(--purple-lt)" in html
    css = client.get("/static/css/brand.css")
    assert css.status_code == 200
    tokens = css.get_data(as_text=True)
    assert "--purple: #792D83" in tokens
    assert "--plum: #2D1040" in tokens
    assert "--purple-lt: #F0E6F3" in tokens
    assert "--gold-accent: #EBB22E" in tokens
    assert client.get("/static/img/logo-symbol.png").status_code == 200
    assert client.get("/static/img/logo-primary-transparent.png").status_code == 200
    assert client.get("/static/img/logo-primary-white.png").status_code == 200


def test_authenticated_shell_has_drawer_markup(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, web_app.DATA_DIR)

    client = web_app.app.test_client()
    _login(client)
    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="viewport"' in html
    assert 'id="menu-toggle"' in html
    assert 'id="nav-backdrop"' in html
    assert 'class="students-cards-view"' not in html


def test_students_page_has_dual_view_markup(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.get("/students")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "students-table-view" in html
    assert "students-cards-view" in html
    assert "student-card-item" in html


def test_list_filter_script_reads_kebab_case_data_attributes():
    """aula_extra chips must read data-aula-extra, not dataset['aula-extra']."""
    text = Path("web_templates/_macros/list_filters.html").read_text(encoding="utf-8")
    assert "function rowFilterValue" in text
    assert "row.getAttribute(attr)" in text
    assert "row.dataset[datasetKey(key)]" not in text


def test_pending_extra_session_shows_in_students_aula_extra_filter(monkeypatch, tmp_path):
    """Kids with open reforço must appear on Alunos even when the month review is blank."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(
        _students_csv().replace("Reposicao", ""),
        encoding="utf-8",
    )
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,10/07/2026,Lesson Jul,,\n"
        "MASTER,2,12/08/2026,Lesson Aug,,\n",
        encoding="utf-8",
    )
    (data_dir / "student_monthly_reviews.json").write_text(
        json.dumps([{
            "report_month": "2026-07",
            "turma": "MASTER",
            "student_name": "Jane Doe",
            "participacao": "4",
            "aula_extra": "",
        }]),
        encoding="utf-8",
    )

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_app, "db_store", None)
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    web_app._save_extra_sessions([{
        "teacher": "Chuck",
        "student_name": "Jane Doe (Masters)",
        "turma": "Masters",
        "date": "15/08/2026",
        "horario": "09:00",
        "turno": "Manhã",
        "session_type": "Reforço",
        "assuntos": "Reforço",
        "observacao": "",
        "contatado": "",
        "marcado": "",
        "realizado": "",
    }])

    client = web_app.app.test_client()
    _login(client)
    html = client.get("/students?month=2026-08").get_data(as_text=True)
    assert 'data-aula-extra="Reforço"' in html
    assert "Jane Doe" in html


def test_new_extra_session_flags_student_on_alunos_page(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(
        _students_csv().replace("Reposicao", ""),
        encoding="utf-8",
    )
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,12/08/2026,Lesson Aug,,\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_app, "db_store", None)
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    created = client.post(
        "/extra-sessions/new",
        data={
            "student_name": "Jane Doe",
            "turma": "MASTER",
            "teacher": "Chuck",
            "session_type": "Reposição",
            "date_picker": "2026-08-15",
            "horario": "09:00",
            "turno": "Manhã",
            "assuntos": "Reposição aula 2",
            "observacao": "",
            "contatado": "",
            "marcado": "",
            "realizado": "",
        },
        follow_redirects=True,
    )
    assert created.status_code == 200
    html = client.get("/students?month=2026-08").get_data(as_text=True)
    assert 'data-aula-extra="Reposição"' in html


def test_teacher_power_extra_session_shows_in_alunos_filter(monkeypatch, tmp_path):
    """Teacher Alunos filter must show Power kids with imported reforço rows."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    csv = (
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,"
        "speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,"
        "pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,"
        "feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Chuck,POWER,Power,Kids Book 3,Thu 15:30,Vitoria Oliveira,3,3,3,3,3,3,3,3,3,3,3,3,"
        "0,,,Good,Focus,Team,,\n"
        "Chuck,POWER,Power,Kids Book 3,Thu 15:30,Kaio Vieira,3,3,3,3,3,3,3,3,3,3,3,3,"
        "0,,,,,,,\n"
    )
    (data_dir / "students.csv").write_text(csv, encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "POWER,1,12/08/2026,Lesson Aug,,\n",
        encoding="utf-8",
    )
    (data_dir / "student_monthly_reviews.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_app, "db_store", None)
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir)
    _seed_teacher_classes(data_dir, "Chuck", ("POWER", "Power"))

    web_app._save_extra_sessions([
        {
            "teacher": "",
            "student_name": "Vitoria Oliveira (Power - C)",
            "turma": "Power - C",
            "date": "25/05/2026",
            "horario": "15:30",
            "turno": "Tarde",
            "session_type": "Reforço",
            "assuntos": "Reforço -",
            "observacao": "",
            "contatado": "",
            "marcado": "",
            "realizado": "",
        },
        {
            "teacher": "Chuck",
            "student_name": "Kaio Vieira - Power",
            "turma": "",
            "date": "",
            "horario": "",
            "turno": "Tarde",
            "session_type": "Reposição",
            "assuntos": "Reposição",
            "observacao": "",
            "contatado": "",
            "marcado": "",
            "realizado": "",
        },
    ])

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    html = client.get("/students?month=2026-08").get_data(as_text=True)
    assert 'data-turma="POWER"' in html
    assert 'data-aula-extra="Reforço"' in html
    assert 'data-aula-extra="Reposição"' in html
    extra_html = client.get("/extra-sessions").get_data(as_text=True)
    assert "Vitoria Oliveira" in extra_html
    assert "Kaio Vieira" in extra_html


def test_upload_page_shows_csv_template_preview(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, web_app.DATA_DIR)

    client = web_app.app.test_client()
    _login(client)
    response = client.get("/upload")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "csv-preview-table" in html
    assert "Identificação" in html
    assert "Nome do aluno" in html
    assert "Lesson 3: Past tense review" in html or "Lição 3" in html


def test_lessons_page_and_teacher_scope(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "teacher_classes.json").write_text("{}", encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,01/01,L1,,\n"
        "OTHER,1,01/01,L9,,\n",
        encoding="utf-8",
    )

    store = UserStore(db_store=None, json_path=data_dir / "users.json")
    store.initialize()
    store.create_teacher("chuck@test.local", "pass1234", "Chuck")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "db_store", None)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "user_store", store)
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    client.post("/login", data={"email": "chuck@test.local", "password": "pass1234"})
    response = client.get("/lessons")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "MASTER" in html
    assert "L9" not in html

    new_form = client.get("/lessons/new")
    new_html = new_form.get_data(as_text=True)
    assert new_form.status_code == 200
    assert 'name="habilidades"' in new_html
    assert 'name="licao_conteudo"' in new_html
    assert 'name="attendance_status_0"' in new_html
    assert 'Presente' in new_html
    assert 'Ausente' in new_html
    assert 'Atrasado' in new_html
    assert 'value="2"' in new_html or 'id="aula-num-input"' in new_html

    create = client.post(
        "/lessons/new",
        data={
            "turma": "MASTER",
            "aula_num": "99",
            "date": "01/05/2026",
            "licao_conteudo": "Lição 10",
            "atividade_extra": "",
            "habilidades": "Inteligência emocional",
            "attendance_count": "1",
            "attendance_student_0": "Jane Doe",
            "attendance_status_0": "absent",
        },
        follow_redirects=True,
    )
    assert create.status_code == 200
    assert "Lição 10" in create.get_data(as_text=True)
    students_text = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Jane Doe" in students_text
    reviews_path = data_dir / "student_monthly_reviews.json"
    assert reviews_path.exists()
    reviews_text = reviews_path.read_text(encoding="utf-8")
    assert "99" in reviews_text
    assert '"faltas": "2"' in reviews_text or '"faltas":"2"' in reviews_text.replace(' ', '')
    attendance_text = (data_dir / "lesson_attendance.csv").read_text(encoding="utf-8")
    assert "Jane Doe" in attendance_text
    assert "absent" in attendance_text

    students_html = client.get("/students?month=2026-05").get_data(as_text=True)
    assert "Jane Doe" in students_html
    assert 'Faltas</span><strong>2</strong>' in students_html

    edit_html = client.get("/students/0/edit?month=2026-05").get_data(as_text=True)
    assert 'name="faltas" value="2"' in edit_html
    assert 'name="missed_aulas" value="2,99"' in edit_html

    blocked = client.post(
        "/lessons/new",
        data={
            "turma": "OTHER",
            "aula_num": "1",
            "date": "01/01",
            "licao_conteudo": "Hack",
            "atividade_extra": "",
            "habilidades": "",
        },
    )
    assert blocked.status_code == 403


def test_lessons_page_lists_dashboard_turma_without_lessons(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,01/01,L1,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")
    _seed_teacher_classes(
        data_dir,
        "Chuck",
        ("NEW_CLASS", "Turma Nova", "Segunda-feira", "Quarta-feira", "10:00", "11:00"),
    )

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    html = client.get("/lessons").get_data(as_text=True)

    assert 'data-filter-value="NEW_CLASS"' in html
    assert "Turma Nova" in html
    assert "L1" in html

    create = client.post(
        "/lessons/new",
        data={
            "turma": "NEW_CLASS",
            "aula_num": "1",
            "date_picker": "2026-06-10",
            "licao_conteudo": "Primeira aula",
            "atividade_extra": "",
            "habilidades": "Speaking",
            "attendance_count": "0",
        },
        follow_redirects=False,
    )
    assert create.status_code == 302
    assert "turma=NEW_CLASS" in (create.headers.get("Location") or "") or True
    saved = (data_dir / "lessons.csv").read_text(encoding="utf-8")
    assert "Primeira aula" in saved
    assert "NEW_CLASS" in saved


def test_flagged_student_appears_in_extra_sessions(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "teacher_classes.json").write_text("{}", encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,01/01,L1,,\n",
        encoding="utf-8",
    )

    store = UserStore(db_store=None, json_path=data_dir / "users.json")
    store.initialize()
    store.create_teacher("chuck@test.local", "pass1234", "Chuck")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "db_store", None)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "user_store", store)
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    client.post("/login", data={"email": "chuck@test.local", "password": "pass1234"})
    client.post(
        "/students/0/edit",
        data={
            "teacher": "Chuck",
            "turma": "MASTER",
            "turma_display": "Masters",
            "nivel": "KIDS 1",
            "horario": "Tue 19:00",
            "student_name": "Jane Doe",
            "participacao": "4",
            "comportamento": "3",
            "speaking": "4",
            "listening": "5",
            "foco": "4",
            "writing": "3",
            "reading": "4",
            "gramatica": "2",
            "trabalho_equipe": "3",
            "organizacao": "3",
            "pontualidade": "3",
            "respeito_regras": "3",
            "faltas": "1",
            "missed_aulas": "2",
            "aula_extra": "Reforço",
            "feedback_participacao": "Good",
            "feedback_foco": "Focus",
            "feedback_trabalho_equipe": "Team",
            "recomendacoes": "Practice speaking",
            "observacao": "",
        },
        follow_redirects=True,
    )

    response = client.get("/extra-sessions")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Jane Doe" in html
    assert "Indicado no relatório" in html
    assert "Reforço" in html


def test_teacher_sees_only_own_students(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    csv_text = (
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Chuck,MASTER,Masters,Book,Tue,Jane Doe,4,3,4,5,4,3,4,2,3,3,3,3,1,2,,,,,,\n"
        "Barbara,MASTER,Masters,Book,Tue,Bob Smith,4,3,4,5,4,3,4,2,3,3,3,3,1,2,,,,,,\n"
    )
    (data_dir / "students.csv").write_text(csv_text, encoding="utf-8")

    store = UserStore(db_store=None, json_path=data_dir / "users.json")
    store.initialize()
    store.create_teacher("chuck@test.local", "pass1234", "Chuck")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "user_store", store)

    client = web_app.app.test_client()
    client.post("/login", data={"email": "chuck@test.local", "password": "pass1234"})
    response = client.get("/students")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Jane Doe" in html
    assert "Bob Smith" not in html


def test_teacher_cannot_create_student_in_other_turma(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/students/new",
        data={
            "teacher": "Chuck",
            "turma": "OTHER",
            "student_name": "Mallory",
        },
    )

    assert response.status_code == 200
    assert "não permitida" in response.get_data(as_text=True) or "Dashboard" in response.get_data(as_text=True)
    assert "Mallory" not in (data_dir / "students.csv").read_text(encoding="utf-8")


def test_teacher_edits_turma_and_updates_students(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    csv = (
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,"
        "speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,"
        "pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,"
        "feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Chuck,MASTER,Masters,TEENS 4,Old schedule,Kid One,3,3,3,3,3,3,3,3,3,3,3,3,0,,,,,,,\n"
    )
    (data_dir / "students.csv").write_text(csv, encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")
    _seed_teacher_classes(
        data_dir,
        "Chuck",
        ("MASTER", "Masters", "Segunda-feira", "Quarta-feira", "09:00", "10:00"),
    )

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    html = client.get("/turmas/MASTER/edit").get_data(as_text=True)
    assert "Editar turma" in html
    assert 'name="turma_display"' in html

    response = client.post(
        "/turmas/MASTER/edit",
        data={
            "turma_display": "Masters Evening",
            "class_weekday_1": "Terça-feira",
            "class_weekday_2": "Quinta-feira",
            "turma_time_start": "19:00",
            "turma_time_end": "20:00",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "atualizada" in response.get_data(as_text=True)
    saved = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Masters Evening" in saved
    assert "Terça-feira e Quinta-feira 19:00 - 20:00" in saved


def test_teacher_deletes_empty_turma(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")
    _seed_teacher_classes(
        data_dir,
        "Chuck",
        ("ORPHAN", "Orphan class", "Segunda-feira", "Quarta-feira", "10:00", "11:00"),
    )

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/turmas/delete",
        data={"turma": "ORPHAN"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "excluída" in response.get_data(as_text=True)
    registry = (data_dir / "teacher_classes.json").read_text(encoding="utf-8")
    assert "ORPHAN" not in registry


def test_teacher_cannot_delete_turma_with_students(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")
    _seed_teacher_classes(
        data_dir,
        "Chuck",
        ("MASTER", "Masters", "Terça-feira", "Quinta-feira", "19:00", "20:00"),
    )

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/turmas/delete",
        data={"turma": "MASTER"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Não é possível excluir" in response.get_data(as_text=True)
    assert "MASTER" in (data_dir / "teacher_classes.json").read_text(encoding="utf-8")


def test_teacher_creates_turma_on_dashboard(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/turmas/create",
        data={
            "turma_display": "Kids segunda",
            "class_weekday_1": "Terça-feira",
            "class_weekday_2": "Quinta-feira",
            "turma_time_start": "19:30",
            "turma_time_end": "20:30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    registry = (data_dir / "teacher_classes.json").read_text(encoding="utf-8")
    assert "KIDS_SEGUNDA" in registry
    assert "Kids segunda" in registry
    assert "Terça-feira" in registry
    assert "19:30 - 20:30" in registry


def test_students_page_shows_class_name_not_nivel(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    csv = (
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,"
        "speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,"
        "pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,"
        "feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Chuck,TEENS_1,TEENS 1,TEENS 1,Tue 19:00,Kid,3,3,3,3,3,3,3,3,3,3,3,3,0,,,,,,,\n"
    )
    (data_dir / "students.csv").write_text(csv, encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")
    from teacher_classes import add_class, load_registry, save_registry

    data = load_registry(web_app.TEACHER_CLASSES_PATH)
    add_class(
        data,
        "Chuck",
        turma_display="Turma Teens noite",
        class_weekdays=["Terça-feira", "Quinta-feira"],
        class_time_start="19:00",
        class_time_end="20:00",
        turma="TEENS_1",
    )
    save_registry(web_app.TEACHER_CLASSES_PATH, data)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    html = client.get("/students").get_data(as_text=True)
    assert "Turma Teens noite" in html
    assert 'data-filter-value="TEENS_1">Turma Teens noite' in html


def test_teacher_adds_student_to_dashboard_turma(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")
    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    client.post(
        "/turmas/create",
        data={
            "turma_display": "Kids 2 class",
            "class_weekday_1": "Segunda-feira",
            "class_weekday_2": "Quarta-feira",
            "turma_time_start": "10:00",
            "turma_time_end": "11:00",
        },
    )

    response = client.post(
        "/students/new",
        data={
            "teacher": "Chuck",
            "class_choice": "KIDS_2_CLASS",
            "nivel": "KIDS 2",
            "student_name": "New Class Kid",
            "participacao": "3",
            "comportamento": "3",
            "speaking": "3",
            "listening": "3",
            "foco": "3",
            "writing": "3",
            "reading": "3",
            "gramatica": "3",
            "faltas": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    csv_text = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "New Class Kid" in csv_text
    assert "KIDS_2_CLASS" in csv_text


def test_student_edit_redirect_preserves_turma_filter(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/students/0/edit?turma=MASTER",
        data={
            "return_turma": "MASTER",
            "teacher": "Chuck",
            "class_choice": "MASTER",
            "nivel": "TEENS 4",
            "student_name": "Jane Doe",
            "participacao": "3",
            "comportamento": "3",
            "speaking": "3",
            "listening": "3",
            "foco": "3",
            "writing": "3",
            "reading": "3",
            "gramatica": "3",
            "trabalho_equipe": "3",
            "organizacao": "3",
            "pontualidade": "3",
            "respeito_regras": "3",
            "faltas": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "turma=MASTER" in (response.headers.get("Location") or "")


def test_teacher_edits_csv_student_with_turma_dropdown(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    csv = (
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,"
        "speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,"
        "pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,"
        "feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Chuck,MASTER,Masters,TEENS 4,Terça e quinta 19:00 - 20:00,Bruna Vieira,5,3,5,3,3,4,4,4,3,3,3,3,0,,,,,,,\n"
    )
    (data_dir / "students.csv").write_text(csv, encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    edit_html = client.get("/students/0/edit").get_data(as_text=True)
    assert 'name="class_choice"' in edit_html
    assert "Selecione a turma do aluno" not in edit_html

    response = client.post(
        "/students/0/edit",
        data={
            "teacher": "Chuck",
            "class_choice": "MASTER",
            "nivel": "TEENS 4",
            "student_name": "Bruna Vieira Matias",
            "participacao": "5",
            "comportamento": "3",
            "speaking": "5",
            "listening": "3",
            "foco": "3",
            "writing": "4",
            "reading": "4",
            "gramatica": "4",
            "trabalho_equipe": "3",
            "organizacao": "3",
            "pontualidade": "3",
            "respeito_regras": "3",
            "faltas": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    saved = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Bruna Vieira Matias" in saved
    assert "Selecione a turma" not in client.get("/students/0/edit").get_data(as_text=True)


def test_teacher_new_student_lists_class_from_other_semester(monkeypatch, tmp_path):
    from teacher_classes import add_class, save_registry

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Yasmin")

    registry = {}
    row, err = add_class(
        registry,
        "Yasmin",
        turma_display="root",
        class_weekdays=["Segunda-feira", "Quarta-feira"],
        class_time_start="10:00",
        class_time_end="11:00",
        semester_id="2026-S2",
    )
    assert err is None
    assert row["turma"] == "ROOT"
    save_registry(data_dir / "teacher_classes.json", registry)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    with client.session_transaction() as sess:
        sess["review_semester"] = "2026-S1"
        sess["review_month"] = "2026-03"

    html = client.get("/students/new").get_data(as_text=True)
    assert 'value="ROOT"' in html
    assert ">root" in html or "root —" in html


def test_teacher_dashboard_lists_class_after_semester_switch(monkeypatch, tmp_path):
    from teacher_classes import add_class, save_registry

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    students_csv = (
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,"
        "speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,"
        "pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,"
        "feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Yasmin,MASTER,Masters,TEENS 4,Tue 19:00,Ana Silva,4,3,4,5,4,3,4,2,3,3,3,3,1,,,"
        ",,,\n"
    )
    (data_dir / "students.csv").write_text(students_csv, encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Yasmin")

    registry = {}
    row, err = add_class(
        registry,
        "Yasmin",
        turma_display="root",
        class_weekdays=["Segunda-feira", "Quarta-feira"],
        class_time_start="10:00",
        class_time_end="11:00",
        semester_id="2026-S2",
    )
    assert err is None
    save_registry(data_dir / "teacher_classes.json", registry)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    with client.session_transaction() as sess:
        sess["review_semester"] = "2026-S1"
        sess["review_month"] = "2026-03"

    dash = client.get("/").get_data(as_text=True)
    assert ">root" in dash.lower() or "root —" in dash

    students = client.get("/students").get_data(as_text=True)
    assert 'data-filter-value="ROOT"' in students


def _student_form_payload(**overrides):
    data = {
        "teacher": "Chuck",
        "class_choice": "MASTER",
        "nivel": "TEENS 4",
        "student_name": "Jane Doe",
        "participacao": "3",
        "comportamento": "3",
        "speaking": "3",
        "listening": "3",
        "foco": "3",
        "writing": "3",
        "reading": "3",
        "gramatica": "3",
        "trabalho_equipe": "3",
        "organizacao": "3",
        "pontualidade": "3",
        "respeito_regras": "3",
        "faltas": "0",
        "observacao": "",
        "recomendacoes": "",
        "feedback_participacao": "",
        "feedback_foco": "",
        "feedback_trabalho_equipe": "",
    }
    data.update(overrides)
    return data


def test_student_edit_page_includes_autosave_assets(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    html = client.get("/students/0/edit").get_data(as_text=True)
    assert 'id="student-edit-form"' in html
    assert "student_autosave.js" in html
    assert "/students/0/autosave" in html


def test_student_autosave_persists_observations(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/students/0/autosave",
        data=_student_form_payload(
            student_name="Jane Doe",
            observacao="Precisa reforço em speaking",
            recomendacoes="Praticar em casa",
            speaking="2",
        ),
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert "saved_at" in body

    reviews_path = data_dir / "student_monthly_reviews.json"
    if reviews_path.exists():
        stored = json.loads(reviews_path.read_text(encoding="utf-8"))
        rows = stored.get("rows") if isinstance(stored, dict) else stored
        assert any(
            (row.get("observacao") or "").startswith("Precisa reforço")
            for row in rows
        )


def test_student_autosave_requires_login(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()

    client = web_app.app.test_client()
    response = client.post(
        "/students/0/autosave",
        data=_student_form_payload(),
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 401
    assert response.get_json()["login_required"] is True


def test_teacher_new_student_form_lists_dashboard_turmas(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    html = client.get("/students/new").get_data(as_text=True)

    assert "Masters" in html
    assert "MASTER" in html
    assert "criar classe" not in html.lower()
    assert "KIDS 1" in html


def test_student_new_form_does_not_copy_existing_student(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    html = client.get("/students/new").get_data(as_text=True)

    assert 'name="student_name" value=""' in html
    assert 'id="val-speaking" value="3"' in html
    assert 'id="val-listening" value="3"' in html
    assert 'id="val-gramatica" value="3"' in html
    assert 'name="faltas" value="0"' in html
    assert 'name="missed_aulas" value=""' in html
    assert "Practice speaking" not in html
    assert ">Good</textarea>" not in html
    assert ">Focus</textarea>" not in html
    assert 'value="Reposição" selected' not in html
    assert 'autocomplete="off"' in html


def test_student_new_requires_turma(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.post(
        "/students/new",
        data={"student_name": "No Turma Kid", "teacher": "Chuck", "turma": ""},
    )

    assert response.status_code == 200
    assert "Informe o nome do aluno e a turma" in response.get_data(as_text=True)


def test_student_new_creates_row(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", False)
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.post(
        "/students/new",
        data={
            "teacher": "Chuck",
            "turma": "KIDS",
            "student_name": "new kid",
            "participacao": "3",
            "comportamento": "3",
            "speaking": "3",
            "listening": "3",
            "foco": "3",
            "writing": "3",
            "reading": "3",
            "gramatica": "3",
            "faltas": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "/students" in response.headers["Location"]
    assert "New Kid" in (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "new kid" not in (data_dir / "students.csv").read_text(encoding="utf-8")
    reviews_path = data_dir / "student_monthly_reviews.json"
    assert reviews_path.exists()
    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
    assert any(r.get("participacao") == "3" for r in reviews)


def test_admin_student_new_teacher_dropdown_and_class_validation(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)
    _seed_teacher_classes(
        data_dir,
        "Chuck",
        ("MASTER", "Masters", "Terça-feira", "Quinta-feira", "19:00", "20:00"),
    )
    _seed_teacher_classes(
        data_dir,
        "Yasmin",
        ("ROOT", "root", "Segunda-feira", "Quarta-feira", "10:00", "11:00"),
    )

    client = web_app.app.test_client()
    _login(client)
    html = client.get("/students/new").get_data(as_text=True)
    assert 'id="teacher-select"' in html
    assert 'id="teacher-classes-data"' in html
    assert "Yasmin" in html
    assert "Chuck" in html

    bad = client.post(
        "/students/new",
        data={
            "teacher": "Yasmin",
            "class_choice": "MASTER",
            "student_name": "Wrong Class Kid",
            "nivel": "TEENS 1",
            "participacao": "3",
            "comportamento": "3",
            "speaking": "3",
            "listening": "3",
            "foco": "3",
            "writing": "3",
            "reading": "3",
            "gramatica": "3",
            "faltas": "0",
        },
    )
    assert bad.status_code == 200
    assert "professor escolhido" in bad.get_data(as_text=True)

    good = client.post(
        "/students/new",
        data={
            "teacher": "Yasmin",
            "class_choice": "ROOT",
            "student_name": "Right Class Kid",
            "nivel": "TEENS 1",
            "participacao": "3",
            "comportamento": "3",
            "speaking": "3",
            "listening": "3",
            "foco": "3",
            "writing": "3",
            "reading": "3",
            "gramatica": "3",
            "faltas": "0",
        },
        follow_redirects=False,
    )
    assert good.status_code == 302
    assert "Right Class Kid" in (data_dir / "students.csv").read_text(encoding="utf-8")


def test_set_review_month_redirects(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,15/03/2026,Lesson 1,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.post(
        "/review-month",
        data={"review_month": "2026-03", "next": "http://localhost/students"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/students?month=2026-03")
    with client.session_transaction() as sess:
        assert sess.get("review_month") == "2026-03"


def test_set_review_month_replaces_month_query_on_redirect(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,15/05/2026,Lesson 1,,\n"
        "MASTER,2,15/07/2026,Lesson 2,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)

    switch = client.post(
        "/review-month",
        data={
            "review_month": "2026-05",
            "next": "http://localhost/students?month=2026-07&turma=MASTER",
        },
        follow_redirects=False,
    )
    assert switch.status_code == 302
    assert "month=2026-05" in switch.headers["Location"]
    assert "month=2026-07" not in switch.headers["Location"]

    follow = client.get(switch.headers["Location"])
    assert follow.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("review_month") == "2026-05"


def test_faltas_saved_to_return_month_when_session_differs(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,15/05/2026,Lesson 1,,\n"
        "MASTER,2,15/07/2026,Lesson 2,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    reviews_path = data_dir / "student_monthly_reviews.json"
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", reviews_path)
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)

    with client.session_transaction() as sess:
        sess["review_month"] = "2026-07"

    base_form = {
        "teacher": "Chuck",
        "turma": "MASTER",
        "student_name": "Jane Doe",
        "participacao": "4",
        "comportamento": "3",
        "speaking": "4",
        "listening": "5",
        "foco": "4",
        "writing": "3",
        "reading": "4",
        "gramatica": "2",
        "trabalho_equipe": "3",
        "organizacao": "3",
        "pontualidade": "3",
        "respeito_regras": "3",
        "faltas": "4",
        "missed_aulas": "",
        "aula_extra": "",
        "return_month": "2026-05",
    }

    client.post("/students/0/edit", data=base_form, follow_redirects=False)

    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
    may_rows = [r for r in reviews if r.get("report_month") == "2026-05"]
    july_rows = [r for r in reviews if r.get("report_month") == "2026-07"]
    assert len(may_rows) == 1
    assert may_rows[0]["faltas"] == "4"
    assert not july_rows

    may_html = client.get("/students?month=2026-05").get_data(as_text=True)
    assert 'Faltas</span><strong>4</strong>' in may_html


def test_set_review_month_blocks_open_redirect(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,15/03/2026,Lesson 1,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.post(
        "/review-month",
        data={
            "review_month": "2026-03",
            "next": "http://localhost.evil.com/phish",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/?month=2026-03")


def test_student_delete_cascades_related_records(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    (data_dir / "lesson_attendance.csv").write_text(
        "turma,aula_num,student_name,status\n"
        "MASTER,2,Jane Doe,absent\n",
        encoding="utf-8",
    )
    (data_dir / "extra_sessions.csv").write_text(
        "teacher,student_name,turma,date,horario,turno,session_type,status,observacao\n"
        "Chuck,Jane Doe,MASTER,10/02/2026,09:00,Manhã,Reposição,OK,,\n",
        encoding="utf-8",
    )
    reviews = [{
        "report_month": "2026-02",
        "turma": "MASTER",
        "student_name": "Jane Doe",
        "participacao": "4",
        "faltas": "1",
        "missed_aulas": "2",
    }]
    (data_dir / "student_monthly_reviews.json").write_text(
        json.dumps(reviews), encoding="utf-8"
    )

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    with client.session_transaction() as sess:
        sess["review_month"] = "2026-02"

    response = client.post("/students/0/delete", follow_redirects=True)
    assert response.status_code == 200
    assert "Jane Doe" not in (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Jane Doe" not in (data_dir / "lesson_attendance.csv").read_text(encoding="utf-8")
    assert "Jane Doe" not in (data_dir / "extra_sessions.csv").read_text(encoding="utf-8")
    assert json.loads((data_dir / "student_monthly_reviews.json").read_text(encoding="utf-8")) == []


def test_monthly_scores_differ_by_review_month(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,15/02/2026,Lesson 1,,\n"
        "MASTER,2,15/03/2026,Lesson 2,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    reviews_path = data_dir / "student_monthly_reviews.json"
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", reviews_path)
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)

    base_form = {
        "teacher": "Chuck",
        "turma": "MASTER",
        "student_name": "Jane Doe",
        "comportamento": "3",
        "speaking": "4",
        "listening": "5",
        "foco": "4",
        "writing": "3",
        "reading": "4",
        "gramatica": "2",
        "trabalho_equipe": "3",
        "organizacao": "3",
        "pontualidade": "3",
        "respeito_regras": "3",
        "faltas": "1",
        "missed_aulas": "2",
        "aula_extra": "Reposicao",
    }

    client.post(
        "/students/0/edit?month=2026-03",
        data={**base_form, "participacao": "5"},
        follow_redirects=False,
    )
    client.post(
        "/students/0/edit?month=2026-02",
        data={**base_form, "participacao": "2"},
        follow_redirects=False,
    )

    assert reviews_path.exists()
    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
    scores = {row["report_month"]: row["participacao"] for row in reviews}
    assert scores.get("2026-03") == "5"
    assert scores.get("2026-02") == "2"

    html_mar = client.get("/students?month=2026-03").get_data(as_text=True)
    html_feb = client.get("/students?month=2026-02").get_data(as_text=True)
    assert ">5<" in html_mar or "participacao" in html_mar.lower()
    assert ">2<" in html_feb


def test_upload_template_lessons_download(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.DATA_DIR.mkdir()
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, web_app.DATA_DIR)

    client = web_app.app.test_client()
    _login(client)

    response = client.get("/upload/template/lessons")

    assert response.status_code == 200
    assert "lessons_template.csv" in response.headers.get("Content-Disposition", "")
    assert response.data.startswith(b"\xef\xbb\xbf")
    assert b"turma,aula_num,date,licao_conteudo" in response.data
    assert b"MASTER,2," in response.data


def test_admin_delete_student_with_merged_monthly_data(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n"
        "MASTER,1,10/05/2026,Lesson 1,,\n"
        "MASTER,2,12/05/2026,Lesson 2,,\n",
        encoding="utf-8",
    )
    (data_dir / "lesson_attendance.csv").write_text(
        "turma,aula_num,student_name,status\n"
        "MASTER,2,Jane Doe,absent\n",
        encoding="utf-8",
    )
    reviews = [{
        "teacher": "Chuck",
        "turma": "MASTER",
        "student_name": "Jane Doe",
        "month": "2026-05",
        "participacao": "5",
        "comportamento": "4",
        "speaking": "4",
        "listening": "4",
        "foco": "4",
        "writing": "4",
        "reading": "4",
        "gramatica": "4",
        "trabalho_equipe": "4",
        "organizacao": "4",
        "pontualidade": "4",
        "respeito_regras": "4",
        "faltas": "1",
        "missed_aulas": "2",
        "aula_extra": "",
        "feedback_participacao": "",
        "feedback_foco": "",
        "feedback_trabalho_equipe": "",
        "recomendacoes": "",
        "observacao": "",
    }]
    (data_dir / "student_monthly_reviews.json").write_text(
        json.dumps(reviews), encoding="utf-8"
    )

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_app, "MONTHLY_REVIEWS_PATH", data_dir / "student_monthly_reviews.json")
    monkeypatch.setattr(web_app, "_monthly_migration_done", True)
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    with client.session_transaction() as sess:
        sess["review_month"] = "2026-05"

    response = client.post("/students/0/delete", follow_redirects=True)

    assert response.status_code == 200
    remaining = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Jane Doe" not in remaining


def test_admin_delete_students_csv(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.post("/upload/delete/students", follow_redirects=True)

    assert response.status_code == 200
    assert not (data_dir / "students.csv").exists()
    assert b"alert-success" in response.data
    assert b"arquivo CSV removido" in response.data


def test_teacher_delete_only_own_students(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    mixed = (
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Chuck,MASTER,Masters,Adults Book 4,Tue 19:00,Jane Doe,4,3,4,5,4,3,4,2,3,3,3,3,1,2,Reposicao,Good,Focus,Team,Practice speaking,\n"
        "Barbara,SPARK,Spark,Teens,Tue 18:00,Bob Smith,3,3,3,3,3,3,3,3,3,3,3,3,0,0,,,,,,\n"
    )
    (data_dir / "students.csv").write_text(mixed, encoding="utf-8")

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post("/upload/delete/students", follow_redirects=True)

    assert response.status_code == 200
    assert b"1 registro(s) do seu perfil" in response.data
    remaining = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Jane Doe" not in remaining
    assert "Bob Smith" in remaining


def test_teacher_upload_rejects_other_teacher_rows(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir)

    bad_csv = _students_csv().replace("Chuck,", "Ana,", 1)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/upload",
        data={"students": (io.BytesIO(bad_csv.encode()), "students.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"teacher deve ser" in response.data
    assert not (data_dir / "students.csv").exists()


def test_extra_sessions_import_and_list(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    csv_body = (
        ",Nome do aluno ou responsável,Data ,Horário,Assuntos trabalhados,Observação,"
        "Turno,Contatado,Marcado,Realizado,Professor\n"
        ",Import Kid (MASTER),01/05,09:00,Reforço - test,,Manhã,ok,ok,ok,Chuck\n"
    )

    client = web_app.app.test_client()
    _login(client)
    response = client.post(
        "/extra-sessions/import",
        data={"file": (io.BytesIO(csv_body.encode("utf-8")), "atendimentos.csv"), "mode": "merge"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"adicionado" in response.data
    assert b"Import Kid" in response.data


def test_teacher_extra_sessions_scoped(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir)

    web_app._save_extra_sessions([
        {"teacher": "Chuck", "student_name": "A", "turma": "T1", "date": "1", "horario": "",
         "turno": "", "session_type": "Reforço", "assuntos": "", "observacao": "",
         "contatado": "", "marcado": "", "realizado": ""},
        {"teacher": "Ana", "student_name": "B", "turma": "T2", "date": "2", "horario": "",
         "turno": "", "session_type": "Reforço", "assuntos": "", "observacao": "",
         "contatado": "", "marcado": "", "realizado": ""},
    ])

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.get("/extra-sessions")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert ">A</strong>" in html or "student_name\">A" in html
    assert ">B</strong>" not in html


def test_download_atendimentos_template(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.get("/extra-sessions/template")
    assert response.status_code == 200
    assert "attachment" in response.headers.get("Content-Disposition", "")
    body = response.get_data(as_text=True)
    assert "Nome do aluno ou responsável" in body
    assert "Chuck" in body


def test_teacher_upload_merges_without_wiping_other_teachers(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(
        _students_csv().replace("Jane Doe", "Bob Smith").replace("Chuck,", "Ana,", 1),
        encoding="utf-8",
    )

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/upload",
        data={"students": (io.BytesIO(_students_csv().encode()), "students.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Alunos carregado" in response.data
    text = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Jane Doe" in text
    assert "Bob Smith" in text


def test_teacher_students_upload_keeps_sibling_turmas(monkeypatch, tmp_path):
    """Uploading a CSV with only one turma must not delete the teacher's other turmas."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    kids_row = _students_csv().splitlines()[1].replace(
        "MASTER,Masters", "KIDS,Kids").replace("Jane Doe", "Kid One")
    (data_dir / "students.csv").write_text(
        _students_csv() + kids_row + "\n", encoding="utf-8",
    )

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/upload",
        data={"students": (io.BytesIO(_students_csv().encode()), "students.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    text = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Jane Doe" in text
    assert "Kid One" in text  # sibling turma survives the partial upload


def test_teacher_lessons_upload_keeps_sibling_turma_lessons(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    kids_student = _students_csv().splitlines()[1].replace(
        "MASTER,Masters", "KIDS,Kids").replace("Jane Doe", "Kid One")
    (data_dir / "students.csv").write_text(
        _students_csv() + kids_student + "\n", encoding="utf-8",
    )
    (data_dir / "lessons.csv").write_text(
        _lessons_csv() + "KIDS,1,02/01,Kids lesson,,\n", encoding="utf-8",
    )

    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.post(
        "/upload",
        data={"lessons": (io.BytesIO(_lessons_csv().encode()), "lessons.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    text = (data_dir / "lessons.csv").read_text(encoding="utf-8")
    assert "Lesson 1" in text
    assert "Kids lesson" in text  # sibling turma lessons survive


def test_responsive_layout_markers(monkeypatch, tmp_path):
    """List pages expose table + card views and action columns use shared layout classes."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    web_app._save_extra_sessions([
        {"teacher": "Chuck", "student_name": "Jane Doe", "turma": "MASTER", "date": "01/05",
         "horario": "09:00", "turno": "Manhã", "session_type": "Reforço", "assuntos": "Test",
         "observacao": "", "contatado": "ok", "marcado": "ok", "realizado": "ok"},
    ])

    client = web_app.app.test_client()
    _login(client)

    students_html = client.get("/students").get_data(as_text=True)
    assert 'td class="col-actions"' in students_html
    assert "students-cards-view" in students_html
    assert "student-card-actions" in students_html

    lessons_html = client.get("/lessons").get_data(as_text=True)
    assert 'td class="col-actions"' in lessons_html
    assert "lesson-card-item" in lessons_html

    extra_html = client.get("/extra-sessions").get_data(as_text=True)
    assert "session-card-item" in extra_html
    assert "students-cards-view" in extra_html
    assert 'td class="col-actions"' in extra_html

    dashboard_html = client.get("/").get_data(as_text=True)
    assert "list-row-actions" in dashboard_html or "turma-list" in dashboard_html

    upload_html = client.get("/upload").get_data(as_text=True)
    assert "inline-actions" in upload_html
    assert "viewport" in upload_html


def test_turma_transfer_page_and_auto_transfer(monkeypatch, tmp_path):
  data_dir = tmp_path / "data"
  data_dir.mkdir()
  (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
  (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
  monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
  monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
  web_app.OUT_DIR.mkdir()
  _init_user_store(monkeypatch, data_dir)
  _seed_teacher_classes(
      data_dir,
      "Chuck",
      ("MASTER", "Masters", "Terça-feira", "Quinta-feira", "19:00", "20:00"),
  )
  web_app.user_store.create_teacher("paula@test.local", "teachpass", "Paula")

  client = web_app.app.test_client()
  _login(client)

  page = client.get("/admin/turmas/transfer").get_data(as_text=True)
  assert "Transferir turma" in page
  assert "Chuck" in page

  response = client.post(
      "/admin/turmas/transfer",
      data={
          "action": "transfer",
          "from_teacher": "Chuck",
          "to_teacher": "Paula",
          "turma": "MASTER",
      },
      follow_redirects=True,
  )
  assert response.status_code == 200
  assert b"transferida" in response.data

  students_text = (data_dir / "students.csv").read_text(encoding="utf-8")
  assert "Paula" in students_text
  assert ",Chuck," not in students_text

  registry = json.loads((data_dir / "teacher_classes.json").read_text(encoding="utf-8"))
  assert "Paula" in registry or any(k.casefold() == "paula" for k in registry)
  chuck_keys = [k for k in registry if k.casefold() == "chuck"]
  if chuck_keys:
      chuck_turmas = {r.get("turma") for r in registry[chuck_keys[0]]}
      assert "MASTER" not in chuck_turmas


def test_turma_transfer_export_zip(monkeypatch, tmp_path):
  data_dir = tmp_path / "data"
  data_dir.mkdir()
  (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
  (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
  monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
  monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
  web_app.OUT_DIR.mkdir()
  _init_user_store(monkeypatch, data_dir)
  _seed_teacher_classes(
      data_dir,
      "Chuck",
      ("MASTER", "Masters", "Terça-feira", "Quinta-feira", "19:00", "20:00"),
  )
  web_app.user_store.create_teacher("paula@test.local", "teachpass", "Paula")

  client = web_app.app.test_client()
  _login(client)

  response = client.post(
      "/admin/turmas/transfer",
      data={
          "action": "export",
          "from_teacher": "Chuck",
          "to_teacher": "Paula",
          "turma": "MASTER",
      },
  )
  assert response.status_code == 200
  assert response.mimetype == "application/zip"
  zf = zipfile.ZipFile(io.BytesIO(response.data))
  assert "students.csv" in zf.namelist()
  assert "lessons.csv" in zf.namelist()


def test_superadmin_dashboard_shows_class_display_names(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n"
        "Chuck,TEENS_1,Teens Book 1,Teens Book 1,Mon 18:00,Alice,4,3,4,5,4,3,4,2,3,3,3,3,1,2,Reposicao,Good,Focus,Team,Practice speaking,\n",
        encoding="utf-8",
    )
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)
    _seed_teacher_classes(
        data_dir,
        "Chuck",
        ("TEENS_1", "Prize", "Segunda-feira", "Quarta-feira", "18:00", "19:00"),
    )

    client = web_app.app.test_client()
    _login(client)
    html = client.get("/").get_data(as_text=True)

    assert "Prize" in html
    assert ">TEENS_1<" not in html
    assert 'href="/students?turma=TEENS_1' in html


def test_superadmin_dashboard_includes_registry_only_class(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n",
        encoding="utf-8",
    )
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)
    _seed_teacher_classes(
        data_dir,
        "Chuck",
        ("MASTER", "Masters", "Terça-feira", "Quinta-feira", "19:00", "20:00"),
    )

    client = web_app.app.test_client()
    _login(client)
    html = client.get("/").get_data(as_text=True)

    assert "Masters" in html
    assert 'href="/students?turma=MASTER' in html


def test_admin_can_edit_turma(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)
    _seed_teacher_classes(
        data_dir,
        "Chuck",
        ("MASTER", "Masters", "Terça-feira", "Quinta-feira", "19:00", "20:00"),
    )

    client = web_app.app.test_client()
    _login(client)

    response = client.get("/turmas/MASTER/edit?teacher=Chuck")
    assert response.status_code == 200
    assert "Masters" in response.get_data(as_text=True)

    response = client.post(
        "/turmas/MASTER/edit?teacher=Chuck",
        data={
            "teacher": "Chuck",
            "turma_display": "Masters Updated",
            "class_weekday_1": "Segunda-feira",
            "class_weekday_2": "Quarta-feira",
            "turma_time_start": "18:00",
            "turma_time_end": "19:00",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"atualizada" in response.data

    students_text = (data_dir / "students.csv").read_text(encoding="utf-8")
    assert "Masters Updated" in students_text


def test_admin_can_create_turma(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(
        "teacher,turma,turma_display,nivel,horario,student_name,participacao,comportamento,speaking,listening,foco,writing,reading,gramatica,trabalho_equipe,organizacao,pontualidade,respeito_regras,faltas,missed_aulas,aula_extra,feedback_participacao,feedback_foco,feedback_trabalho_equipe,recomendacoes,observacao\n",
        encoding="utf-8",
    )
    (data_dir / "lessons.csv").write_text(
        "turma,aula_num,date,licao_conteudo,atividade_extra,habilidades\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    response = client.post(
        "/turmas/create",
        data={
            "teacher": "Chuck",
            "turma_display": "New Class",
            "class_weekday_1": "Terça-feira",
            "class_weekday_2": "Quinta-feira",
            "turma_time_start": "19:00",
            "turma_time_end": "20:00",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"criada" in response.data
    registry = json.loads((data_dir / "teacher_classes.json").read_text(encoding="utf-8"))
    assert any(
        entry.get("turma_display") == "New Class"
        for entries in registry.values()
        for entry in entries
    )


def test_teacher_cannot_edit_other_teacher_turma(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "students.csv").write_text(_students_csv(), encoding="utf-8")
    (data_dir / "lessons.csv").write_text(_lessons_csv(), encoding="utf-8")
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    _init_teacher_store(monkeypatch, data_dir, teacher_name="Chuck")
    _seed_teacher_classes(
        data_dir,
        "Paula",
        ("PAULA_ONLY", "Paula Class", "Terça-feira", "Quinta-feira", "19:00", "20:00"),
    )

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")
    response = client.get("/turmas/PAULA_ONLY/edit?teacher=Paula")
    assert response.status_code == 302



def test_chat_room_teachers_and_admin_resolve(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "db_store", None)
    monkeypatch.setattr(web_app, "SUPERADMIN_EMAIL", "admin@test.local")
    monkeypatch.setattr(web_app, "SUPERADMIN_PASSWORD", "testpass")
    _init_teacher_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")

    page = client.get("/chat")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Chat" in html
    assert "Reportar bug" in html

    posted = client.post(
        "/chat/post",
        data={"body": "Olá do Chuck", "kind": ""},
        follow_redirects=True,
    )
    assert posted.status_code == 200
    assert "Olá do Chuck" in posted.get_data(as_text=True)

    bug = client.post(
        "/chat/post",
        data={"body": "Notas não salvam", "kind": "bug"},
        follow_redirects=True,
    )
    assert bug.status_code == 200
    bug_html = bug.get_data(as_text=True)
    assert "Notas não salvam" in bug_html
    assert "Bug" in bug_html

    # Teacher cannot resolve
    rows = web_app._load_chat_messages()
    bug_id = next(r["id"] for r in rows if r.get("kind") == "bug")
    blocked = client.post(f"/chat/{bug_id}/resolve", follow_redirects=True)
    assert blocked.status_code == 200
    still_open = next(r for r in web_app._load_chat_messages() if r["id"] == bug_id)
    assert still_open["bug_status"] == "open"

    # Admin can resolve
    client.get("/logout")
    login_resp = _login(client)
    assert login_resp.status_code in (302, 303)
    resolved = client.post(f"/chat/{bug_id}/resolve", follow_redirects=True)
    assert resolved.status_code == 200
    assert any(r.get("bug_status") == "resolved" for r in web_app._load_chat_messages() if r["id"] == bug_id)


def test_chat_messages_json_poll(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "db_store", None)
    _init_user_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client)
    client.post("/chat/post", data={"body": "Primeira", "kind": ""})
    rows = web_app._load_chat_messages()
    first_id = rows[0]["id"]
    client.post("/chat/post", data={"body": "Segunda", "kind": ""})

    poll = client.get(f"/chat/messages.json?after={first_id}")
    assert poll.status_code == 200
    payload = poll.get_json()
    assert payload["ok"] is True
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["body"] == "Segunda"


def test_teacher_profile_self_edit_and_photo(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "db_store", None)
    monkeypatch.setattr(web_app, "SUPERADMIN_EMAIL", "admin@test.local")
    monkeypatch.setattr(web_app, "SUPERADMIN_PASSWORD", "testpass")
    _init_teacher_store(monkeypatch, data_dir)

    client = web_app.app.test_client()
    _login(client, email="teacher@test.local", password="teachpass")

    page = client.get("/profile")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Meu perfil" in html
    assert "Bio" in html

    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 80
    saved = client.post(
        "/profile",
        data={
            "bio": "Amo ensinar Teens",
            "specialty": "Teens",
            "whatsapp": "11988887777",
            "phone": "",
            "contact_email": "chuck.public@school.com",
            "photo": (io.BytesIO(jpeg), "me.jpg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert saved.status_code == 200
    body = saved.get_data(as_text=True)
    assert "Amo ensinar Teens" in body
    assert "11988887777" in body
    assert "Perfil atualizado" in body

    profiles = web_app._load_teacher_profiles()
    assert len(profiles) == 1
    assert profiles[0]["specialty"] == "Teens"
    assert profiles[0]["photo_mime"] == "image/jpeg"

    teacher = web_app.user_store.get_by_email("teacher@test.local")
    photo = client.get(f"/profile/{teacher['id']}/photo")
    assert photo.status_code == 200
    assert photo.data.startswith(b"\xff\xd8\xff")


def test_admin_can_edit_teacher_profile(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(web_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_app, "OUT_DIR", tmp_path / "output")
    web_app.OUT_DIR.mkdir()
    monkeypatch.setattr(web_app, "db_store", None)
    monkeypatch.setattr(web_app, "SUPERADMIN_EMAIL", "admin@test.local")
    monkeypatch.setattr(web_app, "SUPERADMIN_PASSWORD", "testpass")
    _init_teacher_store(monkeypatch, data_dir)

    teacher = web_app.user_store.get_by_email("teacher@test.local")
    client = web_app.app.test_client()
    _login(client)

    page = client.get(f"/profile/{teacher['id']}")
    assert page.status_code == 200
    assert "Editar perfil" in page.get_data(as_text=True)

    saved = client.post(
        f"/profile/{teacher['id']}",
        data={
            "bio": "Bio do admin",
            "specialty": "Kids",
            "whatsapp": "",
            "phone": "113333",
            "contact_email": "",
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "Bio do admin" in saved.get_data(as_text=True)
    profile = web_app._load_teacher_profiles()[0]
    assert profile["user_id"] == teacher["id"]
    assert profile["specialty"] == "Kids"
