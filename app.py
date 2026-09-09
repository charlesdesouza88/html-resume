#!/usr/bin/env python3
"""Mister Wiz Report Compiler — Web Dashboard"""

import csv
import base64
import functools
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from compiler import (build_class_ctx, build_student_ctx, create_report_environment,
                      generate_class_diagnostics, generate_individual_reports,
                      group_by_turma, load_csv, turmas_without_lessons)

from auth import (ROLE_ADMIN, ROLE_LABELS, ROLE_SUPERADMIN, ROLE_TEACHER,
                  UserStore, can_manage_teachers,
                  filter_lessons_for_user, filter_reports_for_user,
                  filter_students_for_user, find_extra_session_global_index,
                  find_lesson_global_index, find_student_global_index,
                  has_full_data_access, normalize_teacher_name, teacher_turmas,
                  user_public_dict)
from csv_import import parse_upload_csv
from extra_sessions import (AUTO_AULA_EXTRA_MARKER, EXTRA_SESSION_FIELD_LABELS,
                            EXTRA_SESSION_FIELDS, apply_open_extra_sessions_to_students,
                            apply_pending_session_flag_to_students,
                            build_atendimentos_template_csv,
                            SESSION_TYPE_CHOICES,
                            coerce_session_status_fields, display_status,
                            filter_sessions_for_teacher, is_status_ok,
                            normalize_aula_extra, parse_import_csv,
                            reconcile_flagged_students, remove_sessions_for_student,
                            row_from_form, sync_student_extra_sessions)
from lesson_attendance import (ATTENDANCE_CHOICES, ATTENDANCE_FIELDS,
                               ATTENDANCE_LABELS, attendance_map_for_lesson,
                               normalize_attendance_status, parse_attendance_form,
                               reconcile_presence_after_lesson_removed,
                               recompute_faltas_from_attendance,
                               remove_attendance_for_lesson,
                               remove_attendance_for_student,
                               replace_lesson_attendance, students_by_turma,
                               students_for_turma,
                               students_with_attendance_in_month)
from form_ui import (HABILIDADES_CHOICES, LICAO_CONTEUDO_CHOICES,
                     LICAO_ESPECIAL_CHOICES, NIVEL_CHOICES, WEEKDAY_CHOICES,
                     capitalize_student_name, date_from_form,
                     format_class_schedule, format_date_for_input,
                     is_valid_nivel, licao_choice_for_value, next_aula_num,
                     normalize_habilidades, parse_time_range_from_horario,
                     storage_date_to_iso,
                     storage_time_to_input, suggest_licao_conteudo,
                     time_from_form, turma_next_aula_map)
from teacher_classes import (add_class as register_teacher_class,
                             apply_registry_to_students,
                             class_display_from_student_rows,
                             count_students_in_turma, dedupe_class_options,
                             ensure_semester_ids,
                             find_class, list_for_teacher, load_registry,
                             registry_from_rows, registry_to_rows,
                             remove_class as delete_teacher_class,
                             save_registry, sync_teacher_classes_from_students,
                             turma_codes_for_teacher,
                             update_class as update_teacher_class)
from teacher_chat import (KIND_BUG, KIND_CHAT, ROOM_TITLE, append_message,
                          can_access_chat, can_resolve_bugs, load_messages,
                          messages_after, open_bug_count, resolve_bug,
                          save_messages, thread_tree)
from teacher_profiles import (can_edit_profile, can_view_profile, encode_photo,
                              get_or_empty, load_profiles, photo_data_url,
                              save_profiles, upsert_profile)
from login_events import (append_login, contact_email_for_user,
                          events_newest_first, format_logged_at,
                          last_login_by_user_id, load_events, mailto_href,
                          save_events, whatsapp_digits, whatsapp_href)
from class_transfer import (apply_transfer, build_transfer_export_zip,
                            list_teachers_from_students, preview_transfer,
                            turmas_for_teacher)
from report_periods import (available_report_months, available_semesters,
                            compute_month_trend, current_semester,
                            default_report_month, default_semester,
                            filter_lessons_by_month, filter_lessons_by_semester,
                            filter_months_by_semester,
                            filter_report_files_by_month,
                            individual_report_filename, load_snapshots,
                            month_in_semester, month_label, parse_lesson_month,
                            report_month_from_filename, save_snapshots,
                            semester_for_date, semester_for_month, semester_label,
                            student_composite_score, upsert_month_snapshots)
from student_transfer import (load_transfer_log, save_transfer_log,
                              students_with_transfer_aliases,
                              transfer_students, transfers_for_student)
from student_reviews import (MONTHLY_REVIEW_FIELDS, ROSTER_FIELDS,
                             apply_upload_row, extract_roster_fields,
                             DEFAULT_MONTHLY_VALUES,
                             load_monthly_reviews, merge_roster_for_month,
                             migrate_roster_scores_to_month,
                             rows_from_store, save_monthly_reviews,
                             split_student_row, store_from_rows,
                             migrate_roster_scores_to_month,
                             remove_reviews_for_student,
                             rows_from_store, save_monthly_reviews,
                             split_student_row, store_from_rows,
                             upsert_monthly_review)

try:
    from db_store import DatabaseStore, StaleDataError
except Exception as exc:
    DatabaseStore = None
    StaleDataError = Exception
    DB_IMPORT_ERROR = exc
else:
    DB_IMPORT_ERROR = None

BASE = Path(__file__).parent


def _atomic_write_csv(path, fieldnames, rows):
    """Write CSV rows to path atomically (temp file → os.replace)."""
    path = Path(path)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    content = buf.getvalue().encode('utf-8')
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ensure_writable_dir(path, fallback):
    candidate = Path(path)
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / '.mw_write_probe'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink(missing_ok=True)
        return candidate
    except OSError:
        backup = Path(fallback)
        backup.mkdir(parents=True, exist_ok=True)
        return backup


def _load_local_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / '.env')
        return
    except ImportError:
        pass
    env_path = BASE / '.env'
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            't': self.formatTime(record, '%Y-%m-%dT%H:%M:%S'),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        if record.exc_info:
            data['exc'] = self.formatException(record.exc_info)
        import json as _json
        return _json.dumps(data, ensure_ascii=False)


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(handlers=[_handler], level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if not DATABASE_URL:
    DATABASE_URL = os.environ.get('DATABASE_PRIVATE_URL', '').strip()
DB_ENABLED = bool(DATABASE_URL) and DatabaseStore is not None
db_store = None
DB_STARTUP_ERROR = None
_services_lock = threading.Lock()
_services_ready = False

if DATABASE_URL and DB_IMPORT_ERROR is not None:
    logger.error('DATABASE_URL is set but database dependencies failed to import: %s', DB_IMPORT_ERROR)
    DB_STARTUP_ERROR = f'Database dependencies failed to import: {DB_IMPORT_ERROR}'


def _init_application_services():
    """Connect DB and bootstrap users after the app process is listening (Railway healthcheck)."""
    global db_store, DB_ENABLED, DB_STARTUP_ERROR, _services_ready

    if _services_ready:
        return

    with _services_lock:
        if _services_ready:
            return

        if DB_ENABLED:
            last_exc = None
            for attempt in range(1, 4):
                try:
                    db_store = DatabaseStore(DATABASE_URL)
                    db_store.initialize()
                    last_exc = None
                    logger.info('Database connected on attempt %s', attempt)
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.warning('Database startup attempt %s failed: %s', attempt, exc)
                    if attempt < 3:
                        time.sleep(2)
            if last_exc is not None:
                logger.exception('Database startup failed; falling back to CSV mode')
                db_store = None
                DB_ENABLED = False
                DB_STARTUP_ERROR = str(last_exc)

        user_store.db_store = db_store
        try:
            user_store.initialize()
            _bootstrap_auth_accounts()
        except Exception as exc:
            logger.exception('User store initialization failed: %s', exc)

        _services_ready = True


def _database_status():
    """Return connection diagnostics for /health/db and the dashboard."""
    if not DATABASE_URL:
        return {
            'configured': False,
            'connected': False,
            'mode': 'csv',
            'message': 'DATABASE_URL not set — using CSV files.',
        }
    if db_store is None:
        return {
            'configured': True,
            'connected': False,
            'mode': 'csv-fallback',
            'message': DB_STARTUP_ERROR or 'Database unavailable; using CSV fallback.',
        }
    try:
        db_store.check_connection()
        students = db_store.load_students()
        lessons = db_store.load_lessons()
        return {
            'configured': True,
            'connected': True,
            'mode': 'postgresql',
            'message': 'Connected to PostgreSQL.',
            'student_rows': len(students),
            'lesson_rows': len(lessons),
            'extra_session_rows': len(db_store.load_extra_sessions()),
        }
    except Exception as exc:
        logger.exception('Database health check failed: %s', exc)
        return {
            'configured': True,
            'connected': False,
            'mode': 'postgresql',
            'message': f'Database ping failed: {exc}',
        }


default_data_dir = str(BASE / 'data')
default_out_dir = str(BASE / 'output')

TMPL_DIR = BASE / 'templates'
DATA_DIR = _ensure_writable_dir(
    os.environ.get('DATA_DIR', default_data_dir), '/tmp/mw/data')
OUT_DIR = _ensure_writable_dir(
    os.environ.get('OUT_DIR', default_out_dir), '/tmp/mw/output')

if str(DATA_DIR).startswith('/tmp'):
    logger.warning(
        'DATA_DIR is %s — this path is ephemeral and data will be lost on container restart. '
        'Set DATA_DIR to a Railway volume mount or use DATABASE_URL for persistent storage.',
        DATA_DIR,
    )
SNAPSHOTS_PATH = DATA_DIR / 'student_snapshots.json'
MONTHLY_REVIEWS_PATH = DATA_DIR / 'student_monthly_reviews.json'
TEACHER_CLASSES_PATH = DATA_DIR / 'teacher_classes.json'
_monthly_migration_done = False

app = Flask(__name__, template_folder='web_templates')
SECRET_KEY = os.environ.get('SECRET_KEY')
PRODUCTION_ENV = (
    os.environ.get('FLASK_ENV') == 'production'
    or os.environ.get('RAILWAY_ENVIRONMENT')
    or os.environ.get('RAILWAY_SERVICE_ID')
)
if PRODUCTION_ENV and not SECRET_KEY:
    logger.critical('SECRET_KEY is not set — set it in Railway service variables.')
    raise RuntimeError('SECRET_KEY must be set in production.')
app.secret_key = SECRET_KEY or 'mw-dev-change-in-prod'
app.config.update(
    MAX_CONTENT_LENGTH=int(os.environ.get('MAX_UPLOAD_MB', '5')) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(PRODUCTION_ENV),
    WTF_CSRF_TIME_LIMIT=3600,
)

csrf = CSRFProtect(app)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri='memory://',
)


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    logger.warning('CSRF validation failed: %s', e.description)
    return render_template('login.html', error='Sessão expirada. Por favor, tente novamente.',
                           bootstrap_email='', accounts_configured=True), 400

app.jinja_env.globals.update(
    storage_date_to_iso=storage_date_to_iso,
    format_date_for_input=format_date_for_input,
    storage_time_to_input=storage_time_to_input,
    is_status_ok=is_status_ok,
    display_status=display_status,
    month_label=month_label,
    semester_label=semester_label,
    report_month_from_filename=report_month_from_filename,
    parse_lesson_month=parse_lesson_month,
)

SAVE_CONFLICT_MESSAGE = (
    'Os dados foram atualizados em outra aba ou por outro usuário. '
    'Recarregue a página e tente novamente.'
)


@app.context_processor
def inject_save_conflict():
    return {'save_conflict': session.pop('save_conflict', None)}


def _bind_store_version(store_name, version):
    if version is None:
        return
    versions = getattr(g, 'store_versions', None)
    if versions is None:
        versions = {}
        g.store_versions = versions
    versions[store_name] = version


def _expected_store_version(store_name):
    versions = getattr(g, 'store_versions', None)
    if not versions:
        return None
    return versions.get(store_name)


def _note_save_conflict():
    session['save_conflict'] = SAVE_CONFLICT_MESSAGE


def _safe_redirect_target(target):
    if not target:
        return None
    ref = urlparse(target)
    if ref.scheme or ref.netloc:
        host = urlparse(request.host_url)
        if (
            ref.scheme.lower() != host.scheme.lower()
            or ref.netloc.lower() != host.netloc.lower()
        ):
            return None
        path = ref.path or '/'
        query = f'?{ref.query}' if ref.query else ''
        fragment = f'#{ref.fragment}' if ref.fragment else ''
        return f'{path}{query}{fragment}'
    if not target.startswith('/') or target.startswith('//'):
        return None
    return target


def _redirect_with_review_month(target, month):
    """Return a safe redirect path with ?month= updated to the selected review month."""
    safe = _safe_redirect_target(target)
    if not safe:
        return url_for('dashboard', month=month)
    parsed = urlparse(safe)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query['month'] = [month]
    new_query = urlencode(query, doseq=True)
    fragment = f'#{parsed.fragment}' if parsed.fragment else ''
    path = parsed.path or '/'
    if new_query:
        return f'{path}?{new_query}{fragment}'
    return f'{path}{fragment}'
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
SUPERADMIN_EMAIL = os.environ.get('SUPERADMIN_EMAIL', 'admin@misterwiz.local').strip()
SUPERADMIN_PASSWORD = os.environ.get('SUPERADMIN_PASSWORD', ADMIN_PASSWORD).strip()
SUPERADMIN_SYNC_PASSWORD = os.environ.get('SUPERADMIN_SYNC_PASSWORD', '').lower() in (
    '1', 'true', 'yes',
)


def _bootstrap_auth_accounts():
    """Keep PostgreSQL/JSON users aligned with SUPERADMIN_EMAIL + SUPERADMIN_PASSWORD."""
    user_store.apply_env_superadmin(SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD)
    if SUPERADMIN_SYNC_PASSWORD:
        user_store.sync_superadmin_password(SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD)


user_store = UserStore(
    db_store=None,
    json_path=DATA_DIR / 'users.json',
)

STUDENT_FIELDS = [
    'teacher', 'turma', 'turma_display', 'nivel', 'horario', 'student_name',
    'participacao', 'comportamento', 'speaking', 'listening', 'foco',
    'writing', 'reading', 'gramatica', 'trabalho_equipe', 'organizacao',
    'pontualidade', 'respeito_regras', 'faltas', 'missed_aulas', 'aula_extra',
    'feedback_participacao', 'feedback_foco', 'feedback_trabalho_equipe',
    'recomendacoes', 'observacao',
]
SCORE_FIELDS = {
    'participacao', 'comportamento', 'speaking', 'listening', 'foco',
    'writing', 'reading', 'gramatica', 'trabalho_equipe', 'organizacao',
    'pontualidade', 'respeito_regras',
}
LESSON_FIELDS = [
    'turma', 'aula_num', 'date', 'licao_conteudo', 'atividade_extra', 'habilidades'
]

TEMPLATE_DIR = BASE / 'data' / 'templates'

STUDENT_FIELD_LABELS = {
    'teacher': 'Professor',
    'turma': 'Código turma',
    'turma_display': 'Nome exibido',
    'nivel': 'Nível / livro',
    'horario': 'Horário',
    'student_name': 'Nome do aluno',
    'participacao': 'Participação',
    'comportamento': 'Comportamento',
    'speaking': 'Fala',
    'listening': 'Audição',
    'foco': 'Foco',
    'writing': 'Escrita',
    'reading': 'Leitura',
    'gramatica': 'Gramática',
    'trabalho_equipe': 'Trabalho em equipe',
    'organizacao': 'Organização',
    'pontualidade': 'Pontualidade',
    'respeito_regras': 'Respeito às regras',
    'faltas': 'Faltas',
    'missed_aulas': 'Aulas perdidas',
    'aula_extra': 'Aula extra',
    'feedback_participacao': 'Feedback — participação',
    'feedback_foco': 'Feedback — foco',
    'feedback_trabalho_equipe': 'Feedback — equipe',
    'recomendacoes': 'Recomendações',
    'observacao': 'Observação',
}

LESSON_FIELD_LABELS = {
    'turma': 'Código turma',
    'aula_num': 'Nº da aula',
    'date': 'Data',
    'licao_conteudo': 'Conteúdo da lição',
    'atividade_extra': 'Atividade extra',
    'habilidades': 'Habilidades',
}

STUDENT_TEMPLATE_SECTIONS = [
    {'title': 'Identificação', 'fields': ['teacher', 'turma', 'turma_display', 'nivel', 'horario', 'student_name']},
    {'title': 'Notas (1–5)', 'fields': [
        'participacao', 'comportamento', 'speaking', 'listening', 'foco',
        'writing', 'reading', 'gramatica', 'trabalho_equipe', 'organizacao',
        'pontualidade', 'respeito_regras',
    ]},
    {'title': 'Presença', 'fields': ['faltas', 'missed_aulas', 'aula_extra']},
    {'title': 'Textos do relatório', 'fields': [
        'feedback_participacao', 'feedback_foco', 'feedback_trabalho_equipe',
        'recomendacoes', 'observacao',
    ]},
]

TEMPLATE_ROWS = {
    'students': {
        'teacher': 'Chuck',
        'turma': 'MASTER',
        'turma_display': 'Masters',
        'nivel': 'Adults Book 4',
        'horario': 'Terça e quinta, 19:00 - 20:00',
        'student_name': 'Jane Doe',
        'participacao': '4',
        'comportamento': '4',
        'speaking': '4',
        'listening': '5',
        'foco': '4',
        'writing': '3',
        'reading': '4',
        'gramatica': '3',
        'trabalho_equipe': '4',
        'organizacao': '4',
        'pontualidade': '4',
        'respeito_regras': '4',
        'faltas': '1',
        'missed_aulas': '2,5',
        'aula_extra': 'Reforço',
        'feedback_participacao': 'Participa bem em aula e responde quando solicitada.',
        'feedback_foco': 'Mantém foco na maior parte do tempo.',
        'feedback_trabalho_equipe': 'Colabora bem com colegas em atividades em grupo.',
        'recomendacoes': 'Praticar speaking em casa 2x por semana com frases da aula.',
        'observacao': 'Substitua este aluno de exemplo pelos dados reais da turma.',
    },
    'lessons': {
        'turma': 'MASTER',
        'aula_num': '1',
        'date': '10/02/2026',
        'licao_conteudo': 'Lição 1',
        'atividade_extra': 'Syllabus / class rules',
        'habilidades': 'Inteligência emocional',
    },
}

