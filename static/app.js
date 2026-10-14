// ==========================================================
// КУБАРЬ & УВАЛЫ — FRONTEND APPLICATION LOGIC
// ==========================================================

const tg = window.Telegram?.WebApp;

if (tg) {
  try {
    tg.ready();
    tg.expand();
    if (tg.setHeaderColor) tg.setHeaderColor('#090d16');
    if (tg.setBackgroundColor) tg.setBackgroundColor('#090d16');
  } catch (e) {
    console.warn("Telegram WebApp initialization warning:", e);
  }
}

// Извлечение chat_id из URL (?chat_id=-100...)
const urlParams = new URLSearchParams(window.location.search);
const currentChatId = parseInt(urlParams.get('chat_id') || '0');

// Глобальное состояние
let state = {
  chat: null,
  members: [],
  knownUsers: [],
  todayDuty: null,
  tomorrowDuty: null,
  forecast7days: [],
  stats: null,
  activeFilter: 'all',
  searchQuery: '',
  activeTab: 'tabDuty'
};

let sortableInstance = null;

// Тактильный отклик (Haptic Feedback)
function haptic(type = 'light') {
  if (tg?.HapticFeedback) {
    try {
      if (type === 'light' || type === 'medium' || type === 'heavy') {
        tg.HapticFeedback.impactOccurred(type);
      } else if (type === 'success' || type === 'warning' || type === 'error') {
        tg.HapticFeedback.notificationOccurred(type);
      }
    } catch (e) {
      // Игнорируем в обычном браузере
    }
  }
}

function showNotification(title, message, isError = false) {
  if (tg?.showPopup) {
    tg.showPopup({ title: title, message: message });
  } else {
    alert(`${title}: ${message}`);
  }
}

// ================= НАВИГАЦИЯ ПО ВКЛАДКАМ =================
const tabButtons = document.querySelectorAll('.tab-btn');
const tabContents = document.querySelectorAll('.tab-content');

function switchTab(targetTabId) {
  haptic('light');
  state.activeTab = targetTabId;

  tabButtons.forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-tab') === targetTabId);
  });

  tabContents.forEach(content => {
    content.classList.toggle('active', content.id === targetTabId);
  });

  if (targetTabId === 'tabLogs') {
    loadLogs();
  } else if (targetTabId === 'tabStats') {
    renderStatsAndForecast();
  }
}

tabButtons.forEach(btn => {
  btn.addEventListener('click', () => {
    switchTab(btn.getAttribute('data-tab'));
  });
});

// Кнопка закрытия
document.getElementById('closeAppBtn')?.addEventListener('click', () => {
  haptic('medium');
  if (tg) {
    tg.close();
  } else {
    window.close();
  }
});

// Кнопка обновления
document.getElementById('refreshBtn')?.addEventListener('click', () => {
  haptic('light');
  loadData();
});

// ================= ЗАГРУЗКА ДАННЫХ =================
async function loadData() {
  try {
    const headers = {};
    if (tg?.initData) {
      headers['X-Telegram-Init-Data'] = tg.initData;
    }

    const res = await fetch(`/api/chat-data?chat_id=${currentChatId}`, { headers });
    if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
    const data = await res.json();

    state.chat = data.chat;
    state.members = data.members || [];
    state.knownUsers = data.known_users || [];
    state.todayDuty = data.today_duty;
    state.tomorrowDuty = data.tomorrow_duty;
    state.forecast7days = data.forecast_7days || [];
    state.stats = data.stats;

    // Обновляем заголовок чата
    const badge = document.getElementById('chatTitleBadge');
    if (badge && state.chat) {
      badge.textContent = state.chat.title || `Чат ID: ${currentChatId}`;
    }

    renderDutyHero();
    renderDutyQueue();
    renderUvalCards();
    renderStatsAndForecast();
    renderKnownUsersSelect();
    updateFilterCounts();

  } catch (err) {
    console.error("Ошибка загрузки данных:", err);
    document.getElementById('dutyQueueList').innerHTML =
      '<div class="loading-state">⚠️ Ошибка связи с сервером. Попробуйте обновить страницу.</div>';
  }
}

