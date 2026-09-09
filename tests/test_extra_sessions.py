from pathlib import Path

from extra_sessions import (
    AUTO_AULA_EXTRA_MARKER,
    ATENDIMENTOS_CSV_HEADERS,
    build_atendimentos_template_csv,
    clear_aula_extra_after_completed_session,
    display_status,
    internal_row_to_csv_row,
    is_auto_aula_extra_row,
    is_status_ok,
    normalize_aula_extra,
    normalize_status,
    parse_import_csv,
    parse_session_type,
    parse_turma_from_student_name,
    reconcile_flagged_students,
    row_from_form,
    sync_student_extra_sessions,
)


SAMPLE_CSV = """\
,Nome do aluno ou responsável,Data ,Horário,Assuntos trabalhados,Observação,Turno,Contatado,Marcado,Realizado,Professor
,Jane Test (Comet - A),13/05,09:30,Reforço - vocabulary,FALTOU,Manhã,ok,ok,NÃO,Chuck
,Bob Test (Star - B),04/05,10:00,Reforço - Reposição ate a lição 12,Feito lição 4,Manhã,ok,ok,ok,Chuck
"""


def test_parse_session_type():
    assert parse_session_type('Reforço - ') == 'Reforço'
    assert parse_session_type('Reforço - Reposição ate a lição 12') == 'Reposição'
    assert parse_session_type('Nivelamento oral') == 'Nivelamento'


def test_parse_turma_from_student_name():
    assert parse_turma_from_student_name('Jane (Comet - A)') == 'Comet - A'
    assert parse_turma_from_student_name('Jane (Comet - A) (2)') == 'Comet - A'
    assert parse_turma_from_student_name('Vitoria - Power') == 'Power'


def test_parse_import_csv_sample():
    rows, errors = parse_import_csv(SAMPLE_CSV)
    assert not errors
    assert len(rows) == 2
    assert rows[0]['student_name'] == 'Jane Test (Comet - A)'
    assert rows[0]['turma'] == 'Comet - A'
    assert rows[0]['realizado'] == 'NÃO'
    assert rows[0]['contatado'] == 'OK'
    assert rows[1]['marcado'] == 'OK'
    assert rows[0]['teacher'] == 'Chuck'
    assert rows[1]['session_type'] == 'Reposição'


def test_parse_import_csv_skips_blank_header_row():
    text = ',,,,,,,,,,\n' + SAMPLE_CSV.lstrip('\n')
    rows, errors = parse_import_csv(text)
    assert not errors
    assert len(rows) == 2
    assert rows[0]['student_name'] == 'Jane Test (Comet - A)'
    assert rows[0]['turma'] == 'Comet - A'
    assert rows[0]['realizado'] == 'NÃO'
    assert rows[0]['contatado'] == 'OK'
    assert rows[1]['marcado'] == 'OK'
    assert rows[0]['teacher'] == 'Chuck'
    assert rows[1]['session_type'] == 'Reposição'


def test_row_from_form():
    row = row_from_form({
        'student_name': 'Ana (MASTER)',
        'teacher': 'Chuck',
        'date': '01/06',
        'horario': '14:00',
        'turno': 'Tarde',
        'session_type': 'Reforço',
        'assuntos': 'Reforço - speaking',
        'observacao': 'ok',
        'contatado': 'ok',
        'marcado': 'ok',
        'realizado': 'OK',
        'turma': '',
    })
    assert row['turma'] == 'MASTER'
    assert row['contatado'] == 'OK'


def test_normalize_status_ok_capitalized():
    assert normalize_status('ok') == 'OK'
    assert normalize_status('OK') == 'OK'
    assert is_status_ok('ok')
    assert display_status('ok') == 'OK'


def test_build_atendimentos_template_csv(tmp_path):
    template_dir = Path(__file__).resolve().parents[1] / 'data' / 'templates'
    text = build_atendimentos_template_csv(template_dir)
    assert text.startswith('\ufeff')
    assert 'Nome do aluno ou responsável' in text
    assert 'Contatado' in text
    assert 'OK' in text
    rows, errors = parse_import_csv(text.lstrip('\ufeff'))
    assert not errors
    assert len(rows) >= 2


def test_build_atendimentos_template_csv_teacher_name(tmp_path):
    text = build_atendimentos_template_csv(tmp_path, teacher_name='Maria')
    assert 'Maria' in text
    rows, _ = parse_import_csv(text.lstrip('\ufeff'))
    assert all(r['teacher'] == 'Maria' for r in rows)


