/**
 * Auto-save for student edit form — server sync + localStorage draft if session expires.
 */
(function () {
  const form = document.getElementById('student-edit-form');
  if (!form) return;

  const cfg = form.dataset;
  const draftKey = [
    'mw-student-draft',
    cfg.autosaveUser || 'anon',
    cfg.autosaveIdx || 'new',
    cfg.autosaveMonth || '',
  ].join(':');

  const statusEl = document.getElementById('autosave-status');
  const bannerEl = document.getElementById('draft-restore-banner');
  const autosaveUrl = cfg.autosaveUrl || '';
  const DEBOUNCE_MS = 2000;
  const LOCAL_DEBOUNCE_MS = 400;

  let serverTimer = null;
  let localTimer = null;
  let dirty = false;
  let saving = false;

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.dataset.kind = kind || '';
  }

  function serializeForm() {
    const data = {};
    new FormData(form).forEach((value, key) => {
      data[key] = value;
    });
    return data;
  }

  function applyFormData(data) {
    Object.entries(data).forEach(([key, value]) => {
      const el = form.elements.namedItem(key);
      if (!el) return;
      if (el instanceof RadioNodeList) {
        for (const node of el) {
          if (node.value === value) node.checked = true;
        }
        return;
      }
      if (el.type === 'checkbox') {
        el.checked = Boolean(value);
        return;
      }
      el.value = value;
    });
    document.querySelectorAll('.score-picker').forEach((picker) => {
      const fieldId = picker.id.replace('picker-', '');
      const hidden = document.getElementById('val-' + fieldId);
      if (!hidden) return;
      const val = hidden.value;
      picker.querySelectorAll('.score-btn').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.val === val);
      });
    });
  }

  function saveLocalDraft() {
    try {
      const payload = {
        savedAt: new Date().toISOString(),
        data: serializeForm(),
      };
      localStorage.setItem(draftKey, JSON.stringify(payload));
    } catch (e) {
      /* ignore quota errors */
    }
  }

  function clearLocalDraft() {
    try {
      localStorage.removeItem(draftKey);
    } catch (e) {
      /* ignore */
    }
  }

  function loadLocalDraft() {
    try {
      const raw = localStorage.getItem(draftKey);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function formFingerprint(data) {
    return JSON.stringify(data);
  }

  function scheduleLocalSave() {
    clearTimeout(localTimer);
    localTimer = setTimeout(saveLocalDraft, LOCAL_DEBOUNCE_MS);
  }

  async function saveToServer() {
    if (!autosaveUrl || saving) return;
    saving = true;
    setStatus('Salvando…', 'pending');
    try {
      const response = await fetch(autosaveUrl, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: { Accept: 'application/json', 'X-Auto-Save': '1' },
        redirect: 'manual',
      });
      if (response.type === 'opaqueredirect' || response.status === 401 || response.status === 302) {
        saveLocalDraft();
        setStatus('Sessão expirada — rascunho salvo neste dispositivo', 'warn');
        dirty = false;
        return;
      }
      let body = {};
      try {
        body = await response.json();
      } catch (e) {
        body = {};
      }
      if (response.ok && body.ok) {
        clearLocalDraft();
        dirty = false;
        setStatus(`Salvo automaticamente às ${body.saved_at || 'agora'}`, 'ok');
        return;
      }
      if (body.login_required) {
        saveLocalDraft();
        setStatus('Sessão expirada — rascunho salvo neste dispositivo', 'warn');
        dirty = false;
        return;
      }
      if (body.conflict) {
        setStatus(body.error || 'Conflito — recarregue a página', 'error');
        return;
      }
      setStatus(body.error || 'Não foi possível salvar', 'error');
    } catch (e) {
      saveLocalDraft();
      setStatus('Sem conexão — rascunho salvo neste dispositivo', 'warn');
    } finally {
      saving = false;
    }
  }

  function scheduleServerSave() {
    if (!autosaveUrl) {
      scheduleLocalSave();
      setStatus('Rascunho local ativo', 'local');
      return;
    }
    clearTimeout(serverTimer);
    serverTimer = setTimeout(saveToServer, DEBOUNCE_MS);
  }

  function markDirty() {
    dirty = true;
    scheduleLocalSave();
    scheduleServerSave();
    if (autosaveUrl) setStatus('Alterações não salvas…', 'dirty');
    else setStatus('Rascunho local…', 'dirty');
  }

  function maybeOfferDraftRestore() {
    const draft = loadLocalDraft();
    if (!draft || !draft.data) return;
    const current = serializeForm();
    if (formFingerprint(draft.data) === formFingerprint(current)) {
      clearLocalDraft();
      return;
    }
    if (!bannerEl) return;
    const when = draft.savedAt
      ? new Date(draft.savedAt).toLocaleString('pt-BR', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' })
      : '';
    bannerEl.querySelector('[data-draft-when]').textContent = when;
    bannerEl.hidden = false;
    document.getElementById('draft-restore-btn')?.addEventListener('click', () => {
      applyFormData(draft.data);
      bannerEl.hidden = true;
      markDirty();
      saveToServer();
    });
    document.getElementById('draft-discard-btn')?.addEventListener('click', () => {
      clearLocalDraft();
      bannerEl.hidden = true;
    });
  }

  form.addEventListener('input', markDirty);
  form.addEventListener('change', markDirty);
  form.addEventListener('submit', () => {
    clearLocalDraft();
    setStatus('Salvando…', 'pending');
  });

  window.addEventListener('beforeunload', () => {
    if (dirty) saveLocalDraft();
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden' && dirty) {
      saveLocalDraft();
      if (autosaveUrl && navigator.sendBeacon) {
        const data = new FormData(form);
        navigator.sendBeacon(autosaveUrl, data);
      }
    }
  });

  if (cfg.autosaveIdx === 'new') {
    clearLocalDraft();
  } else {
    maybeOfferDraftRestore();
  }
  if (autosaveUrl) setStatus('Salvamento automático ativo', 'ok');
  else setStatus('Rascunho local ativo (novo aluno)', 'local');

  window.mwStudentFormGuard = function () {
    if (!dirty) return true;
    return window.confirm(
      'Você tem alterações não salvas neste aluno. Trocar o mês de revisão mesmo assim?'
    );
  };
})();
