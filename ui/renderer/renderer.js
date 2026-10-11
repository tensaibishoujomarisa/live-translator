// 字幕窗逻辑：接收后端事件，维护「行」列表。
'use strict';

const api = window.jplt;

const el = (id) => document.getElementById(id);
const subsEl = el('subs');
const hintEl = el('hint');
const dotEl = el('dot');
const statusEl = el('status');
const levelFill = el('level-fill');
const rtfEl = el('rtf');
const panelEl = el('panel');

let settings = {};
let rows = [];           // {id, ja, zh, state} —— 按时间正序，最后一个是当前行
let currentId = 0;
let partialKey = 0;      // partial 行的本地 key，保证「当前句」始终渲染在同一行
let lastPartialSeg = -1;
let lastEventAt = 0;
let clearTimer = null;
let paused = false;
let clickThrough = false;
let statsSnapshot = null;
// 是否跟随最新字幕（用户往上滚回看历史时置 false）
let stickToBottom = true;
// 后端「当前实际在跑什么」——用来确认设置真的生效了
const runningInfo = { asr: '', translator: '', audio: '' };

function updateRunningInfo() {
  const box = el('running-info');
  if (!box) return;
  const bits = [];
  if (runningInfo.translator) bits.push(`翻译 ${runningInfo.translator}`);
  if (runningInfo.asr) bits.push(`识别 ${runningInfo.asr}`);
  if (runningInfo.audio) bits.push(`音源 ${runningInfo.audio}`);
  box.textContent = bits.length ? '当前：' + bits.join(' ｜ ') : '当前：等待后端…';
  box.title = box.textContent;
}

// ---------------------------------------------------------------- 行管理
function ensureHintGone() {
  if (hintEl && hintEl.parentNode) hintEl.parentNode.removeChild(hintEl);
}

function makeRow(id) {
  const div = document.createElement('div');
  div.className = 'line partial';
  div.dataset.id = String(id);

  const ja = document.createElement('div');
  ja.className = 'ja';

  const zh = document.createElement('div');
  zh.className = 'zh pending';

  // 「越用越准」入口：悬停时出现，点开可以直接改这条译文
  const fix = document.createElement('button');
  fix.className = 'fix-btn';
  fix.textContent = '✏';
  fix.title = '改这条译文（改过之后它会记住）';
  fix.onclick = (e) => { e.stopPropagation(); openFixPop(id); };

  div.appendChild(fix);
  div.appendChild(ja);
  div.appendChild(zh);

  const row = { id, el: div, jaEl: ja, zhEl: zh, ja: '', zh: '', meta: null };
  subsEl.appendChild(div);
  rows.push(row);
  return row;
}

function findRow(id, create) {
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].id === id) return rows[i];
  }
  return create ? makeRow(id) : null;
}

function currentRow() {
  if (!rows.length) return makeRow(0);
  return rows[rows.length - 1];
}

function setMeta(row) {
  if (!row.meta) {
    row.meta = document.createElement('div');
    row.meta.className = 'meta';
    row.el.appendChild(row.meta);
  }
  const bits = [];
  if (row.asrMs != null) bits.push(`识别 ${row.asrMs}ms`);
  if (row.rtf != null) bits.push(`x${row.rtf}`);
  if (row.mtMs != null) bits.push(`翻译 ${row.mtMs}ms`);
  if (row.zhParts > 1) bits.push(`${row.zhParts} 句`);
  row.meta.textContent = bits.join(' · ');
  row.meta.style.display = bits.length ? '' : 'none';
}

