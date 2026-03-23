const { app, BrowserWindow, ipcMain, dialog, screen, shell } = require('electron')
const { spawn, spawnSync } = require('child_process')
const path = require('path')
const fs   = require('fs')

// ---- Config ----------------------------------------------------------------

const CONFIG_PATH = path.join(app.getPath('userData'), 'config.json')

function readConfig() {
  try   { return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8')) }
  catch { return null }
}

function writeConfig(data) {
  fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true })
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(data, null, 2), 'utf8')
}

// ---- Server path -----------------------------------------------------------

function getServerPath() {
  if (app.isPackaged) return path.join(process.resourcesPath, 'server.exe')
  return path.join(__dirname, 'dist', 'server.exe')
}

// ---- Python server ---------------------------------------------------------

let pythonProcess = null
let mainWindow    = null

function sendLog(line) {
  process.stdout.write(line + '\n')
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('server-log', line)
  }
}

function killServerSync(pid) {
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/F', '/T', '/PID', String(pid)], { stdio: 'ignore' })
  } else {
    try { process.kill(-pid, 'SIGTERM') } catch {}
  }
  sendLog(`[server] killed pid ${pid}`)
}

function startServer(libraryPath, projectsPath) {
  if (pythonProcess) {
    const pid = pythonProcess.pid
    pythonProcess = null
    killServerSync(pid)
  }

  const serverExe = getServerPath()
  const args      = ['--library', libraryPath]
  if (projectsPath) args.push('--projects', projectsPath)

  sendLog(`[server] starting  library=${libraryPath}`)
  if (projectsPath) sendLog(`[server] projects=${projectsPath}`)

  const proc = spawn(serverExe, args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,      // prevents console window flash on Windows
    detached: false,
  })
  proc.on('error', err => sendLog(`[server] ERROR: ${err.message}`))
  proc.stdout.on('data', d => d.toString().trim().split('\n').forEach(l => sendLog(`[py] ${l.trim()}`)))
  proc.stderr.on('data', d => d.toString().trim().split('\n').forEach(l => sendLog(`[py:err] ${l.trim()}`)))
  proc.on('exit', code => sendLog(`[server] exited (${code})`))
  pythonProcess = proc
}

// ---- Window ----------------------------------------------------------------

function createWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize
  mainWindow = new BrowserWindow({
    width:           Math.round(width  * 2/3),
    height:          Math.round(height * 2/3),
    minWidth:        820,
    minHeight:       520,
    center:          true,
    frame:           false,
    backgroundColor: '#131415',
    show:            false,
    webPreferences: {
      preload:          path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration:  false,
    },
  })
  mainWindow.loadFile('index.html')
  mainWindow.once('ready-to-show', () => mainWindow.show())
}

// ---- App lifecycle ---------------------------------------------------------

app.whenReady().then(() => {
  const config       = readConfig()
  const libraryPath  = config?.libraryPath  || path.join(app.getPath('home'), 'yt-dl')
  const projectsPath = config?.projectsPath || null

  createWindow()
  startServer(libraryPath, projectsPath)

  // --- IPC -----------------------------------------------------------------

  ipcMain.handle('is-first-run',  () => !readConfig())
  ipcMain.handle('get-config',    () => readConfig())

  // Save library path — restarts server
  ipcMain.handle('save-config', (_, cfg) => {
    writeConfig(cfg)
    startServer(cfg.libraryPath, cfg.projectsPath || null)
    return true
  })

  // Save colors only — never restarts server
  ipcMain.handle('save-colors', (_, colors) => {
    const cfg = readConfig() || {}
    cfg.colorBg     = colors.colorBg
    cfg.colorAccent = colors.colorAccent
    writeConfig(cfg)
    return true
  })

  // Save projects path — restarts server so it picks up --projects arg
  ipcMain.handle('save-projects-path', (_, projectsPath) => {
    const cfg = readConfig() || {}
    cfg.projectsPath = projectsPath
    writeConfig(cfg)
    startServer(cfg.libraryPath || path.join(app.getPath('home'), 'yt-dl'), projectsPath)
    return true
  })

  ipcMain.handle('select-folder', async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      title: 'Choose a folder', properties: ['openDirectory', 'createDirectory'],
    })
    return r.canceled ? null : r.filePaths[0]
  })

  // Audio/video file picker — used by the projects view to import tracks
  ipcMain.handle('select-file', async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      title: 'Import audio file',
      properties: ['openFile'],
      filters: [
        { name: 'Audio / Video', extensions: ['mp3','wav','flac','aac','ogg','m4a','mp4','aiff','wma'] },
        { name: 'All Files', extensions: ['*'] },
      ],
    })
    return r.canceled ? null : r.filePaths[0]
  })

  // Image file picker — used to set project cover art
  ipcMain.handle('select-image', async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      title: 'Choose cover image',
      properties: ['openFile'],
      filters: [
        { name: 'Images', extensions: ['png','jpg','jpeg','webp','gif'] },
      ],
    })
    return r.canceled ? null : r.filePaths[0]
  })
  ipcMain.handle('get-projects-path', () => readConfig()?.projectsPath || null)

  ipcMain.handle('open-folder',   (_, p)   => shell.openPath(p))
  ipcMain.handle('open-file',     (_, p)   => shell.openPath(p))   // opens file in OS default app
  ipcMain.handle('open-external', (_, url) => shell.openExternal(url))

  ipcMain.handle('window-minimize', () => mainWindow?.minimize())
  ipcMain.handle('window-close',    () => mainWindow?.close())
  ipcMain.handle('window-maximize', () => {
    if (mainWindow?.isMaximized()) mainWindow.unmaximize()
    else mainWindow?.maximize()
  })
})

function killServer() {
  if (!pythonProcess) return
  const pid = pythonProcess.pid
  pythonProcess = null
  killServerSync(pid)
}

app.on('before-quit',       killServer)
app.on('will-quit',         killServer)
app.on('window-all-closed', () => { killServer(); if (process.platform !== 'darwin') app.quit() })
app.on('activate',          () => { if (BrowserWindow.getAllWindows().length === 0) createWindow() })