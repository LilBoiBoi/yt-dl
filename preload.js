const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  isFirstRun:        ()              => ipcRenderer.invoke('is-first-run'),
  getConfig:         ()              => ipcRenderer.invoke('get-config'),
  saveConfig:        (cfg)           => ipcRenderer.invoke('save-config', cfg),
  saveColors:        (colors)        => ipcRenderer.invoke('save-colors', colors),
  saveProjectsPath:  (p)             => ipcRenderer.invoke('save-projects-path', p),

  selectFolder:      ()              => ipcRenderer.invoke('select-folder'),
  selectFile:        ()              => ipcRenderer.invoke('select-file'),
  selectImage:       ()              => ipcRenderer.invoke('select-image'),

  getLibraryPath:    ()              => ipcRenderer.invoke('get-library-path'),
  getProjectsPath:   ()              => ipcRenderer.invoke('get-projects-path'),

  openExternal:      (url)           => ipcRenderer.invoke('open-external', url),
  openFolder:        (path)          => ipcRenderer.invoke('open-folder', path),
  openFile:          (path)          => ipcRenderer.invoke('open-file', path),

  windowMinimize:    ()              => ipcRenderer.invoke('window-minimize'),
  windowMaximize:    ()              => ipcRenderer.invoke('window-maximize'),
  windowClose:       ()              => ipcRenderer.invoke('window-close'),

  onServerLog:       (cb)            => ipcRenderer.on('server-log', (_, line) => cb(line)),
})