// ================= TAB 1: КУБАРЬ =================

function renderDutyHero() {
  const monthsRu = ["", "янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
  const now = new Date();
  const tom = new Date(now.getTime() + 86400000);

  document.getElementById('todayDateLabel').textContent = `${now.getDate()} ${monthsRu[now.getMonth() + 1]}`;
  document.getElementById('tomorrowDateLabel').textContent = `${tom.getDate()} ${monthsRu[tom.getMonth() + 1]}`;

  const today = state.todayDuty;
  const tomorrow = state.tomorrowDuty;

  // Сегодня
  const todayAvatar = document.getElementById('todayAvatar');
  const todayName = document.getElementById('todayName');
  const todaySub = document.getElementById('todaySub');

  if (today) {
    todayAvatar.textContent = today.full_name ? today.full_name.trim().charAt(0).toUpperCase() : '?';
    todayName.textContent = today.full_name;
    todaySub.textContent = `№${today.order_num || 1} в очереди • Смен: ${today.duty_count || 0}`;
  } else {
    todayAvatar.textContent = '—';
    todayName.textContent = 'Не назначен';
    todaySub.textContent = 'Очередь пуста';
  }

  // Завтра
  const tomAvatar = document.getElementById('tomorrowAvatar');
  const tomName = document.getElementById('tomorrowName');
  const tomSub = document.getElementById('tomorrowSub');

  if (tomorrow) {
    tomAvatar.textContent = tomorrow.full_name ? tomorrow.full_name.trim().charAt(0).toUpperCase() : '?';
    tomName.textContent = tomorrow.full_name;
    tomSub.textContent = `№${tomorrow.order_num || 2} в очереди • Смен: ${tomorrow.duty_count || 0}`;
  } else {
    tomAvatar.textContent = '—';
    tomName.textContent = 'Не назначен';
    tomSub.textContent = 'Следующий в очереди';
  }
}

function renderDutyQueue() {
  const container = document.getElementById('dutyQueueList');
  if (!container) return;

  if (state.members.length === 0) {
    container.innerHTML = `
      <div class="loading-state">
        Бойцы не добавлены.<br>
        Нажмите «+ Боец» выше, чтобы добавить личный состав.
      </div>
    `;
    return;
  }

  container.innerHTML = '';

  state.members.forEach((m, index) => {
    const item = document.createElement('div');
    item.className = 'queue-item';
    item.dataset.id = m.id;
    item.dataset.tgId = m.telegram_id || '';

    const uname = m.username ? `@${m.username}` : (m.telegram_id ? `ID: ${m.telegram_id}` : 'ручной ввод');

    item.innerHTML = `
      <div class="queue-left">
        <span class="drag-handle" title="Перетащить">☰</span>
        <span class="order-badge">#${index + 1}</span>
        <div class="queue-names">
          <div class="queue-full-name">${escapeHtml(m.full_name)}</div>
          <div class="queue-username">${escapeHtml(uname)}</div>
        </div>
      </div>
      <div class="queue-right">
        <span class="duty-count-pill">⚡ ${m.duty_count || 0}</span>
        <label class="toggle-wrap" title="В строю / Освобожден">
          <span class="toggle-switch">
            <input type="checkbox" class="active-toggle" ${m.is_active ? 'checked' : ''} data-id="${m.id}">
            <span class="toggle-slider"></span>
          </span>
        </label>
        <button class="btn-del-item" data-id="${m.id}" title="Удалить бойца">✕</button>
      </div>
    `;
    container.appendChild(item);
  });

  // Слушатели переключателя активности
  container.querySelectorAll('.active-toggle').forEach(chk => {
    chk.addEventListener('change', (e) => {
      const id = parseInt(e.target.dataset.id);
      const member = state.members.find(x => x.id === id);
      if (member) {
        member.is_active = e.target.checked ? 1 : 0;
        haptic('light');
        updateFilterCounts();
      }
    });
  });

  // Слушатели удаления
  container.querySelectorAll('.btn-del-item').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const id = parseInt(e.currentTarget.dataset.id);
      deleteMember(id);
    });
  });

  // Инициализация или обновление Sortable
  if (sortableInstance) {
    sortableInstance.destroy();
  }

  sortableInstance = new Sortable(container, {
    handle: '.drag-handle',
    animation: 200,
    ghostClass: 'sortable-ghost',
    chosenClass: 'sortable-chosen',
    onEnd: () => {
      haptic('medium');
      recalculateOrderFromDOM();
    }
  });
}