// 把两句中文译文拼成一行。日语句末通常没有标点，中文直接相连会糊在一起，
// 所以前一句没有句末标点时补一个逗号。
const ZH_SENT_END = '。！？!?…';
function joinZh(a, b) {
  if (!a) return b;
  if (!b) return a;
  const left = a.replace(/\s+$/, '');
  const right = b.replace(/^\s+/, '');
  if (!left) return right;
  if (!right) return left;

  // 两边一模一样就别拼第二遍（后端重复投递时会走到这里，
  // 屏幕上会显示成「字幕制作人 字幕制作人」）
  if (left === right) return left;

  // 已经是「前一句 + 后一句」的完整形态，就别再追加
  if (right.startsWith(left)) return right;
  if (left.endsWith(right)) return left;

  if (ZH_SENT_END.includes(left[left.length - 1])) return left + right;
  if ('，,、；;：:'.includes(left[left.length - 1])) return left + right;
  if (ZH_SENT_END.includes(right[0]) || '，,、；;：:'.includes(right[0])) return left + right;
  return left + '，' + right;
}

function trimRows() {
  // 可见行数由 maxLines 控制，但 DOM 里保留更多历史供回看
  const keep = Math.max(
    Math.max(1, Number(settings.maxLines) || 3),
    Math.max(10, Number(settings.historyLines) || 300)
  );
  if (rows.length <= keep) return;

  // 从顶部裁掉时，把被裁掉的高度补回滚动位置，
  // 否则正在回看历史的用户会被"往上跳"
  const beforeTop = subsEl.scrollTop;
  const beforeHeight = subsEl.scrollHeight;
  let removed = 0;
  while (rows.length > keep) {
    const r = rows.shift();
    if (r && r.el && r.el.parentNode) r.el.parentNode.removeChild(r.el);
    removed++;
  }
  if (removed && !stickToBottom) {
    const delta = beforeHeight - subsEl.scrollHeight;
    subsEl.scrollTop = Math.max(0, beforeTop - delta);
  }
}

// ---------------------------------------------------------------- 修正（越用越准）
let fixTarget = null;   // 正在修正的行
let fixCloseTimer = null;

function openFixPop(rowId) {
  // 先取消上一次保存留下的自动关闭定时器。
  // 否则「保存后 0.9 秒内想再改一条」会被上一个定时器莫名关掉弹层
  // （实测就是这个原因导致空值校验测试失败）。
  if (fixCloseTimer) { clearTimeout(fixCloseTimer); fixCloseTimer = null; }

  const row = findRow(rowId, false);
  if (!row) return;
  if (!row.ja && !row.zh) return;
  fixTarget = row;
  el('fix-ja').textContent = row.ja || '(无日文原文)';
  el('fix-wrong').textContent = row.zh || '(还没出译文)';
  const input = el('fix-right');
  input.value = row.zh || '';
  el('fix-as-term').checked = false;
  el('fix-note').textContent = '';
  el('fix-pop').classList.remove('hidden');
  // 让用户能直接开始打字
  setTimeout(() => { input.focus(); input.select(); }, 30);
}

function closeFixPop() {
  if (fixCloseTimer) { clearTimeout(fixCloseTimer); fixCloseTimer = null; }
  el('fix-pop').classList.add('hidden');
  fixTarget = null;
}

async function saveFix() {
  if (!fixTarget) return;
  const right = el('fix-right').value.trim();
  const ja = fixTarget.ja || '';
  const wrong = fixTarget.zh || '';
  const note = el('fix-note');

  if (!right) {
    note.textContent = '请填写正确的译文';
    return;
  }
  if (right === wrong) {
    note.textContent = '和原来一样，没有需要记住的东西';
    return;
  }
  if (!ja) {
    note.textContent = '这条没有日文原文，没法作为参考';
    return;
  }

  const asTerm = el('fix-as-term').checked;
  note.textContent = '保存中…';
  const res = await api.correct({ ja, wrong, right, asTerm });
  if (!res || !res.ok) {
    note.textContent = '保存失败：' + ((res && res.error) || '未知错误');
    return;
  }

  // 立刻反映到界面上，让用户看到确实生效了
  fixTarget.zh = right;
  fixTarget.zhEl.textContent = right;
  setMeta(fixTarget);
  note.textContent = asTerm
    ? '已记住，并提示加到术语表'
    : '已记住，以后同类句子会自动参考';
  if (fixCloseTimer) clearTimeout(fixCloseTimer);
  fixCloseTimer = setTimeout(closeFixPop, 900);
}

