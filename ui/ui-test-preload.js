// UI 测试专用 preload：替代 preload.js，提供 window.jplt 的测试替身。
// 事件由主进程通过 __UI_TEST_EMIT 实时推进来，避免时间上的竞态。
'use strict';

const { contextBridge } = require('electron');

const listeners = [];

const settings = {
  fontSize: 30,
  sourceFontSize: 20,
  showSource: true,
  opacity: 0.82,
  background: '#0b0b10',
  alwaysOnTop: true,
  maxLines: 3,
  historyLines: 300,
  clearAfterMs: 0,
  provider: 'llm',
  asrModel: 'large-v3-turbo',
  asrDevice: 'cuda',
  audioDevice: '',
  gain: 1.0,
  minSilenceMs: 380,
  vadThreshold: 0.5,
  contextSentences: 4,
  apiKey: '',
  baseUrl: '',
  llmModel: ''
};

const commands = [];

contextBridge.exposeInMainWorld('jplt', {
  onEvent: (cb) => {
    listeners.push(cb);
    return () => {
      const i = listeners.indexOf(cb);
      if (i >= 0) listeners.splice(i, 1);
    };
  },
  getSettings: async () => settings,
  setSettings: async (patch) => Object.assign(settings, patch || {}),
  sendCommand: async (cmd) => { commands.push(cmd); return true; },
  backendStatus: async () => ({ running: true, ready: true }),
  restartBackend: async () => true,
  listDevices: async () => ({
    ok: true,
    backends: { chosen: 'pyaudiowpatch', loopback_count: 11 },
    devices: [
      { id: 'pa:70', name: '耳机 (HyperX Cloud III) [Loopback]', samplerate: 48000, is_default: true },
      { id: 'pa:65', name: 'Realtek Digital Output [Loopback]', samplerate: 48000, is_default: false }
    ]
  }),
  setClickThrough: () => {},
  quitApp: () => {},
  openLog: () => {},
  openExternal: () => {}
});

// 主进程用这个把合成事件喂进来
contextBridge.exposeInMainWorld('__uiTest', {
  emit: (msg) => { listeners.forEach((cb) => { try { cb(msg); } catch (_) {} }); },
  listenerCount: () => listeners.length,
  commands: () => commands.slice()
});