LESSON_TEMPLATE_ROWS = [
    TEMPLATE_ROWS['lessons'],
    {
        'turma': 'MASTER',
        'aula_num': '2',
        'date': '12/02/2026',
        'licao_conteudo': 'Lição 2',
        'atividade_extra': 'Pair work — connection words',
        'habilidades': 'Empreendedorismo',
    },
    {
        'turma': 'MASTER',
        'aula_num': '3',
        'date': '19/02/2026',
        'licao_conteudo': 'Lição 3',
        'atividade_extra': 'Group presentation',
        'habilidades': 'Inteligência Artificial',
    },
]


def _student_preview_columns():
    columns = []
    for section in STUDENT_TEMPLATE_SECTIONS:
        for field in section['fields']:
            columns.append({
                'field': field,
                'label': STUDENT_FIELD_LABELS.get(field, field),
                'section': section['title'],
            })
    return columns


def _lesson_preview_columns():
    return [
        {'field': field, 'label': LESSON_FIELD_LABELS.get(field, field)}
        for field in LESSON_FIELDS
    ]


def _load_template_rows(name):
    path = TEMPLATE_DIR / f'{name}_template.csv'
    if path.exists():
        return load_csv(path)
    if name == 'students':
        return [TEMPLATE_ROWS['students']]
    return LESSON_TEMPLATE_ROWS


def _build_template_csv(name):
    fields = STUDENT_FIELDS if name == 'students' else LESSON_FIELDS
    rows = _load_template_rows(name)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return '\ufeff' + buf.getvalue()


def _read_csv_text(uploaded_file):
    try:
        return uploaded_file.read().decode('utf-8-sig'), None
    except UnicodeDecodeError:
        return None, 'Arquivo deve estar em UTF-8.'