function recalculateOrderFromDOM() {
  const container = document.getElementById('dutyQueueList');
  const items = container.querySelectorAll('.queue-item');
  const newOrder = [];

  items.forEach((item, index) => {
    const id = parseInt(item.dataset.id);
    const badge = item.querySelector('.order-badge');
    if (badge) badge.textContent = `#${index + 1}`;

    const member = state.members.find(x => x.id === id);
    if (member) {
      member.order_num = index + 1;
      newOrder.push(member);
    }
  });

  state.members = newOrder;
}

async function saveDutyQueue() {
  recalculateOrderFromDOM();
  haptic('medium');

  const btn = document.getElementById('btnSaveQueue');
  const oldText = btn.innerHTML;
  btn.innerHTML = '<span>⏳ Сохранение...</span>';
  btn.disabled = true;

  try {
    const payload = {
      chat_id: currentChatId,
      members: state.members.map(m => ({
        id: m.id,
        telegram_id: m.telegram_id,
        full_name: m.full_name,
        username: m.username,
        order_num: m.order_num,
        is_active: m.is_active
      }))
    };

    const headers = { 'Content-Type': 'application/json' };
    if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;

    const res = await fetch('/api/save-members', {
      method: 'POST',
      headers,
      body: JSON.stringify(payload)
    });

    if (res.ok) {
      haptic('success');
      btn.innerHTML = '<span>✅ Очередь сохранена!</span>';
      setTimeout(() => {
        btn.innerHTML = oldText;
        btn.disabled = false;
      }, 1500);
      await loadData();
    } else {
      throw new Error('Ошибка сохранения');
    }
  } catch (e) {
    haptic('error');
    btn.innerHTML = oldText;
    btn.disabled = false;
    showNotification('Ошибка', 'Не удалось сохранить очередь на сервере.');
  }
}

document.getElementById('btnSaveQueue')?.addEventListener('click', saveDutyQueue);

// Действие: Смена сдана
document.getElementById('btnAdvanceDuty')?.addEventListener('click', async () => {
  haptic('medium');
  if (!confirm('Передать смену следующему бойцу по очереди?')) return;

  try {
    const res = await fetch('/api/duty/advance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: currentChatId })
    });
    if (res.ok) {
      haptic('success');
      await loadData();
    } else {
      showNotification('Ошибка', 'Не удалось переключить смену.');
    }
  } catch (e) {
    showNotification('Ошибка', 'Сбой сети при передаче смены.');
  }
});

// Действие: Замена ответственного
const replaceModal = document.getElementById('replaceDutyModal');

document.getElementById('btnReplaceToday')?.addEventListener('click', () => {
  haptic('light');
  replaceModal.classList.add('open');
});

document.getElementById('modalCloseReplace')?.addEventListener('click', () => {
  replaceModal.classList.remove('open');
});

document.getElementById('btnCancelReplace')?.addEventListener('click', () => {
  replaceModal.classList.remove('open');
});

