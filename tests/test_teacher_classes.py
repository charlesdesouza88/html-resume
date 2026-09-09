from form_ui import turma_code_from_display
from teacher_classes import (
    add_class,
    apply_registry_to_students,
    count_students_in_turma,
    dedupe_class_options,
    ensure_semester_ids,
    find_class,
    list_for_teacher,
    load_registry,
    registry_from_rows,
    registry_to_rows,
    remove_class,
    save_registry,
    sync_teacher_classes_from_students,
    update_class,
)


def test_turma_code_from_display():
    assert turma_code_from_display('Turma terça') == 'TURMA_TERCA'
    assert turma_code_from_display('Kids 2 class') == 'KIDS_2_CLASS'
    assert turma_code_from_display('root') == 'ROOT'


def test_add_class_two_weekdays(tmp_path):
    path = tmp_path / 'teacher_classes.json'
    data = {}
    row, err = add_class(
        data,
        'Chuck',
        turma_display='Teens night',
        class_weekdays=['Terça-feira', 'Quinta-feira'],
        class_time_start='19:00',
        class_time_end='20:00',
        semester_id='2026-S1',
    )
    assert err is None
    assert row['turma'] == 'TEENS_NIGHT'
    assert row['semester_id'] == '2026-S1'
    assert row['class_weekdays'] == ['Terça-feira', 'Quinta-feira']
    assert row['horario'] == 'Terça-feira e Quinta-feira 19:00 - 20:00'
    save_registry(path, data)

    rows = list_for_teacher(load_registry(path), 'Chuck', semester_id='2026-S1')
    assert len(rows) == 1
    assert rows[0]['class_weekdays'] == ['Terça-feira', 'Quinta-feira']


def test_same_turma_allowed_in_different_semesters():
    data = {}
    row1, err1 = add_class(
        data,
        'Chuck',
        turma_display='Masters',
        class_weekdays=['Terça-feira', 'Sexta-feira'],
        class_time_start='13:00',
        class_time_end='14:00',
        turma='MASTER',
        semester_id='2026-S1',
    )
    row2, err2 = add_class(
        data,
        'Chuck',
        turma_display='Masters',
        class_weekdays=['Terça-feira', 'Sexta-feira'],
        class_time_start='13:00',
        class_time_end='14:00',
        turma='MASTER',
        semester_id='2026-S2',
    )
    assert err1 is None and err2 is None
    assert row1['semester_id'] == '2026-S1'
    assert row2['semester_id'] == '2026-S2'
    assert len(list_for_teacher(data, 'Chuck', semester_id='2026-S1')) == 1
    assert len(list_for_teacher(data, 'Chuck', semester_id='2026-S2')) == 1
    assert find_class(data, 'Chuck', 'MASTER', semester_id='2026-S2')['semester_id'] == '2026-S2'


def test_dedupe_class_options_prefers_review_semester():
    options = [
        {'turma': 'ROOT', 'turma_display': 'root', 'semester_id': '2026-S1'},
        {'turma': 'ROOT', 'turma_display': 'root evening', 'semester_id': '2026-S2'},
    ]
    deduped = dedupe_class_options(options, prefer_semester='2026-S2')
    assert len(deduped) == 1
    assert deduped[0]['turma_display'] == 'root evening'


def test_duplicate_turma_rejected_same_semester():
    data = {}
    add_class(
        data,
        'Chuck',
        turma_display='Turma A',
        class_weekdays=['Segunda-feira', 'Quarta-feira'],
        class_time_start='09:00',
        class_time_end='10:00',
        turma='TURMA_A',
        semester_id='2026-S1',
    )
    _, err = add_class(
        data,
        'Chuck',
        turma_display='Turma A2',
        class_weekdays=['Terça-feira', 'Quinta-feira'],
        class_time_start='10:00',
        class_time_end='11:00',
        turma='TURMA_A',
        semester_id='2026-S1',
    )
    assert err is not None
    assert 'já está cadastrada' in err
    assert 'Turma A2' in err
    assert 'Turma A' in err


def test_renamed_class_blocks_original_name_with_clear_error():
    data = {}
    row, err = add_class(
        data,
        'Amanda',
        turma_display='Spark',
        class_weekdays=['Segunda-feira', 'Quarta-feira'],
        class_time_start='08:00',
        class_time_end='09:30',
        semester_id='2026-S2',
    )
    assert err is None
    updated, err = update_class(
        data,
        'Amanda',
        row['turma'],
        turma_display='Scout',
        class_weekdays=['Segunda-feira', 'Quarta-feira'],
        class_time_start='08:00',
        class_time_end='09:30',
        semester_id='2026-S2',
    )
    assert err is None
    assert updated['turma'] == 'SPARK'
    _, err = add_class(
        data,
        'Amanda',
        turma_display='Spark',
        class_weekdays=['Segunda-feira', 'Quarta-feira'],
        class_time_start='08:00',
        class_time_end='09:30',
        semester_id='2026-S2',
    )
    assert err is not None
    assert 'Scout' in err
    assert 'Spark' in err


def test_ensure_semester_ids_infers_from_lessons():
    data = {
        'Chuck': [
            {
                'turma': 'MASTER',
                'turma_display': 'Masters',
                'class_weekdays': ['Terça-feira', 'Sexta-feira'],
                'class_time_start': '13:00',
                'class_time_end': '14:00',
                'horario': 'x',
            }
        ]
    }
    lessons = [
        {'turma': 'MASTER', 'date': '10/03/2026'},
        {'turma': 'MASTER', 'date': '12/03/2026'},
    ]
    assert ensure_semester_ids(data, lessons=lessons, default_semester='2026-S2') == 1
    assert data['Chuck'][0]['semester_id'] == '2026-S1'