// ---------------------------------------------------------------- 滚动与跟随
// 默认跟随最新字幕；用户往上滚回看历史时停住，滚回底部再恢复。
function isAtBottom(slack = 40) {
  return subsEl.scrollHeight - subsEl.scrollTop - subsEl.clientHeight <= slack;
}

function scrollDown(force) {
  if (!force && !stickToBottom) return;
  subsEl.scrollTop = subsEl.scrollHeight;
}

function onScroll() {
  const atBottom = isAtBottom();
  const wasSticking = stickToBottom;
  stickToBottom = atBottom;
  el('btn-bottom').classList.toggle('hidden', atBottom);
  if (atBottom && !wasSticking) {
    // 刚回到最新，恢复跟随
    scrollDown(true);
  }
  updateScrollHint();
}

function updateScrollHint() {
  const box = el('scroll-hint');
  if (!box) return;
  if (stickToBottom) {
    box.classList.add('hidden');
    return;
  }
  // 用 rect 比较而不是 offsetTop：offsetTop 相对 offsetParent，
  // 不含 margin-top:auto 撑出的那段空白，滚到最顶时会算错。
  const top = subsEl.getBoundingClientRect().top + 4;
  const above = rows.filter((r) => {
    if (!r.el) return false;
    return r.el.getBoundingClientRect().bottom <= top;
  }).length;
  box.textContent = above > 0 ? `↑ 还有 ${above} 条更早的` : '已到最早';
  box.classList.remove('hidden');
}

function jumpToBottom() {
  stickToBottom = true;
  scrollDown(true);
  onScroll();
}

function scheduleClear() {
  if (clearTimer) clearTimeout(clearTimer);
  const ms = Number(settings.clearAfterMs) || 25000;
  if (ms <= 0) return;
  clearTimer = setTimeout(() => {
    // 只有确实很久没新内容才清；正在回看历史时不清
    if (Date.now() - lastEventAt >= ms && stickToBottom) clearAll();
  }, ms + 500);
}

function clearAll() {
  rows.forEach((r) => r.el && r.el.parentNode && r.el.parentNode.removeChild(r.el));
  rows = [];
  currentId = 0;
  stickToBottom = true;
  el('btn-bottom').classList.add('hidden');
  updateScrollHint();
}

