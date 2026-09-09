"""Per-teacher class (turma) registry — created on the dashboard before adding students."""

import json
import logging
import os
import tempfile
from collections import Counter
from pathlib import Path

from auth import normalize_teacher_name
from form_ui import (
    format_class_schedule,
    is_valid_nivel,
    normalize_weekdays,
    parse_time_range_from_horario,
    turma_code_from_display,
)
from report_periods import (
    current_semester,
    parse_semester_id,
    semester_for_date,
)

logger = logging.getLogger(__name__)


def _empty_registry():
    return {}


def load_registry(path):
    if not path or not Path(path).exists():
        return _empty_registry()
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else _empty_registry()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning('Could not read teacher classes %s: %s', path, exc)
        return _empty_registry()


def save_registry(path, data):
    """Atomic write so concurrent readers never see a partial JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix='.teacher_classes_', suffix='.tmp',
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def registry_to_rows(data):
    """Flatten nested registry dict → list of rows (for DB storage)."""
    rows = []
    if not isinstance(data, dict):
        return rows
    for teacher_name, classes in data.items():
        if not isinstance(classes, list):
            continue
        teacher = normalize_teacher_name(teacher_name) or str(teacher_name).strip()
        for row in classes:
            if not isinstance(row, dict):
                continue
            flat = dict(row)
            flat['teacher'] = teacher
            rows.append(flat)
    return rows


def registry_from_rows(rows):
    """Rebuild nested registry dict from flat DB rows."""
    data = _empty_registry()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        teacher = normalize_teacher_name(row.get('teacher', ''))
        code = (row.get('turma') or '').strip()
        if not teacher or not code:
            continue
        entry = {k: v for k, v in row.items() if k != 'teacher'}
        entry['turma'] = code
        data.setdefault(teacher, []).append(entry)
    return data


def _weekdays_from_row(row):
    """Support class_weekdays list and legacy single class_weekday / class_date."""
    stored = row.get('class_weekdays')
    if isinstance(stored, list) and stored:
        return normalize_weekdays(stored)
    legacy = (row.get('class_weekday') or row.get('class_date') or '').strip()
    return normalize_weekdays([legacy] if legacy else [])


def _schedule_fields(row):
    weekdays = _weekdays_from_row(row)
    time_start = (row.get('class_time_start') or row.get('class_time') or '').strip()
    time_end = (row.get('class_time_end') or '').strip()
    horario = (row.get('horario') or '').strip()
    if not time_end and horario:
        parsed_start, parsed_end = parse_time_range_from_horario(horario)
        if parsed_start:
            time_start = time_start or parsed_start
        time_end = parsed_end
    if not horario and (weekdays or time_start or time_end):
        horario = format_class_schedule(weekdays, time_start, time_end)
    return weekdays, time_start, time_end, horario


def _normalize_semester_id(semester_id):
    raw = (semester_id or '').strip()
    return raw if parse_semester_id(raw) else ''


def list_for_teacher(data, teacher_name, semester_id=None):
    key = normalize_teacher_name(teacher_name)
    if not key:
        return []
    _bucket_key, bucket = _teacher_bucket(data, teacher_name)
    rows = bucket if isinstance(bucket, list) else []
    if not isinstance(rows, list):
        return []
    want_semester = _normalize_semester_id(semester_id)
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        turma = (row.get('turma') or '').strip()
        if not turma:
            continue
        row_semester = _normalize_semester_id(row.get('semester_id'))
        if want_semester and row_semester and row_semester != want_semester:
            continue
        if want_semester and not row_semester:
            # Untagged legacy rows appear in every semester until migrated.
            pass
        weekdays, time_start, time_end, horario = _schedule_fields(row)
        out.append({
            'turma': turma,
            'turma_display': (row.get('turma_display') or turma).strip(),
            'class_weekdays': weekdays,
            'class_time_start': time_start,
            'class_time_end': time_end,
            'horario': horario,
            'semester_id': row_semester,
            'needs_schedule': len(weekdays) < 2 or not time_start or not time_end,
            'legacy_import': bool(row.get('legacy_import')),
        })
    return sorted(
        out,
        key=lambda r: (r['turma_display'].casefold(), r['turma'], r.get('semester_id') or ''),
    )


def dedupe_class_options(options, prefer_semester=''):
    """Keep one row per turma code; prefer the entry for prefer_semester when duplicated."""
    prefer = _normalize_semester_id(prefer_semester)
    by_code = {}
    order = []
    for row in options or []:
        code = (row.get('turma') or '').strip()
        if not code:
            continue
        if code not in by_code:
            by_code[code] = row
            order.append(code)
            continue
        current = by_code[code]
        row_sem = _normalize_semester_id(row.get('semester_id'))
        cur_sem = _normalize_semester_id(current.get('semester_id'))
        if prefer and row_sem == prefer:
            by_code[code] = row
        elif prefer and cur_sem == prefer:
            continue
        elif row_sem and not cur_sem:
            by_code[code] = row
    return [by_code[code] for code in order]


def turma_codes_for_teacher(data, teacher_name, semester_id=None):
    return {r['turma'] for r in list_for_teacher(data, teacher_name, semester_id=semester_id)}


def find_class(data, teacher_name, turma, semester_id=None):
    code = (turma or '').strip()
    want_semester = _normalize_semester_id(semester_id)
    matches = [
        row for row in list_for_teacher(data, teacher_name)
        if row['turma'] == code
    ]
    if not matches:
        return None
    if want_semester:
        for row in matches:
            if row.get('semester_id') == want_semester:
                return row
        # Fall back to untagged legacy row for this code.
        for row in matches:
            if not row.get('semester_id'):
                return row
        return None
    return matches[0]


def count_students_in_turma(students, teacher_name, turma):
    """Students assigned to this teacher's turma code."""
    key = normalize_teacher_name(teacher_name).casefold()
    code = (turma or '').strip()
    if not key or not code:
        return 0
    total = 0
    for row in students:
        if normalize_teacher_name(row.get('teacher', '')).casefold() != key:
            continue
        if (row.get('turma') or '').strip() == code:
            total += 1
    return total