def test_apply_registry_to_students_rejoins_schedule():
    data = {}
    add_class(
        data,
        'Chuck',
        turma_display='Masters',
        class_weekdays=['Terça-feira', 'Sexta-feira'],
        class_time_start='13:00',
        class_time_end='14:00',
        turma='MASTER',
        semester_id='2026-S1',
    )
    students = [
        {
            'teacher': 'Chuck',
            'turma': 'MASTER',
            'turma_display': 'Old',
            'horario': 'wrong',
            'student_name': 'Jane',
        }
    ]
    changed = apply_registry_to_students(students, data, semester_id='2026-S1')
    assert changed == 1
    assert students[0]['turma_display'] == 'Masters'
    assert 'Terça-feira' in students[0]['horario']


def test_registry_roundtrip_rows():
    data = {}
    add_class(
        data,
        'Chuck',
        turma_display='Prize',
        class_weekdays=['Quarta-feira', 'Sexta-feira'],
        class_time_start='22:00',
        class_time_end='23:00',
        turma='TEENS_1',
        semester_id='2026-S1',
    )
    rows = registry_to_rows(data)
    assert rows[0]['teacher'] == 'Chuck'
    rebuilt = registry_from_rows(rows)
    assert list_for_teacher(rebuilt, 'Chuck')[0]['turma'] == 'TEENS_1'


def test_add_class_rejects_same_weekday():
    data = {}
    _, err = add_class(
        data,
        'Chuck',
        turma_display='Dup',
        class_weekdays=['Segunda-feira', 'Segunda-feira'],
        class_time_start='10:00',
        class_time_end='11:00',
    )
    assert err is not None


def test_add_class_rejects_missing_time():
    data = {}
    _, err = add_class(
        data,
        'Chuck',
        turma_display='No time',
        class_weekdays=['Segunda-feira', 'Quarta-feira'],
        class_time_start='',
        class_time_end='',
    )
    assert err is not None


def test_sync_imports_turmas_from_existing_students():
    data = {}
    students = [
        {
            'teacher': 'Chuck',
            'turma': 'LIVE_FLOW',
            'turma_display': 'Book 1',
            'nivel': 'Book 1',
            'horario': 'Mon 10:00',
        },
        {
            'teacher': 'Chuck',
            'turma': 'MASTER',
            'turma_display': 'Masters',
            'nivel': 'Adults Book 4',
            'horario': 'Tue 19:00',
        },
        {'teacher': 'Ana', 'turma': 'OTHER', 'turma_display': 'X', 'nivel': '', 'horario': ''},
    ]
    added = sync_teacher_classes_from_students(data, 'Chuck', students)
    assert added == 2
    rows = list_for_teacher(data, 'Chuck')
    codes = {r['turma'] for r in rows}
    assert codes == {'LIVE_FLOW', 'MASTER'}
    master = next(r for r in rows if r['turma'] == 'MASTER')
    assert master['turma_display'] == 'Masters'
    live = next(r for r in rows if r['turma'] == 'LIVE_FLOW')
    assert live['turma_display'] == 'LIVE FLOW'
    assert live['horario'] == 'Mon 10:00'
    assert sync_teacher_classes_from_students(data, 'Chuck', students) == 0


def test_update_class_changes_schedule():
    data = {}
    add_class(
        data,
        'Chuck',
        turma_display='Old name',
        class_weekdays=['Segunda-feira', 'Quarta-feira'],
        class_time_start='09:00',
        class_time_end='10:00',
        turma='OLD',
    )
    row, err = update_class(
        data,
        'Chuck',
        'OLD',
        turma_display='New name',
        class_weekdays=['Terça-feira', 'Quinta-feira'],
        class_time_start='19:00',
        class_time_end='20:00',
    )
    assert err is None
    assert row['turma'] == 'OLD'
    assert row['turma_display'] == 'New name'
    assert 'Terça-feira e Quinta-feira 19:00 - 20:00' == row['horario']
    listed = list_for_teacher(data, 'Chuck')[0]
    assert listed['turma_display'] == 'New name'


def test_remove_class_without_students():
    data = {}
    add_class(
        data,
        'Chuck',
        turma_display='Empty class',
        class_weekdays=['Segunda-feira', 'Quarta-feira'],
        class_time_start='09:00',
        class_time_end='10:00',
    )
    ok, err = remove_class(data, 'Chuck', 'EMPTY_CLASS', students=[])
    assert err is None
    assert ok is True
    assert list_for_teacher(data, 'Chuck') == []


def test_remove_class_blocked_when_students_linked():
    data = {}
    add_class(
        data,
        'Chuck',
        turma_display='Busy class',
        class_weekdays=['Terça-feira', 'Quinta-feira'],
        class_time_start='19:00',
        class_time_end='20:00',
        turma='BUSY',
    )
    students = [{'teacher': 'Chuck', 'turma': 'BUSY', 'student_name': 'Kid'}]
    assert count_students_in_turma(students, 'Chuck', 'BUSY') == 1
    ok, err = remove_class(data, 'Chuck', 'BUSY', students=students)
    assert ok is False
    assert 'aluno' in err
    assert len(list_for_teacher(data, 'Chuck')) == 1