// ---------------------------------------------------------------- 事件处理
function onEvent(msg) {
  if (!msg || typeof msg !== 'object') return;
  lastEventAt = Date.now();

  switch (msg.kind) {
    case 'settings':
      settings = msg.settings || {};
      applyAppearance();
      fillPanel();
      if (msg.settings) settings._loaded = true;
      break;

    case 'status':
      setStatus(msg.stage, msg.message);
      // 翻译后端切换后会来一条这样的消息，顺手更新「当前在跑什么」
      if (msg.stage === 'mt' && /翻译后端/.test(msg.message || '')) {
        const m = /翻译后端[:：]\s*(.+)$/.exec(msg.message);
        if (m) { runningInfo.translator = m[1].trim(); updateRunningInfo(); }
      }
      break;

    case 'partial': {
      ensureHintGone();
      const seg = msg.id || 0;
      // 每个新句子分配一个新 key，同一个句子的反复推测覆盖同一行
      if (seg && seg !== lastPartialSeg) {
        partialKey = seg;
        lastPartialSeg = seg;
      } else if (!partialKey) {
        partialKey = 1;
      }
      let row = findRow(partialKey, false);
      if (!row || row.state === 'final' || row.state === 'error') {
        row = makeRow(partialKey);
      }
      row.state = 'partial';
      row.ja = msg.text || '';
      row.jaEl.textContent = row.ja;
      row.el.classList.add('partial');
      applyAppearance();
      trimRows();
      scrollDown();
      break;
    }

    case 'final': {
      ensureHintGone();
      const id = msg.id || 0;
      currentId = id;
      let row = findRow(id, true);
      row.state = 'final';
      // 定稿可能比翻译先到（常见），也可能在 partial/翻译之后重跑。
      // 重新定稿意味着这一行的内容整体换新，之前的译文拼接作废。
      row.ja = msg.text || row.ja;
      row.jaEl.textContent = row.ja;
      row.el.classList.remove('partial');
      row.asrMs = msg.compute_ms;
      row.rtf = msg.rtf;
      row.zh = '';
      row.zhBase = '';
      row.zhParts = 0;
      row.zhEl.textContent = '';
      row.zhEl.classList.add('pending');
      setMeta(row);
      applyAppearance();
      trimRows();
      scrollDown();
      scheduleClear();
      break;
    }

    case 'translation': {
      ensureHintGone();
      const id = msg.id || 0;
      let row = findRow(id, false);
      if (!row) {
        // 翻译先到（少见）：先建行占位
        row = makeRow(id);
        row.state = 'final';
        row.el.classList.remove('partial');
      }
      const incoming = msg.text || '';
      const prev = row.zh || '';
      // 一段识别结果会被切成多句翻译，每条都带同一个 id。
      // 后端从第二句起发的是「累积文本」，所以：
      //   新文本以旧文本开头  -> 直接替换（累积更新）
      //   否则                -> 追加（拼接多句）
      // 早期版本这里是直接覆盖，导致观众只看得到最后一句。
      if (!prev || incoming === prev) {
        row.zh = incoming;
      } else if (incoming.startsWith(prev)) {
        row.zh = incoming;
      } else if (prev.startsWith(incoming)) {
        row.zh = prev;
      } else {
        row.zh = joinZh(prev, incoming);
      }
      row.zhEl.textContent = row.zh;
      row.zhEl.classList.remove('pending');
      row.mtMs = msg.latency_ms;
      if (msg.part) row.zhParts = msg.part;
      setMeta(row);
      applyAppearance();
      trimRows();
      scrollDown();
      break;
    }

    case 'level': {
      const pct = Math.min(100, Math.round(Math.sqrt(Math.max(0, msg.rms || 0)) * 420));
      levelFill.style.width = pct + '%';
      if (!paused) {
        dotEl.className = 'dot ' + (msg.speaking ? 'speaking' : (dotEl.classList.contains('error') ? 'error' : 'listen'));
      }
      break;
    }

    case 'ready':
      setStatus('ready', `就绪 · ${msg.asr || ''} ${msg.device ? '(' + msg.device + ')' : ''} · ${msg.translator || ''}`);
      dotEl.className = 'dot listen';
      runningInfo.asr = `${msg.asr || '?'} / ${msg.device || '?'}`;
      runningInfo.translator = msg.translator || '?';
      const a = msg.audio || {};
      runningInfo.audio = a.name ? `${a.name} @${a.samplerate}Hz` : '未识别';
      updateRunningInfo();
      break;

    case 'stats': {
      statsSnapshot = msg;
      const a = msg.asr_stats || {};
      if (a.calls) {
        const rtf = a.audio_s > 0 ? (a.compute_s / a.audio_s) : 0;
        rtfEl.textContent = `x${rtf.toFixed(2)} · ${a.calls}句 · 丢${msg.dropped || 0}`;
      }
      break;
    }

    case 'asr_drop':
      // 被幻觉过滤器干掉的片段：不显示，只在日志里有
      break;

    case 'warn':
      setStatus('warn', msg.message);
      break;

    case 'error':
      setStatus('error', `${msg.where || ''}: ${msg.message || ''}`);
      dotEl.className = 'dot error';
      addErrorLine(msg.message);
      break;

    case 'backend':
      if (msg.ready === false) {
        setStatus('stopped', msg.message || '后端已停止');
        dotEl.className = 'dot error';
      }
      break;

    case 'server':
      // SSE 端口信息，静默
      break;

    case 'log':
      // 后端 stderr，仅在开发时看
      break;

    case 'clear':
      clearAll();
      break;

    case 'learn_stats': {
      const s = (msg && msg.stats) || {};
      const n = Number(s.total || 0);
      const sug = (s.suggestions || []).length;
      el('learn-note').textContent = s.enabled === false
        ? '修正功能未启用'
        : `已记住 ${n} 条` + (sug ? `，其中 ${sug} 条建议加进术语表` : '');
      break;
    }

    case 'terms_promoted': {
      const a = Number(msg.added || 0);
      const u = Number(msg.updated || 0);
      const sk = Number(msg.skipped || 0);
      el('learn-note').textContent =
        (a || u)
          ? `已写入术语表：新增 ${a} 条、更新 ${u} 条，共 ${msg.total} 条`
          : `没有需要写入的（跳过 ${sk} 条）`;
      break;
    }

    case 'corrected':
      // 保存成功后的回执（弹层里已经给了即时反馈，这里不重复弹）
      break;

    case 'clickthrough':
      clickThrough = !!msg.value;
      el('btn-click').classList.toggle('active', clickThrough);
      break;

    default:
      break;
  }
}

