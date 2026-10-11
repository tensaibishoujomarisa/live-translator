// Electron 主进程：拉起 Python 后端，把 JSONL 事件转给字幕窗。
'use strict';

// --ui-test：不启动后端，只在真实 Chromium 里加载字幕窗、喂合成事件、断言 DOM。
// 用来验证「事件真的变成了屏幕上的字幕行」，而不是只验证进程起来了。
const IS_UI_TEST = process.argv.includes('--ui-test');

const electron = require('electron');
const { app, BrowserWindow, ipcMain, screen, shell, dialog, globalShortcut } = electron;

// 有些环境（CI、某些启动器）会设置 ELECTRON_RUN_AS_NODE=1，那会让本进程退化成纯 Node，
// require('electron') 只拿到 npm 包的路径字符串，后面所有 app/ipcMain 调用都会炸。
// 检测到就直接报清楚的错，不要丢一个莫名其妙的 TypeError。
if (!electron || !electron.app || !electron.ipcMain) {
  console.error(
    '[fatal] 当前进程不是 Electron 主进程（require("electron") 返回了 ' +
    (typeof electron) + '）。\n' +
    '        最常见原因：环境变量 ELECTRON_RUN_AS_NODE=1。请清掉它再启动：\n' +
    '        set ELECTRON_RUN_AS_NODE=    然后重新运行 03_启动字幕.bat'
  );
  process.exit(1);
}
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const IS_DEV = process.argv.includes('--dev');
// --smoke：拉起窗口和后端，等后端 ready（或超时）后自动退出并打印结果。
// 用来在没有人工盯着的情况下验证 UI 链路是否完好。
const IS_SMOKE = process.argv.includes('--smoke');
const PROJECT_ROOT = path.resolve(__dirname, '..');

// ---------------------------------------------------------------- 找 Python
const REQUIRED_MODULES = ['faster_whisper', 'sounddevice'];

function looksUsable(py) {
  // 只检查文件存在不够：还得确认关键依赖装在这个解释器里，
  // 否则会出现「窗口起来了但后端立刻报 No module named faster_whisper」这种难查的问题
  try {
    const r = spawnSync(py, ['-c', `import ${REQUIRED_MODULES.join(', ')}`], {
      encoding: 'utf8', timeout: 25000, windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }
    });
    return r.status === 0;
  } catch (_) {
    return false;
  }
}

function resolvePython() {
  const exe = process.platform === 'win32' ? 'python.exe' : 'python';
  const candidates = [];
  if (process.env.JP_LIVE_PYTHON) candidates.push(process.env.JP_LIVE_PYTHON);

  // 1) 项目自带的虚拟环境（01_安装依赖.bat 会建在这里）
  candidates.push(
    path.join(PROJECT_ROOT, '.venv', 'Scripts', exe),
    path.join(PROJECT_ROOT, '.venv', 'bin', 'python'),
    path.join(PROJECT_ROOT, 'venv', 'Scripts', exe)
  );
  // 2) 用户可能把 venv 建在别处
  if (process.env.JP_LIVE_TRANSLATOR_HOME) {
    candidates.push(path.join(process.env.JP_LIVE_TRANSLATOR_HOME, '.venv', 'Scripts', exe));
  }
  // 3) 系统 Python
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA || '';
    for (const v of ['Python313', 'Python312', 'Python311', 'Python310']) {
      candidates.push(path.join(local, 'Programs', 'Python', v, exe));
    }
  }

  const existing = candidates.filter((c) => {
    try { return c && fs.existsSync(c); } catch (_) { return false; }
  });

  // 优先挑依赖齐全的那个
  for (const c of existing) {
    if (looksUsable(c)) return { python: c, verified: true };
  }
  if (existing.length) return { python: existing[0], verified: false };

  // 4) 交给 PATH
  const probe = spawnSync(exe, ['--version'], { encoding: 'utf8', windowsHide: true });
  if (probe.status === 0) {
    return { python: exe, verified: looksUsable(exe) };
  }
  return { python: null, verified: false };
}