def _duplicate_class_message(display, existing_row):
    """Explain a code collision, including when the class was renamed."""
    existing_display = (
        existing_row.get('turma_display') or existing_row.get('turma') or ''
    ).strip()
    if existing_display and existing_display.casefold() != display.casefold():
        return (
            f'A turma "{display}" já está cadastrada neste semestre como '
            f'"{existing_display}". Edite essa turma se quiser corrigir o nome.'
        )
    return f'A turma "{display}" já está cadastrada neste semestre.'


def _teacher_bucket(data, teacher_name):
    key = normalize_teacher_name(teacher_name)
    if not key:
        return None, None
    bucket = data.get(key)
    if bucket is not None:
        return key, bucket
    for k, bucket in data.items():
        if k.casefold() == key.casefold():
            return k, bucket
    return key, None


def remove_class(data, teacher_name, turma, students=None, semester_id=None):
    """
    Remove a turma from the teacher registry.
    When semester_id is set, only that semester's entry is removed.
    Returns (True, None) or (False, error_message).
    """
    key, bucket = _teacher_bucket(data, teacher_name)
    if not key:
        return False, 'Professor não identificado.'

    code = (turma or '').strip()
    if not code:
        return False, 'Turma não informada.'

    want_semester = _normalize_semester_id(semester_id)

    if students is not None and not want_semester:
        linked = count_students_in_turma(students, teacher_name, code)
        if linked:
            label = 'aluno' if linked == 1 else 'alunos'
            return False, (
                f'Não é possível excluir: {linked} {label} ainda vinculado(s) a esta turma. '
                'Altere a turma deles em Alunos antes de excluir.'
            )

    if not isinstance(bucket, list):
        return False, 'Turma não encontrada.'

    kept = []
    removed = 0
    for row in bucket:
        if not isinstance(row, dict):
            kept.append(row)
            continue
        if (row.get('turma') or '').strip() != code:
            kept.append(row)
            continue
        row_semester = _normalize_semester_id(row.get('semester_id'))
        if want_semester and row_semester and row_semester != want_semester:
            kept.append(row)
            continue
        removed += 1

    if not removed:
        return False, 'Turma não encontrada.'

    if students is not None and want_semester:
        # Only block when this is the last registry entry for the code.
        still_listed = any(
            isinstance(row, dict) and (row.get('turma') or '').strip() == code
            for row in kept
        )
        if not still_listed:
            linked = count_students_in_turma(students, teacher_name, code)
            if linked:
                label = 'aluno' if linked == 1 else 'alunos'
                return False, (
                    f'Não é possível excluir: {linked} {label} ainda vinculado(s) a esta turma. '
                    'Altere a turma deles em Alunos antes de excluir.'
                )

    if kept:
        data[key] = kept
    else:
        data.pop(key, None)
    return True, None