function addErrorLine(text) {
  ensureHintGone();
  const div = document.createElement('div');
  div.className = 'line err';
  const zh = document.createElement('div');
  zh.className = 'zh';
  zh.textContent = String(text || '').slice(0, 300);
  div.appendChild(zh);
  subsEl.appendChild(div);
  rows.push({ id: -Date.now(), el: div, jaEl: null, zhEl: zh, ja: '', zh: '', state: 'error' });
  trimRows();
  scrollDown();
}

function setStatus(stage, message) {
  statusEl.textContent = message || stage || '';
  if (stage === 'error') { dotEl.className = 'dot error'; return; }
  if (stage === 'paused') { dotEl.className = 'dot paused'; paused = true; el('btn-pause').classList.add('active'); return; }
  if (stage === 'running' || stage === 'ready') {
    paused = false;
    el('btn-pause').classList.remove('active');
    if (!dotEl.classList.contains('error')) dotEl.className = 'dot listen';
    return;
  }
  if (stage === 'init' || stage === 'asr' || stage === 'mt' || stage === 'audio' || stage === 'spawn' || stage === 'restart') {
    if (!paused) dotEl.className = 'dot loading';
  }
}

// ---------------------------------------------------------------- 外观
function applyAppearance() {
  const root = document.documentElement.style;
  root.setProperty('--fs', `${Number(settings.fontSize) || 30}px`);
  root.setProperty('--sfs', `${Number(settings.sourceFontSize) || 20}px`);
  root.setProperty('--op', String(settings.opacity != null ? settings.opacity : 0.82));
  if (settings.background) root.setProperty('--bg', hexToRgb(settings.background));
  const showSrc = settings.showSource !== false;
  document.querySelectorAll('.line').forEach((l) => {
    const ja = l.querySelector('.ja');
    if (ja) ja.style.display = showSrc ? '' : 'none';
  });
}

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(hex || '').trim());
  if (!m) return '11, 11, 16';
  return `${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}`;
}

// ---------------------------------------------------------------- 设置面板
function fillPanel() {
  const set = (id, v) => { const n = el(id); if (n && v != null && !n.matches(':focus')) n.value = v; };
  const check = (id, v) => { const n = el(id); if (n) n.checked = !!v; };
  set('set-gain', settings.gain);
  set('set-minSilence', settings.minSilenceMs);
  set('set-vadThreshold', settings.vadThreshold);
  set('set-maxLines', settings.maxLines);
  set('set-historyLines', settings.historyLines);
  set('set-asrModel', settings.asrModel);
  set('set-asrDevice', settings.asrDevice);
  set('set-provider', settings.provider);
  set('set-baseUrl', settings.baseUrl);
  set('set-llmModel', settings.llmModel);
  set('set-apiKey', settings.apiKey);
  set('set-contextSentences', settings.contextSentences);
  set('set-fontSize', settings.fontSize);
  set('set-sourceFontSize', settings.sourceFontSize);
  set('set-opacity', settings.opacity);
  check('set-showSource', settings.showSource !== false);
  check('set-alwaysOnTop', settings.alwaysOnTop !== false);
  updateLabels();
}

