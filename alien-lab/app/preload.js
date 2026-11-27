// 画面（ui/index.html）から Mac の機能を使うための窓口
const { contextBridge, ipcRenderer, webUtils } = require('electron');

let initial = null;
try { initial = ipcRenderer.sendSync('al:initial'); } catch {}

contextBridge.exposeInMainWorld('alienApp', {
  initial,
  version: (() => { try { return ipcRenderer.sendSync('al:version'); } catch { return ''; } })(),
  pathForFile: (f) => { try { return webUtils.getPathForFile(f) || ''; } catch { return ''; } },
  mediaURL: (p) => 'alien://app/media/' + encodeURIComponent(p),
  save: (json, saveAs, name) => ipcRenderer.invoke('al:save', json, saveAs, name),
  openDialog: () => ipcRenderer.invoke('al:open'),
  newWindow: () => ipcRenderer.invoke('al:new'),
  backup: (json, name) => ipcRenderer.invoke('al:backup', json, name),
  chooseFolder: () => ipcRenderer.invoke('al:folder'),
  setDirty: (v) => ipcRenderer.send('al:dirty', !!v),
  recovery: (json) => ipcRenderer.send('al:recovery', json || ''),
  closeWindow: () => ipcRenderer.send('al:close'),
  onMenu: (cb) => ipcRenderer.on('al:menu', (e, cmd) => cb(cmd)),
  remote: {
    info: () => ipcRenderer.invoke('al:rinfo'),
    state: (body) => ipcRenderer.invoke('al:rstate', body),
    frame: (buf) => ipcRenderer.send('al:rframe', buf),
    onCmd: (cb) => ipcRenderer.on('al:rcmd', (e, m) => cb(m)),
  },
});
