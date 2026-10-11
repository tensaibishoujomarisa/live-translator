// 预加载：只暴露必要的 IPC 接口给字幕窗。
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jplt', {
  onEvent: (cb) => {
    const handler = (_e, msg) => cb(msg);
    ipcRenderer.on('backend-event', handler);
    return () => ipcRenderer.removeListener('backend-event', handler);
  },
  getSettings: () => ipcRenderer.invoke('get-settings'),
  setSettings: (patch) => ipcRenderer.invoke('set-settings', patch),
  sendCommand: (cmd) => ipcRenderer.invoke('send-command', cmd),
  backendStatus: () => ipcRenderer.invoke('backend-status'),
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  listDevices: () => ipcRenderer.invoke('list-devices'),
  setClickThrough: (v) => ipcRenderer.send('set-click-through', v),
  quitApp: () => ipcRenderer.send('quit-app'),
  openLog: () => ipcRenderer.send('open-log'),
  openExternal: (url) => ipcRenderer.send('open-external', url)
});