def move_class_between_teachers(data, from_teacher, to_teacher, turma):
    """
    Move a turma registry entry from one teacher to another.
    Returns (class_row, None) or (None, error_message).
    """
    from_name = normalize_teacher_name(from_teacher)
    to_name = normalize_teacher_name(to_teacher)
    if not from_name or not to_name:
        return None, 'Professor de origem ou destino não identificado.'
    if from_name.casefold() == to_name.casefold():
        return None, 'Origem e destino devem ser professores diferentes.'

    code = (turma or '').strip()
    if not code:
        return None, 'Turma não informada.'

    if code in turma_codes_for_teacher(data, to_name):
        return None, (
            f'O professor "{to_name}" já possui a turma "{code}". '
            'Escolha outro destino ou renomeie a turma antes de transferir.'
        )

    from_key, from_bucket = _teacher_bucket(data, from_name)
    if not from_key:
        return None, 'Professor de origem não identificado.'

    moved_row = None
    kept = []
    if isinstance(from_bucket, list):
        for row in from_bucket:
            if isinstance(row, dict) and (row.get('turma') or '').strip() == code:
                moved_row = dict(row)
            else:
                kept.append(row)

    if moved_row is None:
        moved_row = {
            'turma': code,
            'turma_display': code.replace('_', ' '),
            'class_weekdays': [],
            'class_time_start': '',
            'class_time_end': '',
            'horario': '',
            'legacy_import': True,
        }
    elif kept:
        data[from_key] = kept
    else:
        data.pop(from_key, None)

    to_key, to_bucket = _teacher_bucket(data, to_name)
    if to_key is None:
        to_key = to_name
    if not isinstance(to_bucket, list):
        to_bucket = []
    to_bucket.append(moved_row)
    data[to_key] = to_bucket
    return moved_row, None