document.getElementById('btnConfirmReplace')?.addEventListener('click', async () => {
  haptic('medium');
  replaceModal.classList.remove('open');

  try {
    const headers = { 'Content-Type': 'application/json' };
    if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;

    const res = await fetch('/api/duty/replace', {
      method: 'POST',
      headers,
      body: JSON.stringify({ chat_id: currentChatId })
    });
    const data = await res.json();
    if (res.ok) {
      haptic('success');
      showNotification('Успех', `Ответственный заменен на: ${data.new_member?.full_name}`);
      await loadData();
    } else {
      showNotification('Внимание', data.detail || 'Не удалось выполнить замену');
    }
  } catch (e) {
    showNotification('Ошибка', 'Сетевая ошибка при замене');
  }
});

// ================= TAB 2: УВАЛЫ (DUTY PASS) =================

function getFilteredUvalMembers() {
  const q = state.searchQuery.toLowerCase().trim();
  let list = [...state.members];

  if (state.activeFilter === 'active') {
    list = list.filter(m => m.is_active === 1);
  } else if (state.activeFilter === 'with_uval') {
    list = list.filter(m => m.uval_count > 0);
  } else if (state.activeFilter === 'zero') {
    list = list.filter(m => m.uval_count === 0);
  } else if (state.activeFilter === 'limit') {
    list = list.filter(m => m.uval_count >= (m.max_limit || 5));
  }

  if (q) {
    list = list.filter(m =>
      (m.full_name && m.full_name.toLowerCase().includes(q)) ||
      (m.username && m.username.toLowerCase().includes(q))
    );
  }

  return list;
}

function renderUvalCards() {
  const container = document.getElementById('uvalCardsList');
  if (!container) return;

  const filtered = getFilteredUvalMembers();

  if (filtered.length === 0) {
    container.innerHTML = `
      <div class="loading-state">
        Бойцы по фильтру не найдены.<br>
        Используйте кнопку добавления ниже.
      </div>
    `;
    return;
  }

  container.innerHTML = '';

  filtered.forEach(m => {
    const card = document.createElement('div');
    card.className = 'uval-card';

    const initial = m.full_name ? m.full_name.trim().charAt(0).toUpperCase() : '?';
    const uname = m.username ? `@${m.username}` : (m.telegram_id ? `ID: ${m.telegram_id}` : 'ручной ввод');
    const limit = m.max_limit || 5;
    const count = m.uval_count || 0;

    // Индикаторы лимита (5 полосок)
    let pipsHtml = '';
    for (let i = 1; i <= Math.max(5, count); i++) {
      const isFilled = i <= count;
      const isOver = i > 5 && isFilled;
      pipsHtml += `<div class="limit-pip ${isFilled ? 'filled' : ''} ${isOver ? 'overlimit' : ''}"></div>`;
    }

    card.innerHTML = `
      <div class="uval-card-left">
        <div class="uval-avatar">${initial}</div>
        <div class="uval-info">
          <div class="uval-name">${escapeHtml(m.full_name)}</div>
          <div class="uval-sub">${escapeHtml(uname)} • ${m.is_active ? '🟢 В строю' : '⚪ Освобожден'}</div>
          <div class="limit-progress-bar">${pipsHtml}</div>
        </div>
      </div>
      <div class="uval-counter-group">
        <button class="counter-btn btn-uval-minus" data-id="${m.id}" title="Списать увал">−</button>
        <span class="counter-val" id="uvalVal-${m.id}">${count}</span>
        <button class="counter-btn btn-uval-plus" data-id="${m.id}" title="Начислить увал">+</button>
      </div>
    `;

    container.appendChild(card);
  });

  // Слушатели кнопок + и -
  container.querySelectorAll('.btn-uval-minus').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const id = parseInt(e.currentTarget.dataset.id);
      changeUval(id, -1);
    });
  });

  container.querySelectorAll('.btn-uval-plus').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const id = parseInt(e.currentTarget.dataset.id);
      changeUval(id, 1);
    });
  });
}