def test_internal_row_to_csv_row():
    row = internal_row_to_csv_row({
        'student_name': 'Ana',
        'turma': 'MASTER',
        'date': '01/06/2026',
        'horario': '10:00',
        'session_type': 'Reforço',
        'assuntos': 'speaking',
        'observacao': 'note',
        'turno': 'Manhã',
        'contatado': 'ok',
        'marcado': '',
        'realizado': 'NÃO',
        'teacher': 'Chuck',
    })
    assert row['Nome do aluno ou responsável'] == 'Ana (MASTER)'
    assert row['Contatado'] == 'OK'
    assert row['Realizado'] == 'NÃO'
    assert set(row.keys()) == set(ATENDIMENTOS_CSV_HEADERS)


def test_normalize_aula_extra():
    assert normalize_aula_extra('Reforço') == 'Reforço'
    assert normalize_aula_extra('reposicao') == 'Reposição'
    assert normalize_aula_extra('') == ''


def test_sync_student_extra_sessions_creates_row():
    student = {
        'teacher': 'Amanda',
        'student_name': 'Maria Fernanda Carvalho Pires',
        'turma': 'COMET',
        'aula_extra': 'Reforço',
    }
    rows = sync_student_extra_sessions([], student)
    assert len(rows) == 1
    assert rows[0]['session_type'] == 'Reforço'
    assert rows[0]['teacher'] == 'Amanda'
    assert is_auto_aula_extra_row(rows[0])


def test_sync_student_extra_sessions_clears_auto_row():
    student = {
        'teacher': 'Amanda',
        'student_name': 'Maria Fernanda Carvalho Pires',
        'turma': 'COMET',
        'aula_extra': '',
    }
    existing = sync_student_extra_sessions([], {
        **student,
        'aula_extra': 'Reposição',
    })
    rows = sync_student_extra_sessions(existing, student)
    assert rows == []


def test_reconcile_flagged_students():
    students = [{
        'teacher': 'Chuck',
        'student_name': 'Jane Doe',
        'turma': 'MASTER',
        'aula_extra': 'Reposição',
    }]
    rows = reconcile_flagged_students([], students)
    assert len(rows) == 1
    assert rows[0]['session_type'] == 'Reposição'


def test_clear_aula_extra_after_completed_session():
    students = [{
        'teacher': 'Chuck',
        'student_name': 'Jane Doe',
        'turma': 'MASTER',
        'aula_extra': 'Reforço',
    }]
    session = {
        'teacher': 'Chuck',
        'student_name': 'Jane Doe (MASTER)',
        'turma': 'MASTER',
        'session_type': 'Reforço',
        'realizado': 'OK',
    }
    updated = clear_aula_extra_after_completed_session(students, session)
    assert updated[0]['aula_extra'] == ''


def test_row_matches_turma_display_and_code():
    from extra_sessions import apply_open_extra_sessions_to_students

    students = [{
        'teacher': 'Amanda',
        'student_name': 'Lais Machado',
        'turma': 'COMET',
        'turma_display': 'Comet',
        'aula_extra': '',
    }]
    sessions = [{
        'teacher': 'Amanda',
        'student_name': 'Lais Machado (Comet - A)',
        'turma': 'Comet - A',
        'session_type': 'Reforço',
        'realizado': '',
    }]
    overlay = apply_open_extra_sessions_to_students(students, sessions)
    assert overlay[0]['aula_extra'] == 'Reforço'


def test_overlay_matches_power_session_without_teacher():
    """Imported atendimentos often omit Professor and store 'Power - C'."""
    from extra_sessions import apply_open_extra_sessions_to_students

    students = [{
        'teacher': 'Chuck',
        'student_name': 'Vitoria Oliveira',
        'turma': 'POWER',
        'turma_display': 'Power',
        'aula_extra': '',
    }]
    sessions = [{
        'teacher': '',
        'student_name': 'Vitoria Oliveira (Power - C)',
        'turma': 'Power - C',
        'session_type': 'Reforço',
        'realizado': '',
    }]
    overlay = apply_open_extra_sessions_to_students(students, sessions)
    assert overlay[0]['aula_extra'] == 'Reforço'