function updateLabels() {
  el('v-gain').textContent = Number(el('set-gain').value).toFixed(1);
  el('v-sil').textContent = el('set-minSilence').value;
  el('v-vth').textContent = Number(el('set-vadThreshold').value).toFixed(2);
  el('v-ctx').textContent = el('set-contextSentences').value;
  el('v-fs').textContent = el('set-fontSize').value;
  el('v-sfs').textContent = el('set-sourceFontSize').value;
  el('v-op').textContent = Number(el('set-opacity').value).toFixed(2);
  const ml = el('set-maxLines'); if (ml) el('v-ml').textContent = ml.value;
  const hl = el('set-historyLines'); if (hl) el('v-hl').textContent = hl.value;
}

function collectPanel() {
  return {
    gain: Number(el('set-gain').value),
    minSilenceMs: Number(el('set-minSilence').value),
    vadThreshold: Number(el('set-vadThreshold').value),
    asrModel: el('set-asrModel').value,
    asrDevice: el('set-asrDevice').value,
    provider: el('set-provider').value,
    baseUrl: el('set-baseUrl').value.trim(),
    llmModel: el('set-llmModel').value.trim(),
    apiKey: el('set-apiKey').value.trim(),
    contextSentences: Number(el('set-contextSentences').value),
    maxLines: Number(el('set-maxLines').value),
    historyLines: Number(el('set-historyLines').value),
    fontSize: Number(el('set-fontSize').value),
    sourceFontSize: Number(el('set-sourceFontSize').value),
    opacity: Number(el('set-opacity').value),
    showSource: el('set-showSource').checked,
    alwaysOnTop: el('set-alwaysOnTop').checked
  };
}

async function refreshDevices() {
  const sel = el('set-audioDevice');
  sel.innerHTML = '<option value="">跟随系统默认输出设备（推荐）</option>';
  const res = await api.listDevices();
  if (!res || !res.ok) {
    const opt = document.createElement('option');
    opt.value = ''; opt.disabled = true;
    opt.textContent = '扫描失败：' + String((res && res.error) || '未知错误').slice(0, 80);
    sel.appendChild(opt);
    return;
  }
  const devs = res.devices || [];
  if (!devs.length) {
    const opt = document.createElement('option');
    opt.value = ''; opt.disabled = true;
    opt.textContent = '没有找到回环采集设备（需要 PyAudioWPatch）';
    sel.appendChild(opt);
    return;
  }
  devs.forEach((d) => {
    const opt = document.createElement('option');
    opt.value = d.id;                     // 形如 pa:70
    opt.textContent = `${d.name}${d.is_default ? ' ← 默认' : ''} (${d.samplerate}Hz)`;
    sel.appendChild(opt);
  });
  if (settings.audioDevice) {
    // 兼容用户手填的设备名
    const hit = devs.find((d) => d.id === settings.audioDevice
      || d.name.toLowerCase().includes(String(settings.audioDevice).toLowerCase()));
    sel.value = hit ? hit.id : '';
  }
  const backends = res.backends || {};
  el('dev-note').textContent = backends.chosen
    ? `采集方式：${backends.chosen}｜可用回环端点 ${backends.loopback_count} 个`
    : '';
}