def _validate_csv(key, text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return ['CSV sem cabecalho valido.']

    headers = [h.strip() for h in reader.fieldnames if h is not None]
    required = STUDENT_FIELDS if key == 'students' else LESSON_FIELDS
    missing = [f for f in required if f not in headers]
    if missing:
        return [f'Colunas obrigatorias ausentes: {", ".join(missing)}']

    rows = []
    for raw in reader:
        row = {}
        for k, v in raw.items():
            if k is None:
                continue
            row[k.strip()] = (v or '').strip()
        if any(row.values()):
            rows.append(row)

    if not rows:
        return ['CSV sem linhas de dados.']

    errors = []
    for idx, row in enumerate(rows, start=2):
        if key == 'students':
            if not row.get('turma'):
                errors.append(f'Linha {idx}: turma nao pode estar vazia.')
            if not row.get('student_name'):
                errors.append(f'Linha {idx}: student_name nao pode estar vazio.')

            for field in SCORE_FIELDS:
                val = row.get(field, '')
                if not val:
                    continue
                try:
                    num = int(float(val))
                except ValueError:
                    errors.append(f'Linha {idx}: {field} deve ser numero de 1 a 5.')
                    continue
                if num < 1 or num > 5:
                    errors.append(f'Linha {idx}: {field} deve estar entre 1 e 5.')

            faltas = row.get('faltas', '')
            if faltas:
                try:
                    if int(float(faltas)) < 0:
                        errors.append(f'Linha {idx}: faltas nao pode ser negativo.')
                except ValueError:
                    errors.append(f'Linha {idx}: faltas deve ser numero inteiro.')
        else:
            if not row.get('turma'):
                errors.append(f'Linha {idx}: turma nao pode estar vazia.')
            if not row.get('aula_num'):
                errors.append(f'Linha {idx}: aula_num nao pode estar vazio.')

        if len(errors) >= 10:
            errors.append('Muitos erros encontrados; corrija os primeiros e tente novamente.')
            break

    return errors


def _teacher_scope_errors(user):
    if has_full_data_access(user['role']):
        return None
    if user['role'] != ROLE_TEACHER:
        return ['Conta sem permissão para enviar CSV.']
    if not normalize_teacher_name(user.get('teacher_name', '')):
        return ['Seu perfil não tem nome de professor vinculado. Peça ao admin em Usuários.']
    return None


def _validate_teacher_student_rows(rows, user):
    teacher_key = normalize_teacher_name(user.get('teacher_name', '')).casefold()
    expected = user.get('teacher_name', '')
    errors = []
    for idx, row in enumerate(rows, start=2):
        row_teacher = normalize_teacher_name(row.get('teacher', '')).casefold()
        if row_teacher != teacher_key:
            errors.append(
                f'Linha {idx}: coluna teacher deve ser "{expected}" (apenas seus alunos).',
            )
        if len(errors) >= 10:
            errors.append('Muitos erros encontrados; corrija os primeiros e tente novamente.')
            break
    return errors


def _validate_teacher_lesson_rows(rows, user, students):
    turmas = teacher_turmas(students, user.get('teacher_name', ''))
    if not turmas:
        return [
            'Nenhuma turma vinculada ao seu perfil. Envie o CSV de alunos antes das aulas.',
        ]
    errors = []
    for idx, row in enumerate(rows, start=2):
        turma = row.get('turma', '').strip()
        if turma not in turmas:
            errors.append(
                f'Linha {idx}: turma "{turma}" não é sua '
                f'(permitidas: {", ".join(sorted(turmas))}).',
            )
        if len(errors) >= 10:
            errors.append('Muitos erros encontrados; corrija os primeiros e tente novamente.')
            break
    return errors


def _merge_teacher_students(new_rows, user):
    """Replace only the turmas present in the upload — never the teacher's other turmas."""
    teacher_key = normalize_teacher_name(user.get('teacher_name', '')).casefold()
    upload_turmas = {
        row.get('turma', '').strip().casefold()
        for row in new_rows
        if row.get('turma', '').strip()
    }
    kept = [
        row for row in _load_roster_students()
        if normalize_teacher_name(row.get('teacher', '')).casefold() != teacher_key
        or row.get('turma', '').strip().casefold() not in upload_turmas
    ]
    return kept + new_rows


def _merge_teacher_lessons(new_rows, user, students):
    """Replace only the teacher's turmas that appear in the upload."""
    turma_keys = {
        t.casefold() for t in teacher_turmas(students, user.get('teacher_name', ''))
    }
    upload_turmas = {
        row.get('turma', '').strip().casefold()
        for row in new_rows
        if row.get('turma', '').strip()
    }
    replaced = turma_keys & upload_turmas
    kept = [
        row for row in _load_lessons()
        if row.get('turma', '').strip().casefold() not in replaced
    ]
    return kept + new_rows


def _save_upload_dataset(key, rows, user, students_context=None):
    if key == 'students':
        review_month = _get_review_month()
        store = _load_monthly_review_store()
        roster_rows = []
        for row in rows:
            profile = apply_upload_row(row, store, review_month)
            roster_rows.append(_roster_row_for_storage(profile))
        _save_monthly_review_store(store)
        if has_full_data_access(user['role']):
            _save_students(roster_rows)
        else:
            _save_students(_merge_teacher_students(roster_rows, user))
        return

    if has_full_data_access(user['role']):
        _save_lessons(rows)
        return

    students = students_context if students_context is not None else _load_roster_students()
    _save_lessons(_merge_teacher_lessons(rows, user, students))


def _teacher_template_rows(name, user):
    if name == 'students':
        row = dict(TEMPLATE_ROWS['students'])
        row['teacher'] = user.get('teacher_name') or row.get('teacher', '')
        return [row]
    all_students = _load_students()
    turmas = teacher_turmas(all_students, user.get('teacher_name', ''))
    if turmas:
        return [r for r in LESSON_TEMPLATE_ROWS if r.get('turma', '').strip() in turmas]
    return [dict(LESSON_TEMPLATE_ROWS[0])]


def _current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    try:
        user = user_store.get_by_id(user_id)
    except Exception as exc:
        logger.warning('Could not refresh session user %s: %s', user_id, exc)
        session.clear()
        return None
    if not user or not user.get('active', True):
        session.clear()
        return None
    public = user_public_dict(user)
    session['email'] = public['email']
    session['role'] = public['role']
    session['teacher_name'] = public.get('teacher_name') or ''
    return public


def _login_session(user):
    session.clear()
    session['authenticated'] = True
    session['user_id'] = user['id']
    session['email'] = user['email']
    session['role'] = user['role']
    session['teacher_name'] = user.get('teacher_name') or ''


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _current_user():
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def login_required_json(f):
    """Like login_required, but returns JSON 401 for autosave / fetch clients."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _current_user():
            return jsonify({
                'ok': False,
                'login_required': True,
                'error': 'Sessão expirada. Faça login novamente.',
            }), 401
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    def decorator(f):
        @functools.wraps(f)
        @login_required
        def wrapped(*args, **kwargs):
            user = _current_user()
            if user['role'] not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


def _scoped_students(review_month=None, merge=True, apply_attendance=False):
    _ensure_monthly_migration()
    roster = _load_roster_students()
    user = _current_user()
    if not user:
        return roster, []
    # Re-join turma display/horario from registry for this request (source of truth).
    _enrich_students_from_registry(roster)
    visible_roster = filter_students_for_user(roster, user)
    if not merge:
        return roster, visible_roster
    if review_month is None:
        review_month = _get_review_month(_load_lessons())
    store = _load_monthly_review_store()
    visible = merge_roster_for_month(visible_roster, store, review_month)
    if review_month and apply_attendance:
        visible = recompute_faltas_from_attendance(
            visible,
            _load_lessons(),
            _load_lesson_attendance(),
            review_month,
        )
    visible = apply_open_extra_sessions_to_students(visible, _load_extra_sessions())
    return roster, visible


def _scoped_lessons(all_students=None, semester_id=None):
    all_lessons = _sort_lessons(_load_lessons())
    user = _current_user()
    if not user:
        return all_lessons, []
    if all_students is None:
        all_students = _load_students()
    visible = filter_lessons_for_user(all_lessons, all_students, user)
    sid = semester_id if semester_id is not None else _get_review_semester(all_lessons)
    if sid:
        visible = filter_lessons_by_semester(visible, sid)
    return all_lessons, visible


def _enrich_students_from_registry(students, semester_id=None):
    """Keep student turma_display/horario aligned with the live registry."""
    if not students:
        return 0
    registry = _load_teacher_class_registry()
    sid = semester_id if semester_id is not None else _get_review_semester()
    return apply_registry_to_students(students, registry, semester_id=sid)


def _teacher_classes_path():
    """Prefer live DATA_DIR so tests/monkeypatches stay consistent."""
    return Path(DATA_DIR) / 'teacher_classes.json'


def _load_teacher_class_registry(*, migrate=True):
    if db_store:
        rows, version = db_store.load_teacher_classes_versioned()
        _bind_store_version('teacher_classes', version)
        if not rows:
            file_data = load_registry(_teacher_classes_path())
            if file_data:
                if migrate:
                    _migrate_registry_semesters(file_data)
                seeded = registry_to_rows(file_data)
                try:
                    version = db_store.save_teacher_classes(seeded)
                    _bind_store_version('teacher_classes', version)
                except StaleDataError:
                    rows, version = db_store.load_teacher_classes_versioned()
                    _bind_store_version('teacher_classes', version)
                    return registry_from_rows(rows)
                return file_data
        data = registry_from_rows(rows)
    else:
        data = load_registry(_teacher_classes_path())

    if migrate and _migrate_registry_semesters(data):
        _save_teacher_class_registry(data)
    return data


def _migrate_registry_semesters(data):
    """Backfill semester_id; return True when the registry was changed."""
    lessons = []
    try:
        lessons = _load_lessons()
    except Exception:
        pass
    return bool(ensure_semester_ids(
        data,
        lessons=lessons,
        default_semester=default_semester(lessons),
    ))


def _save_teacher_class_registry(data):
    if db_store:
        rows = registry_to_rows(data)
        try:
            version = db_store.save_teacher_classes(
                rows,
                expected_version=_expected_store_version('teacher_classes'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('teacher_classes', version)
        try:
            save_registry(_teacher_classes_path(), data)
        except OSError as exc:
            logger.warning('Could not mirror teacher_classes.json: %s', exc)
        return True
    save_registry(_teacher_classes_path(), data)
    return True


def _sync_teacher_registry(user, all_students, semester_id=None):
    """After deploy: register turmas that already exist on student rows (one-time per turma)."""
    if not user or user['role'] != ROLE_TEACHER:
        return 0
    name = user.get('teacher_name', '')
    if not name:
        return 0
    data = _load_teacher_class_registry()
    sid = semester_id or _get_review_semester()
    added = sync_teacher_classes_from_students(
        data, name, all_students, semester_id=sid,
    )
    if added:
        if not _save_teacher_class_registry(data):
            return 0
        logger.info('Synced %s legacy turma(s) for teacher %s', added, name)
    return added


def _teacher_class_options(teacher_name, semester_id=None, *, all_semesters=False):
    registry = _load_teacher_class_registry()
    if all_semesters:
        return dedupe_class_options(
            list_for_teacher(registry, teacher_name, semester_id=None),
            prefer_semester=_get_review_semester(),
        )
    sid = semester_id if semester_id is not None else _get_review_semester()
    return list_for_teacher(registry, teacher_name, semester_id=sid)


def _teacher_class_registry_map(teacher_name, *, all_semesters=False):
    return {
        row['turma']: row
        for row in _teacher_class_options(teacher_name, all_semesters=all_semesters)
    }


def _teacher_class_options_for_form(teacher_name, student=None, *, is_new=False):
    """Registry turmas plus the student's current turma when editing legacy rows."""
    if is_new:
        options = _teacher_class_options(teacher_name, all_semesters=True)
    else:
        options = _teacher_class_options(teacher_name)
    if not student:
        return options
    code = (student.get('turma') or '').strip()
    if not code or code in {o['turma'] for o in options}:
        return options
    options = list(options)
    options.append({
        'turma': code,
        'turma_display': _class_name_from_student_row(student),
        'horario': (student.get('horario') or '').strip(),
        'needs_schedule': True,
    })
    return sorted(options, key=lambda r: (r['turma_display'].casefold(), r['turma']))


def _serialize_class_option(option):
    sid = (option.get('semester_id') or '').strip()
    return {
        'turma': option['turma'],
        'turma_display': option['turma_display'],
        'horario': (option.get('horario') or '').strip(),
        'needs_schedule': bool(option.get('needs_schedule')),
        'semester_id': sid,
        'semester_label': semester_label(sid) if sid else '',
    }


def _teacher_classes_by_teacher(*, is_new=False):
    """Map teacher name → registry turmas (for student form dropdowns)."""
    out = {}
    for name in _teacher_names_for_forms():
        options = _teacher_class_options_for_form(name, student=None, is_new=is_new)
        if options:
            out[name] = [_serialize_class_option(row) for row in options]
    return out


def _teacher_registered_turmas(teacher_name, semester_id=None, *, all_semesters=False):
    registry = _load_teacher_class_registry()
    if all_semesters:
        return turma_codes_for_teacher(registry, teacher_name, semester_id=None)
    sid = semester_id if semester_id is not None else _get_review_semester()
    return turma_codes_for_teacher(registry, teacher_name, semester_id=sid)


def _allowed_turmas(all_students, user):
    if has_full_data_access(user['role']):
        return sorted(
            {s.get('turma', '').strip() for s in all_students if s.get('turma', '').strip()}
            | _registry_turma_codes()
        )
    name = user.get('teacher_name', '')
    return sorted(
        _teacher_registered_turmas(name, all_semesters=True)
        | teacher_turmas(all_students, name)
    )


def _teacher_may_use_turma(turma, all_students, user):
    if has_full_data_access(user['role']):
        return True
    turma = (turma or '').strip()
    name = user.get('teacher_name', '')
    if turma in _teacher_registered_turmas(name):
        return True
    return turma in teacher_turmas(all_students, name)


def _teacher_may_use_student_turma(turma, all_students, user, nivel=''):
    if has_full_data_access(user['role']):
        return True
    turma = (turma or '').strip()
    name = user.get('teacher_name', '')
    if turma in _teacher_registered_turmas(name, all_semesters=True):
        return True
    if turma in teacher_turmas(all_students, name):
        return True
    return False


def _student_row_from_form():
    row = {field: (request.form.get(field, '') or '').strip() for field in STUDENT_FIELDS}
    row['student_name'] = capitalize_student_name(row.get('student_name'))
    row = _resolve_teacher_class_choice(row)
    row['aula_extra'] = normalize_aula_extra(row.get('aula_extra'))
    return _normalize_presence_fields(row)


def _normalize_presence_fields(row):
    """Keep faltas and missed_aulas consistent when teachers save presence data."""
    missed_raw = (row.get('missed_aulas') or '').strip()
    nums = [num.strip() for num in missed_raw.split(',') if num.strip()]
    try:
        faltas = max(0, int((row.get('faltas') or '0').strip() or 0))
    except (TypeError, ValueError):
        faltas = 0
    if nums:
        row['missed_aulas'] = ','.join(nums)
        row['faltas'] = str(max(faltas, len(nums)))
    else:
        row['missed_aulas'] = ''
        row['faltas'] = str(faltas)
    return row


def _resolve_teacher_class_choice(row):
    """Apply turma registry metadata when a class is picked from the dashboard list."""
    user = _current_user()
    if not user:
        return row

    choice = (request.form.get('class_choice') or '').strip()
    if not choice:
        return row

    if user['role'] == ROLE_TEACHER:
        owner = user.get('teacher_name', '')
    elif has_full_data_access(user['role']):
        owner = (row.get('teacher') or request.form.get('teacher') or '').strip()
    else:
        return row

    if not owner:
        return row

    row['turma'] = choice
    registered = _teacher_class_registry_map(owner, all_semesters=True)
    if choice in registered:
        reg = registered[choice]
        row['turma_display'] = reg['turma_display']
        if reg.get('horario'):
            row['horario'] = reg['horario']
    return row


def _duplicate_student_error(all_students, turma, name):
    """Error message when saving would create two same-named students in a turma.

    Duplicate names share review/report keys, so the second student would
    silently overwrite the first one's history.
    """
    key = (turma.strip().casefold(), name.strip().casefold())
    matches = sum(
        1 for row in all_students
        if ((row.get('turma') or '').strip().casefold(),
            (row.get('student_name') or '').strip().casefold()) == key
    )
    is_new_student = request.endpoint == 'student_new'
    orig_name = (request.form.get('orig_student_name') or '').strip()
    orig_turma = (request.form.get('orig_turma') or '').strip()
    if is_new_student:
        duplicate = matches >= 1
    elif orig_name:
        same_identity = (orig_turma.casefold(), orig_name.casefold()) == key
        duplicate = matches > 1 if same_identity else matches >= 1
    else:
        # Legacy form without original identity: assume the row itself matches.
        duplicate = matches > 1
    if duplicate:
        return (
            f'Já existe um aluno chamado "{name}" na turma {turma}. '
            'Use um sobrenome ou inicial para diferenciar.'
        )
    return None


def _student_identity_conflict(visible_row, form):
    """True when the row at this index is no longer the student the form was opened for."""
    orig_name = (form.get('orig_student_name') or '').strip()
    if not orig_name:
        return False
    orig_turma = (form.get('orig_turma') or '').strip()
    return (
        orig_name.casefold() != (visible_row.get('student_name') or '').strip().casefold()
        or orig_turma.casefold() != (visible_row.get('turma') or '').strip().casefold()
    )


STUDENT_LIST_CHANGED_MESSAGE = (
    'A lista de alunos mudou desde que você abriu esta página. '
    'Recarregue a página e tente novamente.'
)


def _validate_student_row(row, all_students, user, *, autosave=False):
    """Return a user-facing error message, or None when the row can be saved."""
    name = (row.get('student_name') or '').strip()
    turma = (row.get('turma') or '').strip()
    nivel = (row.get('nivel') or '').strip()
    class_choice = (request.form.get('class_choice') or '').strip()

    if autosave:
        if not name or not turma:
            return 'Informe o nome do aluno e a turma.'
        if not _teacher_may_use_student_turma(turma, all_students, user, nivel=nivel):
            return 'Turma não permitida.'
        return _duplicate_student_error(all_students, turma, name)

    if (
        user['role'] == ROLE_TEACHER
        and request.endpoint == 'student_new'
        and not class_choice
    ):
        registered = sorted(_teacher_registered_turmas(
            user.get('teacher_name', ''), all_semesters=True,
        ))
        if not registered:
            return 'Crie uma turma no Dashboard antes de cadastrar alunos.'
        return 'Selecione a turma do aluno.'

    if not name or not turma:
        return 'Informe o nome do aluno e a turma.'

    if has_full_data_access(user['role']) and request.endpoint in ('student_new', 'student_edit'):
        owner = (row.get('teacher') or request.form.get('teacher') or '').strip()
        if not owner:
            return 'Selecione o professor.'
        registered = _teacher_registered_turmas(owner, all_semesters=True)
        if registered:
            if not class_choice:
                return 'Selecione a turma do aluno.'
            if turma not in registered:
                return 'Selecione uma turma cadastrada para o professor escolhido.'

    if user['role'] == ROLE_TEACHER and request.endpoint == 'student_new':
        if not is_valid_nivel(nivel):
            return 'Selecione o livro/nível do aluno (KIDS 1–4 ou TEENS 1–5).'

    if user['role'] == ROLE_TEACHER and request.endpoint == 'student_new':
        registered = _teacher_registered_turmas(
            user.get('teacher_name', ''), all_semesters=True,
        )
        if turma not in registered:
            if not registered:
                return 'Crie uma turma no Dashboard antes de cadastrar alunos.'
            return (
                f'Selecione uma turma criada no Dashboard '
                f'({", ".join(sorted(registered))}).'
            )

    if not _teacher_may_use_student_turma(turma, all_students, user, nivel=nivel):
        registered = sorted(_teacher_registered_turmas(
            user.get('teacher_name', ''), all_semesters=True,
        ))
        if registered:
            return (
                f'Turma não permitida. Escolha uma turma criada no Dashboard '
                f'({", ".join(registered)}).'
            )
        return 'Crie uma turma no Dashboard antes de cadastrar alunos.'
    return _duplicate_student_error(all_students, turma, name)


def _student_form_context(all_rows, user, is_new, student, idx, form_error=None):
    teacher_class_options = []
    class_owner_teacher = ''
    teacher_names = []
    teacher_classes_by_teacher = {}
    teacher_picker = False
    if user and user['role'] == ROLE_TEACHER:
        class_owner_teacher = user.get('teacher_name', '')
        teacher_names = [class_owner_teacher] if class_owner_teacher else []
        teacher_picker = bool(class_owner_teacher)
        teacher_classes_by_teacher = _teacher_classes_by_teacher(is_new=is_new)
        teacher_class_options = _teacher_class_options_for_form(
            class_owner_teacher,
            student,
            is_new=is_new,
        )
    elif user and has_full_data_access(user['role']):
        teacher_picker = True
        teacher_names = _teacher_names_for_forms()
        teacher_classes_by_teacher = _teacher_classes_by_teacher(is_new=is_new)
        if request.method == 'POST':
            class_owner_teacher = (request.form.get('teacher') or '').strip()
        elif student:
            class_owner_teacher = (student.get('teacher') or '').strip()
        if class_owner_teacher:
            teacher_class_options = _teacher_class_options_for_form(
                class_owner_teacher,
                student,
                is_new=is_new,
            )
    class_choice = ''
    if request.method == 'POST':
        class_choice = (request.form.get('class_choice') or '').strip()
    elif user and user['role'] == ROLE_TEACHER and student and (student.get('turma') or '').strip():
        class_choice = (student.get('turma') or '').strip()
    elif user and has_full_data_access(user['role']) and student and (student.get('turma') or '').strip():
        class_choice = (student.get('turma') or '').strip()
    elif is_new and user and user['role'] == ROLE_TEACHER and teacher_class_options:
        wanted = (request.args.get('turma') or '').strip()
        codes = {c['turma'] for c in teacher_class_options}
        class_choice = wanted if wanted in codes else teacher_class_options[0]['turma']

    no_teacher_classes = bool(
        user and user['role'] == ROLE_TEACHER and not teacher_class_options,
    )
    needs_teacher_first = bool(
        user
        and has_full_data_access(user['role'])
        and is_new
        and not class_owner_teacher,
    )
    use_manual_turma = bool(
        user
        and has_full_data_access(user['role'])
        and class_owner_teacher
        and not teacher_class_options,
    )
    allow_class_pick = bool(
        user
        and not use_manual_turma
        and not needs_teacher_first
        and (
            user['role'] == ROLE_TEACHER
            or has_full_data_access(user['role'])
        )
    )

    transfer_history = []
    if not is_new and student and (student.get('student_name') or '').strip():
        entries = load_transfer_log(_student_transfers_path())
        transfer_history = transfers_for_student(
            entries, student.get('student_name'), student.get('turma'),
        )

    return dict(
        student=student,
        idx=idx,
        score_fields=SCORE_FIELDS,
        is_new=is_new,
        form_error=form_error,
        nivel_choices=NIVEL_CHOICES,
        teacher_class_options=teacher_class_options,
        class_choice=class_choice,
        class_owner_teacher=class_owner_teacher,
        teacher_names=teacher_names,
        teacher_classes_by_teacher=teacher_classes_by_teacher,
        teacher_picker=teacher_picker,
        needs_teacher_first=needs_teacher_first,
        use_manual_turma=use_manual_turma,
        no_teacher_classes=no_teacher_classes,
        allow_teacher_class_pick=allow_class_pick,
        transfer_history=transfer_history,
    )


def _lesson_from_form():
    row = {field: (request.form.get(field) or '').strip() for field in LESSON_FIELDS}
    row['date'] = date_from_form(request.form)
    row['habilidades'] = normalize_habilidades(row.get('habilidades', ''))
    return row


def _persist_lesson_attendance(lesson, roster):
    """Save attendance picks and sync faltas/missed_aulas into the lesson's review month."""
    entries = parse_attendance_form(request.form)
    if not entries:
        return
    turma = lesson.get('turma', '')
    aula_num = lesson.get('aula_num', '')
    attendance_rows = replace_lesson_attendance(
        _load_lesson_attendance(),
        turma,
        aula_num,
        entries,
    )
    _save_lesson_attendance(attendance_rows)
    month_key = parse_lesson_month(lesson.get('date', '')) or _get_review_month()
    lessons = _load_lessons()
    store = _load_monthly_review_store()
    merged = merge_roster_for_month(roster, store, month_key)
    tracked, _ = students_with_attendance_in_month(lessons, attendance_rows, month_key)
    updated = recompute_faltas_from_attendance(merged, lessons, attendance_rows, month_key)
    for row in updated:
        key = (
            (row.get('turma') or '').strip().upper(),
            (row.get('student_name') or '').strip(),
        )
        if key in tracked:
            upsert_monthly_review(store, row, month_key)
    _save_monthly_review_store(store)


def _lesson_edit_context(lesson, all_rows, all_students, allowed_turmas, is_new,
                         attendance_rows):
    """Template context for lesson create/edit with dropdown choices and auto aula_num."""
    lesson = dict(lesson)
    turmas = list(allowed_turmas or [])
    turma_next = turma_next_aula_map(all_rows, turmas)

    if is_new:
        if turmas:
            turma = next(
                (code for code in turmas if students_for_turma(all_students, code)),
                turmas[0],
            )
        else:
            turma = lesson.get('turma') or ''
        lesson['turma'] = turma
        if not lesson.get('aula_num'):
            lesson['aula_num'] = next_aula_num(all_rows, turma)
        if not lesson.get('licao_conteudo'):
            lesson['licao_conteudo'] = suggest_licao_conteudo(lesson.get('aula_num'))
    else:
        turma = lesson.get('turma') or ''

    lesson['habilidades'] = normalize_habilidades(lesson.get('habilidades', ''))
    licao_selected = licao_choice_for_value(lesson.get('licao_conteudo', ''))
    licao_legacy = (
        licao_selected
        if licao_selected and licao_selected not in LICAO_CONTEUDO_CHOICES
        else ''
    )
    if licao_legacy:
        lesson['licao_conteudo'] = licao_legacy
    elif licao_selected:
        lesson['licao_conteudo'] = licao_selected

    habilidades_legacy = (
        lesson.get('habilidades', '')
        if lesson.get('habilidades') and lesson['habilidades'] not in HABILIDADES_CHOICES
        else ''
    )

    turma_students = students_for_turma(all_students, turma)
    attendance_map = attendance_map_for_lesson(
        attendance_rows,
        turma,
        lesson.get('aula_num'),
    )
    for student in turma_students:
        name = (student.get('student_name') or '').strip()
        if name and name not in attendance_map:
            attendance_map[name] = 'present'

    return dict(
        lesson=lesson,
        habilidades_choices=HABILIDADES_CHOICES,
        habilidades_legacy=habilidades_legacy,
        licao_especial_choices=LICAO_ESPECIAL_CHOICES,
        licao_choices=tuple(f'Lição {n}' for n in range(1, 101)),
        licao_legacy=licao_legacy,
        turma_next_aula=turma_next,
        turma_students=turma_students,
        students_by_turma=students_by_turma(all_students, turmas),
        attendance_map=attendance_map,
        attendance_choices=ATTENDANCE_CHOICES,
        attendance_labels=ATTENDANCE_LABELS,
    )


def _list_turma_param():
    """Turma filter to preserve on list pages (query string or form hidden field)."""
    return (request.args.get('turma') or request.form.get('return_turma') or '').strip()


def _list_month_param():
    return (request.args.get('month') or request.form.get('return_month') or '').strip()


def _redirect_with_list_params(endpoint):
    turma = _list_turma_param()
    month = _list_month_param() or session.get('review_month') or ''
    kwargs = {}
    if turma:
        kwargs['turma'] = turma
    if month:
        kwargs['month'] = month
    return redirect(url_for(endpoint, **kwargs))


def _redirect_students(flash_ok=None, flash_error=None):
    if flash_ok:
        session['student_flash_ok'] = flash_ok
    if flash_error:
        session['student_flash_error'] = flash_error
    return _redirect_with_list_params('students')


def _redirect_lessons():
    return _redirect_with_list_params('lessons')


def _sort_lessons(rows):
    def sort_key(row):
        turma = row.get('turma', '')
        num = row.get('aula_num', '')
        try:
            num_val = int(float(num))
        except (TypeError, ValueError):
            num_val = 9999
        return (turma, num_val, num)

    return sorted(rows, key=sort_key)


def _merge_scoped_students(all_students, visible_students):
    """Replace visible rows in full list; used when teachers save edits."""
    if has_full_data_access(_current_user()['role']):
        return visible_students
    merged = list(all_students)
    visible_set = {id(r) for r in visible_students}
    for i, row in enumerate(merged):
        if id(row) in visible_set:
            for v in visible_students:
                if v is row or (
                    v.get('turma') == row.get('turma')
                    and v.get('student_name') == row.get('student_name')
                    and v.get('teacher') == row.get('teacher')
                ):
                    merged[i] = v
                    break
    return merged


@app.context_processor
def inject_auth():
    user = _current_user()
    lessons = _load_lessons()
    review_semester = _get_review_semester(lessons) if user else ''
    review_month = _get_review_month(lessons) if user else ''
    return {
        'current_user': user,
        'role_labels': ROLE_LABELS,
        'can_manage_teachers': can_manage_teachers(user['role']) if user else False,
        'can_edit_all_data': has_full_data_access(user['role']) if user else False,
        'can_upload_csv': bool(user),
        'can_upload_all_csv': has_full_data_access(user['role']) if user else False,
        'review_semester': review_semester,
        'review_semester_label': semester_label(review_semester) if review_semester else '',
        'available_review_semesters': _available_review_semesters(lessons) if user else [],
        'review_month': review_month,
        'review_month_label': month_label(review_month) if review_month else '',
        'available_review_months': _available_review_months(lessons) if user else [],
        'url_with_month': _url_with_month,
        'can_access_chat': can_access_chat(user) if user else False,
        'can_resolve_chat_bugs': can_resolve_bugs(user) if user else False,
        'chat_open_bugs': _chat_open_bug_count() if user and can_access_chat(user) else 0,
    }


def _load_roster_students():
    if db_store:
        rows, version = db_store.load_students_versioned()
        _bind_store_version('students', version)
        return rows
    path = DATA_DIR / 'students.csv'
    return load_csv(path) if path.exists() else []


def _load_students():
    return _load_roster_students()


def _roster_row_for_storage(profile):
    row = {field: '' for field in STUDENT_FIELDS}
    row.update({field: (profile.get(field) or '').strip() for field in ROSTER_FIELDS})
    return row


def _monthly_reviews_path():
    """Resolve monthly reviews file from DATA_DIR (supports test monkeypatching)."""
    return DATA_DIR / 'student_monthly_reviews.json'


def _student_transfers_path():
    """Resolve the student transfer log from DATA_DIR (supports test monkeypatching)."""
    return DATA_DIR / 'student_transfers.json'


def _load_monthly_review_store():
    if db_store:
        rows, version = db_store.load_monthly_reviews_versioned()
        _bind_store_version('monthly_reviews', version)
        return store_from_rows(rows)
    return load_monthly_reviews(_monthly_reviews_path())


def _save_monthly_review_store(store):
    rows = rows_from_store(store)
    if db_store:
        try:
            version = db_store.save_monthly_reviews(
                rows,
                expected_version=_expected_store_version('monthly_reviews'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('monthly_reviews', version)
        return True
    save_monthly_reviews(_monthly_reviews_path(), store)
    return True


def _merged_roster_for_month(roster, month_key):
    store = _load_monthly_review_store()
    return merge_roster_for_month(roster, store, month_key)


def _persist_monthly_rows(students, month_key):
    store = _load_monthly_review_store()
    for student in students:
        upsert_monthly_review(store, student, month_key)
    _save_monthly_review_store(store)


def _ensure_monthly_migration():
    global _monthly_migration_done
    if _monthly_migration_done:
        return
    lessons = _load_lessons()
    month = default_report_month(lessons)
    roster = _load_roster_students()
    store = _load_monthly_review_store()
    if migrate_roster_scores_to_month(roster, store, month):
        _save_monthly_review_store(store)
    _monthly_migration_done = True


def _available_review_semesters(lessons):
    found = set(available_semesters(lessons, include_current=True))
    # Keep the active session semester selectable even before it has lessons.
    session_sid = (session.get('review_semester') or '').strip()
    if session_sid:
        found.add(session_sid)
    month = (session.get('review_month') or '').strip()
    month_sid = semester_for_month(month)
    if month_sid:
        found.add(month_sid)
    return sorted(found)


def _get_review_semester(lessons=None):
    if lessons is None:
        lessons = _load_lessons()
    available = _available_review_semesters(lessons)
    raw = (
        request.args.get('semester')
        or request.form.get('return_semester')
        or session.get('review_semester')
        or ''
    ).strip()
    if raw in available:
        session['review_semester'] = raw
        return raw
    # Infer from selected month when possible.
    month = (session.get('review_month') or '').strip()
    inferred = semester_for_month(month) if month else None
    if inferred in available:
        session['review_semester'] = inferred
        return inferred
    default = default_semester(lessons)
    if default not in available and available:
        default = available[-1]
    session['review_semester'] = default
    return default


def _set_review_semester(semester_id, lessons=None):
    if lessons is None:
        lessons = _load_lessons()
    semester_id = (semester_id or '').strip()
    available = _available_review_semesters(lessons)
    if semester_id not in available:
        return False
    session['review_semester'] = semester_id
    # Keep review month inside the new semester.
    month = (session.get('review_month') or '').strip()
    if not month_in_semester(month, semester_id):
        semester_months = filter_months_by_semester(
            _available_review_months(lessons, semester_id=semester_id),
            semester_id,
        )
        if semester_months:
            session['review_month'] = semester_months[-1]
    return True


def _available_review_months(lessons, semester_id=None):
    months = list(available_report_months(lessons))
    current = datetime.now().strftime('%Y-%m')
    if current not in months:
        months.append(current)
    # Keep the selected review month visible after imports/creates.
    session_month = (session.get('review_month') or '').strip()
    if session_month and semester_for_month(session_month):
        months.append(session_month)
    months = sorted(set(months))
    sid = semester_id if semester_id is not None else session.get('review_semester')
    if sid:
        filtered = filter_months_by_semester(months, sid)
        if filtered:
            return filtered
    return months


def _get_review_month(lessons=None):
    if lessons is None:
        lessons = _load_lessons()
    semester = _get_review_semester(lessons)
    available = _available_review_months(lessons, semester_id=semester)
    raw = (
        request.args.get('month')
        or request.form.get('return_month')
        or session.get('review_month')
        or ''
    ).strip()
    if raw in available:
        session['review_month'] = raw
        # Keep semester aligned with the chosen month.
        month_semester = semester_for_month(raw)
        if month_semester:
            session['review_semester'] = month_semester
        return raw
    default = default_report_month(lessons)
    if default not in available:
        # Prefer a month inside the active semester.
        semester_default = filter_months_by_semester(
            available_report_months(lessons), semester,
        )
        if semester_default:
            default = semester_default[-1]
        elif available:
            default = available[-1]
    session['review_month'] = default
    return default


def _set_review_month(month, lessons=None):
    if lessons is None:
        lessons = _load_lessons()
    month = (month or '').strip()
    if not semester_for_month(month):
        return False
    session['review_month'] = month
    month_semester = semester_for_month(month)
    if month_semester:
        session['review_semester'] = month_semester
    return True


def _url_with_month(endpoint, **kwargs):
    month = kwargs.pop('month', None)
    if month is None:
        month = session.get('review_month', '')
    if month and 'month' not in kwargs:
        kwargs['month'] = month
    return url_for(endpoint, **kwargs)


def _persist_student_save(all_rows, global_idx, updated, review_month):
    profile, _ = split_student_row(updated)
    all_rows[global_idx] = _roster_row_for_storage(profile)
    if not _save_students(all_rows):
        return False
    store = _load_monthly_review_store()
    upsert_monthly_review(store, updated, review_month)
    if not _save_monthly_review_store(store):
        return False
    return True


def _persist_student_create(all_rows, new_row, review_month):
    profile, _ = split_student_row(new_row)
    all_rows.append(_roster_row_for_storage(profile))
    _save_students(all_rows)
    store = _load_monthly_review_store()
    upsert_monthly_review(store, new_row, review_month)
    _save_monthly_review_store(store)


def _load_lessons():
    if db_store:
        rows, version = db_store.load_lessons_versioned()
        _bind_store_version('lessons', version)
        return rows
    path = DATA_DIR / 'lessons.csv'
    return load_csv(path) if path.exists() else []


def _save_students(students):
    if db_store:
        try:
            version = db_store.save_students(
                students,
                expected_version=_expected_store_version('students'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('students', version)
        return True
    _atomic_write_csv(DATA_DIR / 'students.csv', STUDENT_FIELDS, students)
    return True


def _save_lessons(lessons):
    if db_store:
        try:
            version = db_store.save_lessons(
                lessons,
                expected_version=_expected_store_version('lessons'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('lessons', version)
        return True
    _atomic_write_csv(DATA_DIR / 'lessons.csv', LESSON_FIELDS, lessons)
    return True


def _load_extra_sessions():
    if db_store:
        rows, version = db_store.load_extra_sessions_versioned()
        _bind_store_version('extra_sessions', version)
        raw_rows = rows
    else:
        path = DATA_DIR / 'extra_sessions.csv'
        raw_rows = load_csv(path) if path.exists() else []
    return [coerce_session_status_fields(dict(row)) for row in raw_rows]


def _save_extra_sessions(rows):
    if db_store:
        try:
            version = db_store.save_extra_sessions(
                rows,
                expected_version=_expected_store_version('extra_sessions'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('extra_sessions', version)
        return True
    _atomic_write_csv(DATA_DIR / 'extra_sessions.csv', EXTRA_SESSION_FIELDS, rows)
    return True


def _chat_messages_path():
    return Path(DATA_DIR) / 'teacher_chat.json'


def _load_chat_messages():
    if db_store:
        rows, version = db_store.load_chat_messages_versioned()
        _bind_store_version('chat_messages', version)
        return list(rows or [])
    return load_messages(_chat_messages_path())


def _save_chat_messages(rows):
    if db_store:
        try:
            version = db_store.save_chat_messages(
                rows,
                expected_version=_expected_store_version('chat_messages'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('chat_messages', version)
        try:
            save_messages(_chat_messages_path(), rows)
        except OSError as exc:
            logger.warning('Could not mirror teacher_chat.json: %s', exc)
        return True
    save_messages(_chat_messages_path(), rows)
    return True


def _chat_open_bug_count():
    try:
        return open_bug_count(_load_chat_messages())
    except Exception:
        return 0


def _teacher_profiles_path():
    return Path(DATA_DIR) / 'teacher_profiles.json'


def _load_teacher_profiles():
    if db_store:
        rows, version = db_store.load_teacher_profiles_versioned()
        _bind_store_version('teacher_profiles', version)
        return list(rows or [])
    return load_profiles(_teacher_profiles_path())


def _save_teacher_profiles(rows):
    if db_store:
        try:
            version = db_store.save_teacher_profiles(
                rows,
                expected_version=_expected_store_version('teacher_profiles'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('teacher_profiles', version)
        try:
            save_profiles(_teacher_profiles_path(), rows)
        except OSError as exc:
            logger.warning('Could not mirror teacher_profiles.json: %s', exc)
        return True
    save_profiles(_teacher_profiles_path(), rows)
    return True


def _login_events_path():
    return Path(DATA_DIR) / 'login_events.json'


def _load_login_events():
    if db_store:
        rows, version = db_store.load_login_events_versioned()
        _bind_store_version('login_events', version)
        return list(rows or [])
    return load_events(_login_events_path())


def _save_login_events(rows):
    if db_store:
        try:
            version = db_store.save_login_events(
                rows,
                expected_version=_expected_store_version('login_events'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('login_events', version)
        try:
            save_events(_login_events_path(), rows)
        except OSError as exc:
            logger.warning('Could not mirror login_events.json: %s', exc)
        return True
    save_events(_login_events_path(), rows)
    return True


def _record_successful_login(user):
    """Persist a login history event; never block the login on storage errors."""
    try:
        rows = _load_login_events()
        if not append_login(rows, user):
            return
        if not _save_login_events(rows):
            logger.warning(
                'Could not save login event for user_id=%s (stale store)',
                user.get('id'),
            )
    except Exception as exc:
        logger.warning('Could not record login event: %s', exc)


def _sync_student_aula_extra_sessions(student):
    all_rows = _load_extra_sessions()
    updated = sync_student_extra_sessions(all_rows, student)
    if updated != all_rows:
        _save_extra_sessions(updated)


def _sync_extra_session_to_student_flag(session_row):
    """Keep monthly aula_extra in sync when an extra session is created or completed."""
    months = []
    current = _get_review_month()
    session_month = parse_lesson_month(session_row.get('date', ''))
    for month in (current, session_month):
        if month and month not in months:
            months.append(month)
    if not months:
        return
    roster = _load_roster_students()
    for month in months:
        merged = _merged_roster_for_month(roster, month)
        updated = apply_pending_session_flag_to_students(merged, session_row)
        if updated is not merged:
            _persist_monthly_rows(updated, month)


def _reconcile_flagged_extra_sessions(students):
    all_rows = _load_extra_sessions()
    updated = reconcile_flagged_students(all_rows, students)
    if updated != all_rows:
        _save_extra_sessions(updated)
    return updated


def _load_lesson_attendance():
    if db_store:
        rows, version = db_store.load_lesson_attendance_versioned()
        _bind_store_version('lesson_attendance', version)
        return rows
    path = DATA_DIR / 'lesson_attendance.csv'
    return load_csv(path) if path.exists() else []


def _save_lesson_attendance(rows):
    if db_store:
        try:
            version = db_store.save_lesson_attendance(
                rows,
                expected_version=_expected_store_version('lesson_attendance'),
            )
        except StaleDataError:
            _note_save_conflict()
            return False
        _bind_store_version('lesson_attendance', version)
        return True
    _atomic_write_csv(DATA_DIR / 'lesson_attendance.csv', ATTENDANCE_FIELDS, rows)
    return True


def _extra_session_visible_in_semester(row, semester_id):
    """Keep undated rows, current-semester rows, and still-pending sessions."""
    row_sid = semester_for_date(row.get('date', ''))
    if row_sid is None or row_sid == semester_id:
        return True
    return not is_status_ok(row.get('realizado'))


def _scoped_extra_sessions(semester_id=None):
    all_rows = _load_extra_sessions()
    user = _current_user()
    if not user:
        return all_rows, []
    if has_full_data_access(user['role']):
        visible = list(all_rows)
    else:
        roster = _load_roster_students()
        owned = filter_students_for_user(roster, user)
        visible = filter_sessions_for_teacher(
            all_rows, user.get('teacher_name', ''), owned,
        )
    sid = semester_id if semester_id is not None else _get_review_semester()
    if sid:
        visible = [row for row in visible if _extra_session_visible_in_semester(row, sid)]
    return all_rows, visible


def _csv_rows_from_text(text):
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        row = {}
        for k, v in raw.items():
            if k is None:
                continue
            row[k.strip()] = (v or '').strip()
        if any(row.values()):
            rows.append(row)
    return rows


def _rows_to_csv_text(key, rows):
    fields = STUDENT_FIELDS if key == 'students' else LESSON_FIELDS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _queue_upload_notice(message=None, error=None):
    if message:
        messages = list(session.get('upload_messages', []))
        messages.append(message)
        session['upload_messages'] = messages
    if error:
        errors = list(session.get('upload_errors', []))
        errors.append(error)
        session['upload_errors'] = errors


def _pull_upload_notices():
    messages = session.pop('upload_messages', [])
    errors = session.pop('upload_errors', [])
    return messages, errors


def _upload_page_context(user, messages=None, errors=None):
    messages = list(messages or [])
    errors = list(errors or [])
    all_students = _load_students()
    all_lessons = _load_lessons()
    if has_full_data_access(user['role']):
        students_exists = bool(all_students) if db_store else (DATA_DIR / 'students.csv').exists()
        lessons_exists = bool(all_lessons) if db_store else (DATA_DIR / 'lessons.csv').exists()
    else:
        students_exists = bool(filter_students_for_user(all_students, user))
        lessons_exists = bool(filter_lessons_for_user(all_lessons, all_students, user))
    return {
        'messages': messages,
        'errors': errors,
        'can_delete_all_csv': has_full_data_access(user['role']),
        'is_teacher_upload': not has_full_data_access(user['role']),
        'students_exists': students_exists,
        'lessons_exists': lessons_exists,
        'student_template_rows': _load_template_rows('students'),
        'lesson_template_rows': _load_template_rows('lessons'),
        'student_field_labels': STUDENT_FIELD_LABELS,
        'lesson_field_labels': LESSON_FIELD_LABELS,
        'student_template_sections': STUDENT_TEMPLATE_SECTIONS,
        'student_fields': STUDENT_FIELDS,
        'lesson_fields': LESSON_FIELDS,
        'student_preview_columns': _student_preview_columns(),
        'lesson_preview_columns': _lesson_preview_columns(),
        'score_fields': sorted(SCORE_FIELDS),
    }


def _delete_csv_dataset(name, user):
    """Remove uploaded CSV data. Admins clear all; teachers only their rows."""
    if name not in ('students', 'lessons'):
        abort(404)

    if has_full_data_access(user['role']):
        if db_store:
            if name == 'students':
                before = len(_load_students())
                _save_students([])
            else:
                before = len(_load_lessons())
                _save_lessons([])
        else:
            path = DATA_DIR / f'{name}.csv'
            before = len(load_csv(path)) if path.exists() else 0
            if path.exists():
                path.unlink()
        return before, True

    teacher_key = normalize_teacher_name(user.get('teacher_name', '')).casefold()
    if not teacher_key:
        return 0, False

    if name == 'students':
        all_rows = _load_students()
        kept = [
            row for row in all_rows
            if normalize_teacher_name(row.get('teacher', '')).casefold() != teacher_key
        ]
        removed = len(all_rows) - len(kept)
        _save_students(kept)
        return removed, False

    all_students = _load_students()
    turmas = teacher_turmas(all_students, user.get('teacher_name', ''))
    all_lessons = _load_lessons()
    kept = [
        row for row in all_lessons
        if row.get('turma', '').strip() not in turmas
    ]
    removed = len(all_lessons) - len(kept)
    _save_lessons(kept)
    return removed, False


# ── Auth ─────────────────────────────────────────────────────────────────────────────

@app.before_request
def _ensure_services_before_request():
    if request.endpoint == 'health':
        return
    _init_application_services()


@app.route('/health')
def health():
    """Railway healthcheck — no auth, always 200 when the app is up."""
    return 'ok', 200


@app.route('/health/db')
def health_db():
    """Database connectivity check (JSON). No auth — for Railway / ops."""
    _init_application_services()
    status = _database_status()
    code = 200 if status.get('connected') or not status.get('configured') else 503
    return json.dumps(status, ensure_ascii=False), code, {'Content-Type': 'application/json'}


@app.route('/health/auth')
def health_auth():
    """Auth diagnostics (JSON). Public payload intentionally excludes PII."""
    status = user_store.auth_status(SUPERADMIN_EMAIL)
    public_status = {
        'user_count': status['user_count'],
        'superadmin_count': status['superadmin_count'],
        'bootstrap_email_configured': status['bootstrap_email_configured'],
        'bootstrap_email_registered': status['bootstrap_email_registered'],
        'password_configured': bool(SUPERADMIN_PASSWORD),
        'storage': 'postgresql' if db_store else 'json',
    }
    return json.dumps(public_status, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
def login():
    error = None
    user_count = len(user_store.list_users())
    bootstrap_email = SUPERADMIN_EMAIL or 'admin@misterwiz.local'

    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        password = request.form.get('password') or ''
        user = user_store.authenticate(email, password)
        if user:
            _record_successful_login(user)
            _login_session(user)
            return redirect(url_for('dashboard'))
        if user_count == 0:
            error = (
                'Nenhuma conta foi criada no servidor. Defina SUPERADMIN_EMAIL e '
                'SUPERADMIN_PASSWORD nas variáveis de ambiente (Railway) e reinicie o app.'
            )
        elif not SUPERADMIN_PASSWORD and user_store.get_by_email(email) is None:
            error = (
                f'E-mail ou senha incorretos. Primeiro acesso do administrador: use '
                f'{bootstrap_email} e a senha definida em SUPERADMIN_PASSWORD.'
            )
        else:
            known = user_store.get_by_email(email)
            if known and not known.get('active', True):
                error = 'Esta conta está desativada. Peça a um administrador para reativá-la.'
            elif known:
                error = 'Senha incorreta para este e-mail.'
            else:
                error = (
                    f'E-mail não cadastrado. Administrador: use {bootstrap_email}. '
                    f'Professores: use o e-mail criado em Usuários.'
                )
    return render_template(
        'login.html',
        error=error,
        bootstrap_email=bootstrap_email,
        accounts_configured=user_count > 0,
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/review-month', methods=['POST'])
@login_required
def set_review_month():
    month = (request.form.get('review_month') or '').strip()
    if not _set_review_month(month):
        abort(400)
    target = _safe_redirect_target((request.form.get('next') or request.referrer or '').strip())
    if target:
        return redirect(_redirect_with_review_month(target, month))
    return redirect(url_for('dashboard', month=month))


@app.route('/review-semester', methods=['POST'])
@login_required
def set_review_semester():
    semester_id = (request.form.get('review_semester') or '').strip()
    if not _set_review_semester(semester_id):
        abort(400)
    target = _safe_redirect_target((request.form.get('next') or request.referrer or '').strip())
    month = session.get('review_month', '')
    if target:
        return redirect(_redirect_with_review_month(target, month))
    return redirect(url_for('dashboard', month=month, semester=semester_id))


# ── Dashboard ─────────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    user = _current_user()
    all_students, students = _scoped_students()
    _, lessons = _scoped_lessons(all_students)
    reports = filter_reports_for_user(
        sorted(OUT_DIR.glob('*.html')),
        _students_with_report_aliases(all_students),
        user,
    )
    turmas = group_by_turma(students) if students else {}
    individual = [f for f in reports if 'class_diagnostic' not in f.name]
    if db_store:
        data_ready = bool(students) and bool(lessons)
    else:
        lessons_path = DATA_DIR / 'lessons.csv'
        data_ready = (DATA_DIR / 'students.csv').exists() and lessons_path.exists()
    db_status = _database_status()
    generate_error = session.pop('generate_error', None)
    generate_error_detail = session.pop('generate_error_detail', None)
    generate_error_skippable = session.pop('generate_error_skippable', None)
    generate_warning = session.pop('generate_warning', None)
    turma_flash_ok = session.pop('turma_flash_ok', None)
    turma_flash_error = session.pop('turma_flash_error', None)
    review_month = _get_review_month(lessons)
    review_semester = _get_review_semester(lessons)
    semester_lessons = filter_lessons_by_semester(lessons, review_semester)
    month_lessons = filter_lessons_by_month(semester_lessons, review_month)

    teacher_classes = []
    admin_turmas = []
    turma_list = list(turmas.keys())
    turma_count = len(turmas)
    if user and user['role'] == ROLE_TEACHER:
        imported = _sync_teacher_registry(user, all_students, semester_id=review_semester)
        if imported and not session.get('legacy_turma_sync_notified'):
            session['legacy_turma_sync_notified'] = True
            if not turma_flash_ok:
                turma_flash_ok = (
                    f'Importamos {imported} turma(s) dos seus alunos já cadastrados. '
                    'Revise dias e horário abaixo se precisar; você pode seguir usando '
                    'Alunos e Relatórios normalmente.'
                )
        teacher_classes = _teacher_class_options(
            user.get('teacher_name', ''), all_semesters=True,
        )
        turma_list = [c['turma'] for c in teacher_classes]
        turma_count = len(teacher_classes)
    elif user and has_full_data_access(user['role']):
        admin_turmas = _admin_dashboard_turmas(
            all_students, semester_id=review_semester,
        )
        turma_list = [c['turma'] for c in admin_turmas]
        turma_count = len(admin_turmas)

    return render_template('dashboard.html',
        student_count=len(students),
        turma_count=turma_count,
        lesson_count=len(month_lessons),
        report_count=len(individual),
        turmas=turma_list,
        teacher_classes=teacher_classes,
        admin_turmas=admin_turmas,
        is_teacher=user and user['role'] == ROLE_TEACHER,
        weekday_choices=WEEKDAY_CHOICES,
        data_ready=data_ready,
        db_status=db_status,
        available_months=_available_review_months(lessons),
        default_month=review_month,
        review_month=review_month,
        review_semester=review_semester,
        generate_error=generate_error,
        generate_error_detail=generate_error_detail,
        generate_error_skippable=generate_error_skippable,
        generate_warning=generate_warning,
        turma_flash_ok=turma_flash_ok,
        turma_flash_error=turma_flash_error,
        teacher_names=_teacher_names_for_forms() if user and has_full_data_access(user['role']) else [],
    )


def _may_manage_turmas(user):
    if not user:
        return False
    return user['role'] == ROLE_TEACHER or has_full_data_access(user['role'])


def _turma_owner_teacher(user):
    if not user:
        return ''
    if user['role'] == ROLE_TEACHER:
        return user.get('teacher_name', '')
    return (request.values.get('teacher') or request.form.get('teacher') or '').strip()


def _teacher_names_for_forms():
    names = set(list_teachers_from_students(_load_students()))
    for entry in user_store.list_users():
        name = normalize_teacher_name(entry.get('teacher_name', ''))
        if name:
            names.add(name)
    registry = _load_teacher_class_registry()
    for key in registry:
        name = normalize_teacher_name(key)
        if name:
            names.add(name)
    return sorted(names, key=str.casefold)


def _resolve_turma_class(registry, teacher_name, turma_code, students=None, semester_id=None):
    """Find turma in registry or build a legacy row from student data."""
    found = find_class(registry, teacher_name, turma_code, semester_id=semester_id)
    if found:
        return found
    owner = normalize_teacher_name(teacher_name)
    code = (turma_code or '').strip()
    if not owner or not code:
        return None
    if students is None:
        students = _load_students()
    owner_key = owner.casefold()
    rows = [
        row for row in students
        if normalize_teacher_name(row.get('teacher', '')).casefold() == owner_key
        and (row.get('turma') or '').strip() == code
    ]
    if not rows:
        return None
    horario = ''
    for row in rows:
        candidate = (row.get('horario') or '').strip()
        if candidate:
            horario = candidate
            break
    time_start, time_end = parse_time_range_from_horario(horario)
    return {
        'turma': code,
        'turma_display': class_display_from_student_rows(rows, code),
        'class_weekdays': [],
        'class_time_start': time_start,
        'class_time_end': time_end,
        'horario': horario,
        'semester_id': (semester_id or '').strip() or _get_review_semester(),
        'legacy_import': True,
    }


def _turma_edit_template_context(
    *,
    turma_code,
    teacher_name,
    edit_class,
    form_error=None,
):
    return dict(
        turma_code=turma_code,
        teacher_name=teacher_name,
        edit_class=edit_class,
        weekday_choices=WEEKDAY_CHOICES,
        form_error=form_error,
        student_count=count_students_in_turma(
            _load_students(), teacher_name, turma_code,
        ),
    )


@app.route('/turmas/create', methods=['POST'])
@login_required
def turma_create():
    user = _current_user()
    if not _may_manage_turmas(user):
        abort(403)

    owner = _turma_owner_teacher(user)
    if not owner:
        session['turma_flash_error'] = 'Selecione o professor responsável pela turma.'
        return redirect(url_for('dashboard'))

    turma_display = (request.form.get('turma_display') or '').strip()
    class_weekdays = [
        request.form.get('class_weekday_1') or '',
        request.form.get('class_weekday_2') or '',
    ]
    class_time_start = time_from_form(request.form, field='turma_time_start')
    class_time_end = time_from_form(request.form, field='turma_time_end')

    data = _load_teacher_class_registry()
    row, err = register_teacher_class(
        data,
        owner,
        turma_display=turma_display,
        class_weekdays=class_weekdays,
        class_time_start=class_time_start,
        class_time_end=class_time_end,
        semester_id=_get_review_semester(),
    )
    if err:
        session['turma_flash_error'] = err
    else:
        if not _save_teacher_class_registry(data):
            session['turma_flash_error'] = (
                session.get('save_conflict')
                or 'Não foi possível salvar a turma (dados desatualizados). Recarregue e tente de novo.'
            )
            return redirect(url_for('dashboard'))
        if row.get('semester_id'):
            _set_review_semester(row['semester_id'])
        schedule = row.get('horario') or row['turma_display']
        semester = row.get('semester_id') or ''
        semester_bit = f' · {semester_label(semester)}' if semester else ''
        session['turma_flash_ok'] = (
            f'Turma "{row["turma_display"]}" criada ({schedule}){semester_bit}. '
            'Agora você pode cadastrar alunos em + Novo aluno.'
        )
    return redirect(url_for('dashboard'))


@app.route('/turmas/delete', methods=['POST'])
@login_required
def turma_delete():
    user = _current_user()
    if not _may_manage_turmas(user):
        abort(403)

    owner = _turma_owner_teacher(user)
    turma = (request.form.get('turma') or '').strip()
    if not owner:
        session['turma_flash_error'] = 'Professor não identificado.'
        return redirect(url_for('dashboard'))

    all_students = _load_students()
    data = _load_teacher_class_registry()
    semester_id = (
        (request.form.get('semester_id') or '').strip()
        or _get_review_semester()
    )
    display = turma
    found = find_class(data, owner, turma, semester_id=semester_id)
    if found:
        display = found.get('turma_display') or turma
        semester_id = found.get('semester_id') or semester_id

    ok, err = delete_teacher_class(
        data,
        owner,
        turma,
        students=all_students,
        semester_id=semester_id,
    )
    if err:
        session['turma_flash_error'] = err
    elif ok:
        if not _save_teacher_class_registry(data):
            session['turma_flash_error'] = (
                session.get('save_conflict')
                or 'Não foi possível excluir a turma (dados desatualizados). Recarregue e tente de novo.'
            )
            return redirect(url_for('dashboard'))
        session['turma_flash_ok'] = f'Turma "{display}" excluída.'
    return redirect(url_for('dashboard'))


def _propagate_turma_to_students(all_students, teacher_name, turma_code, class_row):
    """Keep student rows aligned when a turma's name or schedule changes."""
    teacher_key = normalize_teacher_name(teacher_name).casefold()
    code = (turma_code or '').strip()
    display = (class_row.get('turma_display') or '').strip()
    horario = (class_row.get('horario') or '').strip()
    updated = 0
    for row in all_students:
        if normalize_teacher_name(row.get('teacher', '')).casefold() != teacher_key:
            continue
        if (row.get('turma') or '').strip() != code:
            continue
        row['turma_display'] = display
        row['horario'] = horario
        updated += 1
    return updated


@app.route('/turmas/<turma>/edit', methods=['GET', 'POST'])
@login_required
def turma_edit(turma):
    user = _current_user()
    if not _may_manage_turmas(user):
        abort(403)

    owner = _turma_owner_teacher(user)
    code = (turma or '').strip()
    if not owner:
        session['turma_flash_error'] = 'Informe o professor da turma.'
        return redirect(url_for('dashboard'))

    data = _load_teacher_class_registry()
    semester_id = (
        (request.values.get('semester_id') or '').strip()
        or _get_review_semester()
    )
    existing = _resolve_turma_class(data, owner, code, semester_id=semester_id)
    if not existing:
        session['turma_flash_error'] = 'Turma não encontrada.'
        return redirect(url_for('dashboard'))
    semester_id = existing.get('semester_id') or semester_id

    if request.method == 'POST':
        class_weekdays = [
            request.form.get('class_weekday_1') or '',
            request.form.get('class_weekday_2') or '',
        ]
        display = (request.form.get('turma_display') or '').strip()
        time_start = time_from_form(request.form, field='turma_time_start')
        time_end = time_from_form(request.form, field='turma_time_end')
        if find_class(data, owner, code, semester_id=semester_id):
            row, err = update_teacher_class(
                data,
                owner,
                code,
                turma_display=display,
                class_weekdays=class_weekdays,
                class_time_start=time_start,
                class_time_end=time_end,
                semester_id=semester_id,
            )
        else:
            row, err = register_teacher_class(
                data,
                owner,
                turma_display=display,
                class_weekdays=class_weekdays,
                class_time_start=time_start,
                class_time_end=time_end,
                turma=code,
                semester_id=semester_id,
            )
        if err:
            return render_template(
                'turma_edit.html',
                **_turma_edit_template_context(
                    turma_code=code,
                    teacher_name=owner,
                    edit_class={
                        **existing,
                        'turma_display': (request.form.get('turma_display') or '').strip(),
                        'class_weekdays': class_weekdays,
                        'class_time_start': time_from_form(request.form, field='turma_time_start'),
                        'class_time_end': time_from_form(request.form, field='turma_time_end'),
                        'semester_id': semester_id,
                    },
                    form_error=err,
                ),
            )
        if not _save_teacher_class_registry(data):
            return render_template(
                'turma_edit.html',
                **_turma_edit_template_context(
                    turma_code=code,
                    teacher_name=owner,
                    edit_class={
                        **existing,
                        'turma_display': display,
                        'class_weekdays': class_weekdays,
                        'class_time_start': time_start,
                        'class_time_end': time_end,
                        'semester_id': semester_id,
                    },
                    form_error=(
                        session.pop('save_conflict', None)
                        or 'Dados desatualizados. Recarregue a página e salve novamente.'
                    ),
                ),
            )
        all_students = _load_students()
        linked = _propagate_turma_to_students(
            all_students, owner, code, row,
        )
        if linked:
            _save_students(all_students)
        schedule = row.get('horario') or row['turma_display']
        msg = f'Turma "{row["turma_display"]}" atualizada ({schedule}).'
        if linked:
            msg += f' {linked} aluno(s) atualizado(s) com o novo horário.'
        session['turma_flash_ok'] = msg
        return redirect(url_for('dashboard'))

    return render_template(
        'turma_edit.html',
        **_turma_edit_template_context(
            turma_code=code,
            teacher_name=owner,
            edit_class=existing,
        ),
    )


def _class_name_from_student_row(row):
    """Use turma_display only when it is a real class name, not the student's livro/nível."""
    code = (row.get('turma') or '').strip()
    display = (row.get('turma_display') or '').strip()
    nivel = (row.get('nivel') or '').strip()
    if not display or display == nivel or is_valid_nivel(display):
        return code
    return display


def _turma_display_map(rows, user):
    """Map turma code → class name (dashboard registry first; never the student's nível)."""
    labels = {}
    registry = _load_teacher_class_registry()
    if user and user['role'] == ROLE_TEACHER:
        for entry in _teacher_class_options(user.get('teacher_name', ''), all_semesters=True):
            labels[entry['turma']] = entry['turma_display']
    elif user and has_full_data_access(user['role']):
        for teacher_name in registry:
            for entry in list_for_teacher(registry, teacher_name):
                labels[entry['turma']] = entry['turma_display']
    for row in rows:
        code = (row.get('turma') or '').strip()
        if not code or code in labels:
            continue
        if user and has_full_data_access(user['role']):
            labels[code] = _class_name_from_student_row(row)
        else:
            labels[code] = code
    return labels


def _registry_turma_codes():
    """Turma codes from every teacher, every semester."""
    registry = _load_teacher_class_registry()
    codes = set()
    for teacher_name in registry:
        codes |= turma_codes_for_teacher(registry, teacher_name, semester_id=None)
    return {code for code in codes if code}


def _admin_dashboard_turmas(all_students, semester_id=None):
    """Active classes for superadmin/admin dashboard (registry + student rows).

    Teachers already see every semester; admins must too, otherwise a class like
    Spark from 1º semestre disappears when the review month is in 2º semestre.
    """
    sid = semester_id or _get_review_semester()
    by_key = {}

    def _upsert(code, *, display='', horario='', teacher='', semester=''):
        code = (code or '').strip()
        if not code:
            return
        teacher = (teacher or '').strip()
        key = (normalize_teacher_name(teacher).casefold(), code.casefold())
        row = by_key.setdefault(key, {
            'turma': code,
            'turma_display': code.replace('_', ' '),
            'horario': '',
            'teacher': teacher,
            'semester_id': semester or sid,
        })
        if display:
            row['turma_display'] = display
        if horario:
            row['horario'] = horario
        if teacher:
            row['teacher'] = teacher
        if semester:
            row['semester_id'] = semester

    registry = _load_teacher_class_registry()
    for teacher_name in registry:
        owner = normalize_teacher_name(teacher_name) or teacher_name
        for entry in dedupe_class_options(
            list_for_teacher(registry, teacher_name, semester_id=None),
            prefer_semester=sid,
        ):
            _upsert(
                entry['turma'],
                display=entry.get('turma_display') or entry['turma'],
                horario=entry.get('horario') or '',
                teacher=owner,
                semester=entry.get('semester_id') or sid,
            )

    for student in all_students:
        code = (student.get('turma') or '').strip()
        if not code:
            continue
        teacher = (student.get('teacher') or '').strip()
        key = (normalize_teacher_name(teacher).casefold(), code.casefold())
        if key in by_key:
            row = by_key[key]
            if not row['horario'] and (student.get('horario') or '').strip():
                row['horario'] = student.get('horario', '').strip()
            if not row['teacher'] and teacher:
                row['teacher'] = teacher
            continue
        _upsert(
            code,
            display=_class_name_from_student_row(student),
            horario=(student.get('horario') or '').strip(),
            teacher=teacher,
            semester=sid,
        )

    return sorted(
        by_key.values(),
        key=lambda row: (
            row['turma_display'].casefold(),
            row['teacher'].casefold(),
            row['turma'],
        ),
    )


def _turma_filters(rows, user):
    """Filter chips: Todos + one per turma, label = class name."""
    codes = {r.get('turma', '').strip() for r in rows if r.get('turma', '').strip()}
    if user and user['role'] == ROLE_TEACHER:
        codes |= _teacher_registered_turmas(user.get('teacher_name', ''), all_semesters=True)
    elif user and has_full_data_access(user['role']):
        codes |= _registry_turma_codes()
    codes = sorted(codes)
    labels = _turma_display_map(rows, user)
    return [{'code': code, 'label': labels.get(code, code)} for code in codes]


def _lesson_turma_filters(all_students, lesson_rows, user):
    """Turma filters on Aulas: dashboard registry + existing lessons (even with zero aulas)."""
    if user and user['role'] == ROLE_TEACHER:
        _sync_teacher_registry(user, all_students)
    codes = {r.get('turma', '').strip() for r in lesson_rows if r.get('turma', '').strip()}
    if user:
        codes |= set(_allowed_turmas(all_students, user))
    labels = _turma_display_map(all_students, user)
    return [
        {'code': code, 'label': labels.get(code, code)}
        for code in sorted(codes) if code
    ]


# ── Students ──────────────────────────────────────────────────────────────────────────

@app.route('/students')
@login_required
def students():
    user = _current_user()
    all_students, rows = _scoped_students()
    if user and user['role'] == ROLE_TEACHER:
        _sync_teacher_registry(user, all_students)
    _reconcile_flagged_extra_sessions(rows)
    turma_labels = _turma_display_map(rows, user)
    turma_filters = _turma_filters(rows, user)
    return render_template(
        'students.html',
        students=rows,
        turma_filters=turma_filters,
        turma_labels=turma_labels,
        student_flash_ok=session.pop('student_flash_ok', None),
        student_flash_error=session.pop('student_flash_error', None),
    )


@app.route('/students/<int:idx>/edit', methods=['GET', 'POST'])
@login_required
def student_edit(idx):
    all_rows, visible = _scoped_students()
    if idx < 0 or idx >= len(visible):
        abort(404)
    user = _current_user()
    if user and user['role'] == ROLE_TEACHER:
        _sync_teacher_registry(user, all_rows)
    if request.method == 'POST':
        updated = _student_row_from_form()
        if user['role'] == ROLE_TEACHER:
            updated['teacher'] = user.get('teacher_name') or updated.get('teacher', '')
        form_error = _validate_student_row(updated, all_rows, user)
        if form_error:
            return render_template(
                'student_edit.html',
                **_student_form_context(
                    all_rows, user, False, updated, idx, form_error=form_error,
                ),
            )
        if _student_identity_conflict(visible[idx], request.form):
            return render_template(
                'student_edit.html',
                **_student_form_context(
                    all_rows, user, False, updated, idx,
                    form_error=STUDENT_LIST_CHANGED_MESSAGE,
                ),
            )
        global_idx = find_student_global_index(all_rows, visible, idx)
        if global_idx is None:
            abort(404)
        review_month = _get_review_month()
        if not _persist_student_save(all_rows, global_idx, updated, review_month):
            return render_template(
                'student_edit.html',
                **_student_form_context(
                    all_rows, user, False, updated, idx,
                    form_error=SAVE_CONFLICT_MESSAGE,
                ),
            )
        _sync_student_aula_extra_sessions(updated)
        return _redirect_students(flash_ok=f'Aluno "{updated.get("student_name", "")}" salvo.')
    return render_template(
        'student_edit.html',
        **_student_form_context(all_rows, user, False, visible[idx], idx),
    )


@app.route('/students/<int:idx>/autosave', methods=['POST'])
@login_required_json
def student_autosave(idx):
    """Save student edits in the background (grades, feedback, observations)."""
    all_rows, visible = _scoped_students()
    if idx < 0 or idx >= len(visible):
        return jsonify({'ok': False, 'error': 'Aluno não encontrado.'}), 404
    user = _current_user()
    if user and user['role'] == ROLE_TEACHER:
        _sync_teacher_registry(user, all_rows)
    updated = _student_row_from_form()
    if user['role'] == ROLE_TEACHER:
        updated['teacher'] = user.get('teacher_name') or updated.get('teacher', '')
    form_error = _validate_student_row(updated, all_rows, user, autosave=True)
    if form_error:
        return jsonify({'ok': False, 'error': form_error}), 400
    if _student_identity_conflict(visible[idx], request.form):
        return jsonify({
            'ok': False,
            'conflict': True,
            'error': STUDENT_LIST_CHANGED_MESSAGE,
        }), 409
    global_idx = find_student_global_index(all_rows, visible, idx)
    if global_idx is None:
        return jsonify({'ok': False, 'error': 'Aluno não encontrado.'}), 404
    review_month = _get_review_month()
    if not _persist_student_save(all_rows, global_idx, updated, review_month):
        return jsonify({
            'ok': False,
            'conflict': True,
            'error': SAVE_CONFLICT_MESSAGE,
        }), 409
    _sync_student_aula_extra_sessions(updated)
    return jsonify({
        'ok': True,
        'saved_at': datetime.now(timezone.utc).strftime('%H:%M'),
        'review_month': review_month,
    })


def _blank_student_defaults(user=None):
    """Empty student row for Novo aluno — never copy another kid's review."""
    row = {field: '' for field in STUDENT_FIELDS}
    row.update(DEFAULT_MONTHLY_VALUES)
    if user and user.get('role') == ROLE_TEACHER:
        row['teacher'] = user.get('teacher_name') or ''
    return row


@app.route('/students/new', methods=['GET', 'POST'])
@login_required
def student_new():
    all_rows, _visible = _scoped_students()
    user = _current_user()
    if user and user['role'] == ROLE_TEACHER:
        _sync_teacher_registry(user, all_rows)
    if request.method == 'POST':
        new_row = _student_row_from_form()
        if user['role'] == ROLE_TEACHER:
            new_row['teacher'] = user.get('teacher_name') or new_row.get('teacher', '')
        form_error = _validate_student_row(new_row, all_rows, user)
        if form_error:
            return render_template(
                'student_edit.html',
                **_student_form_context(
                    all_rows, user, True, new_row, None, form_error=form_error,
                ),
            )
        review_month = _get_review_month()
        _persist_student_create(all_rows, new_row, review_month)
        _sync_student_aula_extra_sessions(new_row)
        return _redirect_students(flash_ok=f'Aluno "{new_row.get("student_name", "")}" cadastrado.')
    defaults = _blank_student_defaults(user)
    return render_template(
        'student_edit.html',
        **_student_form_context(all_rows, user, True, defaults, None),
    )


@app.route('/students/<int:idx>/delete', methods=['POST'])
@login_required
def student_delete(idx):
    all_rows, visible = _scoped_students()
    if 0 <= idx < len(visible):
        global_idx = find_student_global_index(all_rows, visible, idx)
        if global_idx is not None:
            removed = all_rows[global_idx]
            remaining = [row for i, row in enumerate(all_rows) if i != global_idx]
            if _save_students(remaining):
                attendance = remove_attendance_for_student(
                    _load_lesson_attendance(),
                    removed.get('turma'),
                    removed.get('student_name'),
                )
                _save_lesson_attendance(attendance)
                review_store = _load_monthly_review_store()
                remove_reviews_for_student(
                    review_store,
                    removed.get('turma'),
                    removed.get('student_name'),
                )
                _save_monthly_review_store(review_store)
                # Snapshots live in a separate store — purge them too so a
                # recreated student with the same name doesn't inherit trends.
                from report_periods import student_snapshot_id as _sid
                snaps = load_snapshots(SNAPSHOTS_PATH)
                old_prefix = (
                    f'{(removed.get("turma") or "").strip()}|'
                    f'{_sid(removed.get("turma"), removed.get("student_name"))}|'
                )
                cleaned = {
                    key: row for key, row in snaps.items()
                    if not key.startswith(old_prefix)
                }
                if len(cleaned) != len(snaps):
                    save_snapshots(SNAPSHOTS_PATH, list(cleaned.values()))
                extras = remove_sessions_for_student(_load_extra_sessions(), removed)
                _save_extra_sessions(extras)
                return _redirect_students(
                    flash_ok=f'Aluno "{removed.get("student_name", "")}" excluído.',
                )
    return _redirect_students()


# ── Lessons (class details) ───────────────────────────────────────────────────────────

@app.route('/lessons')
@login_required
def lessons():
    user = _current_user()
    all_students, _ = _scoped_students()
    _, rows = _scoped_lessons(all_students)
    turma_filters = _lesson_turma_filters(all_students, rows, user)
    rows = [
        {**r, 'habilidades': normalize_habilidades(r.get('habilidades', ''))}
        for r in rows
    ]
    habilidades = sorted({r['habilidades'] for r in rows if r['habilidades']})
    return render_template(
        'lessons.html',
        lessons=rows,
        turma_filters=turma_filters,
        habilidades=habilidades,
        lesson_field_labels=LESSON_FIELD_LABELS,
        turma_labels=_turma_display_map(all_students, user),
    )


@app.route('/lessons/<int:idx>/edit', methods=['GET', 'POST'])
@login_required
def lesson_edit(idx):
    all_students, _ = _scoped_students()
    all_rows, visible = _scoped_lessons(all_students)
    user = _current_user()
    allowed_turmas = _allowed_turmas(all_students, user)

    if idx < 0 or idx >= len(visible):
        abort(404)

    if request.method == 'POST':
        updated = _lesson_from_form()
        if not updated.get('turma') or not updated.get('aula_num'):
            abort(400)
        if not _teacher_may_use_turma(updated['turma'], all_students, user):
            abort(403)
        global_idx = find_lesson_global_index(all_rows, visible, idx)
        if global_idx is None:
            abort(404)
        all_rows[global_idx] = updated
        _save_lessons(_sort_lessons(all_rows))
        _persist_lesson_attendance(updated, _load_roster_students())
        lesson_month = parse_lesson_month(updated.get('date', ''))
        if lesson_month:
            _set_review_month(lesson_month)
        return _redirect_lessons()

    attendance_rows = _load_lesson_attendance()
    lesson_month = parse_lesson_month(visible[idx].get('date', '')) or _get_review_month()
    students_merged = _merged_roster_for_month(all_students, lesson_month)
    ctx = _lesson_edit_context(
        visible[idx], all_rows, students_merged, allowed_turmas, is_new=False,
        attendance_rows=attendance_rows,
    )
    return render_template(
        'lesson_edit.html',
        idx=idx,
        is_new=False,
        allowed_turmas=allowed_turmas,
        lesson_field_labels=LESSON_FIELD_LABELS,
        **ctx,
    )


@app.route('/lessons/new', methods=['GET', 'POST'])
@login_required
def lesson_new():
    all_students, _ = _scoped_students()
    all_rows, visible = _scoped_lessons(all_students)
    user = _current_user()
    allowed_turmas = _allowed_turmas(all_students, user)

    if request.method == 'POST':
        new_row = _lesson_from_form()
        if not new_row.get('turma') or not new_row.get('aula_num'):
            abort(400)
        if not _teacher_may_use_turma(new_row['turma'], all_students, user):
            abort(403)
        all_rows.append(new_row)
        _save_lessons(_sort_lessons(all_rows))
        _persist_lesson_attendance(new_row, _load_roster_students())
        lesson_month = parse_lesson_month(new_row.get('date', ''))
        if lesson_month:
            _set_review_month(lesson_month)
        return _redirect_lessons()

    defaults = {}
    for field in LESSON_FIELDS:
        defaults[field] = ''
    if allowed_turmas:
        wanted = (request.args.get('turma') or '').strip()
        defaults['turma'] = wanted if wanted in allowed_turmas else allowed_turmas[0]
    attendance_rows = _load_lesson_attendance()
    ctx = _lesson_edit_context(
        defaults, all_rows, all_students, allowed_turmas, is_new=True,
        attendance_rows=attendance_rows,
    )
    return render_template(
        'lesson_edit.html',
        idx=None,
        is_new=True,
        allowed_turmas=allowed_turmas,
        lesson_field_labels=LESSON_FIELD_LABELS,
        **ctx,
    )


# ── Extra sessions (reforço / reposição / nivelamento) ───────────────────────────────

@app.route('/extra-sessions')
@login_required
def extra_sessions():
    user = _current_user()
    all_students, visible_students = _scoped_students()
    scope_students = all_students if has_full_data_access(user['role']) else visible_students
    _reconcile_flagged_extra_sessions(scope_students)
    _, rows = _scoped_extra_sessions()
    teachers = sorted({r.get('teacher', '').strip() for r in rows if r.get('teacher', '').strip()})
    types = sorted({r.get('session_type', '').strip() for r in rows if r.get('session_type', '').strip()})
    message = session.pop('extra_session_flash_message', None)
    error = session.pop('extra_session_flash_error', None)
    return render_template(
        'extra_sessions.html',
        sessions=rows,
        teachers=teachers,
        session_types=types or list(SESSION_TYPE_CHOICES),
        field_labels=EXTRA_SESSION_FIELD_LABELS,
        auto_aula_extra_marker=AUTO_AULA_EXTRA_MARKER,
        message=message,
        error=error,
    )


@app.route('/extra-sessions/new', methods=['GET', 'POST'])
@login_required
def extra_session_new():
    all_rows, visible = _scoped_extra_sessions()
    user = _current_user()
    if request.method == 'POST':
        new_row = row_from_form(request.form)
        if user['role'] == ROLE_TEACHER:
            new_row['teacher'] = user.get('teacher_name') or new_row.get('teacher', '')
        if not new_row.get('student_name'):
            abort(400)
        all_rows.append(new_row)
        _save_extra_sessions(all_rows)
        _sync_extra_session_to_student_flag(new_row)
        return redirect(url_for('extra_sessions'))

    defaults = {f: '' for f in EXTRA_SESSION_FIELDS}
    defaults['session_type'] = 'Reforço'
    if user['role'] == ROLE_TEACHER:
        defaults['teacher'] = user.get('teacher_name', '')
    return render_template(
        'extra_session_edit.html',
        session=defaults,
        idx=None,
        is_new=True,
        field_labels=EXTRA_SESSION_FIELD_LABELS,
        session_type_choices=SESSION_TYPE_CHOICES,
        teacher_names=_teacher_names_from_students(),
    )


@app.route('/extra-sessions/<int:idx>/edit', methods=['GET', 'POST'])
@login_required
def extra_session_edit(idx):
    all_rows, visible = _scoped_extra_sessions()
    user = _current_user()
    if idx < 0 or idx >= len(visible):
        abort(404)

    if request.method == 'POST':
        updated = row_from_form(request.form)
        if user['role'] == ROLE_TEACHER:
            updated['teacher'] = user.get('teacher_name') or updated.get('teacher', '')
        if not updated.get('student_name'):
            abort(400)
        global_idx = find_extra_session_global_index(all_rows, visible, idx)
        if global_idx is None:
            abort(404)
        all_rows[global_idx] = updated
        _save_extra_sessions(all_rows)
        _sync_extra_session_to_student_flag(updated)
        return redirect(url_for('extra_sessions'))

    return render_template(
        'extra_session_edit.html',
        session=visible[idx],
        idx=idx,
        is_new=False,
        field_labels=EXTRA_SESSION_FIELD_LABELS,
        session_type_choices=SESSION_TYPE_CHOICES,
        teacher_names=_teacher_names_from_students(),
    )


@app.route('/extra-sessions/<int:idx>/delete', methods=['POST'])
@login_required
def extra_session_delete(idx):
    all_rows, visible = _scoped_extra_sessions()
    if 0 <= idx < len(visible):
        global_idx = find_extra_session_global_index(all_rows, visible, idx)
        if global_idx is not None:
            all_rows.pop(global_idx)
            _save_extra_sessions(all_rows)
    return redirect(url_for('extra_sessions'))


@app.route('/extra-sessions/import', methods=['POST'])
@role_required(ROLE_SUPERADMIN, ROLE_ADMIN)
def extra_sessions_import():
    f = request.files.get('file')
    if not f or not f.filename:
        return redirect(url_for('extra_sessions'))

    text, decode_error = _read_csv_text(f)
    if decode_error:
        session['extra_session_flash_error'] = decode_error
        return redirect(url_for('extra_sessions'))

    rows, errors = parse_import_csv(text)
    if errors:
        session['extra_session_flash_error'] = errors[0]
        return redirect(url_for('extra_sessions'))

    mode = request.form.get('mode', 'merge')
    if mode == 'replace':
        _save_extra_sessions(rows)
        session['extra_session_flash_message'] = f'{len(rows)} atendimento(s) importado(s) (substituiu tudo).'
    else:
        existing = _load_extra_sessions()
        existing.extend(rows)
        _save_extra_sessions(existing)
        session['extra_session_flash_message'] = f'{len(rows)} atendimento(s) adicionado(s).'
    for row in rows:
        _sync_extra_session_to_student_flag(row)
    for row in rows:
        month = parse_lesson_month(row.get('date', ''))
        if month:
            _set_review_month(month)
            break
    return redirect(url_for('extra_sessions'))


@app.route('/extra-sessions/template')
@login_required
def download_atendimentos_template():
    user = _current_user()
    teacher_name = None
    if not has_full_data_access(user['role']):
        teacher_name = normalize_teacher_name(user.get('teacher_name', ''))
    csv_text = build_atendimentos_template_csv(
        TEMPLATE_DIR,
        teacher_name=teacher_name or None,
    )
    data = io.BytesIO(csv_text.encode('utf-8'))
    data.seek(0)
    return send_file(
        data,
        as_attachment=True,
        download_name='atendimentos_template.csv',
        mimetype='text/csv; charset=utf-8',
    )


def _teacher_names_from_students():
    names = {
        normalize_teacher_name(s.get('teacher', ''))
        for s in _load_students()
        if s.get('teacher', '').strip()
    }
    return sorted(n for n in names if n)


@app.route('/lessons/<int:idx>/delete', methods=['POST'])
@login_required
def lesson_delete(idx):
    all_students, _ = _scoped_students()
    all_rows, visible = _scoped_lessons(all_students)
    if 0 <= idx < len(visible):
        global_idx = find_lesson_global_index(all_rows, visible, idx)
        if global_idx is not None:
            removed = all_rows[global_idx]
            remaining = [row for i, row in enumerate(all_rows) if i != global_idx]
            if _save_lessons(remaining):
                attendance = remove_attendance_for_lesson(
                    _load_lesson_attendance(),
                    removed.get('turma'),
                    removed.get('aula_num'),
                )
                _save_lesson_attendance(attendance)
                month_key = parse_lesson_month(removed.get('date', ''))
                if month_key:
                    store = _load_monthly_review_store()
                    merged = merge_roster_for_month(all_students, store, month_key)
                    updated = reconcile_presence_after_lesson_removed(
                        merged,
                        remaining,
                        attendance,
                        month_key,
                        removed.get('turma'),
                        removed.get('aula_num'),
                    )
                    turma_key = (removed.get('turma') or '').strip().upper()
                    for row in updated:
                        if (row.get('turma') or '').strip().upper() != turma_key:
                            continue
                        upsert_monthly_review(store, row, month_key)
                    _save_monthly_review_store(store)
    return _redirect_lessons()


# ── Upload ────────────────────────────────────────────────────────────────────────────

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    user = _current_user()
    messages, errors = _pull_upload_notices()
    if request.method == 'POST':
        scope_errors = _teacher_scope_errors(user)
        if scope_errors:
            errors.extend(scope_errors)
        else:
            students_after_upload = _load_students()
            for key, label in [('students', 'Alunos'), ('lessons', 'Aulas')]:
                f = request.files.get(key)
                if not (f and f.filename):
                    continue

                text, decode_error = _read_csv_text(f)
                if decode_error:
                    errors.append(f'Erro no CSV de {label}: {decode_error}')
                    continue

                rows, convert_note, parse_errors = parse_upload_csv(
                    key,
                    text,
                    user=user,
                    source_filename=f.filename,
                )
                if parse_errors:
                    errors.append(f'Erro no CSV de {label}: {parse_errors[0]}')
                    continue

                validation_errors = _validate_csv(key, _rows_to_csv_text(key, rows))
                if validation_errors:
                    errors.append(f'Erro no CSV de {label}: {validation_errors[0]}')
                    continue

                if not has_full_data_access(user['role']):
                    if key == 'students':
                        validation_errors = _validate_teacher_student_rows(rows, user)
                    else:
                        validation_errors = _validate_teacher_lesson_rows(
                            rows, user, students_after_upload,
                        )
                    if validation_errors:
                        errors.append(f'Erro no CSV de {label}: {validation_errors[0]}')
                        continue

                try:
                    _save_upload_dataset(key, rows, user, students_after_upload)
                    if key == 'students':
                        students_after_upload = _load_students()
                    success = f'{label} carregado: {f.filename}'
                    if convert_note:
                        success = f'{success} ({convert_note})'
                    messages.append(success)
                except OSError as exc:
                    errors.append(f'Erro ao salvar {label}: {exc}')
    return render_template('upload.html', **_upload_page_context(user, messages, errors))


@app.route('/upload/delete/<name>', methods=['POST'])
@login_required
def delete_csv(name):
    user = _current_user()
    removed, full_delete = _delete_csv_dataset(name, user)
    label = 'Alunos' if name == 'students' else 'Aulas'
    if removed == 0:
        _queue_upload_notice(error=f'Nenhum dado de {label.lower()} para remover.')
    elif full_delete:
        _queue_upload_notice(message=f'{label}: arquivo CSV removido ({removed} registro(s)).')
    else:
        _queue_upload_notice(
            message=f'{label}: {removed} registro(s) do seu perfil removido(s).')
    return redirect(url_for('upload'))


@app.route('/upload/template/<name>')
@login_required
def download_template(name):
    if name not in ('students', 'lessons'):
        abort(404)

    user = _current_user()
    if has_full_data_access(user['role']):
        buf = _build_template_csv(name)
    else:
        scope_errors = _teacher_scope_errors(user)
        if scope_errors:
            abort(403)
        fields = STUDENT_FIELDS if name == 'students' else LESSON_FIELDS
        rows = _teacher_template_rows(name, user)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
        buf = '\ufeff' + buf.getvalue()
    data = io.BytesIO(buf.encode('utf-8'))
    data.seek(0)
    return send_file(
        data,
        as_attachment=True,
        download_name=f'{name}_template.csv',
        mimetype='text/csv; charset=utf-8',
    )


@app.route('/upload/download/<name>')
@login_required
def download_csv(name):
    if name not in ('students', 'lessons'):
        abort(404)
    user = _current_user()
    if db_store:
        all_students = _load_students()
        if name == 'students':
            rows = (
                _load_students()
                if has_full_data_access(user['role'])
                else filter_students_for_user(all_students, user)
            )
        else:
            all_lessons = _load_lessons()
            rows = (
                all_lessons
                if has_full_data_access(user['role'])
                else filter_lessons_for_user(all_lessons, all_students, user)
            )
        if not rows:
            abort(404)
        fields = STUDENT_FIELDS if name == 'students' else LESSON_FIELDS
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
        data = io.BytesIO(buf.getvalue().encode('utf-8'))
        data.seek(0)
        return send_file(data, as_attachment=True, download_name=f'{name}.csv', mimetype='text/csv')

    path = DATA_DIR / f'{name}.csv'
    if not path.exists():
        abort(404)
    if has_full_data_access(user['role']):
        return send_file(path, as_attachment=True, download_name=f'{name}.csv')

    all_students = load_csv(DATA_DIR / 'students.csv') if (DATA_DIR / 'students.csv').exists() else []
    all_lessons = load_csv(path) if name == 'lessons' else []
    if name == 'students':
        rows = filter_students_for_user(all_students, user)
    else:
        rows = filter_lessons_for_user(all_lessons, all_students, user)
    if not rows:
        abort(404)
    fields = STUDENT_FIELDS if name == 'students' else LESSON_FIELDS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    data = io.BytesIO(buf.getvalue().encode('utf-8'))
    data.seek(0)
    return send_file(data, as_attachment=True, download_name=f'{name}.csv', mimetype='text/csv')


# ── Generate & Reports ────────────────────────────────────────────────────────────────

def _report_month_from_request(lessons):
    raw = (request.form.get('report_month') or '').strip()
    if raw and raw in _available_review_months(lessons):
        return raw
    return _get_review_month(lessons)


def _turma_display_names_for_codes(students, codes):
    """Map turma codes to their human-facing display names (best effort)."""
    names = []
    for code in codes:
        rows = [s for s in students if (s.get('turma') or '').strip() == code]
        names.append(class_display_from_student_rows(rows, code) if rows else code)
    return names


def _validate_generation_inputs(students, lessons, *, skip_incomplete=False):
    """Return a user-facing error message, or None when generation can proceed.

    When skip_incomplete is True, turmas with no lessons for the reviewed
    month are not treated as a blocking error (the caller filters them out
    of `students` before generating instead of failing the whole batch).
    """
    if not students:
        return 'Nenhum aluno encontrado para o seu perfil. Cadastre alunos antes de gerar relatórios.'
    if not lessons:
        return (
            'Nenhuma aula encontrada para as turmas selecionadas. '
            'Cadastre o CSV de aulas (ou confira se as turmas dos alunos batem com as aulas).'
        )
    missing = []
    for idx, student in enumerate(students, start=1):
        if not (student.get('turma') or '').strip():
            missing.append(f'linha {idx}: turma em branco')
        if not (student.get('student_name') or '').strip():
            missing.append(f'linha {idx}: nome do aluno em branco')
        if len(missing) >= 3:
            break
    if missing:
        return 'Dados de alunos incompletos: ' + '; '.join(missing) + '.'
    if skip_incomplete:
        return None
    turmas_sem_aula = turmas_without_lessons(students, lessons)
    if turmas_sem_aula:
        names = _turma_display_names_for_codes(students, turmas_sem_aula)
        return (
            'Nenhuma aula cadastrada para a(s) turma(s): '
            + ', '.join(names)
            + '. Sem aulas, a presença sairia como 100% para todos os alunos. '
            'Cadastre as aulas dessas turmas antes de gerar, ou gere pulando essas turmas.'
        )
    return None


def _run_report_generation(students, lessons, report_month):
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = OUT_DIR / '.mw_write_probe'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(f'Cannot write reports to {OUT_DIR}: {exc}') from exc

    snapshots = load_snapshots(SNAPSHOTS_PATH)
    attendance_rows = _load_lesson_attendance()
    env = create_report_environment(TMPL_DIR)
    generate_individual_reports(
        students, lessons, env, OUT_DIR,
        report_month=report_month, snapshots=snapshots,
        attendance_rows=attendance_rows,
    )
    generate_class_diagnostics(
        students, lessons, env, OUT_DIR,
        report_month=report_month, snapshots=snapshots,
    )
    try:
        upsert_month_snapshots(
            SNAPSHOTS_PATH, report_month, students, lessons, build_student_ctx,
        )
    except OSError as exc:
        logger.warning('Could not save monthly snapshots to %s: %s', SNAPSHOTS_PATH, exc)


def _turma_from_diagnostic_filename(filename):
    """Extract turma code from a class diagnostic report filename."""
    name = Path(filename).name
    match = re.match(r'^(.+)_(\d{4}-\d{2})_class_diagnostic\.html$', name)
    if match:
        return match.group(1)
    if name.endswith('_class_diagnostic.html'):
        return name[: -len('_class_diagnostic.html')]
    return ''


def _students_with_report_aliases(students):
    """Students plus past identities from the transfer log, for report matching.

    Keeps historical HTML reports (named with the old turma) visible and
    correctly attributed after a student is promoted to another turma.
    """
    return students_with_transfer_aliases(
        students, load_transfer_log(_student_transfers_path()),
    )


def _build_report_rows(individual, diagnostics, students, lessons, snapshots):
    """Unified report list entries for the Relatórios page (newest month first)."""
    rows = []
    for path in individual:
        month = report_month_from_filename(path.name) or ''
        turma, student_name = _student_for_report_file(path, students, month)
        trend = _trend_for_report_file(path, month, students, lessons, snapshots) if month else None
        display = student_name or path.stem.replace('_', ' ')
        rows.append({
            'filename': path.name,
            'kind': 'individual',
            'display': display,
            'turma': turma or '',
            'month': month,
            'trend': trend,
        })
    for path in diagnostics:
        month = report_month_from_filename(path.name) or ''
        turma = _turma_from_diagnostic_filename(path.name)
        rows.append({
            'filename': path.name,
            'kind': 'diagnostic',
            'display': 'Diagnóstico de turma',
            'turma': turma,
            'month': month,
            'trend': None,
        })
    rows.sort(
        key=lambda row: (row['month'] or '', row['kind'], row['display'].casefold()),
        reverse=True,
    )
    return rows


def _report_turma_filters(report_rows, students, user):
    codes = sorted({row['turma'].strip() for row in report_rows if row.get('turma', '').strip()})
    labels = _turma_display_map(students, user)
    return [{'code': code, 'label': labels.get(code, code)} for code in codes]


def _student_for_report_file(path, students, month):
    for student in students:
        turma = student.get('turma', '').strip()
        name = student.get('student_name', '').strip()
        if not turma or not name:
            continue
        if individual_report_filename(turma, name, month) == path.name:
            return turma, name
        if not month and individual_report_filename(turma, name) == path.name:
            return turma, name
    return None, None


def _trend_for_report_file(path, month, students, lessons, snapshots):
    """Trend badge for the reports list — prefer snapshots (fast), no full re-render."""
    if not month:
        return None
    turma, name = _student_for_report_file(path, students, month)
    if not turma or not name:
        return None
    from report_periods import _snapshot_key, student_snapshot_id

    snap_key = _snapshot_key(turma, student_snapshot_id(turma, name), month)
    row = snapshots.get(snap_key)
    if row and row.get('composite_score') is not None:
        composite = int(row['composite_score'])
    else:
        student = None
        for row in students:
            if row.get('_transfer_alias'):
                continue
            if row.get('turma', '').strip() == turma and row.get('student_name', '').strip() == name:
                student = row
                break
        if not student:
            return None
        try:
            ctx = build_student_ctx(student, lessons, report_month=month)
            composite = student_composite_score(ctx)
        except (ValueError, KeyError, TypeError):
            return None
    try:
        return compute_month_trend(composite, month, snapshots, turma, name)
    except (TypeError, ValueError, KeyError):
        return None


@app.route('/generate', methods=['POST'])
@login_required
def generate():
    all_students, _ = _scoped_students(merge=False)
    _, lessons = _scoped_lessons(all_students)
    user = _current_user()
    report_month = _report_month_from_request(lessons)
    _, students = _scoped_students(review_month=report_month, apply_attendance=True)
    skip_incomplete = request.form.get('skip_incomplete') == '1'

    if has_full_data_access(user['role']) and not db_store:
        students_file = DATA_DIR / 'students.csv'
        lessons_file = DATA_DIR / 'lessons.csv'
        missing = []
        if not students_file.exists():
            missing.append('students.csv não encontrado.')
        if not lessons_file.exists():
            missing.append('lessons.csv não encontrado.')
        if missing:
            return render_template(
                'upload.html',
                **_upload_page_context(user, errors=missing),
            )
        students_text = students_file.read_text(encoding='utf-8')
        lessons_text = lessons_file.read_text(encoding='utf-8')
        students_errors = _validate_csv('students', students_text)
        lessons_errors = _validate_csv('lessons', lessons_text)
        if students_errors or lessons_errors:
            errors = []
            if students_errors:
                errors.append(f'CSV de Alunos invalido: {students_errors[0]}')
            if lessons_errors:
                errors.append(f'CSV de Aulas invalido: {lessons_errors[0]}')
            return render_template(
                'upload.html',
                **_upload_page_context(user, errors=errors),
            )

    skipped_names = []
    if skip_incomplete:
        turmas_sem_aula = turmas_without_lessons(students, lessons)
        if turmas_sem_aula:
            skipped_names = _turma_display_names_for_codes(students, turmas_sem_aula)
            students = [
                s for s in students
                if (s.get('turma') or '').strip() not in turmas_sem_aula
            ]
            if not students:
                session['generate_error'] = (
                    'Nenhuma turma tem aulas cadastradas para este mês ainda. '
                    'Cadastre as aulas antes de gerar relatórios.'
                )
                return redirect(url_for('dashboard'))

    input_error = _validate_generation_inputs(students, lessons, skip_incomplete=skip_incomplete)
    if input_error:
        session['generate_error'] = input_error
        if not skip_incomplete and turmas_without_lessons(students, lessons):
            session['generate_error_skippable'] = True
        return redirect(url_for('dashboard'))

    try:
        _run_report_generation(students, lessons, report_month)
    except Exception as exc:
        logger.exception('Report generation failed for month %s: %s', report_month, exc)
        session['generate_error'] = (
            'Não foi possível gerar os relatórios. Verifique os dados de alunos e aulas '
            'e tente novamente. Se o problema continuar, contate o suporte.'
        )
        if has_full_data_access(user.get('role', '')):
            session['generate_error_detail'] = str(exc)[:300]
        return redirect(url_for('dashboard'))

    if skipped_names:
        session['generate_warning'] = (
            'Relatórios gerados. Turma(s) sem aulas cadastradas para este mês foram '
            'puladas: ' + ', '.join(skipped_names) + '.'
        )

    return redirect(url_for('reports', month=report_month))


@app.route('/admin/teachers', methods=['GET', 'POST'])
@role_required(ROLE_SUPERADMIN, ROLE_ADMIN)
def manage_teachers():
    messages = []
    errors = []
    actor = _current_user()

    if request.method == 'POST':
        action = request.form.get('action', '')
        try:
            if action == 'create_teacher':
                user_store.create_teacher(
                    request.form.get('email', ''),
                    request.form.get('password', ''),
                    request.form.get('teacher_name', ''),
                )
                messages.append('Conta de professor criada.')
            elif action == 'create_admin' and actor['role'] == ROLE_SUPERADMIN:
                user_store.create_admin(
                    request.form.get('email', ''),
                    request.form.get('password', ''),
                    request.form.get('role', ROLE_ADMIN),
                )
                messages.append('Conta administrativa criada.')
            elif action == 'update':
                user_id = int(request.form.get('user_id', '0'))
                password = request.form.get('password', '').strip()
                user_store.update_user(
                    user_id,
                    email=request.form.get('email'),
                    password=password or None,
                    teacher_name=request.form.get('teacher_name'),
                    active=request.form.get('active') == '1',
                )
                messages.append('Usuário atualizado.')
            elif action == 'delete':
                user_id = int(request.form.get('user_id', '0'))
                user_store.delete_user(user_id, actor_id=actor['id'])
                messages.append('Usuário removido.')
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))

    users = [user_public_dict(u) for u in user_store.list_users()]
    profiles = _load_teacher_profiles()
    login_events = _load_login_events()
    last_by_user = last_login_by_user_id(login_events)
    default_subject = 'Mister Wiz — contato da plataforma'
    enriched_users = []
    for u in users:
        profile = get_or_empty(profiles, u['id'])
        last_event = last_by_user.get(u['id'])
        email_to = contact_email_for_user(u, profile)
        wa_raw = (profile.get('whatsapp') or '').strip()
        enriched_users.append({
            **u,
            'last_login_at': last_event['logged_at'] if last_event else '',
            'last_login_label': format_logged_at(
                last_event['logged_at'] if last_event else '',
            ),
            'contact_email': email_to,
            'mailto_href': mailto_href(email_to, subject=default_subject),
            'whatsapp': wa_raw,
            'whatsapp_digits': whatsapp_digits(wa_raw),
            'whatsapp_href': whatsapp_href(wa_raw),
        })
    login_history = []
    for event in events_newest_first(login_events, limit=100):
        login_history.append({
            **event,
            'logged_at_label': format_logged_at(event.get('logged_at', '')),
            'role_label': ROLE_LABELS.get(event.get('role'), event.get('role') or ''),
        })
    teacher_names = sorted({
        normalize_teacher_name(s.get('teacher', ''))
        for s in _load_students()
        if s.get('teacher', '').strip()
    })
    return render_template(
        'teachers.html',
        users=enriched_users,
        login_history=login_history,
        teacher_names=teacher_names,
        messages=messages,
        errors=errors,
        roles_manageable=[ROLE_ADMIN, ROLE_TEACHER] if actor['role'] == ROLE_SUPERADMIN else [ROLE_TEACHER],
    )


@app.route('/admin/turmas/transfer', methods=['GET', 'POST'])
@role_required(ROLE_SUPERADMIN, ROLE_ADMIN)
def turma_transfer():
    students = _load_students()
    lessons = _load_lessons()
    extra_sessions = _load_extra_sessions()
    registry = _load_teacher_class_registry()

    teacher_names = list_teachers_from_students(students)
    for entry in user_store.list_users():
        name = normalize_teacher_name(entry.get('teacher_name', ''))
        if name and name not in teacher_names:
            teacher_names.append(name)
    teacher_names = sorted(set(teacher_names), key=str.casefold)

    from_teacher = (request.values.get('from_teacher') or '').strip()
    to_teacher = (request.values.get('to_teacher') or '').strip()
    turma = (request.values.get('turma') or '').strip()

    turma_options = turmas_for_teacher(registry, students, from_teacher) if from_teacher else []
    preview = None
    messages = []
    errors = []

    if from_teacher and to_teacher and turma:
        preview, preview_err = preview_transfer(
            students, lessons, extra_sessions, registry,
            from_teacher, to_teacher, turma,
        )
        if preview_err:
            errors.append(preview_err)

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        if action == 'export':
            payload, filename, export_err = build_transfer_export_zip(
                students, lessons, registry, from_teacher, to_teacher, turma,
            )
            if export_err:
                errors.append(export_err)
            else:
                data = io.BytesIO(payload)
                data.seek(0)
                return send_file(
                    data,
                    as_attachment=True,
                    download_name=filename,
                    mimetype='application/zip',
                )
        elif action == 'transfer':
            if not preview:
                errors.append(errors[-1] if errors else 'Não foi possível validar a transferência.')
            else:
                summary, transfer_err = apply_transfer(
                    students, lessons, extra_sessions, registry,
                    from_teacher, to_teacher, turma,
                )
                if transfer_err:
                    errors.append(transfer_err)
                else:
                    if not _save_students(students):
                        errors.append(
                            'Outro usuário alterou os alunos ao mesmo tempo. '
                            'Recarregue a página e tente novamente.'
                        )
                    elif not _save_extra_sessions(extra_sessions):
                        errors.append(
                            'Outro usuário alterou atendimentos ao mesmo tempo. '
                            'Recarregue a página e tente novamente.'
                        )
                    else:
                        _save_teacher_class_registry(registry)
                        messages.append(
                            f'Turma "{summary["turma_display"]}" transferida de '
                            f'{summary["from_teacher"]} para {summary["to_teacher"]}: '
                            f'{summary["students_updated"]} aluno(s), '
                            f'{summary["lesson_count"]} aula(s) vinculada(s), '
                            f'{summary["extra_sessions_updated"]} atendimento(s) atualizado(s).'
                        )
                        from_teacher = to_teacher = turma = ''
                        turma_options = []
                        preview = None

    return render_template(
        'turma_transfer.html',
        teacher_names=teacher_names,
        from_teacher=from_teacher,
        to_teacher=to_teacher,
        turma=turma,
        turma_options=turma_options,
        preview=preview,
        messages=messages,
        errors=errors,
    )


def _student_transfer_options(students, registry):
    """Origin turmas (from roster) and destination turmas (registry + roster)."""
    from_by_code = {}
    for row in students:
        code = (row.get('turma') or '').strip()
        if not code:
            continue
        opt = from_by_code.setdefault(code, {
            'turma': code,
            'turma_display': (row.get('turma_display') or '').strip() or code,
            'teacher': normalize_teacher_name(row.get('teacher', '')),
            'count': 0,
        })
        opt['count'] += 1
    from_options = sorted(
        from_by_code.values(), key=lambda o: o['turma_display'].casefold(),
    )

    dest_options = []
    seen = set()
    for teacher in sorted(registry.keys(), key=str.casefold):
        for row in list_for_teacher(registry, teacher):
            teacher_name = normalize_teacher_name(teacher)
            pair = (teacher_name.casefold(), row['turma'])
            if pair in seen:
                continue
            seen.add(pair)
            dest_options.append({
                'key': f'{teacher_name}||{row["turma"]}',
                'teacher': teacher_name,
                'turma': row['turma'],
                'turma_display': row['turma_display'],
                'horario': row.get('horario', ''),
            })
    for opt in from_options:
        pair = (opt['teacher'].casefold(), opt['turma'])
        if not opt['teacher'] or pair in seen:
            continue
        seen.add(pair)
        dest_options.append({
            'key': f'{opt["teacher"]}||{opt["turma"]}',
            'teacher': opt['teacher'],
            'turma': opt['turma'],
            'turma_display': opt['turma_display'],
            'horario': '',
        })
    dest_options.sort(key=lambda o: (o['turma_display'].casefold(), o['teacher'].casefold()))
    return from_options, dest_options


@app.route('/admin/alunos/transfer', methods=['GET', 'POST'])
@role_required(ROLE_SUPERADMIN, ROLE_ADMIN)
def student_transfer_page():
    students = _load_students()
    registry = _load_teacher_class_registry()
    log_entries = load_transfer_log(_student_transfers_path())
    messages = []
    errors = []

    from_turma = (request.values.get('from_turma') or '').strip()
    dest_key = (request.values.get('dest') or '').strip()

    if request.method == 'POST' and (request.form.get('action') or '').strip() == 'transfer':
        selected = [n.strip() for n in request.form.getlist('students') if n.strip()]
        dest_teacher, _, dest_code = dest_key.partition('||')
        dest_teacher = normalize_teacher_name(dest_teacher)
        dest_code = dest_code.strip()
        dest_class = find_class(registry, dest_teacher, dest_code) or {}
        dest_info = {
            'turma': dest_code,
            'teacher': dest_teacher,
            'turma_display': (dest_class.get('turma_display') or '').strip(),
            'horario': (dest_class.get('horario') or '').strip(),
        }
        if not dest_info['turma_display']:
            sample = [
                r for r in students
                if (r.get('turma') or '').strip() == dest_code
            ]
            dest_info['turma_display'] = (
                class_display_from_student_rows(sample, dest_code) if sample else dest_code
            )

        monthly_store = _load_monthly_review_store()
        snapshot_store = load_snapshots(SNAPSHOTS_PATH)
        extra_rows = _load_extra_sessions()
        summary, err = transfer_students(
            students, selected, from_turma, dest_info,
            monthly_store, snapshot_store, extra_rows, log_entries,
        )
        if err:
            errors.append(err)
        elif not _save_students(students):
            errors.append(SAVE_CONFLICT_MESSAGE)
        else:
            history_ok = _save_monthly_review_store(monthly_store)
            save_snapshots(SNAPSHOTS_PATH, list(snapshot_store.values()))
            extras_ok = _save_extra_sessions(extra_rows)
            save_transfer_log(_student_transfers_path(), log_entries)
            if not history_ok or not extras_ok:
                errors.append(
                    'Os alunos foram movidos, mas parte do histórico não pôde ser '
                    'salva. Recarregue a página e confira os alunos transferidos.'
                )
            else:
                messages.append(
                    f'{summary["count"]} aluno(s) transferido(s) para '
                    f'{summary["to_turma_display"]} ({summary["to_teacher"]}). '
                    'O histórico de relatórios e evolução foi mantido.'
                )
            students = _load_students()
            from_turma = ''
            dest_key = ''

    from_options, dest_options = _student_transfer_options(students, registry)
    turma_students = []
    if from_turma:
        turma_students = sorted(
            (row for row in students if (row.get('turma') or '').strip() == from_turma),
            key=lambda r: (r.get('student_name') or '').casefold(),
        )

    recent_transfers = list(reversed(log_entries))[:30]
    return render_template(
        'student_transfer.html',
        from_options=from_options,
        dest_options=dest_options,
        from_turma=from_turma,
        dest_key=dest_key,
        turma_students=turma_students,
        recent_transfers=recent_transfers,
        messages=messages,
        errors=errors,
    )


@app.route('/reports')
@login_required
def reports():
    try:
        all_students, students = _scoped_students()
        _, lessons = _scoped_lessons(all_students)
        selected_month = (request.args.get('month') or '').strip()
        available_months = _available_review_months(lessons)
        if not selected_month:
            selected_month = _get_review_month(lessons)
        elif selected_month not in available_months:
            selected_month = _get_review_month(lessons)

        students_for_reports = _students_with_report_aliases(all_students)
        files = filter_reports_for_user(
            sorted(OUT_DIR.glob('*.html')), students_for_reports, _current_user())
        individual = [f for f in files if 'class_diagnostic' not in f.name]
        diagnostics = [f for f in files if 'class_diagnostic' in f.name]

        snapshots = load_snapshots(SNAPSHOTS_PATH)
        user = _current_user()
        report_rows = _build_report_rows(
            individual, diagnostics,
            _students_with_report_aliases(students), lessons, snapshots)
        report_turma_filters = _report_turma_filters(report_rows, students, user)
        months_in_reports = sorted(
            {row['month'] for row in report_rows if row.get('month')},
            reverse=True,
        )

        return render_template(
            'reports.html',
            report_rows=report_rows,
            report_turma_filters=report_turma_filters,
            months_in_reports=months_in_reports,
            turma_labels=_turma_display_map(students, user),
            available_months=available_months,
            selected_month=selected_month,
            default_month=_get_review_month(lessons),
            generate_warning=session.pop('generate_warning', None),
        )
    except Exception as exc:
        logger.exception('Reports page failed: %s', exc)
        session['generate_error'] = (
            'Não foi possível abrir a lista de relatórios. Tente gerar novamente.'
        )
        user = _current_user()
        if user and has_full_data_access(user.get('role', '')):
            session['generate_error_detail'] = str(exc)[:300]
        return redirect(url_for('dashboard'))


def _allowed_report_path(filename):
    path = OUT_DIR / Path(filename).name
    if not path.exists() or path.suffix != '.html':
        return None
    all_students, _ = _scoped_students()
    allowed = filter_reports_for_user(
        [path], _students_with_report_aliases(all_students), _current_user())
    return path if allowed else None


def _live_render_individual_preview(path):
    """Rebuild an individual report from current templates so preview
    reflects template changes without requiring a full generate."""
    name = Path(path).name
    if not name.endswith('_report.html') or 'class_diagnostic' in name:
        return None
    month = report_month_from_filename(name)
    all_students, students = _scoped_students(
        review_month=month, apply_attendance=True,
    )
    _, lessons = _scoped_lessons(all_students)
    turma, student_name = _student_for_report_file(path, students, month)
    if not turma:
        turma, student_name = _student_for_report_file(path, all_students, month)
    if not turma or not student_name:
        return None
    student = None
    for row in list(students) + list(all_students):
        if row.get('_transfer_alias'):
            continue
        if (row.get('turma', '').strip() == turma
                and row.get('student_name', '').strip() == student_name):
            student = row
            break
    if not student:
        return None
    snapshots = load_snapshots(SNAPSHOTS_PATH)
    attendance_rows = _load_lesson_attendance()
    trend = None
    if month:
        base_ctx = build_student_ctx(
            student, lessons, report_month=month,
            attendance_rows=attendance_rows,
        )
        trend = compute_month_trend(
            student_composite_score(base_ctx), month, snapshots,
            turma, student_name,
        )
    ctx = build_student_ctx(
        student, lessons, report_month=month, trend=trend,
        snapshots=snapshots, attendance_rows=attendance_rows,
    )
    if month:
        ctx['report_month_label'] = month_label(month)
    env = create_report_environment(TMPL_DIR)
    return env.get_template('individual_report.html').render(**ctx)


def _live_render_class_preview(path):
    """Rebuild a class diagnostic from current templates so preview
    reflects template changes without requiring a full generate."""
    name = Path(path).name
    if 'class_diagnostic' not in name:
        return None
    turma = _turma_from_diagnostic_filename(name)
    if not turma:
        return None
    month = report_month_from_filename(name)
    all_students, students = _scoped_students(
        review_month=month, apply_attendance=True,
    )
    _, lessons = _scoped_lessons(all_students)
    group = [
        row for row in students
        if not row.get('_transfer_alias')
        and row.get('turma', '').strip() == turma
    ]
    if not group:
        group = [
            row for row in all_students
            if not row.get('_transfer_alias')
            and row.get('turma', '').strip() == turma
        ]
    if not group:
        return None
    snapshots = load_snapshots(SNAPSHOTS_PATH)
    env = create_report_environment(TMPL_DIR)
    ctx = build_class_ctx(
        turma, group, lessons, report_month=month, snapshots=snapshots,
    )
    return env.get_template('class_diagnostic.html').render(**ctx)


@app.route('/reports/preview/<path:filename>')
@login_required
def preview(filename):
    path = _allowed_report_path(filename)
    if not path:
        abort(404)
    try:
        live = _live_render_class_preview(path) or _live_render_individual_preview(path)
        if live:
            return live
    except Exception:
        logger.exception('Live report preview failed for %s', path.name)
    return path.read_text(encoding='utf-8')


@app.route('/reports/download/<path:filename>')
@login_required
def download_report(filename):
    path = _allowed_report_path(filename)
    if not path:
        abort(404)
    return send_file(path, as_attachment=True)


@app.route('/reports/download-all')
@login_required
def download_all():
    all_students, _ = _scoped_students()
    _, lessons = _scoped_lessons(all_students)
    selected_month = (request.args.get('month') or '').strip()
    available_months = available_report_months(lessons)
    if selected_month and selected_month not in available_months:
        selected_month = ''
    files = filter_reports_for_user(
        sorted(OUT_DIR.glob('*.html')),
        _students_with_report_aliases(all_students),
        _current_user(),
    )
    files = filter_report_files_by_month(files, selected_month)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name='mister_wiz_reports.zip')


# ── Teacher chat ──────────────────────────────────────────────────────────────────────

@app.route('/chat')
@login_required
def chat_room():
    user = _current_user()
    if not can_access_chat(user):
        abort(403)
    rows = _load_chat_messages()
    threads = thread_tree(rows)
    last_id = rows[-1]['id'] if rows else ''
    return render_template(
        'chat.html',
        room_title=ROOM_TITLE,
        threads=threads,
        messages=rows,
        last_message_id=last_id,
        open_bugs=open_bug_count(rows),
        chat_flash_ok=session.pop('chat_flash_ok', None),
        chat_flash_error=session.pop('chat_flash_error', None),
    )


@app.route('/chat/post', methods=['POST'])
@login_required
def chat_post():
    user = _current_user()
    if not can_access_chat(user):
        abort(403)
    body = request.form.get('body') or ''
    kind = KIND_BUG if (request.form.get('kind') or '').strip() == KIND_BUG else KIND_CHAT
    parent_id = (request.form.get('parent_id') or '').strip()
    rows = _load_chat_messages()
    msg, err = append_message(rows, user, body, kind=kind, parent_id=parent_id)
    if err:
        session['chat_flash_error'] = err
        return redirect(url_for('chat_room'))
    if not _save_chat_messages(rows):
        session['chat_flash_error'] = (
            session.get('save_conflict')
            or 'Não foi possível enviar (dados desatualizados). Recarregue e tente de novo.'
        )
        return redirect(url_for('chat_room'))
    session['chat_flash_ok'] = (
        'Bug reportado.' if kind == KIND_BUG else 'Mensagem enviada.'
    )
    return redirect(url_for('chat_room') + f'#msg-{msg["id"]}')


@app.route('/chat/<message_id>/resolve', methods=['POST'])
@login_required
def chat_resolve_bug(message_id):
    user = _current_user()
    if not can_access_chat(user):
        abort(403)
    rows = _load_chat_messages()
    msg, err = resolve_bug(rows, message_id, user)
    if err:
        session['chat_flash_error'] = err
        return redirect(url_for('chat_room'))
    if not _save_chat_messages(rows):
        session['chat_flash_error'] = (
            session.get('save_conflict')
            or 'Não foi possível atualizar o bug. Recarregue e tente de novo.'
        )
        return redirect(url_for('chat_room'))
    session['chat_flash_ok'] = f'Bug resolvido ({msg["author_name"]}).'
    return redirect(url_for('chat_room') + f'#msg-{msg["id"]}')


@app.route('/chat/messages.json')
@login_required_json
def chat_messages_json():
    user = _current_user()
    if not can_access_chat(user):
        return jsonify({'ok': False, 'error': 'Sem permissão.'}), 403
    rows = _load_chat_messages()
    after_id = (request.args.get('after') or '').strip()
    newer = messages_after(rows, after_id=after_id)
    return jsonify({
        'ok': True,
        'messages': newer,
        'open_bugs': open_bug_count(rows),
        'last_id': rows[-1]['id'] if rows else after_id,
    })


# ── Teacher profile ───────────────────────────────────────────────────────────────────

def _profile_target_user(user_id=None):
    actor = _current_user()
    if not actor:
        return None, None
    if user_id is None:
        return actor, user_store.get_by_id(actor['id'])
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return actor, None
    target = user_store.get_by_id(uid)
    return actor, target


@app.route('/profile', methods=['GET', 'POST'])
@app.route('/profile/<int:user_id>', methods=['GET', 'POST'])
@login_required
def teacher_profile(user_id=None):
    actor, target = _profile_target_user(user_id)
    if not target:
        abort(404)
    if not can_view_profile(actor, target):
        abort(403)

    target_public = user_public_dict(target)
    editable = can_edit_profile(actor, target['id'])
    profiles = _load_teacher_profiles()
    profile = get_or_empty(profiles, target['id'])
    form_error = None

    if request.method == 'POST':
        if not editable:
            abort(403)
        clear_photo = bool(request.form.get('clear_photo'))
        photo_mime = None
        photo_b64 = None
        upload = request.files.get('photo')
        if upload and upload.filename:
            raw = upload.read()
            photo_mime, photo_b64, photo_err = encode_photo(raw)
            if photo_err:
                form_error = photo_err
        if not form_error:
            updated, err = upsert_profile(
                profiles,
                target['id'],
                bio=request.form.get('bio') or '',
                phone=request.form.get('phone') or '',
                whatsapp=request.form.get('whatsapp') or '',
                contact_email=request.form.get('contact_email') or '',
                specialty=request.form.get('specialty') or '',
                photo_mime=photo_mime,
                photo_base64=photo_b64,
                clear_photo=clear_photo and not (photo_mime and photo_b64),
            )
            if err:
                form_error = err
            elif not _save_teacher_profiles(profiles):
                form_error = (
                    session.get('save_conflict')
                    or 'Não foi possível salvar o perfil. Recarregue e tente de novo.'
                )
            else:
                session['profile_flash_ok'] = 'Perfil atualizado.'
                if user_id is None:
                    return redirect(url_for('teacher_profile'))
                return redirect(url_for('teacher_profile', user_id=target['id']))
        profile = get_or_empty(profiles, target['id'])
        if form_error:
            # Keep submitted text on validation errors.
            profile = {
                **profile,
                'bio': (request.form.get('bio') or '')[:1000],
                'phone': (request.form.get('phone') or '')[:120],
                'whatsapp': (request.form.get('whatsapp') or '')[:120],
                'contact_email': (request.form.get('contact_email') or '')[:120],
                'specialty': (request.form.get('specialty') or '')[:120],
            }

    return render_template(
        'profile.html',
        target_user=target_public,
        profile=profile,
        photo_url=photo_data_url(profile),
        editable=editable,
        is_own_profile=int(actor['id']) == int(target['id']),
        form_error=form_error,
        profile_flash_ok=session.pop('profile_flash_ok', None),
    )


@app.route('/profile/<int:user_id>/photo')
@login_required
def teacher_profile_photo(user_id):
    actor, target = _profile_target_user(user_id)
    if not target or not can_view_profile(actor, target):
        abort(404)
    profile = get_or_empty(_load_teacher_profiles(), user_id)
    if not profile.get('photo_base64') or not profile.get('photo_mime'):
        abort(404)
    try:
        raw = base64.b64decode(profile['photo_base64'], validate=True)
    except Exception:
        abort(404)
    return Response(raw, mimetype=profile['photo_mime'])


def _pick_port(preferred):
    """Use preferred port, or the next free one (macOS AirPlay often blocks 5000)."""
    import socket

    for candidate in range(preferred, preferred + 10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('0.0.0.0', candidate))
            except OSError:
                continue
            return candidate
    return preferred


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    preferred = int(os.environ.get('PORT', '5000'))
    port = _pick_port(preferred)
    if port != preferred:
        logger.warning('Port %s is in use; starting on http://127.0.0.1:%s', preferred, port)
    else:
        logger.info('Starting on http://127.0.0.1:%s', port)
    app.run(debug=debug, host='0.0.0.0', port=port)