def test_overlay_does_not_use_another_teachers_session():
    from extra_sessions import apply_open_extra_sessions_to_students

    students = [{
        'teacher': 'Chuck',
        'student_name': 'Vitoria Oliveira',
        'turma': 'POWER',
        'turma_display': 'Power',
        'aula_extra': '',
    }]
    sessions = [{
        'teacher': 'Bárbara',
        'student_name': 'Vitoria Oliveira (Power - C)',
        'turma': 'Power - C',
        'session_type': 'Reforço',
        'realizado': '',
    }]
    overlay = apply_open_extra_sessions_to_students(students, sessions)
    assert overlay[0]['aula_extra'] == ''


def test_overlay_matches_name_dash_turma_label():
    from extra_sessions import apply_open_extra_sessions_to_students

    students = [{
        'teacher': 'Chuck',
        'student_name': 'Vitoria',
        'turma': 'POWER',
        'turma_display': 'Power',
        'aula_extra': '',
    }]
    sessions = [{
        'teacher': 'Chuck',
        'student_name': 'Vitoria - Power',
        'turma': '',
        'session_type': 'Reposição',
        'realizado': '',
    }]
    overlay = apply_open_extra_sessions_to_students(students, sessions)
    assert overlay[0]['aula_extra'] == 'Reposição'


def test_filter_sessions_for_teacher_includes_unassigned_roster_match():
    from extra_sessions import filter_sessions_for_teacher

    students = [{
        'teacher': 'Chuck',
        'student_name': 'Vitoria Oliveira',
        'turma': 'POWER',
        'turma_display': 'Power',
    }]
    sessions = [
        {
            'teacher': '',
            'student_name': 'Vitoria Oliveira (Power - C)',
            'turma': 'Power - C',
            'session_type': 'Reforço',
        },
        {
            'teacher': 'Amanda',
            'student_name': 'Vitoria Oliveira (Power - C)',
            'turma': 'Power - C',
            'session_type': 'Reforço',
        },
    ]
    visible = filter_sessions_for_teacher(sessions, 'Chuck', students)
    assert len(visible) == 1
    assert visible[0]['teacher'] == ''


def test_apply_open_extra_sessions_ignores_completed():
    from extra_sessions import apply_open_extra_sessions_to_students

    students = [{
        'teacher': 'Chuck',
        'student_name': 'Jane Doe',
        'turma': 'MASTER',
        'turma_display': 'Masters',
        'aula_extra': '',
    }]
    sessions = [{
        'teacher': 'Chuck',
        'student_name': 'Jane Doe',
        'turma': 'MASTER',
        'session_type': 'Reforço',
        'realizado': 'OK',
    }]
    overlay = apply_open_extra_sessions_to_students(students, sessions)
    assert overlay[0]['aula_extra'] == ''


def test_apply_open_extra_sessions_normalizes_legacy_flag():
    from extra_sessions import apply_open_extra_sessions_to_students

    students = [{
        'teacher': 'Chuck',
        'student_name': 'Jane Doe',
        'turma': 'MASTER',
        'aula_extra': 'Reposicao',
    }]
    overlay = apply_open_extra_sessions_to_students(students, [])
    assert overlay[0]['aula_extra'] == 'Reposição'


def test_apply_pending_session_flag_sets_empty_student():
    from extra_sessions import apply_pending_session_flag_to_students

    students = [{
        'teacher': 'Chuck',
        'student_name': 'Jane Doe',
        'turma': 'MASTER',
        'turma_display': 'Masters',
        'aula_extra': '',
    }]
    session = {
        'teacher': 'Chuck',
        'student_name': 'Jane Doe (Masters)',
        'turma': 'Masters',
        'session_type': 'Reposição',
        'realizado': '',
    }
    updated = apply_pending_session_flag_to_students(students, session)
    assert updated[0]['aula_extra'] == 'Reposição'


def test_sync_reflags_after_completed_session():
    student = {
        'teacher': 'Chuck',
        'student_name': 'Jane Doe',
        'turma': 'MASTER',
        'aula_extra': 'Reforço',
    }
    completed = {
        'teacher': 'Chuck',
        'student_name': 'Jane Doe (MASTER)',
        'turma': 'MASTER',
        'session_type': 'Reforço',
        'realizado': 'OK',
        'observacao': '',
    }
    rows = sync_student_extra_sessions([completed], student)
    pending = [row for row in rows if not is_status_ok(row.get('realizado'))]
    assert len(pending) == 1
    assert pending[0]['session_type'] == 'Reforço'