async function changeUval(memberId, delta) {
  haptic('medium');

  // Оптимистичное обновление UI
  const member = state.members.find(x => x.id === memberId);
  if (member) {
    member.uval_count = Math.max(0, (member.uval_count || 0) + delta);
    const valEl = document.getElementById(`uvalVal-${memberId}`);
    if (valEl) valEl.textContent = member.uval_count;
    renderUvalCards();
    updateFilterCounts();
    renderStatsAndForecast();
  }

  try {
    const res = await fetch('/api/uval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: currentChatId,
        member_id: memberId,
        delta: delta
      })
    });
    const data = await res.json();
    if (data.status === 'ok') {
      if (member) member.uval_count = data.new_count;
    }
  } catch (err) {
    console.error("Ошибка при изменении увала:", err);
  }
}

// Фильтры увалов
document.querySelectorAll('.filter-chips .chip').forEach(chip => {
  chip.addEventListener('click', () => {
    haptic('light');
    document.querySelectorAll('.filter-chips .chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    state.activeFilter = chip.getAttribute('data-filter');
    renderUvalCards();
  });
});

// Поиск
const searchInput = document.getElementById('uvalSearchInput');
const searchClear = document.getElementById('uvalSearchClear');

searchInput?.addEventListener('input', (e) => {
  state.searchQuery = e.target.value;
  searchClear.style.display = state.searchQuery ? 'block' : 'none';
  renderUvalCards();
});

searchClear?.addEventListener('click', () => {
  searchInput.value = '';
  state.searchQuery = '';
  searchClear.style.display = 'none';
  renderUvalCards();
});

function updateFilterCounts() {
  const all = state.members.length;
  const active = state.members.filter(m => m.is_active === 1).length;
  const withUval = state.members.filter(m => (m.uval_count || 0) > 0).length;
  const zero = state.members.filter(m => (m.uval_count || 0) === 0).length;
  const limit = state.members.filter(m => (m.uval_count || 0) >= (m.max_limit || 5)).length;

  document.getElementById('chipAllCount').textContent = all;
  document.getElementById('chipActiveCount').textContent = active;
  document.getElementById('chipWithUvalCount').textContent = withUval;
  document.getElementById('chipZeroCount').textContent = zero;
  document.getElementById('chipLimitCount').textContent = limit;
}

// ================= TAB 3: СВОДКА & ПРОГНОЗ =================

function renderStatsAndForecast() {
  const members = state.members;
  const total = members.length;
  const active = members.filter(m => m.is_active === 1).length;
  const totalUvals = members.reduce((acc, m) => acc + (m.uval_count || 0), 0);
  const totalDuties = members.reduce((acc, m) => acc + (m.duty_count || 0), 0);

  document.getElementById('statTotalMembers').textContent = total;
  document.getElementById('statActiveMembers').textContent = active;
  document.getElementById('statTotalUvals').textContent = totalUvals;
  document.getElementById('statTotalDuties').textContent = totalDuties;

  // 7-day forecast
  const forecastContainer = document.getElementById('forecastCardsContainer');
  if (forecastContainer) {
    if (state.forecast7days.length === 0) {
      forecastContainer.innerHTML = '<div class="loading-state">Нет данных для прогноза</div>';
    } else {
      forecastContainer.innerHTML = '';
      state.forecast7days.forEach((f, idx) => {
        const item = document.createElement('div');
        item.className = `forecast-item ${idx === 0 ? 'is-today' : ''}`;
        item.innerHTML = `
          <div class="forecast-date-box">
            <span class="forecast-day-badge">${escapeHtml(f.day_name)}</span>
            <span style="font-size:12px; color:var(--text-muted);">${f.date}</span>
          </div>
          <div class="forecast-soldier-name">
            ${escapeHtml(f.full_name)} <span style="font-size:11px; color:var(--text-muted);">(№${f.order_num})</span>
          </div>
        `;
        forecastContainer.appendChild(item);
      });
    }
  }

  // Рейтинг по увалам
  const topUvalsList = document.getElementById('topUvalsList');
  if (topUvalsList) {
    const sortedUvals = [...members].sort((a, b) => (b.uval_count || 0) - (a.uval_count || 0)).slice(0, 5);
    if (sortedUvals.length === 0) {
      topUvalsList.innerHTML = '<div class="loading-state">Список пуст</div>';
    } else {
      topUvalsList.innerHTML = sortedUvals.map((m, idx) => `
        <div class="leader-row">
          <span class="leader-rank">${idx + 1}.</span>
          <span class="leader-name">${escapeHtml(m.full_name)}</span>
          <span class="leader-score">${m.uval_count || 0} ув.</span>
        </div>
      `).join('');
    }
  }

  // Рейтинг по дежурствам
  const topDutiesList = document.getElementById('topDutiesList');
  if (topDutiesList) {
    const sortedDuties = [...members].sort((a, b) => (b.duty_count || 0) - (a.duty_count || 0)).slice(0, 5);
    if (sortedDuties.length === 0) {
      topDutiesList.innerHTML = '<div class="loading-state">Список пуст</div>';
    } else {
      topDutiesList.innerHTML = sortedDuties.map((m, idx) => `
        <div class="leader-row">
          <span class="leader-rank">${idx + 1}.</span>
          <span class="leader-name">${escapeHtml(m.full_name)}</span>
          <span class="leader-score">${m.duty_count || 0} смен</span>
        </div>
      `).join('');
    }
  }
}

// ================= TAB 4: ЖУРНАЛ СОБЫТИЙ =================

async function loadLogs() {
  const container = document.getElementById('logsFeedList');
  if (!container) return;

  container.innerHTML = '<div class="loading-state">Загрузка журнала событий...</div>';

  try {
    const res = await fetch(`/api/logs?chat_id=${currentChatId}`);
    const data = await res.json();
    const logs = data.logs || [];

    if (logs.length === 0) {
      container.innerHTML = '<div class="loading-state">Журнал действий пока пуст</div>';
      return;
    }

    container.innerHTML = '';
    logs.forEach(l => {
      const card = document.createElement('div');
      card.className = 'log-item-card';

      let badgeClass = 'duty';
      let badgeText = l.action;

      if (l.action === 'uval_change') {
        if (l.delta > 0) {
          badgeClass = 'uval-plus';
          badgeText = `+${l.delta} ув. (всего ${l.new_count})`;
        } else {
          badgeClass = 'uval-minus';
          badgeText = `${l.delta} ув. (всего ${l.new_count})`;
        }
      } else if (l.action === 'duty_assigned') {
        badgeText = 'Дежурство';
      } else if (l.action === 'duty_replaced') {
        badgeText = 'Замена';
      } else if (l.action === 'member_added') {
        badgeText = 'Добавлен';
      }

      card.innerHTML = `
        <div>
          <div class="log-user">${escapeHtml(l.full_name)}</div>
          <div class="log-detail">${escapeHtml(l.details || '')} • ${escapeHtml(l.created_at)}</div>
        </div>
        <div class="log-badge ${badgeClass}">${badgeText}</div>
      `;
      container.appendChild(card);
    });

  } catch (err) {
    container.innerHTML = '<div class="loading-state">Не удалось загрузить журнал</div>';
  }
}

document.getElementById('btnClearLogs')?.addEventListener('click', async () => {
  haptic('medium');
  if (!confirm('Вы действительно хотите очистить журнал действий?')) return;

  try {
    await fetch('/api/logs/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: currentChatId })
    });
    haptic('success');
    loadLogs();
  } catch (e) {
    showNotification('Ошибка', 'Не удалось очистить журнал');
  }
});