def add_class(
    data,
    teacher_name,
    *,
    turma_display,
    class_weekdays=None,
    class_time_start='',
    class_time_end='',
    class_time='',
    horario='',
    turma='',
    semester_id='',
):
    """
    Register a new turma for a teacher. Returns (row, None) or (None, error_message).
    Livro/nível is chosen per student. Turma id is generated from the display name.
    Same code may exist again in a different semester.
    """
    key, bucket = _teacher_bucket(data, teacher_name)
    if not key:
        return None, 'Professor não identificado.'

    display = (turma_display or '').strip()
    if not display:
        return None, 'Informe o nome da turma.'

    weekdays = normalize_weekdays(class_weekdays or [])
    if len(weekdays) < 2:
        return None, 'Selecione dois dias da semana diferentes (a turma tem aula 2x por semana).'

    time_start = (class_time_start or class_time or '').strip()
    time_end = (class_time_end or '').strip()
    if not time_start or not time_end:
        return None, 'Informe o horário de início e de término da turma.'
    if time_start >= time_end:
        return None, 'O horário de término deve ser depois do horário de início.'

    code = (turma or '').strip() or turma_code_from_display(display)
    if not code:
        return None, 'Não foi possível gerar o identificador da turma.'

    sid = _normalize_semester_id(semester_id) or current_semester()

    horario = (horario or '').strip() or format_class_schedule(
        weekdays, time_start, time_end,
    )

    if bucket is None or not isinstance(bucket, list):
        bucket = []
        data[key] = bucket

    for row in bucket:
        if not isinstance(row, dict):
            continue
        if (row.get('turma') or '').strip().casefold() != code.casefold():
            continue
        row_semester = _normalize_semester_id(row.get('semester_id'))
        if not row_semester or row_semester == sid:
            return None, _duplicate_class_message(display, row)

    new_row = {
        'turma': code,
        'turma_display': display,
        'class_weekdays': weekdays,
        'class_time_start': time_start,
        'class_time_end': time_end,
        'horario': horario,
        'semester_id': sid,
    }
    bucket.append(new_row)
    return new_row, None


def update_class(
    data,
    teacher_name,
    turma,
    *,
    turma_display,
    class_weekdays=None,
    class_time_start='',
    class_time_end='',
    semester_id='',
):
    """
    Update an existing turma (code stays the same). Returns (row, None) or (None, error).
    """
    key, bucket = _teacher_bucket(data, teacher_name)
    if not key:
        return None, 'Professor não identificado.'

    code = (turma or '').strip()
    if not code:
        return None, 'Turma não informada.'

    display = (turma_display or '').strip()
    if not display:
        return None, 'Informe o nome da turma.'

    weekdays = normalize_weekdays(class_weekdays or [])
    if len(weekdays) < 2:
        return None, 'Selecione dois dias da semana diferentes (a turma tem aula 2x por semana).'

    time_start = (class_time_start or '').strip()
    time_end = (class_time_end or '').strip()
    if not time_start or not time_end:
        return None, 'Informe o horário de início e de término da turma.'
    if time_start >= time_end:
        return None, 'O horário de término deve ser depois do horário de início.'

    horario = format_class_schedule(weekdays, time_start, time_end)
    want_semester = _normalize_semester_id(semester_id)

    if not isinstance(bucket, list):
        return None, 'Turma não encontrada.'

    for i, row in enumerate(bucket):
        if not isinstance(row, dict) or (row.get('turma') or '').strip() != code:
            continue
        row_semester = _normalize_semester_id(row.get('semester_id'))
        if want_semester and row_semester and row_semester != want_semester:
            continue
        if want_semester and not row_semester:
            row_semester = want_semester
        updated = {
            'turma': code,
            'turma_display': display,
            'class_weekdays': weekdays,
            'class_time_start': time_start,
            'class_time_end': time_end,
            'horario': horario,
            'semester_id': row_semester or want_semester or current_semester(),
        }
        if row.get('legacy_import'):
            updated['legacy_import'] = True
        bucket[i] = updated
        data[key] = bucket
        return updated, None

    return None, 'Turma não encontrada.'


def class_display_from_student_rows(student_rows, turma_code):
    """Best-effort class name from existing student CSV rows (not livro/nível)."""
    names = []
    for row in student_rows:
        display = (row.get('turma_display') or '').strip()
        nivel = (row.get('nivel') or '').strip()
        if display and display != nivel and not is_valid_nivel(display):
            names.append(display)
    if names:
        return Counter(names).most_common(1)[0][0]
    return turma_code.replace('_', ' ')