// ---------------------------------------------------------------- 交互
function bindUI() {
  // 滚动：用户往上滚就停止跟随，滚回底部恢复
  subsEl.addEventListener('scroll', onScroll, { passive: true });
  el('btn-bottom').onclick = (e) => { e.stopPropagation(); jumpToBottom(); };

  el('btn-set').onclick = () => {
    panelEl.classList.toggle('hidden');
    if (!panelEl.classList.contains('hidden')) {
      refreshDevices();
      fillPanel();
    }
  };
  el('btn-close-panel').onclick = () => panelEl.classList.add('hidden');

  el('btn-pause').onclick = async () => {
    await api.sendCommand('toggle-pause');
    paused = !paused;
    el('btn-pause').classList.toggle('active', paused);
    el('btn-pause').textContent = paused ? '▶' : '⏸';
  };

  el('btn-clear').onclick = () => clearAll();

  el('btn-click').onclick = () => {
    clickThrough = !clickThrough;
    api.setClickThrough(clickThrough);
    el('btn-click').classList.toggle('active', clickThrough);
  };

  el('btn-top').onclick = async () => {
    const next = !(settings.alwaysOnTop !== false);
    await api.setSettings({ alwaysOnTop: next });
    el('btn-top').classList.toggle('active', next);
  };

  el('btn-hide').onclick = () => api.sendCommand('hide-window');

  el('btn-refresh-dev').onclick = () => refreshDevices();

  // 修正弹层
  el('fix-cancel').onclick = () => closeFixPop();
  el('fix-save').onclick = () => saveFix();
  el('fix-right').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveFix(); }
    if (e.key === 'Escape') { e.preventDefault(); closeFixPop(); }
  });

  // 顺手回个「攒了多少修正」的提示
  el('btn-learn').onclick = async () => {
    el('learn-note').textContent = '查询中…';
    const s = await api.learnStats();
    if (!s || !s.ok) el('learn-note').textContent = '后端未运行';
    // 真正的数字由 learn_stats 事件回来后再填
  };

  // 把候选写进术语表（只处理程序自己判定「反复出现」的那些）
  el('btn-promote').onclick = async () => {
    if (!api.promoteTerms) return;
    el('learn-note').textContent = '写入中…';
    const r = await api.promoteTerms([]);
    if (!r || !r.ok) el('learn-note').textContent = '后端未运行';
    // 结果由 terms_promoted / status 事件回来后再填
  };

  ['set-gain', 'set-minSilence', 'set-vadThreshold', 'set-contextSentences', 'set-fontSize',
   'set-sourceFontSize', 'set-opacity', 'set-maxLines', 'set-historyLines']
    .forEach((id) => { const n = el(id); if (n) n.addEventListener('input', updateLabels); });

  el('set-fontSize').addEventListener('input', () => {
    document.documentElement.style.setProperty('--fs', `${el('set-fontSize').value}px`);
  });
  el('set-sourceFontSize').addEventListener('input', () => {
    document.documentElement.style.setProperty('--sfs', `${el('set-sourceFontSize').value}px`);
  });
  el('set-opacity').addEventListener('input', () => {
    document.documentElement.style.setProperty('--op', el('set-opacity').value);
  });
  el('set-showSource').addEventListener('change', () => {
    const show = el('set-showSource').checked;
    document.querySelectorAll('.line .ja').forEach((n) => { n.style.display = show ? '' : 'none'; });
  });

  el('btn-apply').onclick = async () => {
    const patch = collectPanel();
    const dev = el('set-audioDevice').value;
    if (dev) patch.audioDevice = dev;
    el('save-note').textContent = '已保存，正在重启后端…';
    await api.setSettings(patch);
    setTimeout(() => { el('save-note').textContent = '已生效。'; }, 2600);
  };

  el('btn-openlog').onclick = () => api.openLog();
  el('btn-quit').onclick = () => api.quitApp();

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') panelEl.classList.add('hidden');
  });
}

// ---------------------------------------------------------------- 启动
(async function init() {
  bindUI();
  api.onEvent(onEvent);

  const s = await api.getSettings();
  settings = s || {};
  applyAppearance();
  fillPanel();
  el('v-gain').textContent = Number(settings.gain || 1).toFixed(1);
  el('btn-top').classList.toggle('active', settings.alwaysOnTop !== false);

  const st = await api.backendStatus();
  if (!st || !st.running) {
    setStatus('error', '后端未运行（看设置里的日志）');
  }
  lastEventAt = Date.now();
  scheduleClear();
})();