// ================= МОДАЛКА: ДОБАВЛЕНИЕ БОЙЦА =================
const addModal = document.getElementById('addMemberModal');
const tabModeKnown = document.getElementById('tabModeKnown');
const tabModeManual = document.getElementById('tabModeManual');
const panelKnown = document.getElementById('panelKnown');
const panelManual = document.getElementById('panelManual');

let addMode = 'known'; // 'known' или 'manual'

function openAddModal() {
  haptic('medium');
  addModal.classList.add('open');
  if (state.knownUsers.length > 0) {
    setAddMode('known');
  } else {
    setAddMode('manual');
  }
}

function closeAddModal() {
  addModal.classList.remove('open');
  document.getElementById('inputManualName').value = '';
  document.getElementById('inputManualUsername').value = '';
  document.getElementById('selectKnownUsers').value = '';
}

function setAddMode(mode) {
  addMode = mode;
  tabModeKnown.classList.toggle('active', mode === 'known');
  tabModeManual.classList.toggle('active', mode === 'manual');
  panelKnown.classList.toggle('active', mode === 'known');
  panelManual.classList.toggle('active', mode === 'manual');
}

tabModeKnown?.addEventListener('click', () => setAddMode('known'));
tabModeManual?.addEventListener('click', () => setAddMode('manual'));