// ---------------------------------------------------------------- 状态
let win = null;
let child = null;
let childReady = false;
let clickThrough = false;
const smokeResult = { ready: null, errors: [], windowLoaded: false, preloadOk: false };
let settings = {
  fontSize: 30,
  sourceFontSize: 20,
  showSource: true,
  opacity: 0.82,
  background: '#0b0b10',
  alwaysOnTop: true,
  maxLines: 3,
  targetLang: '简体中文',
  provider: '',          // 留空表示「听 config.yaml 的」，避免 UI 默认值反过来覆盖配置
  asrModel: '',          // 同上：真正加载前会用 config.yaml 的值填上
  asrDevice: '',
  audioDevice: '',
  gain: 1.0,
  minSilenceMs: 380,
  vadThreshold: 0.5,
  contextSentences: 4,
  apiKey: '',
  baseUrl: 'https://api.deepseek.com',
  llmModel: 'deepseek-chat'
};
const SETTINGS_FILE = path.join(PROJECT_ROOT, 'ui-settings.json');

// 极简 YAML 读取：支持缩进层级的 a.b 取值，够读 config.yaml 这种结构。
// 只解析 "key: value" 与 "section:"，不处理嵌套列表/多行字符串。
function readYamlValue(text, dottedKey) {
  const path = String(dottedKey).split('.');
  const lines = String(text).split(/\r?\n/);
  let depth = 0;
  let matched = 0;
  for (const raw of lines) {
    const line = raw.replace(/#.*$/, '');
    if (!line.trim()) continue;
    const indent = line.length - line.trimStart().length;
    const m = /^([A-Za-z_][\w-]*)\s*:\s*(.*)$/.exec(line.trim());
    if (!m) continue;
    const level = Math.floor(indent / 2);
    if (level > matched) continue;         // 比目标更深的层，跳过
    if (level < matched) { depth = level; matched = level; }
    const isSection = m[2].trim() === '';
    if (isSection) {
      if (m[1] === path[depth] && depth < path.length - 1) {
        depth += 1;
        matched = depth;
      }
      continue;
    }
    if (m[1] === path[depth] && depth === path.length - 1) {
      let val = m[2].trim().replace(/^["']|["']$/g, '');
      if (/^-?\d+(\.\d+)?$/.test(val)) return Number(val);
      if (val === 'true') return true;
      if (val === 'false') return false;
      return val;
    }
  }
  return undefined;
}

// 从 config.yaml 读「后端行为」类默认值。
// 关键：UI 会把这些值当命令行参数传给后端，而命令行优先级高于配置文件；
// 所以这里必须**真的读配置文件**，否则 UI 的硬编码默认值会把 config.yaml 覆盖掉。
function readBackendDefaults() {
  const out = {};
  try {
    const p = path.join(PROJECT_ROOT, 'config.yaml');
    if (!fs.existsSync(p)) return out;
    const text = fs.readFileSync(p, 'utf8');
    const map = {
      provider: 'translate.provider',
      targetLang: 'translate.target_lang',
      asrModel: 'asr.model',
      asrDevice: 'asr.device',
      audioDevice: 'audio.device',
      gain: 'audio.gain',
      vadThreshold: 'vad.threshold',
      minSilenceMs: 'vad.min_silence_ms',
      contextSentences: 'translate.context_sentences',
      baseUrl: 'translate.llm.base_url',
      llmModel: 'translate.llm.model'
    };
    for (const [key, dotted] of Object.entries(map)) {
      const v = readYamlValue(text, dotted);
      if (v !== undefined && v !== '') out[key] = v;
    }
  } catch (_) { /* 读不到就用内置默认值 */ }
  return out;
}

// 从 config.yaml 的 subtitle 段读默认显示参数（极简 YAML 解析，只认 key: value）
function readSubtitleDefaults() {
  const out = {};
  try {
    const p = path.join(PROJECT_ROOT, 'config.yaml');
    if (!fs.existsSync(p)) return out;
    const lines = fs.readFileSync(p, 'utf8').split(/\r?\n/);
    let inSub = false;
    for (const raw of lines) {
      const line = raw.replace(/#.*$/, '');
      if (/^subtitle:\s*$/.test(line)) { inSub = true; continue; }
      if (inSub) {
        if (/^\S/.test(line)) break; // 出了 subtitle 段
        const m = /^\s+([A-Za-z_]+)\s*:\s*(.+?)\s*$/.exec(line);
        if (!m) continue;
        const key = m[1];
        let val = m[2].replace(/^["']|["']$/g, '');
        if (/^-?\d+(\.\d+)?$/.test(val)) val = Number(val);
        else if (val === 'true') val = true;
        else if (val === 'false') val = false;
        const camel = key.replace(/_([a-z])/g, (_s, c) => c.toUpperCase());
        out[camel] = val;
      }
    }
  } catch (_) { /* 用默认值 */ }
  return out;
}

function loadSettings() {
  // 优先级：内置默认 < config.yaml < ui-settings.json（用户上一次在界面里改的）
  settings = { ...settings, ...readBackendDefaults(), ...readSubtitleDefaults() };
  try {
    if (fs.existsSync(SETTINGS_FILE)) {
      const j = JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf8'));
      // 只覆盖用户显式改过的键，没写过的键留给 config.yaml
      for (const [k, v] of Object.entries(j || {})) {
        if (v !== undefined && v !== null) settings[k] = v;
      }
    }
  } catch (e) { /* 坏文件就当默认值 */ }
  if (settings.clearAfterMs == null) settings.clearAfterMs = 25000;
  if (!settings.asrModel) settings.asrModel = 'large-v3-turbo';
  if (!settings.asrDevice) settings.asrDevice = 'cuda';
  if (!settings.provider) settings.provider = 'llm';
}
function saveSettings() {
  try { fs.writeFileSync(SETTINGS_FILE, JSON.stringify(settings, null, 2), 'utf8'); } catch (_) {}
}

// ---------------------------------------------------------------- 窗口
function createWindow() {
  const { workArea } = screen.getPrimaryDisplay();
  const w = Math.min(1200, Math.round(workArea.width * 0.72));
  const h = 240;
  win = new BrowserWindow({
    width: w,
    height: h,
    x: workArea.x + Math.round((workArea.width - w) / 2),
    y: workArea.y + workArea.height - h - 60,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: true,
    movable: true,
    skipTaskbar: false,
    alwaysOnTop: settings.alwaysOnTop,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false
    }
  });

  if (settings.alwaysOnTop) win.setAlwaysOnTop(true, 'screen-saver');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  win.on('closed', () => { win = null; });
  if (IS_DEV) win.webContents.openDevTools({ mode: 'detach' });

  win.webContents.on('did-finish-load', () => {
    smokeResult.windowLoaded = true;
    pushToRenderer({ kind: 'settings', settings });
    pushToRenderer({ kind: 'status', stage: 'ui', message: child ? '后端运行中' : '等待后端启动' });
    if (childReady) pushToRenderer({ kind: 'backend', ready: true });
  });
  win.webContents.on('preload-error', (_e, p, err) => {
    smokeResult.errors.push(`preload: ${p} ${err && err.message}`);
  });
}

function pushToRenderer(msg) {
  if (win && !win.isDestroyed()) {
    try { win.webContents.send('backend-event', msg); } catch (_) {}
  }
}

function setClickThrough(on) {
  clickThrough = !!on;
  if (win && !win.isDestroyed()) {
    win.setIgnoreMouseEvents(clickThrough, { forward: true });
    pushToRenderer({ kind: 'clickthrough', value: clickThrough });
  }
}

// ---------------------------------------------------------------- Python 后端
function buildArgs() {
  const args = ['-u', '-m', 'backend.main', '--stdin-commands', '--serve'];
  args.push('--model', settings.asrModel);
  args.push('--device', settings.asrDevice);
  if (settings.provider) args.push('--provider', settings.provider);
  if (settings.targetLang) args.push('--target-lang', settings.targetLang);
  if (settings.audioDevice) args.push('--audio-device', settings.audioDevice);
  if (settings.gain != null) args.push('--gain', String(settings.gain));
  if (settings.apiKey) args.push('--api-key', settings.apiKey);
  if (settings.baseUrl) args.push('--base-url', settings.baseUrl);
  if (settings.llmModel) args.push('--llm-model', settings.llmModel);
  // 这几个后端也支持命令行覆盖，带上可以少依赖一次运行时 tune
  if (settings.minSilenceMs != null) args.push('--min-silence', String(settings.minSilenceMs));
  if (settings.contextSentences != null) args.push('--context-sentences', String(settings.contextSentences));
  return args;
}

// 不需要重启后端就能生效的参数，用 tune 命令热更新
function liveTuneArgs() {
  const cmds = [];
  const parts = [];
  if (settings.minSilenceMs != null) parts.push(`vad.min_silence_ms=${Math.round(settings.minSilenceMs)}`);
  if (settings.vadThreshold != null) parts.push(`vad.threshold=${settings.vadThreshold}`);
  if (settings.gain != null) parts.push(`audio.gain=${settings.gain}`);
  if (parts.length) cmds.push('tune ' + parts.join(' '));
  if (settings.contextSentences != null) {
    cmds.push(`context ${Math.round(settings.contextSentences)}`);
  }
  return cmds;
}

function startBackend() {
  const resolved = resolvePython();
  const py = resolved.python;
  if (!py) {
    pushToRenderer({
      kind: 'error', where: 'python',
      message: '找不到 Python。请先运行「01_安装依赖.bat」创建 .venv，或设置环境变量 JP_LIVE_PYTHON 指向 python.exe。'
    });
    return;
  }
  if (!resolved.verified) {
    pushToRenderer({
      kind: 'warn',
      message: `${py} 里缺少 faster-whisper / sounddevice，后端可能起不来。请运行「01_安装依赖.bat」重装依赖。`
    });
  }
  pushToRenderer({ kind: 'status', stage: 'spawn', message: `启动后端：${py}` });

  const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' };
  // 运行期不主动设置代理：识别全本地，翻译直连 API（用户配了系统代理也不影响）
  delete env.HTTP_PROXY; delete env.HTTPS_PROXY; delete env.ALL_PROXY;
  delete env.http_proxy; delete env.https_proxy; delete env.all_proxy;
  // ELECTRON_RUN_AS_NODE 若被置上，会传染给子进程里的 Node 行为，一并清掉
  delete env.ELECTRON_RUN_AS_NODE;
  env.HF_HOME = env.HF_HOME || path.join(PROJECT_ROOT, 'models');

  child = spawn(py, buildArgs(), {
    cwd: PROJECT_ROOT,
    env,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe']
  });

  const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  rl.on('line', (line) => {
    const t = line.trim();
    if (!t) return;
    let msg;
    try { msg = JSON.parse(t); } catch (_) {
      if (IS_DEV) console.log('[py:raw]', t);
      return;
    }
    if (msg.kind === 'ready') {
      childReady = true;
      if (IS_SMOKE) smokeResult.ready = msg;
      // 后端就绪后把「不用重启就能生效」的参数推过去
      setTimeout(() => {
        for (const cmd of liveTuneArgs()) sendCommand(cmd);
      }, 400);
    }
    if (IS_SMOKE && msg.kind === 'error') {
      smokeResult.errors.push(`${msg.where}: ${msg.message}`);
    }
    pushToRenderer(msg);
  });

  const erl = readline.createInterface({ input: child.stderr, crlfDelay: Infinity });
  erl.on('line', (line) => {
    const t = line.trim();
    if (!t) return;
    if (IS_DEV) console.log('[py]', t);
    pushToRenderer({ kind: 'log', text: t });
  });

  child.on('exit', (code, signal) => {
    childReady = false;
    pushToRenderer({
      kind: 'backend', ready: false,
      message: `后端已退出 (code=${code}${signal ? ', signal=' + signal : ''})`
    });
    child = null;
  });

  child.on('error', (err) => {
    pushToRenderer({ kind: 'error', where: 'spawn', message: String(err && err.message ? err.message : err) });
  });
}

function sendCommand(cmd) {
  if (child && child.stdin && child.stdin.writable) {
    try { child.stdin.write(cmd + '\n'); return true; } catch (_) { return false; }
  }
  return false;
}

function stopBackend() {
  if (!child) return;
  try { sendCommand('quit'); } catch (_) {}
  const c = child;
  setTimeout(() => { try { if (c && !c.killed) c.kill(); } catch (_) {} }, 1500);
}

function restartBackend() {
  pushToRenderer({ kind: 'status', stage: 'restart', message: '重启后端以应用设置…' });
  const old = child;
  if (old) {
    // 先摘掉 exit 监听：这次是我们主动杀的，不要弹出「后端已退出」误导用户
    try { old.removeAllListeners('exit'); } catch (_) {}
    stopBackend();
  }
  setTimeout(() => {
    if (old && !old.killed) {
      try { old.kill(); } catch (_) {}
    }
    startBackend();
  }, 2500);
}

// ---------------------------------------------------------------- IPC
ipcMain.handle('get-settings', () => settings);

// 应用一批设置。抽成函数是为了让 IPC 和自动化测试走**完全同一条**代码路径。
function applySettings(patch) {
  const before = JSON.stringify(settings);
  settings = { ...settings, ...(patch || {}) };
  saveSettings();
  pushToRenderer({ kind: 'settings', settings });

  // 立即生效的显示类设置
  if (win && !win.isDestroyed()) {
    if (patch && 'alwaysOnTop' in patch) {
      win.setAlwaysOnTop(!!settings.alwaysOnTop, settings.alwaysOnTop ? 'screen-saver' : 'normal');
    }
    if (patch && 'opacity' in patch) win.setOpacity(Number(settings.opacity) || 0.82);
    if (patch && 'clickThrough' in patch) setClickThrough(!!settings.clickThrough);
  }

  // 需要重启后端的设置：模型/设备/翻译后端这类换不了热的
  const needsRestart = ['asrModel', 'asrDevice', 'provider', 'audioDevice', 'gain', 'apiKey',
    'baseUrl', 'llmModel', 'targetLang'];
  const prev = JSON.parse(before);
  const changedKeys = patch
    ? Object.keys(patch).filter((k) => JSON.stringify(patch[k]) !== JSON.stringify(prev[k]))
    : [];
  const restartNeeded = changedKeys.some((k) => needsRestart.includes(k));

  // 不需要重启的（断句灵敏度、上下文句数、阈值）直接热更新
  const liveKeys = ['minSilenceMs', 'vadThreshold', 'contextSentences'];
  if (!restartNeeded && changedKeys.some((k) => liveKeys.includes(k))) {
    for (const cmd of liveTuneArgs()) sendCommand(cmd);
  }

  if (restartNeeded) restartBackend();
  return settings;
}

ipcMain.handle('set-settings', (_e, patch) => applySettings(patch));

ipcMain.handle('send-command', (_e, cmd) => {
  const name = String(cmd || '').trim();
  // UI 自身的命令（后端不认识）
  if (name === 'hide-window') {
    if (win) win.hide();
    return true;
  }
  if (name === 'show-window') {
    if (win) { win.show(); win.focus(); }
    return true;
  }
  if (name === 'clear') {
    pushToRenderer({ kind: 'clear' });
    return true;
  }
  return sendCommand(name);
});

ipcMain.handle('backend-status', () => ({ running: !!child, ready: childReady }));

ipcMain.handle('restart-backend', () => { restartBackend(); return true; });

ipcMain.handle('list-devices', () => {
  const py = resolvePython().python;
  if (!py) return { ok: false, error: '找不到 Python' };
  const r = spawnSync(py, ['-m', 'backend.main', '--list-devices', '--json'], {
    cwd: PROJECT_ROOT, encoding: 'utf8', timeout: 40000,
    env: {
      ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1',
      HF_HOME: path.join(PROJECT_ROOT, 'models')
    }
  });
  const out = (r.stdout || '').trim();
  if (!out) {
    return { ok: false, error: (r.stderr || '没有输出').slice(-400) };
  }
  try {
    return { ok: true, ...JSON.parse(out) };
  } catch (_) {
    return { ok: false, error: out.slice(-400) };
  }
});

ipcMain.on('set-click-through', (_e, v) => setClickThrough(!!v));

ipcMain.on('quit-app', () => { stopBackend(); setTimeout(() => app.quit(), 800); });

ipcMain.on('open-external', (_e, url) => { try { shell.openExternal(url); } catch (_) {} });

ipcMain.on('open-log', () => {
  const p = path.join(PROJECT_ROOT, 'logs', 'jp-live-translator.log');
  if (fs.existsSync(p)) shell.showItemInFolder(p);
  else dialog.showMessageBox({ message: '还没有日志文件', detail: p });
});

// ---------------------------------------------------------------- 生命周期
function mainInit() {
  loadSettings();
  createWindow();
  if (win) win.setOpacity(Number(settings.opacity) || 0.82);
  startBackend();

  if (IS_SMOKE) {
    // 可选：顺便验证「在界面里改设置 -> 后端按新参数重启」这条链路。
    // 用法：set JP_LIVE_SMOKE_SWITCH_PROVIDER=off
    const switchTo = process.env.JP_LIVE_SMOKE_SWITCH_PROVIDER || '';
    // 冒烟测试会写 ui-settings.json，跑完要还原，别污染用户真实设置
    let settingsBackup = null;
    if (switchTo) {
      try {
        settingsBackup = fs.existsSync(SETTINGS_FILE)
          ? fs.readFileSync(SETTINGS_FILE, 'utf8') : null;
      } catch (_) {}
    }
    const restoreSettings = () => {
      if (!switchTo) return;
      try {
        if (settingsBackup !== null) fs.writeFileSync(SETTINGS_FILE, settingsBackup, 'utf8');
        else if (fs.existsSync(SETTINGS_FILE)) fs.unlinkSync(SETTINGS_FILE);
      } catch (_) {}
    };
    let switched = false;
    const t0 = Date.now();
    const timer = setInterval(() => {
      // 第一轮 ready 之后触发一次设置变更
      if (switchTo && smokeResult.ready && !switched) {
        switched = true;
        const first = smokeResult.ready.translator;
        const before = smokeResult.ready;
        console.log('SMOKE_SWITCH from=' + first + ' to=' + switchTo);
        smokeResult.ready = null;               // 等第二次 ready
        applySettings({ provider: switchTo });  // 和界面点「保存」走同一条路径
        setTimeout(() => {
          smokeResult.switch = {
            from: first,
            to: switchTo,
            beforeAsr: before && before.asr,
            after: smokeResult.ready
          };
        }, 1200);
        return;
      }

      const done = (smokeResult.ready && (!switchTo || smokeResult.switch))
        || Date.now() - t0 > 180000;
      if (done) {
        clearInterval(timer);
        smokeResult.windowLoaded = !!(win && !win.isDestroyed());
        restoreSettings();
        console.log('SMOKE_RESULT ' + JSON.stringify({
          windowLoaded: smokeResult.windowLoaded,
          backendReady: !!smokeResult.ready,
          ready: smokeResult.ready,
          switch: smokeResult.switch || null,
          errors: smokeResult.errors
        }));
        stopBackend();
        setTimeout(() => app.quit(), 1200);
      }
    }, 500);
  }

  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
}

// 全局快捷键：即使焦点在浏览器看直播也能操作
function registerShortcuts() {
  const reg = (accel, fn) => { try { globalShortcut.register(accel, fn); } catch (_) {} };
  reg('CommandOrControl+Shift+J', () => { if (win) win.show(); });
  reg('CommandOrControl+Shift+H', () => { if (win) win.isVisible() ? win.hide() : win.show(); });
  reg('CommandOrControl+Shift+P', () => sendCommand('toggle-pause'));
  reg('CommandOrControl+Shift+M', () => setClickThrough(!clickThrough));
  reg('CommandOrControl+Shift+Up', () => {
    settings.fontSize = Math.min(64, (settings.fontSize || 30) + 2);
    saveSettings(); pushToRenderer({ kind: 'settings', settings });
  });
  reg('CommandOrControl+Shift+Down', () => {
    settings.fontSize = Math.max(12, (settings.fontSize || 30) - 2);
    saveSettings(); pushToRenderer({ kind: 'settings', settings });
  });
}

if (IS_UI_TEST) {
  // UI 渲染验收：完全交给 ui-test.js，不要拉后端、不要注册快捷键
  require('./ui-test.js');
} else {
  app.whenReady().then(() => {
    mainInit();
    registerShortcuts();
  });

  app.on('window-all-closed', () => { stopBackend(); app.quit(); });
  app.on('before-quit', () => { stopBackend(); });
  process.on('exit', () => { stopBackend(); });
}