def sync_teacher_classes_from_students(data, teacher_name, students, semester_id=''):
    """
    Import turmas that already exist on student rows but not in teacher_classes.json.
    Safe to run on every request (no-op when already synced).
    """
    key = normalize_teacher_name(teacher_name)
    if not key:
        return 0

    sid = _normalize_semester_id(semester_id) or current_semester()
    existing = turma_codes_for_teacher(data, teacher_name, semester_id=sid)
    # Also treat untagged legacy codes as already present.
    existing |= {
        r['turma'] for r in list_for_teacher(data, teacher_name)
        if not r.get('semester_id')
    }
    teacher_key = normalize_teacher_name(teacher_name).casefold()
    by_turma = {}
    for row in students:
        if normalize_teacher_name(row.get('teacher', '')).casefold() != teacher_key:
            continue
        code = (row.get('turma') or '').strip()
        if code:
            by_turma.setdefault(code, []).append(row)

    bucket = data.setdefault(key, [])
    if not isinstance(bucket, list):
        bucket = []
        data[key] = bucket

    added = 0
    for code, rows in sorted(by_turma.items()):
        if code in existing:
            continue
        horario = ''
        for row in rows:
            candidate = (row.get('horario') or '').strip()
            if candidate:
                horario = candidate
                break
        time_start, time_end = parse_time_range_from_horario(horario)
        bucket.append({
            'turma': code,
            'turma_display': class_display_from_student_rows(rows, code),
            'class_weekdays': [],
            'class_time_start': time_start,
            'class_time_end': time_end,
            'horario': horario,
            'semester_id': sid,
            'legacy_import': True,
        })
        existing.add(code)
        added += 1
        logger.info('Legacy turma imported for %s: %s', key, code)

    return added


def ensure_semester_ids(data, lessons=None, default_semester=None):
    """
    Backfill semester_id on registry rows missing it.
    Uses the most common lesson semester for that turma when available.
    Returns number of rows updated.
    """
    default = _normalize_semester_id(default_semester) or current_semester()
    by_turma = {}
    for lesson in lessons or []:
        code = (lesson.get('turma') or '').strip()
        sid = semester_for_date(lesson.get('date', ''))
        if code and sid:
            by_turma.setdefault(code, []).append(sid)

    inferred = {}
    for code, sids in by_turma.items():
        inferred[code] = Counter(sids).most_common(1)[0][0]

    updated = 0
    if not isinstance(data, dict):
        return 0
    for teacher_name, bucket in data.items():
        if not isinstance(bucket, list):
            continue
        for row in bucket:
            if not isinstance(row, dict):
                continue
            if _normalize_semester_id(row.get('semester_id')):
                continue
            code = (row.get('turma') or '').strip()
            row['semester_id'] = inferred.get(code) or default
            updated += 1
    return updated


def apply_registry_to_students(students, data, semester_id=None):
    """
    Re-join turma_display / horario from the registry (source of truth).
    Prefer the selected semester's entry; fall back to any matching code.
    Returns number of student rows updated in-memory.
    """
    if not students or not isinstance(data, dict):
        return 0

    # teacher_key -> turma -> preferred row
    lookup = {}
    for teacher_name in data:
        for entry in list_for_teacher(data, teacher_name):
            tkey = normalize_teacher_name(teacher_name).casefold()
            lookup.setdefault(tkey, {})
            code = entry['turma']
            existing = lookup[tkey].get(code)
            if existing is None:
                lookup[tkey][code] = entry
                continue
            want = _normalize_semester_id(semester_id)
            if want and entry.get('semester_id') == want:
                lookup[tkey][code] = entry

    changed = 0
    for row in students:
        tkey = normalize_teacher_name(row.get('teacher', '')).casefold()
        code = (row.get('turma') or '').strip()
        entry = (lookup.get(tkey) or {}).get(code)
        if not entry:
            continue
        display = (entry.get('turma_display') or '').strip()
        horario = (entry.get('horario') or '').strip()
        row_changed = False
        if display and (row.get('turma_display') or '').strip() != display:
            row['turma_display'] = display
            row_changed = True
        if horario and (row.get('horario') or '').strip() != horario:
            row['horario'] = horario
            row_changed = True
        if row_changed:
            changed += 1
    return changed