document.getElementById('btnAddMemberDuty')?.addEventListener('click', openAddModal);
document.getElementById('btnAddMemberUval')?.addEventListener('click', openAddModal);
document.getElementById('modalCloseAdd')?.addEventListener('click', closeAddModal);
document.getElementById('btnCancelAdd')?.addEventListener('click', closeAddModal);

function renderKnownUsersSelect() {
  const select = document.getElementById('selectKnownUsers');
  if (!select) return;

  select.innerHTML = '<option value="">-- Выберите участника чата --</option>';
  if (state.knownUsers.length === 0) {
    select.innerHTML = '<option value="">(Нет новых участников чата)</option>';
    return;
  }

  state.knownUsers.forEach(u => {
    const opt = document.createElement('option');
    opt.value = u.telegram_id;
    opt.textContent = `${u.full_name} ${u.username ? '(@' + u.username + ')' : ''}`;
    select.appendChild(opt);
  });
}

document.getElementById('btnSubmitAdd')?.addEventListener('click', async () => {
  let fullName = '';
  let username = '';
  let tgId = null;

  if (addMode === 'known') {
    const selVal = document.getElementById('selectKnownUsers').value;
    if (!selVal) {
      alert('Пожалуйста, выберите бойца из списка участников');
      return;
    }
    const targetUser = state.knownUsers.find(u => String(u.telegram_id) === String(selVal));
    if (!targetUser) return;
    fullName = targetUser.full_name;
    username = targetUser.username || '';
    tgId = targetUser.telegram_id;
  } else {
    fullName = document.getElementById('inputManualName').value.trim();
    username = document.getElementById('inputManualUsername').value.trim().replace(/^@/, '');
    if (!fullName) {
      alert('Введите фамилию и имя бойца');
      return;
    }
  }

  haptic('success');
  closeAddModal();

  try {
    const res = await fetch('/api/members/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: currentChatId,
        full_name: fullName,
        username: username || null,
        telegram_id: tgId
      })
    });
    if (res.ok) {
      await loadData();
    } else {
      showNotification('Ошибка', 'Не удалось добавить бойца.');
    }
  } catch (err) {
    showNotification('Ошибка', 'Сетевая ошибка при добавлении.');
  }
});

async function deleteMember(memberId) {
  haptic('medium');
  const m = state.members.find(x => x.id === memberId);
  const name = m ? m.full_name : 'бойца';

  if (!confirm(`Удалить ${name} из состава кубаря?`)) return;

  try {
    const res = await fetch(`/api/members/${memberId}?chat_id=${currentChatId}`, {
      method: 'DELETE'
    });
    if (res.ok) {
      haptic('success');
      await loadData();
    } else {
      showNotification('Ошибка', 'Не удалось удалить бойца.');
    }
  } catch (e) {
    showNotification('Ошибка', 'Сетевая ошибка при удалении.');
  }
}

function escapeHtml(str) {
  if (!str) return '';
  const d = document.createElement('div');
  d.innerText = str;
  return d.innerHTML;
}

// Запуск при загрузке страницы
loadData();
